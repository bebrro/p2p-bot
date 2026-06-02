"""
Монетизация: планы Free / Pro / Max.
Оплата через USDT TRC20 (0% комиссия).
Автоматическая верификация через TronScan API.

Команды:
  /subscribe             — показать планы и купить
  /pay_confirm TXID      — проверить транзакцию и активировать
  /give_pro ID DAYS plan — выдать подписку (только ADMIN_IDS)
"""
import aiohttp
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, Message, PreCheckoutQuery, LabeledPrice,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import db
from config import ADMIN_IDS, CRYPTO_WALLET_TRC20, ADMIN_USERNAME
from utils.subscription import PLANS, get_plan_key, format_plan_card, format_expires

logger = logging.getLogger(__name__)
router = Router()

# USDT TRC20 contract on TRON network
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# Pending payments хранятся в PostgreSQL (db.pending_payment_*), чтобы
# переживать рестарты Railway. _pending_mem — fallback для локальной
# разработки без БД.
_pending_mem: dict[int, dict] = {}
PAYMENT_EXPIRE_SEC = 86_400  # 24 часа


async def _pending_set(uid: int, plan: str, amount: float, lifetime: bool) -> None:
    if db.ok():
        await db.pending_payment_set(uid, plan, amount, lifetime)
    else:
        _pending_mem[uid] = {
            "plan": plan, "amount_usdt": amount,
            "lifetime": lifetime, "created": time.time(),
        }


async def _pending_get(uid: int) -> dict | None:
    if db.ok():
        return await db.pending_payment_get(uid)
    p = _pending_mem.get(uid)
    if p and time.time() - p["created"] > PAYMENT_EXPIRE_SEC:
        _pending_mem.pop(uid, None)
        return None
    return p


async def _pending_pop(uid: int) -> None:
    if db.ok():
        await db.pending_payment_delete(uid)
    else:
        _pending_mem.pop(uid, None)


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

def _admin_btn() -> list | None:
    """Кнопка 'Написать администратору' если ADMIN_USERNAME задан."""
    if not ADMIN_USERNAME:
        return None
    return [InlineKeyboardButton(
        text="💬 Написать администратору",
        url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}",
    )]


def _sub_kb(current_plan: str) -> InlineKeyboardMarkup:
    btns = []
    if current_plan != "pro":
        btns.append([
            InlineKeyboardButton(text="⭐ Pro — 12.99/мес",     callback_data="sub:pay:pro"),
            InlineKeyboardButton(text="⭐ Pro — 59 навсегда🔥", callback_data="sub:pay:pro_life"),
        ])
    if current_plan != "team":
        btns.append([
            InlineKeyboardButton(text="👑 Max — 24.99/мес",      callback_data="sub:pay:team"),
            InlineKeyboardButton(text="👑 Max — 149 навсегда🔥",  callback_data="sub:pay:team_life"),
        ])
    admin = _admin_btn()
    if admin:
        btns.append(admin)
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=btns)


