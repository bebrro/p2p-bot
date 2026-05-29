"""
Монетизация: планы Free / Pro / Team.
Оплата через USDT TRC20 (0% комиссия).
Автоматическая верификация через TronScan API.

Команды:
  /subscribe             — показать планы и купить
  /pay_confirm TXID      — проверить транзакцию и активировать
  /give_pro ID DAYS plan — выдать подписку (только ADMIN_IDS)
"""
import aiohttp
import logging
import time
from datetime import datetime, timezone, timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, Message,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import db
from config import ADMIN_IDS, CRYPTO_WALLET_TRC20
from utils.subscription import PLANS, get_plan_key, format_plan_card, format_expires

logger = logging.getLogger(__name__)
router = Router()

# USDT TRC20 contract on TRON network
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# In-memory pending payments: {user_id: {"plan": "pro", "amount_usdt": 9.99, "created": unix_ts}}
_pending: dict[int, dict] = {}
PAYMENT_EXPIRE_SEC = 86_400  # 24 часа


# ─── TronScan verification ─────────────────────────────────────────────────────

async def _verify_trc20(txid: str, wallet: str, min_usdt: float) -> tuple[bool, str]:
    """
    Проверяет USDT TRC20 транзакцию через TronScan API.
    Возвращает (ok, detail_str).
    """
    if not wallet:
        return False, "Кошелёк для приёма платежей не настроен (CRYPTO_WALLET_TRC20)"
    try:
        url = f"https://apilist.tronscan.org/api/transaction-info?hash={txid}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json(content_type=None)

        if not data or not isinstance(data, dict):
            return False, "Транзакция не найдена в сети TRON"

        if data.get("contractRet") != "SUCCESS":
            ret = data.get("contractRet") or data.get("receipt", {}).get("result", "?")
            return False, f"Транзакция не подтверждена (статус: {ret})"

        # Проверяем через tokenTransferInfo (USDT TRC20 — TRC10/TRC20 transfer)
        ti = data.get("tokenTransferInfo") or {}
        if not ti:
            # Попробуем через trc20TransferInfo
            trc20_list = data.get("trc20TransferInfo") or []
            if trc20_list:
                ti = trc20_list[0]

        if not ti:
            return False, "Не найдена информация о передаче TRC20 токена"

        # Получатель
        to_addr = ti.get("to_address") or ti.get("to") or ""
        if to_addr.lower() != wallet.lower():
            return False, f"Получатель не совпадает с нашим кошельком"

        # Контракт (токен)
        contract = ti.get("contract_address") or ti.get("tokenId") or ""
        if contract != USDT_TRC20_CONTRACT:
            return False, f"Токен не USDT (TRC20). Контракт: {contract or '?'}"

        # Сумма (6 знаков после запятой у USDT TRC20)
        raw_amount = ti.get("amount_str") or ti.get("amount") or "0"
        amount_usdt = float(raw_amount) / 1_000_000

        if amount_usdt < min_usdt * 0.99:
            return False, (
                f"Сумма {amount_usdt:.2f} USDT меньше ожидаемой {min_usdt:.2f} USDT\n"
                f"Разница: {min_usdt - amount_usdt:.2f} USDT"
            )

        return True, f"{amount_usdt:.2f}"

    except aiohttp.ClientError as e:
        return False, f"Ошибка сети при проверке: {e}"
    except Exception as e:
        logger.exception("TronScan verification error")
        return False, f"Ошибка проверки: {e}"


# ─── Keyboard helpers ──────────────────────────────────────────────────────────

def _sub_kb(current_plan: str) -> InlineKeyboardMarkup:
    btns = []
    if current_plan != "pro":
        btns.append([InlineKeyboardButton(
            text="⭐ Pro — 9.99 USDT / мес",  callback_data="sub:pay:pro",
        )])
    if current_plan != "team":
        btns.append([InlineKeyboardButton(
            text="👑 Team — 24.99 USDT / мес", callback_data="sub:pay:team",
        )])
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=btns)


async def _get_sub_text(uid: int) -> tuple[str, str]:
    """Возвращает (html_text, plan_key)."""
    sub      = await db.subscription_get(uid)
    plan_key = get_plan_key(sub)
    expires  = format_expires(sub) if sub else ""

    lines = [
        "⭐ <b>Подписка P2P Monitor</b>",
        "",
        f"Твой план: <b>{PLANS[plan_key]['emoji']} {PLANS[plan_key]['name']}</b>"
        + (f"  —  {expires}" if expires else ""),
        "",
        "─────────────────",
        format_plan_card("free",  plan_key == "free"),
        "",
        format_plan_card("pro",   plan_key == "pro"),
        "",
        format_plan_card("team",  plan_key == "team"),
        "",
        "─────────────────",
        "💳 Оплата через <b>USDT TRC20</b> (0% комиссия)",
        "Нажми кнопку ниже — получишь адрес кошелька и инструкцию.",
    ]
    return "\n".join(lines), plan_key


