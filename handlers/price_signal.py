"""
Сигнал оптимальной цены для P2P.
Учитывает биржу, фиат, сторону, банк и сумму.
Советует куда выставить объявление чтобы занять нужную позицию в стакане.
"""
import logging
from aiogram import Router
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import FIATS, FIAT_FLAGS, PAYMENT_METHODS_BINANCE, PAYMENT_METHODS_BYBIT, PAYMENT_LABELS
from api import binance_p2p, bybit_p2p
from utils.spread import calc_spread

router = Router()
logger = logging.getLogger(__name__)

# Минимальный шаг цены для каждой валюты
FIAT_STEP = {
    "KZT": 0.50,
    "RUB": 0.05,
    "TRY": 0.05,
    "USD": 0.001,
}


class SigStates(StatesGroup):
    waiting_amount = State()


# ── Клавиатуры ────────────────────────────────────────────────────────────────

def _ex_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟡 Binance", callback_data="sig:ex:binance"),
            InlineKeyboardButton(text="🟠 Bybit",   callback_data="sig:ex:bybit"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")],
    ])


def _fiat_kb(exchange: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=FIAT_FLAGS.get(f, f),
            callback_data=f"sig:fiat:{exchange}:{f}",
        )]
        for f in FIATS
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="sig:start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _side_kb(exchange: str, fiat: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📗 Я покупаю (BUY)",
                callback_data=f"sig:side:{exchange}:{fiat}:buy",
            ),
            InlineKeyboardButton(
                text="📕 Я продаю (SELL)",
                callback_data=f"sig:side:{exchange}:{fiat}:sell",
            ),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"sig:ex:{exchange}")],
    ])


def _bank_kb(exchange: str, fiat: str, side: str) -> InlineKeyboardMarkup:
    methods = PAYMENT_METHODS_BINANCE if exchange == "binance" else PAYMENT_METHODS_BYBIT
    banks   = methods.get(fiat, [])
    prefix  = f"sig:bank:{exchange}:{fiat}:{side}"

    rows = [[InlineKeyboardButton(text="🏦 Любой банк", callback_data=f"{prefix}:any")]]
    pair: list = []
    for b in banks:
        pair.append(InlineKeyboardButton(
            text=PAYMENT_LABELS.get(b, b),
            callback_data=f"{prefix}:{b}",
        ))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    rows.append([InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"sig:side:{exchange}:{fiat}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _result_kb(exchange: str, fiat: str, side: str, bank: str, amount: int) -> InlineKeyboardMarkup:
    base = f"sig:show:{exchange}:{fiat}:{side}:{bank}:{amount}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Обновить",      callback_data=base),
            InlineKeyboardButton(
                text="💰 Изменить сумму",
                callback_data=f"sig:setamt:{exchange}:{fiat}:{side}:{bank}",
            ),
        ],
        [InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"sig:side:{exchange}:{fiat}",
        )],
    ])


# ── Вычисление сигнала ────────────────────────────────────────────────────────

def _norm_comp(c) -> int:
    if isinstance(c, float) and 0 < c <= 1:
        return int(round(c * 100))
    return int(round(float(c or 0)))


def _recs(ads: list, side: str, fiat: str) -> list[tuple]:
    """
    Возвращает [(позиция, рекомендуемая_цена)] для мест 1, 2, 3.

    BUY — стакан отсортирован DESC (выше = лучше):
      #1 → чуть выше текущего лучшего
      #2 → чуть ниже лучшего (между #1 и #2)
      #3 → чуть ниже текущего #2

    SELL — стакан отсортирован ASC (ниже = лучше):
      #1 → чуть ниже текущего лучшего
      #2 → чуть выше лучшего (между #1 и #2)
      #3 → чуть выше текущего #2
    """
    step = FIAT_STEP.get(fiat, 0.10)
    dp   = 3 if fiat == "USD" else 2
    result = []
    for pos in range(3):
        if side == "buy":
            price = (ads[0]["price"] + step) if pos == 0 else (ads[pos - 1]["price"] - step if pos < len(ads) else ads[-1]["price"] - step)
        else:
            price = (ads[0]["price"] - step) if pos == 0 else (ads[pos - 1]["price"] + step if pos < len(ads) else ads[-1]["price"] + step)
        result.append((pos + 1, round(price, dp)))
    return result


