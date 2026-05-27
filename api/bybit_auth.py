"""
Авторизованные запросы к Bybit API (HMAC-SHA256).
Используется для: просмотра своих объявлений, изменения цены,
публикации объявлений, просмотра ордеров и статистики.

Права API ключа (создавать на Bybit):
  ✅ P2P торговля
  ❌ НЕТ права вывода средств!
"""
import hmac
import hashlib
import time
import json
import aiohttp
from typing import Optional

BYBIT_BASE = "https://api.bybit.com"


# ─── Подпись ──────────────────────────────────────────────────────────────────

def _make_headers(api_key: str, api_secret: str, params_str: str) -> dict:
    ts          = str(int(time.time() * 1000))
    recv_window = "5000"
    sign_str    = ts + api_key + recv_window + params_str
    sig = hmac.new(api_secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY":      api_key,
        "X-BAPI-SIGN":         sig,
        "X-BAPI-TIMESTAMP":    ts,
        "X-BAPI-RECV-WINDOW":  recv_window,
        "X-BAPI-SIGN-TYPE":    "2",
        "Content-Type":        "application/json",
    }


async def _get(api_key: str, api_secret: str, path: str, params: dict = None) -> dict:
    params = params or {}
    qs      = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    headers = _make_headers(api_key, api_secret, qs)
    url     = f"{BYBIT_BASE}{path}" + (f"?{qs}" if qs else "")
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json(content_type=None)


async def _post(api_key: str, api_secret: str, path: str, body: dict = None) -> dict:
    body     = body or {}
    body_str = json.dumps(body, separators=(",", ":"))
    headers  = _make_headers(api_key, api_secret, body_str)
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{BYBIT_BASE}{path}", headers=headers, data=body_str,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            return await r.json(content_type=None)


# ─── Проверка ключа ────────────────────────────────────────────────────────────

async def verify_api_key(api_key: str, api_secret: str) -> tuple[bool, str]:
    """
    Проверяет что ключ работает.
    Returns: (True, nickname) или (False, error_message)
    """
    try:
        data = await _get(api_key, api_secret, "/v5/p2p/user/personal/info")
        ret  = data.get("retCode", -1)
        if ret == 0:
            nick = data.get("result", {}).get("nickName", "—")
            return True, nick
        return False, data.get("retMsg", "Unknown error")
    except Exception as e:
        return False, str(e)


# ─── Мои объявления ────────────────────────────────────────────────────────────

async def get_my_ads(api_key: str, api_secret: str, status: int = 1) -> list[dict]:
    """
    status: 1=online, 2=offline, 10=all
    """
    data  = await _post(api_key, api_secret, "/v5/p2p/item/personal/list",
                        {"status": status, "page": 1, "size": 20})
    items = data.get("result", {}).get("items") or []
    result = []
    for item in items:
        result.append({
            "id":         item.get("id", ""),
            "price":      float(item.get("price", 0)),
            "min_amount": float(item.get("minAmount", 0)),
            "max_amount": float(item.get("maxAmount", 0)),
            "quantity":   float(item.get("quantity", 0)),
            "side":       str(item.get("side", "")),   # "0"=buy, "1"=sell
            "fiat":       item.get("currencyId", ""),
            "asset":      item.get("tokenId", ""),
            "status":     item.get("status", 0),
            "payments":   item.get("payments", []),
        })
    return result


# ─── Изменить цену объявления ──────────────────────────────────────────────────

async def update_ad_price(api_key: str, api_secret: str,
                          item_id: str, new_price: float) -> dict:
    return await _post(api_key, api_secret, "/v5/p2p/item/update", {
        "itemId": item_id,
        "price":  f"{new_price:.2f}",
    })


# ─── Создать объявление ────────────────────────────────────────────────────────

async def create_ad(
    api_key: str, api_secret: str,
    asset: str,        fiat: str,
    side: str,         # "0"=купить USDT, "1"=продать USDT
    price: float,      quantity: float,
    min_amount: float, max_amount: float,
    payment_ids: list[str],
    remark: str = "",
) -> dict:
    return await _post(api_key, api_secret, "/v5/p2p/item/create", {
        "tokenId":    asset,
        "currencyId": fiat,
        "side":       side,
        "priceType":  "1",              # фиксированная цена
        "price":      f"{price:.2f}",
        "quantity":   f"{quantity:.2f}",
        "minAmount":  f"{min_amount:.2f}",
        "maxAmount":  f"{max_amount:.2f}",
        "payments":   payment_ids,
        "remark":     remark,
        "tradingPreferenceSet": {
            "hasUnPostAd":              0,
            "isKyc":                    0,
            "isEmail":                  0,
            "isMobile":                 0,
            "minCompletedOrderCount":   0,
            "completedOrderPercent":    "0",
        },
    })


async def set_ad_online(api_key: str, api_secret: str, item_id: str) -> dict:
    return await _post(api_key, api_secret, "/v5/p2p/item/online", {"itemId": item_id})


async def set_ad_offline(api_key: str, api_secret: str, item_id: str) -> dict:
    return await _post(api_key, api_secret, "/v5/p2p/item/offline", {"itemId": item_id})


# ─── Статистика и ордера ───────────────────────────────────────────────────────

async def get_p2p_stats(api_key: str, api_secret: str) -> dict:
    data = await _get(api_key, api_secret, "/v5/p2p/user/personal/info")
    info = data.get("result", {}) or {}
    return {
        "nickname":     info.get("nickName", "—"),
        "orders_30d":   info.get("recentOrderNum", 0),
        "completion":   float(info.get("recentExecuteRate", 0)),
        "avg_release":  info.get("avgReleaseTime", "—"),
        "total_orders": info.get("totalOrderNum", 0),
        "good_reviews": info.get("goodEvaluateCount", 0),
        "bad_reviews":  info.get("badEvaluateCount", 0),
    }


async def get_p2p_orders(api_key: str, api_secret: str,
                         status: str = "10") -> list[dict]:
    """
    status: "10"=все, "20"=ожидание, "30"=выпуск, "50"=завершены, "60"=отмена
    """
    data   = await _post(api_key, api_secret, "/v5/p2p/order/list",
                         {"page": 1, "size": 20, "orderStatus": status})
    orders = data.get("result", {}).get("result") or []
    result = []
    for o in orders:
        result.append({
            "order_id":   o.get("orderId", ""),
            "side":       str(o.get("side", "")),
            "asset":      o.get("tokenId", ""),
            "fiat":       o.get("currencyId", ""),
            "price":      float(o.get("price", 0)),
            "amount":     float(o.get("amount", 0)),
            "quantity":   float(o.get("quantity", 0)),
            "status":     str(o.get("status", "")),
            "nickname":   o.get("nickName", "—"),
            "created_at": o.get("createDate", ""),
        })
    return result