# ─── /subscribe & sub:list ─────────────────────────────────────────────────────

@router.message(Command("subscribe"))
@router.callback_query(lambda c: c.data == "sub:list")
async def sub_list(event: Message | CallbackQuery):
    uid = event.from_user.id
    text, plan_key = await _get_sub_text(uid)
    kb = _sub_kb(plan_key)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


# ─── sub:pay:pro / sub:pay:team ────────────────────────────────────────────────

@router.callback_query(lambda c: c.data in ("sub:pay:pro", "sub:pay:team"))
async def sub_pay(callback: CallbackQuery):
    plan_key = callback.data.split(":")[2]          # "pro" or "team"
    plan     = PLANS.get(plan_key)
    if not plan:
        await callback.answer("Ошибка плана", show_alert=True)
        return

    uid        = callback.from_user.id
    amount     = plan["price_usdt"]
    days       = plan["duration_days"]
    wallet     = CRYPTO_WALLET_TRC20 or "⚠️ кошелёк не настроен"

    # Сохраняем pending-платёж
    _pending[uid] = {
        "plan":        plan_key,
        "amount_usdt": amount,
        "created":     time.time(),
    }

    text = (
        f"💳 <b>Оплата {plan['emoji']} {plan['name']} — {amount} USDT</b>\n\n"
        f"Сеть: <b>TRON (TRC20)</b>\n\n"
        f"📋 Адрес кошелька:\n"
        f"<code>{wallet}</code>\n\n"
        f"💵 Сумма: <b>{amount} USDT</b>\n"
        f"📅 Период: <b>{days} дней</b>\n\n"
        f"<b>Как оплатить:</b>\n"
        f"1. Отправь ровно <b>{amount} USDT TRC20</b> на адрес выше\n"
        f"2. Дождись 1-2 подтверждений в сети (~1-2 мин)\n"
        f"3. Скопируй <b>TXID (хэш транзакции)</b>\n"
        f"4. Отправь боту:\n"
        f"   <code>/pay_confirm ВАШ_TXID</code>\n\n"
        f"⏰ Платёж действителен 24 часа.\n"
        f"❓ Проблемы? Напиши /start и обратись в поддержку."
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К планам", callback_data="sub:list")],
        ]),
    )
    await callback.answer()


# ─── /pay_confirm TXID ─────────────────────────────────────────────────────────

@router.message(Command("pay_confirm"))
async def pay_confirm(message: Message):
    uid   = message.from_user.id
    parts = message.text.split()

    if len(parts) < 2:
        await message.answer(
            "❌ Укажи TXID транзакции:\n"
            "<code>/pay_confirm TXID_ТРАНЗАКЦИИ</code>",
            parse_mode="HTML",
        )
        return

    txid = parts[1].strip()

    # Проверяем pending
    pending = _pending.get(uid)
    if not pending:
        await message.answer(
            "⚠️ Нет ожидающего платежа.\n"
            "Сначала выбери план: /subscribe",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⭐ К подпискам", callback_data="sub:list")],
            ]),
        )
        return

    # Проверяем срок действия pending
    if time.time() - pending["created"] > PAYMENT_EXPIRE_SEC:
        _pending.pop(uid, None)
        await message.answer(
            "⏰ Время ожидания платежа истекло (24 часа).\n"
            "Начни заново: /subscribe",
        )
        return

    plan_key   = pending["plan"]
    amount     = pending["amount_usdt"]
    wallet     = CRYPTO_WALLET_TRC20

    await message.answer("⏳ Проверяю транзакцию в сети TRON...")

    ok, detail = await _verify_trc20(txid, wallet, amount)

    if not ok:
        await message.answer(
            f"❌ <b>Транзакция не прошла проверку</b>\n\n"
            f"Причина: {detail}\n\n"
            f"Попробуй снова через 1-2 минуты (нужно 1-2 подтверждения):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Проверить снова",
                    callback_data=f"sub:verify:{txid}",
                )],
                [InlineKeyboardButton(text="⬅️ К планам", callback_data="sub:list")],
            ]),
        )
        return

    # ✅ Верификация прошла — активируем подписку
    await _activate_subscription(message, uid, plan_key, amount_confirmed=detail, txid=txid)


