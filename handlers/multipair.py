import asyncio
from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from api import binance_p2p, bybit_p2p
from utils.spread import calc_spread
from config import FIATS, FIAT_FLAGS

router = Router()

# Дефолтный набор пар — показываем всем без настройки
DEFAULT_PAIRS = [
    ("binance", "KZT", "USDT"),
    ("bybit",   "KZT", "USDT"),
    ("binance", "RUB", "USDT"),
    ("bybit",   "RUB", "USDT"),
    ("binance", "TRY", "USDT"),
    ("bybit",   "TRY", "USDT"),
]

# Пользовательские пары: {user_id: [(exchange, fiat, asset)]}
_user_pairs: dict[int, list] = {}

MAX_PAIRS = 8


def _pairs_kb(user_id: int) -> InlineKeyboardMarkup:
    pairs = _user_pairs.get(user_id, DEFAULT_PAIRS)
    buttons = []
    for i, (ex, fiat, asset) in enumerate(pairs):
        label = f"{'🟡' if ex == 'binance' else '🟠'} {asset}/{fiat}"
        buttons.append([InlineKeyboardButton(text=f"❌ {label}", callback_data=f"mp:del:{i}")])
    if len(pairs) < MAX_PAIRS:
        buttons.append([InlineKeyboardButton(text="➕ Добавить пару", callback_data="mp:add:start")])
    buttons.append([InlineKeyboardButton(text="📊 Показать сейчас", callback_data="mp:view")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _add_ex_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟡 Binance", callback_data="mp:add:ex:binance"),
         InlineKeyboardButton(text="🟠 Bybit",   callback_data="mp:add:ex:bybit")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="mp:settings")],
    ])


def _add_fiat_kb(exchange: str) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(
        text=FIAT_FLAGS.get(f, f),
        callback_data=f"mp:add:fiat:{exchange}:{f}:USDT",
    )] for f in FIATS]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="mp:add:start")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(lambda c: c.data == "mp:settings")
async def mp_settings(callback: CallbackQuery):
    uid   = callback.from_user.id
    pairs = _user_pairs.get(uid, DEFAULT_PAIRS)
    await callback.message.edit_text(
        f"🔀 <b>Мультипара</b>\n\nАктивных пар: {len(pairs)}/{MAX_PAIRS}\n"
        "Нажми ❌ рядом с парой чтобы удалить.",
        reply_markup=_pairs_kb(uid),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "mp:view")
async def mp_view(callback: CallbackQuery):
    uid   = callback.from_user.id
    pairs = _user_pairs.get(uid, DEFAULT_PAIRS)
    await callback.message.edit_text("⏳ Загружаю все пары...")

    async def fetch_pair(exchange, fiat, asset):
        try:
            if exchange == "binance":
                buy  = await binance_p2p.get_best_price(asset, fiat, "BUY")
                sell = await binance_p2p.get_best_price(asset, fiat, "SELL")
            else:
                buy  = await bybit_p2p.get_best_price(asset, fiat, "1")
                sell = await bybit_p2p.get_best_price(asset, fiat, "0")
            return exchange, fiat, asset, buy, sell
        except Exception:
            return exchange, fiat, asset, None, None

    results = await asyncio.gather(*[fetch_pair(*p) for p in pairs])

    lines = ["🔀 <b>Мультипара — обзор рынка</b>\n"]
    lines.append("<code>")
    lines.append(f"{'Пара':<12} {'Покупка':>10} {'Продажа':>10} {'Спред':>8}")
    lines.append("─" * 44)

    for exchange, fiat, asset, buy, sell in results:
        ex    = "🟡" if exchange == "binance" else "🟠"
        label = f"{ex}{asset}/{fiat}"
        if buy and sell:
            s = calc_spread(sell, buy)
            lines.append(
                f"{label:<12} {buy:>10,.1f} {sell:>10,.1f} {s['spread_pct']:>7.2f}%"
            )
        else:
            lines.append(f"{label:<12} {'—':>10} {'—':>10} {'—':>8}")

    lines.append("</code>")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить",    callback_data="mp:view")],
        [InlineKeyboardButton(text="⚙️ Настройки",   callback_data="mp:settings")],
        [InlineKeyboardButton(text="⬅️ Назад",       callback_data="back:main")],
    ])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "mp:add:start")
async def mp_add_start(callback: CallbackQuery):
    await callback.message.edit_text("🔀 Выбери биржу:", reply_markup=_add_ex_kb())


@router.callback_query(lambda c: c.data and c.data.startswith("mp:add:ex:"))
async def mp_add_ex(callback: CallbackQuery):
    exchange = callback.data.split(":")[3]
    await callback.message.edit_text("🔀 Выбери валюту:", reply_markup=_add_fiat_kb(exchange))


@router.callback_query(lambda c: c.data and c.data.startswith("mp:add:fiat:"))
async def mp_add_fiat(callback: CallbackQuery):
    parts    = callback.data.split(":")
    exchange, fiat, asset = parts[3], parts[4], parts[5]
    uid      = callback.from_user.id

    if uid not in _user_pairs:
        _user_pairs[uid] = list(DEFAULT_PAIRS)

    pair = (exchange, fiat, asset)
    if pair not in _user_pairs[uid]:
        _user_pairs[uid].append(pair)
        await callback.answer(f"✅ Добавлено: {asset}/{fiat}")
    else:
        await callback.answer("ℹ️ Уже есть в списке")

    await mp_settings(callback)


@router.callback_query(lambda c: c.data and c.data.startswith("mp:del:"))
async def mp_del(callback: CallbackQuery):
    idx = int(callback.data.split(":")[2])
    uid = callback.from_user.id
    pairs = _user_pairs.get(uid, list(DEFAULT_PAIRS))
    if uid not in _user_pairs:
        _user_pairs[uid] = list(DEFAULT_PAIRS)
    if 0 <= idx < len(_user_pairs[uid]):
        _user_pairs[uid].pop(idx)
        await callback.answer("✅ Удалено")
    await mp_settings(callback)