async def _get_sub_text(uid: int) -> tuple[str, str]:
    """Возвращает (html_text, plan_key)."""
    sub      = await db.subscription_get(uid)
    plan_key = get_plan_key(sub)
    expires  = format_expires(sub) if sub else ""

    lines = [
        "🚀 <b>P2P Sniper — тарифы</b>",
        "",
        "Один пропущенный кидала или одна связка окупают подписку на месяцы вперёд.",
        "",
        f"Твой план: <b>{PLANS[plan_key]['emoji']} {PLANS[plan_key]['name']}</b>"
        + (f"  —  {expires}" if expires else ""),
        "",
        "━━━━━━━━━━━━━━━━━",
        format_plan_card("free",  plan_key == "free"),
        "",
        "━━━━━━━━━━━━━━━━━",
        format_plan_card("pro",   plan_key == "pro"),
        "",
        "━━━━━━━━━━━━━━━━━",
        format_plan_card("team",  plan_key == "team"),
        "",
        "━━━━━━━━━━━━━━━━━",
        "💳 USDT TRC20 (0% комиссии) или ⭐ Telegram Stars (в 1 тап)",
        "♾ <b>Lifetime</b> = заплатил раз и навсегда. Окупается за ~6 месяцев против помесячной.",
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


# ─── sub:pay:* — выбор способа оплаты ──────────────────────────────────────────

def _parse_variant(suffix: str) -> tuple[str, bool]:
    """'pro_life' → ('pro', True);  'team' → ('team', False)."""
    lifetime = suffix.endswith("_life")
    plan_key = suffix[:-5] if lifetime else suffix
    return plan_key, lifetime


@router.callback_query(lambda c: c.data and c.data.startswith("sub:pay:"))
async def sub_pay(callback: CallbackQuery):
    """Экран выбора способа оплаты: USDT (0%) или Telegram Stars (в 1 тап)."""
    suffix = callback.data[len("sub:pay:"):]
    plan_key, lifetime = _parse_variant(suffix)
    plan = PLANS.get(plan_key)
    if not plan:
        await callback.answer("Ошибка плана", show_alert=True)
        return

    usdt  = plan["price_lifetime"]   if lifetime else plan["price_usdt"]
    stars = plan["price_stars_life"] if lifetime else plan["price_stars"]
    period = "♾ навсегда" if lifetime else f"📅 {plan['duration_days']} дней"

    text = (
        f"💳 <b>Оплата {plan['emoji']} {plan['name']}</b>\n"
        f"Период: {period}\n\n"
        f"Выбери способ оплаты:\n\n"
        f"💵 <b>USDT TRC20</b> — {usdt}$ · комиссия 0%\n"
        f"   (нужен крипто-кошелёк)\n\n"
        f"⭐ <b>Telegram Stars</b> — {stars} ⭐ · оплата в 1 тап\n"
        f"   (прямо в Telegram, мгновенно)"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💵 USDT — {usdt}$",      callback_data=f"sub:usdt:{suffix}")],
        [InlineKeyboardButton(text=f"⭐ Stars — {stars}",      callback_data=f"sub:stars:{suffix}")],
        [InlineKeyboardButton(text="⬅️ К планам",             callback_data="sub:list")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ─── sub:usdt:* — оплата криптой ───────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("sub:usdt:"))
async def sub_pay_usdt(callback: CallbackQuery):
    suffix = callback.data[len("sub:usdt:"):]
    plan_key, lifetime = _parse_variant(suffix)
    plan   = PLANS.get(plan_key)
    if not plan:
        await callback.answer("Ошибка плана", show_alert=True)
        return

    uid    = callback.from_user.id
    amount = plan["price_lifetime"] if lifetime else plan["price_usdt"]
    wallet = CRYPTO_WALLET_TRC20 or "⚠️ кошелёк не настроен"

    # Сохраняем pending-платёж (в БД — переживёт рестарт)
    await _pending_set(uid, plan_key, amount, lifetime)

    if lifetime:
        period_str = "♾ <b>навсегда</b> 🔥"
        note       = "Подписка активируется <b>без срока действия</b>."
    else:
        period_str = f"📅 <b>{plan['duration_days']} дней</b>"
        note       = f"Следующий платёж через {plan['duration_days']} дней."

    text = (
        f"💳 <b>Оплата {plan['emoji']} {plan['name']}</b>\n\n"
        f"Сеть: <b>TRON (TRC20)</b>\n\n"
        f"📋 Адрес кошелька:\n"
        f"<code>{wallet}</code>\n\n"
        f"💵 Сумма: <b>{amount} USDT</b>\n"
        f"⏳ Период: {period_str}\n"
        f"{note}\n\n"
        f"<b>Как оплатить:</b>\n"
        f"1. Отправь ровно <b>{amount} USDT TRC20</b> на адрес выше\n"
        f"2. Дождись 1-2 подтверждений в сети (~1-2 мин)\n"
        f"3. Скопируй <b>TXID (хэш транзакции)</b>\n"
        f"4. Отправь боту:\n"
        f"   <code>/pay_confirm ВАШ_TXID</code>\n\n"
        f"⏰ Платёж действителен 24 часа.\n"
        f"❓ Проблемы? Напиши /start и обратись в поддержку."
    )

    pay_kb = []
    admin  = _admin_btn()
    if admin:
        pay_kb.append(admin)
    pay_kb.append([
        InlineKeyboardButton(text="⭐ Лучше оплатить Stars", callback_data=f"sub:stars:{suffix}"),
    ])
    pay_kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"sub:pay:{suffix}")])

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=pay_kb),
    )
    await callback.answer()


