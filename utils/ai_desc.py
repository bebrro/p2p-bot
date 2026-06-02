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
import time

from api import gemini

logger = logging.getLogger(__name__)

# norm_text -> {"third_party": True|False|None, "scam_recruit", "trap", "any_bank"}
_CACHE: dict[str, dict] = {}
_CACHE_MAX = 5000

# Бэкофф при 429 (лимит Gemini). Пока активен — разбор описаний работает ТОЛЬКО
# из кэша и не дёргает API, чтобы не отнимать квоту у чата (он важнее).
_COOLDOWN_UNTIL = 0.0
_COOLDOWN_SEC = 60
# Троттл: не чаще одного батч-запроса в N секунд (бесплатный лимит ~15/мин).
_LAST_CALL = 0.0
_MIN_INTERVAL = 4.0


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
    banks = d.get("banks") or []
    if not isinstance(banks, list):
        banks = []
    return {
        "third_party":  third,
        "scam_recruit": bool(d.get("scam")),
        "trap":         bool(d.get("trap")),
        "any_bank":     bool(d.get("any_bank")),
        "banks":        [str(x) for x in banks][:6],
    }


_PROMPT = (
    "Ты — опытный P2P-трейдер криптой (USDT за фиат) на Binance/Bybit/OKX/TG Wallet. "
    "Свободно читаешь сленг, опечатки, сокращения, эмодзи, латиницу вперемешку с "
    "кириллицей. Для КАЖДОГО описания объявления верни 4 признака. Не выдумывай — "
    "если не уверен, ставь \"unknown\"/false.\n\n"

    "🌍 ВАЖНО: описания бывают на ЛЮБОМ языке — русский, английский, турецкий (TRY), "
    "индонезийский (IDR), вьетнамский (VND), хинди/английский (INR), арабский (AED), "
    "португальский (BRL), украинский, казахский, грузинский, армянский и др. "
    "Понимай ВСЕ языки, мысленно переводи и применяй ту же логику. Признаки 3-х лиц / "
    "скама не зависят от языка.\n\n"

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

    "════════ trap — ловушка / цена-приманка ════════\n"
    "true если: (а) просят сделать что-то для выигрыша спора — написать фразу-"
    "«согласие» («напишите: со всем согласен»), «отмена ордера только по запросу/"
    "через меня», «подтвердите что претензий нет» до получения; ИЛИ (б) цена-"
    "приманка / нереальное предложение: «ордер строго по заявкам», «без заявки — "
    "другое объявление в профиле», «напишите перед сделкой», «пишите в чат перед "
    "ордером» (показанная цена не для обычного клика); ИЛИ (в) просят написать "
    "КОНКРЕТНУЮ фразу в назначении/комментарии платежа («ödeme açıklama kısmına "
    "... yazınız», «в комментарии к платежу напишите ...», «write ... in payment "
    "description») — манипуляция для спора/чарджбэка. Иначе false.\n\n"

    "════════ any_bank ════════\n"
    "true если принимает с ЛЮБОГО банка / любой картой / не важно какой банк. Иначе false.\n\n"

    "════════ banks — какие банки указаны в ОПИСАНИИ ════════\n"
    "Список банков, которые мерчант называет в тексте («только Т-Банк», «строго "
    "Каспи», «sadece Ziraat», «only Tinkoff»). Это важно: в способах оплаты часто "
    "стоит общий «Bank Transfer», а настоящий банк указан словами. Верни массив "
    "канонических имён: Tinkoff, Sber, Alfa, VTB, Raiffeisen, Kaspi, Halyk, Freedom, "
    "Jusan, Forte, Ziraat, Garanti, Akbank, Papara, Enpara и т.п. Если банк не "
    "назван — пустой массив [].\n\n"

    "════════ СЛОВАРЬ ════════\n"
    "1л=первое лицо • 3л=третье лицо • дов лицо=доверенное лицо • ЛК=личный кабинет • "
    "СБП=система быстрых платежей • КЗ=Казахстан • Каспи/Halyk/Freedom/Jusan/Forte=банки KZ • "
    "Тинёк/Сбер/Альфа/Райф=банки RU • терминал/банкомат=внесение наличными.\n\n"

    "════════ ФРАЗЫ ПРО 3-х ЛИЦ НА РАЗНЫХ ЯЗЫКАХ ════════\n"
    "НЕ принимает (third_party=no):\n"
    "  EN: «no third party», «own account only», «sender name must match», «only from your card»\n"
    "  TR: «3. şahıs kabul edilmez», «üçüncü şahıs/kişi ödemesi yok», «sadece kendi hesabınızdan/kartınızdan», «gönderen adı eşleşmeli»\n"
    "  ID: «tidak menerima pihak ketiga», «hanya rekening sendiri», «nama pengirim harus sama»\n"
    "  VI: «không nhận bên thứ ba», «chỉ chuyển từ tài khoản của bạn», «tên người gửi phải trùng»\n"
    "  PT: «não aceito terceiros», «somente da sua conta», «apenas conta própria»\n"
    "  AR: «لا أقبل طرف ثالث», «من حسابك فقط»\n"
    "ПРИНИМАЕТ (third_party=yes):\n"
    "  EN: «third party ok», «any bank», «any card accepted»\n"
    "  TR: «3. şahıs kabul», «her bankadan», «herhangi bir karttan/hesaptan»\n"
    "  ID: «pihak ketiga boleh», «bank apa saja», «kartu siapa saja»\n"
    "  VI: «nhận bên thứ ba», «mọi ngân hàng», «thẻ nào cũng được»\n"
    "  PT: «aceito terceiros», «qualquer banco»\n\n"

    "════════ ПРИМЕРЫ ════════\n"
    "«1/3 на дов лицо, чек обязательно» → {\"third_party\":\"yes\",\"scam\":false,\"trap\":false,\"any_bank\":false}\n"
    "«Принимаю только от 1 го, на дов лицо» → {\"third_party\":\"no\",\"scam\":false,\"trap\":false,\"any_bank\":false}\n"
    "«Из-за блокировки карт принимаю на доверенное лицо. Не принимаю платежи от 3-х лиц» → {\"third_party\":\"no\",\"scam\":false,\"trap\":false,\"any_bank\":false}\n"
    "«С любого банка КЗ по номеру карты» → {\"third_party\":\"yes\",\"scam\":false,\"trap\":false,\"any_bank\":true}\n"
    "«Оплата только с личных счетов или счетов доверенных лиц» → {\"third_party\":\"no\",\"scam\":false,\"trap\":false,\"any_bank\":false}\n"
    "«Каспи только, перевод с вашей карты» → {\"third_party\":\"no\",\"scam\":false,\"trap\":false,\"any_bank\":false,\"banks\":[\"Kaspi\"]}\n"
    "«★Только Т-Банк★ Переводы строго с Т-БАНКА. Принимаю от 3 лица если есть доступ к ЛК» → {\"third_party\":\"yes\",\"scam\":false,\"trap\":false,\"any_bank\":false,\"banks\":[\"Tinkoff\"]}\n"
    "«Sadece Ziraat veya Garanti» (TR) → {\"third_party\":\"unknown\",\"scam\":false,\"trap\":false,\"any_bank\":false,\"banks\":[\"Ziraat\",\"Garanti\"]}\n"
    "«Рубим капусту 300%, пиши @bigmoney в тг» → {\"third_party\":\"unknown\",\"scam\":true,\"trap\":false,\"any_bank\":false}\n"
    "«Перед сделкой напишите: со всем согласен» → {\"third_party\":\"unknown\",\"scam\":false,\"trap\":true,\"any_bank\":false}\n"
    "«Ордер строго по заявкам! Без заявки другое объявление в профиле» → {\"third_party\":\"unknown\",\"scam\":false,\"trap\":true,\"any_bank\":false}\n"
    "«Оплата картой Каспи» → {\"third_party\":\"unknown\",\"scam\":false,\"trap\":false,\"any_bank\":false}\n"
    "«Sadece kendi hesabınızdan ödeme. 3. şahıs kabul edilmez» (TR) → {\"third_party\":\"no\",\"scam\":false,\"trap\":false,\"any_bank\":false}\n"
    "«Üçüncü şahıslardan kabul etmiyorum» (TR) → {\"third_party\":\"no\",\"scam\":false,\"trap\":false,\"any_bank\":false}\n"
    "«Üçüncü şahıslardan kabul etmiyorum. Ödeme açıklama kısmına \"Dijital varlık satış ödemesi\" yazınız» (TR) → {\"third_party\":\"no\",\"scam\":false,\"trap\":true,\"any_bank\":false}\n"
    "«Her bankadan kabul edilir» (TR) → {\"third_party\":\"yes\",\"scam\":false,\"trap\":false,\"any_bank\":true}\n"
    "«Tidak menerima pihak ketiga, hanya rekening sendiri» (ID) → {\"third_party\":\"no\",\"scam\":false,\"trap\":false,\"any_bank\":false}\n"
    "«Không nhận bên thứ ba» (VI) → {\"third_party\":\"no\",\"scam\":false,\"trap\":false,\"any_bank\":false}\n"
    "«third party ok, any bank» (EN) → {\"third_party\":\"yes\",\"scam\":false,\"trap\":false,\"any_bank\":true}\n\n"

    "════════ ФОРМАТ ОТВЕТА ════════\n"
    "Верни СТРОГО JSON-массив РОВНО той же длины и порядка, что список ниже, по одному "
    "объекту на описание, без пояснений:\n"
    "[{\"third_party\":\"yes|no|unknown\",\"scam\":false,\"trap\":false,\"any_bank\":false,\"banks\":[]}, ...]\n\n"
    "ОПИСАНИЯ:\n"
)


async def classify(texts: list[str]) -> dict[str, dict]:
    """
    Классифицирует описания. Возвращает {исходный_текст: {признаки}}.
    Пустые тексты пропускает. При выключенном/упавшем AI — то что есть в кэше.
    """
    global _LAST_CALL, _COOLDOWN_UNTIL
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

    # Бэкофф после 429 или троттл — не трогаем API, отдаём что есть в кэше
    now = time.time()
    if now < _COOLDOWN_UNTIL or (now - _LAST_CALL) < _MIN_INTERVAL:
        return out
    _LAST_CALL = now

    listing = "\n".join(f"{i + 1}. {t[:400]}" for i, t in enumerate(todo))
    try:
        raw = await gemini.ask_json(_PROMPT + listing, max_tokens=4000, timeout=12)
        if raw.startswith("❌"):
            low = raw.lower()
            if "429" in raw or "quota" in low or "exhaust" in low or "rate" in low:
                _COOLDOWN_UNTIL = time.time() + _COOLDOWN_SEC
                logger.warning("ai_desc: 429 — пауза разбора описаний на %ss", _COOLDOWN_SEC)
            else:
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