async def _build_signal(
    exchange: str, fiat: str, side: str, bank: str, amount: float = 0
) -> tuple[str, str]:
    """Возвращает (текст_сигнала, текст_ошибки)."""
    pay_types = [bank] if bank and bank != "any" else None
    asset = "USDT"

    try:
        if exchange == "binance":
            tr  = "BUY"  if side == "buy" else "SELL"
            otr = "SELL" if side == "buy" else "BUY"
            ads     = await binance_p2p.get_ads(asset=asset, fiat=fiat, trade_type=tr,  pay_types=pay_types, rows=10)
            opp_ads = await binance_p2p.get_ads(asset=asset, fiat=fiat, trade_type=otr, rows=1)
        else:
            bs  = "1" if side == "buy" else "0"
            obs = "0" if side == "buy" else "1"
            ads     = await bybit_p2p.get_ads(asset=asset, fiat=fiat, side=bs,  pay_types=pay_types, size=10)
            opp_ads = await bybit_p2p.get_ads(asset=asset, fiat=fiat, side=obs, size=1)
    except Exception as e:
        logger.error(f"price_signal fetch error: {e}")
        return "", f"❌ Ошибка загрузки данных: {e}"

    if not ads:
        return "", "❌ Нет объявлений по выбранным фильтрам"

    # Фильтр по сумме
    if amount > 0:
        ads = [a for a in ads if a["min_amount"] <= amount <= a["max_amount"]]
        if not ads:
            return "", f"❌ Нет объявлений для суммы {amount:,.0f} {fiat}\nПопробуй без фильтра суммы"

    ex_name  = "🟡 Binance" if exchange == "binance" else "🟠 Bybit"
    side_str = "📗 BUY"     if side == "buy"         else "📕 SELL"
    bank_str = PAYMENT_LABELS.get(bank, bank) if bank and bank != "any" else "Все банки"
    amt_str  = f"  ·  {amount:,.0f} {fiat}" if amount > 0 else ""

    lines = [
        f"📍 <b>Сигнал оптимальной цены</b>",
        f"{ex_name}  ·  {asset}/{fiat}  ·  {side_str}  ·  {bank_str}{amt_str}\n",
        f"📊 <b>Текущий стакан (топ {min(5, len(ads))}):</b>",
    ]

    for i, ad in enumerate(ads[:5]):
        comp   = _norm_comp(ad["completion"])
        marker = "▶" if i == 0 else str(i + 1)
        lines.append(
            f"  {marker}. <b>{ad['price']:,.2f}</b> — {ad['nickname']} "
            f"({ad['orders']} сд · {comp}%)"
        )

    # Рекомендации
    recs      = _recs(ads, side, fiat)
    opp_price = opp_ads[0]["price"] if opp_ads else None
    medals    = ["🥇", "🥈", "🥉"]
    labels    = ["агрессивно", "оптимально", "консервативно"]

    lines.append("\n💡 <b>Рекомендации:</b>")
    for (pos, price), medal, label in zip(recs, medals, labels):
        sp_str = ""
        if opp_price:
            try:
                sp = calc_spread(price, opp_price) if side == "buy" else calc_spread(opp_price, price)
                sp_str = f"  ·  спред {sp['spread_pct']}%"
            except Exception:
                pass
        suffix = " ✅" if label == "оптимально" else ""
        lines.append(f"  {medal} #{pos} → <b>{price:,.2f}</b>{sp_str}  <i>({label}{suffix})</i>")

    # Итоговый совет
    _, best_price = recs[1] if len(recs) >= 2 else recs[0]
    lines.append(
        f"\n✅ Выставляй <b>{best_price:,.2f}</b> — позиция #2, "
        "хороший баланс видимости и маржи"
    )

    return "\n".join(lines), ""


# ── Хендлеры ──────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "sig:start")
async def sig_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "📍 <b>Сигнал оптимальной цены</b>\n\n"
        "Выбери биржу:",
        reply_markup=_ex_kb(),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("sig:ex:"))
async def sig_pick_exchange(callback: CallbackQuery):
    exchange = callback.data.split(":")[2]
    await callback.message.edit_text(
        "📍 Сигнал оптимальной цены\n\nВыбери валюту:",
        reply_markup=_fiat_kb(exchange),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("sig:fiat:"))
