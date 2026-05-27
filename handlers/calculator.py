from aiogram import Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

router = Router()

class CalcStates(StatesGroup):
    buy_price  = State()
    sell_price = State()
    amount     = State()
    fee        = State()

def _float(text: str) -> float:
    return float(text.replace(" ", "").replace(",", "."))

def calc_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Новый расчёт", callback_data="calc:start")],
        [InlineKeyboardButton(text="⬅️ Назад",        callback_data="back:main")],
    ])

@router.callback_query(lambda c: c.data == "calc:start")
async def calc_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(CalcStates.buy_price)
    await callback.message.edit_text(
        "🧮 <b>Калькулятор прибыли</b>\n\n"
        "Шаг 1/4 — Введи цену покупки (сколько заплатил за 1 USDT):\n"
        "<i>Например: 483.00</i>",
        parse_mode="HTML",
    )

@router.message(CalcStates.buy_price)
async def calc_sell(message: Message, state: FSMContext):
    try:
        await state.update_data(buy=_float(message.text))
    except ValueError:
        await message.answer("❌ Введи число"); return
    await state.set_state(CalcStates.sell_price)
    await message.answer("Шаг 2/4 — Цена продажи (за 1 USDT):\n<i>Например: 490.00</i>", parse_mode="HTML")

@router.message(CalcStates.sell_price)
async def calc_amount(message: Message, state: FSMContext):
    try:
        await state.update_data(sell=_float(message.text))
    except ValueError:
        await message.answer("❌ Введи число"); return
    await state.set_state(CalcStates.amount)
    await message.answer("Шаг 3/4 — Сколько USDT торговал:\n<i>Например: 100</i>", parse_mode="HTML")

@router.message(CalcStates.amount)
async def calc_fee(message: Message, state: FSMContext):
    try:
        await state.update_data(amount=_float(message.text))
    except ValueError:
        await message.answer("❌ Введи число"); return
    await state.set_state(CalcStates.fee)
    await message.answer(
        "Шаг 4/4 — Комиссия биржи % (0 если нет):\n"
        "<i>Binance P2P = 0, Bybit P2P = 0. Введи 0 если бесплатно</i>",
        parse_mode="HTML",
    )

@router.message(CalcStates.fee)
async def calc_result(message: Message, state: FSMContext):
    try:
        fee_pct = _float(message.text)
    except ValueError:
        await message.answer("❌ Введи число"); return

    data   = await state.get_data()
    await state.clear()

    buy    = data["buy"]
    sell   = data["sell"]
    amount = data["amount"]

    spent      = buy * amount
    received   = sell * amount
    fee_cost   = received * (fee_pct / 100)
    net        = received - fee_cost - spent
    net_pct    = (net / spent) * 100 if spent else 0
    per_usdt   = sell - buy

    emoji = "✅" if net > 0 else "❌"

    text = (
        f"🧮 <b>Результат сделки</b>\n\n"
        f"<code>"
        f"Куплено:      {amount:.2f} USDT\n"
        f"Цена покупки: {buy:,.2f}\n"
        f"Цена продажи: {sell:,.2f}\n"
        f"Прибыль/USDT: {per_usdt:+,.2f}\n"
        f"──────────────────────\n"
        f"Потрачено:    {spent:,.2f}\n"
        f"Получено:     {received:,.2f}\n"
        f"Комиссия:     {fee_cost:,.2f} ({fee_pct}%)\n"
        f"──────────────────────\n"
        f"Чистая пр.:   {net:+,.2f}\n"
        f"Доходность:   {net_pct:+.2f}%\n"
        f"</code>\n"
        f"{emoji} {'Прибыль' if net > 0 else 'Убыток'}: <b>{abs(net):,.2f}</b>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=calc_kb())
