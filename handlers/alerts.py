from aiogram import Router
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from keyboards import main_menu
from config import PAYMENT_METHODS_BINANCE, PAYMENT_METHODS_BYBIT, PAYMENT_LABELS
from utils.limits import check_allowed, upsell_text, upsell_kb
import db

router = Router()

# Fallback когда DATABASE_URL не задан (локальная разработка)
_alerts: dict[int, list] = {}


class AlertStates(StatesGroup):
    waiting_threshold = State()


# ── Клавиатура выбора банка ───────────────────────────────────────────────────

def _bank_kb(fiat: str, asset: str, exchange: str) -> InlineKeyboardMarkup:
    methods = PAYMENT_METHODS_BINANCE if exchange == "binance" else PAYMENT_METHODS_BYBIT
    banks   = methods.get(fiat, [])
    prefix  = f"alertpay:{fiat}:{asset}:{exchange}"

    rows = [[InlineKeyboardButton(text="🏦 Любой банк", callback_data=f"{prefix}:any")]]
    pair: list = []
    for b in banks:
        label = PAYMENT_LABELS.get(b, b)
        pair.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}:{b}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="alerts:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Показать список алертов ───────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "alerts:list")
async def show_alerts(callback: CallbackQuery):
    user_id = callback.from_user.id
    if db.ok():
        alerts_list = await db.alerts_get(user_id)
    else:
        alerts_list = _alerts.get(user_id, [])

    if not alerts_list:
        text = "🔔 У тебя нет активных алертов.\n\nДобавить алерт на спред?"
    else:
        lines = ["🔔 Твои алерты:\n"]
        for i, a in enumerate(alerts_list):
            direction = "выше" if a["direction"] == "above" else "ниже"
            pay_raw   = a.get("pay", "")
            pay_str   = f" · {PAYMENT_LABELS.get(pay_raw, pay_raw)}" if pay_raw else ""
            lines.append(
                f"{i+1}. {a['exchange'].title()} {a['asset']}/{a['fiat']} "
                f"— спред {direction} {a['threshold']}%{pay_str}"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить алерт", callback_data="alert:add:KZT:USDT:binance")],
        [InlineKeyboardButton(text="🗑 Удалить все",    callback_data="alert:clear")],
        [InlineKeyboardButton(text="⬅️ Назад",          callback_data="back:main")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)


# ── Шаг 1: выбор банка ────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("alert:add:"))
async def alert_add_start(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id

    # Лимит по плану — апселл при упоре
    current = len(await db.alerts_get(user_id)) if db.ok() else len(_alerts.get(user_id, []))
    allowed, limit, plan_key = await check_allowed(user_id, "alerts", current)
    if not allowed:
        await callback.message.edit_text(
            upsell_text("alerts", current, limit, plan_key),
            reply_markup=upsell_kb(manage_cb="alerts:list", manage_text="🗑 Удалить алерт"),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    parts    = callback.data.split(":")
    fiat     = parts[2]
    asset    = parts[3] if len(parts) > 3 else "USDT"
    exchange = parts[4] if len(parts) > 4 else "binance"

    await state.update_data(fiat=fiat, asset=asset, exchange=exchange)
    await callback.message.edit_text(
        f"🔔 Алерт {asset}/{fiat} ({exchange.title()})\n\n"
        "💳 Выбери банк / метод оплаты:",
        reply_markup=_bank_kb(fiat, asset, exchange),
    )


# ── Шаг 2: выбран банк — просим порог ─────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("alertpay:"))
async def alert_pick_bank(callback: CallbackQuery, state: FSMContext):
    # alertpay:{fiat}:{asset}:{exchange}:{pay}
    parts    = callback.data.split(":")
    fiat     = parts[1]
    asset    = parts[2]
    exchange = parts[3]
    pay      = parts[4] if len(parts) > 4 else "any"

    pay_label = "Любой банк" if pay == "any" else PAYMENT_LABELS.get(pay, pay)
    await state.update_data(fiat=fiat, asset=asset, exchange=exchange, pay=pay)
    await state.set_state(AlertStates.waiting_threshold)
    await callback.message.edit_text(
        f"🔔 Алерт на спред {asset}/{fiat} ({exchange.title()})\n"
        f"💳 Банк: {pay_label}\n\n"
        "Введи минимальный % спреда для уведомления.\n"
        "Например: <b>0.5</b> — уведомлю когда спред превысит 0.5%",
        parse_mode="HTML",
    )


# ── Шаг 3: ввод порога ────────────────────────────────────────────────────────

@router.message(AlertStates.waiting_threshold)
async def alert_set_threshold(message: Message, state: FSMContext):
    try:
        threshold = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Введи число, например: 0.5")
        return

    data     = await state.get_data()
    user_id  = message.from_user.id
    exchange = data["exchange"]
    fiat     = data["fiat"]
    asset    = data["asset"]
    pay      = data.get("pay", "any")
    pay_val  = "" if pay == "any" else pay

    if db.ok():
        await db.alerts_add(user_id, exchange, fiat, asset, threshold, pay=pay_val)
    else:
        _alerts.setdefault(user_id, []).append({
            "fiat": fiat, "asset": asset, "exchange": exchange,
            "threshold": threshold, "direction": "above", "pay": pay_val,
        })

    pay_label = (f" · {PAYMENT_LABELS.get(pay_val, pay_val)}" if pay_val else "")
    await state.clear()
    await message.answer(
        f"✅ Алерт добавлен!\n"
        f"Уведомлю когда спред {asset}/{fiat}{pay_label} превысит {threshold}%",
        reply_markup=main_menu(),
    )


# ── Удалить все алерты ────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "alert:clear")
async def alert_clear(callback: CallbackQuery):
    uid = callback.from_user.id
    if db.ok():
        await db.alerts_clear(uid)
    else:
        _alerts.pop(uid, None)
    await callback.answer("✅ Все алерты удалены")
    await show_alerts(callback)


# ── Получить все алерты (для bot.py) ──────────────────────────────────────────

async def get_all_alerts() -> dict[int, list]:
    """Для фонового опроса в bot.py."""
    if db.ok():
        return await db.alerts_get_all()
    return dict(_alerts)
