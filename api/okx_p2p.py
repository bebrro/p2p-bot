import logging
import time
from typing import Optional
from utils.http import get_json

logger = logging.getLogger(__name__)

# OKX v3 C2C endpoint — GET with query params
OKX_C2C_URL = "https://www.okx.com/v3/c2c/tradingOrders/books"

_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.okx.com/p2p-markets/",
    "Origin":          "https://www.okx.com",
}


async def get_ads(
    asset:     str = "USDT",
    fiat:      str = "RUB",
    side:      str = "buy",
    pay_types: Optional[list] = None,
    rows:      int = 10,
) -> list[dict]:
    params = {
        "t":                   str(int(time.time() * 1000)),
        "quoteCurrency":       fiat,
        "baseCurrency":        asset,
        "side":                side,
        "paymentMethod":       "all",
        "userType":            "all",
        "showTrade":           "false",
        "showFollow":          "false",
        "showAlreadyTraded":   "false",
        "isAbleFilter":        "false",
        "receivingAds":        "false",
        "urlSource":           "P2P",
    }
    data = await get_json(OKX_C2C_URL, params=params, headers=_HEADERS)

    if not data:
        return []

    code = data.get("code")
    if code not in ("0", 0, None):
        logger.warning(f"OKX P2P error code={code}: {data.get('msg', data)}")
        return []

    # Response: {"code":"0","data":[...]} or {"code":"0","data":{"sell":[...],"buy":[...]}}
    raw = data.get("data", [])
    if isinstance(raw, dict):
        items = raw.get(side, raw.get("sell", raw.get("buy", [])))
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    ads = []
    for item in items:
        pay_raw = item.get("paymentMethods") or item.get("paymentMethod") or []
        if isinstance(pay_raw, list):
            pay_names = (
                [p.get("paymentMethod") or p.get("name") or str(p) for p in pay_raw]
                if pay_raw and isinstance(pay_raw[0], dict)
                else [str(p) for p in pay_raw]
            )
        else:
            pay_names = [str(pay_raw)] if pay_raw else []

        try:
            comp = float(item.get("completionRate") or 0)
        except (ValueError, TypeError):
            comp = 0.0

        ads.append({
            "price":       float(item.get("price", 0)),
            "min_amount":  float(item.get("limitMinAmount") or item.get("quoteMinAmountPerOrder") or 0),
            "max_amount":  float(item.get("limitMaxAmount") or item.get("quoteMaxAmountPerOrder") or 0),
            "available":   float(item.get("availableAmount") or item.get("availCryptoAmount") or 0),
            "pay_types":   pay_names,
            "nickname":    item.get("nickName") or item.get("publicUserId") or "—",
            "orders":      int(item.get("completedOrderQuantity") or 0),
            "completion":  comp,
            "description": item.get("remark") or "",
            "ad_no":       item.get("advNo") or item.get("id") or "",
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
    ads = await get_ads(asset=asset, fiat=fiat, side=side, rows=1)
    return ads[0]["price"] if ads else None
