"""
Ежедневный дайджест — утренняя рассылка.

Время: 06:00 UTC = 09:00 МСК = 12:00 Алматы
Отправляет: лучший спред по фиатам + лучший арбитраж.
Пользователь может отключить через /digest.
"""
import asyncio
import logging
from datetime import datetime, timezone, date

from aiogram import Bot

import db
from api import binance_p2p, bybit_p2p, okx_p2p, wallet_p2p
from utils.spread import calc_spread
from config import FIATS

logger = logging.getLogger(__name__)

DIGEST_HOUR_UTC = 6   # 06:00 UTC = 09:00 МСК

# Защита от двойной отправки
_last_digest_date: date | None = None


# ─── Сбор данных ──────────────────────────────────────────────────────────────

async def _best_spread_for_fiat(fiat: str) -> dict | None:
    """Лучший спред USDT/{fiat} по всем 4 биржам."""
    try:
        results = await asyncio.gather(
            binance_p2p.get_best_price("USDT", fiat, "BUY"),
            binance_p2p.get_best_price("USDT", fiat, "SELL"),
            bybit_p2p.get_best_price("USDT", fiat, "1"),
            bybit_p2p.get_best_price("USDT", fiat, "0"),
            okx_p2p.get_best_price("USDT", fiat, "buy"),
            okx_p2p.get_best_price("USDT", fiat, "sell"),
            wallet_p2p.get_best_price("USDT", fiat, "buy"),
            wallet_p2p.get_best_price("USDT", fiat, "sell"),
            return_exceptions=True,
        )

        def _v(x): return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None

        buys  = [_v(results[i]) for i in (0, 2, 4, 6)]
        sells = [_v(results[i]) for i in (1, 3, 5, 7)]

        valid_buys  = [b for b in buys  if b]
        valid_sells = [s for s in sells if s]
        if not valid_buys or not valid_sells:
            return None

        best_buy  = min(valid_buys)
        best_sell = max(valid_sells)
        sp = calc_spread(best_buy, best_sell)
        return {"fiat": fiat, "buy": best_buy, "sell": best_sell,
                "spread_pct": sp["spread_pct"]}
    except Exception:
        return None


async def _best_arbitrage() -> dict | None:
    """Лучший арбитраж среди всех фиатов."""
    results = await asyncio.gather(*[_best_spread_for_fiat(f) for f in FIATS])
    valids  = [r for r in results if r]
    if not valids:
        return None
    return max(valids, key=lambda x: x["spread_pct"])


# ─── Формирование сообщения ────────────────────────────────────────────────────

async def _build_digest_text() -> str:
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%d.%m.%Y")

    lines = [f"☀️ <b>Доброе утро! P2P рынок — {date_str}</b>\n"]

    # Спред по каждому фиату
    fiat_icons = {"KZT": "🇰🇿", "RUB": "🇷🇺", "TRY": "🇹🇷", "USD": "🇺🇸"}
    spreads = await asyncio.gather(*[_best_spread_for_fiat(f) for f in FIATS])

    lines.append("<b>Спред сегодня:</b>")
    best_overall = None
    for sp in spreads:
        if not sp:
            continue
        icon = fiat_icons.get(sp["fiat"], "")
        pct  = sp["spread_pct"]
        emoji = "🔥" if pct >= 1.5 else ("✅" if pct >= 0.8 else "➖")
        lines.append(
            f"  {icon} USDT/{sp['fiat']}: <b>{pct:.2f}%</b> {emoji}  "
            f"({sp['buy']:,.0f} → {sp['sell']:,.0f})"
        )
        if best_overall is None or pct > best_overall["spread_pct"]:
            best_overall = sp

    if best_overall:
        lines.append(
            f"\n🏆 <b>Лучшая пара: USDT/{best_overall['fiat']} — {best_overall['spread_pct']:.2f}%</b>"
        )

    lines.append(
        "\n💡 <i>Открой бота чтобы поймать момент!</i>\n"
        "/p2p · /alerts · арбитраж"
    )

    return "\n".join(lines)


# ─── Рассылка ─────────────────────────────────────────────────────────────────

async def send_daily_digest(bot: Bot) -> None:
    """
    Вызывается каждую минуту из bot.py.
    Отправляет дайджест один раз в день в DIGEST_HOUR_UTC.
    """
    global _last_digest_date

    now = datetime.now(timezone.utc)
    if now.hour != DIGEST_HOUR_UTC:
        return
    if _last_digest_date == now.date():
        return   # сегодня уже отправляли

    _last_digest_date = now.date()
    logger.info(f"Sending daily digest ({now.date()})")

    try:
        text = await _build_digest_text()
    except Exception as e:
        logger.error(f"Digest build error: {e}")
        return

    recipients = await db.users_get_digest_recipients()
    sent = 0
    for uid in recipients:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)   # ~20 msg/sec — не попасть под flood
        except Exception:
            pass   # заблокировал бота или другая ошибка — пропускаем

    logger.info(f"Digest sent: {sent}/{len(recipients)} users")
