import asyncio
from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from api import binance_p2p, bybit_p2p, okx_p2p, wallet_p2p
from utils.spread import calc_spread, format_ad
from keyboards import fiat_menu, asset_menu, trade_type_menu, ads_list_menu, book_menu, spread_fiat_menu
from handlers.filters import get_filter, set_filter, clear_filter, filter_summary
from handlers.blacklist import is_blacklisted, add_to_blacklist
from config import (
    PAYMENT_METHODS_BINANCE, PAYMENT_METHODS_BYBIT,
    PAYMENT_METHODS_OKX, PAYMENT_METHODS_WALLET,
    PAYMENT_LABELS,
)

router = Router()

_ads_cache: dict[str, list] = {}

_EX_LABELS = {
    "binance": "🟡 Binance",
    "bybit":   "🟠 Bybit",
    "okx":     "🔵 OKX",
    "wallet":  "💎 TG Wallet",
}

def _ex_label(exchange: str) -> str:
    return _EX_LABELS.get(exchange, exchange.title())

AMOUNT_PRESETS = {
    "KZT": [5_000, 10_000, 30_000, 50_000, 100_000],
    "RUB": [1_000, 5_000, 10_000, 30_000, 50_000],
    "TRY": [500, 1_000, 5_000, 10_000],
    "USD": [100, 500, 1_000, 5_000],
}


def sort_ads(ads: list, sort: str, reverse: bool = False) -> list:
    if sort == "completion":
        return sorted(ads, key=lambda x: x["completion"], reverse=not reverse)
    if sort == "volume":
        return sorted(ads, key=lambda x: x["available"], reverse=not reverse)
    return sorted(ads, key=lambda x: x["price"], reverse=reverse)


def format_orderbook(buy_ads: list, sell_ads: list, asset: str, fiat: str, exchange: str) -> str:
    """
    buy_ads  — объявления где ТЫ ПОКУПАЕШЬ (мерчант продаёт) → цены ВЫШЕ
    sell_ads — объявления где ТЫ ПРОДАЁШЬ (мерчант покупает) → цены НИЖЕ
    """
    ex = _ex_label(exchange)
    lines = [f"{ex} | {asset}/{fiat}\n"]
    lines.append("<code>")
    lines.append(f"{'📗 Купить':<14}│ 📕 Продать")
    lines.append("─" * 31)
    rows = max(len(buy_ads), len(sell_ads))
    for i in range(rows):
        # Покупка: сортируем по возрастанию (дешевле = лучше для покупателя)
        buy_price  = f"{buy_ads[i]['price']:>11,.2f}" if i < len(buy_ads) else " " * 11
        # Продажа: сортируем по убыванию (дороже = лучше для продавца)
        sell_price = f"{sell_ads[i]['price']:>11,.2f}" if i < len(sell_ads) else ""
        lines.append(f"{buy_price}  │ {sell_price}")
    lines.append("─" * 31)
    if buy_ads and sell_ads:
        best_buy  = buy_ads[0]["price"]   # наименьшая цена покупки
        best_sell = sell_ads[0]["price"]  # наибольшая цена продажи
        s = calc_spread(best_sell, best_buy)
        lines.append(f"Спред: {s['spread_abs']:,.2f} ({s['spread_pct']}%)")
    buy_vol  = sum(a["available"] for a in buy_ads)
    sell_vol = sum(a["available"] for a in sell_ads)
    if buy_vol or sell_vol:
        lines.append(f"Объём:  {buy_vol:>8.2f} │ {sell_vol:.2f} {asset}")
    lines.append("</code>")
    return "\n".join(lines)


