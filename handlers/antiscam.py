"""
Антискам для P2P — защита от фейк-чеков и кидал.

«Белая» фича: не рисуем подделки, а ЛОВИМ их и защищаем трейдера.
  • 🧾 AI-анализ чека — пришли скрин «оплаты», бот ищет признаки подделки
  • 👤 Проверка никнейма — есть ли контрагент в антискам-базе (краудсорс)
  • 🚨 Сообщить о кидале — пополнить общую базу
  • 📋 Памятка безопасной сделки

Команда: /antiscam
"""
import base64
import logging

from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import db

logger = logging.getLogger(__name__)
router = Router()


class AntiScamStates(StatesGroup):
    waiting_receipt = State()
    waiting_nick    = State()


# ─── Промпт для AI-анализа чека ────────────────────────────────────────────────

_RECEIPT_PROMPT = (
    "Ты эксперт по безопасности P2P-сделок с криптой. На изображении — "
    "«чек об оплате», который прислал контрагент. P2P-мошенники присылают "
    "ПОДДЕЛЬНЫЕ чеки, чтобы продавец отпустил USDT не получив реальных денег.\n\n"
    "Проанализируй скриншот на признаки подделки и ответь на русском СТРОГО так:\n\n"
    "🔎 <b>Признаки редактирования:</b> [шрифты, выравнивание, артефакты сжатия, "
    "несовпадение стиля — или 'явных нет']\n"
    "📋 <b>Нестыковки:</b> [время/дата/сумма/баланс/реквизиты/статус — или 'не вижу']\n"
    "🏦 <b>Похоже на реальный интерфейс банка/приложения:</b> [да/нет/трудно сказать]\n"
    "⚖️ <b>Вердикт:</b> 🟢 похоже на настоящий | 🟡 подозрительно | 🔴 признаки подделки\n\n"
    "Будь конкретен, но не выдумывай. В конце ОБЯЗАТЕЛЬНО добавь дословно:\n"
    "«⚠️ Скриншот — НЕ доказательство. Отпускай USDT только когда деньги "
    "реально пришли на твой счёт (проверь в своём банке/приложении).»"
)


# ─── UI ────────────────────────────────────────────────────────────────────────

def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧾 Проверить чек (AI)", callback_data="as:receipt")],
        [InlineKeyboardButton(text="👤 Проверить никнейм",   callback_data="as:nick")],
        [InlineKeyboardButton(text="📋 Памятка безопасности", callback_data="as:memo")],
        [InlineKeyboardButton(text="⬅️ Назад",               callback_data="back:main")],
    ])


_MENU_TEXT = (
    "🛡 <b>Антискам P2P</b>\n\n"
    "Защита от фейк-чеков и кидал.\n\n"
    "🧾 <b>Проверить чек</b> — пришли скрин «оплаты», AI найдёт признаки подделки\n"
    "👤 <b>Проверить никнейм</b> — есть ли контрагент в базе кидал\n"
    "📋 <b>Памятка</b> — правила безопасной сделки\n\n"
    "Выбери действие:"
)


@router.message(Command("antiscam"))
@router.callback_query(F.data == "antiscam:start")
async def antiscam_start(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(_MENU_TEXT, reply_markup=_menu_kb(), parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(_MENU_TEXT, reply_markup=_menu_kb(), parse_mode="HTML")


# ─── 🧾 AI-анализ чека ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "as:receipt")
async def as_receipt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AntiScamStates.waiting_receipt)
    await callback.message.edit_text(
        "🧾 <b>Проверка чека</b>\n\n"
        "Пришли <b>скриншот «чека об оплате»</b>, который прислал контрагент.\n\n"
        "AI проверит его на признаки подделки.\n\n"
        "<i>Совет: шли картинкой (фото), не файлом.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="antiscam:start")],
        ]),
    )
    await callback.answer()


@router.message(AntiScamStates.waiting_receipt, F.photo)
async def as_receipt_photo(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    wait = await message.answer("🔎 AI анализирует чек... (~10-20 сек)")

    try:
        from api import gemini
        photo = message.photo[-1]                       # самое большое фото
        tg_file = await bot.get_file(photo.file_id)
        buf = await bot.download_file(tg_file.file_path)
        img_b64 = base64.b64encode(buf.read()).decode()

        result = await gemini.vision(img_b64, _RECEIPT_PROMPT, mime="image/jpeg")
    except Exception as e:
        logger.warning(f"receipt analyze error: {e}")
        result = f"❌ Не удалось проанализировать: {e}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧾 Проверить ещё чек", callback_data="as:receipt")],
        [InlineKeyboardButton(text="⬅️ Антискам",          callback_data="antiscam:start")],
    ])
    header = "🧾 <b>Анализ чека</b>\n" + "─" * 22 + "\n\n"
    try:
        await wait.edit_text(header + result, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await wait.edit_text(header + result, reply_markup=kb)


@router.message(AntiScamStates.waiting_receipt)
async def as_receipt_not_photo(message: Message):
    await message.answer("📸 Пришли именно <b>фото</b> чека (картинкой).", parse_mode="HTML")


# ─── 👤 Проверка никнейма ──────────────────────────────────────────────────────

@router.callback_query(F.data == "as:nick")
async def as_nick(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AntiScamStates.waiting_nick)
    await callback.message.edit_text(
        "👤 <b>Проверка никнейма</b>\n\n"
        "Введи <b>ник контрагента</b> (как в объявлении) — проверю по базе кидал.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="antiscam:start")],
        ]),
    )
    await callback.answer()


