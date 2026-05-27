"""
Авто-переценка объявлений Bybit.

Логика:
• Пользователь указывает ID объявления + целевую позицию + шаг + диапазон цен
• Каждые 60 секунд бот смотрит на стакан и корректирует цену так,
  чтобы объявление оставалось на нужной позиции
• Например: топ-3, шаг 0.5 → цена = цена_3го_конкурента - 0.5
• Ограничения: min_price ≤ цена ≤ max_price
"""
import asyncio
from datetime import datetime
from aiogram import Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from api import bybit_p2p, bybit_auth
from handlers.account_manager import get_account_credentials
from config import FIATS, FIAT_FLAGS

router = Router()

# {user_id: [{item_id, fiat, asset, side, target_pos, delta, min_price, max_price, current_price}]}
_repricers: dict[int, list] = {}
MAX_REPRICERS = 5

# Лог последних переценок {user_id: ["[10:30] USDT/KZT 519.5 → 518.8"]}
_log: dict[int, list] = {}
MAX_LOG = 30


class RepriceStates(StatesGroup):
    waiting_item_id = State()
    waiting_params  = State()


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _add_log(user_id: int, msg: str):
    if user_id not in _log:
        _log[user_id] = []
    _log[user_id].append(msg)
    if len(_log[user_id]) > MAX_LOG:
        _log[user_id].pop(0)


def _repricers_kb(user_id: int) -> InlineKeyboardMarkup:
    rps     = _repricers.get(user_id, [])
    buttons = []
    for i, r in enumerate(rps):
        side_em = "📗" if r["side"] == "1" else "📕"
        price   = r.get("current_price", "?")
        price_s = f"{price:,.2f}" if isinstance(price, float) else str(price)
        buttons.append([InlineKeyboardButton(
            text=f"{side_em} {r['asset']}/{r['fiat']} топ-{r['target_pos']} [{price_s}]",
            callback_data=f"rp:del:{i}",
        )])
    if len(rps) < MAX_REPRICERS:
        buttons.append([InlineKeyboardButton(text="➕ Добавить правило", callback_data="rp:add:start")])
    buttons.append([InlineKeyboardButton(text="📋 Лог переценок", callback_data="rp:log")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─── Handlers ──────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "rp:list")
async def rp_list(callback: CallbackQuery):
    uid = callback.from_user.id
    rps = _repricers.get(uid, [])
    await callback.message.edit_text(
        "🔄 <b>Авто-переценка</b>\n\n"
        "Бот автоматически корректирует цену твоих объявлений каждые 60 секунд.\n\n"
        f"Активных правил: {len(rps)}/{MAX_REPRICERS}\n"
        "Нажми на правило чтобы удалить.",
        reply_markup=_repricers_kb(uid),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "rp:add:start")
async def rp_add_start(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    api_key, _ = get_account_credentials(uid)
    if not api_key:
        await callback.message.edit_text(
            "❌ Сначала добавь API ключ Bybit.\n\n"
            "Нажми 🔑 Аккаунты → Добавить аккаунт",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Аккаунты", callback_data="acc:list")],
            ]),
        )
        return
    await state.set_state(RepriceStates.waiting_item_id)
    await callback.message.edit_text(
        "🔄 <b>Авто-переценка — шаг 1/2</b>\n\n"
        "Введи <b>ID объявления Bybit</b>\n\n"
        "Где найти ID:\n"
        "Bybit → P2P → Мои объявления → нажми на объявление\n"
        "ID в строке URL или под заголовком.",
        parse_mode="HTML",
    )


@router.message(RepriceStates.waiting_item_id)
async def rp_got_item_id(message: Message, state: FSMContext):
    await state.update_data(item_id=message.text.strip())
    await state.set_state(RepriceStates.waiting_params)
    await message.answer(
        "🔄 <b>Авто-переценка — шаг 2/2</b>\n\n"
        "Введи параметры одной строкой:\n"
        "<code>позиция шаг мин_цена макс_цена</code>\n\n"
        "<b>Примеры:</b>\n"
        "<code>1 0.5 510 530</code> — держаться #1, шаг 0.5, не ниже 510\n"
        "<code>3 1.0 500 600</code> — держаться в топ-3, шаг 1.0",
        parse_mode="HTML",
    )