async def fetch_ads(exchange: str, asset: str, fiat: str, trade_type: str, rows: int = 10, user_id: int = 0) -> list:
    f = get_filter(user_id) if user_id else {}
    pay_filter  = f.get("pay")
    min_amount  = f.get("min_amount", 0)

    if exchange == "binance":
        bn_type = "BUY" if trade_type == "buy" else "SELL"
        pay = [pay_filter] if pay_filter else []
        ads = await binance_p2p.get_ads(asset=asset, fiat=fiat, trade_type=bn_type, rows=rows, pay_types=pay)
        # Binance: клиентская фильтрация по сумме
        if min_amount:
            ads = [a for a in ads if a["min_amount"] <= min_amount <= a["max_amount"]]
    elif exchange == "bybit":
        # Bybit: серверная фильтрация по сумме и методу оплаты (через числовые ID)
        side   = "1" if trade_type == "buy" else "0"
        pay    = [pay_filter] if pay_filter else []
        amount = str(int(min_amount)) if min_amount else ""
        ads = await bybit_p2p.get_ads(
            asset=asset, fiat=fiat, side=side,
            size=rows, pay_types=pay, amount=amount,
        )
    elif exchange == "okx":
        pay = [pay_filter] if pay_filter else None
        ads = await okx_p2p.get_ads(asset=asset, fiat=fiat, side=trade_type, pay_types=pay, rows=rows)
        if min_amount:
            ads = [a for a in ads if a["min_amount"] <= min_amount <= a["max_amount"]]
    elif exchange == "wallet":
        pay = [pay_filter] if pay_filter else None
        ads = await wallet_p2p.get_ads(asset=asset, fiat=fiat, side=trade_type, pay_types=pay, rows=rows)
        if min_amount:
            ads = [a for a in ads if a["min_amount"] <= min_amount <= a["max_amount"]]
    else:
        ads = []

    # ── Фильтр по репутации ───────────────────────────────────────────────────────
    min_orders     = f.get("min_orders", 0)
    min_completion = f.get("min_completion", 0)
    if min_orders:
        ads = [a for a in ads if a.get("orders", 0) >= min_orders]
    if min_completion:
        ads = [a for a in ads if a.get("completion", 0) >= min_completion]

    # Скрываем заблокированных мерчантов
    if user_id:
        ads = [a for a in ads if not is_blacklisted(user_id, a["nickname"])]

    # ── Умный фильтр описаний (3-е лица) ──────────────────────────────────────
    tp_filter = f.get("third_party") if user_id else None
    if tp_filter and ads:
        from utils.desc_parser import parse_description
        filtered = []
        for ad in ads:
            info = parse_description(ad.get("description", ""))
            if tp_filter == "yes" and info["third_party"] is False:
                continue  # явно написал "нет третьих лиц" — скрываем
            if tp_filter == "no" and info["third_party"] is True:
                continue  # явно написал "принимаю третьих" — скрываем
            filtered.append(ad)
        ads = filtered

    return ads


# ─── Бан прямо из списка объявлений ───────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("ban:"))
async def ban_from_list(callback: CallbackQuery):
    parts    = callback.data.split(":")
    exchange = parts[1]
    idx      = int(parts[2])
    uid      = callback.from_user.id

    # Ищем по всем возможным ключам кэша
    ads = (_ads_cache.get(f"{uid}:{exchange}")
           or _ads_cache.get(f"{uid}:{exchange}:buy")
           or _ads_cache.get(f"{uid}:{exchange}:sell")
           or [])

    if not ads or idx >= len(ads):
        await callback.answer("❌ Обнови список.", show_alert=True)
        return

    nick = ads[idx].get("nickname", "")
    if not nick or nick == "—":
        await callback.answer("❌ Не удалось определить никнейм.", show_alert=True)
        return

    added = add_to_blacklist(uid, nick)
    if added:
        await callback.answer(f"🚫 {nick} заблокирован", show_alert=True)
    else:
        await callback.answer(f"ℹ️ {nick} уже в чёрном списке", show_alert=True)


# ─── Фильтры: платёжный метод ─────────────────────────────────────────────────

_PAY_METHODS_BY_EXCHANGE = {
    "binance": PAYMENT_METHODS_BINANCE,
    "bybit":   PAYMENT_METHODS_BYBIT,
    "okx":     PAYMENT_METHODS_OKX,
    "wallet":  PAYMENT_METHODS_WALLET,
}


