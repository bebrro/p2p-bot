import aiohttp
from typing import Optional

BINANCE_P2P_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0",
}


async def get_ads(
    asset: str = "USDT",
    fiat: str = "KZT",
    trade_type: str = "BUY",
    pay_types: Optional[list] = None,
    rows: int = 10,
) -> list[dict]:
    payload = {
        "asset": asset,
        "fiat": fiat,
        "merchantCheck": False,
        "page": 1,
        "payTypes": pay_types or [],
        "publisherType": None,
        "rows": rows,
        "tradeType": trade_type,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(BINANCE_P2P_URL, json=payload, headers=HEADERS) as resp:
            data = await resp.json()

    ads = []
    for item in data.get("data", []):
        adv = item.get("adv", {})
        advertiser = item.get("advertiser", {})
        ads.append({
            "price": float(adv.get("price", 0)),
            "min_amount": float(adv.get("minSingleTransAmount", 0)),
            "max_amount": float(adv.get("maxSingleTransAmount", 0)),
            "available": float(adv.get("tradableQuantity", 0)),
            "pay_types": [p.get("payType", "") for p in adv.get("tradeMethods", [])],
            "nickname": advertiser.get("nickName", "—"),
            "orders": advertiser.get("monthOrderCount", 0),
            "completion": advertiser.get("monthFinishRate", 0),
            "description": adv.get("remarks", ""),
            "ad_no": adv.get("advNo", ""),
        })
    return ads


async def get_best_price(asset: str, fiat: str, trade_type: str) -> Optional[float]:
    ads = await get_ads(asset=asset, fiat=fiat, trade_type=trade_type, rows=1)
    return ads[0]["price"] if ads else None
