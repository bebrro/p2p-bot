from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from api import binance_p2p, bybit_p2p
from keyboards import main_menu

router = Router()

# Хранилище алертов: {user_id: [{"fiat", "asset", "exchange", "threshold", "direction"}]}
_alerts: dict[int, list] = {}


class AlertStates(StatesGroup):
    waiting_threshold = State()


@router.callback_query(lambda c: c.data == "alerts:list")
async def show_alerts(callback: CallbackQuery):
    user_id = callback.from_user.id
    alerts = _alerts.get(user_id, [])

    if not alerts:
        text = "🔔 У тебя нет активных алертов.\n\nДобавить алерт на спред?"
    else:
        lines = ["🔔 Твои алерты:\n"]
        for i, a in enumerate(alerts):
            direction = "выше" if a["direction"] == "above" else "ниже"
            lines.append(
                f"{i+1}. {a['exchange'].title()} {a['asset']}/{a['fiat']} — спред {direction} {a['threshold']}%"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить алерт", callback_data="alert:add:KZT:USDT:binance")],
        [InlineKeyboardButton(text="🗑 Удалить все", callback_data="alert:clear")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("alert:add:"))
async def alert_add_start(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    fiat = parts[2]
    asset = parts[3] if len(parts) > 3 else "USDT"
    exchange = parts[4] if len(parts) > 4 else "binance"

    await state.update_data(fiat=fiat, asset=asset, exchange=exchange)
    await state.set_state(AlertStates.waiting_threshold)
    await callback.message.edit_text(
        f"🔔 Алерт на спред {asset}/{fiat} ({exchange.title()})\n\n"
        "Введи минимальный % спреда для уведомления.\n"
        "Например: <b>0.5</b> — уведомлю когда спред превысит 0.5%",
        parse_mode="HTML",
    )


@router.message(AlertStates.waiting_threshold)
async def alert_set_threshold(message: Message, state: FSMContext):
    try:
        threshold = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Введи число, например: 0.5")
        return

    data = await state.get_data()
    user_id = message.from_user.id

    if user_id not in _alerts:
        _alerts[user_id] = []

    _alerts[user_id].append({
        "fiat": data["fiat"],
        "asset": data["asset"],
        "exchange": data["exchange"],
        "threshold": threshold,
        "direction": "above",
    })

    await state.clear()
    await message.answer(
        f"✅ Алерт добавлен!\n"
        f"Уведомлю когда спред {data['asset']}/{data['fiat']} превысит {threshold}%",
        reply_markup=main_menu(),
    )


@router.callback_query(lambda c: c.data == "alert:clear")
async def alert_clear(callback: CallbackQuery):
    _alerts.pop(callback.from_user.id, None)
    await callback.answer("✅ Все алерты удалены")
    await show_alerts(callback)


def get_all_alerts() -> dict[int, list]:
    return _alerts
