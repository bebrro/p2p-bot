from aiogram import Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from api import binance_p2p, bybit_p2p
from config import FIATS, FIAT_FLAGS

router = Router()

# {user_id: [{exchange, fiat, asset, ad_type, my_price, last_position}]}
_monitors: dict[int, list[dict]] = {}
MAX_MONITORS = 3


class MonStates(StatesGroup):
    waiting_price = State()


def get_all_monitors() -> dict:
    return _monitors


def _mon_kb(user_id: int) -> InlineKeyboardMarkup:
    mons = _monitors.get(user_id, [])
    buttons = [[InlineKeyboardButton(
        text=f"{'🟡' if m['exchange']=='binance' else '🟠'} {m['asset']}/{m['fiat']} "
             f"{'📗' if m['ad_type']=='sell' else '📕'} {m['my_price']:,.2f} → #{m.get('last_position','?')}",
        callback_data=f"mon:del:{i}",
    )] for i, m in enumerate(mons)]
    buttons.append([InlineKeyboardButton(text="➕ Добавить",  callback_data="mon:add:start")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад",    callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _ex_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟡 Binance", callback_data="mon:add:ex:binance"),
         InlineKeyboardButton(text="🟠 Bybit",   callback_data="mon:add:ex:bybit")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="mon:list")],
    ])


def _fiat_kb(exchange: str) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(
        text=FIAT_FLAGS.get(f, f), callback_data=f"mon:add:fiat:{exchange}:{f}"
    )] for f in FIATS]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="mon:add:start")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _type_kb(exchange: str, fiat: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📗 Я продаю USDT", callback_data=f"mon:add:type:{exchange}:{fiat}:sell"),
         InlineKeyboardButton(text="📕 Я покупаю USDT", callback_data=f"mon:add:type:{exchange}:{fiat}:buy")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mon:add:ex:{exchange}")],
    ])


@router.callback_query(lambda c: c.data == "mon:list")
async def mon_list(callback: CallbackQuery):
    mons = _monitors.get(callback.from_user.id, [])
    text = (
        f"📡 <b>Мониторинг позиции</b>\n\n"
        f"Бот следит за твоей позицией в стакане и уведомляет когда тебя обходят.\n"
        f"Активно: {len(mons)}/{MAX_MONITORS}\n\n"
        f"{'Нажми на монитор чтобы удалить.' if mons else 'Добавь первый монитор.'}"
    )
    await callback.message.edit_text(text, reply_markup=_mon_kb(callback.from_user.id), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "mon:add:start")
async def mon_add_start(callback: CallbackQuery):
    if len(_monitors.get(callback.from_user.id, [])) >= MAX_MONITORS:
        await callback.answer(f"❌ Максимум {MAX_MONITORS}", show_alert=True); return
    await callback.message.edit_text("📡 Выбери биржу:", reply_markup=_ex_kb())


@router.callback_query(lambda c: c.data and c.data.startswith("mon:add:ex:"))
async def mon_add_ex(callback: CallbackQuery):
    exchange = callback.data.split(":")[3]
    await callback.message.edit_text("📡 Выбери валюту:", reply_markup=_fiat_kb(exchange))


@router.callback_query(lambda c: c.data and c.data.startswith("mon:add:fiat:"))
async def mon_add_fiat(callback: CallbackQuery):
    _, _, _, exchange, fiat = callback.data.split(":")
    await callback.message.edit_text("📡 Тип объявления:", reply_markup=_type_kb(exchange, fiat))


@router.callback_query(lambda c: c.data and c.data.startswith("mon:add:type:"))
async def mon_add_type(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    exchange, fiat, ad_type = parts[3], parts[4], parts[5]
    await state.update_data(exchange=exchange, fiat=fiat, ad_type=ad_type)
    await state.set_state(MonStates.waiting_price)
    type_label = "продажа (ты продаёшь)" if ad_type == "sell" else "покупка (ты покупаешь)"
    await callback.message.edit_text(
        f"📡 Мониторинг | {exchange.title()} | USDT/{fiat} | {type_label}\n\n"
        f"Введи цену по которой выставил своё объявление:",
    )


@router.message(MonStates.waiting_price)
async def mon_save(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", ".").replace(" ", ""))
    except ValueError:
        await message.answer("❌ Введи число"); return

    data = await state.get_data()
    await state.clear()

    uid = message.from_user.id
    if uid not in _monitors:
        _monitors[uid] = []

    entry = {**data, "my_price": price, "asset": "USDT", "last_position": None}
    _monitors[uid].append(entry)

    await message.answer(
        f"✅ Монитор добавлен!\n\n"
        f"Цена: {price:,.2f}\n"
        f"Буду уведомлять если тебя обойдут по позиции.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📡 Мои мониторы", callback_data="mon:list")]
        ]),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("mon:del:"))
async def mon_delete(callback: CallbackQuery):
    idx = int(callback.data.split(":")[2])
    lst = _monitors.get(callback.from_user.id, [])
    if 0 <= idx < len(lst):
        lst.pop(idx)
        await callback.answer("✅ Удалён")
    await mon_list(callback)


async def check_positions(bot) -> None:
    import logging
    logger = logging.getLogger(__name__)

    for user_id, monitors in list(_monitors.items()):
        for mon in monitors:
            try:
                exchange = mon["exchange"]
                fiat     = mon["fiat"]
                asset    = mon["asset"]
                ad_type  = mon["ad_type"]
                my_price = mon["my_price"]

                if exchange == "binance":
                    bn_type = "SELL" if ad_type == "sell" else "BUY"
                    ads = await binance_p2p.get_ads(asset=asset, fiat=fiat, trade_type=bn_type, rows=30)
                else:
                    side = "0" if ad_type == "sell" else "1"
                    ads = await bybit_p2p.get_ads(asset=asset, fiat=fiat, side=side, size=30)

                if not ads:
                    continue

                # Считаем позицию
                if ad_type == "sell":
                    sorted_ads = sorted(ads, key=lambda x: x["price"])
                    above = [a for a in sorted_ads if a["price"] < my_price]
                else:
                    sorted_ads = sorted(ads, key=lambda x: x["price"], reverse=True)
                    above = [a for a in sorted_ads if a["price"] > my_price]

                position = len(above) + 1
                last_pos = mon.get("last_position")
                mon["last_position"] = position

                if last_pos is not None and position > last_pos:
                    ex = "🟡 Binance" if exchange == "binance" else "🟠 Bybit"
                    type_label = "продажа" if ad_type == "sell" else "покупка"
                    beater = above[-1] if above else None
                    beater_line = f"\nОбошёл: <b>{beater['nickname']}</b> → {beater['price']:,.2f}" if beater else ""

                    await bot.send_message(
                        user_id,
                        f"📡 <b>Тебя обошли!</b>\n"
                        f"{ex} | {asset}/{fiat} | {type_label}\n"
                        f"Твоя цена: {my_price:,.2f}\n"
                        f"Позиция: #{last_pos} → <b>#{position}</b>{beater_line}",
                        parse_mode="HTML",
                    )
            except Exception as e:
                logger.error(f"Position monitor error: {e}")
