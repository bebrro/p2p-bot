from typing import Optional
from utils.http import post_json
from utils.pricing import pick_best_price

BYBIT_P2P_URL  = "https://api2.bybit.com/fiat/otc/item/online"
BYBIT_PAY_URL  = "https://api2.bybit.com/fiat/otc/configuration/queryAllPaymentList"

_HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":      "https://www.bybit.com/",
}

# id → displayName,  normalizedName → id
_id_to_name: dict[str, str] = {}
_name_to_id: dict[str, str] = {}


async def _load_payment_map() -> None:
    global _id_to_name, _name_to_id
    if _id_to_name:
        return
    data = await post_json(BYBIT_PAY_URL, json={}, headers=_HEADERS)
    for p in data.get("result", {}).get("paymentConfigVo", []):
        pid  = str(p.get("paymentType", ""))
        name = p.get("paymentName", "").strip()
        if pid and name:
            _id_to_name[pid] = name
            _name_to_id[name.replace(" ", "").lower()] = pid


def _names_to_ids(pay_names: list[str]) -> list[str]:
    return [pid for name in pay_names
            if (pid := _name_to_id.get(name.replace(" ", "").lower()))]


async def get_ads(
    asset:     str = "USDT",
    fiat:      str = "KZT",
    side:      str = "1",
    pay_types: Optional[list] = None,
    size:      int = 10,
    amount:    str = "",
    _force:    bool = False,
) -> list[dict]:
    from config import DISABLED_EXCHANGES
    if "bybit" in DISABLED_EXCHANGES and not _force:
        return []
    await _load_payment_map()
    pay_ids = _names_to_ids(pay_types) if pay_types else []

    payload = {
        "tokenId":    asset,
        "currencyId": fiat,
        "payment":    pay_ids,
        "side":       side,
        "size":       str(size),
        "page":       "1",
        "amount":     amount,
    }
    data = await post_json(BYBIT_P2P_URL, json=payload, headers=_HEADERS)

    ads = []
    for item in data.get("result", {}).get("items", []):
        raw_pay   = item.get("payments", [])
        pay_names = [_id_to_name.get(str(p), str(p)) for p in raw_pay]
        ads.append({
            "price":       float(item.get("price", 0)),
            "min_amount":  float(item.get("minAmount", 0)),
            "max_amount":  float(item.get("maxAmount", 0)),
            "available":   float(item.get("quantity", 0)),
            "pay_types":   pay_names,
            "nickname":    item.get("nickName", "—"),
            "orders":      item.get("recentOrderNum", 0),
            "completion":  float(item.get("recentExecuteRate", 0)),
            "description": item.get("remark", ""),
            "ad_no":       item.get("id", ""),
            "advertiser_no": item.get("userId", ""),
        })
    return ads


async def get_best_price(asset: str, fiat: str, side: str) -> Optional[float]:
    ads = await get_ads(asset=asset, fiat=fiat, side=side, size=10)
    # Bybit: side "1" = ты покупаешь крипту, "0" = продаёшь
    return pick_best_price(ads, buy_side=(side == "1"), fiat=fiat, asset=asset)