# ─── sub:stars:* — оплата Telegram Stars ───────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("sub:stars:"))
async def sub_pay_stars(callback: CallbackQuery, bot: Bot):
    suffix = callback.data[len("sub:stars:"):]
    plan_key, lifetime = _parse_variant(suffix)
    plan = PLANS.get(plan_key)
    if not plan:
        await callback.answer("Ошибка плана", show_alert=True)
        return

    stars  = plan["price_stars_life"] if lifetime else plan["price_stars"]
    period = "навсегда" if lifetime else f"на {plan['duration_days']} дней"
    title  = f"{plan['name']} {period}"
    desc   = (
        f"Подписка {plan['name']} {period}. "
        f"Репрайсер, {plan['trackers']} трекеров, Smart Арбитраж, P&L, экспорт."
    )
    # payload кодирует план и тип: 'sub:pro:1' (1=lifetime)
    payload = f"sub:{plan_key}:{1 if lifetime else 0}"

    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=title,
            description=desc,
            payload=payload,
            provider_token="",          # пусто = оплата Telegram Stars
            currency="XTR",             # код валюты Stars
            prices=[LabeledPrice(label=title, amount=stars)],
            start_parameter=f"sub_{plan_key}",
        )
        await callback.answer("Счёт отправлен ⭐")
    except Exception as e:
        logger.error(f"Stars invoice error uid={callback.from_user.id}: {e}")
        await callback.answer("Не удалось создать счёт. Попробуй USDT.", show_alert=True)


# ─── Telegram Stars: pre-checkout + успешная оплата ────────────────────────────

@router.pre_checkout_query()
async def stars_pre_checkout(pcq: PreCheckoutQuery, bot: Bot):
    """Подтверждаем все pre-checkout (товар цифровой, всегда в наличии)."""
    await bot.answer_pre_checkout_query(pcq.id, ok=True)


@router.message(F.successful_payment)
async def stars_successful_payment(message: Message):
    sp      = message.successful_payment
    payload = sp.invoice_payload or ""
    parts   = payload.split(":")
    if len(parts) != 3 or parts[0] != "sub":
        logger.warning(f"Unknown Stars payload: {payload}")
        return

    plan_key = parts[1]
    lifetime = parts[2] == "1"
    if plan_key not in PLANS:
        logger.warning(f"Unknown plan in Stars payload: {plan_key}")
        return

    await _activate_subscription(
        message,
        message.from_user.id,
        plan_key,
        amount_confirmed=str(sp.total_amount),
        txid=sp.telegram_payment_charge_id,
        lifetime=lifetime,
        currency="⭐",
        ref_label="Платёж",
    )


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

    # Проверяем pending (БД сама отсекает платежи старше 24ч)
    pending = await _pending_get(uid)
    if not pending:
        await message.answer(
            "⚠️ Нет ожидающего платежа (или истёк срок 24ч).\n"
            "Сначала выбери план: /subscribe",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⭐ К подпискам", callback_data="sub:list")],
            ]),
        )
        return

    plan_key   = pending["plan"]
    amount     = pending["amount_usdt"]
    lifetime   = pending.get("lifetime", False)
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
    await _activate_subscription(message, uid, plan_key, amount_confirmed=detail, txid=txid, lifetime=lifetime)


# ─── sub:verify:{txid} callback (retry) ───────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("sub:verify:"))
async def sub_verify_retry(callback: CallbackQuery):
    uid  = callback.from_user.id
    txid = callback.data[len("sub:verify:"):]

    pending = await _pending_get(uid)
    if not pending:
        await callback.answer("Нет ожидающего платежа. Выбери план заново.", show_alert=True)
        return

    plan_key   = pending["plan"]
    amount     = pending["amount_usdt"]
    lifetime   = pending.get("lifetime", False)
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

    await _activate_subscription(callback.message, uid, plan_key, amount_confirmed=detail, txid=txid, lifetime=lifetime)


# ─── Activation helper ─────────────────────────────────────────────────────────

