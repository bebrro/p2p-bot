"""
Планы подписки, лимиты и вспомогательные утилиты.
Free → Pro → Team.
"""
from datetime import datetime, timezone
from typing import Literal

PlanType = Literal["free", "pro", "team"]

PLANS: dict[str, dict] = {
    "free": {
        "name":          "Free",
        "emoji":         "🆓",
        "repricer_rules": 1,
        "trackers":       3,
        "alerts":         3,
        "arbitrage_4x":   False,
        "pnl":            False,
        "export":         False,
    },
    "pro": {
        "name":            "Pro",
        "emoji":           "⭐",
        "price_stars":     500,
        "duration_days":   30,
        "repricer_rules":  10,
        "trackers":        20,
        "alerts":          20,
        "arbitrage_4x":    True,
        "pnl":             True,
        "export":          True,
    },
    "team": {
        "name":            "Team",
        "emoji":           "👑",
        "price_stars":     1500,
        "duration_days":   30,
        "repricer_rules":  50,
        "trackers":        100,
        "alerts":          100,
        "arbitrage_4x":    True,
        "pnl":             True,
        "export":          True,
    },
}


# ─── Helpers ───────────────────────────────────────────────────────────────────

def get_plan_key(user_sub: dict | None) -> str:
    """Возвращает актуальный ключ плана с учётом срока действия."""
    if not user_sub:
        return "free"
    plan = user_sub.get("plan", "free")
    if plan == "free":
        return "free"
    expires_at = user_sub.get("expires_at")
    if expires_at is None:
        return plan   # бессрочная (выдана вручную без даты)
    # expires_at может быть datetime или строкой ISO
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except Exception:
            return "free"
    if expires_at < datetime.now(timezone.utc):
        return "free"   # истекла
    return plan


def is_pro(user_sub: dict | None) -> bool:
    return get_plan_key(user_sub) != "free"


def get_limits(plan_key: str) -> dict:
    return PLANS.get(plan_key, PLANS["free"])


def format_expires(user_sub: dict | None) -> str:
    """Человекочитаемый остаток подписки."""
    if not user_sub or user_sub.get("plan", "free") == "free":
        return ""
    expires_at = user_sub.get("expires_at")
    if not expires_at:
        return "∞ навсегда"
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except Exception:
            return ""
    now   = datetime.now(timezone.utc)
    delta = expires_at - now
    if delta.total_seconds() < 0:
        return "❌ истёк"
    days = delta.days
    if days == 0:
        hours = delta.seconds // 3600
        return f"⏰ ещё {hours}ч"
    return f"⏰ ещё {days}д (до {expires_at.strftime('%d.%m.%Y')})"


def format_plan_card(plan_key: str, is_current: bool = False) -> str:
    """HTML-блок описания плана для Telegram."""
    p    = PLANS.get(plan_key, PLANS["free"])
    mark = " ✅ (текущий)" if is_current else ""
    lines = [f"{p['emoji']} <b>{p['name']}</b>{mark}"]
    if plan_key == "free":
        lines.append("💰 Бесплатно")
    else:
        lines.append(f"💰 {p['price_stars']} ⭐ / {p['duration_days']} дней")
    lines += [
        f"  • Репрайсер: до {p['repricer_rules']} правил",
        f"  • Трекеры: до {p['trackers']}",
        f"  • Алерты: до {p['alerts']}",
        f"  • 4-биржевой арбитраж: {'✅' if p['arbitrage_4x'] else '❌'}",
        f"  • P&L трекер: {'✅' if p['pnl'] else '❌'}",
        f"  • Экспорт данных: {'✅' if p['export'] else '❌'}",
    ]
    return "\n".join(lines)
