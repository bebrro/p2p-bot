"""
Whale Tracker — отслеживание крупных мерчантов в стакане.

Логика:
• "Кит" = мерчант с доступным объёмом > порога (default 50k в фиате)
• Каждые 2 минуты сканируем стаканы по всем парам из watchlist
• Новый кит появился → уведомление + совет
• Кит ушёл → уведомление + "окно возможностей" (его цена освободилась)
• Кит сильно изменил объём → уведомление
"""
import asyncio
from datetime import datetime
from aiogram import Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from api import binance_p2p, bybit_p2p
from config import FIATS, FIAT_FLAGS

router = Router()

# {user_id: {"pairs": [(exchange,fiat,asset,side)], "threshold": float}}
_user_settings: dict[int, dict] = {}

# Текущее состояние китов: {(exchange,fiat,asset,side): {nickname: {volume, price, first_seen}}}
_whale_state: dict[tuple, dict] = {}

DEFAULT_THRESHOLD = 50_000   # фиат
VOLUME_CHANGE_PCT = 30       # % изменения объёма для уведомления

DEFAULT_PAIRS = [
    ("binance", "KZT", "USDT", "buy"),
    ("binance", "KZT", "USDT", "sell"),
    ("bybit",   "KZT", "USDT", "buy"),
    ("bybit",   "KZT", "USDT", "sell"),
]


class WhaleStates(StatesGroup):
    waiting_threshold = State()


# ─── UI ───────────────────────────────────────────────────────────────────────

def _settings(user_id: int) -> dict:
    return _user_settings.setdefault(user_id, {
        "pairs":     list(DEFAULT_PAIRS),
        "threshold": DEFAULT_THRESHOLD,
        "enabled":   True,
    })


def _whale_kb(user_id: int) -> InlineKeyboardMarkup:
    cfg = _settings(user_id)
    thr  = cfg["threshold"]
    on   = cfg["enabled"]
    toggle_text = "✅ Включён" if on else "⏸ Выключен"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{toggle_text}", callback_data="wt:toggle"),
         InlineKeyboardButton(text=f"⚖️ Порог: {thr:,.0f}", callback_data="wt:threshold")],
        [InlineKeyboardButton(text="📊 Текущие киты", callback_data="wt:now")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")],
    ])


@router.callback_query(lambda c: c.data == "wt:list")
async def wt_list(callback: CallbackQuery):
    uid = callback.from_user.id
    cfg = _settings(uid)
    status = "✅ Активен" if cfg["enabled"] else "⏸ Выключен"
    await callback.message.edit_text(
        f"🐋 <b>Whale Tracker</b>\n\n"
        f"Отслеживаю мерчантов с объёмом > <b>{cfg['threshold']:,.0f}</b> фиата.\n"
        f"При появлении, уходе или резком изменении объёма — уведомляю.\n\n"
        f"Статус: {status}\n"
        f"Мониторю: Binance + Bybit | KZT, RUB, TRY, USD | обе стороны",
        reply_markup=_whale_kb(uid),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "wt:toggle")
async def wt_toggle(callback: CallbackQuery):
    cfg = _settings(callback.from_user.id)
    cfg["enabled"] = not cfg["enabled"]
    await callback.answer("✅ Включён" if cfg["enabled"] else "⏸ Выключен")
    await wt_list(callback)


@router.callback_query(lambda c: c.data == "wt:threshold")
async def wt_threshold(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WhaleStates.waiting_threshold)
    cfg = _settings(callback.from_user.id)
    await callback.message.edit_text(
        f"⚖️ <b>Порог объёма кита</b>\n\n"
        f"Текущий: <b>{cfg['threshold']:,.0f}</b> фиата\n\n"
        f"Введи новое значение (число):\n"
        f"Например: <code>30000</code> или <code>100000</code>",
        parse_mode="HTML",
    )


@router.message(WhaleStates.waiting_threshold)
async def wt_got_threshold(message: Message, state: FSMContext):
    await state.clear()
    try:
        val = float(message.text.replace(" ", "").replace(",", "."))
        if val <= 0:
            raise ValueError
    except Exception:
        await message.answer("❌ Введи положительное число, например: <code>50000</code>", parse_mode="HTML")
        return
    _settings(message.from_user.id)["threshold"] = val
    await message.answer(
        f"✅ Порог установлен: <b>{val:,.0f}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🐋 Whale Tracker", callback_data="wt:list")]
        ]),
    )


@router.callback_query(lambda c: c.data == "wt:now")
async def wt_now(callback: CallbackQuery):
    uid = callback.from_user.id
    cfg = _settings(uid)
    await callback.message.edit_text("⏳ Сканирую стаканы...")

    # Собираем все пары для всех фиатов
    pairs = []
    for fiat in FIATS:
        for ex in ("binance", "bybit"):
            for side in ("buy", "sell"):
                pairs.append((ex, fiat, "USDT", side))

    results = await asyncio.gather(*[_fetch_whales(ex, fiat, asset, side, cfg["threshold"])
                                     for ex, fiat, asset, side in pairs])

    lines = [f"🐋 <b>Текущие киты</b> (> {cfg['threshold']:,.0f})\n"]
    found_any = False

    for (ex, fiat, asset, side), whales in zip(pairs, results):
        if not whales:
            continue
        found_any = True
        ex_em  = "🟡" if ex == "binance" else "🟠"
        side_s = "покупка" if side == "buy" else "продажа"
        lines.append(f"\n{ex_em} {asset}/{fiat} | {side_s}")
        for nick, info in sorted(whales.items(), key=lambda x: x[1]["volume"], reverse=True)[:5]:
            vol_m = info["volume"] / 1_000
            lines.append(f"  🐋 <b>{nick}</b> — {vol_m:.1f}k @ {info['price']:,.2f}")

    if not found_any:
        lines.append("Китов не обнаружено.\nПопробуй снизить порог.")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="wt:now")],
            [InlineKeyboardButton(text="⬅️ Назад",    callback_data="wt:list")],
        ]),
        parse_mode="HTML",
    )


