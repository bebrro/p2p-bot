def calc_spread(buy_price: float, sell_price: float) -> dict:
    """
    buy_price  — цена покупки крипты (меньше)
    sell_price — цена продажи крипты (больше)
    """
    if buy_price <= 0:
        return {"spread_abs": 0, "spread_pct": 0}
    spread_abs = sell_price - buy_price
    spread_pct = (spread_abs / buy_price) * 100
    return {
        "spread_abs": round(spread_abs, 2),
        "spread_pct": round(spread_pct, 4),
    }


def format_ad(ad: dict, index: int, exchange: str) -> str:
    from utils.scam_detector import risk_badge, risk_tooltip
    from utils.desc_parser import flags_line

    completion = ad["completion"]
    if isinstance(completion, float) and completion <= 1:
        completion = round(completion * 100, 1)

    unique_pay = list(dict.fromkeys(ad["pay_types"]))
    pay  = " | ".join(unique_pay[:4]) or "—"
    desc = ad.get("description", "")

    badge   = risk_badge(ad)
    tooltip = risk_tooltip(ad)

    # Значки из описания (3-е лица, точная сумма, и т.д.)
    smart_flags = flags_line(desc)

    # Короткий превью описания (только если нет флагов — не дублировать)
    if desc and not smart_flags:
        desc_preview = f"\n📝 {desc[:80]}{'...' if len(desc) > 80 else ''}"
    elif desc and smart_flags:
        desc_preview = ""  # флаги уже несут суть
    else:
        desc_preview = ""

    return (
        f"{'🟡 Binance' if exchange == 'binance' else '🟠 Bybit'} #{index} {badge}\n"
        f"💰 Цена: {ad['price']:,.2f}\n"
        f"📦 Доступно: {ad['available']:.2f} USDT\n"
        f"↔️ Лимиты: {ad['min_amount']:,.0f} – {ad['max_amount']:,.0f}\n"
        f"💳 Оплата: {pay}\n"
        f"👤 {ad['nickname']} | {ad['orders']} сделок | {completion}% — {tooltip}"
        f"{smart_flags}"
        f"{desc_preview}"
    )
