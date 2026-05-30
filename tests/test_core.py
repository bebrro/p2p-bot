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

def test_lifetime_no_expiry():
    """Lifetime подписка: expires_at = None → активна навсегда."""
    lifetime = {"plan": "pro", "expires_at": None}
    assert get_plan_key(lifetime) == "pro"

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
    assert "P2P Panel Bot" in txt   # MAIN_TEXT приклеен


# ─── Реферал в главном меню (task 7) ──────────────────────────────────────────

def test_main_menu_has_referral():
    kb  = main_menu()
    cbs = {b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data}
    assert "ref:show" in cbs


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

def test_triangle_requires_third_party():
    """Объявления, ЯВНО не принимающие 3-х лиц, исключаются из связки."""
    import webapp.server as srv
    # покупка USDT (asks) — дешёвый ad запрещает 3-х лиц → не годится
    buy = [
        {"price": 500, "third_party": False, "nickname": "no3p", "min_amount": 0, "max_amount": 9e9, "pay_types": []},
        {"price": 505, "third_party": True,  "nickname": "ok3p", "min_amount": 0, "max_amount": 9e9, "pay_types": []},
    ]
    # продажа USDT (bids)
    sell = [
        {"price": 515, "third_party": None, "nickname": "may", "min_amount": 0, "max_amount": 9e9, "pay_types": []},
    ]
    t = srv._triangle(buy, sell, "KZT")
    assert t is not None
    assert t["buy"]["price"] == 505      # 500 отброшен (запретил 3-х лиц)
    assert t["sell"]["price"] == 515
    assert t["buy"]["confirm3p"] is True
    assert t["pct"] > 0
    assert t["profit"] > 0

def test_triangle_none_without_eligible_legs():
    import webapp.server as srv
    buy  = [{"price": 500, "third_party": False, "nickname": "x", "min_amount": 0, "max_amount": 9e9, "pay_types": []}]
    sell = [{"price": 515, "third_party": None,  "nickname": "y", "min_amount": 0, "max_amount": 9e9, "pay_types": []}]
    # покупка целиком запрещает 3-х лиц → связки нет
    assert srv._triangle(buy, sell, "KZT") is None

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
    # НЕ принимает
    for txt in ("не принимаю от третьих лиц", "третьи лица не принимаю",
                "без третьих лиц", "только свои переводы", "3 лица не принимаю"):
        assert p(txt)["third_party"] is False, txt
    # принимает
    for txt in ("принимаю от третьих лиц", "3 лица ок",
                "третьи лица допускаю", "от третьих лиц можно"):
        assert p(txt)["third_party"] is True, txt
    # не упомянуто
    for txt in ("оплата картой сбербанк", "перевод ровно 5000", ""):
        assert p(txt)["third_party"] is None, txt

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
