"""
Подтягивание УСЛОВИЙ (remark/terms) объявлений Binance через залогиненную
БЁРНЕР-сессию. Публичный search условия не отдаёт — они за логином.

⚠️ БЕЗОПАСНОСТЬ (read-only, минимальный риск):
  • Используется ОТДЕЛЬНЫЙ левый аккаунт (НЕ основной). Им НИКОГДА не торгуем
    и не выводим — только читаем.
  • Тянем detail ТОЛЬКО для топ-объявлений и ног связки (≈5 на экран), а не
    для всего стакана — низкий RPS, не палимся.
  • Жёсткий кэш по advNo (условия меняются редко) → почти нет повторных запросов.
  • Троттл + бэкофф при 401/429/«робот»: при бане сессии фича засыпает и
    отдаёт пусто, бот продолжает работать как раньше (description = "").
  • Куки/заголовки сессии — ТОЛЬКО в env (Railway), НИКОГДА в коде/гите.

Настройка (после перехвата cURL залогиненного detail-запроса):
  BINANCE_DETAIL_URL   — точный URL эндпоинта
  BINANCE_P2P_COOKIE   — строка Cookie из запроса
  BINANCE_P2P_CSRF     — значение заголовка csrftoken (если есть)
  BINANCE_DETAIL_KEY   — имя поля advNo в payload (adNo|advNo|advertNo), деф. adNo
Пусто → фича спит.
"""
import os
import time
import json
import logging
import asyncio

import aiohttp

logger = logging.getLogger(__name__)

_CACHE: dict[str, str] = {}          # advNo -> remark ("" = условий нет)
_CACHE_MAX = 5000

_COOLDOWN_UNTIL = 0.0
_COOLDOWN_SEC = 300                   # 5 мин паузы при бане/лимите
_LAST_CALL = 0.0
_MIN_INTERVAL = 0.6                   # не чаще ~1.6 запроса/сек


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def enabled() -> bool:
    """Фича включена только если задан URL и кука бёрнер-сессии."""
    return bool(_env("BINANCE_DETAIL_URL") and _env("BINANCE_P2P_COOKIE"))


def _headers() -> dict:
    h = {
        "Content-Type": "application/json",
        "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "clientType":   "web",
        "Accept":       "*/*",
        "Origin":       "https://p2p.binance.com",
        "Referer":      "https://p2p.binance.com/",
        "Cookie":       _env("BINANCE_P2P_COOKIE"),
    }
    csrf = _env("BINANCE_P2P_CSRF")
    if csrf:
        h["csrftoken"] = csrf
    return h


def _find_remark(obj) -> str:
    """
    Рекурсивно ищет в ответе поле с условиями. Имена у Binance плавают
    (remark / adRemark / remarks / tradeInstruction), поэтому ищем по ключу.
    Возвращает первую непустую строку, иначе "".
    """
    KEYS = ("remark", "adremark", "remarks", "tradeinstruction", "advremark")
    found = ""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if isinstance(v, str) and v.strip() and k.lower() in KEYS:
                    return v.strip()
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return found


async def get_remark(adv_no: str, session: aiohttp.ClientSession | None = None) -> str | None:
    """
    Возвращает текст условий объявления по advNo (или "" если их нет).
    None — если фича выключена / сессия забанена / ошибка (бот работает как обычно).
    """
    global _LAST_CALL, _COOLDOWN_UNTIL
    if not adv_no:
        return None
    if adv_no in _CACHE:
        return _CACHE[adv_no]
    if not enabled():
        return None

    now = time.time()
    if now < _COOLDOWN_UNTIL:
        return None
    # мягкий троттл (между запросами в рамках одного экрана)
    wait = _MIN_INTERVAL - (now - _LAST_CALL)
    if wait > 0:
        await asyncio.sleep(wait)
    _LAST_CALL = time.time()

    url = _env("BINANCE_DETAIL_URL")
    key = _env("BINANCE_DETAIL_KEY") or "adNo"
    payload = {key: adv_no}

    own = session is None
    if own:
        session = aiohttp.ClientSession()
    try:
        async with session.post(url, json=payload, headers=_headers(),
                                timeout=aiohttp.ClientTimeout(total=12)) as r:
            text = await r.text()
            if r.status in (401, 403):
                _COOLDOWN_UNTIL = time.time() + _COOLDOWN_SEC
                logger.warning("binance_detail: %s — сессия невалидна, пауза %ss",
                               r.status, _COOLDOWN_SEC)
                return None
            if r.status == 429 or "robot" in text.lower() or "verify" in text.lower():
                _COOLDOWN_UNTIL = time.time() + _COOLDOWN_SEC
                logger.warning("binance_detail: 429/robot — пауза %ss", _COOLDOWN_SEC)
                return None
            data = json.loads(text)
    except Exception as e:
        logger.warning("binance_detail: %s", e)
        return None
    finally:
        if own:
            await session.close()

    remark = _find_remark(data)
    _CACHE[adv_no] = remark            # кэшируем и пустоту — не дёргаем повторно
    if len(_CACHE) > _CACHE_MAX:
        for k in list(_CACHE)[: len(_CACHE) - _CACHE_MAX]:
            _CACHE.pop(k, None)
    return remark


async def fill_remarks(ads: list, limit: int = 6) -> None:
    """
    Дозаполняет description у первых `limit` объявлений (топ по цене + ноги связки
    идут первыми). Делает это ПОСЛЕДОВАТЕЛЬНО (троттл), мягко — не блокирует
    основной поток надолго. Уже кэшированные advNo берутся мгновенно.
    """
    if not enabled():
        return
    done = 0
    async with aiohttp.ClientSession() as s:
        for ad in ads:
            if done >= limit:
                break
            if ad.get("description"):          # уже есть (или с другой биржи)
                continue
            adv_no = ad.get("ad_no") or ad.get("advNo")
            if not adv_no:
                continue
            rm = await get_remark(adv_no, session=s)
            if rm:
                ad["description"] = rm
            done += 1