async def sig_pick_fiat(callback: CallbackQuery):
    _, _, exchange, fiat = callback.data.split(":")
    await callback.message.edit_text(
        f"📍 Сигнал  ·  {exchange.title()}  ·  {fiat}\n\n"
        "Ты <b>покупаешь</b> крипту или <b>продаёшь</b>?",
        reply_markup=_side_kb(exchange, fiat),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("sig:side:"))
async def sig_pick_side(callback: CallbackQuery):
    parts = callback.data.split(":")
    exchange = parts[2]
    fiat     = parts[3]

    if len(parts) > 4:
        # Пришли с выбором стороны → показываем банки
        side = parts[4]
        side_str = "Купить" if side == "buy" else "Продать"
        await callback.message.edit_text(
            f"📍 Сигнал  ·  {exchange.title()}  ·  {fiat}  ·  {side_str}\n\n"
            "💳 Выбери банк / метод оплаты:",
            reply_markup=_bank_kb(exchange, fiat, side),
        )
    else:
        # Пришли через кнопку Назад → показываем выбор стороны
        await callback.message.edit_text(
            f"📍 Сигнал  ·  {exchange.title()}  ·  {fiat}\n\n"
            "Ты <b>покупаешь</b> крипту или <b>продаёшь</b>?",
            reply_markup=_side_kb(exchange, fiat),
            parse_mode="HTML",
        )


@router.callback_query(lambda c: c.data and c.data.startswith("sig:bank:"))
async def sig_pick_bank(callback: CallbackQuery, state: FSMContext):
    # sig:bank:{exchange}:{fiat}:{side}:{bank}
    parts    = callback.data.split(":")
    exchange = parts[2]
    fiat     = parts[3]
    side     = parts[4]
    bank     = parts[5]

    await state.update_data(exchange=exchange, fiat=fiat, side=side, bank=bank)
    await state.set_state(SigStates.waiting_amount)

    bank_str = PAYMENT_LABELS.get(bank, bank) if bank != "any" else "Любой банк"
    await callback.message.edit_text(
        f"📍 Сигнал  ·  {exchange.title()}  ·  {fiat}  ·  {bank_str}\n\n"
        "💰 Введи <b>сумму сделки</b> в фиате для точного фильтра\n"
        "Например: <code>50000</code>\n\n"
        "Или нажми <b>Пропустить</b> — покажу все объявления",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="⏩ Пропустить",
                callback_data=f"sig:show:{exchange}:{fiat}:{side}:{bank}:0",
            )],
            [InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"sig:side:{exchange}:{fiat}:{side}",
            )],
        ]),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("sig:show:"))
async def sig_show(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    # sig:show:{exchange}:{fiat}:{side}:{bank}:{amount}
    parts    = callback.data.split(":")
    exchange = parts[2]
    fiat     = parts[3]
    side     = parts[4]
    bank     = parts[5]
    amount   = float(parts[6]) if len(parts) > 6 else 0

    await callback.answer("⏳ Загружаю стакан...")
    text, err = await _build_signal(exchange, fiat, side, bank, amount)
    if err:
        await callback.message.edit_text(err)
        return

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_result_kb(exchange, fiat, side, bank, int(amount)),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("sig:setamt:"))
async def sig_set_amount_prompt(callback: CallbackQuery, state: FSMContext):
    # sig:setamt:{exchange}:{fiat}:{side}:{bank}
    parts    = callback.data.split(":")
    exchange = parts[2]
    fiat     = parts[3]
    side     = parts[4]
    bank     = parts[5]

    await state.update_data(exchange=exchange, fiat=fiat, side=side, bank=bank)
    await state.set_state(SigStates.waiting_amount)

    await callback.message.edit_text(
        "💰 Введи сумму сделки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="⏩ Без суммы",
                callback_data=f"sig:show:{exchange}:{fiat}:{side}:{bank}:0",
            )],
        ]),
    )


@router.message(SigStates.waiting_amount)
async def sig_got_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(" ", "").replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи положительное число, например: 50000")
        return

    data     = await state.get_data()
    exchange = data["exchange"]
    fiat     = data["fiat"]
    side     = data["side"]
    bank     = data["bank"]
    await state.clear()

    sent = await message.answer("⏳ Считаю оптимальную цену...")
    text, err = await _build_signal(exchange, fiat, side, bank, amount)

    if err:
        await sent.edit_text(err)
        return

    await sent.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_result_kb(exchange, fiat, side, bank, int(amount)),
    )
