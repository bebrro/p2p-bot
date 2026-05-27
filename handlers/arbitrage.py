"""
Арбитражный сканер: отслеживает расхождение цен между Binance и Bybit.

Логика:
• best_buy  = min(binance_buy, bybit_buy)    — где дешевле купить
• best_sell = max(binance_sell, bybit_sell)  — где дороже продать
• arb_pct   = (best_sell - best_buy) / best_buy * 100

Уведомляет когда разница > порога пользователя.
Кулдаун 30 минут чтобы не спамить.
"""
import asyncio
import time
from aiogram import Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from api import binance_p2p, bybit_p2p
from utils.spread import calc_spread
from config import FIATS, FIAT_FLAGS

router = Router()

# {user_id: [{"fiat", "asset", "threshold"}]}
_arb_alerts: dict[int, list] = {}

# Кулдаун: "{user_id}:{fiat}:{asset}" → unix_timestamp последнего уведомления
_last_fired: dict[str, float] = {}
COOLDOWN_SEC = 1800  # 30 минут


class ArbStates(StatesGroup):
    waiting_threshold = State()


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _arb_kb(user_id: int) -> InlineKeyboardMarkup:
    alerts  = _arb_alerts.get(user_id, [])
    buttons = []
    for i, a in enumerate(alerts):
        buttons.append([InlineKeyboardButton(
            text=f"🔍 {a['asset']}/{a['fiat']} > {a['threshold']}% (нажми = удалить)",
            callback_data=f"arb:del:{i}",
        )])
    buttons.append([InlineKeyboardButton(text="➕ Добавить алерт",  callback_data="arb:add:start")])
    buttons.append([InlineKeyboardButton(text="📊 Скан прямо сейчас", callback_data="arb:scan")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад",            callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _fiat_kb_arb() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(
        text=FIAT_FLAGS.get(f, f), callback_data=f"arb:add:fiat:{f}:USDT",
    )] for f in FIATS]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="arb:list")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _fetch_arb(fiat: str, asset: str) -> dict | None:
    """Запрашивает цены на обеих биржах и считает арбитражный спред."""
    try:
        (bn_buy, bn_sell, bb_buy, bb_sell) = await asyncio.gather(
            binance_p2p.get_best_price(asset, fiat, "BUY"),
            binance_p2p.get_best_price(asset, fiat, "SELL"),
            bybit_p2p.get_best_price(asset, fiat, "1"),
            bybit_p2p.get_best_price(asset, fiat, "0"),
        )
        if not all([bn_buy, bn_sell, bb_buy, bb_sell]):
            return None

        best_buy  = min(bn_buy, bb_buy)    # покупаем дешевле
        best_sell = max(bn_sell, bb_sell)  # продаём дороже
        sp        = calc_spread(best_buy, best_sell)

        return {
            "fiat":      fiat,
            "asset":     asset,
            "bn_buy":    bn_buy,   "bn_sell": bn_sell,
            "bb_buy":    bb_buy,   "bb_sell": bb_sell,
            "best_buy":  best_buy, "best_sell": best_sell,
            "arb_pct":   sp["spread_pct"],
            "arb_abs":   sp["spread_abs"],
            "buy_ex":    "Binance" if bn_buy <= bb_buy else "Bybit",
            "sell_ex":   "Binance" if bn_sell >= bb_sell else "Bybit",
        }
    except Exception:
        return None


# ─── Handlers ──────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "arb:list")
async def arb_list(callback: CallbackQuery):
    uid    = callback.from_user.id
    alerts = _arb_alerts.get(uid, [])
    text   = (
        "🔍 <b>Арбитражный сканер</b>\n\n"
        "Отслеживает расхождение цен между Binance и Bybit.\n"
        "Уведомит когда разница превысит твой порог.\n\n"
        f"Активных алертов: <b>{len(alerts)}</b>\n"
        "Нажми на алерт чтобы удалить."
    )
    await callback.message.edit_text(text, reply_markup=_arb_kb(uid), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "arb:add:start")
async def arb_add_start(callback: CallbackQuery):
    await callback.message.edit_text("🔍 Выбери валютную пару:", reply_markup=_fiat_kb_arb())


@router.callback_query(lambda c: c.data and c.data.startswith("arb:add:fiat:"))
async def arb_add_fiat(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    fiat, asset = parts[3], parts[4]
    await state.update_data(fiat=fiat, asset=asset)
    await state.set_state(ArbStates.waiting_threshold)
    await callback.message.edit_text(
        f"🔍 Арбитраж {asset}/{fiat}\n\n"
        "Введи минимальный % расхождения для уведомления.\n"
        "Например: <b>0.3</b> → пришлю когда разница > 0.3%",
        parse_mode="HTML",
    )


@router.message(ArbStates.waiting_threshold)
async def arb_got_threshold(message: Message, state: FSMContext):
    try:
        threshold = float(message.text.replace(",", "."))
        if threshold <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи положительное число, например: 0.3")
        return

    data = await state.get_data()
    await state.clear()

    uid = message.from_user.id
    if uid not in _arb_alerts:
        _arb_alerts[uid] = []
    _arb_alerts[uid].append({
        "fiat": data["fiat"], "asset": data["asset"], "threshold": threshold
    })
    await message.answer(
        f"✅ Алерт добавлен!\n"
        f"{data['asset']}/{data['fiat']} → уведомлю при разнице > {threshold}%\n"
        f"Кулдаун: 30 минут между повторными уведомлениями.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Арбитраж", callback_data="arb:list")],
            [InlineKeyboardButton(text="📊 Скан сейчас", callback_data="arb:scan")],
        ]),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("arb:del:"))
