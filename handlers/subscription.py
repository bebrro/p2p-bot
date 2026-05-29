"""
Монетизация: планы Free / Pro / Team.
Оплата через Telegram Stars (currency="XTR").

Команды:
  /subscribe        — показать планы и купить
  /give_pro ID DAYS — выдать подписку (только ADMIN_IDS)
"""
import logging
from datetime import datetime, timezone, timedelta

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, Message,
    InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery,
)

import db
from config import ADMIN_IDS
from utils.subscription import PLANS, get_plan_key, format_plan_card, format_expires

logger = logging.getLogger(__name__)
router = Router()


# ─── Keyboard helpers ──────────────────────────────────────────────────────────

def _sub_kb(current_plan: str) -> InlineKeyboardMarkup:
    btns = []
    if current_plan != "pro":
        btns.append([InlineKeyboardButton(
            text="⭐ Pro — 500 Stars / мес",  callback_data="sub:buy:pro",
        )])
    if current_plan != "team":
        btns.append([InlineKeyboardButton(
            text="👑 Team — 1500 Stars / мес", callback_data="sub:buy:team",
        )])
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=btns)


async def _get_sub_text(uid: int) -> tuple[str, str]:
    """Возвращает (html_text, plan_key)."""
    sub      = await db.subscription_get(uid)
    plan_key = get_plan_key(sub)
    expires  = format_expires(sub) if sub else ""

    lines = [
        "⭐ <b>Подписка P2P Monitor</b>",
        "",
        f"Твой план: <b>{PLANS[plan_key]['emoji']} {PLANS[plan_key]['name']}</b>"
        + (f"  —  {expires}" if expires else ""),
        "",
        "─────────────────",
        format_plan_card("free",  plan_key == "free"),
        "",
        format_plan_card("pro",   plan_key == "pro"),
        "",
        format_plan_card("team",  plan_key == "team"),
        "",
        "─────────────────",
        "💳 Оплата через <b>Telegram Stars</b>",
        "Купить Stars: Telegram → ⚙️ Настройки → Telegram Stars",
    ]
    return "\n".join(lines), plan_key


# ─── Handlers ──────────────────────────────────────────────────────────────────

@router.message(Command("subscribe"))
@router.callback_query(lambda c: c.data == "sub:list")
async def sub_list(event: Message | CallbackQuery):
    uid = event.from_user.id
    text, plan_key = await _get_sub_text(uid)
    kb = _sub_kb(plan_key)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data in ("sub:buy:pro", "sub:buy:team"))
async def sub_buy(callback: CallbackQuery):
    plan_key = callback.data.split(":")[2]          # "pro" or "team"
    plan     = PLANS.get(plan_key)
    if not plan:
        await callback.answer("Ошибка плана", show_alert=True)
        return

    stars = plan["price_stars"]
    days  = plan["duration_days"]

    await callback.message.answer_invoice(
        title       = f"P2P Monitor {plan['name']} — {days} дней",
        description = (
            f"✅ Репрайсер до {plan['repricer_rules']} правил\n"
            f"✅ Трекеры конкурентов до {plan['trackers']}\n"
            f"✅ Алерты до {plan['alerts']}\n"
            f"✅ Smart Арбитраж (4 биржи)\n"
            f"✅ P&L трекер"
        ),
        payload    = f"{plan_key}:{days}",
        currency   = "XTR",
        prices     = [LabeledPrice(label=f"P2P Monitor {plan['name']}", amount=stars)],
    )
    await callback.answer()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    """Telegram требует ответа в течение 10 сек. Всегда подтверждаем."""
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message):
    uid     = message.from_user.id
    payload = message.successful_payment.invoice_payload   # "pro:30"
    stars   = message.successful_payment.total_amount

    try:
        plan_key, days_str = payload.split(":")
        days = int(days_str)
    except Exception:
        logger.error(f"Invalid payment payload: {payload!r}")
        await message.answer("✅ Оплата получена! Свяжитесь с поддержкой для активации.")
        return

    if plan_key not in PLANS:
        logger.error(f"Unknown plan in payload: {plan_key!r}")
        return

    expires_at = datetime.now(timezone.utc) + timedelta(days=days)
    await db.subscription_set(uid, plan_key, expires_at)

    plan = PLANS[plan_key]
    await message.answer(
        f"🎉 <b>Подписка активирована!</b>\n\n"
        f"План: <b>{plan['emoji']} {plan['name']}</b>\n"
        f"Действует: <b>{days} дней</b>  →  {expires_at.strftime('%d.%m.%Y')}\n"
        f"Оплачено: <b>{stars} ⭐</b>\n\n"
        f"Разблокировано:\n"
        f"✅ Репрайсер до {plan['repricer_rules']} правил\n"
        f"✅ Трекеры до {plan['trackers']}\n"
        f"✅ Smart Арбитраж (4 биржи)\n"
        f"✅ P&L трекер",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 P&L Трекер", callback_data="pnl:view")],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back:main")],
        ]),
    )
    logger.info(f"Subscription: uid={uid} plan={plan_key} days={days} stars={stars}")


# ─── Admin ─────────────────────────────────────────────────────────────────────

@router.message(Command("give_pro"))
async def admin_give_pro(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return   # молча игнорируем

    parts = message.text.split()
    # /give_pro USER_ID [DAYS] [plan]
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/give_pro USER_ID [DAYS] [pro|team]</code>\n"
            "Пример: <code>/give_pro 123456789 30 pro</code>",
            parse_mode="HTML",
        )
        return

    try:
        target_uid = int(parts[1])
        days       = int(parts[2]) if len(parts) >= 3 else 30
        plan_key   = parts[3].lower() if len(parts) >= 4 else "pro"
        if plan_key not in PLANS:
            plan_key = "pro"
    except ValueError:
        await message.answer("❌ Неверный формат. /give_pro USER_ID DAYS [pro|team]")
        return

    expires_at = datetime.now(timezone.utc) + timedelta(days=days)
    await db.subscription_set(target_uid, plan_key, expires_at)

    plan = PLANS[plan_key]
    await message.answer(
        f"✅ Подписка выдана!\n"
        f"Пользователь: <code>{target_uid}</code>\n"
        f"План: {plan['emoji']} {plan['name']}\n"
        f"Действует до: {expires_at.strftime('%d.%m.%Y')} ({days}д)",
        parse_mode="HTML",
    )
