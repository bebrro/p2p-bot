from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from keyboards import main_menu

router = Router()

MAIN_TEXT = "👋 <b>P2P Panel Bot</b>\n\nМониторинг объявлений Binance и Bybit.\n\nВыбери раздел:"


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(MAIN_TEXT, reply_markup=main_menu(), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "back:main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text(MAIN_TEXT, reply_markup=main_menu(), parse_mode="HTML")


# ── Шорткаты команд из ☰ меню ──────────────────────────────────────────────

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
