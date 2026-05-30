"""
Автопостинг арбитража в Telegram-канал — маркетинг + трафик + соц. доказательство.

Логика:
• Несколько раз в день бот сам публикует в канал лучшие арбитражные
  возможности по всем фиатам (живые данные, де-байтенные цены).
• Канал = «SEO» Telegram-бота: индексируется, шарится, гонит подписчиков.

Настройка:
• CHANNEL_ID в Railway (напр. @my_p2p_channel). Пусто = функция спит.
• Бот должен быть АДМИНОМ канала с правом публикации.
• /post_now — ручная публикация (для теста), только ADMIN_IDS.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta, date

from aiogram import Router, Bot
from aiogram.filters import Command
from aiogram.types import Message

from api import binance_p2p, bybit_p2p, okx_p2p, wallet_p2p
from utils.spread import calc_spread
from config import (
    FIATS, CHANNEL_ID, CHANNEL_POST_HOURS, ADMIN_IDS,
    DISABLED_EXCHANGES, SUSPICIOUS_SPREAD_PCT,
)

logger = logging.getLogger(__name__)
router = Router()

_FIAT_ICON = {
    "KZT": "🇰🇿", "RUB": "🇷🇺", "TRY": "🇹🇷", "USD": "🇺🇸",
    "THB": "🇹🇭", "IDR": "🇮🇩", "VND": "🇻🇳", "INR": "🇮🇳", "AED": "🇦🇪",
    "NGN": "🇳🇬", "BRL": "🇧🇷", "GEL": "🇬🇪", "AMD": "🇦🇲", "AZN": "🇦🇿",
    "UZS": "🇺🇿", "KGS": "🇰🇬",
}

# (имя, fn_buy, fn_sell, id) — конвенции сторон у бирж разные
_POST_EX = [
    ("🟡 Binance",   lambda a, f: binance_p2p.get_best_price(a, f, "BUY"),
                     lambda a, f: binance_p2p.get_best_price(a, f, "SELL"), "binance"),
    ("🟠 Bybit",     lambda a, f: bybit_p2p.get_best_price(a, f, "1"),
                     lambda a, f: bybit_p2p.get_best_price(a, f, "0"), "bybit"),
    ("🔵 OKX",       lambda a, f: okx_p2p.get_best_price(a, f, "buy"),
                     lambda a, f: okx_p2p.get_best_price(a, f, "sell"), "okx"),
    ("💎 TG Wallet", lambda a, f: wallet_p2p.get_best_price(a, f, "buy"),
                     lambda a, f: wallet_p2p.get_best_price(a, f, "sell"), "wallet"),
]

# Дедуп автопостинга: (date, hour) последнего поста
_last_post: tuple | None = None


def _v(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


async def _best_for_fiat(fiat: str, asset: str = "USDT") -> dict | None:
    """Лучший кросс-биржевой арбитраж по одному фиату (среди живых бирж)."""
    tasks, names = [], []
    for name, buy_fn, sell_fn, ex_id in _POST_EX:
        if ex_id in DISABLED_EXCHANGES:
            continue
        names.append(name)
        tasks.append(buy_fn(asset, fiat))
        tasks.append(sell_fn(asset, fiat))

    res = await asyncio.gather(*tasks, return_exceptions=True)

    buys, sells = [], []
    for i, name in enumerate(names):
        b, s = _v(res[i * 2]), _v(res[i * 2 + 1])
        if b:
            buys.append((name, b))
        if s:
            sells.append((name, s))
    if not buys or not sells:
        return None

    buy_ex,  buy_p  = min(buys,  key=lambda x: x[1])   # купить дешевле
    sell_ex, sell_p = max(sells, key=lambda x: x[1])   # продать дороже
    sp = calc_spread(buy_p, sell_p)
    return {
        "fiat": fiat, "buy_ex": buy_ex, "buy": buy_p,
        "sell_ex": sell_ex, "sell": sell_p, "pct": sp["spread_pct"],
    }


def _format_post(rows: list[dict], uname: str = "") -> str | None:
    """Чистое форматирование поста из готовых строк арбитража."""
    rows = [r for r in rows if r and r.get("pct", 0) > 0]
    if not rows:
        return None
    rows.sort(key=lambda x: -x["pct"])

    msk = datetime.now(timezone.utc) + timedelta(hours=3)
    lines = [f"🔥 <b>P2P Арбитраж USDT</b>\n🕐 {msk.strftime('%d.%m %H:%M')} МСК\n"]

    has_susp = False
    for r in rows[:4]:
        icon = _FIAT_ICON.get(r["fiat"], "")
        warn = ""
        if r["pct"] > SUSPICIOUS_SPREAD_PCT:
            warn = " ⚠️"
            has_susp = True
        lines.append(
            f"{icon} <b>USDT/{r['fiat']}</b>{warn}\n"
            f"  📥 Купить {r['buy_ex']} — <code>{r['buy']:,.2f}</code>\n"
            f"  📤 Продать {r['sell_ex']} — <code>{r['sell']:,.2f}</code>\n"
            f"  💰 Спред: <b>+{r['pct']:.2f}%</b>\n"
        )

    best = rows[0]
    lines.append(f"🏆 Лучшее сейчас: <b>USDT/{best['fiat']} +{best['pct']:.2f}%</b>")
    if has_susp:
        lines.append("⚠️ — проверь ликвидность (тонкий рынок)")

    lines.append(
        f"\n📲 Живой стакан 4 бирж, алерты на спред и авто-репрайсер:\n"
        f"👉 {uname or 'открой бота'}"
    )
    return "\n".join(lines)


async def build_channel_post(bot: Bot | None = None) -> str | None:
    """Собирает данные по всем фиатам и формирует текст поста. None если пусто."""
    results = await asyncio.gather(
        *[_best_for_fiat(f) for f in FIATS], return_exceptions=True
    )
    rows = [r for r in results if isinstance(r, dict)]

    uname = ""
    if bot:
        try:
            me = await bot.get_me()
            uname = f"@{me.username}"
        except Exception:
            pass
    return _format_post(rows, uname)


async def post_to_channel(bot: Bot) -> bool:
    """Публикует пост в канал. False если спит/нет данных/ошибка."""
    if not CHANNEL_ID:
        return False
    text = await build_channel_post(bot)
    if not text:
        return False
    try:
        await bot.send_message(
            CHANNEL_ID, text, parse_mode="HTML", disable_web_page_preview=True
        )
        logger.info(f"Channel post published to {CHANNEL_ID}")
        return True
    except Exception as e:
        logger.warning(f"Channel post failed ({CHANNEL_ID}): {e}")
        return False


async def channel_scheduler(bot: Bot) -> None:
    """
    Вызывать из bot.py раз в ~минуту. Постит в CHANNEL_POST_HOURS (UTC),
    не чаще одного раза в час (дедуп).
    """
    global _last_post
    if not CHANNEL_ID:
        return
    now = datetime.now(timezone.utc)
    if now.hour not in CHANNEL_POST_HOURS:
        return
    key = (now.date(), now.hour)
    if _last_post == key:
        return
    _last_post = key
    await post_to_channel(bot)


# ─── /post_now — ручная публикация (тест) ─────────────────────────────────────

@router.message(Command("post_now"))
async def post_now_cmd(message: Message, bot: Bot):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not CHANNEL_ID:
        await message.answer(
            "📢 <b>Автопостинг настроен, но спит.</b>\n\n"
            "Чтобы включить:\n"
            "1. Создай Telegram-канал\n"
            "2. Добавь бота в админы канала (право «Публикация»)\n"
            "3. Railway → Variables → <code>CHANNEL_ID</code> = "
            "<code>@твой_канал</code>\n\n"
            "После этого бот будет постить арбитраж 3 раза в день автоматически "
            "(09/15/21 МСК), а /post_now опубликует прямо сейчас.",
            parse_mode="HTML",
        )
        return

    wait = await message.answer("⏳ Собираю данные и публикую...")
    ok = await post_to_channel(bot)
    if ok:
        await wait.edit_text(f"✅ Опубликовано в {CHANNEL_ID}")
    else:
        await wait.edit_text(
            f"❌ Не удалось опубликовать в {CHANNEL_ID}.\n\n"
            "Проверь:\n"
            "• Бот — админ канала с правом публикации\n"
            "• CHANNEL_ID правильный (@username или -100...)\n"
            "• Биржи отдают данные (/health)"
        )
