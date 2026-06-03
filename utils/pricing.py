"""
Честный выбор лучшей цены из стакана + sanity-check.

Зачем: раньше get_best_price слепо брал ads[0], полагаясь на сортировку
биржи. Если API сменит порядок — бот молча покажет неверную «лучшую» цену.
Здесь мы САМИ вычисляем min/max и отсекаем мусорные цены.

Семантика:
  buy_side=True  → ТЫ покупаешь крипту → лучшая цена = минимальная (дешевле купить)
  buy_side=False → ТЫ продаёшь крипту → лучшая цена = максимальная (дороже продать)
"""
import statistics
from typing import Optional

# Разумный диапазон цены 1 USDT в фиате — отсекает 0, мусор и битые ответы.
# Широкие границы: задача — поймать явный брак, а не микро-отклонения.
_USDT_RANGE = {
    "KZT": (300, 750),
    "RUB": (50, 140),
    "TRY": (15, 70),
    "USD": (0.85, 1.25),
    # ЮВА
    "THB": (25, 45),
    "IDR": (13_000, 20_000),
    "VND": (21_000, 30_000),
    # Крупные рынки
    "INR": (75, 120),   # Индия торгует с премией к споту
    "AED": (3.4, 4.0),
    "NGN": (900, 2_500),
    "BRL": (4.5, 7.5),
    # СНГ
    "GEL": (2.2, 3.3),
    "AMD": (340, 460),
    "AZN": (1.5, 2.0),
    "UZS": (10_500, 15_500),
    "KGS": (75, 100),
}


def sane_price(price: float, fiat: str, asset: str = "USDT") -> bool:
    """True если цена правдоподобна. Для не-USDT проверяем только > 0."""
    if not isinstance(price, (int, float)) or price <= 0:
        return False
    if asset != "USDT":
        return True
    lo, hi = _USDT_RANGE.get(fiat, (0, float("inf")))
    return lo <= price <= hi


# Полоса вокруг медианы = «реальный рынок». Цены за её пределами —
# скам-приманки (дикая цена + крошечный лимит), они НЕ исполнимы.
# На P2P разброс реальных цен обычно <5%, поэтому ±5% — надёжный якорь.
_MEDIAN_BAND = 0.05


def pick_best_price(ads: list[dict], *, buy_side: bool,
                    fiat: str, asset: str = "USDT") -> Optional[float]:
    """
    Возвращает лучшую РЕАЛИСТИЧНУЮ цену ОДНОЙ стороны стакана (де-байтинг по
    собственной медиане стороны).

    Защита от двух проблем:
      1. Битая сортировка биржи — считаем сами (не верим ads[0]).
      2. Объявления-приманки на хвостах — оставляем только цены в пределах
         ±5% от медианы стороны и уже среди них берём лучшую.

    ВАЖНО: кросс-сторонние приманки-биды (где ПРИМАНОК большинство и медиана
    стороны задрана) тут не ловятся — это делает _headline_prices в server.py
    через якорь на сторону асков.
    """
    prices = sorted(
        a["price"] for a in ads
        if sane_price(a.get("price", 0), fiat, asset)
    )
    n = len(prices)
    if n == 0:
        return None
    if n <= 3:
        return prices[0] if buy_side else prices[-1]

    med  = statistics.median(prices)
    band = [p for p in prices
            if med * (1 - _MEDIAN_BAND) <= p <= med * (1 + _MEDIAN_BAND)]
    if not band:
        band = prices
    return min(band) if buy_side else max(band)
