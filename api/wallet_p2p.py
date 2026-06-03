import logging
from typing import Optional
from utils.http import post_json
from utils.pricing import pick_best_price

logger = logging.getLogger(__name__)

# TG Wallet P2P integration API (требует X-API-Key)
# Ключ выдаётся бесплатно: wallet.tg → P2P Market → настройки → API
WALLET_URL = "https://p2p.walletbot.me/p2p/integration-api/v1/item/online"


def _get_api_key() -> str:
    """Возвращает API-ключ из конфига (lazy import чтобы не было circular)."""
    try:
        from config import WALLET_P2P_API_KEY
        return WALLET_P2P_API_KEY
    except ImportError:
        return ""


async def get_ads(
    asset:     str = "USDT",
    fiat:      str = "RUB",
    side:      str = "buy",
    pay_types: Optional[list] = None,
    rows:      int = 10,
    _force:    bool = False,
) -> list[dict]:
    from config import DISABLED_EXCHANGES
    if "wallet" in DISABLED_EXCHANGES and not _force:
        return []
    api_key = _get_api_key()
    if not api_key:
        logger.debug("WALLET_P2P_API_KEY не задан — TG Wallet P2P недоступен")
        return []

    headers = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "X-API-Key":    api_key,
    }

    # API: side "BUY" = merchants selling USDT (taker buys), "SELL" = merchants buying USDT
    wallet_side = "BUY" if side in ("buy", "BUY") else "SELL"

    # При фильтре по банку тянем БОЛЬШЕ объявлений и фильтруем на клиенте
    # (ниже, по реальным именам методов). API-параметр paymentMethod НЕ шлём:
    # он ждёт внутренний код, а у нас отображаемое имя («Garanti») — иначе
    # биржа вернёт пусто и фильтр будет «не работать».
    fetch_rows = max(rows * 5, 60) if pay_types else rows
    payload = {
        "cryptoCurrency": asset,
        "fiatCurrency":   fiat,
        "side":           wallet_side,
        "page":           1,
        "pageSize":       fetch_rows,
    }

    data = await post_json(WALLET_URL, json=payload, headers=headers)
    logger.info(f"Wallet raw response type={type(data).__name__} keys={list(data.keys()) if isinstance(data, dict) else f'list[{len(data)}]' if isinstance(data, list) else data}")

    if not data:
        return []

    # API may return a bare list OR a dict with items inside
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, list):
            items = inner
        elif isinstance(inner, dict):
            items = inner.get("items") or []
        else:
            items = data.get("items") or []
    else:
        items = []

    ads = []
    for item in items:
        # Payments field
        pay_raw = item.get("payments") or item.get("paymentMethods") or []
        if isinstance(pay_raw, list):
            pay_names = [
                (p.get("name") or p.get("type") or str(p)) if isinstance(p, dict) else str(p)
                for p in pay_raw
            ]
        else:
            pay_names = [str(pay_raw)] if pay_raw else []

        try:
            comp = float(item.get("executeRate") or item.get("completionRate") or 0)
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
            "nickname":    item.get("nickname") or item.get("userName") or "—",
            "orders":      int(item.get("completedOrdersCount") or item.get("orderCount") or 0),
            "completion":  comp,
            "description": item.get("comment") or item.get("remark") or "",
            "ad_no":       str(item.get("id") or ""),
            "advertiser_no": str(item.get("userId") or ""),
        })

    if pay_types:
        lower_pay = [p.lower() for p in pay_types]
        def _matches(pay_list: list) -> bool:
            for pt in pay_list:
                ptl = pt.lower()
                for lp in lower_pay:
                    if lp in ptl or ptl in lp:
                        return True
            return False
        ads = [a for a in ads if _matches(a["pay_types"])]

    return ads[:rows]


async def get_best_price(asset: str, fiat: str, side: str) -> Optional[float]:
    ads = await get_ads(asset=asset, fiat=fiat, side=side, rows=10)
    return pick_best_price(ads, buy_side=(side == "buy"), fiat=fiat, asset=asset)
