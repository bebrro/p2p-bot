"""
План-зависимые лимиты + апселл при их достижении.

Идея: когда пользователь упирается в лимит Free-плана (3 алерта,
3 трекера, 1 правило репрайсера) — вместо сухого "лимит достигнут"
показываем мотивирующее сообщение с кнопкой апгрейда. Это самый
прямой путь к конверсии: человек уже хочет фичу прямо сейчас.

Лимиты берутся из utils.subscription.PLANS по ключам:
  "alerts" · "trackers" · "repricer_rules"
"""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import db
from utils.subscription import PLANS, get_plan_key

# Человекочитаемые названия фич (для текста апселла)
_FEATURE_LABELS = {
    "alerts":         ("алерт",         "алертов"),
    "trackers":       ("трекер",        "трекеров"),
    "repricer_rules": ("правило",       "правил репрайсера"),
}


async def get_limit(uid: int, feature: str) -> tuple[int, str]:
    """
    Возвращает (лимит_для_фичи, plan_key) с учётом активной подписки.
    Без БД (локалка) — лимит Free.
    """
    sub      = await db.subscription_get(uid) if db.ok() else None
    plan_key = get_plan_key(sub)
    limit    = int(PLANS.get(plan_key, PLANS["free"]).get(feature, 0))
    return limit, plan_key


async def check_allowed(uid: int, feature: str, current_count: int) -> tuple[bool, int, str]:
    """
    Можно ли добавить ещё одну сущность данной фичи.
    Возвращает (allowed, limit, plan_key).
    """
    limit, plan_key = await get_limit(uid, feature)
    return current_count < limit, limit, plan_key


def _plural(n: int, feature: str) -> str:
    one, many = _FEATURE_LABELS.get(feature, ("шт", "шт"))
    return one if n == 1 else many


def upsell_text(feature: str, current: int, limit: int, plan_key: str) -> str:
    """Мотивирующий текст при упоре в лимит. plan_key — текущий план юзера."""
    pro = PLANS["pro"]
    noun = _plural(limit, feature)

    # Что получит при апгрейде до Pro
    perks = (
        f"  • {pro['alerts']} алертов · {pro['trackers']} трекеров\n"
        f"  • Авто-репрайсер ({pro['repricer_rules']} правил)\n"
        f"  • Smart Арбитраж — 4 биржи\n"
        f"  • P&L трекер · Экспорт данных"
    )

    return (
        f"🔒 <b>Лимит достигнут — {current}/{limit} {noun}</b>\n\n"
        f"На плане <b>{PLANS[plan_key]['emoji']} {PLANS[plan_key]['name']}</b> "
        f"доступно {limit} {noun}.\n\n"
        f"⭐ <b>С Pro открывается:</b>\n"
        f"{perks}\n\n"
        f"💰 Pro — <b>{pro['price_usdt']:.2f}$/мес</b> "
        f"или <b>{pro['price_lifetime']:.0f}$ навсегда</b> 🔥"
    )


def upsell_kb(manage_cb: str | None = None, manage_text: str = "🗑 Управлять") -> InlineKeyboardMarkup:
    """
    Клавиатура апселла: кнопка апгрейда + (опц.) управление существующими.
    manage_cb — callback для возврата к списку (чтобы удалить лишнее).
    """
    rows = [[InlineKeyboardButton(text="⭐ Получить Pro", callback_data="sub:list")]]
    if manage_cb:
        rows.append([InlineKeyboardButton(text=manage_text, callback_data=manage_cb)])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
