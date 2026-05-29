from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from config import FIATS, ASSETS, FIAT_FLAGS, WEBAPP_URL

# Shorthand
_B = InlineKeyboardButton

SORT_LABELS = {
    "price":      "💰 Цена",
    "completion": "✅ % Сделок",
    "volume":     "📦 Объём",
}


# ══════════════════════════════════════════════════════════════════════════════
#  ГЛАВНОЕ МЕНЮ (4 ряда вместо 11 — категории + биржи)
# ══════════════════════════════════════════════════════════════════════════════

def main_menu() -> InlineKeyboardMarkup:
    rows = []

    # Mini App — только если задан URL
    if WEBAPP_URL:
        rows.append([_B(text="🌐 Открыть Mini App", web_app=WebAppInfo(url=WEBAPP_URL))])

    rows += [
        # Главная CTA — найти лучший курс
        [_B(text="🔍 Найти лучший курс → Арбитраж", callback_data="spread:compare")],
        # Биржи — для просмотра конкретной биржи
        [
            _B(text="🟡 Binance", callback_data="exchange:binance"),
            _B(text="🟠 Bybit",   callback_data="exchange:bybit"),
            _B(text="🔵 OKX",    callback_data="exchange:okx"),
            _B(text="💎 Wallet", callback_data="exchange:wallet"),
        ],
        # Категории функций
        [
            _B(text="🔔 Алерты на спред",   callback_data="menu:alerts"),
            _B(text="🤖 Авто-торговля ▸",   callback_data="menu:auto"),
        ],
        [
            _B(text="📈 Аналитика ▸",       callback_data="menu:analytics"),
            _B(text="👤 Мой кабинет ▸",     callback_data="menu:account"),
        ],
        # Быстрый доступ
        [
            _B(text="⭐ Подписка", callback_data="sub:list"),
            _B(text="📊 P&L",     callback_data="pnl:view"),
            _B(text="🤖 AI",      callback_data="ai:start"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ══════════════════════════════════════════════════════════════════════════════
#  ПОДМЕНЮ КАТЕГОРИЙ
# ══════════════════════════════════════════════════════════════════════════════

def alerts_submenu() -> InlineKeyboardMarkup:
    """Алерты и сигналы."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            _B(text="🔔 Алерты",   callback_data="alerts:list"),
            _B(text="🔍 Арбитраж", callback_data="arb:list"),
        ],
        [
            _B(text="📊 Спред",    callback_data="spread:compare"),
            _B(text="📈 История",  callback_data="hist:binance:KZT:USDT"),
        ],
        [
            _B(text="📍 Сигналы",   callback_data="sig:start"),
            _B(text="🔀 Мультипара", callback_data="mp:view"),
        ],
        [_B(text="⬅️ Главное меню", callback_data="back:main")],
    ])


def auto_submenu() -> InlineKeyboardMarkup:
    """Авто-режимы."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            _B(text="🎯 Мейкер",    callback_data="maker:start"),
            _B(text="👁 Трекер",   callback_data="tracker:list"),
        ],
        [
            _B(text="📡 Позиция",   callback_data="mon:list"),
            _B(text="🔄 Авто-цена", callback_data="rp:list"),
        ],
        [
            _B(text="⏰ Расписание", callback_data="sch:list"),
            _B(text="🧮 Калькулятор", callback_data="calc:start"),
        ],
        [_B(text="⬅️ Главное меню", callback_data="back:main")],
    ])


def analytics_submenu() -> InlineKeyboardMarkup:
    """Аналитика."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            _B(text="🐋 Киты",       callback_data="wt:list"),
            _B(text="🧠 Паттерны",   callback_data="pat:start"),
        ],
        [
            _B(text="📊 Статистика", callback_data="stats:view"),
            _B(text="📊 P&L",        callback_data="pnl:view"),
        ],
        [_B(text="⬅️ Главное меню", callback_data="back:main")],
    ])


def account_submenu() -> InlineKeyboardMarkup:
    """Личный кабинет."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            _B(text="🔑 Аккаунты",  callback_data="acc:list"),
            _B(text="👥 Рефералы",  callback_data="ref:show"),
        ],
        [
            _B(text="🚫 Блок-список", callback_data="bl:list"),
            _B(text="📥 Экспорт",     callback_data="export:start"),
        ],
        [_B(text="⬅️ Главное меню", callback_data="back:main")],
    ])


# ══════════════════════════════════════════════════════════════════════════════
#  P2P НАВИГАЦИЯ (биржи → фиат → ассет → тип)
# ══════════════════════════════════════════════════════════════════════════════