def _pay_keyboard(exchange: str, fiat: str, back_cb: str) -> InlineKeyboardMarkup:
    methods  = _PAY_METHODS_BY_EXCHANGE.get(exchange, PAYMENT_METHODS_BINANCE)
    pay_list = methods.get(fiat, [])
    buttons = []
    row = []
    for method in pay_list:
        label = PAYMENT_LABELS.get(method, method)
        row.append(InlineKeyboardButton(text=label, callback_data=f"setpay:{method}:{back_cb}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="❌ Любой метод", callback_data=f"setpay:NONE:{back_cb}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _amount_keyboard(fiat: str, back_cb: str) -> InlineKeyboardMarkup:
    presets = AMOUNT_PRESETS.get(fiat, [1_000, 5_000, 10_000, 50_000])
    buttons = []
    row = []
    for amount in presets:
        label = f"{amount:,.0f}".replace(",", " ")
        row.append(InlineKeyboardButton(text=label, callback_data=f"setamt:{amount}:{back_cb}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="❌ Любая сумма", callback_data=f"setamt:0:{back_cb}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(lambda c: c.data and c.data.startswith("filterthirdparty:"))
async def show_thirdparty_filter(callback: CallbackQuery):
    """Меню фильтра по третьим лицам."""
    back_cb_parts = callback.data.split(":", 1)[1]   # exchange:fiat:asset:trade_type:sort
    back_cb = f"ads:{back_cb_parts}"

    uid = callback.from_user.id
    cur = get_filter(uid).get("third_party")

    await callback.message.edit_text(
        "👥 <b>Фильтр: третьи лица</b>\n\n"
        "Некоторые мерчанты пишут в описании:\n"
        "• «принимаю от третьих лиц» — можно слать с чужой карты\n"
        "• «только свой перевод» — нельзя с чужой карты\n\n"
        "Бот читает описание и фильтрует автоматически.\n\n"
        f"Текущий фильтр: <b>{'✅ Принимает 3-е лица' if cur == 'yes' else '❌ Без 3-х лиц' if cur == 'no' else 'Неважно'}</b>",
        parse_mode="HTML",
        reply_markup=_thirdparty_kb(back_cb_parts),
    )


def _thirdparty_kb(back_parts: str) -> InlineKeyboardMarkup:
    back_cb = f"ads:{back_parts}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принимает 3-е лица", callback_data=f"setthirdparty:yes:{back_parts}"),
        ],
        [
            InlineKeyboardButton(text="❌ Только свой перевод", callback_data=f"setthirdparty:no:{back_parts}"),
        ],
        [
            InlineKeyboardButton(text="🔄 Неважно (сбросить)", callback_data=f"setthirdparty:off:{back_parts}"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)],
    ])


@router.callback_query(lambda c: c.data and c.data.startswith("setthirdparty:"))
async def set_thirdparty(callback: CallbackQuery):
    parts    = callback.data.split(":", 2)
    val      = parts[1]   # yes / no / off
    back_parts = parts[2]

    uid = callback.from_user.id
    if val == "off":
        clear_filter(uid, "third_party")
        await callback.answer("🔄 Фильтр сброшен")
    else:
        set_filter(uid, "third_party", val)
        label = "✅ Только с 3-ми лицами" if val == "yes" else "❌ Только свой перевод"
        await callback.answer(label)

    callback.data = f"ads:{back_parts}"
    await show_ads(callback)


@router.callback_query(lambda c: c.data and c.data.startswith("filterpay:"))
async def show_pay_filter(callback: CallbackQuery):
    parts = callback.data.split(":", 5)
    _, exchange, fiat, asset, trade_type, sort = parts
    back_cb = f"ads:{exchange}:{fiat}:{asset}:{trade_type}:{sort}"
    await callback.message.edit_text(
        "💳 Выбери платёжный метод:",
        reply_markup=_pay_keyboard(exchange, fiat, back_cb),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("setpay:"))
async def set_pay(callback: CallbackQuery):
    _, method, back_cb = callback.data.split(":", 2)
    if method == "NONE":
        clear_filter(callback.from_user.id, "pay")
        await callback.answer("❌ Фильтр по методу сброшен")
    else:
        set_filter(callback.from_user.id, "pay", method)
        await callback.answer(f"✅ {PAYMENT_LABELS.get(method, method)}")
    callback.data = back_cb
    await show_ads(callback)


@router.callback_query(lambda c: c.data and c.data.startswith("filteramt:"))
async def show_amount_filter(callback: CallbackQuery):
    parts = callback.data.split(":", 5)
    _, exchange, fiat, asset, trade_type, sort = parts
    back_cb = f"ads:{exchange}:{fiat}:{asset}:{trade_type}:{sort}"
    await callback.message.edit_text(
        f"💰 Выбери минимальную сумму сделки ({fiat}):",
        reply_markup=_amount_keyboard(fiat, back_cb),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("setamt:"))
