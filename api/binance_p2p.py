from typing import Optional
from utils.http import post_json
from utils.pricing import pick_best_price

BINANCE_P2P_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent":   "Mozilla/5.0",
}


async def get_ads(
    asset:      str = "USDT",
    fiat:       str = "KZT",
    trade_type: str = "BUY",
    pay_types:  Optional[list] = None,
    rows:       int = 10,
    _force:     bool = False,
) -> list[dict]:
    from config import DISABLED_EXCHANGES
    if "binance" in DISABLED_EXCHANGES and not _force:
        return []
    payload = {
        "asset":         asset,
        "fiat":          fiat,
        "merchantCheck": False,
        "page":          1,
        "payTypes":      pay_types or [],
        "publisherType": None,
        "rows":          rows,
        "tradeType":     trade_type,
    }
    data = await post_json(BINANCE_P2P_URL, json=payload, headers=_HEADERS)

    ads = []
    for item in data.get("data", []):
        adv        = item.get("adv", {})
        advertiser = item.get("advertiser", {})
        ads.append({
            "price":       float(adv.get("price", 0)),
            "min_amount":  float(adv.get("minSingleTransAmount", 0)),
            "max_amount":  float(adv.get("maxSingleTransAmount", 0)),
            "available":   float(adv.get("tradableQuantity", 0)),
            "pay_types":   [p.get("payType", "") for p in adv.get("tradeMethods", [])],
            "nickname":    advertiser.get("nickName", "—"),
            "orders":      advertiser.get("monthOrderCount", 0),
            "completion":  advertiser.get("monthFinishRate", 0),
            "description": adv.get("remarks", ""),
            "ad_no":       adv.get("advNo", ""),
            "advertiser_no": advertiser.get("userNo", ""),
        })
    return ads


async def get_best_price(asset: str, fiat: str, trade_type: str) -> Optional[float]:
    ads = await get_ads(asset=asset, fiat=fiat, trade_type=trade_type, rows=10)
    return pick_best_price(ads, buy_side=(trade_type == "BUY"), fiat=fiat, asset=asset)
