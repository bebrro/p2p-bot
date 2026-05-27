from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from keyboards import main_menu

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 P2P Panel Bot\n\n"
        "Мониторинг объявлений Binance и Bybit без авторизации.\n\n"
        "Выбери биржу:",
        reply_markup=main_menu(),
    )


@router.callback_query(lambda c: c.data == "back:main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text(
        "👋 P2P Panel Bot\n\nВыбери биржу:",
        reply_markup=main_menu(),
    )