async def arb_del(callback: CallbackQuery):
    idx = int(callback.data.split(":")[2])
    uid = callback.from_user.id
    lst = _arb_alerts.get(uid, [])
    if 0 <= idx < len(lst):
        lst.pop(idx)
        await callback.answer("✅ Удалён")
    await arb_list(callback)


@router.callback_query(lambda c: c.data == "arb:scan")
async def arb_scan(callback: CallbackQuery):
    await callback.message.edit_text("⏳ Сканирую все пары...")

    pairs   = [(f, "USDT") for f in FIATS]
    results = await asyncio.gather(*[_fetch_arb(f, a) for f, a in pairs])
    valids  = [r for r in results if r]

    lines = ["🔍 <b>Арбитражный скан — Binance vs Bybit</b>\n", "<code>"]
    lines.append(f"{'Пара':<10} {'Купить':>10} {'Продать':>10} {'%':>7}")
    lines.append("─" * 41)
    for r in valids:
        label = f"{r['asset']}/{r['fiat']}"
        lines.append(
            f"{label:<10} {r['best_buy']:>10,.1f} {r['best_sell']:>10,.1f} {r['arb_pct']:>6.2f}%"
        )
    lines.append("</code>")

    best = max(valids, key=lambda x: x["arb_pct"], default=None)
    if best:
        lines += [
            "",
            f"🏆 <b>Лучшая возможность: {best['asset']}/{best['fiat']}</b>",
            f"Купить на <b>{best['buy_ex']}</b> по {best['best_buy']:,.2f}",
            f"Продать на <b>{best['sell_ex']}</b> по {best['best_sell']:,.2f}",
            f"Потенциал: <b>{best['arb_pct']:.2f}%</b> ({best['arb_abs']:,.2f} за USDT)\n",
            "<b>Детали по биржам:</b>",
            f"Binance: покупка {best['bn_buy']:,.2f} | продажа {best['bn_sell']:,.2f}",
            f"Bybit:   покупка {best['bb_buy']:,.2f} | продажа {best['bb_sell']:,.2f}",
        ]

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="arb:scan")],
            [InlineKeyboardButton(text="⬅️ Назад",    callback_data="arb:list")],
        ]),
        parse_mode="HTML",
    )


# ─── Background task ───────────────────────────────────────────────────────────

def get_all_arb_alerts() -> dict:
    return _arb_alerts


async def check_arbitrage(bot) -> None:
    """Вызывается каждые 120 секунд из bot.py"""
    import logging
    logger = logging.getLogger(__name__)

    # Собираем уникальные пары
    pairs_needed: set[tuple] = set()
    for alerts in _arb_alerts.values():
        for a in alerts:
            pairs_needed.add((a["fiat"], a["asset"]))
    if not pairs_needed:
        return

    # Один пакетный запрос
    raw     = await asyncio.gather(*[_fetch_arb(f, a) for f, a in pairs_needed])
    data_map = {(r["fiat"], r["asset"]): r for r in raw if r}

    now = time.time()
    for user_id, alerts in list(_arb_alerts.items()):
        for alert in alerts:
            data = data_map.get((alert["fiat"], alert["asset"]))
            if not data or data["arb_pct"] < alert["threshold"]:
                continue

            cool_key  = f"{user_id}:{alert['fiat']}:{alert['asset']}"
            if now - _last_fired.get(cool_key, 0) < COOLDOWN_SEC:
                continue
            _last_fired[cool_key] = now

            try:
                await bot.send_message(
                    user_id,
                    f"🔍 <b>Арбитраж {data['asset']}/{data['fiat']}!</b>\n\n"
                    f"Купить на <b>{data['buy_ex']}</b> по {data['best_buy']:,.2f}\n"
                    f"Продать на <b>{data['sell_ex']}</b> по {data['best_sell']:,.2f}\n\n"
                    f"Разница: <b>{data['arb_pct']:.2f}%</b> ({data['arb_abs']:,.2f})\n\n"
                    f"Binance: покупка {data['bn_buy']:,.2f} | продажа {data['bn_sell']:,.2f}\n"
                    f"Bybit:   покупка {data['bb_buy']:,.2f} | продажа {data['bb_sell']:,.2f}",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Arb alert error uid={user_id}: {e}")