# ─── Core logic ───────────────────────────────────────────────────────────────

async def _fetch_whales(exchange: str, fiat: str, asset: str, side: str, threshold: float) -> dict:
    """Возвращает {nickname: {volume, price}} для мерчантов выше порога."""
    try:
        if exchange == "binance":
            bn_type = "BUY" if side == "buy" else "SELL"
            ads = await binance_p2p.get_ads(asset=asset, fiat=fiat, trade_type=bn_type, rows=20)
        else:
            bb_side = "1" if side == "buy" else "0"
            ads = await bybit_p2p.get_ads(asset=asset, fiat=fiat, side=bb_side, size=20)

        whales = {}
        for ad in ads:
            # Объём в фиате = available_usdt * price
            fiat_volume = ad["available"] * ad["price"]
            if fiat_volume >= threshold:
                whales[ad["nickname"]] = {
                    "volume":     fiat_volume,
                    "volume_raw": ad["available"],
                    "price":      ad["price"],
                }
        return whales
    except Exception:
        return {}


async def check_whales(bot) -> None:
    """Вызывается каждые 120 секунд из bot.py"""
    import logging
    logger = logging.getLogger(__name__)

    # Собираем уникальные пары из настроек всех пользователей
    pairs_to_check: set[tuple] = set()
    active_users: list[tuple[int, dict]] = []
    for uid, cfg in _user_settings.items():
        if not cfg.get("enabled", True):
            continue
        active_users.append((uid, cfg))
        for fiat in FIATS:
            for ex in ("binance", "bybit"):
                for side in ("buy", "sell"):
                    pairs_to_check.add((ex, fiat, "USDT", side))

    if not active_users or not pairs_to_check:
        return

    # Один пакетный запрос — одна пара = один запрос
    # Используем порог из первого активного пользователя (TODO: per-user если разные)
    min_threshold = min(cfg["threshold"] for _, cfg in active_users)

    tasks  = [_fetch_whales(ex, fiat, asset, side, min_threshold)
              for ex, fiat, asset, side in pairs_to_check]
    raw    = await asyncio.gather(*tasks, return_exceptions=True)
    fresh  = {pair: res for pair, res in zip(pairs_to_check, raw)
              if isinstance(res, dict)}

    now_s  = datetime.now().strftime("%H:%M")

    for pair, new_whales in fresh.items():
        ex, fiat, asset, side = pair
        old_whales = _whale_state.get(pair, {})
        ex_em  = "🟡 Binance" if ex == "binance" else "🟠 Bybit"
        side_s = "покупка" if side == "buy" else "продажа"
        label  = f"{ex_em} | {asset}/{fiat} | {side_s}"

        # Новые киты
        appeared = {n: v for n, v in new_whales.items() if n not in old_whales}
        # Ушедшие киты
        departed = {n: v for n, v in old_whales.items() if n not in new_whales}
        # Резко изменившие объём
        changed  = {}
        for n in new_whales:
            if n in old_whales:
                old_vol = old_whales[n]["volume"]
                new_vol = new_whales[n]["volume"]
                if old_vol > 0:
                    pct = abs(new_vol - old_vol) / old_vol * 100
                    if pct >= VOLUME_CHANGE_PCT:
                        changed[n] = {"old": old_vol, "new": new_vol, "pct": pct,
                                      "price": new_whales[n]["price"]}

        # Обновляем состояние
        _whale_state[pair] = new_whales

        # Уведомляем каждого активного пользователя
        for uid, cfg in active_users:
            threshold = cfg["threshold"]

            for nick, info in appeared.items():
                if info["volume"] < threshold:
                    continue
                try:
                    await bot.send_message(
                        uid,
                        f"🐋 <b>Кит появился!</b> [{now_s}]\n"
                        f"{label}\n\n"
                        f"👤 <b>{nick}</b>\n"
                        f"💰 Цена: {info['price']:,.2f}\n"
                        f"📦 Объём: {info['volume']/1000:.1f}k {fiat}\n\n"
                        f"⚡ Крупный игрок вошёл в стакан.",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"Whale appeared notify uid={uid}: {e}")

            for nick, info in departed.items():
                if info["volume"] < threshold:
                    continue
                try:
                    await bot.send_message(
                        uid,
                        f"🐋 <b>Кит ушёл!</b> [{now_s}]\n"
                        f"{label}\n\n"
                        f"👤 <b>{nick}</b> покинул стакан\n"
                        f"📦 Был объём: {info['volume']/1000:.1f}k {fiat} @ {info['price']:,.2f}\n\n"
                        f"💡 <b>Окно возможностей:</b>\n"
                        f"Его место сейчас свободно — рассмотри выставить\n"
                        f"объявление рядом с ценой {info['price']:,.2f}",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"Whale departed notify uid={uid}: {e}")

            for nick, info in changed.items():
                if max(info["old"], info["new"]) < threshold:
                    continue
                direction = "📈 вырос" if info["new"] > info["old"] else "📉 упал"
                try:
                    await bot.send_message(
                        uid,
                        f"🐋 <b>Кит изменил объём!</b> [{now_s}]\n"
                        f"{label}\n\n"
                        f"👤 <b>{nick}</b>\n"
                        f"💰 Цена: {info['price']:,.2f}\n"
                        f"📦 Объём {direction} на {info['pct']:.0f}%\n"
                        f"   {info['old']/1000:.1f}k → {info['new']/1000:.1f}k {fiat}",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"Whale changed notify uid={uid}: {e}")
