"""
AI Советник на базе Gemini 2.0 Flash.

Что делает:
1. Загружает текущий стакан (обе стороны)
2. Загружает паттерны из price_history
3. Строит детальный промпт с контекстом рынка
4. Отправляет в Gemini
5. Возвращает конкретную рекомендацию по цене

Кулдаун 20 секунд между запросами (защита от спама в бесплатном тире).
"""
import asyncio
import time
from datetime import datetime
from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from api import binance_p2p, bybit_p2p, gemini
from handlers.price_history import get_history
from handlers.pattern_engine import _compute_patterns
from utils.spread import calc_spread
from config import FIATS, FIAT_FLAGS

router = Router()

# Кулдаун: {user_id: timestamp_последнего_запроса}
_cooldowns: dict[int, float] = {}
COOLDOWN_SEC = 20

DAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


# ─── UI ───────────────────────────────────────────────────────────────────────

def _start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟡 Binance", callback_data="ai:ex:binance"),
            InlineKeyboardButton(text="🟠 Bybit",   callback_data="ai:ex:bybit"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")],
    ])


def _fiat_kb(exchange: str) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(
        text=FIAT_FLAGS.get(f, f),
        callback_data=f"ai:go:{exchange}:{f}:USDT",
    )] for f in FIATS]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="ai:start")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _result_kb(exchange: str, fiat: str, asset: str) -> InlineKeyboardMarkup:
    other   = "bybit" if exchange == "binance" else "binance"
    other_l = "🟠 Bybit" if other == "bybit" else "🟡 Binance"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Спросить снова",   callback_data=f"ai:go:{exchange}:{fiat}:{asset}")],
        [InlineKeyboardButton(text=f"↔️ {other_l}",       callback_data=f"ai:go:{other}:{fiat}:{asset}")],
        [InlineKeyboardButton(text="🔙 Сменить пару",     callback_data=f"ai:ex:{exchange}")],
        [InlineKeyboardButton(text="⬅️ Назад",            callback_data="back:main")],
    ])


# ─── Промпт ───────────────────────────────────────────────────────────────────

def _build_prompt(
    exchange: str, fiat: str, asset: str,
    buy_ads: list, sell_ads: list,
    patterns: dict | None,
) -> str:
    now      = datetime.now()
    ex_label = "Binance" if exchange == "binance" else "Bybit"
    day_name = DAYS_RU[now.weekday()]

    # Форматируем объявления
    def fmt_ad(ad: dict, idx: int) -> str:
        compl = float(ad.get("completion", 0))
        if 0 < compl <= 1:
            compl *= 100
        pay = " / ".join(list(dict.fromkeys(ad["pay_types"]))[:2]) or "—"
        return (
            f"  #{idx+1} {ad['nickname']} | "
            f"цена {ad['price']:,.2f} | "
            f"лимит {ad['min_amount']:,.0f}-{ad['max_amount']:,.0f} | "
            f"{int(ad['orders'])} сд. | {compl:.0f}% | {pay}"
        )

    buy_block  = "\n".join(fmt_ad(a, i) for i, a in enumerate(buy_ads[:6]))
    sell_block = "\n".join(fmt_ad(a, i) for i, a in enumerate(sell_ads[:6]))

    # Спред
    spread_line = ""
    if buy_ads and sell_ads:
        sp = calc_spread(buy_ads[0]["price"], sell_ads[0]["price"])
        spread_line = f"Спред: {sp['spread_abs']:,.2f} ({sp['spread_pct']}%)"

    # Паттерны
    pat_block = ""
    if patterns and patterns.get("points", 0) >= 10:
        avg = patterns["avg_all"]
        cur = patterns["current"]
        vs  = patterns.get("vs_avg")
        vs_s = f"{'+' if vs and vs >= 0 else ''}{vs}%" if vs is not None else "нет данных"

        best_h = sorted(patterns["hour_avg"].items(), key=lambda x: x[1], reverse=True)[:3]
        best_h_str = ", ".join(f"{h:02d}:00 ({v:.2f}%)" for h, v in best_h)

        trend_s = ""
        if patterns.get("trend"):
            tr    = patterns["trend"]
            direc = "растёт" if tr["direction"] == "up" else "падает"
            trend_s = f"\n  Тренд 3ч: спред {direc} ({'+' if tr['direction']=='up' else ''}{tr['delta']}%)"

        pat_block = (
            f"\nИСТОРИЯ (последние {patterns['points']} точек по 30 мин):\n"
            f"  Средний спред: {avg}% | Текущий: {cur}% (vs среднее: {vs_s})\n"
            f"  Лучшие часы: {best_h_str}"
            f"{trend_s}"
        )

    return f"""Ты опытный трейдер P2P крипторынка. Проанализируй данные и дай точную рекомендацию на русском.

═══ ДАННЫЕ РЫНКА ═══
Биржа: {ex_label} | Пара: {asset}/{fiat}
Время: {now.strftime('%H:%M')} | День: {day_name}

ТЫ ПОКУПАЕШЬ {asset} (мерчанты выставили на продажу):
{buy_block or '  Нет данных'}

ТЫ ПРОДАЁШЬ {asset} (мерчанты готовы купить):
{sell_block or '  Нет данных'}

{spread_line}
{pat_block}

═══ ЗАДАНИЕ ═══
Дай рекомендацию СТРОГО в этом формате (без лишних слов):

💡 ЦЕНА ДЛЯ ВЫСТАВЛЕНИЯ: [конкретное число] {fiat}
   (Если продаю {asset}: [число] | Если покупаю: [число])

📊 ПОЧЕМУ: [2-3 предложения с конкретными аргументами]

⏱ МОМЕНТ: [Хороший / Средний / Плохой] — [одно предложение почему]

🔮 ПРОГНОЗ: [что ожидать в следующие 1-2 часа]

⚠️ РИСК: [Низкий / Средний / Высокий] — [причина]

Говори конкретно. Называй числа. Без воды."""


