from collections import deque
from datetime import datetime, timedelta
from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from api import binance_p2p, bybit_p2p
from utils.spread import calc_spread
from config import FIATS, FIAT_FLAGS
import db

router = Router()

# В памяти храним последние точки (кэш). При перезапуске — грузим из DB.
_history: dict[tuple, deque] = {}
MAX_POINTS = 672   # 14 дней × 48 точек/день


async def record_price(exchange: str, fiat: str, asset: str, buy: float, sell: float) -> None:
    """Записывает точку в память и в DB."""
    key = (exchange, fiat, asset)
    if key not in _history:
        _history[key] = deque(maxlen=MAX_POINTS)
    _history[key].append((datetime.now(), buy, sell))
    if db.ok():
        await db.history_add(exchange, fiat, asset, buy, sell)


async def get_history(exchange: str, fiat: str, asset: str) -> list:
    """Возвращает [(datetime, buy, sell), ...] от старого к новому.
    Сначала ищет в памяти, при пустой памяти читает из DB.

    ВАЖНО: память хранит НАИВНЫЕ даты (datetime.now()), а БД — tz-aware
    (TIMESTAMPTZ). Потребители (pattern_engine и др.) сравнивают с наивным
    datetime.now(), поэтому приводим всё к наивному — иначе «can't compare
    offset-naive and offset-aware datetimes»."""
    key = (exchange, fiat, asset)
    if key in _history and _history[key]:
        rows = list(_history[key])
    elif db.ok():
        rows = await db.history_get(exchange, fiat, asset)
    else:
        rows = []
    out = []
    for ts, b, s in rows:
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.replace(tzinfo=None)      # tz-aware → наивный
        out.append((ts, b, s))
    return out


def _fiat_kb(exchange: str) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(
        text=FIAT_FLAGS.get(f, f),
        callback_data=f"hist:{exchange}:{f}:USDT",
    )] for f in FIATS]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _hist_kb(exchange: str, fiat: str, asset: str) -> InlineKeyboardMarkup:
    other       = "bybit" if exchange == "binance" else "binance"
    other_label = "🟠 Bybit" if other == "bybit" else "🟡 Binance"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"↔️ {other_label}", callback_data=f"hist:{other}:{fiat}:{asset}")],
        [InlineKeyboardButton(text="🔄 Обновить",       callback_data=f"hist:{exchange}:{fiat}:{asset}")],
        [InlineKeyboardButton(text="⬅️ Назад",          callback_data="back:main")],
    ])


def _ascii_chart(values: list[float], width: int = 20, height: int = 5) -> str:
    if not values:
        return "Нет данных"
    mn, mx = min(values), max(values)
    if mx == mn:
        return "─" * width
    lines = []
    for row in range(height, 0, -1):
        threshold = mn + (mx - mn) * row / height
        line = ""
        for v in values[-width:]:
            line += "█" if v >= threshold else " "
        lines.append(line)
    return "\n".join(lines)


