import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")  # Fernet key для шифрования API ключей
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")  # https://aistudio.google.com/app/apikey
WEBAPP_URL     = os.getenv("WEBAPP_URL",     "")  # HTTPS URL Mini App (ngrok / VPS)
WEBAPP_PORT    = int(os.getenv("PORT", os.getenv("WEBAPP_PORT", "8765")))

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
