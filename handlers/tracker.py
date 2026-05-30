from aiogram import Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from config import FIATS, FIAT_FLAGS
from api import binance_p2p, bybit_p2p, okx_p2p, wallet_p2p
from utils.limits import check_allowed, get_limit, upsell_text, upsell_kb
import db

router = Router()

# Хранилище: {user_id: [entry, ...]}
# entry = {id (DB), exchange, fiat, asset, nickname, buy: {price, available}|None, sell: ...}
_trackers: dict[int, list[dict]] = {}

# Пользователи, чьи трекеры уже загружены из DB
_loaded: set[int] = set()


class TrackerStates(StatesGroup):
    waiting_nickname = State()


# ─── DB helpers ───────────────────────────────────────────────────────────────

async def _ensure_loaded(uid: int) -> None:
    """Загружает трекеры пользователя из DB (однократно)."""
    if uid in _loaded:
        return
    _loaded.add(uid)
    if db.ok():
        rows = await db.trackers_get(uid)
        _trackers[uid] = rows  # уже имеют {id, exchange, fiat, asset, nickname, buy, sell}


# ─── Утилиты ──────────────────────────────────────────────────────────────────

async def _fetch_trader_ads(exchange: str, fiat: str, asset: str, nickname: str) -> dict:
    """Ищет объявления конкретного трейдера. Возвращает {buy: {...}, sell: {...}}"""
    result = {"buy": None, "sell": None}

    async def search_side(trade_type: str):
        if exchange == "binance":
            bn_type = "SELL" if trade_type == "buy" else "BUY"
            ads = await binance_p2p.get_ads(asset=asset, fiat=fiat, trade_type=bn_type, rows=50)
        elif exchange == "bybit":
            side = "0" if trade_type == "buy" else "1"
            ads = await bybit_p2p.get_ads(asset=asset, fiat=fiat, side=side, size=50)
        elif exchange == "okx":
            ads = await okx_p2p.get_ads(asset=asset, fiat=fiat, side=trade_type, rows=50)
        elif exchange == "wallet":
            ads = await wallet_p2p.get_ads(asset=asset, fiat=fiat, side=trade_type, rows=50)
        else:
            ads = []

        nick_lower = nickname.lower()
        for ad in ads:
            if ad["nickname"].lower() == nick_lower:
                return ad
        return None

    import asyncio
    buy_ad, sell_ad = await asyncio.gather(search_side("buy"), search_side("sell"))
    result["buy"]  = buy_ad
    result["sell"] = sell_ad
    return result


def _snapshot(ad: dict | None) -> dict | None:
    if not ad:
        return None
    return {"price": ad["price"], "available": ad["available"]}


def _get_trackers(user_id: int) -> list:
    return _trackers.get(user_id, [])


async def _add_tracker(user_id: int, entry: dict) -> None:
    """Добавляет в память и в DB."""
    if db.ok():
        db_id = await db.trackers_add(
            user_id,
            entry["exchange"], entry["fiat"], entry["asset"], entry["nickname"],
            entry["buy"]["price"]  if entry["buy"]  else None,
            entry["sell"]["price"] if entry["sell"] else None,
        )
        entry["id"] = db_id
    if user_id not in _trackers:
        _trackers[user_id] = []
    _trackers[user_id].append(entry)


async def _remove_tracker(user_id: int, index: int) -> None:
    """Удаляет из памяти и из DB."""
    lst = _trackers.get(user_id, [])
    if 0 <= index < len(lst):
        entry = lst[index]
        if db.ok() and "id" in entry:
            await db.trackers_delete(user_id, entry["id"])
        lst.pop(index)


def get_all_trackers() -> dict[int, list]:
    return _trackers


# ─── Клавиатуры ───────────────────────────────────────────────────────────────

