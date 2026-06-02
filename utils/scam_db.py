"""
Чёрный список P2P-кидал из публичных каналов-блэклистов.

Источник: канал @BlackListBybit (экспорт истории). Ключ — Bybit userMaskId
(вид «s<32hex>», стабильный ID мерчанта, в отличие от ника его не сменить).
Лежит в data/bybit_scammers.json: {userMaskId: "краткая причина"}.

Использование: помечаем объявление, сливаем вниз стакана и исключаем из связок.
"""
import json
import os
import logging

logger = logging.getLogger(__name__)

_BYBIT_SCAM: dict[str, str] = {}     # userMaskId -> reason


def _load() -> None:
    global _BYBIT_SCAM
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "data", "bybit_scammers.json")
    try:
        with open(path, encoding="utf-8") as f:
            _BYBIT_SCAM = json.load(f)
        logger.info("scam_db: загружено %d кидал Bybit", len(_BYBIT_SCAM))
    except Exception as e:
        logger.warning("scam_db: не удалось загрузить (%s) — ЧС пуст", e)
        _BYBIT_SCAM = {}


_load()


def is_scammer(mask_id: str | None) -> bool:
    """True если userMaskId (Bybit) есть в чёрном списке. У других бирж поля нет."""
    return bool(mask_id and mask_id in _BYBIT_SCAM)


def reason(mask_id: str | None) -> str:
    return _BYBIT_SCAM.get(mask_id or "", "")


def count() -> int:
    return len(_BYBIT_SCAM)