# ─── Handlers ──────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "ai:start")
async def ai_start(callback: CallbackQuery):
    await callback.message.edit_text(
        "🤖 <b>AI Советник</b>\n\n"
        "Анализирую стакан + историю паттернов.\n"
        "Даю конкретную цену и рекомендацию.\n\n"
        "Выбери биржу:",
        reply_markup=_start_kb(),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("ai:ex:"))
async def ai_choose_fiat(callback: CallbackQuery):
    exchange = callback.data.split(":")[2]
    ex_label = "🟡 Binance" if exchange == "binance" else "🟠 Bybit"
    await callback.message.edit_text(
        f"🤖 AI Советник | {ex_label}\n\nВыбери валютную пару:",
        reply_markup=_fiat_kb(exchange),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("ai:go:"))
async def ai_go(callback: CallbackQuery):
    parts            = callback.data.split(":")
    exchange, fiat, asset = parts[2], parts[3], parts[4]
    uid              = callback.from_user.id

    # Кулдаун
    now = time.time()
    last = _cooldowns.get(uid, 0)
    if now - last < COOLDOWN_SEC:
        wait = int(COOLDOWN_SEC - (now - last))
        await callback.answer(f"⏳ Подожди {wait} сек.", show_alert=True)
        return

    await callback.message.edit_text(
        f"🤖 <b>AI анализирует {asset}/{fiat}...</b>\n\n"
        f"⏳ Загружаю стакан и историю...",
        parse_mode="HTML",
    )

    # Грузим данные параллельно
    try:
        if exchange == "binance":
            buy_ads_task  = binance_p2p.get_ads(asset=asset, fiat=fiat, trade_type="BUY",  rows=8)
            sell_ads_task = binance_p2p.get_ads(asset=asset, fiat=fiat, trade_type="SELL", rows=8)
        else:
            buy_ads_task  = bybit_p2p.get_ads(asset=asset, fiat=fiat, side="1", size=8)
            sell_ads_task = bybit_p2p.get_ads(asset=asset, fiat=fiat, side="0", size=8)

        buy_ads, sell_ads = await asyncio.gather(buy_ads_task, sell_ads_task)
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка загрузки данных: {e}")
        return

    # Паттерны из истории
    hist     = list(get_history(exchange, fiat, asset))
    patterns = _compute_patterns(hist) if len(hist) >= 10 else None

    # Строим промпт и спрашиваем AI
    await callback.message.edit_text(
        f"🤖 <b>AI анализирует {asset}/{fiat}...</b>\n\n"
        f"🧠 Обрабатываю данные...",
        parse_mode="HTML",
    )

    prompt   = _build_prompt(exchange, fiat, asset, buy_ads, sell_ads, patterns)
    response = await gemini.ask(prompt)

    _cooldowns[uid] = time.time()

    ex_label = "🟡 Binance" if exchange == "binance" else "🟠 Bybit"
    now_s    = datetime.now().strftime("%H:%M")

    header = (
        f"🤖 <b>AI Советник</b> | {ex_label} | {asset}/{fiat}\n"
        f"<i>Анализ от {now_s}</i>\n"
        f"{'─' * 30}\n\n"
    )

    # Если ответ начинается с ошибки — не добавляем заголовок
    if response.startswith("❌") or response.startswith("⏳"):
        text = response
    else:
        text = header + response

    try:
        await callback.message.edit_text(
            text,
            reply_markup=_result_kb(exchange, fiat, asset),
            parse_mode="HTML",
        )
    except Exception:
        # Если HTML в ответе сломал разметку — отправляем без parse_mode
        await callback.message.edit_text(
            text,
            reply_markup=_result_kb(exchange, fiat, asset),
        )