@router.message(RepriceStates.waiting_params)
async def rp_got_params(message: Message, state: FSMContext):
    try:
        parts      = message.text.strip().split()
        target_pos = int(parts[0])
        delta      = float(parts[1].replace(",", "."))
        min_price  = float(parts[2].replace(",", "."))
        max_price  = float(parts[3].replace(",", "."))
        if target_pos < 1 or delta < 0 or min_price >= max_price:
            raise ValueError
    except Exception:
        await message.answer(
            "❌ Неверный формат. Пример:\n<code>3 0.5 500 600</code>",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    await state.clear()

    uid = message.from_user.id
    api_key, api_secret = get_account_credentials(uid)
    wait = await message.answer("⏳ Загружаю данные объявления...")

    try:
        my_ads = await bybit_auth.get_my_ads(api_key, api_secret, status=10)
        ad = next((a for a in my_ads if a["id"] == data["item_id"]), None)
        if not ad:
            await wait.edit_text(
                "❌ Объявление не найдено.\n\n"
                "Проверь ID. Объявление должно быть активным (online)."
            )
            return
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка API: <code>{e}</code>", parse_mode="HTML")
        return

    if uid not in _repricers:
        _repricers[uid] = []

    _repricers[uid].append({
        "item_id":       data["item_id"],
        "fiat":          ad["fiat"],
        "asset":         ad["asset"],
        "side":          ad["side"],
        "target_pos":    target_pos,
        "delta":         delta,
        "min_price":     min_price,
        "max_price":     max_price,
        "current_price": ad["price"],
    })

    side_label = "продажа" if ad["side"] == "1" else "покупка"
    await wait.edit_text(
        f"✅ <b>Правило добавлено!</b>\n\n"
        f"Объявление: {ad['asset']}/{ad['fiat']} ({side_label})\n"
        f"Цель: топ-{target_pos} | шаг: ±{delta}\n"
        f"Диапазон цены: {min_price:,.2f} — {max_price:,.2f}\n\n"
        f"Переценка запустится в течение 60 секунд.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Мои правила", callback_data="rp:list")]
        ]),
    )


@router.callback_query(lambda c: c.data == "rp:log")
async def rp_log(callback: CallbackQuery):
    uid  = callback.from_user.id
    logs = _log.get(uid, [])
    text = "📋 <b>Лог переценок</b>\n\n" + ("\n".join(logs[-20:]) if logs else "Пока пусто.")
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="rp:log")],
            [InlineKeyboardButton(text="⬅️ Назад",    callback_data="rp:list")],
        ]),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("rp:del:"))
async def rp_del(callback: CallbackQuery):
    idx = int(callback.data.split(":")[2])
    uid = callback.from_user.id
    rps = _repricers.get(uid, [])
    if 0 <= idx < len(rps):
        rps.pop(idx)
        await callback.answer("✅ Удалено")
    await rp_list(callback)


# ─── Background task ───────────────────────────────────────────────────────────

async def run_repricer(bot) -> None:
    """Вызывается каждые 60 секунд из bot.py"""
    import logging
    logger = logging.getLogger(__name__)

    for user_id, rules in list(_repricers.items()):
        for rule in rules:
            try:
                api_key, api_secret = get_account_credentials(user_id)
                if not api_key:
                    continue

                fiat  = rule["fiat"]
                asset = rule["asset"]
                side  = rule["side"]  # "1"=sell, "0"=buy

                # Получаем публичный стакан конкурентов
                ads = await bybit_p2p.get_ads(asset=asset, fiat=fiat, side=side, size=20)
                if not ads:
                    continue

                # Сортировка: sell — дешевле лучше (ascending), buy — дороже лучше (descending)
                if side == "1":
                    sorted_ads = sorted(ads, key=lambda x: x["price"])
                    def calc_target(ref): return round(ref - rule["delta"], 2)
                else:
                    sorted_ads = sorted(ads, key=lambda x: x["price"], reverse=True)
                    def calc_target(ref): return round(ref + rule["delta"], 2)

                target_pos = rule["target_pos"]
                if len(sorted_ads) >= target_pos:
                    ref_price    = sorted_ads[target_pos - 1]["price"]
                    target_price = calc_target(ref_price)
                elif sorted_ads:
                    target_price = sorted_ads[-1]["price"]
                else:
                    continue

                # Зажимаем в диапазон
                target_price = max(rule["min_price"], min(rule["max_price"], target_price))
                old_price    = rule.get("current_price", 0)

                # Не обновляем если разница < 0.01
                if abs(target_price - old_price) < 0.01:
                    continue

                # Обновляем через API
                res      = await bybit_auth.update_ad_price(api_key, api_secret, rule["item_id"], target_price)
                ret_code = res.get("retCode", -1)
                ts       = datetime.now().strftime("%H:%M:%S")

                if ret_code == 0:
                    rule["current_price"] = target_price
                    _add_log(user_id, f"[{ts}] {asset}/{fiat} {old_price:,.2f} → <b>{target_price:,.2f}</b>")
                    logger.info(f"Repriced uid={user_id} {asset}/{fiat} {old_price}→{target_price}")
                else:
                    err = res.get("retMsg", "?")
                    _add_log(user_id, f"[{ts}] ❌ {asset}/{fiat}: {err}")
                    logger.warning(f"Reprice failed uid={user_id}: {err}")

            except Exception as e:
                logger.error(f"Repricer error uid={user_id}: {e}")