def fiat_menu(exchange: str) -> InlineKeyboardMarkup:
    """Шаг 1: выбор фиатной валюты."""
    buttons = [
        [_B(text=FIAT_FLAGS.get(f, f), callback_data=f"asset:{exchange}:{f}")]
        for f in FIATS
    ]
    buttons.append([_B(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def asset_menu(exchange: str, fiat: str) -> InlineKeyboardMarkup:
    """Шаг 2: выбор ассета (USDT / BTC / ETH)."""
    asset_buttons = [
        _B(text=a, callback_data=f"ads:{exchange}:{fiat}:{a}")
        for a in ASSETS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        asset_buttons,
        [_B(text="⬅️ Назад", callback_data=f"exchange:{exchange}")],
    ])


def trade_type_menu(exchange: str, fiat: str, asset: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            _B(text="📗 Купить",  callback_data=f"ads:{exchange}:{fiat}:{asset}:buy:price"),
            _B(text="📕 Продать", callback_data=f"ads:{exchange}:{fiat}:{asset}:sell:price"),
        ],
        [_B(text="📊 Двойной стакан", callback_data=f"book:{exchange}:{fiat}:{asset}:price")],
        [_B(text="⬅️ Назад",          callback_data=f"asset:{exchange}:{fiat}")],
    ])


def sort_buttons(callback_prefix: str, current_sort: str) -> list:
    row = []
    for key, label in SORT_LABELS.items():
        text = f"• {label}" if key == current_sort else label
        row.append(_B(text=text, callback_data=f"{callback_prefix}:{key}"))
    return [row]


def ads_list_menu(exchange: str, fiat: str, asset: str, trade_type: str, sort: str, ads: list,
                  user_id: int = 0) -> InlineKeyboardMarkup:
    from handlers.filters import get_filter
    f  = get_filter(user_id) if user_id else {}
    tp = f.get("third_party")
    rep_orders = f.get("min_orders", 0)
    rep_comp   = f.get("min_completion", 0)

    prefix  = f"ads:{exchange}:{fiat}:{asset}:{trade_type}"
    tp_back = f"{exchange}:{fiat}:{asset}:{trade_type}:{sort}"
    buttons = sort_buttons(f"sort_ads:{exchange}:{fiat}:{asset}:{trade_type}", sort)

    buttons.append([
        _B(text="🔍 Метод оплаты",
           callback_data=f"filterpay:{exchange}:{fiat}:{asset}:{trade_type}:{sort}"),
        _B(text="💰 Мин. сумма",
           callback_data=f"filteramt:{exchange}:{fiat}:{asset}:{trade_type}:{sort}"),
    ])

    rep_text = f"🏅 ≥{rep_orders}сд/{rep_comp}% ●" if (rep_orders or rep_comp) else "🏅 Репутация"
    tp_btn_text = (
        "✅ 3-е лица  ●" if tp == "yes" else
        "❌ без 3-х лиц  ●" if tp == "no" else
        "👥 3-е лица"
    )
    buttons.append([
        _B(text=rep_text,     callback_data=f"filterrep:{tp_back}"),
        _B(text=tp_btn_text,  callback_data=f"filterthirdparty:{tp_back}"),
    ])

    buttons.append([_B(text="✖️ Сбросить фильтры", callback_data=f"filterclear:{prefix}:{sort}")])

    for i, ad in enumerate(ads):
        buttons.append([
            _B(text=f"📋 Описание #{i + 1}", callback_data=f"copy:{exchange}:{i}"),
            _B(text="🚫 Бан",                callback_data=f"ban:{exchange}:{i}"),
        ])

    buttons.append([
        _B(text="🔄 Обновить", callback_data=f"{prefix}:{sort}"),
        _B(text="⬅️ Назад",    callback_data=f"ads:{exchange}:{fiat}:{asset}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def book_menu(exchange: str, fiat: str, asset: str, sort: str) -> InlineKeyboardMarkup:
    prefix  = f"sort_book:{exchange}:{fiat}:{asset}"
    buttons = sort_buttons(prefix, sort)
    buttons.append([
        _B(text="🔄 Обновить", callback_data=f"book:{exchange}:{fiat}:{asset}:{sort}"),
        _B(text="⬅️ Назад",    callback_data=f"ads:{exchange}:{fiat}:{asset}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def spread_fiat_menu() -> InlineKeyboardMarkup:
    buttons = [
        [_B(text=FIAT_FLAGS.get(f, f), callback_data=f"spread:{f}:USDT")]
        for f in FIATS
    ]
    buttons.append([_B(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def alert_menu(fiat: str = "KZT") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_B(text="➕ Добавить алерт", callback_data=f"alert:add:{fiat}")],
        [_B(text="🗑 Удалить все",     callback_data="alert:clear")],
        [_B(text="⬅️ Назад",           callback_data="back:main")],
    ])
