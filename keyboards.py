from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from config import FIATS, ASSETS, FIAT_FLAGS, WEBAPP_URL

SORT_LABELS = {
    "price": "💰 Цена",
    "completion": "✅ % Сделок",
    "volume": "📦 Объём",
}


def main_menu() -> InlineKeyboardMarkup:
    # Mini App кнопка — показываем только если URL задан
    webapp_row = []
    if WEBAPP_URL:
        webapp_row = [InlineKeyboardButton(
            text="🌐 Открыть Mini App",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )]

    rows = []
    if webapp_row:
        rows.append(webapp_row)

    rows += [
        [
            InlineKeyboardButton(text="🟡 Binance P2P", callback_data="exchange:binance"),
            InlineKeyboardButton(text="🟠 Bybit P2P",   callback_data="exchange:bybit"),
        ],
        [
            InlineKeyboardButton(text="🔵 OKX P2P",    callback_data="exchange:okx"),
            InlineKeyboardButton(text="💎 TG Wallet",  callback_data="exchange:wallet"),
        ],
        [
            InlineKeyboardButton(text="🔀 Мультипара", callback_data="mp:view"),
            InlineKeyboardButton(text="🔍 Арбитраж",   callback_data="arb:list"),
        ],
        [
            InlineKeyboardButton(text="🎯 Мейкер",   callback_data="maker:start"),
            InlineKeyboardButton(text="👁 Трекер",   callback_data="tracker:list"),
            InlineKeyboardButton(text="📡 Позиция",  callback_data="mon:list"),
        ],
        [
            InlineKeyboardButton(text="📍 Сигнал цены", callback_data="sig:start"),
            InlineKeyboardButton(text="🧮 Калькулятор", callback_data="calc:start"),
        ],
        [
            InlineKeyboardButton(text="📊 Спред",   callback_data="spread:compare"),
            InlineKeyboardButton(text="🔔 Алерты",  callback_data="alerts:list"),
            InlineKeyboardButton(text="📈 История", callback_data="hist:binance:KZT:USDT"),
        ],
        [
            InlineKeyboardButton(text="🔑 Аккаунты",   callback_data="acc:list"),
            InlineKeyboardButton(text="🔄 Авто-цена",  callback_data="rp:list"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats:view"),
        ],
        [InlineKeyboardButton(text="🤖 AI Советник",   callback_data="ai:start")],
        [
            InlineKeyboardButton(text="🐋 Киты",        callback_data="wt:list"),
            InlineKeyboardButton(text="🧠 Паттерны",    callback_data="pat:start"),
        ],
        [
            InlineKeyboardButton(text="🚫 Блок.список", callback_data="bl:list"),
            InlineKeyboardButton(text="📥 Экспорт",     callback_data="export:start"),
            InlineKeyboardButton(text="⏰ Расписание",  callback_data="sch:list"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fiat_menu(exchange: str) -> InlineKeyboardMarkup:
    """Шаг 1: выбор фиатной валюты → ведёт к выбору ассета."""
    buttons = [
        [InlineKeyboardButton(
            text=FIAT_FLAGS.get(f, f),
            callback_data=f"asset:{exchange}:{f}",
        )]
        for f in FIATS
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def asset_menu(exchange: str, fiat: str) -> InlineKeyboardMarkup:
    """Шаг 2: выбор ассета (USDT / BTC / ETH) → ведёт к выбору типа сделки."""
    asset_buttons = [
        InlineKeyboardButton(text=a, callback_data=f"ads:{exchange}:{fiat}:{a}")
        for a in ASSETS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        asset_buttons,
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"exchange:{exchange}")],
    ])


def trade_type_menu(exchange: str, fiat: str, asset: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📗 Купить", callback_data=f"ads:{exchange}:{fiat}:{asset}:buy:price"),
            InlineKeyboardButton(text="📕 Продать", callback_data=f"ads:{exchange}:{fiat}:{asset}:sell:price"),
        ],
        [InlineKeyboardButton(text="📊 Двойной стакан", callback_data=f"book:{exchange}:{fiat}:{asset}:price")],
        # Назад → выбор ассета (не биржа)
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"asset:{exchange}:{fiat}")],
    ])


