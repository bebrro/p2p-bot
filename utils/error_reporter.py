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
import time
import traceback
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_bot      = None
_chat_id  = 0

# ── Антифлуд ──────────────────────────────────────────────────────────────────
# Одинаковая ошибка (контекст+тип) — не чаще раза в 10 мин. Любые ошибки —
# не чаще раза в 15с. Иначе при сбое БД 14 фоновых задач завалят личку.
_COOLDOWN   = 600.0      # сек: повтор одной и той же ошибки
_MIN_GAP    = 15.0       # сек: минимальный зазор между ЛЮБЫМИ алертами
_last_sent: dict[str, float] = {}
_last_any   = 0.0
_suppressed: dict[str, int] = {}   # сколько раз подавили (покажем в след. алерте)


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

    # ── Антифлуд: дедуп по (контекст+тип) + глобальный зазор ──────────────────
    global _last_any
    key = f"{context}:{type(error).__name__}"
    now = time.monotonic()
    if now - _last_sent.get(key, -1e9) < _COOLDOWN:
        _suppressed[key] = _suppressed.get(key, 0) + 1
        return                              # такое уже слали недавно — молчим
    if now - _last_any < _MIN_GAP:
        _suppressed[key] = _suppressed.get(key, 0) + 1
        return                              # слишком частим вообще — притормозим
    _last_sent[key] = now
    _last_any = now
    skipped = _suppressed.pop(key, 0)

    try:
        ts  = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        extra = f"\n<i>(+{skipped} таких же подавлено за последние 10 мин)</i>" if skipped else ""
        msg = (
            f"🚨 <b>Ошибка бота</b> · {ts}\n\n"
            f"<b>Контекст:</b> <code>{context}</code>\n"
            f"<b>Ошибка:</b> <code>{type(error).__name__}: {str(error)[:300]}</code>{extra}\n\n"
            f"<pre>{tb[-1400:]}</pre>"
        )
        await _bot.send_message(_chat_id, msg, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error reporter send failed: {e}")
