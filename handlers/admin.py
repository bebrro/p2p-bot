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


# ─── /selftest — полная проверка всех подсистем ───────────────────────────────

async def _check_parser() -> str:
    """Гоняет regex-парсер по эталонным кейсам."""
    from utils.desc_parser import parse_description as p
    cases = [
        ("не принимаю от третьих лиц",            lambda r: r["third_party"] is False),
        ("3 лица ок",                             lambda r: r["third_party"] is True),
        ("с любого банка по номеру карты",        lambda r: r["any_bank"] is True),
        ("Üçüncü şahıslardan kabul etmiyorum",    lambda r: r["third_party"] is False),
        ("рубим капусту, пиши @x в тг",           lambda r: r["scam_recruit"] is True),
        ("Ордер строго по заявкам",               lambda r: r["trap"] is True),
        ("только Т-Банк",                         lambda r: "Tinkoff" in r["banks"]),
    ]
    ok = sum(1 for txt, chk in cases if chk(p(txt)))
    mark = "✅" if ok == len(cases) else "🔴"
    return f"{mark} Парсер описаний: {ok}/{len(cases)} кейсов"


async def _check_ai() -> str:
    from api import gemini
    from utils import ai_desc
    if not ai_desc.enabled():
        return "⚪ AI-разбор: выключен (нет GEMINI_API_KEY или AI_DESC_PARSE=0)"
    try:
        t0 = time.time()
        ans = await gemini.ask("Ответь одним словом: ок", max_tokens=10)
        dt = time.time() - t0
        if ans.startswith("⏳"):     # лимит — не поломка, работает regex-фолбэк
            return "⚪ AI (Gemini): лимит исчерпан — работает regex-фолбэк"
        if ans.startswith("❌"):
            return f"🔴 AI (Gemini): {ans[:40]}"
        return f"✅ AI (Gemini): отвечает · {dt:.1f}с ({gemini.GEMINI_MODEL})"
    except Exception as e:
        return f"🔴 AI (Gemini): {type(e).__name__}"


async def _check_descriptions() -> list:
    """Сколько объявлений с непустым описанием у каждой биржи (по 12 шт)."""
    probes = [
        ("🟡 Binance",   lambda: binance_p2p.get_ads(asset="USDT", fiat="RUB", trade_type="BUY", rows=12, _force=True)),
        ("🟠 Bybit",     lambda: bybit_p2p.get_ads(asset="USDT", fiat="RUB", side="1", size=12, _force=True)),
        ("🔵 OKX",       lambda: okx_p2p.get_ads(asset="USDT", fiat="RUB", side="buy", rows=12, _force=True)),
        ("💎 TG Wallet", lambda: wallet_p2p.get_ads(asset="USDT", fiat="RUB", side="buy", rows=12, _force=True)),
    ]
    async def one(name, fn):
        try:
            ads = await fn()
            n = len(ads)
            got = sum(1 for a in ads if (a.get("description") or "").strip())
            if n == 0:
                return f"{name}: нет данных"
            mark = "✅" if got else "🔴"
            return f"{mark} {name}: {got}/{n} с описанием"
        except Exception as e:
            return f"🔴 {name}: {type(e).__name__}"
    return await asyncio.gather(*[one(n, f) for n, f in probes])


async def _check_link() -> str:
    try:
        from webapp.server import best_link
        t0 = time.time()
        lk = await best_link("RUB", "USDT")
        dt = time.time() - t0
        if lk:
            return f"✅ Связки: считаются · RUB сейчас +{lk['pct']:.2f}% · {dt:.1f}с"
        return f"✅ Связки: считаются · RUB сейчас нет · {dt:.1f}с"
    except Exception as e:
        return f"🔴 Связки: {type(e).__name__}"


@router.message(Command("selftest"))
async def selftest_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return
    wait = await message.answer("🧪 Полная проверка систем... (~15-20 сек)")

    # биржи
    ex_results = await asyncio.gather(*[_probe_one(n, fn) for n, fn in _PROBES])
    # остальное параллельно
    parser, ai, link, descs = await asyncio.gather(
        _check_parser(), _check_ai(), _check_link(), _check_descriptions(),
    )

    from utils import scam_db, ai_desc
    from api import binance_detail
    try:
        ai_cached = await db.ai_cache_count()
    except Exception:
        ai_cached = 0
    extra = [
        f"{'✅' if db.ok() else '⚪'} База данных: {'подключена' if db.ok() else 'память (без БД)'}",
        f"✅ Кэш разбора: {len(ai_desc._CACHE)} в памяти · {ai_cached:,} в БД".replace(",", " "),
        f"✅ Антискам-ЧС: {scam_db.count():,} кидал Bybit".replace(",", " "),
        f"{'✅' if binance_detail.enabled() else '⚪'} Binance-условия: "
        f"{'бёрнер настроен' if binance_detail.enabled() else 'выключено (нет сессии)'}",
        f"✅ Админов: {len(ADMIN_IDS)}",
    ]

    text = (
        "🧪 <b>Селф-тест системы</b>\n\n"
        "<b>Биржи:</b>\n" + "\n".join(ex_results) + "\n\n"
        "<b>Логика:</b>\n" + "\n".join([parser, ai, link]) + "\n\n"
        "<b>Описания у бирж:</b>\n" + "\n".join(descs) + "\n\n"
        "<b>Сервисы:</b>\n" + "\n".join(extra) + "\n\n"
        "<i>✅ ок · 🔴 ошибка · ⚪ выключено/нет данных</i>"
    )
    await wait.edit_text(text, parse_mode="HTML")
