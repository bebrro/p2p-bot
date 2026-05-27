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


FIAT_FLAGS = {
    "KZT": "🇰🇿",
    "RUB": "🇷🇺",
    "TRY": "🇹🇷",
    "USD": "🇺🇸",
}


def _float(text: str) -> float:
    return float(text.replace(" ", "").replace(",", "."))


def _currency_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇰🇿 KZT", callback_data="calc:cur:KZT"),
            InlineKeyboardButton(text="🇷🇺 RUB", callback_data="calc:cur:RUB"),
        ],
        [
            InlineKeyboardButton(text="🇹🇷 TRY", callback_data="calc:cur:TRY"),
            InlineKeyboardButton(text="🇺🇸 USD", callback_data="calc:cur:USD"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")],
    ])


def _result_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Новый расчёт", callback_data="calc:start")],
        [InlineKeyboardButton(text="⬅️ Назад",        callback_data="back:main")],
    ])


# ── Шаг 0: выбор валюты (кнопками) ───────────────────────────────────────────

@router.callback_query(lambda c: c.data == "calc:start")
async def calc_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🧮 <b>Калькулятор прибыли</b>\n\n"
        "Выбери валюту, в которой торговал:",
        reply_markup=_currency_kb(),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("calc:cur:"))
async def calc_got_currency(callback: CallbackQuery, state: FSMContext):
    cur = callback.data.split(":")[2]   # KZT / RUB / TRY / USD
    flag = FIAT_FLAGS.get(cur, "")
    await state.update_data(cur=cur)
    await state.set_state(CalcStates.buy_price)
    await callback.message.edit_text(
        f"🧮 <b>Калькулятор прибыли</b>  {flag} {cur}\n\n"
        f"Шаг 1/4 — Цена покупки (сколько {cur} за 1 USDT):\n"
        f"<i>Например: 460</i>",
        parse_mode="HTML",
    )


# ── Шаг 1: цена покупки ───────────────────────────────────────────────────────

@router.message(CalcStates.buy_price)
async def calc_got_buy(message: Message, state: FSMContext):
    try:
        val = _float(message.text)
        if val <= 0:
            raise ValueError
        await state.update_data(buy=val)
    except ValueError:
        await message.answer("❌ Введи положительное число, например: <code>460</code>", parse_mode="HTML")
        return
    data = await state.get_data()
    cur  = data.get("cur", "")
    flag = FIAT_FLAGS.get(cur, "")
    await state.set_state(CalcStates.sell_price)
    await message.answer(
        f"Шаг 2/4 — Цена продажи (сколько {flag}{cur} за 1 USDT):\n"
        f"<i>Например: 550</i>",
        parse_mode="HTML",
    )


# ── Шаг 2: цена продажи ───────────────────────────────────────────────────────

@router.message(CalcStates.sell_price)
async def calc_got_sell(message: Message, state: FSMContext):
    try:
        val = _float(message.text)
        if val <= 0:
            raise ValueError
        await state.update_data(sell=val)
    except ValueError:
        await message.answer("❌ Введи положительное число, например: <code>550</code>", parse_mode="HTML")
        return
    data = await state.get_data()
    cur  = data.get("cur", "")
    flag = FIAT_FLAGS.get(cur, "")
    await state.set_state(CalcStates.spent_sum)
    await message.answer(
        f"Шаг 3/4 — Сумма покупки (сколько {flag}{cur} потратил):\n"
        f"<i>Например: 100000</i>",
        parse_mode="HTML",
    )


# ── Шаг 3: сумма покупки ──────────────────────────────────────────────────────

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


# ── Шаг 4: комиссия → результат ───────────────────────────────────────────────

@router.message(CalcStates.fee)
async def calc_result(message: Message, state: FSMContext):
    try:
        fee_pct = _float(message.text)
    except ValueError:
        await message.answer("❌ Введи число, например: <code>0</code>", parse_mode="HTML")
        return

    data = await state.get_data()
    await state.clear()

    cur   = data.get("cur", "")
    flag  = FIAT_FLAGS.get(cur, "")
    buy   = data["buy"]
    sell  = data["sell"]
    spent = data["spent"]

    # Расчёт
    usdt_bought = spent / buy
    received    = usdt_bought * sell
    fee_cost    = received * (fee_pct / 100)
    net         = received - fee_cost - spent
    net_pct     = (net / spent) * 100 if spent else 0
    per_usdt    = sell - buy
    emoji       = "✅" if net > 0 else "❌"
    label       = f"{flag}{cur}"

    text = (
        f"🧮 <b>Результат сделки</b>  {label}\n\n"
        f"<code>"
        f"Потрачено:    {spent:>14,.2f} {cur}\n"
        f"Куплено:      {usdt_bought:>11.4f} USDT\n"
        f"──────────────────────────────\n"
        f"Цена покупки: {buy:>14,.2f} {cur}\n"
        f"Цена продажи: {sell:>14,.2f} {cur}\n"
        f"Прибыль/USDT: {per_usdt:>+14,.2f} {cur}\n"
        f"──────────────────────────────\n"
        f"Получено:     {received:>14,.2f} {cur}\n"
        f"Комиссия:     {fee_cost:>14,.2f} {cur} ({fee_pct}%)\n"
        f"──────────────────────────────\n"
        f"Чистая пр.:   {net:>+14,.2f} {cur}\n"
        f"Доходность:   {net_pct:>+13.2f}%\n"
        f"</code>\n"
        f"{emoji} {'Прибыль' if net > 0 else 'Убыток'}: "
        f"<b>{abs(net):,.2f} {cur}</b>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=_result_kb())
