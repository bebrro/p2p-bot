from config import PAYMENT_LABELS

# Хранилище фильтров: {user_id: {"pay": "KaspiBank", "min_amount": 5000}}
_filters: dict[int, dict] = {}


def get_filter(user_id: int) -> dict:
    return _filters.get(user_id, {})


def set_filter(user_id: int, key: str, value):
    if user_id not in _filters:
        _filters[user_id] = {}
    _filters[user_id][key] = value


def clear_filter(user_id: int, key: str = None):
    if key:
        _filters.get(user_id, {}).pop(key, None)
    else:
        _filters.pop(user_id, None)


def filter_summary(user_id: int) -> str:
    f = get_filter(user_id)
    parts = []
    if f.get("pay"):
        parts.append(f"💳 {PAYMENT_LABELS.get(f['pay'], f['pay'])}")
    if f.get("min_amount"):
        parts.append(f"💰 от {f['min_amount']:,.0f}")
    return "  |  ".join(parts) if parts else ""
