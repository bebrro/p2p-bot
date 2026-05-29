"""
Реферальная система.

Логика:
• /ref → уникальная ссылка + статистика
• /start ref_{uid} → новый пользователь пришёл по ссылке
  - Рефереру: +7 дней Pro за каждого нового
  - Новому: стандартный 3-дневный триал (в start.py)
• /digest → включить / выключить утренний дайджест
"""
import logging
from datetime import datetime, timezone, timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    Bot,
)

import db
from utils.subscription import PLANS, get_plan_key

logger = logging.getLogger(__name__)
router = Router()

BONUS_DAYS = 7   # дней Pro за каждого приглашённого


# ─── Расширение подписки ───────────────────────────────────────────────────────

async def extend_subscription(user_id: int, days: int) -> datetime:
    """
    Добавляет N дней Pro к подписке пользователя.
    Если lifetime — не трогаем. Возвращает новую дату окончания.
    """
    sub      = await db.subscription_get(user_id)
    plan_key = get_plan_key(sub)

    if plan_key != "free" and sub and sub.get("expires_at") is None:
        return None  # lifetime — не трогаем

    if plan_key == "free" or not sub:
        base = datetime.now(timezone.utc)
    else:
        expires_at = sub["expires_at"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        base = max(expires_at, datetime.now(timezone.utc))

    new_expires = base + timedelta(days=days)
    # Всегда апгрейдим до Pro при начислении бонуса
    await db.subscription_set(user_id, "pro", new_expires)
    return new_expires


# ─── Обработка реферала при /start ────────────────────────────────────────────

async def process_referral(referrer_id: int, referred_id: int, bot: Bot) -> None:
    """
    Вызывается из start.py когда новый пользователь пришёл по реф-ссылке.
    Начисляет бонус рефереру и уведомляет его.
    """
    if referrer_id == referred_id:
        return   # нельзя пригласить самого себя

    saved = await db.referral_add(referrer_id, referred_id)
    if not saved:
        return   # этот пользователь уже был кем-то приглашён

    new_expires = await extend_subscription(referrer_id, BONUS_DAYS)
    if new_expires is None:
        bonus_text = f"+{BONUS_DAYS} дней Pro (у тебя lifetime — бонус не нужен 😉)"
    else:
        bonus_text = f"+{BONUS_DAYS} дней Pro → до {new_expires.strftime('%d.%m.%Y')}"

    total = await db.referral_count(referrer_id)

    try:
        await bot.send_message(
            referrer_id,
            f"🎉 <b>По твоей ссылке пришёл новый пользователь!</b>\n\n"
            f"Бонус: <b>{bonus_text}</b>\n"
            f"Всего приглашено: <b>{total}</b> чел.\n\n"
            f"Продолжай делиться ссылкой — каждый новый = +{BONUS_DAYS} дней Pro!",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Referral notify failed uid={referrer_id}: {e}")

    logger.info(f"Referral: referrer={referrer_id} referred={referred_id} bonus={BONUS_DAYS}d")


# ─── /ref ─────────────────────────────────────────────────────────────────────

@router.message(Command("ref"))
@router.callback_query(lambda c: c.data == "ref:show")
async def cmd_ref(event: Message | CallbackQuery, bot: Bot):
    uid      = event.from_user.id
    bot_info = await bot.get_me()
    link     = f"https://t.me/{bot_info.username}?start=ref_{uid}"
    count    = await db.referral_count(uid)
    bonus    = count * BONUS_DAYS

    text = (
        f"👥 <b>Реферальная программа</b>\n\n"
        f"Приглашай друзей — зарабатывай бесплатные дни Pro!\n\n"
        f"🔗 Твоя ссылка:\n"
        f"<code>{link}</code>\n\n"
        f"За каждого нового пользователя:\n"
        f"<b>+{BONUS_DAYS} дней Pro</b> тебе · стандартный триал другу\n\n"
        f"📊 Твоя статистика:\n"
        f"  Приглашено: <b>{count}</b> чел.\n"
        f"  Заработано: <b>{bonus}</b> дней Pro\n\n"
        f"💡 Поделись ссылкой в чате P2P мерчантов — "
        f"это лучший способ получить Pro бесплатно!"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить статистику", callback_data="ref:show")],
        [InlineKeyboardButton(text="⬅️ Главное меню",       callback_data="back:main")],
    ])
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


# ─── /digest ──────────────────────────────────────────────────────────────────

@router.message(Command("digest"))
@router.callback_query(lambda c: c.data == "digest:settings")
async def cmd_digest(event: Message | CallbackQuery):
    uid      = event.from_user.id
    settings = await db.user_settings_get(uid)
    enabled  = settings.get("digest_enabled", True)

    status = "✅ включён" if enabled else "❌ выключен"
    text   = (
        f"☀️ <b>Ежедневный дайджест</b>\n\n"
        f"Статус: <b>{status}</b>\n\n"
        f"Каждое утро в <b>9:00 МСК</b> бот присылает:\n"
        f"  • Лучший спред по всем фиатам\n"
        f"  • Лучшую арбитражную возможность\n"
        f"  • Сравнение с вчера\n\n"
        f"{'Нажми кнопку ниже чтобы выключить.' if enabled else 'Нажми кнопку ниже чтобы включить.'}"
    )
    toggle_text = "🔕 Выключить дайджест" if enabled else "🔔 Включить дайджест"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data="digest:toggle")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back:main")],
    ])
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "digest:toggle")
async def digest_toggle(callback: CallbackQuery):
    uid      = callback.from_user.id
    settings = await db.user_settings_get(uid)
    new_val  = not settings.get("digest_enabled", True)
    await db.user_settings_set(uid, new_val)
    await callback.answer("✅ Сохранено")
    await cmd_digest(callback)
