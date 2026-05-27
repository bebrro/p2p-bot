from aiogram import Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from api import binance_p2p, bybit_p2p
from config import FIATS, FIAT_FLAGS

router = Router()


class MakerStates(StatesGroup):
    waiting_price = State()


# ─── Клавиатуры ───────────────────────────────────────────────────────────────

def maker_exchange_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟡 Binance", callback_data="maker:exchange:binance"),
            InlineKeyboardButton(text="🟠 Bybit",   callback_data="maker:exchange:bybit"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")],
    ])


def maker_fiat_kb(exchange: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=FIAT_FLAGS.get(f, f),
            callback_data=f"maker:fiat:{exchange}:{f}",
        )]
        for f in FIATS
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="maker:start")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def maker_type_kb(exchange: str, fiat: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📗 Я продаю USDT",
                callback_data=f"maker:type:{exchange}:{fiat}:sell",
            ),
            InlineKeyboardButton(
                text="📕 Я покупаю USDT",
                callback_data=f"maker:type:{exchange}:{fiat}:buy",
            ),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"maker:exchange:{exchange}")],
    ])


def maker_result_kb(exchange: str, fiat: str, ad_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔄 Ввести другую цену",
            callback_data=f"maker:type:{exchange}:{fiat}:{ad_type}",
        )],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="maker:start")],
    ])


# ─── Хендлеры ─────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "maker:start")
async def maker_start(callback: CallbackQuery):
    await callback.message.edit_text(
        "🎯 <b>Мейкер-режим</b>\n\n"
        "Введи свою цену — покажу на каком месте окажется твоё объявление "
        "и сколько конкурентов выше/ниже.\n\n"
        "Выбери биржу:",
        reply_markup=maker_exchange_kb(),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("maker:exchange:"))
async def maker_choose_exchange(callback: CallbackQuery):
    exchange = callback.data.split(":")[2]
    await callback.message.edit_text(
        "🎯 Мейкер-режим\n\nВыбери валюту:",
        reply_markup=maker_fiat_kb(exchange),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("maker:fiat:"))
async def maker_choose_fiat(callback: CallbackQuery):
    _, _, exchange, fiat = callback.data.split(":")
    await callback.message.edit_text(
        f"🎯 Мейкер-режим | {'Binance' if exchange == 'binance' else 'Bybit'} | USDT/{fiat}\n\n"
        "Какое объявление хочешь выставить?",
        reply_markup=maker_type_kb(exchange, fiat),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("maker:type:"))