async def _activate_subscription(
    msg_obj: Message,
    uid: int,
    plan_key: str,
    amount_confirmed: str,
    txid: str,
    lifetime: bool = False,
    currency: str = "USDT",
    ref_label: str = "TXID",
) -> None:
    """Записывает подписку в БД, отправляет подтверждение."""
    plan = PLANS[plan_key]

    if lifetime:
        expires_at  = None   # NULL в БД = бессрочно
        period_line = "♾ <b>Навсегда</b> — срок не истечёт 🔥"
    else:
        days        = plan["duration_days"]
        expires_at  = datetime.now(timezone.utc) + timedelta(days=days)
        period_line = f"Действует: <b>{days} дней</b>  →  до {expires_at.strftime('%d.%m.%Y')}"

    await db.subscription_set(uid, plan_key, expires_at)
    await _pending_pop(uid)
    # Сбрасываем флаги "подписка истекает" — при продлении нужно уведомлять заново
    if db.ok():
        await db.expiry_notif_clear(uid)
        # Логируем платёж для аналитики выручки
        try:
            amount_num = float(amount_confirmed)
        except (TypeError, ValueError):
            amount_num = 0.0
        pay_currency = "XTR" if currency == "⭐" else "USDT"
        await db.payment_log(uid, plan_key, lifetime, amount_num, pay_currency, txid)

    await msg_obj.answer(
        f"🎉 <b>Подписка активирована!</b>\n\n"
        f"План: <b>{plan['emoji']} {plan['name']}</b>\n"
        f"{period_line}\n"
        f"Оплачено: <b>{amount_confirmed} {currency}</b>\n"
        f"{ref_label}: <code>{txid[:24]}…</code>\n\n"
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
        f"lifetime={lifetime} amount={amount_confirmed} txid={txid}"
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
    if db.ok():
        await db.expiry_notif_clear(target_uid)   # сброс флагов истечения/win-back

    plan = PLANS[plan_key]
    await message.answer(
        f"✅ Подписка выдана!\n"
        f"Пользователь: <code>{target_uid}</code>\n"
        f"План: {plan['emoji']} {plan['name']}\n"
        f"Действует до: {expires_at.strftime('%d.%m.%Y')} ({days}д)",
        parse_mode="HTML",
    )
    logger.info(f"Admin give_pro: uid={target_uid} plan={plan_key} days={days} by={message.from_user.id}")


# ─── Win-back: возврат остывших триальщиков ────────────────────────────────────

_WINBACK_TEXT = (
    "👋 <b>Твой пробный Pro закончился</b>\n\n"
    "Эти 5 дней ты пользовался полным арсеналом:\n"
    "🤖 Авто-репрайсер — держал цену лучше конкурентов\n"
    "🐋 Whale Tracker · 🔔 Алерты на связки в ЛС\n"
    "🧾 AI-проверка чеков · 📈 P&L · ♾ AI-ассистент\n\n"
    "Сейчас ты на <b>Free</b> — большинство фич закрыто. 🔒\n\n"
    "🎁 <b>Вернись на Pro:</b>\n"
    "💵 59$ навсегда 🔥 или 12.99$/мес\n"
    "⭐ или Telegram Stars — оплата в 1 тап\n\n"
    "💡 <b>Или бесплатно:</b> пригласи 2 друзей → получи Pro в подарок!"
)


def _winback_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Вернуть Pro",      callback_data="sub:list")],
        [InlineKeyboardButton(text="👥 Пригласить друзей", callback_data="ref:show")],
    ])


async def check_winback(bot: Bot) -> None:
    """
    Фоновая задача (вызывать из bot.py раз в час).
    Находит триальщиков, у кого подписка истекла за последние 7 дней
    и кто ни разу не платил, шлёт ОДНО win-back сообщение (с дедупом).
    """
    if not db.ok():
        return
    candidates = await db.winback_candidates(within_days=7)
    sent = 0
    for row in candidates:
        uid = row["user_id"]
        if await db.expiry_notif_sent(uid, "winback"):
            continue
        try:
            await bot.send_message(
                uid, _WINBACK_TEXT, parse_mode="HTML", reply_markup=_winback_kb()
            )
            await db.expiry_notif_mark(uid, "winback")
            sent += 1
            await asyncio.sleep(0.05)   # ~20 msg/sec, не попасть под flood
        except Exception as e:
            logger.warning(f"winback uid={uid}: {e}")
    if sent:
        logger.info(f"Win-back sent: {sent} users")
