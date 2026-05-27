"""
Оценка надёжности мерчанта по публичным данным объявления.

Score 0-100 (100 = идеально):
  🟢 85-100  Надёжный
  🟡 55-84   Средний риск
  🔴  0-54   Высокий риск
"""


def risk_score(ad: dict) -> int:
    score = 100

    orders = int(ad.get("orders", 0) or 0)
    completion = float(ad.get("completion", 0) or 0)
    # Bybit может отдавать 0..1 вместо 0..100
    if 0 < completion <= 1.0:
        completion *= 100

    # Штраф за мало сделок
    if orders < 5:
        score -= 40
    elif orders < 20:
        score -= 20
    elif orders < 50:
        score -= 8

    # Штраф за низкий процент успеха
    if completion < 70:
        score -= 40
    elif completion < 85:
        score -= 15
    elif completion < 95:
        score -= 5

    return max(0, min(100, score))


def risk_badge(ad: dict) -> str:
    """Возвращает цветной кружок риска."""
    s = risk_score(ad)
    if s >= 85:
        return "🟢"
    if s >= 55:
        return "🟡"
    return "🔴"


def risk_tooltip(ad: dict) -> str:
    """Короткое объяснение для детального вида."""
    orders = int(ad.get("orders", 0) or 0)
    completion = float(ad.get("completion", 0) or 0)
    if 0 < completion <= 1.0:
        completion *= 100

    warnings = []
    if orders < 5:
        warnings.append("очень мало сделок")
    elif orders < 20:
        warnings.append("мало сделок")
    if completion < 70:
        warnings.append("низкий % успеха")
    elif completion < 85:
        warnings.append("средний % успеха")

    s = risk_score(ad)
    if s >= 85:
        level = "Надёжный"
    elif s >= 55:
        level = "Средний риск"
    else:
        level = "Высокий риск"

    return f"{level}" + (f" ({', '.join(warnings)})" if warnings else "")
