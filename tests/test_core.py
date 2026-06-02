"""
Smoke-тесты для критических pure-функций.
Запуск: pytest tests/ -v

Тестируем без БД и без сети — только логику.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from datetime import datetime, timezone, timedelta


# ─── utils/spread.py ──────────────────────────────────────────────────────────

from utils.spread import calc_spread

def test_spread_positive():
    r = calc_spread(500.0, 510.0)
    assert r["spread_abs"] == pytest.approx(10.0)
    assert r["spread_pct"] == pytest.approx(2.0)

def test_spread_zero_buy():
    r = calc_spread(0, 500.0)
    assert r["spread_pct"] == 0.0

def test_spread_equal_prices():
    r = calc_spread(100.0, 100.0)
    assert r["spread_pct"] == 0.0

def test_spread_inverted():
    """Продажа < покупка → отрицательный спред."""
    r = calc_spread(510.0, 500.0)
    assert r["spread_pct"] < 0


# ─── handlers/pnl.py — calc_pnl ───────────────────────────────────────────────

from handlers.pnl import calc_pnl

_SAMPLE_ORDERS = [
    {"side": "0", "fiat": "KZT", "price": 500.0, "amount": 500_000.0, "quantity": 1000.0},
    {"side": "0", "fiat": "KZT", "price": 502.0, "amount": 251_000.0, "quantity": 500.0},
    {"side": "1", "fiat": "KZT", "price": 510.0, "amount": 510_000.0, "quantity": 1000.0},
    {"side": "1", "fiat": "KZT", "price": 512.0, "amount": 256_000.0, "quantity": 500.0},
]

def test_calc_pnl_basic():
    s = calc_pnl(_SAMPLE_ORDERS)
    assert s["total"]    == 4
    assert s["buy_cnt"]  == 2
    assert s["sell_cnt"] == 2

def test_calc_pnl_avg_buy():
    s = calc_pnl(_SAMPLE_ORDERS)
    # Сред. покупка = (500k+251k) / (1000+500) = 751000/1500 ≈ 500.67
    assert s["avg_buy"] == pytest.approx(751_000 / 1500, rel=1e-4)

def test_calc_pnl_profit_positive():
    s = calc_pnl(_SAMPLE_ORDERS)
    assert s["est_profit"] > 0

def test_calc_pnl_margin_positive():
    s = calc_pnl(_SAMPLE_ORDERS)
    assert s["margin_pct"] > 0

def test_calc_pnl_empty():
    s = calc_pnl([])
    assert s["total"] == 0
    assert s["est_profit"] == 0

def test_calc_pnl_only_buys():
    orders = [{"side": "0", "fiat": "RUB", "price": 90.0, "amount": 9000.0, "quantity": 100.0}]
    s = calc_pnl(orders)
    assert s["buy_cnt"]  == 1
    assert s["sell_cnt"] == 0
    assert s["est_profit"] == 0   # нечего матчить

def test_calc_pnl_fiat_grouping():
    orders = [
        {"side": "0", "fiat": "KZT", "price": 500.0, "amount": 50_000.0, "quantity": 100.0},
        {"side": "1", "fiat": "RUB", "price": 90.0,  "amount":  9_000.0, "quantity": 100.0},
    ]
    s = calc_pnl(orders)
    assert "KZT" in s["fiats"]
    assert "RUB" in s["fiats"]


# ─── utils/subscription.py ────────────────────────────────────────────────────

from utils.subscription import get_plan_key, PLANS

def test_free_plan_no_sub():
    assert get_plan_key(None) == "free"

def test_free_plan_expired():
    expired = {
        "plan": "pro",
        "expires_at": datetime.now(timezone.utc) - timedelta(days=1),
    }
    assert get_plan_key(expired) == "free"

def test_pro_plan_active():
    active = {
        "plan": "pro",
        "expires_at": datetime.now(timezone.utc) + timedelta(days=10),
    }
    assert get_plan_key(active) == "pro"

def test_lifetime_fallback_after_subscription_expires():
    """Купил Pro навсегда + взял Max помесячно → после Max откат на Pro, не Free."""
    from utils.subscription import get_plan_key
    from datetime import datetime, timezone, timedelta
    past = datetime.now(timezone.utc) - timedelta(days=1)
    fut  = datetime.now(timezone.utc) + timedelta(days=5)
    assert get_plan_key({"plan": "team", "expires_at": past, "lifetime_plan": "pro"}) == "pro"
    assert get_plan_key({"plan": "team", "expires_at": fut,  "lifetime_plan": "pro"}) == "team"
    assert get_plan_key({"plan": "pro",  "expires_at": past}) == "free"   # без lifetime


def test_max_plan_no_lifetime():
    """Max — только подписка: в карточке нет 'навсегда'."""
    from utils.subscription import format_plan_card
    card = format_plan_card("team")
    assert "навсегда" not in card and "24.99" in card


def test_firsttouch_price(monkeypatch):
    """First-touch: новичку Pro навсегда за 59, старому — 79."""
    import asyncio
    from datetime import datetime, timezone, timedelta
    import handlers.subscription as sub
    import db
    monkeypatch.setattr(db, "ok", lambda: True)
    async def fresh(uid): return datetime.now(timezone.utc) - timedelta(hours=2)
    monkeypatch.setattr(db, "user_first_seen", fresh)
    assert asyncio.run(sub._eff_amount(1, "pro", True, "usdt")) == 59.0
    async def old(uid): return datetime.now(timezone.utc) - timedelta(days=10)
    monkeypatch.setattr(db, "user_first_seen", old)
    assert asyncio.run(sub._eff_amount(1, "pro", True, "usdt")) == 79.0
    assert asyncio.run(sub._eff_amount(1, "pro", False, "usdt")) == 12.99


def test_lifetime_no_expiry():
    """Lifetime подписка: expires_at = None → активна навсегда."""
    lifetime = {"plan": "pro", "expires_at": None}
    assert get_plan_key(lifetime) == "pro"

def test_subscription_naive_expires_no_crash():
    """Наивный expires_at (без tz) не должен ронять сравнение дат."""
    from datetime import datetime, timezone, timedelta
    from utils.subscription import get_plan_key, format_expires
    naive_future = datetime.now() + timedelta(days=5)          # без tzinfo
    assert get_plan_key({"plan": "pro", "expires_at": naive_future}) == "pro"
    naive_past = datetime.now() - timedelta(days=1)
    assert get_plan_key({"plan": "pro", "expires_at": naive_past}) == "free"
    # ISO-строка без таймзоны
    assert get_plan_key({"plan": "pro", "expires_at": "2999-01-01T00:00:00"}) == "pro"
    assert format_expires({"plan": "pro", "expires_at": naive_future}).startswith("⏰")

def test_get_history_strips_tz(monkeypatch):
    """get_history приводит tz-aware даты из БД к наивным (иначе краш сравнения)."""
    import asyncio
    from datetime import datetime, timezone
    import handlers.price_history as ph
    aware = [(datetime.now(timezone.utc), 500.0, 510.0)]
    monkeypatch.setattr(ph.db, "ok", lambda: True)
    async def _hist(*a, **k):
        return aware
    monkeypatch.setattr(ph.db, "history_get", _hist)
    ph._history.clear()
    rows = asyncio.run(ph.get_history("binance", "KZT", "USDT"))
    assert rows and all(ts.tzinfo is None for ts, _b, _s in rows)

def test_team_plan_active():
    active = {
        "plan": "team",
        "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
    }
    assert get_plan_key(active) == "team"

def test_plans_dict_complete():
    """Все планы имеют обязательные поля."""
    required = {"name", "emoji", "price_usdt", "price_lifetime", "duration_days"}
    for key, plan in PLANS.items():
        if key == "free":
            continue
        missing = required - set(plan.keys())
        assert not missing, f"Plan '{key}' missing: {missing}"


# ─── keyboards.py — структура меню ────────────────────────────────────────────

from keyboards import main_menu, alerts_submenu, auto_submenu, analytics_submenu, account_submenu

def test_main_menu_returns_markup():
    kb = main_menu()
    assert kb is not None
    assert len(kb.inline_keyboard) >= 3   # минимум 3 ряда кнопок

def test_main_menu_has_exchanges():
    """Все 4 биржи присутствуют в главном меню."""
    kb   = main_menu()
    cbs  = {btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data}
    for ex in ("exchange:binance", "exchange:bybit", "exchange:okx", "exchange:wallet"):
        assert ex in cbs, f"Missing exchange button: {ex}"

def test_submenus_have_back():
    """Каждое подменю имеет кнопку назад."""
    for fn in (alerts_submenu, auto_submenu, analytics_submenu, account_submenu):
        kb  = fn()
        cbs = {btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data}
        assert "back:main" in cbs, f"{fn.__name__} missing back:main button"

def test_no_duplicate_callbacks_main():
    """В главном меню нет дублирующих callback_data."""
    kb   = main_menu()
    cbs  = [btn.callback_data for row in kb.inline_keyboard
            for btn in row if btn.callback_data]
    assert len(cbs) == len(set(cbs)), "Duplicate callbacks in main_menu"


# ─── utils/http.py — базовая структура ────────────────────────────────────────

import asyncio
from utils.http import get_json, post_json

def test_http_functions_exist():
    import inspect
    assert inspect.iscoroutinefunction(get_json)
    assert inspect.iscoroutinefunction(post_json)


# ─── utils/error_reporter.py ──────────────────────────────────────────────────

from utils.error_reporter import setup_reporter, report as report_error

def test_reporter_no_crash_without_bot():
    """report() не должен падать если бот не настроен."""
    setup_reporter(None, 0)
    # Создаём event loop и запускаем корутину
    asyncio.run(report_error("test", ValueError("test error")))


# ─── Pending payments (in-memory fallback, без БД) ────────────────────────────

import handlers.subscription as subm

def _reset_pending_mem():
    subm._pending_mem.clear()

def test_pending_set_get_roundtrip():
    """Без БД pending пишется/читается из памяти."""
    _reset_pending_mem()
    asyncio.run(subm._pending_set(123, "pro", 9.99, False))
    p = asyncio.run(subm._pending_get(123))
    assert p is not None
    assert p["plan"] == "pro"
    assert p["amount_usdt"] == pytest.approx(9.99)
    assert p["lifetime"] is False

def test_pending_pop_removes():
    _reset_pending_mem()
    asyncio.run(subm._pending_set(456, "team", 24.99, True))
    asyncio.run(subm._pending_pop(456))
    assert asyncio.run(subm._pending_get(456)) is None

def test_pending_get_missing_returns_none():
    _reset_pending_mem()
    assert asyncio.run(subm._pending_get(999)) is None

def test_pending_expires_after_24h():
    """Платёж старше 24ч считается отсутствующим."""
    import time as _t
    _reset_pending_mem()
    subm._pending_mem[789] = {
        "plan": "pro", "amount_usdt": 9.99,
        "lifetime": False, "created": _t.time() - 90_000,  # >24ч назад
    }
    assert asyncio.run(subm._pending_get(789)) is None

def test_pending_lifetime_flag_preserved():
    _reset_pending_mem()
    asyncio.run(subm._pending_set(111, "pro", 59.0, True))
    p = asyncio.run(subm._pending_get(111))
    assert p["lifetime"] is True

def test_pending_overwrite_same_user():
    """Новый платёж того же юзера перезаписывает старый."""
    _reset_pending_mem()
    asyncio.run(subm._pending_set(222, "pro", 9.99, False))
    asyncio.run(subm._pending_set(222, "team", 24.99, False))
    p = asyncio.run(subm._pending_get(222))
    assert p["plan"] == "team"
    assert p["amount_usdt"] == pytest.approx(24.99)


# ─── utils/limits.py — план-зависимые лимиты + апселл ─────────────────────────

from utils.limits import get_limit, check_allowed, upsell_text, upsell_kb
from utils.subscription import PLANS as _PLANS

def test_limit_free_alerts():
    """Без БД (mock ok=False) лимит = Free."""
    limit, plan = asyncio.run(get_limit(1, "alerts"))
    assert plan == "free"
    assert limit == _PLANS["free"]["alerts"]

def test_check_allowed_under_limit():
    allowed, limit, plan = asyncio.run(check_allowed(1, "trackers", 0))
    assert allowed is True
    assert limit == _PLANS["free"]["trackers"]

def test_check_allowed_at_limit():
    """На границе лимита — не разрешено."""
    free_trackers = _PLANS["free"]["trackers"]
    allowed, limit, plan = asyncio.run(check_allowed(1, "trackers", free_trackers))
    assert allowed is False

def test_check_allowed_over_limit():
    allowed, _, _ = asyncio.run(check_allowed(1, "alerts", 999))
    assert allowed is False

def test_upsell_text_mentions_pro_price():
    txt = upsell_text("alerts", 3, 3, "free")
    assert "Pro" in txt
    assert "Лимит достигнут" in txt
    assert f"{_PLANS['pro']['price_usdt']:.2f}" in txt

def test_upsell_kb_has_upgrade_button():
    kb  = upsell_kb(manage_cb="alerts:list")
    cbs = {b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data}
    assert "sub:list"   in cbs   # кнопка апгрейда
    assert "alerts:list" in cbs  # кнопка управления
    assert "back:main"  in cbs

def test_upsell_kb_without_manage():
    kb  = upsell_kb()
    cbs = {b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data}
    assert "sub:list" in cbs
    assert "back:main" in cbs


# ─── Telegram Stars — цены и парсер вариантов ─────────────────────────────────

def test_stars_prices_present():
    """У платных планов есть цены в Stars."""
    for key in ("pro", "team"):
        assert _PLANS[key]["price_stars"] > 0
        assert _PLANS[key]["price_stars_life"] > 0

def test_stars_markup_over_usdt():
    """Stars-цена примерно на 30% дороже USDT (покрывает комиссию сторов)."""
    pro = _PLANS["pro"]
    # 650 ⭐ при курсе ~$0.02 ≈ $13 vs $9.99 USDT → наценка есть
    implied_usd = pro["price_stars"] * 0.02
    assert implied_usd > pro["price_usdt"]

def test_parse_variant():
    from handlers.subscription import _parse_variant
    assert _parse_variant("pro")       == ("pro", False)
    assert _parse_variant("pro_life")  == ("pro", True)
    assert _parse_variant("team")      == ("team", False)
    assert _parse_variant("team_life") == ("team", True)


# ─── handlers/admin.py — аналитика ────────────────────────────────────────────

import handlers.admin as adminm

_SAMPLE_STATS = {
    "total_users": 150, "new_24h": 5, "new_7d": 22,
    "active_pro": 12, "active_team": 3, "lifetime_cnt": 4,
    "trials": 8, "paid_users": 11,
    "rev_usdt": 109.89, "rev_stars": 3250,
    "rev_usdt_7d": 29.97, "rev_stars_7d": 650,
    "pay_cnt": 14, "pay_24h": 1, "conversion": 7.3,
}

def test_admin_format_contains_key_metrics():
    txt = adminm._format_stats(_SAMPLE_STATS)
    assert "150" in txt                # total users
    assert "109.89" in txt             # USDT revenue
    assert "3250" in txt               # Stars revenue
    assert "7.3%" in txt               # conversion
    assert "Conversion rate" in txt

def test_admin_format_empty_db():
    """Без БД — дружелюбное предупреждение, не падает."""
    txt = adminm._format_stats({})
    assert "недоступна" in txt.lower() or "database" in txt.lower()

def test_admin_stars_usd_conversion():
    """Stars пересчитываются в USD по курсу нетто."""
    txt = adminm._format_stats(_SAMPLE_STATS)
    # 3250 * 0.013 ≈ 42.25
    assert "42.2" in txt or "42.3" in txt

def test_admin_is_admin_check():
    adminm.ADMIN_IDS.clear()
    adminm.ADMIN_IDS.extend([111, 222])
    assert adminm._is_admin(111) is True
    assert adminm._is_admin(999) is False


# ─── Win-back ─────────────────────────────────────────────────────────────────

def test_winback_kb_buttons():
    kb  = subm._winback_kb()
    cbs = {b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data}
    assert "sub:list" in cbs    # вернуть Pro
    assert "ref:show" in cbs    # пригласить друзей

def test_winback_text_has_offer():
    txt = subm._WINBACK_TEXT
    assert "Pro" in txt
    assert "9.99" in txt or "Stars" in txt   # есть оффер
    assert "друзей" in txt                    # реферальный путь

def test_check_winback_noop_without_db():
    """Без БД check_winback не падает и просто выходит."""
    asyncio.run(subm.check_winback(None))   # bot=None, db.ok()=False → ранний return


# ─── Баннер меню (task 6) ─────────────────────────────────────────────────────

def test_menu_text_free_banner():
    """Без БД (free) — баннер зовёт открыть Pro."""
    import handlers.start as st
    txt = asyncio.run(st.menu_text(123))
    assert "Free" in txt
    assert "Pro" in txt
    assert "P2P Sniper" in txt   # MAIN_TEXT приклеен


# ─── Реферал в главном меню (task 7) ──────────────────────────────────────────

def test_main_menu_has_referral():
    kb  = main_menu()
    cbs = {b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data}
    assert "ref:show" in cbs

def test_main_menu_has_antiscam():
    kb  = main_menu()
    cbs = {b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data}
    assert "antiscam:start" in cbs


# ─── Антискам ─────────────────────────────────────────────────────────────────

def test_antiscam_memo_has_key_rule():
    """Памятка содержит главное правило безопасности."""
    import handlers.antiscam as a
    assert "реально пришли" in a._MEMO
    assert "НЕ доказательство" in a._MEMO or "Скриншот" in a._MEMO

def test_antiscam_receipt_prompt_reminds_to_verify():
    """Промпт анализа чека заставляет AI напомнить про реальное поступление."""
    import handlers.antiscam as a
    assert "реально пришли" in a._RECEIPT_PROMPT
    assert "подделк" in a._RECEIPT_PROMPT.lower()

def test_gemini_has_vision():
    import inspect
    from api import gemini
    assert inspect.iscoroutinefunction(gemini.vision)


# ─── AI советник: 4 биржи (task 8) ────────────────────────────────────────────

import handlers.ai_advisor as aim

def test_ai_supports_four_exchanges():
    assert set(aim._EX.keys()) == {"binance", "bybit", "okx", "wallet"}

def test_ai_start_kb_has_all_exchanges():
    kb  = aim._start_kb()
    cbs = {b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data}
    for ex in ("binance", "bybit", "okx", "wallet"):
        assert f"ai:ex:{ex}" in cbs

def test_ai_result_kb_offers_other_exchanges():
    """На результате OKX предлагает переключиться на 3 другие биржи."""
    kb  = aim._result_kb("okx", "KZT", "USDT")
    cbs = {b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data}
    assert "ai:go:binance:KZT:USDT" in cbs
    assert "ai:go:bybit:KZT:USDT"   in cbs
    assert "ai:go:wallet:KZT:USDT"  in cbs
    assert "ai:go:okx:KZT:USDT"     in cbs   # «спросить снова»

def test_ai_ad_tasks_unknown_exchange():
    buy, sell = aim._ad_tasks("nonexistent", "USDT", "KZT")
    assert buy is None and sell is None


# ─── utils/pricing.py — честный выбор цены + де-байтинг ───────────────────────

from utils.pricing import pick_best_price, sane_price

def _ads(*prices):
    return [{"price": p} for p in prices]

def test_pricing_empty_returns_none():
    assert pick_best_price([], buy_side=True, fiat="KZT") is None

def test_pricing_rejects_low_bait_on_buy():
    """Приманка 480 снизу не должна стать 'лучшей ценой покупки'."""
    ads = _ads(480, 508, 510, 511, 512, 513, 514, 515, 516, 520)
    buy = pick_best_price(ads, buy_side=True, fiat="KZT")
    assert buy >= 505   # 480-приманка отброшена

def test_pricing_rejects_high_bait_on_sell():
    """Приманки 553-557 сверху не должны стать 'лучшей ценой продажи'."""
    ads = _ads(505, 508, 509, 510, 511, 512, 513, 553, 555, 557)
    sell = pick_best_price(ads, buy_side=False, fiat="KZT")
    assert sell <= 520   # высокие приманки отброшены

def test_pricing_clean_book_unchanged():
    """Чистый стакан без приманок — берём настоящие экстремумы."""
    ads = _ads(510, 511, 512, 513, 514, 515)
    assert pick_best_price(ads, buy_side=True,  fiat="KZT") == 510
    assert pick_best_price(ads, buy_side=False, fiat="KZT") == 515

def test_pricing_sanity_rejects_garbage():
    """Нулевые и дикие цены отсекаются sane_price."""
    assert sane_price(0, "KZT") is False
    assert sane_price(-5, "KZT") is False
    assert sane_price(50, "KZT") is False      # USDT/KZT не может быть 50
    assert sane_price(512, "KZT") is True
    assert sane_price(95, "RUB") is True

def test_pricing_small_list_takes_extreme():
    """Мало данных (≤3) — отсекать нечего, берём крайнее."""
    ads = _ads(510, 515)
    assert pick_best_price(ads, buy_side=True,  fiat="KZT") == 510
    assert pick_best_price(ads, buy_side=False, fiat="KZT") == 515

def test_pricing_all_garbage_returns_none():
    ads = _ads(0, -1, 9999)   # всё вне диапазона KZT
    assert pick_best_price(ads, buy_side=True, fiat="KZT") is None


# ─── Отключённые биржи + флаг подозрительного спреда ──────────────────────────

def test_config_suspicious_threshold():
    from config import DISABLED_EXCHANGES, SUSPICIOUS_SPREAD_PCT
    # По умолчанию ни одна биржа не отключена (все 4 работают с Railway)
    assert isinstance(DISABLED_EXCHANGES, set)
    assert SUSPICIOUS_SPREAD_PCT == 5.0

def test_build_arbitrage_excludes_disabled():
    import webapp.server as srv
    exs = [
        {"id": "binance", "name": "Binance", "status": "ok",       "buy": 512, "sell": 525},
        {"id": "bybit",   "name": "Bybit",   "status": "ok",       "buy": 504, "sell": 556},
        {"id": "okx",     "name": "OKX",     "status": "disabled", "buy": None, "sell": None},
    ]
    arb = srv._build_arbitrage(exs)
    # disabled биржа не участвует в арбитраже
    assert all(a["from_id"] != "okx" and a["to_id"] != "okx" for a in arb)
    # спред >5% помечен подозрительным
    assert any(a["suspicious"] for a in arb)

def test_earn_demo_wow():
    """«Момент вау»: считает заработок на референсном обороте."""
    import webapp.server as srv
    arb = [
        {"from": "Binance", "to": "Bybit", "from_id": "binance", "to_id": "bybit",
         "pct": 2.0, "abs": 10.0, "suspicious": False},
    ]
    earn = srv._earn_demo(arb, "KZT")
    assert earn is not None
    # 500000 KZT × 2% = 10000
    assert earn["profit"] == 10_000
    assert earn["amount"] == 500_000
    assert earn["fiat"] == "KZT"

def test_earn_demo_prefers_credible():
    """Подозрительный спред не берётся для вау-числа, если есть нормальный."""
    import webapp.server as srv
    arb = [
        {"from": "A", "to": "B", "from_id": "a", "to_id": "b",
         "pct": 15.0, "abs": 75.0, "suspicious": True},     # фейк-спред
        {"from": "C", "to": "D", "from_id": "c", "to_id": "d",
         "pct": 1.5, "abs": 7.5, "suspicious": False},      # реальный
    ]
    earn = srv._earn_demo(arb, "USD")
    assert earn["pct"] == 1.5            # взял реальный, не 15%
    assert earn["profit"] == 15         # 1000 × 1.5%

def test_earn_demo_none_when_no_arb():
    import webapp.server as srv
    assert srv._earn_demo([], "KZT") is None


# ─── Связка «без карт» (треугольник посредника) ───────────────────────────────

def test_triangle_requires_explicit_third_party():
    """Связка берёт только ЯВНО подтвердивших 3-х лиц (None/False — мимо)."""
    import webapp.server as srv
    buy = [
        {"price": 500, "third_party": False, "nickname": "no3p", "min_amount": 0, "max_amount": 9e9, "pay_types": ["Kaspi"]},
        {"price": 503, "third_party": None,  "nickname": "unk",  "min_amount": 0, "max_amount": 9e9, "pay_types": ["Kaspi"]},
        {"price": 505, "third_party": True,  "nickname": "ok3p", "min_amount": 0, "max_amount": 9e9, "pay_types": ["Kaspi"]},
    ]
    sell = [
        {"price": 515, "third_party": None, "nickname": "may", "min_amount": 0, "max_amount": 9e9, "pay_types": ["Kaspi"]},
    ]
    # все sell-ноги «не указано» → связки нет (None недостаточно)
    assert srv._triangle(buy, sell, "KZT") is None
    # добавим явно подтверждённую sell-ногу → связка появляется
    sell.append({"price": 514, "third_party": True, "nickname": "yes3p",
                 "min_amount": 0, "max_amount": 9e9, "pay_types": ["Kaspi"]})
    t = srv._triangle(buy, sell, "KZT")
    assert t is not None
    assert t["buy"]["price"] == 505 and t["buy"]["confirm3p"] is True   # только ok3p
    assert t["sell"]["price"] == 514 and t["sell"]["confirm3p"] is True # только yes3p

def test_triangle_excludes_scammers():
    """Помеченные скамеры/ловушки не предлагаются в связке, даже с лучшей ценой."""
    import webapp.server as srv
    buy = [
        {"price": 500, "third_party": True, "scam_recruit": True, "nickname": "scammer",
         "min_amount": 0, "max_amount": 9e9, "pay_types": ["Kaspi"]},   # дешевле, но скам
        {"price": 506, "third_party": True, "nickname": "honest",
         "min_amount": 0, "max_amount": 9e9, "pay_types": ["Kaspi"]},
    ]
    sell = [
        {"price": 515, "third_party": True, "trap": True, "nickname": "trapper",
         "min_amount": 0, "max_amount": 9e9, "pay_types": ["Kaspi"]},   # дороже, но ловушка
        {"price": 512, "third_party": True, "nickname": "honest2",
         "min_amount": 0, "max_amount": 9e9, "pay_types": ["Kaspi"]},
    ]
    t = srv._triangle(buy, sell, "KZT")
    assert t is not None
    assert t["buy"]["nickname"] == "honest"    # скамер 500 исключён
    assert t["sell"]["nickname"] == "honest2"  # ловушка 515 исключена


def test_triangle_none_without_eligible_legs():
    import webapp.server as srv
    buy  = [{"price": 500, "third_party": False, "nickname": "x", "min_amount": 0, "max_amount": 9e9, "pay_types": ["Kaspi"]}]
    sell = [{"price": 515, "third_party": None,  "nickname": "y", "min_amount": 0, "max_amount": 9e9, "pay_types": ["Kaspi"]}]
    # покупка целиком запрещает 3-х лиц → связки нет
    assert srv._triangle(buy, sell, "KZT") is None

def test_triangle_requires_overlapping_limits():
    """Связка невыполнима если лимиты ног не пересекаются (8.5k vs 50k)."""
    import webapp.server as srv
    buy  = [{"price": 46.88, "third_party": True, "nickname": "Xpay",
             "min_amount": 8500, "max_amount": 8500, "pay_types": ["Kaspi"]}]
    sell = [{"price": 53.53, "third_party": True, "nickname": "Flash",
             "min_amount": 50000, "max_amount": 50000, "pay_types": ["Kaspi"]}]
    assert srv._triangle(buy, sell, "TRY") is None     # лимиты не пересекаются
    # совместимые лимиты → связка есть, с диапазоном пересечения
    buy2  = [{"price": 46.88, "third_party": True, "nickname": "A",
              "min_amount": 5000, "max_amount": 100000, "pay_types": ["Kaspi"]}]
    sell2 = [{"price": 53.53, "third_party": True, "nickname": "B",
              "min_amount": 10000, "max_amount": 80000, "pay_types": ["Kaspi"]}]
    t = srv._triangle(buy2, sell2, "TRY")
    assert t is not None
    assert t["lo"] == 10000 and t["hi"] == 80000       # пересечение лимитов


def test_triangle_requires_common_bank():
    """Без общего банка платёж не провести → связки нет."""
    import webapp.server as srv
    buy  = [{"price": 504, "third_party": True, "nickname": "A", "min_amount": 1000,
             "max_amount": 500000, "pay_types": ["Kaspi Bank"]}]
    sell = [{"price": 515, "third_party": True, "nickname": "B", "min_amount": 5000,
             "max_amount": 300000, "pay_types": ["Halyk"]}]   # другой банк
    assert srv._triangle(buy, sell, "KZT") is None
    # общий банк (Каспи в разных написаниях) → связка есть
    sell2 = [{"price": 515, "third_party": True, "nickname": "C", "min_amount": 5000,
              "max_amount": 300000, "pay_types": ["КаспиБанк"]}]
    t = srv._triangle(buy, sell2, "KZT")
    assert t is not None and t["banks"] == ["Kaspi Bank"]

def test_scam_blacklist_flags_and_excludes(monkeypatch):
    """Кидала из ЧС (Bybit userMaskId) помечается и не попадает в связку."""
    from utils import scam_db
    import webapp.server as srv
    monkeypatch.setattr(scam_db, "_BYBIT_SCAM", {"sdeadbeef": "мороз"})
    # _enrich помечает scam_recruit + known_scammer
    ad = {"price": 75, "nickname": "X", "user_mask_id": "sdeadbeef",
          "description": "", "pay_types": ["Kaspi"]}
    srv._enrich([ad])
    assert ad["known_scammer"] is True and ad["scam_recruit"] is True
    assert ad["scam_reason"] == "мороз"
    # связка не берёт помеченного кидалу
    buy = [{"price": 500, "third_party": True, "nickname": "S", "min_amount": 1000,
            "max_amount": 900000, "available": 5000, "pay_types": ["Kaspi"],
            "user_mask_id": "sdeadbeef", "scam_recruit": True}]
    sell = [{"price": 515, "third_party": True, "nickname": "B", "min_amount": 1000,
             "max_amount": 900000, "available": 5000, "pay_types": ["Kaspi"]}]
    assert srv._find_link(buy, sell, "KZT") is None
    # чужой mask — не кидала
    assert scam_db.is_scammer("sok123") is False


def test_bank_from_description():
    """Банк из описания («только Т-Банк») участвует в матчинге связки."""
    from utils.desc_parser import parse_description as p
    assert "Tinkoff" in p("★Только Т-Банк★ Переводы строго с Т-БАНКА")["banks"]
    assert p("Sadece Ziraat ve Garanti")["banks"] == ["Ziraat", "Garanti"]

    import webapp.server as srv
    B = lambda **k: {"price": 74, "third_party": True, "nickname": "A", "min_amount": 1000,
                     "max_amount": 900000, "available": 5000, **k}
    S = lambda **k: {"price": 76, "third_party": True, "nickname": "B", "min_amount": 1000,
                     "max_amount": 900000, "available": 5000, **k}
    # generic Bank Transfer, но реальный банк (Т-Банк) в описании у обоих → связка есть
    t = srv._find_link([B(pay_types=["Bank Transfer"], desc_banks=["Tinkoff"])],
                       [S(pay_types=["Bank Transfer"], desc_banks=["Tinkoff"])], "RUB")
    assert t is not None and t["banks"] == ["Tinkoff"]
    # разные банки в описании → generic не должен давать ложный матч
    assert srv._find_link([B(pay_types=["Bank Transfer"], desc_banks=["Tinkoff"])],
                          [S(pay_types=["Bank Transfer"], desc_banks=["Sber"])], "RUB") is None


def test_any_bank_relaxes_common_bank():
    """Если нога пишет «любой банк» — общий банк не требуется."""
    import webapp.server as srv
    buy  = [{"price": 504, "third_party": True, "any_bank": True, "nickname": "A",
             "min_amount": 1000, "max_amount": 500000, "pay_types": ["Freedom"]}]
    sell = [{"price": 515, "third_party": True, "nickname": "B", "min_amount": 5000,
             "max_amount": 300000, "pay_types": ["Halyk"]}]   # другой банк
    t = srv._find_link(buy, sell, "KZT")
    assert t is not None
    assert t["banks"] == ["Halyk"]          # показываем банк конкретной ноги
    buy2 = [{"price": 504, "third_party": True, "nickname": "A", "min_amount": 1000,
             "max_amount": 500000, "pay_types": ["Freedom"]}]
    assert srv._find_link(buy2, sell, "KZT") is None


def test_eff_max_capped_by_inventory():
    """Лимит «до 8 млн» при наличии 378 USDT режется до ≈ доступно×цена."""
    import webapp.server as srv
    ad = {"price": 577, "min_amount": 4500, "max_amount": 8_000_000,
          "available": 378.62, "pay_types": ["Kaspi"], "description": ""}
    srv._enrich([ad])
    assert ad["max_capped"] is True
    assert abs(ad["eff_max"] - 378.62 * 577) < 1      # ≈ 218 464
    assert ad["eff_max"] < ad["max_amount"]


def test_eff_max_not_capped_when_enough_inventory():
    """Если запаса хватает — eff_max = заявленный лимит, флага нет."""
    import webapp.server as srv
    ad = {"price": 577, "min_amount": 4500, "max_amount": 100_000,
          "available": 5000, "pay_types": ["Kaspi"], "description": ""}
    srv._enrich([ad])
    assert ad["max_capped"] is False
    assert ad["eff_max"] == 100_000


def test_triangle_uses_eff_max_for_overlap():
    """Связка считает перекрытие по реальному запасу, а не по фейк-лимиту."""
    import webapp.server as srv
    # buy: заявлен 8М, но в наличии 100 USDT × 500 = 50k реального потолка
    buy  = [{"price": 500, "third_party": True, "nickname": "A", "min_amount": 1000,
             "max_amount": 8_000_000, "available": 100, "pay_types": ["Kaspi"],
             "description": ""}]
    # sell: минимум 60k — выше реального потолка покупателя → пересечения нет
    sell = [{"price": 515, "third_party": True, "nickname": "B", "min_amount": 60_000,
             "max_amount": 300_000, "available": 10000, "pay_types": ["Kaspi"],
             "description": ""}]
    srv._enrich(buy); srv._enrich(sell)
    assert srv._find_link(buy, sell, "KZT") is None


def test_new_tool_endpoints_registered():
    """Антискам + whale роуты подключены в приложении."""
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    import webapp.server as srv
    app = srv.create_app()
    paths = {str(r.resource.canonical) for r in app.router.routes()}
    assert "/api/antiscam/receipt" in paths
    assert "/api/antiscam/nick/{nick}" in paths
    assert "/api/antiscam/report" in paths
    assert "/api/whales/{fiat}" in paths


def test_chat_quota_by_plan(monkeypatch):
    """Лимит AI-чата по плану: free=5, Pro=50 (fair-use), Max=безлимит."""
    import asyncio
    from datetime import datetime, timezone, timedelta
    import webapp.server as srv
    from utils.subscription import PLANS
    srv._chat_counts.clear()
    monkeypatch.setattr(srv.db, "ok", lambda: True)

    # free → ровно ai_daily (5)
    async def _no_sub(uid): return None
    monkeypatch.setattr(srv.db, "subscription_get", _no_sub)
    async def run(uid, n):
        used = 0
        for _ in range(n):
            ok, rem, plan = await srv._chat_quota(uid)
            if ok:
                srv._chat_used(uid); used += 1
        return used
    assert asyncio.run(run(111, 8)) == PLANS["free"]["ai_daily"]  # 5

    # Pro → 50 (НЕ безлимит — защита lifetime)
    async def _pro(uid):
        return {"plan": "pro", "expires_at": datetime.now(timezone.utc) + timedelta(days=5)}
    monkeypatch.setattr(srv.db, "subscription_get", _pro)
    assert asyncio.run(run(222, 60)) == PLANS["pro"]["ai_daily"]  # 50

    # Max → безлимит (-1)
    async def _max(uid):
        return {"plan": "team", "expires_at": datetime.now(timezone.utc) + timedelta(days=5)}
    monkeypatch.setattr(srv.db, "subscription_get", _max)
    ok, rem, plan = asyncio.run(srv._chat_quota(333))
    assert ok is True and rem == -1


def test_link_alerts_threshold_and_cooldown(monkeypatch):
    """Связка-алерт шлёт при net% ≥ порога и молчит по кулдауну/ниже порога."""
    import asyncio
    from handlers import link_alerts as la

    la._last_fired.clear()
    subs = {777: [{"fiat": "KZT", "asset": "USDT", "threshold": 2.0}]}
    monkeypatch.setattr(la, "get_all_arb_alerts", lambda: subs)

    link = {"buy": {"price": 500, "nickname": "A"}, "sell": {"price": 515, "nickname": "B"},
            "cross": False, "banks": ["Kaspi"], "profit_net": 2900, "pct_net": 2.9,
            "fee_fiat": 0, "amount": 100000, "lo": 1000, "hi": 300000, "suspicious": False}

    import webapp.server as srv
    async def _bl(fiat, asset="USDT"):
        return link
    monkeypatch.setattr(srv, "best_link", _bl)

    sent = []
    class _Bot:
        async def send_message(self, uid, text, **k):
            sent.append((uid, text))

    bot = _Bot()
    asyncio.run(la.check_link_alerts(bot))
    assert len(sent) == 1 and sent[0][0] == 777        # сработал

    asyncio.run(la.check_link_alerts(bot))
    assert len(sent) == 1                               # кулдаун — молчит

    # ниже порога — не шлём
    la._last_fired.clear()
    link["pct_net"] = 1.0
    asyncio.run(la.check_link_alerts(bot))
    assert len(sent) == 1

    # suspicious (приманка) — не шлём
    la._last_fired.clear()
    link["pct_net"] = 5.0
    link["suspicious"] = True
    asyncio.run(la.check_link_alerts(bot))
    assert len(sent) == 1


def test_ai_desc_disabled_without_key(monkeypatch):
    """Без GEMINI_API_KEY AI-разбор выключен, classify не лезет в сеть."""
    import asyncio
    from utils import ai_desc
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert ai_desc.enabled() is False
    res = asyncio.run(ai_desc.classify(["с любого банка", ""]))
    assert res == {}                      # ничего не классифицировано, без падений


def test_ai_desc_persistent_cache(monkeypatch):
    """После рестарта (память пуста) разбор берётся из БД, без вызова API."""
    import asyncio, json
    from utils import ai_desc
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("AI_DESC_PARSE", "1")
    ai_desc._CACHE.clear(); ai_desc._LAST_CALL = 0; ai_desc._COOLDOWN_UNTIL = 0

    store = {}
    async def cget(hashes): return {h: store[h] for h in hashes if h in store}
    async def cput(items): store.update(items)
    monkeypatch.setattr(ai_desc, "_key", ai_desc._key)  # noop, keep
    import db
    monkeypatch.setattr(db, "ai_cache_get", cget)
    monkeypatch.setattr(db, "ai_cache_put", cput)

    calls = {"n": 0}
    async def fake(prompt, **k):
        calls["n"] += 1
        n = prompt.split("ОПИСАНИЯ:\n", 1)[1].strip().count("\n") + 1
        return json.dumps([{"third_party": "no", "scam": False, "trap": False,
                            "any_bank": False, "banks": []} for _ in range(n)])
    monkeypatch.setattr(ai_desc.gemini, "ask_json", fake)

    asyncio.run(ai_desc.classify(["не принимаю 3 лица"]))
    assert calls["n"] == 1 and len(store) == 1          # разобрано + записано в БД

    ai_desc._CACHE.clear(); ai_desc._LAST_CALL = 0      # имитируем рестарт
    r = asyncio.run(ai_desc.classify(["не принимаю 3 лица"]))
    assert calls["n"] == 1                              # API НЕ дёргали — взяли из БД
    assert r["не принимаю 3 лица"]["third_party"] is False


def test_ai_desc_normalize():
    from utils import ai_desc
    assert ai_desc._normalize({"third_party": "yes"})["third_party"] is True
    assert ai_desc._normalize({"third_party": "no"})["third_party"] is False
    assert ai_desc._normalize({"third_party": "unknown"})["third_party"] is None
    n = ai_desc._normalize({"third_party": "no", "scam": True, "trap": False, "any_bank": True})
    assert n["scam_recruit"] is True and n["trap"] is False and n["any_bank"] is True


def test_ai_desc_uses_cache(monkeypatch):
    """При попадании в кэш classify не делает запрос к Gemini."""
    import asyncio
    from utils import ai_desc
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("AI_DESC_PARSE", "1")
    ai_desc._CACHE[ai_desc._key("кэш-тест")] = {
        "third_party": True, "scam_recruit": False, "trap": False, "any_bank": False}
    called = {"n": 0}
    async def _boom(*a, **k):
        called["n"] += 1
        return "[]"
    monkeypatch.setattr(ai_desc.gemini, "ask_json", _boom)
    res = asyncio.run(ai_desc.classify(["кэш-тест"]))
    assert res["кэш-тест"]["third_party"] is True
    assert called["n"] == 0               # всё из кэша, сеть не трогали


def test_apply_third_party_flag(monkeypatch):
    """AI-оверлей пересобирает плашку 3-х лиц без дублей."""
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    import webapp.server as srv
    ad = {"smart_flags": ["❌ нет 3-х лиц", "🕐 время ограничено"], "third_party": True}
    srv._apply_third_party_flag(ad)
    assert ad["smart_flags"].count("✅ 3-е лица ок") == 1
    assert "❌ нет 3-х лиц" not in ad["smart_flags"]
    assert "🕐 время ограничено" in ad["smart_flags"]


def test_any_bank_detection():
    from utils.desc_parser import parse_description as p
    assert p("Переводите с ЛЮБОГО банка!")["any_bank"] is True
    assert p("принимаю с любой карты")["any_bank"] is True
    assert p("не важно какой банк")["any_bank"] is True
    assert p("Kaspi только")["any_bank"] is False


def test_cross_exchange_link():
    """Межбиржевая связка: купить на одной, продать на другой, общий банк."""
    import webapp.server as srv
    buy  = [{"price": 504, "third_party": True, "nickname": "A", "min_amount": 1000,
             "max_amount": 500000, "pay_types": ["Kaspi"], "exchange": "bybit",
             "ex_name": "Bybit", "ex_icon": "🟠"}]
    sell = [{"price": 515, "third_party": True, "nickname": "B", "min_amount": 5000,
             "max_amount": 300000, "pay_types": ["Kaspi Bank"], "exchange": "wallet",
             "ex_name": "TG Wallet", "ex_icon": "💎"}]
    t = srv._find_link(buy, sell, "KZT")
    assert t is not None
    assert t["cross"] is True
    assert t["buy"]["ex_name"] == "Bybit" and t["sell"]["ex_name"] == "TG Wallet"

def test_norm_bank_aliases():
    import webapp.server as srv
    assert srv._norm_bank("Kaspi Bank") == srv._norm_bank("КаспиБанк") == "kaspi"
    assert srv._norm_bank("Tinkoff") == srv._norm_bank("Т-Банк") == "tinkoff"


def test_link_min_profit_threshold():
    """Связка с мизерной чистой прибылью (< MIN_LINK_PCT) не показывается."""
    import webapp.server as srv
    # спред ~0.02% (503.00 → 503.10) — шум, не возможность
    buy  = [{"price": 503.0, "third_party": True, "nickname": "A", "min_amount": 1000,
             "max_amount": 900000, "available": 5000, "pay_types": ["Kaspi"]}]
    sell = [{"price": 503.1, "third_party": True, "nickname": "B", "min_amount": 1000,
             "max_amount": 900000, "available": 5000, "pay_types": ["Kaspi"]}]
    assert srv._find_link(buy, sell, "KZT") is None
    # нормальная связка (~2%) — показывается
    sell2 = [{"price": 513.0, "third_party": True, "nickname": "B", "min_amount": 1000,
              "max_amount": 900000, "available": 5000, "pay_types": ["Kaspi"]}]
    assert srv._find_link(buy, sell2, "KZT") is not None


def test_price_bait_flagging():
    """Нереальные цены (вне ±5% медианы в выгодную сторону) → price_bait."""
    import webapp.server as srv
    # продажа: 564.7 слишком дорого при медиане ~521 → приманка, 535/530 — нет
    sell = [{"price": p} for p in [508, 510, 513, 521, 521, 521, 530, 535, 564.7]]
    srv._flag_price_baits(sell, "KZT", "USDT", buy_side=False)
    fb = {a["price"] for a in sell if a.get("price_bait")}
    assert 564.7 in fb and 535 not in fb and 521 not in fb
    # покупка: 450 слишком дёшево при медиане ~505 → приманка
    buy = [{"price": p} for p in [450, 503, 504, 505, 505, 506, 507, 508, 510]]
    srv._flag_price_baits(buy, "KZT", "USDT", buy_side=True)
    fbb = {a["price"] for a in buy if a.get("price_bait")}
    assert 450 in fbb and 503 not in fbb
    # цена вне разумного диапазона (_USDT_RANGE) → всегда приманка
    junk = [{"price": 1.0}] + [{"price": p} for p in [503, 504, 505, 505, 506]]
    srv._flag_price_baits(junk, "KZT", "USDT", buy_side=True)
    assert any(a["price"] == 1.0 and a.get("price_bait") for a in junk)


def test_triangle_none_when_no_spread():
    import webapp.server as srv
    buy  = [{"price": 515, "third_party": True, "nickname": "x", "min_amount": 0, "max_amount": 9e9, "pay_types": []}]
    sell = [{"price": 510, "third_party": True, "nickname": "y", "min_amount": 0, "max_amount": 9e9, "pay_types": []}]
    # купить дороже чем продать → положительной связки нет
    assert srv._triangle(buy, sell, "KZT") is None


def test_channel_post_format():
    """Пост для канала формируется из готовых строк арбитража."""
    import handlers.channel as ch
    rows = [
        {"fiat": "KZT", "buy_ex": "🟡 Binance", "buy": 510.0,
         "sell_ex": "🟠 Bybit", "sell": 519.0, "pct": 1.76},
        {"fiat": "RUB", "buy_ex": "🟠 Bybit", "buy": 90.0,
         "sell_ex": "🟡 Binance", "sell": 92.0, "pct": 2.22},
    ]
    txt = ch._format_post(rows, uname="@Sniper_P2P_Bot")
    assert "P2P Арбитраж" in txt
    assert "USDT/RUB" in txt and "USDT/KZT" in txt
    assert "@Sniper_P2P_Bot" in txt
    assert "🏆 Лучшее" in txt
    # сортировка по убыванию спреда — RUB (2.22) первым
    assert txt.index("USDT/RUB") < txt.index("USDT/KZT")

def test_channel_post_empty():
    """Нет положительных спредов → None (не постим пустоту)."""
    import handlers.channel as ch
    assert ch._format_post([]) is None
    assert ch._format_post([{"fiat": "KZT", "pct": 0}]) is None

def test_channel_scheduler_dormant_without_id():
    """Без CHANNEL_ID планировщик спит (no-op)."""
    import asyncio, handlers.channel as ch
    # CHANNEL_ID пустой в тестах → ранний выход, без падения
    asyncio.run(ch.channel_scheduler(None))


def test_desc_parser_third_party():
    """Парсер описаний правильно читает «3-е лица» в разных формах."""
    from utils.desc_parser import parse_description as p
    # НЕ принимает (вкл. реальные формулировки: разделители, «строго», «от 3»)
    for txt in ("не принимаю от третьих лиц", "третьи лица не принимаю",
                "без третьих лиц", "только свои переводы", "3 лица не принимаю",
                "от третьих лиц не приму", "С 3-ими лицами НЕ работаю",
                "от 3 не принимаю", "ТРЕТЬИ ЛИЦА - НЕ ПРИНИМАЮТСЯ",
                "ОТ 3 ЛИЦ СТРОГО НЕ ПРИНИМАЮ", "переводы только с ваших личных карт",
                # «только личные счета» = ограничение даже при «доверенных лицах»
                "Оплата принимается только с личных счетов или счетов доверенных лиц",
                "оплата только с личного счёта", "только свои счета",
                # «только от 1 го» = только первое лицо (даже при «на дов лицо»)
                "Принимаю только от 1 го", "только от 1-го", "работаю с 1 го лица",
                "только от первого лица",
                "Принимаю только от 1 го. на дов лицо. Принимаю на дов лицо",
                # «не принимаю платежи от 3-х лиц» (слово-вставка между принимаю и от)
                "Принимаю на доверенное лицо. Не принимаю платежи от 3-х лиц",
                "не принимаю платежи от 3-х лиц", "не принимаю деньги от 3 лиц"):
        assert p(txt)["third_party"] is False, txt
    # «за 3 дня» / «с 3 до 18» / «от 1 000» / «от 1 года» — НЕ про третьих лиц
    for txt in ("за 3 дня отвечу", "работаю с 3 до 18 не ночью",
                "от 1 000 тенге", "от 1 года на рынке"):
        assert p(txt)["third_party"] is None, txt
    # принимает (вкл. «доверенные лица» / «родственники» — кандидаты для связки)
    for txt in ("принимаю от третьих лиц", "3 лица ок",
                "третьи лица допускаю", "от третьих лиц можно",
                "оплачиваю с карт родственников", "от доверенного лица",
                "дов лица принимаю",
                # «с любого банка / любой картой» → приём от 3-х лиц
                "С любого банка 1/3 если на руках чек обязательно",
                "Приму с любого банка Кз по номеру карты", "принимаю любой картой",
                # «1/3» = без разницы 1-е или 3-е лицо → принимает третьих
                "1/3 на дов лицо, чек обязательно", "1 или 3 лицо без разницы",
                "1 и 3 принимаю"):
        assert p(txt)["third_party"] is True, txt
    # «любой банк» НЕ перебивает явный запрет (NEG в приоритете)
    assert p("с любого банка но только свои карты")["third_party"] is False
    assert p("с любого банка, только от 1 го")["third_party"] is False
    # не упомянуто
    for txt in ("оплата картой сбербанк", "перевод ровно 5000", ""):
        assert p(txt)["third_party"] is None, txt

def test_desc_parser_scam_recruit():
    """Детектор вербовки/скама в описании объявления."""
    from utils.desc_parser import parse_description as p
    # реальный скам-пример
    s = p("Рубим капусту внутри биржы 7% с круга в 15 минут teleg: werezov")
    assert s["scam_recruit"] is True
    assert any("ВЕРБОВКА" in f for f in s["flags"])
    # другие вербовочные
    for txt in ("ищу людей для заработка пиши в тг", "обучу схеме заработка",
                "@scam_bot пиши", "обучение в тг", "все вопросы в тг",
                "научу наставник", "пиши в личку"):
        assert p(txt)["scam_recruit"] is True, txt
    # развод с отложенной оплатой («оплачу завтра» = отпусти крипту до оплаты)
    for txt in ("оплата будет завтра сегодня выходной", "оплачу позже",
                "деньги будут завтра", "кинь первым", "отпусти первым",
                "переведу вечером"):
        assert p(txt)["scam_recruit"] is True, txt
    # мгновенная оплата — НЕ развод
    for txt in ("оплата сразу", "оплачу быстро онлайн", "оплата моментально"):
        assert p(txt)["scam_recruit"] is False, txt
    # обход фильтра латиницей (гомоглифы): «Pyбuм кaпyсту» — P,y,u латинские
    for txt in ("Pyбuм кaпyсту 7% с кpyгa tеlеg: werezov",
                "Ai-торги с обучением, связки без пластика tелеg: x"):
        assert p(txt)["scam_recruit"] is True, txt
    # легитимные — чисто (латиница и кириллица в РАЗНЫХ словах — это норма)
    for txt in ("работаю строго с 1 лицами", "Kaspi Bank оплата сразу",
                "принимаю от третьих лиц", "VISA / МИР принимаю",
                "Tinkoff Sberbank USDT", ""):
        assert p(txt)["scam_recruit"] is False, txt

def test_enrich_sets_scam_recruit():
    import webapp.server as srv
    ads = [{"description": "рубим капусту 7% с круга teleg: x", "completion": 0, "pay_types": []}]
    out = srv._enrich(ads)
    assert out[0]["scam_recruit"] is True

def test_desc_parser_trap_and_exact_amount():
    """Ловушка для спора + фикс ложного «ровно 1»."""
    from utils.desc_parser import parse_description as p
    ad = ('Принимаю только 1 ПЛАТЕЖОМ. После оплаты чек на почту от имени банка. '
          'Пишите: "отправлю и со всем согласен"')
    r = p(ad)
    assert r["trap"] is True               # фраза-согласие + чек на почту
    assert r["exact_amount"] is None       # «только 1» — это НЕ сумма (не «ровно 1»)
    # реальная сумма ≥100 всё ещё ловится
    assert p("оплата ровно 5000 руб")["exact_amount"] == 5000
    # легитимные — без ловушки
    for txt in ("Kaspi оплата сразу", "работаю строго с 1 лицами", ""):
        assert p(txt)["trap"] is False, txt
    # цена-приманка «строго по заявкам / без заявки другое объявление» = ловушка
    bait = p("Ордер СТРОГО по заявкам!\nБез заявки другое объявление в профиле.")
    assert bait["trap"] is True
    assert any("приманка" in f for f in bait["flags"])
    assert p("по предварительной заявке")["trap"] is True
    # ложных срабатываний на «заявку» нет
    assert p("заявка на возврат не принимается")["trap"] is False
    # турецкий: «не принимаю от 3-х лиц» + просят фразу в назначении платежа
    tr = p('Üçüncü şahıslardan kabul etmiyorum. Ödeme açıklama kısmına "Dijital '
            'varlık satış ödemesi" yazınız')
    assert tr["third_party"] is False and tr["trap"] is True
    assert p("Üçüncü şahıslardan ödeme kabul etmiyorum")["third_party"] is False
    assert p("I do not accept third party payments")["third_party"] is False
    assert p("Tidak menerima pihak ketiga")["third_party"] is False
    # «açıklama yazmayın» (НЕ писать) — не ловушка
    assert p("Lütfen açıklama yazmayın")["trap"] is False


def test_enrich_sets_third_party():
    """_enrich парсит описание и проставляет third_party на объявление."""
    import webapp.server as srv
    ads = [
        {"description": "не принимаю от третьих лиц", "completion": 0, "pay_types": []},
        {"description": "принимаю от 3 лиц ок",        "completion": 0, "pay_types": []},
        {"description": "просто оплата",               "completion": 0, "pay_types": []},
    ]
    out = srv._enrich(ads)
    assert out[0]["third_party"] is False
    assert out[1]["third_party"] is True
    assert out[2]["third_party"] is None


def test_orderbook_amount_filter():
    """Фильтр суммы: показывает только объявления, где сумму реально сделать."""
    import webapp.server as srv
    # объявление с минималкой 200k — нельзя сделать 500
    big = {"min_amount": 200_000, "max_amount": 5_000_000}
    assert srv._tradeable_at(big, 500) is False        # 500 < min 200k → скрыть
    # обычное объявление 1k..50k
    normal = {"min_amount": 1_000, "max_amount": 50_000}
    assert srv._tradeable_at(normal, 5_000) is True    # 5000 в диапазоне → показать
    assert srv._tradeable_at(normal, 500) is False     # 500 < min 1k → скрыть
    assert srv._tradeable_at(normal, 99_000) is False  # больше max → скрыть
    # нет данных о максимуме — проверяем только нижнюю границу
    no_max = {"min_amount": 1_000, "max_amount": 0}
    assert srv._tradeable_at(no_max, 5_000) is True
    assert srv._tradeable_at(no_max, 500) is False


def test_disabled_exchange_returns_empty_fast():
    """get_ads мгновенно возвращает [] когда биржа в DISABLED_EXCHANGES."""
    import asyncio
    import config
    from api import okx_p2p
    config.DISABLED_EXCHANGES.add("okx")          # временно отключаем
    try:
        ads = asyncio.run(okx_p2p.get_ads(asset="USDT", fiat="KZT", side="buy"))
        assert ads == []
    finally:
        config.DISABLED_EXCHANGES.discard("okx")  # возвращаем как было
