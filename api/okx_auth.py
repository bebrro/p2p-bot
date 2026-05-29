"""
OKX REST API v5 — приватные запросы для C2C/P2P мерчантов.
Auth: HMAC-SHA256 + Base64 (стандарт OKX v5).

Права API ключа (создавать на okx.com → Профиль → API → Создать):
  ✅ Trade (C2C)
  ❌ БЕЗ прав вывода и переводов
"""
import hmac
import hashlib
import base64
import json
import aiohttp
from datetime import datetime, timezone
from typing import Optional

OKX_BASE = "https://www.okx.com"


# ─── Подпись ──────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sign(secret: str, ts: str, method: str, path: str, body: str = "") -> str:
    msg = ts + method.upper() + path + body
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()


def _make_headers(api_key: str, secret: str, passphrase: str,
                  method: str, path: str, body: str = "") -> dict:
    ts = _ts()
    return {
        "OK-ACCESS-KEY":        api_key,
        "OK-ACCESS-SIGN":       _sign(secret, ts, method, path, body),
        "OK-ACCESS-TIMESTAMP":  ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type":         "application/json",
    }


# ─── HTTP ─────────────────────────────────────────────────────────────────────

async def _get(api_key: str, secret: str, passphrase: str,
               path: str, params: dict = None) -> dict:
    qs = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    headers = _make_headers(api_key, secret, passphrase, "GET", path + qs)
    async with aiohttp.ClientSession() as s:
        async with s.get(
            OKX_BASE + path + qs, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            return await r.json(content_type=None)


async def _post(api_key: str, secret: str, passphrase: str,
                path: str, body: dict = None) -> dict:
    body     = body or {}
    body_str = json.dumps(body)
    headers  = _make_headers(api_key, secret, passphrase, "POST", path, body_str)
    async with aiohttp.ClientSession() as s:
        async with s.post(
            OKX_BASE + path, headers=headers, data=body_str,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            return await r.json(content_type=None)


# ─── Проверка ключа ───────────────────────────────────────────────────────────

async def verify_api_key(api_key: str, secret: str, passphrase: str) -> tuple[bool, str]:
    """
    Проверяет ключ через /api/v5/account/balance.
    Returns: (True, "OKX") или (False, error_message)
    """
    try:
        data = await _get(api_key, secret, passphrase, "/api/v5/account/balance")
        code = data.get("code", "-1")
        if str(code) == "0":
            return True, "OKX"
        return False, data.get("msg", "Неизвестная ошибка")
    except Exception as e:
        return False, str(e)


# ─── Мои объявления ───────────────────────────────────────────────────────────

async def get_my_ads(api_key: str, secret: str, passphrase: str) -> list[dict]:
    """
    Возвращает список своих C2C объявлений.
    Формат нормализован под тот же что у bybit_auth.get_my_ads:
      side "1" = sell, side "0" = buy
    """
    try:
        data  = await _post(api_key, secret, passphrase,
                            "/api/v5/p2p/advertisement/list", {})
        items = data.get("data", []) or []
    except Exception:
        return []

    result = []
    for item in items:
        side_raw = str(item.get("side", "")).lower()   # "buy" / "sell"
        result.append({
            "id":         str(item.get("advId", "")),
            "price":      float(item.get("price", 0)),
            "min_amount": float(item.get("minSingleTransAmount", 0)),
            "max_amount": float(item.get("maxSingleTransAmount", 0)),
            "quantity":   float(item.get("availableAmount", 0)),
            "side":       "1" if side_raw == "sell" else "0",
            "fiat":       item.get("quoteCurrency", ""),
            "asset":      item.get("baseCurrency", ""),
            "status":     item.get("state", 1),
        })
    return result


# ─── Изменить цену объявления ──────────────────────────────────────────────────

async def update_ad_price(api_key: str, secret: str, passphrase: str,
                           ad_id: str, new_price: float) -> dict:
    """
    Обновляет цену C2C объявления.
    OKX endpoint: POST /api/v5/p2p/advertisement/update-price
    Ответ: {"code":"0"} при успехе, иначе {"code":"...", "msg":"..."}
    """
    return await _post(api_key, secret, passphrase,
                       "/api/v5/p2p/advertisement/update-price", {
                           "advId": ad_id,
                           "price": f"{new_price:.2f}",
                       })


# ─── Статистика ────────────────────────────────────────────────────────────────

async def get_p2p_stats(api_key: str, secret: str, passphrase: str) -> dict:
    """Информация о мерчанте (если доступна)."""
    try:
        data = await _get(api_key, secret, passphrase, "/api/v5/p2p/merchant/info")
        info = (data.get("data") or [{}])[0]
        return {
            "nickname":     info.get("nickName", "OKX"),
            "orders_30d":   info.get("orderQuantity", 0),
            "completion":   float(info.get("completionRate", 0)),
            "total_orders": info.get("totalOrderQuantity", 0),
            "good_reviews": info.get("buyPositiveComment", 0),
            "bad_reviews":  info.get("buyNegativeComment", 0),
        }
    except Exception:
        return {}
