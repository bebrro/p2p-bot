"""
Binance C2C/P2P authenticated API (HMAC-SHA256).

Права API ключа (создавать на binance.com → Профиль → API):
  ✅ Enable Reading
  ❌ НЕТ права вывода средств!

Эндпоинты:
  GET /sapi/v1/account/apiRestrictions    — проверка ключа
  GET /sapi/v1/c2c/orderMatch/listUserOrderHistory — история P2P ордеров
"""
import hmac
import hashlib
import time
import logging
import aiohttp

logger = logging.getLogger(__name__)
BINANCE_BASE = "https://api.binance.com"


# ─── Подпись ──────────────────────────────────────────────────────────────────

def _sign(secret: str, params_str: str) -> str:
    return hmac.new(secret.encode(), params_str.encode(), hashlib.sha256).hexdigest()


async def _get(api_key: str, secret: str, path: str, params: dict = None) -> dict:
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    sig = _sign(secret, qs)
    url = f"{BINANCE_BASE}{path}?{qs}&signature={sig}"
    headers = {"X-MBX-APIKEY": api_key}
    async with aiohttp.ClientSession() as s:
        async with s.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            return await r.json(content_type=None)


# ─── Проверка ключа ────────────────────────────────────────────────────────────

async def verify_api_key(api_key: str, secret: str) -> tuple[bool, str]:
    """
    Проверяет ключ через /sapi/v1/account/apiRestrictions.
    Returns: (True, "Binance") или (False, error_message)
    """
    try:
        data = await _get(api_key, secret, "/sapi/v1/account/apiRestrictions")
        # При успехе возвращается объект с полем enableReading
        if "enableReading" in data:
            return True, "Binance"
        # При ошибке — {"code": -2014, "msg": "API-key format invalid."}
        msg = data.get("msg", str(data))
        return False, msg
    except Exception as e:
        return False, str(e)


# ─── История P2P ордеров ───────────────────────────────────────────────────────

async def get_p2p_orders(api_key: str, secret: str, rows: int = 20) -> list[dict]:
    """
    Получает последние завершённые C2C ордера (BUY + SELL).

    Поля Binance API:
      amount      = количество крипты (USDT)
      totalPrice  = сумма в фиате
      unitPrice   = цена за 1 USDT
      orderStatus = COMPLETED / CANCELLED / ...
    """
    result: list[dict] = []
    for trade_type in ("BUY", "SELL"):
        try:
            data = await _get(api_key, secret,
                              "/sapi/v1/c2c/orderMatch/listUserOrderHistory", {
                                  "tradeType": trade_type,
                                  "page":      1,
                                  "rows":      rows,
                              })
            orders = data.get("data", []) or []
            for o in orders:
                if o.get("orderStatus") != "COMPLETED":
                    continue
                result.append({
                    "order_id":  o.get("orderNumber", ""),
                    # BUY = пользователь покупает крипту  → side "0"
                    # SELL = пользователь продаёт крипту → side "1"
                    "side":      "0" if trade_type == "BUY" else "1",
                    "asset":     o.get("asset", ""),
                    "fiat":      o.get("fiat", ""),
                    "price":     float(o.get("unitPrice",   0)),
                    "amount":    float(o.get("totalPrice",  0)),   # фиат
                    "quantity":  float(o.get("amount",      0)),   # крипта
                    "status":    "50",   # completed — приводим к общему формату
                    "nickname":  o.get("counterPartNickName", "—"),
                })
        except Exception as e:
            logger.warning(f"Binance P2P orders {trade_type} error: {e}")
    return result


# ─── Статистика аккаунта ──────────────────────────────────────────────────────

async def get_p2p_stats(api_key: str, secret: str) -> dict:
    """Базовая информация об API ключе."""
    try:
        data = await _get(api_key, secret, "/sapi/v1/account/apiRestrictions")
        return {
            "nickname":     "Binance",
            "reading":      data.get("enableReading", False),
            "total_orders": 0,
        }
    except Exception:
        return {"nickname": "Binance"}
