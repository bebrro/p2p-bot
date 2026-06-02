"""
Инструкции / помощь — пошаговые гайды по «сложным» вещам.

Главный барьер активации — API-ключи (страшно, легко дать лишние права).
Команда /help + кнопка «❓ Инструкции». Тексты с упором на БЕЗОПАСНОСТЬ.
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)

router = Router()


def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 API-ключ Bybit (безопасно)", callback_data="help:bybit")],
        [InlineKeyboardButton(text="🔵 API-ключ OKX",               callback_data="help:okx")],
        [InlineKeyboardButton(text="💎 Подключить TG Wallet",        callback_data="help:wallet")],
        [InlineKeyboardButton(text="🔺 Связки без карт — что это",   callback_data="help:link")],
        [InlineKeyboardButton(text="🛡 Как не попасть на скам",      callback_data="help:scam")],
        [InlineKeyboardButton(text="🔒 Безопасность ключей",         callback_data="help:security")],
    ])


_MENU = (
    "❓ <b>Инструкции</b>\n\n"
    "Короткие пошаговые гайды по самому важному. Начни с того, что нужно сейчас 👇\n\n"
    "<i>Если что-то непонятно — напиши в поддержку (Кабинет → Поддержка).</i>"
)


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Все инструкции", callback_data="help:menu")],
    ])


_GUIDES = {
    "bybit": (
        "🔑 <b>API-ключ Bybit — за 2 минуты и безопасно</b>\n\n"
        "Зачем: чтобы бот сам держал твои объявления в топе (авто-репрайсер) "
        "и считал P&amp;L. Боту НЕ нужен доступ к деньгам.\n\n"
        "<b>Шаг 1. Создай ключ</b>\n"
        "bybit.com → профиль (вверху справа) → <b>API</b> → "
        "<b>Create New Key</b> → <b>System-generated API Keys</b>.\n"
        "Имя — любое, например <code>p2p-bot</code>.\n\n"
        "<b>Шаг 2. Права — это главное ⚠️</b>\n"
        "✅ Включи: <b>P2P</b> (или Spot Trading) — управление объявлениями.\n"
        "❌ <b>Withdraw (вывод) — НИКОГДА не включай.</b>\n"
        "Боту вывод не нужен и не будет нужен. Без него деньги в безопасности "
        "даже если ключ утечёт.\n\n"
        "<b>Шаг 3 (опц.). IP-привязка</b>\n"
        "Можешь ограничить ключ по IP — ещё безопаснее.\n\n"
        "<b>Шаг 4. Добавь в бота</b>\n"
        "Кабинет → <b>API аккаунты</b> → Добавить → вставь <b>API Key</b>, "
        "затем <b>API Secret</b>.\n"
        "🔒 Сообщения с ключами бот удалит из чата сразу, ключи хранятся "
        "зашифрованными.\n\n"
        "❗️ <b>Правило:</b> только ТОРГОВЛЯ/P2P, ВЫВОД — никогда."
    ),
    "okx": (
        "🔵 <b>API-ключ OKX</b>\n\n"
        "<b>Шаг 1.</b> okx.com → профиль → <b>API</b> → <b>Create API Key</b>.\n"
        "<b>Шаг 2. Права:</b> ✅ <b>Trade</b> · ❌ <b>Withdraw</b> (вывод) — "
        "выключен ВСЕГДА.\n"
        "<b>Шаг 3.</b> OKX попросит придумать <b>Passphrase</b> — запомни её, "
        "бот спросит её третьим шагом.\n"
        "<b>Шаг 4.</b> Кабинет → API аккаунты → Добавить (OKX): API Key → "
        "API Secret → Passphrase.\n\n"
        "🔒 Ключи шифруются, сообщения удаляются. Вывод — никогда не включай."
    ),
    "wallet": (
        "💎 <b>TG Wallet</b>\n\n"
        "Курсы TG Wallet бот тянет автоматически — отдельный ключ для просмотра "
        "<b>не нужен</b>.\n\n"
        "API-ключ Wallet нужен только если ты сам мерчант и хочешь автоматизацию "
        "своих объявлений: открой @wallet → P2P → Настройки → API.\n"
        "Права — только торговля, вывод выключен."
    ),
    "link": (
        "🔺 <b>Связка без карт (белый треугольник)</b>\n\n"
        "Это когда ты как посредник зарабатываешь на разнице, НЕ имея карты:\n"
        "1️⃣ Находишь продавца USDT дёшево и покупателя дорого.\n"
        "2️⃣ Покупатель платит продавцу <b>напрямую</b> (третьим лицом).\n"
        "3️⃣ USDT идёт тебе, тебе остаётся разница.\n\n"
        "Бот показывает связку только если ОБЕ стороны:\n"
        "✅ принимают платёж от 3-х лиц\n"
        "✅ общий банк\n"
        "✅ лимиты пересекаются\n"
        "✅ не скам и реальная цена\n\n"
        "Поэтому связки редкие, но рабочие. Чистая прибыль уже за вычетом "
        "комиссий. Тапни по ноге — откроется объявление на бирже."
    ),
    "scam": (
        "🛡 <b>Как не попасть на скам</b>\n\n"
        "<b>Главное правило:</b> отпускай USDT ТОЛЬКО когда деньги реально "
        "пришли на счёт. Скриншот/СМС/«чек» — НЕ доказательство.\n\n"
        "🔴 Торопят «быстрее отпускай»\n"
        "🔴 «Деньги в пути / зависли» — почти всегда обман\n"
        "🔴 Платёж от 3-го лица, которого ты не разрешал\n"
        "🔴 Просят написать фразу в комментарии платежа\n"
        "🔴 Сумма в чеке не сходится до копейки\n\n"
        "<b>В боте:</b> Инструменты → 🛡 Антискам:\n"
        "• кинь скрин чека → AI проверит на подделку\n"
        "• вставь ссылку на профиль Bybit → проверка по базе 11 500+ кидал\n\n"
        "Объявления кидал бот сам метит 🚨 и топит вниз стакана."
    ),
    "security": (
        "🔒 <b>Безопасность твоих ключей</b>\n\n"
        "• Ключи шифруются (Fernet) — в БД лежат нечитаемыми, "
        "расшифровка только в момент API-вызова.\n"
        "• Сообщения с ключами удаляются из чата сразу.\n"
        "• Ключи никогда не попадают в логи.\n"
        "• По возможности привяжи ключ к IP сервера.\n\n"
        "❗️ <b>Золотое правило:</b> у API-ключа НЕ должно быть права на ВЫВОД "
        "средств. Боту нужно только управлять объявлениями. Без вывода — "
        "максимум что может случиться при утечке: переставят цену объявления, "
        "но деньги не тронут."
    ),
}


async def _show_menu(target):
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(_MENU, reply_markup=_menu_kb(), parse_mode="HTML")
        await target.answer()
    else:
        await target.answer(_MENU, reply_markup=_menu_kb(), parse_mode="HTML")


@router.message(Command("help"))
async def help_cmd(message: Message):
    await _show_menu(message)


@router.callback_query(F.data == "help:menu")
async def help_menu(callback: CallbackQuery):
    await _show_menu(callback)


@router.callback_query(F.data.startswith("help:"))
async def help_guide(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    text = _GUIDES.get(key)
    if not text:
        await callback.answer()
        return
    await callback.message.edit_text(text, reply_markup=_back_kb(),
                                     parse_mode="HTML", disable_web_page_preview=True)
    await callback.answer()
