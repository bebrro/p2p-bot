"""
AI-классификация описаний P2P-объявлений через Gemini.

Зачем: regex (utils/desc_parser) невозможно бесконечно латать под каждую новую
формулировку («1/3», «на дов лицо но от 1 го», «с любого банка кроме…»). LLM
читает смысл и решает сам. Regex остаётся мгновенным фолбэком, когда AI выключен,
недоступен или ещё не прогрел кэш.

Производительность:
  • ВСЕ описания батчатся в ОДИН запрос Gemini.
  • Результат кэшируется по тексту → в установившемся режиме (одни и те же
    мерчанты висят в стакане) новых запросов почти нет, ответ мгновенный.
  • Жёсткий таймаут: если AI не успел — отдаём regex, кэш прогреется к
    следующему обновлению стакана.
"""
import hashlib
import json
import logging
import os

from api import gemini

logger = logging.getLogger(__name__)

# norm_text -> {"third_party": True|False|None, "scam_recruit", "trap", "any_bank"}
_CACHE: dict[str, dict] = {}
_CACHE_MAX = 5000


def enabled() -> bool:
    """AI-разбор включён, если есть ключ и не выключен явно (AI_DESC_PARSE=0)."""
    if not os.getenv("GEMINI_API_KEY", "").strip():
        return False
    return os.getenv("AI_DESC_PARSE", "1").strip().lower() not in ("0", "false", "no", "off")


def _key(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()


def _normalize(d: dict) -> dict:
    tp = str(d.get("third_party", "unknown")).strip().lower()
    third = True if tp in ("yes", "true") else (False if tp == "no" else None)
    return {
        "third_party":  third,
        "scam_recruit": bool(d.get("scam")),
        "trap":         bool(d.get("trap")),
        "any_bank":     bool(d.get("any_bank")),
    }


_PROMPT = (
    "Ты эксперт по P2P-торговле криптовалютой. Для КАЖДОГО описания объявления "
    "определи 4 признака. Думай как опытный трейдер, понимай сленг, опечатки, "
    "сокращения и эмодзи.\n\n"
    "third_party — принимает ли мерчант ОПЛАТУ ОТ ТРЕТЬИХ ЛИЦ (платёж не от самого "
    "контрагента, а с чужого счёта):\n"
    "  \"yes\"  — явно принимает: «с любого банка/карты», «от любого лица», «3 лица ок», "
    "«от родственников/доверенных» (как ОТПРАВИТЕЛЕЙ платежа)\n"
    "  \"no\"   — НЕ принимает: «только свои/личные счета», «только от 1-го лица», "
    "«перевод только с вашей карты», «строго первое лицо»\n"
    "  \"unknown\" — не упомянуто\n"
    "  ПРАВИЛА КОНФЛИКТА: если есть и ограничение, и разрешение (напр. «только от 1 го» "
    "и «на дов лицо») — выбирай \"no\". «на дов лицо»/«на доверенное лицо» обычно = счёт "
    "ПОЛУЧАТЕЛЯ (сторона мерчанта), это НЕ про отправителя, сам по себе НЕ делает \"yes\".\n"
    "scam — вербовка/развод/фишинг: зовут в личку/телеграм мимо биржи, «лёгкие деньги», "
    "«% с круга», «рубим капусту», просят ФИО/реквизиты/СБП, обещают оплату «завтра/позже», "
    "просят отпустить первым. true/false\n"
    "trap — ловушка для апелляции: просят написать фразу-«согласие», «отмена ордера только "
    "по запросу», скрипты-условия для выигрыша спора. true/false\n"
    "any_bank — принимает с ЛЮБОГО банка / любой картой. true/false\n\n"
    "Верни СТРОГО JSON-массив РОВНО той же длины и порядка, что и список ниже, "
    "по одному объекту на описание:\n"
    "[{\"third_party\":\"yes|no|unknown\",\"scam\":false,\"trap\":false,\"any_bank\":false}, ...]\n\n"
    "ОПИСАНИЯ:\n"
)


async def classify(texts: list[str]) -> dict[str, dict]:
    """
    Классифицирует описания. Возвращает {исходный_текст: {признаки}}.
    Пустые тексты пропускает. При выключенном/упавшем AI — то что есть в кэше.
    """
    out: dict[str, dict] = {}
    todo: list[str] = []
    for t in texts:
        t = (t or "").strip()
        if not t:
            continue
        k = _key(t)
        if k in _CACHE:
            out[t] = _CACHE[k]
        elif t not in todo:
            todo.append(t)

    if not todo or not enabled():
        return out

    listing = "\n".join(f"{i + 1}. {t[:400]}" for i, t in enumerate(todo))
    try:
        raw = await gemini.ask_json(_PROMPT + listing, max_tokens=4000, timeout=12)
        if raw.startswith("❌"):
            logger.warning(f"ai_desc: gemini error: {raw[:80]}")
            return out
        arr = json.loads(raw)
        if isinstance(arr, list):
            for i, t in enumerate(todo):
                if i < len(arr) and isinstance(arr[i], dict):
                    parsed = _normalize(arr[i])
                    _CACHE[_key(t)] = parsed
                    out[t] = parsed
    except Exception as e:
        logger.warning(f"ai_desc classify failed: {e}")

    if len(_CACHE) > _CACHE_MAX:                       # подрезаем старое
        for k in list(_CACHE)[: len(_CACHE) - _CACHE_MAX]:
            _CACHE.pop(k, None)
    return out