async def _scam_votes(nick: str) -> int:
    """Сколько раз ник отмечен как кидала в краудсорс-базе."""
    if not db.ok():
        return 0
    nick_l = nick.strip().lower()
    for n, cnt in await db.community_get_all():
        if n.strip().lower() == nick_l:
            return int(cnt)
    return 0


@router.message(AntiScamStates.waiting_nick)
async def as_nick_check(message: Message, state: FSMContext):
    await state.clear()
    nick = (message.text or "").strip()
    if len(nick) < 2:
        await message.answer("❌ Слишком короткий ник. Попробуй снова: /antiscam")
        return

    votes = await _scam_votes(nick)
    if votes >= 3:
        verdict = f"🔴 <b>ОПАСНО</b> — отмечен как кидала <b>{votes}</b> раз"
    elif votes >= 1:
        verdict = f"🟡 <b>Осторожно</b> — есть <b>{votes}</b> жалоб(ы)"
    else:
        verdict = "🟢 Жалоб в базе нет (но это не гарантия — будь начеку)"

    await message.answer(
        f"👤 <b>{nick}</b>\n\n{verdict}\n\n"
        "Если тебя пытались кинуть этим ником — добавь его в базу, "
        "защитишь других 👇",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚨 Это кидала — в базу", callback_data=f"as:report:{nick[:48]}")],
            [InlineKeyboardButton(text="⬅️ Антискам",            callback_data="antiscam:start")],
        ]),
    )


@router.callback_query(F.data.startswith("as:report:"))
async def as_report(callback: CallbackQuery):
    nick = callback.data[len("as:report:"):]
    if not nick:
        await callback.answer()
        return
    total = await db.community_vote(callback.from_user.id, nick) if db.ok() else 0
    await callback.answer("🚨 Добавлено в базу. Спасибо!", show_alert=True)
    try:
        await callback.message.edit_text(
            f"🚨 <b>{nick}</b> добавлен в базу кидал.\n"
            f"Всего жалоб: <b>{total}</b>\n\n"
            "Спасибо — ты защитил других трейдеров 🛡",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Антискам", callback_data="antiscam:start")],
            ]),
        )
    except Exception:
        pass


# ─── 📋 Памятка ────────────────────────────────────────────────────────────────

_MEMO = (
    "📋 <b>Памятка: как не потерять деньги</b>\n\n"
    "<b>Главное правило:</b>\n"
    "💰 Отпускай USDT ТОЛЬКО когда деньги реально пришли на счёт.\n"
    "Скриншот, СМС, «чек» — НЕ доказательство.\n\n"
    "<b>Признаки развода:</b>\n"
    "🔴 Торопят: «быстрее отпускай, я спешу»\n"
    "🔴 Прислали красивый «чек», но в банке денег нет\n"
    "🔴 «Деньги в пути / зависли в банке» — почти всегда обман\n"
    "🔴 Просят отпустить «частями» / «на доверии»\n"
    "🔴 Сумма в чеке не совпадает до копейки\n"
    "🔴 Платёж «от третьего лица», а ты это не разрешал\n\n"
    "<b>Что делать:</b>\n"
    "✅ Зайди в своё банк-приложение и проверь баланс сам\n"
    "✅ Сверь сумму до копейки и имя отправителя\n"
    "✅ Не ведись на спешку — мошенник всегда торопит\n"
    "✅ Проверь ник контрагента в антискам-базе\n"
    "✅ Сомневаешься — открой спор/позови поддержку биржи"
)


@router.callback_query(F.data == "as:memo")
async def as_memo(callback: CallbackQuery):
    await callback.message.edit_text(
        _MEMO, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧾 Проверить чек", callback_data="as:receipt")],
            [InlineKeyboardButton(text="⬅️ Антискам",      callback_data="antiscam:start")],
        ]),
    )
    await callback.answer()
