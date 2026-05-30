"""
Единый HTTP клиент с автоматическим retry и общим пулом соединений.

Поведение:
  • HTTP 429 (rate limit) → ждём и повторяем до MAX_RETRIES раз
  • HTTP 5xx            → то же самое
  • Timeout             → retry
  • Другие ошибки       → бросаем исключение сразу

Задержки между попытками: 1s → 3s → 7s  (экспоненциальный backoff)

Производительность:
  Используется ОДНА общая aiohttp.ClientSession на весь процесс.
  Это переиспользует TCP/TLS соединения (keep-alive) и DNS-кэш —
  каждый запрос не платит заново за хендшейк. Сессия создаётся лениво
  в текущем event loop при первом запросе.
"""
import asyncio
import logging
import aiohttp

logger = logging.getLogger(__name__)

_RETRY_DELAYS  = (1.0, 3.0, 7.0)   # секунды между попытками
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_TIMEOUT        = aiohttp.ClientTimeout(total=10)

# ── Общая сессия на весь процесс ───────────────────────────────────────────────
_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    """Лениво создаёт/возвращает общую сессию с пулом соединений."""
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(
            limit=100,            # всего одновременных соединений
            limit_per_host=20,    # на один хост (биржу)
            ttl_dns_cache=300,    # кэш DNS 5 мин
            enable_cleanup_closed=True,
        )
        _session = aiohttp.ClientSession(connector=connector)
        logger.info("HTTP: создана общая ClientSession (keep-alive пул)")
    return _session


async def close_session() -> None:
    """Закрывает общую сессию (вызывать при graceful shutdown)."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


async def get_json(url: str, *,
                   headers: dict = None,
                   params:  dict = None,
                   timeout: aiohttp.ClientTimeout = None) -> dict:
    """GET-запрос с retry. Возвращает dict или {} при полном провале."""
    to = timeout or _TIMEOUT
    for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
        try:
            s = _get_session()
            async with s.get(url, headers=headers, params=params, timeout=to) as r:
                if r.status in _RETRY_STATUSES and delay is not None:
                    logger.warning(f"GET {url} → HTTP {r.status}, retry in {delay}s (attempt {attempt+1})")
                    await asyncio.sleep(delay)
                    continue
                return await r.json(content_type=None)
        except asyncio.TimeoutError:
            if delay is not None:
                logger.warning(f"GET {url} → timeout, retry in {delay}s")
                await asyncio.sleep(delay)
                continue
            logger.error(f"GET {url} → timeout after all retries")
        except Exception as e:
            logger.error(f"GET {url} → {e}")
            raise
    return {}


async def post_json(url: str, *,
                    json:    dict = None,
                    data:    str  = None,
                    headers: dict = None,
                    timeout: aiohttp.ClientTimeout = None) -> dict:
    """POST-запрос с retry. Возвращает dict или {} при полном провале."""
    to = timeout or _TIMEOUT
    for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
        try:
            s = _get_session()
            async with s.post(url, json=json, data=data,
                              headers=headers, timeout=to) as r:
                if r.status in _RETRY_STATUSES and delay is not None:
                    logger.warning(f"POST {url} → HTTP {r.status}, retry in {delay}s (attempt {attempt+1})")
                    await asyncio.sleep(delay)
                    continue
                return await r.json(content_type=None)
        except asyncio.TimeoutError:
            if delay is not None:
                logger.warning(f"POST {url} → timeout, retry in {delay}s")
                await asyncio.sleep(delay)
                continue
            logger.error(f"POST {url} → timeout after all retries")
        except Exception as e:
            logger.error(f"POST {url} → {e}")
            raise
    return {}