def sort_buttons(callback_prefix: str, current_sort: str) -> list:
    row = []
    for key, label in SORT_LABELS.items():
        text = f"• {label}" if key == current_sort else label
        row.append(InlineKeyboardButton(text=text, callback_data=f"{callback_prefix}:{key}"))
    return [row]


def ads_list_menu(exchange: str, fiat: str, asset: str, trade_type: str, sort: str, ads: list,
                  user_id: int = 0) -> InlineKeyboardMarkup:
    from handlers.filters import get_filter
    f = get_filter(user_id) if user_id else {}
    tp         = f.get("third_party")
    rep_orders = f.get("min_orders", 0)
    rep_comp   = f.get("min_completion", 0)

    prefix  = f"ads:{exchange}:{fiat}:{asset}:{trade_type}"
    tp_back = f"{exchange}:{fiat}:{asset}:{trade_type}:{sort}"
    buttons = sort_buttons(f"sort_ads:{exchange}:{fiat}:{asset}:{trade_type}", sort)

    # Строка 1: платёжный метод + сумма
    buttons.append([
        InlineKeyboardButton(
            text="🔍 Метод оплаты",
            callback_data=f"filterpay:{exchange}:{fiat}:{asset}:{trade_type}:{sort}",
        ),
        InlineKeyboardButton(
            text="💰 Мин. сумма",
            callback_data=f"filteramt:{exchange}:{fiat}:{asset}:{trade_type}:{sort}",
        ),
    ])

    # Строка 2: репутация + 3-е лица
    rep_text = f"🏅 ≥{rep_orders}сд/{rep_comp}% ●" if (rep_orders or rep_comp) else "🏅 Репутация"
    if tp == "yes":
        tp_btn_text = "✅ 3-е лица  ●"
    elif tp == "no":
        tp_btn_text = "❌ без 3-х лиц  ●"
    else:
        tp_btn_text = "👥 3-е лица"
    buttons.append([
        InlineKeyboardButton(text=rep_text,     callback_data=f"filterrep:{tp_back}"),
        InlineKeyboardButton(text=tp_btn_text,  callback_data=f"filterthirdparty:{tp_back}"),
    ])

    # Строка 3: сброс фильтров
    buttons.append([
        InlineKeyboardButton(
            text="✖️ Сбросить фильтры",
            callback_data=f"filterclear:{prefix}:{sort}",
        ),
    ])

    for i, ad in enumerate(ads):
        buttons.append([
            InlineKeyboardButton(
                text=f"📋 Описание #{i + 1}",
                callback_data=f"copy:{exchange}:{i}",
            ),
            InlineKeyboardButton(
                text="🚫 Бан",
                callback_data=f"ban:{exchange}:{i}",
            ),
        ])

    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"{prefix}:{sort}"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ads:{exchange}:{fiat}:{asset}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def book_menu(exchange: str, fiat: str, asset: str, sort: str) -> InlineKeyboardMarkup:
    prefix = f"sort_book:{exchange}:{fiat}:{asset}"
    buttons = sort_buttons(prefix, sort)
    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"book:{exchange}:{fiat}:{asset}:{sort}"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ads:{exchange}:{fiat}:{asset}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def spread_fiat_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=FIAT_FLAGS.get(f, f),
            callback_data=f"spread:{f}:USDT",
        )]
        for f in FIATS
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def alert_menu(fiat: str = "KZT") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить алерт", callback_data=f"alert:add:{fiat}")],
        [InlineKeyboardButton(text="🗑 Удалить все", callback_data="alert:clear")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")],
    ])