async def set_amount(callback: CallbackQuery):
    _, amount_str, back_cb = callback.data.split(":", 2)
    amount = float(amount_str)
    if amount <= 0:
        clear_filter(callback.from_user.id, "min_amount")
        await callback.answer("❌ Фильтр по сумме сброшен")
    else:
        set_filter(callback.from_user.id, "min_amount", amount)
        await callback.answer(f"✅ от {amount:,.0f}")
    callback.data = back_cb
    await show_ads(callback)


def _reputation_kb(back_parts: str) -> InlineKeyboardMarkup:
    back_cb = f"ads:{back_parts}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 Топ  (≥500 сд / ≥90%)",    callback_data=f"setrep:500:90:{back_parts}")],
        [InlineKeyboardButton(text="✅ Надёжные (≥100 сд / ≥80%)", callback_data=f"setrep:100:80:{back_parts}")],
        [
            InlineKeyboardButton(text="📊 Только % (≥80%)",         callback_data=f"setrep:0:80:{back_parts}"),
            InlineKeyboardButton(text="🔢 Только сделки (≥50)",     callback_data=f"setrep:50:0:{back_parts}"),
        ],
        [InlineKeyboardButton(text="❌ Сбросить",                   callback_data=f"setrep:0:0:{back_parts}")],
        [InlineKeyboardButton(text="⬅️ Назад",                      callback_data=back_cb)],
    ])


@router.callback_query(lambda c: c.data and c.data.startswith("filterrep:"))
async def show_reputation_filter(callback: CallbackQuery):
    back_parts = callback.data.split(":", 1)[1]   # exchange:fiat:asset:trade_type:sort
    f = get_filter(callback.from_user.id)
    cur_orders = f.get("min_orders", 0)
    cur_comp   = f.get("min_completion", 0)

    active = f"≥{cur_orders} сд / ≥{cur_comp}%" if (cur_orders or cur_comp) else "не задан"
    await callback.message.edit_text(
        f"🏅 <b>Фильтр по репутации мерчанта</b>\n\n"
        f"Скрывает мерчантов с малым количеством сделок или низким % успеха.\n\n"
        f"Текущий: <b>{active}</b>",
        parse_mode="HTML",
        reply_markup=_reputation_kb(back_parts),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("setrep:"))
async def set_reputation(callback: CallbackQuery):
    # setrep:{orders}:{completion}:{exchange}:{fiat}:{asset}:{trade_type}:{sort}
    _, orders_str, comp_str, back_parts = callback.data.split(":", 3)
    orders = int(orders_str)
    comp   = int(comp_str)
    uid    = callback.from_user.id

    if orders:
        set_filter(uid, "min_orders", orders)
    else:
        clear_filter(uid, "min_orders")
    if comp:
        set_filter(uid, "min_completion", comp)
    else:
        clear_filter(uid, "min_completion")

    if orders or comp:
        await callback.answer(f"✅ ≥{orders} сд / ≥{comp}%")
    else:
        await callback.answer("❌ Фильтр репутации сброшен")

    callback.data = f"ads:{back_parts}"
    await show_ads(callback)


@router.callback_query(lambda c: c.data and c.data.startswith("filterclear:"))
async def clear_all_filters(callback: CallbackQuery):
    back_cb = callback.data.split(":", 1)[1]
    clear_filter(callback.from_user.id)
    await callback.answer("✅ Фильтры сброшены")
    callback.data = back_cb
    await show_ads(callback)


# ─── Основные обработчики ──────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("exchange:"))
async def choose_exchange(callback: CallbackQuery):
    exchange = callback.data.split(":")[1]
    name = _ex_label(exchange) + " P2P"
    await callback.message.edit_text(f"{name}\n\nВыбери валюту:", reply_markup=fiat_menu(exchange))


@router.callback_query(lambda c: c.data and c.data.startswith("asset:") and len(c.data.split(":")) == 3)
async def choose_asset(callback: CallbackQuery):
    _, exchange, fiat = callback.data.split(":")
    await callback.message.edit_text(
        f"{_ex_label(exchange)} | {fiat}\n\nВыбери ассет:",
        reply_markup=asset_menu(exchange, fiat),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("ads:") and len(c.data.split(":")) == 4)