def tracker_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    trackers = _get_trackers(user_id)
    buttons  = []

    _EX_ICONS = {"binance": "🟡", "bybit": "🟠", "okx": "🔵", "wallet": "💎"}
    for i, t in enumerate(trackers):
        ex = _EX_ICONS.get(t["exchange"], "🔷")
        buttons.append([InlineKeyboardButton(
            text=f"{ex} {t['nickname']} | {t['asset']}/{t['fiat']}",
            callback_data=f"tracker:view:{i}",
        )])

    buttons.append([InlineKeyboardButton(text="➕ Добавить трекер", callback_data="tracker:add:start")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


_EX_LABELS = {
    "binance": "🟡 Binance",
    "bybit":   "🟠 Bybit",
    "okx":     "🔵 OKX",
    "wallet":  "💎 TG Wallet",
}

def _ex_label(exchange: str) -> str:
    return _EX_LABELS.get(exchange, exchange.title())


def tracker_exchange_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟡 Binance",  callback_data="tracker:add:ex:binance"),
            InlineKeyboardButton(text="🟠 Bybit",    callback_data="tracker:add:ex:bybit"),
        ],
        [
            InlineKeyboardButton(text="🔵 OKX",      callback_data="tracker:add:ex:okx"),
            InlineKeyboardButton(text="💎 TG Wallet", callback_data="tracker:add:ex:wallet"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="tracker:list")],
    ])


def tracker_fiat_kb(exchange: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=FIAT_FLAGS.get(f, f),
            callback_data=f"tracker:add:fiat:{exchange}:{f}",
        )]
        for f in FIATS
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="tracker:add:start")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def tracker_view_kb(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить",       callback_data=f"tracker:refresh:{index}")],
        [InlineKeyboardButton(text="🗑 Удалить трекер", callback_data=f"tracker:delete:{index}")],
        [InlineKeyboardButton(text="⬅️ Назад",          callback_data="tracker:list")],
    ])


# ─── Хендлеры ─────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "tracker:list")
async def tracker_list(callback: CallbackQuery):
    user_id = callback.from_user.id
    await _ensure_loaded(user_id)
    trackers = _get_trackers(user_id)
    count    = len(trackers)
    limit, _ = await get_limit(user_id, "trackers")
    text = (
        f"👁 <b>Трекер конкурентов</b>\n\n"
        f"Активных трекеров: {count}/{limit}\n"
        f"Проверка каждые 5 минут. Пришлю уведомление при изменении цены."
    )
    await callback.message.edit_text(text, reply_markup=tracker_menu_kb(user_id), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "tracker:add:start")
