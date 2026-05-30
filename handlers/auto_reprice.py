"""
Авто-переценка объявлений — Bybit и OKX.

Логика:
• Пользователь указывает биржу, ID объявления, целевую позицию, шаг, диапазон
• Каждые 60 секунд бот смотрит на публичный стакан и корректирует цену
• Например: топ-1, шаг 0.5 → цена = цена_лидера ± 0.5
• Правила хранятся в PostgreSQL; при рестарте не теряются
"""
import logging
from datetime import datetime
from aiogram import Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from api import bybit_p2p, okx_p2p, bybit_auth, okx_auth
from handlers.account_manager import get_account_credentials, get_active_account, EXCHANGE_NAMES
from utils.limits import check_allowed, get_limit, upsell_text, upsell_kb
import db

logger = logging.getLogger(__name__)
router = Router()

# In-memory зеркало правил: {user_id: [{...}]}
_repricers: dict[int, list] = {}
MAX_REPRICERS = 5  # fallback-дефолт; реальный лимит берётся из плана (utils.limits)

# Лог переценок: {user_id: ["[10:30] USDT/KZT 519.5 → 518.8"]}
_log: dict[int, list] = {}
MAX_LOG = 30


class RepriceStates(StatesGroup):
    waiting_ad_id  = State()
    waiting_params = State()


# ─── DB-sync helpers ──────────────────────────────────────────────────────────

def add_rule_memory(uid: int, rule: dict) -> None:
    """Добавляет правило в память (для вызова из server.py)."""
    _repricers.setdefault(uid, []).append(rule)


def remove_rule_memory(uid: int, db_id: int) -> None:
    """Удаляет правило из памяти."""
    if uid in _repricers:
        _repricers[uid] = [r for r in _repricers[uid] if r.get("db_id") != db_id]


async def load_from_db() -> None:
    """Загружает все правила из БД в _repricers. Вызывать из bot.py при старте."""
    rows = await db.repricer_get_all_enabled()
    for r in rows:
        uid = r["user_id"]
        if uid not in _repricers:
            _repricers[uid] = []
        rule = {
            "db_id":      r["id"],
            "exchange":   r["exchange"],
            "item_id":    r["ad_id"],
            "fiat":       r["fiat"],
            "asset":      r["asset"],
            "side":       r["side"],
            "target_pos": r["target_pos"],
            "delta":      r["delta"],
            "min_price":  r["min_price"],
            "max_price":  r["max_price"],
            "current_price": r.get("current_price"),
        }
        _repricers[uid].append(rule)
    logger.info(f"Загружено {len(rows)} правил репрайсера из БД")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _add_log(user_id: int, msg: str):
    _log.setdefault(user_id, []).append(msg)
    if len(_log[user_id]) > MAX_LOG:
        _log[user_id].pop(0)


def _rule_label(rule: dict) -> str:
    ex     = EXCHANGE_NAMES.get(rule.get("exchange", "bybit"), "?")
    side_e = "📗" if rule["side"] == "1" else "📕"
    price  = rule.get("current_price")
    price_s = f"{price:,.2f}" if isinstance(price, float) else "—"
    return f"{side_e} {ex} {rule['asset']}/{rule['fiat']} топ-{rule['target_pos']} [{price_s}]"


def _repricers_kb(user_id: int, limit: int = MAX_REPRICERS) -> InlineKeyboardMarkup:
    rules   = _repricers.get(user_id, [])
    buttons = []
    for i, r in enumerate(rules):
        buttons.append([InlineKeyboardButton(
            text=_rule_label(r),
            callback_data=f"rp:del:{i}",
        )])
    if len(rules) < limit:
        buttons.append([InlineKeyboardButton(text="➕ Добавить правило", callback_data="rp:add:start")])
    buttons.append([InlineKeyboardButton(text="📋 Лог переценок", callback_data="rp:log")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад",         callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─── Handlers ──────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "rp:list")
async def rp_list(callback: CallbackQuery):
    uid  = callback.from_user.id
    rps  = _repricers.get(uid, [])
    limit, _ = await get_limit(uid, "repricer_rules")
    text = (
        "🔄 <b>Авто-переценка</b>\n\n"
        "Бот автоматически корректирует цену объявлений каждые 60 сек.\n"
        "Поддерживаются: 🟠 Bybit, 🔵 OKX\n\n"
        f"Активных правил: {len(rps)}/{limit}\n"
        "Нажми на правило чтобы удалить."
    )
    await callback.message.edit_text(text, reply_markup=_repricers_kb(uid, limit), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "rp:add:start")
async def rp_add_start(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id

    # Лимит по плану — апселл при упоре
    current = len(_repricers.get(uid, []))
    allowed, limit, plan_key = await check_allowed(uid, "repricer_rules", current)
    if not allowed:
        await callback.message.edit_text(
            upsell_text("repricer_rules", current, limit, plan_key),
            reply_markup=upsell_kb(manage_cb="rp:list", manage_text="🗑 Удалить правило"),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    # Проверяем есть ли хоть один аккаунт (Bybit или OKX)
    has_bybit = get_active_account(uid, "bybit") is not None
    has_okx   = get_active_account(uid, "okx")   is not None

    if not has_bybit and not has_okx:
        await callback.message.edit_text(
            "❌ Сначала добавь API ключ.\n\n"
            "🔑 Аккаунты → Добавить аккаунт → Bybit или OKX",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Аккаунты", callback_data="acc:list")],
            ]),
        )
        return

    # Показываем выбор биржи (только те у которых есть активный аккаунт)
    ex_buttons = []
    if has_bybit:
        ex_buttons.append(InlineKeyboardButton(text="🟠 Bybit", callback_data="rp:ex:bybit"))
    if has_okx:
        ex_buttons.append(InlineKeyboardButton(text="🔵 OKX",   callback_data="rp:ex:okx"))

    await callback.message.edit_text(
        "🔄 <b>Авто-переценка — выбери биржу</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            ex_buttons,
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="rp:list")],
        ]),
    )


