"""
P&L Трекер — считает прибыльность P2P торговли по истории ордеров.
Поддерживаемые биржи: Bybit, OKX, Binance.
Доступно только для подписчиков Pro / Team.

Логика:
• Пользователь выбирает биржу из тех, где есть активный API ключ
• Забирает последние 20 завершённых сделок
• Считает среднюю цену покупки и продажи, маржу и прибыль
"""
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, Message,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import db
from handlers.account_manager import get_account_credentials
from api import bybit_auth, okx_auth, binance_auth
from utils.subscription import get_plan_key

logger = logging.getLogger(__name__)
router = Router()

_EX_NAMES = {
    "bybit":   "🟠 Bybit",
    "okx":     "🔵 OKX",
    "binance": "🟡 Binance",
}
_EXCHANGE_ORDER = ("bybit", "okx", "binance")


# ─── Entry points ──────────────────────────────────────────────────────────────

@router.message(Command("pnl"))
async def pnl_command(message: Message):
    await _show_picker(message.from_user.id, message)


@router.callback_query(lambda c: c.data == "pnl:view")
async def pnl_callback(callback: CallbackQuery):
    await _show_picker(callback.from_user.id, callback)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("pnl:ex:"))
async def pnl_exchange(callback: CallbackQuery):
    exchange = callback.data.split(":")[2]
    await _show_pnl(callback.from_user.id, callback, exchange)
    await callback.answer()


# ─── Picker ────────────────────────────────────────────────────────────────────

async def _show_picker(uid: int, event: Message | CallbackQuery):
    is_cb = isinstance(event, CallbackQuery)

    # ── Проверка подписки ──────────────────────────────────────────────────────
    sub      = await db.subscription_get(uid)
    plan_key = get_plan_key(sub)

    if plan_key == "free":
        text = (
            "📊 <b>P&L Трекер</b>\n\n"
            "⭐ Функция доступна в планах <b>Pro</b> и <b>Team</b>.\n\n"
            "Показывает:\n"
            "• Историю последних сделок с 3 бирж\n"
            "• Среднюю цену покупки и продажи\n"
            "• Маржу и расчётную прибыль\n"
            "• Разбивку по фиатным валютам"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Оформить подписку", callback_data="sub:list")],
            [InlineKeyboardButton(text="⬅️ Назад",             callback_data="back:main")],
        ])
        if is_cb:
            await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await event.answer(text, reply_markup=kb, parse_mode="HTML")
        return

    # ── Доступные биржи (где есть активный API ключ) ───────────────────────────
    avail = [ex for ex in _EXCHANGE_ORDER
             if get_account_credentials(uid, ex)[0]]

    if not avail:
        text = (
            "📊 <b>P&L Трекер</b>\n\n"
            "❌ Нет подключённых API ключей.\n\n"
            "Добавь ключ в <b>🔑 Аккаунты</b>.\n\n"
            "Поддерживается:\n"
            "🟠 Bybit — P2P ключ\n"
            "🔵 OKX — Trade (C2C) ключ\n"
            "🟡 Binance — Read-only ключ"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Аккаунты", callback_data="acc:list")],
            [InlineKeyboardButton(text="⬅️ Назад",    callback_data="back:main")],
        ])
        if is_cb:
            await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await event.answer(text, reply_markup=kb, parse_mode="HTML")
        return

    # Если биржа одна — сразу показываем P&L
    if len(avail) == 1:
        if is_cb:
            await _show_pnl(uid, event, avail[0])
        else:
            # для Message нужен промежуточный ответ — показываем picker
            btns = [[InlineKeyboardButton(
                text=_EX_NAMES[avail[0]],
                callback_data=f"pnl:ex:{avail[0]}",
            )]]
            btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
            await event.answer(
                "📊 <b>P&L Трекер</b>\n\nВыбери биржу:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
                parse_mode="HTML",
            )
        return

    # Несколько бирж — показываем picker
    btns = [
        [InlineKeyboardButton(text=_EX_NAMES[ex], callback_data=f"pnl:ex:{ex}")]
        for ex in avail
    ]
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
    kb   = InlineKeyboardMarkup(inline_keyboard=btns)
    text = "📊 <b>P&L Трекер</b>\n\nВыбери биржу для анализа:"

    if is_cb:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


# ─── P&L по конкретной бирже ───────────────────────────────────────────────────