async def tracker_add_start(callback: CallbackQuery):
    user_id = callback.from_user.id
    await _ensure_loaded(user_id)
    current = len(_get_trackers(user_id))
    allowed, limit, plan_key = await check_allowed(user_id, "trackers", current)
    if not allowed:
        await callback.message.edit_text(
            upsell_text("trackers", current, limit, plan_key),
            reply_markup=upsell_kb(manage_cb="tracker:list", manage_text="🗑 Удалить трекер"),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    await callback.message.edit_text("👁 Выбери биржу:", reply_markup=tracker_exchange_kb())


@router.callback_query(lambda c: c.data and c.data.startswith("tracker:add:ex:"))
async def tracker_add_exchange(callback: CallbackQuery):
    exchange = callback.data.split(":")[3]
    await callback.message.edit_text(
        "👁 Выбери валюту:",
        reply_markup=tracker_fiat_kb(exchange),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("tracker:add:fiat:"))
async def tracker_add_fiat(callback: CallbackQuery, state: FSMContext):
    parts    = callback.data.split(":")
    exchange = parts[3]
    fiat     = parts[4]

    await state.update_data(exchange=exchange, fiat=fiat, asset="USDT")
    await state.set_state(TrackerStates.waiting_nickname)

    ex = _ex_label(exchange)
    await callback.message.edit_text(
        f"👁 Трекер | {ex} | USDT/{fiat}\n\n"
        f"Введи <b>точный никнейм</b> трейдера которого хочешь отслеживать:\n"
        f"Например: <code>CryptoKing_KZ</code>",
        parse_mode="HTML",
    )


@router.message(TrackerStates.waiting_nickname)
async def tracker_save_nickname(message: Message, state: FSMContext):
    nickname = message.text.strip()
    if len(nickname) < 2:
        await message.answer("❌ Слишком короткий никнейм.")
        return

    data     = await state.get_data()
    exchange = data["exchange"]
    fiat     = data["fiat"]
    asset    = data["asset"]
    uid      = message.from_user.id

    await state.clear()
    await _ensure_loaded(uid)
    await message.answer("⏳ Ищу трейдера на бирже...")

    ads = await _fetch_trader_ads(exchange, fiat, asset, nickname)

    if not ads["buy"] and not ads["sell"]:
        await message.answer(
            f"❌ Трейдер <b>{nickname}</b> не найден в топ-50 объявлений.\n\n"
            f"Проверь:\n• Правильность никнейма (регистр важен)\n"
            f"• Что у него есть активные объявления",
            parse_mode="HTML",
        )
        return

    entry = {
        "exchange": exchange,
        "fiat":     fiat,
        "asset":    asset,
        "nickname": nickname,
        "buy":      _snapshot(ads["buy"]),
        "sell":     _snapshot(ads["sell"]),
    }
    await _add_tracker(uid, entry)

    text = _format_tracker_card(entry, ads, is_new=True)
    await message.answer(
        text, parse_mode="HTML",
        reply_markup=tracker_view_kb(len(_get_trackers(uid)) - 1),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("tracker:view:"))
async def tracker_view(callback: CallbackQuery):
    user_id  = callback.from_user.id
    await _ensure_loaded(user_id)
    index    = int(callback.data.split(":")[2])
    trackers = _get_trackers(user_id)
    if index >= len(trackers):
        await callback.answer("❌ Трекер не найден", show_alert=True)
        return

    t = trackers[index]
    await callback.message.edit_text("⏳ Загружаю...")
    ads  = await _fetch_trader_ads(t["exchange"], t["fiat"], t["asset"], t["nickname"])
    text = _format_tracker_card(t, ads)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=tracker_view_kb(index))


@router.callback_query(lambda c: c.data and c.data.startswith("tracker:refresh:"))
async def tracker_refresh(callback: CallbackQuery):
    await tracker_view(callback)


@router.callback_query(lambda c: c.data and c.data.startswith("tracker:delete:"))
async def tracker_delete(callback: CallbackQuery):
    user_id = callback.from_user.id
    await _ensure_loaded(user_id)
    index   = int(callback.data.split(":")[2])
    await _remove_tracker(user_id, index)
    await callback.answer("✅ Трекер удалён")
    await tracker_list(callback)


# ─── Форматирование ───────────────────────────────────────────────────────────

def _format_tracker_card(entry: dict, ads: dict, is_new: bool = False) -> str:
    ex       = _ex_label(entry["exchange"])
    nickname = entry["nickname"]
    fiat     = entry["fiat"]
    asset    = entry["asset"]

    buy_ad  = ads.get("buy")
    sell_ad = ads.get("sell")

    lines = [
        f"👁 {'Трекер добавлен!' if is_new else 'Текущие объявления'}",
        f"{ex} | <b>{nickname}</b> | {asset}/{fiat}\n",
    ]

    if buy_ad:
        completion = buy_ad["completion"]
        if isinstance(completion, float) and completion <= 1:
            completion = round(completion * 100, 1)
        lines.append(
            f"📗 Покупка (он продаёт тебе)\n"
            f"   Цена: <b>{buy_ad['price']:,.2f} {fiat}</b>\n"
            f"   Доступно: {buy_ad['available']:.2f} {asset}\n"
            f"   Сделок: {buy_ad['orders']} | {completion}%"
        )
    else:
        lines.append("📗 Нет активных объявлений на покупку")

    lines.append("")

    if sell_ad:
        completion = sell_ad["completion"]
        if isinstance(completion, float) and completion <= 1:
            completion = round(completion * 100, 1)
        lines.append(
            f"📕 Продажа (он покупает у тебя)\n"
            f"   Цена: <b>{sell_ad['price']:,.2f} {fiat}</b>\n"
            f"   Доступно: {sell_ad['available']:.2f} {asset}\n"
            f"   Сделок: {sell_ad['orders']} | {completion}%"
        )
    else:
        lines.append("📕 Нет активных объявлений на продажу")

    lines.append("\n🔄 Обновляю каждые 5 минут")
    return "\n".join(lines)