@router.callback_query(lambda c: c.data in ("rp:ex:bybit", "rp:ex:okx"))
async def rp_choose_exchange(callback: CallbackQuery, state: FSMContext):
    exchange = callback.data.split(":")[-1]
    await state.set_state(RepriceStates.waiting_ad_id)
    await state.update_data(exchange=exchange)
    ex_name = "Bybit" if exchange == "bybit" else "OKX"
    await callback.message.edit_text(
        f"🔄 <b>Авто-переценка {ex_name} — шаг 1/2</b>\n\n"
        "Введи <b>ID объявления</b>\n\n"
        f"{'Bybit: P2P → Мои объявления → открой объявление → ID в URL' if exchange == 'bybit' else 'OKX: C2C → Мои объявления → открой → advId в URL'}",
        parse_mode="HTML",
    )


@router.message(RepriceStates.waiting_ad_id)
async def rp_got_ad_id(message: Message, state: FSMContext):
    await state.update_data(ad_id=message.text.strip())
    await state.set_state(RepriceStates.waiting_params)
    await message.answer(
        "🔄 <b>Авто-переценка — шаг 2/2</b>\n\n"
        "Введи параметры одной строкой:\n"
        "<code>позиция шаг мин_цена макс_цена</code>\n\n"
        "<b>Примеры:</b>\n"
        "<code>1 0.5 510 530</code> — держаться #1, шаг 0.5, диапазон 510–530\n"
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
        await message.answer("❌ Неверный формат. Пример:\n<code>3 0.5 500 600</code>", parse_mode="HTML")
        return

    data     = await state.get_data()
    await state.clear()

    uid      = message.from_user.id
    exchange = data.get("exchange", "bybit")
    ad_id    = data["ad_id"]

    api_key, api_secret, extra = get_account_credentials(uid, exchange)
    if not api_key:
        await message.answer("❌ Нет активного аккаунта для этой биржи.")
        return

    wait = await message.answer("⏳ Загружаю данные объявления...")

    try:
        if exchange == "bybit":
            my_ads = await bybit_auth.get_my_ads(api_key, api_secret, status=10)
        else:
            my_ads = await okx_auth.get_my_ads(api_key, api_secret, extra)

        ad = next((a for a in my_ads if str(a["id"]) == str(ad_id)), None)
        if not ad:
            await wait.edit_text(
                "❌ Объявление не найдено.\n"
                "Проверь ID. Объявление должно быть активным."
            )
            return
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка API: <code>{e}</code>", parse_mode="HTML")
        return

    rules = _repricers.setdefault(uid, [])
    _limit, _ = await get_limit(uid, "repricer_rules")
    if len(rules) >= _limit:
        await wait.edit_text(
            upsell_text("repricer_rules", len(rules), _limit, _),
            reply_markup=upsell_kb(manage_cb="rp:list", manage_text="🗑 Удалить правило"),
            parse_mode="HTML",
        )
        return

    # Сохраняем в БД
    db_id = await db.repricer_add(
        uid, exchange, ad_id, ad["fiat"], ad["asset"], ad["side"],
        target_pos, delta, min_price, max_price, ad["price"],
    )

    rule = {
        "db_id":       db_id,
        "exchange":    exchange,
        "item_id":     ad_id,
        "fiat":        ad["fiat"],
        "asset":       ad["asset"],
        "side":        ad["side"],
        "target_pos":  target_pos,
        "delta":       delta,
        "min_price":   min_price,
        "max_price":   max_price,
        "current_price": ad["price"],
    }
    rules.append(rule)

    side_label = "продажа" if ad["side"] == "1" else "покупка"
    ex_name    = EXCHANGE_NAMES.get(exchange, exchange.title())
    await wait.edit_text(
        f"✅ <b>Правило добавлено!</b>\n\n"
        f"Биржа: {ex_name}\n"
        f"Объявление: {ad['asset']}/{ad['fiat']} ({side_label})\n"
        f"Цель: топ-{target_pos} | шаг: ±{delta}\n"
        f"Диапазон: {min_price:,.2f} — {max_price:,.2f}\n\n"
        "Переценка запустится в течение 60 секунд.",
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
    idx  = int(callback.data.split(":")[2])
    uid  = callback.from_user.id
    rps  = _repricers.get(uid, [])
    if 0 <= idx < len(rps):
        removed = rps.pop(idx)
        if db.ok() and removed.get("db_id"):
            await db.repricer_delete(uid, removed["db_id"])
        await callback.answer("✅ Удалено")
    await rp_list(callback)


# ─── Background task ───────────────────────────────────────────────────────────

async def run_repricer(bot) -> None:
    """Вызывается каждые 60 секунд из bot.py."""
    for user_id, rules in list(_repricers.items()):
        for rule in rules:
            try:
                exchange = rule.get("exchange", "bybit")
                fiat     = rule["fiat"]
                asset    = rule["asset"]
                side     = rule["side"]   # "1"=sell, "0"=buy (normalized)

                # Получаем кредс для этой биржи
                api_key, api_secret, extra = get_account_credentials(user_id, exchange)
                if not api_key:
                    continue

                # Публичный стакан (конкуренты)
                if exchange == "bybit":
                    ads = await bybit_p2p.get_ads(asset=asset, fiat=fiat, side=side, size=20)
                else:  # okx
                    okx_side = "sell" if side == "1" else "buy"
                    ads = await okx_p2p.get_ads(asset=asset, fiat=fiat, side=okx_side, rows=20)

                if not ads:
                    continue

                # Сортировка: sell → ascending (дешевле лучше), buy → descending (дороже лучше)
                if side == "1":
                    sorted_ads   = sorted(ads, key=lambda x: x["price"])
                    calc_target  = lambda ref: round(ref - rule["delta"], 2)
                else:
                    sorted_ads   = sorted(ads, key=lambda x: x["price"], reverse=True)
                    calc_target  = lambda ref: round(ref + rule["delta"], 2)

                target_pos = rule["target_pos"]
                if len(sorted_ads) >= target_pos:
                    target_price = calc_target(sorted_ads[target_pos - 1]["price"])
                elif sorted_ads:
                    target_price = sorted_ads[-1]["price"]
                else:
                    continue

                # Зажимаем в диапазон
                target_price = max(rule["min_price"], min(rule["max_price"], target_price))
                old_price    = rule.get("current_price") or 0

                if abs(target_price - old_price) < 0.01:
                    continue

                # Обновляем цену через API
                if exchange == "bybit":
                    res      = await bybit_auth.update_ad_price(api_key, api_secret, rule["item_id"], target_price)
                    ret_code = res.get("retCode", -1)
                    success  = ret_code == 0
                    err_msg  = res.get("retMsg", "?") if not success else ""
                else:  # okx
                    res      = await okx_auth.update_ad_price(api_key, api_secret, extra, rule["item_id"], target_price)
                    ret_code = str(res.get("code", "-1"))
                    success  = ret_code == "0"
                    err_msg  = res.get("msg", "?") if not success else ""

                ts     = datetime.now().strftime("%H:%M:%S")
                ex_tag = "BY" if exchange == "bybit" else "OX"

                if success:
                    rule["current_price"] = target_price
                    if db.ok() and rule.get("db_id"):
                        await db.repricer_update_price(rule["db_id"], target_price)
                    _add_log(user_id,
                             f"[{ts}] [{ex_tag}] {asset}/{fiat} {old_price:,.2f} → <b>{target_price:,.2f}</b>")
                    logger.info(f"Repriced uid={user_id} {exchange} {asset}/{fiat} "
                                f"{old_price}→{target_price}")
                else:
                    _add_log(user_id, f"[{ts}] [{ex_tag}] ❌ {asset}/{fiat}: {err_msg}")
                    logger.warning(f"Reprice failed uid={user_id} {exchange}: {err_msg}")

            except Exception as e:
                logger.error(f"Repricer error uid={user_id}: {e}")
