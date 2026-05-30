"""
Админ-аналитика: воронка пользователей, подписки, выручка, конверсия.

Команда /admin — доступна только ADMIN_IDS.
Данные берутся из db.admin_stats() (агрегаты по users/subscriptions/payments).
"""
import asyncio
import logging
import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import db
from config import ADMIN_IDS, DISABLED_EXCHANGES
from api import binance_p2p, bybit_p2p, okx_p2p, wallet_p2p

logger = logging.getLogger(__name__)
router = Router()

# Курс конвертации Stars → USD (нетто при выводе через Fragment)
_STAR_USD = 0.013


def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def _format_stats(s: dict) -> str:
    if not s:
        return (
            "📊 <b>Админ-панель</b>\n\n"
            "⚠️ База данных недоступна — статистика не собирается.\n"
            "Проверь DATABASE_URL в Railway."
        )

    stars_usd = s["rev_stars"] * _STAR_USD
    total_usd = s["rev_usdt"] + stars_usd
    active_subs = s["active_pro"] + s["active_team"]

    return (
        "📊 <b>Админ-панель</b>\n\n"
        "👥 <b>Пользователи</b>\n"
        f"  Всего: <b>{s['total_users']}</b>\n"
        f"  Новых за 24ч: <b>+{s['new_24h']}</b>  ·  за 7д: <b>+{s['new_7d']}</b>\n\n"
        "⭐ <b>Подписки (активные)</b>\n"
        f"  Pro: <b>{s['active_pro']}</b>  ·  Team: <b>{s['active_team']}</b>\n"
        f"  Lifetime: <b>{s['lifetime_cnt']}</b>\n"
        f"  🎁 На триале (ещё не платили): <b>{s['trials']}</b>\n\n"
        "💰 <b>Выручка (всего)</b>\n"
        f"  USDT: <b>{s['rev_usdt']:.2f}$</b>\n"
        f"  Stars: <b>{s['rev_stars']} ⭐</b> (~{stars_usd:.2f}$)\n"
        f"  ИТОГО: <b>≈ {total_usd:.2f}$</b>\n"
        f"  Платежей: <b>{s['pay_cnt']}</b>  ·  за 24ч: <b>{s['pay_24h']}</b>\n\n"
        "📈 <b>За 7 дней</b>\n"
        f"  USDT: {s['rev_usdt_7d']:.2f}$  ·  Stars: {s['rev_stars_7d']} ⭐\n\n"
        "🎯 <b>Конверсия</b>\n"
        f"  Платящих: <b>{s['paid_users']}</b> из {s['total_users']}\n"
        f"  Conversion rate: <b>{s['conversion']}%</b>\n"
        f"  Активных подписок: <b>{active_subs}</b>"
    )


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:refresh")],
    ])


@router.message(Command("admin"))
async def admin_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return   # молча игнорируем не-админов
    stats = await db.admin_stats()
    await message.answer(_format_stats(stats), parse_mode="HTML", reply_markup=_kb())


@router.callback_query(lambda c: c.data == "admin:refresh")
async def admin_refresh(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    stats = await db.admin_stats()
    try:
        await callback.message.edit_text(
            _format_stats(stats), parse_mode="HTML", reply_markup=_kb()
        )
    except Exception:
        pass   # «message is not modified» если данные не изменились
    await callback.answer("Обновлено")


# ─── /health — проверка живости бирж (в обход kill-switch) ────────────────────

# Проба каждой биржи: (имя, корутина get_ads с _force=True)
_PROBES = [
    ("🟡 Binance",   lambda: binance_p2p.get_ads(asset="USDT", fiat="KZT", trade_type="BUY", rows=3, _force=True)),
    ("🟠 Bybit",     lambda: bybit_p2p.get_ads(asset="USDT", fiat="KZT", side="1", size=3, _force=True)),
    ("🔵 OKX",       lambda: okx_p2p.get_ads(asset="USDT", fiat="KZT", side="buy", rows=3, _force=True)),
    ("💎 TG Wallet", lambda: wallet_p2p.get_ads(asset="USDT", fiat="KZT", side="buy", rows=3, _force=True)),
]


async def _probe_one(name: str, coro_fn) -> str:
    t0 = time.time()
    try:
        ads = await coro_fn()
        dt  = time.time() - t0
        if ads:
            price = ads[0].get("price", "?")
            return f"{name}: ✅ {len(ads)} объяв · {dt:.1f}с · цена {price}"
        return f"{name}: ⚪ пусто · {dt:.1f}с (жив, но нет данных)"
    except Exception as e:
        dt = time.time() - t0
        return f"{name}: 🔴 {type(e).__name__} · {dt:.1f}с"


@router.message(Command("health"))
async def health_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return
    wait = await message.answer("🩺 Проверяю биржи (в обход отключений)...")
    results = await asyncio.gather(*[_probe_one(n, fn) for n, fn in _PROBES])

    disabled = ", ".join(sorted(DISABLED_EXCHANGES)) or "нет"
    text = (
        "🩺 <b>Health-check бирж</b>\n"
        "<i>(тест реальной сети, kill-switch проигнорирован)</i>\n\n"
        + "\n".join(results)
        + f"\n\n⏸ Сейчас отключены: <b>{disabled}</b>\n"
        "💡 Если биржа ✅ здесь, но отключена — убери её из "
        "<code>DISABLED_EXCHANGES</code> в Railway, чтобы вернуть."
    )
    await wait.edit_text(text, parse_mode="HTML")
