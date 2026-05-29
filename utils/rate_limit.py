"""
Простой rate limiter для Telegram бота.
Защищает от спама — не более N запросов за период T.

По умолчанию: 10 запросов за 3 секунды на пользователя.
Спамящий запрос молча игнорируется.
"""
import time
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject

logger = logging.getLogger(__name__)


class RateLimitMiddleware(BaseMiddleware):
    """
    Подключить:
        dp.message.middleware(RateLimitMiddleware())
        dp.callback_query.middleware(RateLimitMiddleware(rate=15))
    """

    def __init__(self, rate: int = 10, period: float = 3.0) -> None:
        """
        rate   — максимум запросов за period секунд
        period — окно в секундах
        """
        super().__init__()
        self.rate   = rate
        self.period = period
        # {user_id: [timestamp, ...]}
        self._calls: dict[int, list[float]] = defaultdict(list)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Получаем user_id из события
        user = None
        if isinstance(event, (Message, CallbackQuery)):
            user = event.from_user

        if user is None:
            return await handler(event, data)

        uid = user.id
        now = time.monotonic()

        # Убираем устаревшие вызовы
        calls = self._calls[uid]
        self._calls[uid] = [t for t in calls if now - t < self.period]

        if len(self._calls[uid]) >= self.rate:
            # Спам — тихо игнорируем, для callback_query можно ответить
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("⏳ Слишком много запросов. Подожди секунду.", show_alert=False)
                except Exception:
                    pass
            logger.debug(f"Rate limit: uid={uid} ({len(self._calls[uid])}/{self.rate} за {self.period}s)")
            return  # не передаём дальше

        self._calls[uid].append(now)
        return await handler(event, data)
