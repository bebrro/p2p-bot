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