# ─── sub:verify:{txid} callback (retry) ───────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("sub:verify:"))
async def sub_verify_retry(callback: CallbackQuery):
    uid  = callback.from_user.id
    txid = callback.data[len("sub:verify:"):]

    pending = _pending.get(uid)
    if not pending:
        await callback.answer("Нет ожидающего платежа. Выбери план заново.", show_alert=True)
        return

    plan_key   = pending["plan"]
    amount     = pending["amount_usdt"]
    wallet     = CRYPTO_WALLET_TRC20

    await callback.message.edit_text("⏳ Проверяю транзакцию...")
    await callback.answer()

    ok, detail = await _verify_trc20(txid, wallet, amount)

    if not ok:
        await callback.message.edit_text(
            f"❌ <b>Транзакция не прошла проверку</b>\n\n"
            f"Причина: {detail}\n\n"
            f"Попробуй ещё раз через минуту:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Проверить снова",
                    callback_data=f"sub:verify:{txid}",
                )],
                [InlineKeyboardButton(text="⬅️ К планам", callback_data="sub:list")],
            ]),
        )
        return

    await _activate_subscription(callback.message, uid, plan_key, amount_confirmed=detail, txid=txid)


# ─── Activation helper ─────────────────────────────────────────────────────────

async def _activate_subscription(
    msg_obj: Message,
    uid: int,
    plan_key: str,
    amount_confirmed: str,
    txid: str,
) -> None:
    """Записывает подписку в БД, отправляет подтверждение."""
    plan       = PLANS[plan_key]
    days       = plan["duration_days"]
    expires_at = datetime.now(timezone.utc) + timedelta(days=days)

    await db.subscription_set(uid, plan_key, expires_at)
    _pending.pop(uid, None)

    await msg_obj.answer(
        f"🎉 <b>Подписка активирована!</b>\n\n"
        f"План: <b>{plan['emoji']} {plan['name']}</b>\n"
        f"Действует: <b>{days} дней</b>  →  до {expires_at.strftime('%d.%m.%Y')}\n"
        f"Оплачено: <b>{amount_confirmed} USDT</b>\n"
        f"TXID: <code>{txid[:20]}…</code>\n\n"
        f"Разблокировано:\n"
        f"✅ Репрайсер до {plan['repricer_rules']} правил\n"
        f"✅ Трекеры до {plan['trackers']}\n"
        f"✅ Smart Арбитраж (4 биржи)\n"
        f"✅ P&L трекер\n"
        f"✅ Экспорт данных",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 P&L Трекер",    callback_data="pnl:view")],
            [InlineKeyboardButton(text="⬅️ Главное меню",  callback_data="back:main")],
        ]),
    )
    logger.info(
        f"Subscription activated: uid={uid} plan={plan_key} "
        f"days={days} amount={amount_confirmed} txid={txid}"
    )


# ─── Admin: /give_pro ─────────────────────────────────────────────────────────

@router.message(Command("give_pro"))
async def admin_give_pro(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return   # молча игнорируем

    parts = message.text.split()
    # /give_pro USER_ID [DAYS] [plan]
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/give_pro USER_ID [DAYS] [pro|team]</code>\n"
            "Пример: <code>/give_pro 123456789 30 pro</code>",
            parse_mode="HTML",
        )
        return

    try:
        target_uid = int(parts[1])
        days       = int(parts[2]) if len(parts) >= 3 else 30
        plan_key   = parts[3].lower() if len(parts) >= 4 else "pro"
        if plan_key not in PLANS:
            plan_key = "pro"
    except ValueError:
        await message.answer("❌ Неверный формат. /give_pro USER_ID DAYS [pro|team]")
        return

    expires_at = datetime.now(timezone.utc) + timedelta(days=days)
    await db.subscription_set(target_uid, plan_key, expires_at)

    plan = PLANS[plan_key]
    await message.answer(
        f"✅ Подписка выдана!\n"
        f"Пользователь: <code>{target_uid}</code>\n"
        f"План: {plan['emoji']} {plan['name']}\n"
        f"Действует до: {expires_at.strftime('%d.%m.%Y')} ({days}д)",
        parse_mode="HTML",
    )
    logger.info(f"Admin give_pro: uid={target_uid} plan={plan_key} days={days} by={message.from_user.id}")