async def choose_trade_type(callback: CallbackQuery):
    _, exchange, fiat, asset = callback.data.split(":")
    await callback.message.edit_text(
        f"{_ex_label(exchange)} | {asset}/{fiat}\n\nВыбери режим:",
        reply_markup=trade_type_menu(exchange, fiat, asset),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("ads:") and len(c.data.split(":")) == 6)
async def show_ads(callback: CallbackQuery):
    _, exchange, fiat, asset, trade_type, sort = callback.data.split(":")

    await callback.message.edit_text("⏳ Загружаю объявления...")

    try:
        ads = await fetch_ads(exchange, asset, fiat, trade_type, rows=10, user_id=callback.from_user.id)
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка загрузки: {e}")
        return

    if not ads:
        f = get_filter(callback.from_user.id)
        hint = "\n\nПопробуй сбросить фильтры." if f else ""
        await callback.message.edit_text(f"😔 Объявления не найдены.{hint}")
        return

    ads = sort_ads(ads, sort)
    _ads_cache[f"{callback.from_user.id}:{exchange}"] = ads

    type_label = "📗 Покупка" if trade_type == "buy" else "📕 Продажа"
    fsum = filter_summary(callback.from_user.id)
    filter_line = f"\n🔍 {fsum}" if fsum else ""

    # Объём стакана
    total_crypto = sum(a["available"] for a in ads)
    total_fiat   = sum(a["available"] * a["price"] for a in ads)
    vol_line = (f"\n📦 Объём: {total_crypto:,.2f} {asset} ≈ {total_fiat:,.0f} {fiat}"
                if total_crypto else "")

    text = f"{_ex_label(exchange)} | {asset}/{fiat} | {type_label}{filter_line}{vol_line}\n\n"
    text += "\n\n─────────────────\n\n".join(
        format_ad(ad, i + 1, exchange) for i, ad in enumerate(ads)
    )

    await callback.message.edit_text(
        text,
        reply_markup=ads_list_menu(exchange, fiat, asset, trade_type, sort, ads,
                                   user_id=callback.from_user.id),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("sort_ads:"))
async def resort_ads(callback: CallbackQuery):
    parts = callback.data.split(":")
    _, exchange, fiat, asset, trade_type, sort = parts
    callback.data = f"ads:{exchange}:{fiat}:{asset}:{trade_type}:{sort}"
    await show_ads(callback)