async def maker_choose_type(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    _, _, exchange, fiat, ad_type = parts

    await state.update_data(exchange=exchange, fiat=fiat, ad_type=ad_type)
    await state.set_state(MakerStates.waiting_price)

    type_label = "📗 Продажа USDT (ты продаёшь)" if ad_type == "sell" else "📕 Покупка USDT (ты покупаешь)"
    ex = "🟡 Binance" if exchange == "binance" else "🟠 Bybit"

    await callback.message.edit_text(
        f"🎯 Мейкер-режим | {ex} | USDT/{fiat}\n"
        f"Тип: {type_label}\n\n"
        f"Введи цену по которой хочешь выставить объявление ({fiat}):\n"
        f"Например: <b>487.50</b>",
        parse_mode="HTML",
    )


@router.message(MakerStates.waiting_price)
async def maker_analyze(message: Message, state: FSMContext):
    try:
        my_price = float(message.text.replace(" ", "").replace(",", "."))
    except ValueError:
        await message.answer("❌ Введи число, например: 487.50")
        return

    data = await state.get_data()
    await state.clear()

    exchange = data["exchange"]
    fiat     = data["fiat"]
    ad_type  = data["ad_type"]  # "sell" = я продаю, "buy" = я покупаю

    await message.answer("⏳ Анализирую рынок...")

    try:
        # Загружаем конкурентов — объявления того же типа
        if exchange == "binance":
            # sell maker = конкурируешь с другими продавцами = tradeType SELL
            bn_type = "SELL" if ad_type == "sell" else "BUY"
            competitors = await binance_p2p.get_ads(
                asset="USDT", fiat=fiat, trade_type=bn_type, rows=20
            )
        else:
            # sell maker = side "0" (мерчант продаёт пользователю)
            side = "0" if ad_type == "sell" else "1"
            competitors = await bybit_p2p.get_ads(
                asset="USDT", fiat=fiat, side=side, size=20
            )
    except Exception as e:
        await message.answer(f"❌ Ошибка загрузки: {e}")
        return

    if not competitors:
        await message.answer("😔 Не удалось загрузить конкурентов.")
        return

    text = _format_maker_result(my_price, competitors, exchange, fiat, ad_type)

    await message.answer(
        text,
        reply_markup=maker_result_kb(exchange, fiat, ad_type),
        parse_mode="HTML",
    )


def _format_maker_result(
    my_price: float,
    competitors: list,
    exchange: str,
    fiat: str,
    ad_type: str,
) -> str:
    ex = "🟡 Binance" if exchange == "binance" else "🟠 Bybit"
    type_label = "📗 Продажа USDT" if ad_type == "sell" else "📕 Покупка USDT"

    # Сортировка:
    # SELL maker: конкурируешь ценой вниз → лучший = наименьшая цена (ascending)
    # BUY  maker: конкурируешь ценой вверх → лучший = наибольшая цена (descending)
    if ad_type == "sell":
        sorted_c = sorted(competitors, key=lambda x: x["price"])
        # Моя позиция: меньше меня = выше в списке
        above = [c for c in sorted_c if c["price"] < my_price]
        below = [c for c in sorted_c if c["price"] > my_price]
        tip_price = round(sorted_c[0]["price"] - 0.01, 2) if sorted_c and sorted_c[0]["price"] > my_price else None
    else:
        sorted_c = sorted(competitors, key=lambda x: x["price"], reverse=True)
        # Моя позиция: больше меня = выше в списке
        above = [c for c in sorted_c if c["price"] > my_price]
        below = [c for c in sorted_c if c["price"] < my_price]
        tip_price = round(sorted_c[0]["price"] + 0.01, 2) if sorted_c and sorted_c[0]["price"] < my_price else None

    my_position = len(above) + 1
    total = len(competitors)

    lines = [
        f"🎯 <b>Мейкер-режим</b> | {ex} | USDT/{fiat}",
        f"Тип: {type_label}",
        "",
        f"💰 Твоя цена: <b>{my_price:,.2f} {fiat}</b>",
        f"📍 Позиция: <b>#{my_position}</b> из {total}",
        "",
        "<code>",
    ]

    # Показываем до 3 конкурентов выше
    if above:
        lines.append(f"▲ Выше (лучше): {len(above)} конк.")
        for i, c in enumerate(above[-3:]):
            pos = len(above) - len(above[-3:]) + i + 1
            completion = c["completion"]
            if isinstance(completion, float) and completion <= 1:
                completion = round(completion * 100, 1)
            lines.append(f"  #{pos:<2} {c['price']:>9,.2f}  {c['nickname'][:12]:<12}  {completion}%")
    else:
        lines.append("  🥇 Ты на первом месте!")

    # Твоя цена
    lines.append(f"──────────────────────────")
    lines.append(f"  ★  {my_price:>9,.2f}  ← ТЫ")
    lines.append(f"──────────────────────────")

    # Показываем до 3 конкурентов ниже
    if below:
        lines.append(f"▼ Ниже (хуже): {len(below)} конк.")
        for i, c in enumerate(below[:3]):
            pos = my_position + i + 1
            completion = c["completion"]
            if isinstance(completion, float) and completion <= 1:
                completion = round(completion * 100, 1)
            lines.append(f"  #{pos:<2} {c['price']:>9,.2f}  {c['nickname'][:12]:<12}  {completion}%")

    lines.append("</code>")

    # Совет
    if my_position == 1:
        lines.append("\n✅ Ты <b>#1</b> — отличная позиция!")
    elif tip_price:
        if ad_type == "sell":
            lines.append(f"\n💡 Чтобы стать <b>#1</b> → выставь <b>{tip_price:,.2f}</b> (на 0.01 ниже лидера)")
        else:
            lines.append(f"\n💡 Чтобы стать <b>#1</b> → выставь <b>{tip_price:,.2f}</b> (на 0.01 выше лидера)")

    return "\n".join(lines)
