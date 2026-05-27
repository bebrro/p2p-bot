"""
Pattern Engine — статистический анализ исторических паттернов.

Использует данные из price_history.py (собираются каждые 30 мин).
Находит:
  • Лучшие часы для торговли (по среднему спреду)
  • Лучшие дни недели
  • Аномалии: текущий момент vs исторический средний
  • Тренд последних 3 часов (растёт/падает спред)
  • Прогноз: "сейчас исторически выгодно/невыгодно"

Требует минимум 48 точек (24ч) для анализа по часам,
7+ дней для анализа по дням недели.
"""
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from utils.spread import calc_spread
from handlers.price_history import get_history
from config import FIATS, FIAT_FLAGS

router = Router()

DAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MIN_POINTS_HOUR = 10   # минимум точек для анализа по часам
MIN_POINTS_DAY  = 3    # минимум точек для анализа по дням


# ─── Статистические функции ────────────────────────────────────────────────────

def _compute_patterns(hist: list) -> dict | None:
    """
    hist: [(ts, buy, sell), ...]
    Возвращает словарь с паттернами или None если данных недостаточно.
    """
    if len(hist) < MIN_POINTS_HOUR:
        return None

    spreads_all  = []
    by_hour: dict[int, list]    = defaultdict(list)
    by_weekday: dict[int, list] = defaultdict(list)

    for ts, buy, sell in hist:
        if buy <= 0 or sell <= 0:
            continue
        sp = calc_spread(buy, sell)["spread_pct"]
        spreads_all.append(sp)
        by_hour[ts.hour].append(sp)
        by_weekday[ts.weekday()].append(sp)

    if not spreads_all:
        return None

    avg_all = statistics.mean(spreads_all)
    std_all = statistics.stdev(spreads_all) if len(spreads_all) > 1 else 0

    # Средний спред по часу (только если достаточно данных)
    hour_avg = {
        h: statistics.mean(vals)
        for h, vals in by_hour.items()
        if len(vals) >= 2
    }

    # По дням недели
    day_avg = {
        d: statistics.mean(vals)
        for d, vals in by_weekday.items()
        if len(vals) >= MIN_POINTS_DAY
    }

    # Тренд последних 3 часов
    now = datetime.now()
    recent = [(ts, buy, sell) for ts, buy, sell in hist
              if ts > now - timedelta(hours=3)]
    trend = None
    if len(recent) >= 2:
        sp_recent = [calc_spread(b, s)["spread_pct"] for _, b, s in recent]
        # Линейный тренд: сравниваем первую и последнюю треть
        n = len(sp_recent)
        first_third = statistics.mean(sp_recent[:max(1, n//3)])
        last_third  = statistics.mean(sp_recent[max(1, -n//3):])
        delta = last_third - first_third
        if abs(delta) > 0.01:
            trend = {"direction": "up" if delta > 0 else "down", "delta": round(delta, 3)}

    # Последние значения (текущий момент)
    if hist:
        last_ts, last_buy, last_sell = hist[-1]
        current_spread = calc_spread(last_buy, last_sell)["spread_pct"]
        current_buy    = last_buy
        current_sell   = last_sell
    else:
        current_spread = current_buy = current_sell = 0

    # Текущий час vs исторический
    cur_hour = datetime.now().hour
    hour_hist_avg = hour_avg.get(cur_hour)
    if hour_hist_avg and avg_all:
        hour_delta = current_spread - hour_hist_avg
        vs_avg     = current_spread - avg_all
    else:
        hour_delta = vs_avg = None

    return {
        "avg_all":       round(avg_all, 3),
        "std_all":       round(std_all, 3),
        "current":       round(current_spread, 3),
        "current_buy":   current_buy,
        "current_sell":  current_sell,
        "hour_avg":      hour_avg,
        "day_avg":       day_avg,
        "trend":         trend,
        "hour_delta":    round(hour_delta, 3) if hour_delta is not None else None,
        "vs_avg":        round(vs_avg, 3) if vs_avg is not None else None,
        "cur_hour":      cur_hour,
        "points":        len(spreads_all),
    }


def _bar(value: float, min_v: float, max_v: float, width: int = 8) -> str:
    """ASCII-бар для отображения значения на шкале."""
    if max_v == min_v:
        filled = width // 2
    else:
        filled = round((value - min_v) / (max_v - min_v) * width)
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _format_report(p: dict, exchange: str, fiat: str, asset: str) -> str:
    ex_label = "🟡 Binance" if exchange == "binance" else "🟠 Bybit"
    lines    = [f"🧠 <b>Pattern Engine</b> | {ex_label} | {asset}/{fiat}\n"]

    # Текущий момент
    now_s    = datetime.now().strftime("%H:%M")
    cur_hour = p["cur_hour"]
    lines.append(f"⏱ Сейчас {now_s} | Спред: <b>{p['current']}%</b>")
    lines.append(f"   Покупка: {p['current_buy']:,.2f} | Продажа: {p['current_sell']:,.2f}")

    # Оценка текущего момента
    if p["vs_avg"] is not None:
        if p["vs_avg"] > 0.05:
            moment_label = "🔥 Выше среднего — хороший момент!"
        elif p["vs_avg"] < -0.05:
            moment_label = "❄️ Ниже среднего — не лучший момент"
        else:
            moment_label = "➖ На уровне среднего"
        sign = "+" if p["vs_avg"] >= 0 else ""
        lines.append(f"   {moment_label} ({sign}{p['vs_avg']}% vs avg {p['avg_all']}%)")

    # Тренд последних 3 часов
    if p["trend"]:
        tr = p["trend"]
        arrow  = "📈" if tr["direction"] == "up" else "📉"
        action = "растёт" if tr["direction"] == "up" else "падает"
        sign   = "+" if tr["direction"] == "up" else ""
        lines.append(f"\n{arrow} Тренд 3ч: спред <b>{action}</b> ({sign}{tr['delta']}%)")

    # Топ-5 лучших часов
    if p["hour_avg"] and len(p["hour_avg"]) >= 3:
        lines.append("\n⏰ <b>Лучшие часы для торговли</b>")
        sorted_hours = sorted(p["hour_avg"].items(), key=lambda x: x[1], reverse=True)
        all_vals     = list(p["hour_avg"].values())
        min_v, max_v = min(all_vals), max(all_vals)

        for rank, (h, avg) in enumerate(sorted_hours[:5], 1):
            bar    = _bar(avg, min_v, max_v)
            mark   = " ← сейчас" if h == cur_hour else ""
            medal  = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][rank - 1]
            lines.append(f"   {medal} {h:02d}:00  {bar}  {avg:.3f}%{mark}")

        # Худшие часы
        worst_3 = sorted_hours[-3:][::-1]
        lines.append("\n⚠️ <b>Часы с низким спредом:</b>")
        for h, avg in worst_3:
            mark = " ← сейчас" if h == cur_hour else ""
            lines.append(f"   {h:02d}:00 — {avg:.3f}%{mark}")

    # По дням недели
    if p["day_avg"] and len(p["day_avg"]) >= 3:
        lines.append("\n📅 <b>По дням недели</b>")
        sorted_days = sorted(p["day_avg"].items(), key=lambda x: x[1], reverse=True)
        all_vals    = list(p["day_avg"].values())
        min_v, max_v = min(all_vals), max(all_vals)
        today       = datetime.now().weekday()

        for d, avg in sorted_days:
            bar  = _bar(avg, min_v, max_v)
            mark = " ← сегодня" if d == today else ""
            lines.append(f"   {DAYS_RU[d]}  {bar}  {avg:.3f}%{mark}")

    # Вывод: рекомендация
    lines.append(f"\n📊 Данных: {p['points']} точек | avg={p['avg_all']}% ±{p['std_all']}%")

    # Финальный вывод
    if p["vs_avg"] is not None:
        lines.append("")
        best_hours = sorted(p["hour_avg"].items(), key=lambda x: x[1], reverse=True)[:3]
        best_h_str = " / ".join(f"{h:02d}:00" for h, _ in best_hours)
        if cur_hour in [h for h, _ in best_hours]:
            lines.append("⭐ <b>Сейчас — один из лучших часов для торговли!</b>")
        else:
            lines.append(f"💡 Лучшие моменты: <b>{best_h_str}</b>")

    return "\n".join(lines)


# ─── Keyboard ──────────────────────────────────────────────────────────────────

def _fiat_kb_pat(exchange: str) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(
        text=FIAT_FLAGS.get(f, f),
        callback_data=f"pat:{exchange}:{f}:USDT",
    )] for f in FIATS]
    other   = "bybit" if exchange == "binance" else "binance"
    other_l = "🟠 Bybit" if other == "bybit" else "🟡 Binance"
    buttons.append([InlineKeyboardButton(text=f"↔️ {other_l}", callback_data=f"pat:{other}:{FIATS[0]}:USDT")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _report_kb(exchange: str, fiat: str, asset: str) -> InlineKeyboardMarkup:
    other   = "bybit" if exchange == "binance" else "binance"
    other_l = "🟠 Bybit" if other == "bybit" else "🟡 Binance"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"↔️ {other_l}", callback_data=f"pat:{other}:{fiat}:{asset}")],
        [InlineKeyboardButton(text="🔄 Обновить",   callback_data=f"pat:{exchange}:{fiat}:{asset}")],
        [InlineKeyboardButton(text="⬅️ Назад",      callback_data="back:main")],
    ])


# ─── Handlers ──────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "pat:start")
async def pat_start(callback: CallbackQuery):
    await callback.message.edit_text(
        "🧠 <b>Pattern Engine</b>\n\n"
        "Анализирую историю цен и нахожу закономерности:\n"
        "• Лучшие часы и дни для торговли\n"
        "• Текущий момент vs исторический средний\n"
        "• Тренд последних 3 часов\n\n"
        "Начни с Binance:",
        reply_markup=_fiat_kb_pat("binance"),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("pat:") and len(c.data.split(":")) == 4)
async def pat_show(callback: CallbackQuery):
    _, exchange, fiat, asset = callback.data.split(":")
    await callback.message.edit_text("🧠 Анализирую данные...")

    hist = list(get_history(exchange, fiat, asset))

    if len(hist) < MIN_POINTS_HOUR:
        needed_h = (MIN_POINTS_HOUR - len(hist)) * 30
        await callback.message.edit_text(
            f"🧠 <b>Pattern Engine</b> | {asset}/{fiat}\n\n"
            f"⏳ Накапливаю данные...\n"
            f"Есть: {len(hist)} точек из {MIN_POINTS_HOUR} минимальных\n"
            f"Ждать ещё: ~{needed_h} минут\n\n"
            f"История собирается каждые 30 минут автоматически.\n"
            f"Зайди позже — паттерны появятся через несколько часов.",
            reply_markup=_report_kb(exchange, fiat, asset),
            parse_mode="HTML",
        )
        return

    patterns = _compute_patterns(hist)
    if not patterns:
        await callback.message.edit_text(
            "❌ Недостаточно данных для анализа.",
            reply_markup=_report_kb(exchange, fiat, asset),
        )
        return

    text = _format_report(patterns, exchange, fiat, asset)
    await callback.message.edit_text(
        text,
        reply_markup=_report_kb(exchange, fiat, asset),
        parse_mode="HTML",
    )