# ─── Фоновая проверка (вызывается из bot.py) ──────────────────────────────────

async def check_trackers(bot) -> None:
    import logging
    logger = logging.getLogger(__name__)

    # Если DB доступна — читаем все трекеры из неё (работает после рестарта)
    if db.ok():
        all_entries = await db.trackers_get_all()  # содержит user_id
        by_user: dict[int, list] = {}
        for e in all_entries:
            uid = e["user_id"]
            by_user.setdefault(uid, []).append(e)
    else:
        by_user = dict(_trackers)

    for user_id, entries in by_user.items():
        for entry in list(entries):
            try:
                ads      = await _fetch_trader_ads(
                    entry["exchange"], entry["fiat"], entry["asset"], entry["nickname"]
                )
                nickname = entry["nickname"]
                fiat     = entry["fiat"]
                exchange = entry["exchange"]
                ex       = _ex_label(exchange)
                changes  = []

                # Проверяем покупку
                new_buy = _snapshot(ads["buy"])
                old_buy = entry.get("buy")
                if old_buy is None and new_buy:
                    changes.append(f"📗 Новое объявление на покупку: <b>{new_buy['price']:,.2f} {fiat}</b>")
                elif old_buy and new_buy is None:
                    changes.append("📗 Убрал объявление на покупку")
                elif old_buy and new_buy and old_buy["price"] != new_buy["price"]:
                    diff  = new_buy["price"] - old_buy["price"]
                    arrow = "⬆️" if diff > 0 else "⬇️"
                    changes.append(
                        f"📗 Изменил цену покупки: {old_buy['price']:,.2f} → "
                        f"<b>{new_buy['price']:,.2f}</b> {arrow}{abs(diff):.2f}"
                    )

                # Проверяем продажу
                new_sell = _snapshot(ads["sell"])
                old_sell = entry.get("sell")
                if old_sell is None and new_sell:
                    changes.append(f"📕 Новое объявление на продажу: <b>{new_sell['price']:,.2f} {fiat}</b>")
                elif old_sell and new_sell is None:
                    changes.append("📕 Убрал объявление на продажу")
                elif old_sell and new_sell and old_sell["price"] != new_sell["price"]:
                    diff  = new_sell["price"] - old_sell["price"]
                    arrow = "⬆️" if diff > 0 else "⬇️"
                    changes.append(
                        f"📕 Изменил цену продажи: {old_sell['price']:,.2f} → "
                        f"<b>{new_sell['price']:,.2f}</b> {arrow}{abs(diff):.2f}"
                    )

                # Обновляем цены в DB
                if db.ok() and "id" in entry:
                    await db.trackers_update_prices(
                        entry["id"],
                        new_buy["price"]  if new_buy  else None,
                        new_sell["price"] if new_sell else None,
                    )

                # Обновляем в памяти (если загружен)
                if user_id in _trackers:
                    for mem in _trackers[user_id]:
                        if mem.get("id") == entry.get("id"):
                            mem["buy"]  = new_buy
                            mem["sell"] = new_sell

                if changes:
                    text = (
                        f"🔔 <b>Изменение у {nickname}</b>\n"
                        f"{ex} | {entry['asset']}/{fiat}\n\n"
                        + "\n".join(changes)
                    )
                    await bot.send_message(user_id, text, parse_mode="HTML")

            except Exception as e:
                logger.error(f"Tracker error user={user_id} nick={entry.get('nickname')}: {e}")
