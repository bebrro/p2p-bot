import asyncio
import aiohttp
import json

HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.bybit.com/",
}

async def main():
    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            "https://api2.bybit.com/fiat/otc/configuration/queryAllPaymentList",
            json={}, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)
        )
        data = await resp.json(content_type=None)

    result = data.get("result", {})

    # KZT IDs
    kzt_ids = set(str(i) for i in json.loads(result.get("currencyPaymentIdMap", "{}")).get("KZT", []))

    # Строим маппинг ID → название
    pay_config = result.get("paymentConfigVo", [])
    id_to_name = {}
    for p in pay_config:
        pid = str(p.get("paymentType", ""))
        name = p.get("paymentName", "")
        id_to_name[pid] = name

    print("=== Методы оплаты KZT ===")
    for pid in sorted(kzt_ids, key=lambda x: int(x)):
        name = id_to_name.get(pid, "???")
        print(f"  ID {pid:>4} → {name}")

asyncio.run(main())
