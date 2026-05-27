from aiogram import Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

router = Router()


class CalcStates(StatesGroup):
    buy_price  = State()   # шаг 1: цена покупки за 1 USDT
    sell_price = State()   # шаг 2: цена продажи за 1 USDT
    spent_sum  = State()   # шаг 3: сумма покупки (фиат)
    fee        = State()   # шаг 4: комиссия %


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
        "Шаг 1/4 — Цена покупки (сколько заплатил за 1 USDT):\n"
        "<i>Например: 460.00</i>",
        parse_mode="HTML",
    )


@router.message(CalcStates.buy_price)
async def calc_got_buy(message: Message, state: FSMContext):
    try:
        await state.update_data(buy=_float(message.text))
    except ValueError:
        await message.answer("❌ Введи число, например: <code>460</code>", parse_mode="HTML")
        return
    await state.set_state(CalcStates.sell_price)
    await message.answer(
        "Шаг 2/4 — Цена продажи (за 1 USDT):\n"
        "<i>Например: 550.00</i>",
        parse_mode="HTML",
    )


@router.message(CalcStates.sell_price)
async def calc_got_sell(message: Message, state: FSMContext):
    try:
        await state.update_data(sell=_float(message.text))
    except ValueError:
        await message.answer("❌ Введи число, например: <code>550</code>", parse_mode="HTML")
        return
    await state.set_state(CalcStates.spent_sum)
    await message.answer(
        "Шаг 3/4 — Сумма покупки (сколько фиата потратил):\n"
        "<i>Например: 100000</i>",
        parse_mode="HTML",
    )


@router.message(CalcStates.spent_sum)
async def calc_got_spent(message: Message, state: FSMContext):
    try:
        val = _float(message.text)
        if val <= 0:
            raise ValueError
        await state.update_data(spent=val)
    except ValueError:
        await message.answer("❌ Введи положительное число, например: <code>100000</code>", parse_mode="HTML")
        return
    await state.set_state(CalcStates.fee)
    await message.answer(
        "Шаг 4/4 — Комиссия биржи % (введи 0 если нет):\n"
        "<i>Binance P2P = 0, Bybit P2P = 0</i>",
        parse_mode="HTML",
    )


@router.message(CalcStates.fee)
async def calc_result(message: Message, state: FSMContext):
    try:
        fee_pct = _float(message.text)
    except ValueError:
        await message.answer("❌ Введи число, например: <code>0</code>", parse_mode="HTML")
        return

    data = await state.get_data()
    await state.clear()

    buy      = data["buy"]    # цена покупки за 1 USDT
    sell     = data["sell"]   # цена продажи за 1 USDT
    spent    = data["spent"]  # потрачено фиата

    # Сколько USDT купили на эту сумму
    usdt_bought = spent / buy

    # Сколько фиата получили за эти USDT
    received    = usdt_bought * sell

    # Комиссия
    fee_cost    = received * (fee_pct / 100)

    # Чистая прибыль
    net         = received - fee_cost - spent
    net_pct     = (net / spent) * 100 if spent else 0
    per_usdt    = sell - buy

    emoji = "✅" if net > 0 else "❌"

    text = (
        f"🧮 <b>Результат сделки</b>\n\n"
        f"<code>"
        f"Потрачено:    {spent:,.2f} (фиат)\n"
        f"Куплено:      {usdt_bought:.4f} USDT\n"
        f"──────────────────────\n"
        f"Цена покупки: {buy:,.2f}\n"
        f"Цена продажи: {sell:,.2f}\n"
        f"Прибыль/USDT: {per_usdt:+,.2f}\n"
        f"──────────────────────\n"
        f"Получено:     {received:,.2f}\n"
        f"Комиссия:     {fee_cost:,.2f} ({fee_pct}%)\n"
        f"──────────────────────\n"
        f"Чистая пр.:   {net:+,.2f}\n"
        f"Доходность:   {net_pct:+.2f}%\n"
        f"</code>\n"
        f"{emoji} {'Прибыль' if net > 0 else 'Убыток'}: <b>{abs(net):,.2f}</b>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=calc_kb())
