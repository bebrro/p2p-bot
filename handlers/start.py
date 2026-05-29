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
from utils.subscription import get_plan_key, PLANS

logger = logging.getLogger(__name__)
router = Router()

TRIAL_DAYS = 3

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

    if is_new or await _is_new_user(uid):
        await _give_trial(uid)
        await message.answer(
            _WELCOME_NEW.format(days=TRIAL_DAYS),
            reply_markup=_new_user_kb(),
            parse_mode="HTML",
        )
    else:
        # Старый пользователь — сразу меню
        sub      = await db.subscription_get(uid)
        plan_key = get_plan_key(sub)
        plan     = PLANS[plan_key]
        greeting = (
            f"{plan['emoji']} <b>План: {plan['name']}</b>\n\n"
            if plan_key != "free" else ""
        )
        await message.answer(
            greeting + MAIN_TEXT,
            reply_markup=main_menu(),
            parse_mode="HTML",
        )


@router.callback_query(lambda c: c.data == "back:main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text(
        MAIN_TEXT, reply_markup=main_menu(), parse_mode="HTML",
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
        MAIN_TEXT, reply_markup=main_menu(), parse_mode="HTML",
    )
    await callback.answer()


# ─── Шорткаты команд из ☰ меню ──────────────────────────────────────────────

@router.message(Command("p2p"))
async def cmd_p2p(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟡 Binance P2P", callback_data="ex:binance"),
         InlineKeyboardButton(text="🟠 Bybit P2P",   callback_data="ex:bybit")],
        [InlineKeyboardButton(text="⬅️ Меню",        callback_data="back:main")],
    ])
    await message.answer("📊 <b>P2P курсы</b>\n\nВыбери биржу:", reply_markup=kb, parse_mode="HTML")


@router.message(Command("calc"))
async def cmd_calc(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧮 Открыть калькулятор", callback_data="calc:start")]
    ])
    await message.answer("🧮 <b>Калькулятор прибыли</b>", reply_markup=kb, parse_mode="HTML")


@router.message(Command("whale"))
async def cmd_whale(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐋 Whale Tracker", callback_data="wt:list")]
    ])
    await message.answer("🐋 <b>Whale Tracker</b>", reply_markup=kb, parse_mode="HTML")


@router.message(Command("ai"))
async def cmd_ai(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 AI Советник", callback_data="ai:start")]
    ])
    await message.answer("🤖 <b>AI Советник</b>", reply_markup=kb, parse_mode="HTML")


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
