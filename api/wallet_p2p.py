"""
Telegram @wallet P2P API (walletbot.me).
Публичный эндпоинт — авторизация не нужна.

Пара: USDT/TON vs RUB/KZT.
"""
import aiohttp
import logging
from typing import Optional

logger = logging.getLogger(__name__)

WALLET_URL = "https://walletbot.me/api/v1/p2p/order/preview/list"

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Origin":       "https://walletbot.me",
    "Referer":      "https://walletbot.me/",
}


async def get_ads(
    asset:     str = "USDT",
    fiat:      str = "RUB",
    side:      str = "buy",     # "buy" = ты покупаешь, "sell" = ты продаёшь
    pay_types: Optional[list] = None,
    rows:      int = 10,
) -> list[dict]:
    # Wallet API: BUY = мерчант продаёт (ты покупаешь), SELL = мерчант покупает (ты продаёшь)
    wallet_side = "BUY" if side == "buy" else "SELL"

    payload = {
        "baseCurrencyCode":   asset,
        "quoteCurrencyCode":  fiat,
        "side":               wallet_side,
        "paymentMethodType":  pay_types[0] if pay_types else None,
        "amount":             None,
        "pageSize":           rows,
        "page":               0,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                WALLET_URL, json=payload, headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
    except Exception as e:
        logger.error(f"Wallet P2P request error: {e}")
        return []

    # Обрабатываем ответ — поддерживаем несколько вариантов структуры
    items = (
        data.get("data", {}).get("items")
        or data.get("data", {}).get("orders")
        or data.get("items")
        or data.get("orders")
        or (data.get("data") if isinstance(data.get("data"), list) else [])
        or []
    )

    ads = []
    for item in items:
        pay_raw = item.get("paymentMethods") or item.get("paymentMethodTypes") or []
        if isinstance(pay_raw, list):
            pay_names = [
                (p.get("type") or p.get("name") or str(p))
                if isinstance(p, dict) else str(p)
                for p in pay_raw
            ]
        else:
            pay_names = [str(pay_raw)] if pay_raw else []

        try:
            comp = float(item.get("completionRate") or item.get("successRate") or 0)
            if 0 < comp <= 1:
                comp = round(comp * 100, 1)
        except (ValueError, TypeError):
            comp = 0.0

        try:
            price = float(item.get("price") or item.get("rate") or 0)
        except (ValueError, TypeError):
            price = 0.0

        ads.append({
            "price":       price,
            "min_amount":  float(item.get("minAmount") or item.get("minOrderAmount") or 0),
            "max_amount":  float(item.get("maxAmount") or item.get("maxOrderAmount") or 0),
            "available":   float(item.get("availableAmount") or item.get("amount") or 0),
            "pay_types":   pay_names,
            "nickname":    item.get("userName") or item.get("userId") or "—",
            "orders":      int(item.get("completedOrdersCount") or item.get("orderCount") or 0),
            "completion":  comp,
            "description": item.get("comment") or item.get("remark") or "",
            "ad_no":       str(item.get("id") or item.get("orderId") or ""),
        })

    return ads[:rows]


async def get_best_price(asset: str, fiat: str, side: str) -> Optional[float]:
    ads = await get_ads(asset=asset, fiat=fiat, side=side, rows=1)
    return ads[0]["price"] if ads else None
