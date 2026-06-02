"""
Планы подписки, лимиты и вспомогательные утилиты.
Free → Pro → Team.
"""
from datetime import datetime, timezone
from typing import Literal

PlanType = Literal["free", "pro", "team"]


def _as_aware(dt):
    """Приводит datetime/ISO-строку к tz-aware (наивный считаем UTC). None → None."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return None
    if isinstance(dt, datetime) and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

PLANS: dict[str, dict] = {
    "free": {
        "name":          "Free",
        "emoji":         "🆓",
        "tagline":       "Разведка рынка",
        "repricer_rules": 1,
        "trackers":       3,
        "alerts":         3,
        "arbitrage_4x":   False,
        "pnl":            False,
        "export":         False,
        "ai_daily":       5,
        "features": [
            "Курсы 4 бирж + стакан с защитой от приманок",
            "Связки без карт (просмотр)",
            "Антискам: проверка ника + база 11 500 кидал",
            "AI-ассистент — 5 вопросов в день",
            "3 алерта · 3 трекера",
        ],
    },
    "pro": {
        "name":             "Pro",
        "emoji":            "⭐",
        "tagline":          "Для трейдера — окупается с одной сделки",
        "price_usdt":       9.99,
        "price_lifetime":   59.0,
        "price_stars":      650,    # ≈ +30% к USDT (покрывает комиссию Apple/Google)
        "price_stars_life": 3800,
        "duration_days":    30,
        "repricer_rules":   10,
        "trackers":         20,
        "alerts":           20,
        "arbitrage_4x":     True,
        "pnl":              True,
        "export":           True,
        "ai_daily":         -1,     # безлимит
        "features": [
            "♾ Безлимитный AI-ассистент по сделкам",
            "🤖 Авто-репрайсер — держит твои объявления в топе 24/7",
            "🔔 Алерты на связки и арбитраж прямо в ЛС",
            "🐋 Whale-трекер — видишь крупных игроков",
            "🧾 AI-проверка чеков на подделку",
            "📈 P&L-аналитика + 📤 экспорт сделок",
            "20 алертов · 20 трекеров · 10 правил репрайсера",
        ],
    },
    "team": {
        "name":             "Team",
        "emoji":            "👑",
        "tagline":          "Для P2P-магазина и мульти-аккаунтов",
        "price_usdt":       24.99,
        "price_lifetime":   149.0,
        "price_stars":      1600,
        "price_stars_life": 9700,
        "duration_days":    30,
        "repricer_rules":   50,
        "trackers":         100,
        "alerts":           100,
        "arbitrage_4x":     True,
        "pnl":              True,
        "export":           True,
        "ai_daily":         -1,
        "features": [
            "Всё из Pro, без ограничений",
            "👥 До 5 API-аккаунтов (мульти-аккаунт)",
            "50 правил репрайсера · 100 алертов · 100 трекеров",
            "⚡ Приоритетная поддержка",
        ],
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
    expires_at = _as_aware(user_sub.get("expires_at"))
    if expires_at is None:
        # None = бессрочная (выдана вручную); строка-мусор → _as_aware вернёт None
        return plan if user_sub.get("expires_at") in (None, "") else "free"
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
    if not user_sub.get("expires_at"):
        return "∞ навсегда"
    expires_at = _as_aware(user_sub.get("expires_at"))
    if expires_at is None:
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
    """HTML-блок описания плана для Telegram — подача через выгоды."""
    p    = PLANS.get(plan_key, PLANS["free"])
    star = " ⭐ ПОПУЛЯРНЫЙ" if plan_key == "pro" else ""
    mark = "  ✅ твой план" if is_current else ""
    lines = [f"{p['emoji']} <b>{p['name']}</b> — <i>{p.get('tagline','')}</i>{star}{mark}"]
    if plan_key == "free":
        lines.append("💰 <b>Бесплатно</b>")
    else:
        lines.append(
            f"💰 <b>{p['price_lifetime']:.0f} USDT навсегда</b> 🔥  "
            f"или {p['price_usdt']:.2f}/мес"
        )
        if p.get("price_stars"):
            lines.append(f"⭐ можно Telegram Stars — оплата в 1 тап")
    for f in p.get("features", []):
        lines.append(f"  ✅ {f}")
    return "\n".join(lines)
