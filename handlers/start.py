"""
Главное меню + онбординг новых пользователей.

Логика:
• Новый пользователь (нет записи в DB) → автоматически 3 дня Pro + онбординг
• Старый пользователь → сразу главное меню
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from aiogram import Router, Bot
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import db
from keyboards import (
    main_menu, alerts_submenu, auto_submenu,
    analytics_submenu, account_submenu,
)
from utils.subscription import get_plan_key, PLANS, format_expires
from config import ADMIN_IDS

logger = logging.getLogger(__name__)
router = Router()

TRIAL_DAYS = 3


# ─── Владелец: автоматический пожизненный Team ────────────────────────────────

async def ensure_owner_access(uid: int) -> None:
    """
    Если пользователь — владелец (uid в ADMIN_IDS), гарантируем ему
    пожизненный Team (expires_at=None). Вызывается при /start и /menu —
    владелец никогда ничего не платит и всегда имеет полный доступ.
    """
    if uid not in ADMIN_IDS or not db.ok():
        return
    sub = await db.subscription_get(uid)
    # Уже пожизненный Team — ничего не делаем
    if sub and sub.get("plan") == "team" and sub.get("expires_at") is None:
        return
    await db.subscription_set(uid, "team", None)   # None = бессрочно
    logger.info(f"Owner access granted (lifetime Team): uid={uid}")


# ─── Текст главного меню с баннером статуса подписки ───────────────────────────

async def menu_text(uid: int) -> str:
    """
    Собирает текст меню + баннер плана сверху.
    Free → промо-баннер «открой Pro». Платный/триал → план + остаток дней.
    """
    sub      = await db.subscription_get(uid) if db.ok() else None
    plan_key = get_plan_key(sub)

    if plan_key == "free":
        banner = (
            "🆓 <b>План: Free</b>\n"
            "💡 Открой <b>Pro</b> — репрайсер, 4-биржевой арбитраж,\n"
            "AI-советник и увеличенные лимиты.\n\n"
        )
    else:
        plan    = PLANS[plan_key]
        expires = format_expires(sub)
        tail    = f" · {expires}" if expires else ""
        banner  = f"{plan['emoji']} <b>План: {plan['name']}</b>{tail}\n\n"

    return banner + MAIN_TEXT

# ─── Тексты ────────────────────────────────────────────────────────────────────

MAIN_TEXT = (
    "👋 <b>P2P Panel Bot</b>\n\n"
    "Умный помощник P2P мерчанта.\n"
    "Мониторит <b>4 биржи</b> в реальном времени,\n"
    "находит арбитраж и автоматически переставляет цены.\n\n"
    "Выбери раздел:"
)

_WELCOME_NEW = (
    "🎉 <b>Добро пожаловать в P2P Panel Bot!</b>\n\n"
    "🎁 Специально для тебя:\n"
    "⭐ <b>Pro на {days} дня — бесплатно и сразу!</b>\n\n"
    "<b>Что умеет бот:</b>\n"
    "📊 Курсы P2P на 4 биржах в реальном времени\n"
    "🔍 Smart Арбитраж — ловит разницу между биржами\n"
    "🔔 Алерты — уведомит когда спред станет выгодным\n"
    "🔄 Авто-репрайсер — держит твою цену лучше конкурентов\n"
    "🐋 Whale Tracker — следи за крупными игроками\n"
    "🧠 Pattern Engine — лучшие часы и дни для торговли\n"
    "🤖 AI Советник — анализ рынка от Gemini\n\n"
    "Пройди быстрый гайд чтобы настроить бота за 2 минуты 👇"
)

_GUIDE_STEPS = [
    # (заголовок, текст, emoji_step)
    (
        "📊 Шаг 1 из 3 — Смотри курсы P2P",
        (
            "Бот показывает <b>живые объявления</b> с 4 бирж:\n"
            "🟡 Binance · 🟠 Bybit · 🔵 OKX · 💎 TG Wallet\n\n"
            "👉 Нажми любую биржу в меню → выбери фиат → ассет\n\n"
            "💡 <b>Лайфхак:</b> раздел <b>🔍 Арбитраж</b> сам найдёт\n"
            "лучшую разницу между всеми биржами одним нажатием."
        ),
    ),
    (
        "🔔 Шаг 2 из 3 — Настрой алерт на спред",
        (
            "Алерт разбудит тебя когда спред станет выгодным.\n\n"
            "Например: <i>«уведоми когда USDT/KZT на Binance\n"
            "выгоднее обычного на 0.5%»</i>\n\n"
            "👉 Нажми <b>🔔 Алерты</b> в меню → Добавить алерт\n\n"
            "💡 Можно поставить на разные биржи и валюты —\n"
            "всё придёт в один чат."
        ),
    ),
    (
        "🔄 Шаг 3 из 3 — Авто-репрайсер",
        (
            "Бот автоматически держит твою цену <b>лучше конкурентов</b>.\n\n"
            "Работает с <b>Bybit и OKX</b> через API ключ.\n"
            "Нужны только торговые права — без права вывода.\n\n"
            "👉 <b>🔑 Аккаунты</b> → добавь API ключ\n"
            "👉 <b>🔄 Авто-цена</b> → настрой правило\n\n"
            "Пока нет API — просто пользуйся курсами и алертами."
        ),
    ),
]


# ─── Keyboards ─────────────────────────────────────────────────────────────────

def _new_user_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Быстрый гайд (2 мин)",  callback_data="ob:step:0")],
        [InlineKeyboardButton(text="❓ Инструкции (API-ключи и др.)", callback_data="help:menu")],
        [InlineKeyboardButton(text="⏭ Пропустить → в меню",   callback_data="ob:skip")],
    ])


def _guide_kb(step: int) -> InlineKeyboardMarkup:
    total = len(_GUIDE_STEPS)
    btns  = []

    if step < total - 1:
        btns.append([InlineKeyboardButton(
            text=f"Далее →  ({step + 2}/{total})",
            callback_data=f"ob:step:{step + 1}",
        )])
    else:
        btns.append([InlineKeyboardButton(
            text="✅ Готово — открыть меню",
            callback_data="ob:done",
        )])

    if step > 0:
        btns.append([InlineKeyboardButton(
            text="← Назад",
            callback_data=f"ob:step:{step - 1}",
        )])
    btns.append([InlineKeyboardButton(
        text="⏭ Пропустить",
        callback_data="ob:skip",
    )])
    return InlineKeyboardMarkup(inline_keyboard=btns)


# ─── Helpers ───────────────────────────────────────────────────────────────────

async def _is_new_user(uid: int) -> bool:
    """True если у пользователя нет записи в subscriptions (никогда не заходил)."""
    if not db.ok():
        return False   # нет DB — не трогаем
    sub = await db.subscription_get(uid)
    return sub is None


async def _give_trial(uid: int) -> datetime:
    """Выдаёт пробный Pro и возвращает дату окончания."""
    expires_at = datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)
    await db.subscription_set(uid, "pro", expires_at)
    logger.info(f"Trial activated: uid={uid} days={TRIAL_DAYS}")
    return expires_at


# ─── /start ────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, bot: Bot):
    uid      = message.from_user.id
    username = message.from_user.username or ""

    # Регистрируем пользователя (idempotent — ON CONFLICT DO NOTHING)
    is_new = await db.user_register(uid, username)

    # Обрабатываем реферальный параметр: /start ref_12345
    ref_arg = command.args or ""
    if ref_arg.startswith("ref_") and is_new:
        try:
            referrer_id = int(ref_arg[4:])
            # Импорт здесь во избежание циклического импорта
            from handlers.referral import process_referral
            asyncio.create_task(process_referral(referrer_id, uid, bot))
        except (ValueError, Exception) as e:
            logger.warning(f"Bad ref arg '{ref_arg}': {e}")

    # Глубокая ссылка на инструкции: /start help (из мини-аппа)
    if (command.args or "") == "help":
        from handlers.help_guide import _MENU, _menu_kb
        await message.answer(_MENU, reply_markup=_menu_kb(), parse_mode="HTML")
        return

    # Владелец — сразу пожизненный Team, без триала
    if uid in ADMIN_IDS:
        await ensure_owner_access(uid)
        await message.answer(
            await menu_text(uid),
            reply_markup=main_menu(),
            parse_mode="HTML",
        )
        return

    if is_new or await _is_new_user(uid):
        await _give_trial(uid)
        await message.answer(
            _WELCOME_NEW.format(days=TRIAL_DAYS),
            reply_markup=_new_user_kb(),
            parse_mode="HTML",
        )
    else:
        # Старый пользователь — сразу меню
        await message.answer(
            await menu_text(uid),
            reply_markup=main_menu(),
            parse_mode="HTML",
        )


@router.callback_query(lambda c: c.data == "back:main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text(
        await menu_text(callback.from_user.id),
        reply_markup=main_menu(), parse_mode="HTML",
    )
    await callback.answer()


# ─── Подменю категорий ─────────────────────────────────────────────────────────

_SUBMENU_TEXTS = {
    "menu:alerts":    ("🔔 <b>Алерты и сигналы</b>\n\nВыбери инструмент:", alerts_submenu),
    "menu:auto":      ("🤖 <b>Авто-режимы</b>\n\nВыбери инструмент:",      auto_submenu),
    "menu:analytics": ("📊 <b>Аналитика</b>\n\nВыбери инструмент:",         analytics_submenu),
    "menu:account":   ("👤 <b>Личный кабинет</b>\n\nВыбери раздел:",        account_submenu),
}


@router.callback_query(lambda c: c.data and c.data.startswith("menu:"))
async def open_submenu(callback: CallbackQuery):
    key = callback.data
    if key not in _SUBMENU_TEXTS:
        await callback.answer()
        return
    text, kb_fn = _SUBMENU_TEXTS[key]
    await callback.message.edit_text(text, reply_markup=kb_fn(), parse_mode="HTML")
    await callback.answer()


# ─── Онбординг: шаги ───────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("ob:step:"))
async def ob_step(callback: CallbackQuery):
    step  = int(callback.data.split(":")[2])
    step  = max(0, min(step, len(_GUIDE_STEPS) - 1))
    title, body = _GUIDE_STEPS[step]
    text  = f"<b>{title}</b>\n\n{body}"
    await callback.message.edit_text(
        text, reply_markup=_guide_kb(step), parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data in ("ob:done", "ob:skip"))
async def ob_finish(callback: CallbackQuery):
    await callback.message.edit_text(
        await menu_text(callback.from_user.id),
        reply_markup=main_menu(), parse_mode="HTML",
    )
    await callback.answer()


# ─── /menu — главное меню для вернувшихся пользователей ───────────────────────

@router.message(Command("menu"))
async def cmd_menu(message: Message):
    """Показывает главное меню без онбординга — для всех повторных заходов."""
    uid = message.from_user.id
    await ensure_owner_access(uid)   # владельцу — гарантированный полный доступ
    await message.answer(
        await menu_text(uid),
        reply_markup=main_menu(),
        parse_mode="HTML",
    )


@router.message(Command("myid"))
async def cmd_myid(message: Message):
    """Показывает Telegram ID — нужен чтобы вписать себя в ADMIN_IDS."""
    uid       = message.from_user.id
    is_owner  = uid in ADMIN_IDS
    status    = "✅ ты владелец (полный доступ)" if is_owner else "обычный пользователь"
    await message.answer(
        f"🆔 <b>Твой Telegram ID:</b>\n<code>{uid}</code>\n\n"
        f"Статус: {status}\n\n"
        + ("" if is_owner else
           "Чтобы получить полный доступ владельца:\n"
           "1. Скопируй ID выше\n"
           "2. Railway → Variables → <code>ADMIN_IDS</code> = твой ID\n"
           "3. После редеплоя отправь /start"),
        parse_mode="HTML",
    )


# ─── Команды-флоу ───────────────────────────────────────────────────────────
# Шорткаты /p2p /calc /whale /ai убраны: их заменяет мини-апп (вкладки) и
# кнопка меню «📊 P2P Sniper». Меньше дублей — чище меню.

@router.message(Command("alerts"))
async def cmd_alerts(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Алерты", callback_data="alert:list")]
    ])
    await message.answer("🔔 <b>Алерты на спред</b>", reply_markup=kb, parse_mode="HTML")


@router.message(Command("stopwhale"))
async def cmd_stopwhale(message: Message):
    """Аварийная остановка кит-трекера."""
    from handlers.whale_tracker import _user_settings, _initialized, _active_pairs
    uid = message.from_user.id
    cfg = _user_settings.get(uid)
    if cfg:
        cfg["enabled"] = False
        for pair in _active_pairs(uid):
            _initialized.discard(pair)
        await message.answer(
            "🛑 <b>Whale Tracker остановлен.</b>\n\n"
            "Уведомления о китах выключены.\n\n"
            "⚠️ <b>Если уведомления всё ещё приходят</b> — значит на твоём компьютере "
            "запущен бот локально (python bot.py в CMD окне). Закрой то окно.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "✅ Whale Tracker уже выключен (настроек нет).\n\n"
            "⚠️ <b>Если уведомления всё ещё приходят</b> — на твоём компьютере запущен "
            "бот локально. Найди CMD/терминал с python bot.py и закрой его.",
            parse_mode="HTML",
        )