@router.callback_query(lambda c: c.data and c.data.startswith("book:"))
async def show_orderbook(callback: CallbackQuery):
    _, exchange, fiat, asset, sort = callback.data.split(":")
    await callback.message.edit_text("⏳ Загружаю стакан...")
    try:
        buy_ads, sell_ads = await asyncio.gather(
            fetch_ads(exchange, asset, fiat, "buy", rows=7, user_id=callback.from_user.id),
            fetch_ads(exchange, asset, fiat, "sell", rows=7, user_id=callback.from_user.id),
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
        return

    # Покупка: дешевле = лучше → по возрастанию
    # Продажа: дороже = лучше → по убыванию
    buy_ads  = sort_ads(buy_ads, sort)
    sell_ads = sort_ads(sell_ads, sort, reverse=True)
    _ads_cache[f"{callback.from_user.id}:{exchange}:buy"]  = buy_ads
    _ads_cache[f"{callback.from_user.id}:{exchange}:sell"] = sell_ads

    text = format_orderbook(buy_ads, sell_ads, asset, fiat, exchange)
    await callback.message.edit_text(
        text,
        reply_markup=book_menu(exchange, fiat, asset, sort),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("sort_book:"))
async def resort_book(callback: CallbackQuery):
    _, exchange, fiat, asset, sort = callback.data.split(":")
    callback.data = f"book:{exchange}:{fiat}:{asset}:{sort}"
    await show_orderbook(callback)


@router.callback_query(lambda c: c.data and c.data.startswith("copy:"))
async def copy_description(callback: CallbackQuery):
    parts = callback.data.split(":")
    exchange = parts[1]
    side = parts[2] if len(parts) == 4 else None
    index = int(parts[-1])
    cache_key = f"{callback.from_user.id}:{exchange}:{side}" if side else f"{callback.from_user.id}:{exchange}"
    ads = _ads_cache.get(cache_key, [])

    if not ads or index >= len(ads):
        await callback.answer("❌ Обнови список.", show_alert=True)
        return
    desc = ads[index].get("description", "")
    if not desc:
        await callback.answer("ℹ️ У этого объявления нет описания.", show_alert=True)
        return

    from utils.desc_parser import parse_description
    info   = parse_description(desc)
    flags  = info["flags"]

    flags_block = ""
    if flags:
        flags_block = "\n\n🏷 <b>Автоанализ:</b>\n" + "\n".join(f"  • {f}" for f in flags)

    await callback.message.answer(
        f"📋 <b>Описание #{index + 1}:</b>\n\n{desc}{flags_block}",
        parse_mode="HTML",
    )
    await callback.answer("✅ Отправлено выше")


@router.callback_query(lambda c: c.data == "spread:compare")
async def spread_choose_fiat(callback: CallbackQuery):
    await callback.message.edit_text(
        "📊 Сравнение спреда по биржам\n\nВыбери валюту:",
        reply_markup=spread_fiat_menu(),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("spread:") and len(c.data.split(":")) == 3)
async def show_spread(callback: CallbackQuery):
    _, fiat, asset = callback.data.split(":")
    await callback.message.edit_text("⏳ Считаю спред...")
    try:
        results = await asyncio.gather(
            binance_p2p.get_best_price(asset, fiat, "BUY"),
            binance_p2p.get_best_price(asset, fiat, "SELL"),
            bybit_p2p.get_best_price(asset, fiat, "1"),
            bybit_p2p.get_best_price(asset, fiat, "0"),
            okx_p2p.get_best_price(asset, fiat, "buy"),
            okx_p2p.get_best_price(asset, fiat, "sell"),
            wallet_p2p.get_best_price(asset, fiat, "buy"),
            wallet_p2p.get_best_price(asset, fiat, "sell"),
            return_exceptions=True,
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
        return

    def _val(v):
        return v if isinstance(v, float) else None

    bn_buy,  bn_sell  = _val(results[0]), _val(results[1])
    bb_buy,  bb_sell  = _val(results[2]), _val(results[3])
    okx_buy, okx_sell = _val(results[4]), _val(results[5])
    wt_buy,  wt_sell  = _val(results[6]), _val(results[7])

    lines = [f"📊 Спред {asset}/{fiat}\n"]

    exchanges = [
        ("🟡 Binance", bn_buy,  bn_sell),
        ("🟠 Bybit",   bb_buy,  bb_sell),
        ("🔵 OKX",     okx_buy, okx_sell),
        ("💎 Wallet",  wt_buy,  wt_sell),
    ]
    for name, buy, sell in exchanges:
        if buy and sell:
            s = calc_spread(buy, sell)
            lines.append(
                f"{name}\n"
                f"  Покупка:  {buy:,.2f}\n"
                f"  Продажа: {sell:,.2f}\n"
                f"  Спред: {s['spread_abs']:,.2f} ({s['spread_pct']}%)"
            )

    # Арбитраж между биржами (best sell на одной vs best buy на другой)
    arb_pairs = [
        ("Binance→Bybit",  bn_buy,  bb_sell),
        ("Bybit→Binance",  bb_buy,  bn_sell),
        ("Binance→OKX",    bn_buy,  okx_sell),
        ("OKX→Binance",    okx_buy, bn_sell),
        ("Bybit→OKX",      bb_buy,  okx_sell),
        ("OKX→Bybit",      okx_buy, bb_sell),
        ("Binance→Wallet", bn_buy,  wt_sell),
        ("Wallet→Binance", wt_buy,  bn_sell),
    ]
    arb_lines = []
    for label, buy, sell in arb_pairs:
        if buy and sell:
            arb = calc_spread(buy, sell)
            if arb["spread_pct"] > 0:
                arb_lines.append(f"  ⚡️ {label}: {arb['spread_pct']}%")
    if arb_lines:
        lines.append("Арбитраж:\n" + "\n".join(arb_lines))

    await callback.message.edit_text("\n\n".join(lines), reply_markup=spread_fiat_menu())
