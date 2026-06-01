"""
Smart Арбитражный сканер — 4 биржи (Binance, Bybit, OKX, TG Wallet).

Логика:
• Сканирует все 4 биржи параллельно
• best_buy  = биржа с минимальной ценой покупки крипто
• best_sell = биржа с максимальной ценой продажи крипто
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

from api import binance_p2p, bybit_p2p, okx_p2p, wallet_p2p
from utils.spread import calc_spread
from config import FIATS, FIAT_FLAGS
import db
import logging as _log

router  = Router()
_logger = _log.getLogger(__name__)

# {user_id: [{"fiat", "asset", "threshold", "db_id"}]}
_arb_alerts: dict[int, list] = {}

# Кулдаун: "{user_id}:{fiat}:{asset}" → unix_timestamp последнего уведомления
_last_fired: dict[str, float] = {}
COOLDOWN_SEC = 1800  # 30 минут

_EX_ICONS = {
    "Binance": "🟡",
    "Bybit":   "🟠",
    "OKX":     "🔵",
    "TG Wallet":  "💎",
}


class ArbStates(StatesGroup):
    waiting_threshold = State()


# ─── Core ──────────────────────────────────────────────────────────────────────

async def _fetch_arb(fiat: str, asset: str) -> dict | None:
    """Запрашивает цены на всех 4 биржах и находит лучший арбитражный спред."""
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

        def _v(x): return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None

        bn_buy,  bn_sell  = _v(results[0]), _v(results[1])
        bb_buy,  bb_sell  = _v(results[2]), _v(results[3])
        ox_buy,  ox_sell  = _v(results[4]), _v(results[5])
        wl_buy,  wl_sell  = _v(results[6]), _v(results[7])

        buy_map  = {"Binance": bn_buy, "Bybit": bb_buy, "OKX": ox_buy, "TG Wallet": wl_buy}
        sell_map = {"Binance": bn_sell, "Bybit": bb_sell, "OKX": ox_sell, "TG Wallet": wl_sell}

        valid_buy  = {k: v for k, v in buy_map.items()  if v}
        valid_sell = {k: v for k, v in sell_map.items() if v}

        if not valid_buy or not valid_sell:
            return None

        best_buy_ex  = min(valid_buy,  key=valid_buy.get)
        best_sell_ex = max(valid_sell, key=valid_sell.get)
        best_buy     = valid_buy[best_buy_ex]
        best_sell    = valid_sell[best_sell_ex]

        sp = calc_spread(best_buy, best_sell)

        return {
            "fiat":      fiat,
            "asset":     asset,
            "buy_map":   buy_map,
            "sell_map":  sell_map,
            "best_buy":  best_buy,
            "best_sell": best_sell,
            "buy_ex":    best_buy_ex,
            "sell_ex":   best_sell_ex,
            "arb_pct":   sp["spread_pct"],
            "arb_abs":   sp["spread_abs"],
            # обратная совместимость (старые уведомления)
            "bn_buy": bn_buy,  "bn_sell": bn_sell,
            "bb_buy": bb_buy,  "bb_sell": bb_sell,
        }
    except Exception:
        return None


# ─── Keyboards ────────────────────────────────────────────────────────────────

def _arb_kb(user_id: int) -> InlineKeyboardMarkup:
    alerts  = _arb_alerts.get(user_id, [])
    buttons = []
    for i, a in enumerate(alerts):
        buttons.append([InlineKeyboardButton(
            text=f"🔍 {a['asset']}/{a['fiat']} > {a['threshold']}% (нажми = удалить)",
            callback_data=f"arb:del:{i}",
        )])
    buttons.append([InlineKeyboardButton(text="➕ Добавить алерт",    callback_data="arb:add:start")])
    buttons.append([InlineKeyboardButton(text="📊 Скан прямо сейчас",  callback_data="arb:scan")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад",              callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _fiat_kb_arb() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(
        text=FIAT_FLAGS.get(f, f), callback_data=f"arb:add:fiat:{f}:USDT",
    )] for f in FIATS]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="arb:list")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─── Handlers ──────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "arb:list")
async def arb_list(callback: CallbackQuery):
    uid    = callback.from_user.id
    alerts = _arb_alerts.get(uid, [])
    text   = (
        "🔍 <b>Smart Арбитраж — 4 биржи</b>\n\n"
        "Ищет расхождение цен между:\n"
        "🟡 Binance · 🟠 Bybit · 🔵 OKX · 💎 TG Wallet\n\n"
        "Находит лучшую точку входа и выхода.\n"
        "🔺 <b>+ Связки без карт</b> — по тем же фиатам бот пришлёт, когда "
        "появится исполнимая связка с чистой прибылью выше твоего порога.\n\n"
        f"Активных алертов: <b>{len(alerts)}</b>\n"
        "Нажми на алерт чтобы удалить."
    )
    await callback.message.edit_text(text, reply_markup=_arb_kb(uid), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "arb:add:start")
async def arb_add_start(callback: CallbackQuery):
    await callback.message.edit_text("🔍 Выбери валютную пару:", reply_markup=_fiat_kb_arb())


@router.callback_query(lambda c: c.data and c.data.startswith("arb:add:fiat:"))
async def arb_add_fiat(callback: CallbackQuery, state: FSMContext):
    parts       = callback.data.split(":")
    fiat, asset = parts[3], parts[4]
    await state.update_data(fiat=fiat, asset=asset)
    await state.set_state(ArbStates.waiting_threshold)
    await callback.message.edit_text(
        f"🔍 Smart Арбитраж {asset}/{fiat}\n\n"
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

    uid    = message.from_user.id
    fiat   = data["fiat"]
    asset  = data["asset"]

    db_id  = await db.arb_alerts_add(uid, fiat, asset, threshold)

    if uid not in _arb_alerts:
        _arb_alerts[uid] = []
    _arb_alerts[uid].append({
        "fiat": fiat, "asset": asset, "threshold": threshold, "db_id": db_id,
    })
    await message.answer(
        f"✅ Алерт добавлен!\n"
        f"{asset}/{fiat} → уведомлю при разнице > {threshold}%\n"
        f"Кулдаун: 30 минут между повторными уведомлениями.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Арбитраж",     callback_data="arb:list")],
            [InlineKeyboardButton(text="📊 Скан сейчас",  callback_data="arb:scan")],
        ]),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("arb:del:"))
async def arb_del(callback: CallbackQuery):
    idx = int(callback.data.split(":")[2])
    uid = callback.from_user.id
    lst = _arb_alerts.get(uid, [])
    if 0 <= idx < len(lst):
        alert = lst.pop(idx)
        if alert.get("db_id") and alert["db_id"] > 0:
            await db.arb_alerts_delete_by_id(uid, alert["db_id"])
        await callback.answer("✅ Удалён")
    await arb_list(callback)


async def load_from_db() -> None:
    """Загружает арбитражные алерты из БД в память при старте."""
    if not db.ok():
        return
    all_alerts = await db.arb_alerts_get_all()
    for uid, alerts in all_alerts.items():
        _arb_alerts[uid] = alerts
    total = sum(len(v) for v in _arb_alerts.values())
    _logger.info(f"Arb alerts loaded from DB: {total}")


@router.callback_query(lambda c: c.data == "arb:scan")
async def arb_scan(callback: CallbackQuery):
    await callback.message.edit_text("⏳ Сканирую все пары (4 биржи)...")

    pairs   = [(f, "USDT") for f in FIATS]
    results = await asyncio.gather(*[_fetch_arb(f, a) for f, a in pairs])
    valids  = [r for r in results if r]

    if not valids:
        await callback.message.edit_text(
            "❌ Не удалось получить данные ни с одной биржи.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="arb:list")]
            ]),
        )
        return

    # ── Таблица по парам ───────────────────────────────────────────────────────
    lines = ["🔍 <b>Smart Арбитраж — 4 биржи</b>\n", "<code>"]
    lines.append(f"{'Пара':<10} {'Купить':>9} {'@':>4} {'Продать':>9} {'@':>4} {'%':>7}")
    lines.append("─" * 47)
    for r in valids:
        label    = f"{r['asset']}/{r['fiat']}"
        buy_ico  = _EX_ICONS.get(r["buy_ex"],  "?")[0]
        sell_ico = _EX_ICONS.get(r["sell_ex"], "?")[0]
        lines.append(
            f"{label:<10} {r['best_buy']:>9,.0f} {buy_ico:>4} "
            f"{r['best_sell']:>9,.0f} {sell_ico:>4} {r['arb_pct']:>6.2f}%"
        )
    lines.append("</code>")

    # ── Лучшая возможность ────────────────────────────────────────────────────
    best = max(valids, key=lambda x: x["arb_pct"], default=None)
    if best:
        buy_icon  = _EX_ICONS.get(best["buy_ex"],  "?")
        sell_icon = _EX_ICONS.get(best["sell_ex"], "?")
        lines += [
            "",
            f"🏆 <b>Лучшая: {best['asset']}/{best['fiat']}</b>",
            f"Купить:  {buy_icon} <b>{best['buy_ex']}</b>   по {best['best_buy']:,.2f}",
            f"Продать: {sell_icon} <b>{best['sell_ex']}</b>  по {best['best_sell']:,.2f}",
            f"Потенциал: <b>+{best['arb_pct']:.2f}%</b>  ({best['arb_abs']:,.2f} за USDT)",
            "",
            "<b>Все цены:</b>",
            "<code>",
        ]
        for ex in ["Binance", "Bybit", "OKX", "TG Wallet"]:
            b = best["buy_map"].get(ex)
            s = best["sell_map"].get(ex)
            b_str = f"{b:,.2f}" if b else "   —  "
            s_str = f"{s:,.2f}" if s else "   —  "
            lines.append(
                f"{_EX_ICONS.get(ex, '?')} {ex:<7}  "
                f"buy {b_str:>10}   sell {s_str:>10}"
            )
        lines.append("</code>")

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
    """Вызывается каждые 120 секунд из bot.py."""
    import logging
    logger = logging.getLogger(__name__)

    pairs_needed: set[tuple] = set()
    for alerts in _arb_alerts.values():
        for a in alerts:
            pairs_needed.add((a["fiat"], a["asset"]))
    if not pairs_needed:
        return

    raw      = await asyncio.gather(*[_fetch_arb(f, a) for f, a in pairs_needed])
    data_map = {(r["fiat"], r["asset"]): r for r in raw if r}

    now = time.time()
    for user_id, alerts in list(_arb_alerts.items()):
        for alert in alerts:
            data = data_map.get((alert["fiat"], alert["asset"]))
            if not data or data["arb_pct"] < alert["threshold"]:
                continue

            cool_key = f"{user_id}:{alert['fiat']}:{alert['asset']}"
            if now - _last_fired.get(cool_key, 0) < COOLDOWN_SEC:
                continue
            _last_fired[cool_key] = now

            buy_icon  = _EX_ICONS.get(data["buy_ex"],  "?")
            sell_icon = _EX_ICONS.get(data["sell_ex"], "?")

            # Строим детальное сообщение
            lines_all = []
            for ex in ["Binance", "Bybit", "OKX", "TG Wallet"]:
                b = data["buy_map"].get(ex)
                s = data["sell_map"].get(ex)
                if b or s:
                    lines_all.append(
                        f"{_EX_ICONS.get(ex, '?')} {ex}: "
                        f"{(f'{b:,.2f}') if b else '—'} | {(f'{s:,.2f}') if s else '—'}"
                    )

            try:
                await bot.send_message(
                    user_id,
                    f"🔍 <b>Smart Арбитраж {data['asset']}/{data['fiat']}!</b>\n\n"
                    f"Купить:  {buy_icon} <b>{data['buy_ex']}</b>  по {data['best_buy']:,.2f}\n"
                    f"Продать: {sell_icon} <b>{data['sell_ex']}</b>  по {data['best_sell']:,.2f}\n\n"
                    f"Разница: <b>+{data['arb_pct']:.2f}%</b>  ({data['arb_abs']:,.2f})\n\n"
                    + "\n".join(lines_all),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Arb alert uid={user_id}: {e}")