@router.callback_query(lambda c: c.data and c.data.startswith("hist:") and len(c.data.split(":")) == 4)
async def show_history(callback: CallbackQuery):
    _, exchange, fiat, asset = callback.data.split(":")
    hist = await get_history(exchange, fiat, asset)

    ex = "🟡 Binance" if exchange == "binance" else "🟠 Bybit"

    if len(hist) < 2:
        await callback.message.edit_text(
            f"📈 История | {ex} | {asset}/{fiat}\n\n"
            f"⏳ Данных пока мало — история накапливается каждые 30 минут.\n"
            f"Зайди через час.",
            reply_markup=_hist_kb(exchange, fiat, asset),
        )
        return

    now = datetime.now()
    h24 = [(ts, b, s) for ts, b, s in hist if ts > now - timedelta(hours=24)]
    h7d = [(ts, b, s) for ts, b, s in hist if ts > now - timedelta(days=7)]

    def stats(data):
        if not data:
            return None
        spreads  = [(calc_spread(b, s)["spread_pct"], b, s, ts) for ts, b, s in data]
        sprds    = [x[0] for x in spreads]
        buys     = [x[1] for x in data]
        sells    = [x[2] for x in data]
        best     = max(spreads, key=lambda x: x[0])
        hours    = {}
        for ts, b, s in data:
            h  = ts.hour
            sp = calc_spread(b, s)["spread_pct"]
            hours[h] = hours.get(h, []) + [sp]
        best_hour = max(hours, key=lambda h: sum(hours[h]) / len(hours[h])) if hours else None
        avg_per_h = {h: round(sum(v) / len(v), 3) for h, v in hours.items()}
        return {
            "spread_avg": round(sum(sprds) / len(sprds), 3),
            "spread_max": round(max(sprds), 3),
            "spread_min": round(min(sprds), 3),
            "buy_now":    buys[-1],
            "sell_now":   sells[-1],
            "best_spread_time": best[3].strftime("%H:%M"),
            "best_hour":  best_hour,
            "avg_per_h":  avg_per_h,
            "buy_chart":  [b for _, b, _ in data[-20:]],
            "sell_chart": [s for _, _, s in data[-20:]],
        }

    s24 = stats(h24)
    s7d = stats(h7d)

    lines = [f"📈 <b>История спреда</b> | {ex} | {asset}/{fiat}\n"]

    if s24:
        lines += [
            "⏱ <b>Последние 24 часа</b>",
            f"  Сейчас: покупка {s24['buy_now']:,.2f} | продажа {s24['sell_now']:,.2f}",
            f"  Спред: avg <b>{s24['spread_avg']}%</b> | min {s24['spread_min']}% | max {s24['spread_max']}%",
            f"  Пик спреда: {s24['best_spread_time']}",
        ]
        if s24["best_hour"] is not None:
            lines.append(f"  🕐 Лучший час для торговли: <b>{s24['best_hour']}:00</b>")

    if s7d:
        lines += [
            "",
            "📅 <b>7 дней</b>",
            f"  Спред: avg <b>{s7d['spread_avg']}%</b> | min {s7d['spread_min']}% | max {s7d['spread_max']}%",
        ]
        if s7d["avg_per_h"]:
            top3      = sorted(s7d["avg_per_h"].items(), key=lambda x: x[1], reverse=True)[:3]
            hours_str = " → ".join(f"{h}:00 ({v}%)" for h, v in top3)
            lines.append(f"  ⭐ Топ часы: {hours_str}")

    if s24 and s24["buy_chart"]:
        lines += ["", "<code>Цена покупки (24ч):"]
        lines.append(_ascii_chart(s24["buy_chart"]))
        lines.append("</code>")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=_hist_kb(exchange, fiat, asset),
        parse_mode="HTML",
    )


async def collect_prices() -> None:
    """Вызывается каждые 30 мин из bot.py"""
    import logging
    logger = logging.getLogger(__name__)
    pairs = [
        ("binance", "KZT", "USDT"), ("binance", "RUB", "USDT"),
        ("bybit",   "KZT", "USDT"), ("bybit",   "RUB", "USDT"),
        ("binance", "TRY", "USDT"), ("bybit",   "TRY", "USDT"),
    ]
    for exchange, fiat, asset in pairs:
        try:
            if exchange == "binance":
                buy  = await binance_p2p.get_best_price(asset, fiat, "BUY")
                sell = await binance_p2p.get_best_price(asset, fiat, "SELL")
            else:
                buy  = await bybit_p2p.get_best_price(asset, fiat, "1")
                sell = await bybit_p2p.get_best_price(asset, fiat, "0")
            if buy and sell:
                await record_price(exchange, fiat, asset, buy, sell)
        except Exception as e:
            logger.error(f"Price collect error {exchange}/{fiat}: {e}")
