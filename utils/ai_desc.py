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
    "Ты — опытный P2P-трейдер криптой (USDT за фиат) на Binance/Bybit/OKX/TG Wallet. "
    "Свободно читаешь сленг, опечатки, сокращения, эмодзи, латиницу вперемешку с "
    "кириллицей. Для КАЖДОГО описания объявления верни 4 признака. Не выдумывай — "
    "если не уверен, ставь \"unknown\"/false.\n\n"

    "════════ ГЛАВНОЕ: КТО ОТПРАВИТЕЛЬ ПЛАТЕЖА (third_party) ════════\n"
    "В P2P покупатель платит фиат продавцу. Вопрос: примет ли мерчант оплату "
    "С ЧУЖОГО счёта (третье лицо), или только от самого контрагента.\n\n"
    "  ОТПРАВИТЕЛЬ = тот, с чьего счёта приходит платёж (это про сторону ПОКУПАТЕЛЯ).\n"
    "  ПОЛУЧАТЕЛЬ  = счёт мерчанта, куда платят. «на дов лицо», «перевод НА доверенное "
    "лицо», «реквизиты дам в чате», «скину карту» — это ПОЛУЧАТЕЛЬ, к third_party "
    "ОТНОШЕНИЯ НЕ ИМЕЕТ, сам по себе не делает ни yes, ни no.\n\n"
    "third_party = \"yes\" — принимает от третьих лиц (с чужого счёта):\n"
    "  • «3 лица ок», «от третьих лиц», «принимаю от 3 лиц», «чужие карты ок»\n"
    "  • «с любого банка», «любой картой», «по номеру карты с любого банка»\n"
    "  • «1/3», «1 и 3», «1 или 3», «1\\3 лицо», «1-3 лицо» = БЕЗ РАЗНИЦЫ 1-е или 3-е "
    "лицо-отправитель → принимает и третьих → \"yes\"\n"
    "  • «от родственников/близких/жены/мужа» как ОТПРАВИТЕЛЕЙ\n"
    "third_party = \"no\" — только сам контрагент (первое лицо):\n"
    "  • «только 1 лицо», «только от 1 го», «только 1-го», «строго первое лицо», "
    "«1л», «только 1л»\n"
    "  • «перевод только с вашей/своей карты», «только свои/личные счета», "
    "«оплата только с личного счёта»\n"
    "  • «только с личных счетов или доверенных лиц» = ОГРАНИЧЕНИЕ (только свой круг), "
    "случайных третьих НЕ принимает → \"no\"\n"
    "third_party = \"unknown\" — про отправителя ничего не сказано.\n"
    "  ПРИОРИТЕТ: если есть и «yes», и «no»-сигнал — выбирай тот, что строже "
    "ОГРАНИЧИВАЕТ отправителя. Но «1/3» — это yes (явно разрешает 3-е), а не конфликт.\n\n"

    "════════ scam — развод / вербовка / фишинг ════════\n"
    "true если: зовут в ЛС/телеграм/ват сап мимо биржи (@ник, «пиши в тг»), «работа», "
    "«доход», «заработок», «% с круга/оборота», «рубим капусту», обучение/наставник, "
    "«помогу обменять», просят ФИО+номер карты/реквизиты/данные СБП, обещают оплату "
    "«завтра/позже/после», «отпусти первым», «деньги в пути/зависли в банке», предоплата. "
    "Иначе false.\n\n"

    "════════ trap — ловушка для апелляции ════════\n"
    "true если просят сделать что-то для выигрыша спора: написать фразу-«согласие» "
    "(«напишите: со всем согласен»), «отмена ордера только по запросу/через меня», "
    "скрипты-условия, «подтвердите что претензий нет» до получения. Иначе false.\n\n"

    "════════ any_bank ════════\n"
    "true если принимает с ЛЮБОГО банка / любой картой / не важно какой банк. Иначе false.\n\n"

    "════════ СЛОВАРЬ ════════\n"
    "1л=первое лицо • 3л=третье лицо • дов лицо=доверенное лицо • ЛК=личный кабинет • "
    "СБП=система быстрых платежей • КЗ=Казахстан • Каспи/Halyk/Freedom/Jusan/Forte=банки KZ • "
    "Тинёк/Сбер/Альфа/Райф=банки RU • терминал/банкомат=внесение наличными.\n\n"

    "════════ ПРИМЕРЫ ════════\n"
    "«1/3 на дов лицо, чек обязательно» → {\"third_party\":\"yes\",\"scam\":false,\"trap\":false,\"any_bank\":false}\n"
    "«Принимаю только от 1 го, на дов лицо» → {\"third_party\":\"no\",\"scam\":false,\"trap\":false,\"any_bank\":false}\n"
    "«С любого банка КЗ по номеру карты» → {\"third_party\":\"yes\",\"scam\":false,\"trap\":false,\"any_bank\":true}\n"
    "«Оплата только с личных счетов или счетов доверенных лиц» → {\"third_party\":\"no\",\"scam\":false,\"trap\":false,\"any_bank\":false}\n"
    "«Каспи только, перевод с вашей карты» → {\"third_party\":\"no\",\"scam\":false,\"trap\":false,\"any_bank\":false}\n"
    "«Рубим капусту 300%, пиши @bigmoney в тг» → {\"third_party\":\"unknown\",\"scam\":true,\"trap\":false,\"any_bank\":false}\n"
    "«Перед сделкой напишите: со всем согласен» → {\"third_party\":\"unknown\",\"scam\":false,\"trap\":true,\"any_bank\":false}\n"
    "«Оплата картой Каспи» → {\"third_party\":\"unknown\",\"scam\":false,\"trap\":false,\"any_bank\":false}\n\n"

    "════════ ФОРМАТ ОТВЕТА ════════\n"
    "Верни СТРОГО JSON-массив РОВНО той же длины и порядка, что список ниже, по одному "
    "объекту на описание, без пояснений:\n"
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