async def _show_pnl(uid: int, event: CallbackQuery, exchange: str):
    ex_label = _EX_NAMES.get(exchange, exchange.title())

    await event.message.edit_text(f"⏳ Загружаю историю {ex_label}...")

    key, secret, extra = get_account_credentials(uid, exchange)
    if not key:
        await event.message.edit_text(
            f"📊 <b>P&L Трекер</b>\n\n"
            f"❌ Нет активного аккаунта <b>{ex_label}</b>.\n\n"
            "Подключи API ключ в 🔑 Аккаунты.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Аккаунты", callback_data="acc:list")],
                [InlineKeyboardButton(text="⬅️ Назад",    callback_data="pnl:view")],
            ]),
        )
        return

    # ── Запрос ордеров ─────────────────────────────────────────────────────────
    try:
        if exchange == "bybit":
            orders = await bybit_auth.get_p2p_orders(key, secret, status="50")
        elif exchange == "okx":
            orders = await okx_auth.get_p2p_orders(key, secret, extra)
        elif exchange == "binance":
            orders = await binance_auth.get_p2p_orders(key, secret)
        else:
            orders = []
    except Exception as e:
        logger.error(f"P&L orders uid={uid} ex={exchange}: {e}")
        await event.message.edit_text(
            f"📊 <b>P&L · {ex_label}</b>\n\n"
            f"❌ Ошибка API:\n<code>{e}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Повторить", callback_data=f"pnl:ex:{exchange}")],
                [InlineKeyboardButton(text="⬅️ Назад",     callback_data="pnl:view")],
            ]),
        )
        return

    if not orders:
        await event.message.edit_text(
            f"📊 <b>P&L · {ex_label}</b>\n\n"
            "📭 Нет завершённых сделок.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="pnl:view")],
            ]),
        )
        return

    stats = calc_pnl(orders)
    text  = _format_pnl(stats, exchange)
    kb    = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"pnl:ex:{exchange}")],
        [InlineKeyboardButton(text="⬅️ Назад",    callback_data="pnl:view")],
    ])
    await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ─── Вычисления (публичные — используются также в webapp/server.py) ────────────

def calc_pnl(orders: list) -> dict:
    """Считает P&L статистику из нормализованного списка ордеров."""
    buy_orders  = [o for o in orders if str(o.get("side", "")) == "0"]
    sell_orders = [o for o in orders if str(o.get("side", "")) == "1"]

    def _sum(lst, key): return sum(float(o.get(key, 0)) for o in lst)

    buy_qty  = _sum(buy_orders,  "quantity")
    sell_qty = _sum(sell_orders, "quantity")
    buy_amt  = _sum(buy_orders,  "amount")
    sell_amt = _sum(sell_orders, "amount")

    avg_buy  = buy_amt  / buy_qty  if buy_qty  else 0
    avg_sell = sell_amt / sell_qty if sell_qty else 0

    matched_qty = min(buy_qty, sell_qty)
    est_profit  = (avg_sell - avg_buy) * matched_qty if avg_buy and avg_sell else 0
    margin_pct  = (avg_sell - avg_buy) / avg_buy * 100 if avg_buy else 0

    # Группировка по фиатной валюте
    fiats: dict[str, dict] = {}
    for o in orders:
        fiat = o.get("fiat", "?")
        if fiat not in fiats:
            fiats[fiat] = {"buy_qty": 0.0, "sell_qty": 0.0,
                           "buy_amt": 0.0, "sell_amt": 0.0, "count": 0}
        d = fiats[fiat]
        d["count"] += 1
        if str(o.get("side", "")) == "0":
            d["buy_qty"] += float(o.get("quantity", 0))
            d["buy_amt"] += float(o.get("amount",   0))
        else:
            d["sell_qty"] += float(o.get("quantity", 0))
            d["sell_amt"] += float(o.get("amount",   0))

    return {
        "total":       len(orders),
        "buy_cnt":     len(buy_orders),
        "sell_cnt":    len(sell_orders),
        "buy_qty":     buy_qty,
        "sell_qty":    sell_qty,
        "buy_amt":     buy_amt,
        "sell_amt":    sell_amt,
        "avg_buy":     avg_buy,
        "avg_sell":    avg_sell,
        "matched_qty": matched_qty,
        "est_profit":  est_profit,
        "margin_pct":  margin_pct,
        "fiats":       fiats,
    }


def _format_pnl(s: dict, exchange: str = "bybit") -> str:
    def p(n): return f"{n:,.2f}" if n else "—"
    sign    = "+" if s["est_profit"] >= 0 else ""
    ex_name = _EX_NAMES.get(exchange, exchange.title())

    lines = [
        f"📊 <b>P&L Трекер · {ex_name}</b>",
        f"<i>Последние {s['total']} завершённых сделок</i>",
        "",
        f"📥 Покупок:  <b>{s['buy_cnt']}</b>  ({p(s['buy_qty'])} USDT)",
        f"📤 Продаж:   <b>{s['sell_cnt']}</b>  ({p(s['sell_qty'])} USDT)",
        "",
        f"Средняя цена покупки:  <b>{p(s['avg_buy'])}</b>",
        f"Средняя цена продажи:  <b>{p(s['avg_sell'])}</b>",
    ]

    if s["avg_buy"] and s["avg_sell"]:
        profit_emoji = "📈" if s["est_profit"] >= 0 else "📉"
        lines += [
            f"Маржа:  <b>{sign}{s['margin_pct']:.3f}%</b>",
            f"Расч. прибыль:  {profit_emoji} <b>{sign}{p(s['est_profit'])}</b>",
        ]

    if s["fiats"]:
        lines += ["", "━━━ По валютам ━━━"]
        for fiat, d in sorted(s["fiats"].items()):
            avg_b = d["buy_amt"]  / d["buy_qty"]  if d["buy_qty"]  else 0
            avg_s = d["sell_amt"] / d["sell_qty"] if d["sell_qty"] else 0
            lines.append(
                f"<b>{fiat}</b> ({d['count']} сд) · "
                f"📥 {p(avg_b)} · 📤 {p(avg_s)}"
            )

    lines.append("\n<i>⚠️ Расчёт приблизительный — на основе публичных данных.</i>")
    return "\n".join(lines)
