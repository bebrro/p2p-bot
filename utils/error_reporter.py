"""
Telegram-алерты об ошибках → пишет в личку администратору.

Никакого Sentry, никакого внешнего сервиса — просто сообщение в Telegram.

Использование:
    # в bot.py при старте:
    from utils.error_reporter import setup_reporter
    setup_reporter(bot, admin_chat_id)

    # в любом месте кода:
    from utils.error_reporter import report
    await report("alerts task", exc)
"""
import logging
import traceback
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_bot      = None
_chat_id  = 0


def setup_reporter(bot, admin_chat_id: int) -> None:
    """Вызвать один раз при старте, до запуска polling/webhook."""
    global _bot, _chat_id
    _bot     = bot
    _chat_id = admin_chat_id
    if admin_chat_id:
        logger.info(f"Error reporter → Telegram {admin_chat_id}")
    else:
        logger.warning("Error reporter: ADMIN_CHAT_ID не задан — алерты выключены")


async def report(context: str, error: Exception) -> None:
    """
    Отправляет сообщение об ошибке администратору.
    Если репортер не настроен — только логирует.
    """
    tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    logger.error(f"[{context}] {error}\n{tb}")

    if not _bot or not _chat_id:
        return
    try:
        ts  = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        msg = (
            f"🚨 <b>Ошибка бота</b> · {ts}\n\n"
            f"<b>Контекст:</b> <code>{context}</code>\n"
            f"<b>Ошибка:</b> <code>{type(error).__name__}: {str(error)[:300]}</code>\n\n"
            f"<pre>{tb[-1500:]}</pre>"
        )
        await _bot.send_message(_chat_id, msg, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error reporter send failed: {e}")
