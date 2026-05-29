"""
P&L Трекер — считает прибыльность P2P торговли по истории ордеров Bybit.
Доступно только для подписчиков Pro / Team.

Логика:
• Забирает последние 20 завершённых сделок (status="50")
• Группирует по side: 0=покупка крипто, 1=продажа крипто
• Считает среднюю цену покупки и продажи
• Оценивает маржу и расчётную прибыль
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
from api import bybit_auth
from utils.subscription import get_plan_key

logger = logging.getLogger(__name__)
router = Router()


# ─── Handlers ──────────────────────────────────────────────────────────────────

@router.message(Command("pnl"))
async def pnl_command(message: Message):
    await _show_pnl(message.from_user.id, message)


@router.callback_query(lambda c: c.data == "pnl:view")
async def pnl_callback(callback: CallbackQuery):
    await _show_pnl(callback.from_user.id, callback)


async def _show_pnl(uid: int, event: Message | CallbackQuery):
    is_cb = isinstance(event, CallbackQuery)

    # ── Проверка подписки ──────────────────────────────────────────────────────
    sub      = await db.subscription_get(uid)
    plan_key = get_plan_key(sub)

    if plan_key == "free":
        text = (
            "📊 <b>P&L Трекер</b>\n\n"
            "⭐ Функция доступна в планах <b>Pro</b> и <b>Team</b>.\n\n"
            "Покажет:\n"
            "• Историю последних 20 завершённых сделок\n"
            "• Среднюю цену покупки и продажи\n"
            "• Маржу и расчётную прибыль\n"
            "• Разбивку по валютам"
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

    # ── Загружаем данные ───────────────────────────────────────────────────────
    wait_msg = None
    if is_cb:
        await event.message.edit_text("⏳ Загружаю историю ордеров...")
    else:
        wait_msg = await event.answer("⏳ Загружаю историю ордеров...")

    api_key, api_secret, _ = get_account_credentials(uid, "bybit")

    if not api_key:
        text = (
            "📊 <b>P&L Трекер</b>\n\n"
            "❌ Нет активного <b>Bybit</b> аккаунта.\n\n"
            "Подключи API ключ в 🔑 Аккаунты."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Аккаунты", callback_data="acc:list")],
            [InlineKeyboardButton(text="⬅️ Назад",    callback_data="back:main")],
        ])
        msg = event.message if is_cb else wait_msg
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        return

    try:
        orders = await bybit_auth.get_p2p_orders(api_key, api_secret, status="50")
    except Exception as e:
        logger.error(f"P&L orders fetch uid={uid}: {e}")
        text = f"📊 <b>P&L Трекер</b>\n\n❌ Ошибка API:\n<code>{e}</code>"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Повторить", callback_data="pnl:view")],
            [InlineKeyboardButton(text="⬅️ Назад",     callback_data="back:main")],
        ])
        msg = event.message if is_cb else wait_msg
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        return

    if not orders:
        text = "📊 <b>P&L Трекер</b>\n\n📭 Нет завершённых сделок на Bybit."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")]
        ])
        msg = event.message if is_cb else wait_msg
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        return

    stats = calc_pnl(orders)
    text  = _format_pnl(stats)
    kb    = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="pnl:view")],
        [InlineKeyboardButton(text="⬅️ Назад",    callback_data="back:main")],
    ])
    msg = event.message if is_cb else wait_msg
    await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ─── Calculations (public — used by server.py too) ─────────────────────────────

def calc_pnl(orders: list) -> dict:
    """Считает P&L статистику из списка ордеров."""
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

    # Группировка по валюте
    fiats: dict[str, dict] = {}
    for o in orders:
        fiat = o.get("fiat", "?")
        if fiat not in fiats:
            fiats[fiat] = {"buy_qty": 0, "sell_qty": 0, "buy_amt": 0, "sell_amt": 0, "count": 0}
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


def _format_pnl(s: dict) -> str:
    def p(n): return f"{n:,.2f}" if n else "—"
    sign = "+" if s["est_profit"] >= 0 else ""

    lines = [
        "📊 <b>P&L Трекер</b>",
        f"<i>Последние {s['total']} завершённых сделок · Bybit</i>",
        "",
        f"📥 Покупок:  <b>{s['buy_cnt']}</b>  ({p(s['buy_qty'])} USDT)",
        f"📤 Продаж:   <b>{s['sell_cnt']}</b>  ({p(s['sell_qty'])} USDT)",
        "",
        f"Средняя цена покупки:  <b>{p(s['avg_buy'])}</b>",
        f"Средняя цена продажи:  <b>{p(s['avg_sell'])}</b>",
    ]

    if s["avg_buy"] and s["avg_sell"]:
        lines += [
            f"Маржа:  <b>{sign}{s['margin_pct']:.3f}%</b>",
            f"Расч. прибыль:  <b>{sign}{p(s['est_profit'])}</b>",
        ]

    if s["fiats"]:
        lines += ["", "━━━ По валютам ━━━"]
        for fiat, d in s["fiats"].items():
            avg_b = d["buy_amt"]  / d["buy_qty"]  if d["buy_qty"]  else 0
            avg_s = d["sell_amt"] / d["sell_qty"] if d["sell_qty"] else 0
            lines.append(
                f"<b>{fiat}</b> ({d['count']} сд)  "
                f"📥 {p(avg_b)}  📤 {p(avg_s)}"
            )

    lines += [
        "",
        "<i>⚠️ Расчёт приблизительный. OKX в разработке.</i>",
    ]
    return "\n".join(lines)
