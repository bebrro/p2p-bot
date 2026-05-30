import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")  # Fernet key для шифрования API ключей
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")  # https://aistudio.google.com/app/apikey
WEBAPP_URL     = os.getenv("WEBAPP_URL",     "")  # HTTPS URL Mini App (ngrok / VPS)
WEBAPP_PORT    = int(os.getenv("PORT", os.getenv("WEBAPP_PORT", "8765")))
# Comma-separated admin Telegram user IDs for /give_pro command
ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]

# USDT TRC20 wallet address for receiving subscription payments
CRYPTO_WALLET_TRC20 = os.getenv("CRYPTO_WALLET_TRC20", "")

# Admin Telegram username (без @) — для кнопки "Написать администратору"
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")

# Admin Telegram chat_id — для Telegram-алертов об ошибках.
# Узнать: отправь боту /start, бот пришлёт твой ID (если ADMIN_USERNAME совпадает).
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

# Redis URL для FSM-хранилища (состояния форм не теряются при рестарте).
# Если не задан — используется MemoryStorage (для разработки).
# Docker: redis://redis:6379  |  Railway: берётся из REDIS_URL автоматически
REDIS_URL = os.getenv("REDIS_URL", "")

# TG Wallet P2P API key (получить: wallet.tg → P2P → Настройки → API)
WALLET_P2P_API_KEY = os.getenv("WALLET_P2P_API_KEY", "")

# Временно отключённые биржи (мёртвый/заблокированный API).
# По умолчанию — НИ ОДНА (все 4 работают с Railway: проверено через /health).
# Если какая-то биржа начнёт таймаутить — отключи через Railway:
#   DISABLED_EXCHANGES="okx"  (или несколько через запятую)
DISABLED_EXCHANGES = {
    x.strip().lower()
    for x in os.getenv("DISABLED_EXCHANGES", "").split(",")
    if x.strip()
}

# Спред выше этого % помечается как подозрительный («проверь ликвидность»).
# На тонких рынках одиночные заявки-приманки дают фейковые 10-20%.
SUSPICIOUS_SPREAD_PCT = float(os.getenv("SUSPICIOUS_SPREAD_PCT", "5.0"))

# Канал для автопостинга арбитража (маркетинг + трафик).
# Пусто = функция спит. Заполни когда создашь канал: CHANNEL_ID=@my_channel
# (бот должен быть АДМИНОМ канала с правом публикации).
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

# Часы автопостинга (UTC). По умолчанию 06/12/18 UTC = 09/15/21 МСК.
CHANNEL_POST_HOURS = {
    int(x) for x in os.getenv("CHANNEL_POST_HOURS", "6,12,18").split(",")
    if x.strip().isdigit()
}

FIATS = ["KZT", "RUB", "TRY", "USD"]
ASSETS = ["USDT", "BTC", "ETH"]

FIAT_FLAGS = {
    "KZT": "🇰🇿 Тенге",
    "RUB": "🇷🇺 Рубль",
    "TRY": "🇹🇷 Лира",
    "USD": "🇺🇸 Доллар",
}

# Названия методов точно как их принимает API биржи
PAYMENT_METHODS_BINANCE = {
    "KZT": ["KaspiBank", "HalykBank", "Jusan", "ForteBank", "FreedomBank"],
    "RUB": ["TinkoffNew", "Sberbank", "RaiffeisenBank", "FPSBANK"],
    "TRY": ["ZiraatBank", "Garanti", "Akbank", "VakifBank", "Papara"],
    "USD": ["BankTransfer"],
}

PAYMENT_METHODS_BYBIT = {
    "KZT": ["KaspiBank", "HalykBank", "FreedomBank", "Jusan", "ForteBank"],
    "RUB": ["Tinkoff", "Sberbank", "RaiffeisenBank", "SBP"],
    "TRY": ["ZiraatBank", "Garanti", "Akbank", "Papara"],
    "USD": ["BankTransfer"],
}

PAYMENT_METHODS_OKX = {
    "KZT": ["KaspiBank", "HalykBank", "Jusan", "ForteBank", "FreedomBank"],
    "RUB": ["TinkoffNew", "Sberbank", "RaiffeisenBank", "FPSBANK"],
    "TRY": ["ZiraatBank", "Garanti", "Akbank", "VakifBank", "Papara"],
    "USD": ["BankTransfer"],
}

PAYMENT_METHODS_WALLET = {
    "RUB": ["TinkoffNew", "Sberbank", "FPSBANK"],
    "KZT": ["KaspiBank"],
    "USD": ["BankTransfer"],
}

# Красивые названия для отображения
PAYMENT_LABELS = {
    "KaspiBank": "Kaspi",
    "HalykBank": "Halyk",
    "FreedomBank": "Freedom",
    "Jusan": "Jusan",
    "ForteBank": "Forte",
    "TinkoffNew": "Tinkoff",
    "Sberbank": "Сбер",
    "RaiffeisenBank": "Raiffeisen",
    "FPSBANK": "СБП",
    "SBP": "СБП",
    "ZiraatBank": "Ziraat",
    "Garanti": "Garanti",
    "Akbank": "Akbank",
    "VakifBank": "Vakif",
    "Papara": "Papara",
    "BankTransfer": "Bank Transfer",
    "Tinkoff": "Tinkoff",
}
