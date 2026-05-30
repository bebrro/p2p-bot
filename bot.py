import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonCommands

from config import BOT_TOKEN, WEBAPP_URL, WEBAPP_PORT, PAYMENT_LABELS, REDIS_URL, ADMIN_CHAT_ID
from webapp.server import start_webapp
from utils.rate_limit import RateLimitMiddleware
from utils.error_reporter import setup_reporter, report as report_error
from utils.subscription import get_plan_key, PLANS

# ── FSM Storage: Redis если задан URL, иначе Memory ───────────────────────────
def _make_storage():
    if REDIS_URL:
        try:
            from aiogram.fsm.storage.redis import RedisStorage
            storage = RedisStorage.from_url(REDIS_URL)
            logging.getLogger(__name__).info(f"FSM storage: Redis ({REDIS_URL})")
            return storage
        except Exception as e:
            logging.getLogger(__name__).warning(f"Redis FSM failed ({e}), fallback to Memory")
    logging.getLogger(__name__).info("FSM storage: Memory")
    return MemoryStorage()

import os
WEBHOOK_HOST   = os.getenv("WEBHOOK_HOST",   "")   # https://example.com
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH",   "/webhook")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
from handlers import (
    start, p2p, alerts, maker, tracker, calculator,
    position_monitor, price_history, multipair,
    account_manager, auto_reprice, arbitrage, stats,
    blacklist, ad_schedule, export,
    whale_tracker, pattern_engine, ai_advisor,
    price_signal,
    subscription, pnl,
    referral, digest, admin, channel,
)
from api import binance_p2p, bybit_p2p, okx_p2p, wallet_p2p
from utils.spread import calc_spread
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def _loop(coro_fn, interval: int, label: str):
    """Универсальный фоновый цикл с Telegram-алертом при ошибке."""
    while True:
        await asyncio.sleep(interval)
        try:
            await coro_fn()
        except Exception as e:
            await report_error(f"background:{label}", e)


async def check_alerts_task(bot: Bot):
    all_alerts = await alerts.get_all_alerts()
    for user_id, user_alerts in list(all_alerts.items()):
        for alert in user_alerts:
            try:
                fiat, asset, exchange = alert["fiat"], alert["asset"], alert["exchange"]
                threshold  = alert["threshold"]
                pay        = alert.get("pay", "")
                pay_types  = [pay] if pay else None

                if exchange == "binance":
                    if pay_types:
                        b_ads = await binance_p2p.get_ads(asset=asset, fiat=fiat, trade_type="BUY",  pay_types=pay_types, rows=1)
                        s_ads = await binance_p2p.get_ads(asset=asset, fiat=fiat, trade_type="SELL", pay_types=pay_types, rows=1)
                        buy  = b_ads[0]["price"] if b_ads else None
                        sell = s_ads[0]["price"] if s_ads else None
                    else:
                        buy  = await binance_p2p.get_best_price(asset, fiat, "BUY")
                        sell = await binance_p2p.get_best_price(asset, fiat, "SELL")
                elif exchange == "bybit":
                    if pay_types:
                        b_ads = await bybit_p2p.get_ads(asset=asset, fiat=fiat, side="1", pay_types=pay_types, size=1)
                        s_ads = await bybit_p2p.get_ads(asset=asset, fiat=fiat, side="0", pay_types=pay_types, size=1)
                        buy  = b_ads[0]["price"] if b_ads else None
                        sell = s_ads[0]["price"] if s_ads else None
                    else:
                        buy  = await bybit_p2p.get_best_price(asset, fiat, "1")
                        sell = await bybit_p2p.get_best_price(asset, fiat, "0")
                elif exchange == "okx":
                    if pay_types:
                        b_ads = await okx_p2p.get_ads(asset=asset, fiat=fiat, side="buy",  pay_types=pay_types, rows=1)
                        s_ads = await okx_p2p.get_ads(asset=asset, fiat=fiat, side="sell", pay_types=pay_types, rows=1)
                        buy  = b_ads[0]["price"] if b_ads else None
                        sell = s_ads[0]["price"] if s_ads else None
                    else:
                        buy  = await okx_p2p.get_best_price(asset, fiat, "buy")
                        sell = await okx_p2p.get_best_price(asset, fiat, "sell")
                elif exchange == "wallet":
                    if pay_types:
                        b_ads = await wallet_p2p.get_ads(asset=asset, fiat=fiat, side="buy",  pay_types=pay_types, rows=1)
                        s_ads = await wallet_p2p.get_ads(asset=asset, fiat=fiat, side="sell", pay_types=pay_types, rows=1)
                        buy  = b_ads[0]["price"] if b_ads else None
                        sell = s_ads[0]["price"] if s_ads else None
                    else:
                        buy  = await wallet_p2p.get_best_price(asset, fiat, "buy")
                        sell = await wallet_p2p.get_best_price(asset, fiat, "sell")
                else:
                    buy, sell = None, None

                _EX_NAMES = {
                    "binance": "🟡 Binance",
                    "bybit":   "🟠 Bybit",
                    "okx":     "🔵 OKX",
                    "wallet":  "💎 TG Wallet",
                }
                if buy and sell:
                    s = calc_spread(buy, sell)
                    if s["spread_pct"] >= threshold:
                        ex_name  = _EX_NAMES.get(exchange, exchange.title())
                        pay_disp = PAYMENT_LABELS.get(pay, pay) if pay else ""
                        pay_str  = f" · {pay_disp}" if pay_disp else ""
                        await bot.send_message(
                            user_id,
                            f"🔔 Алерт сработал!\n{ex_name} {asset}/{fiat}{pay_str}\n"
                            f"Спред: {s['spread_pct']}% (порог {threshold}%)\n"
                            f"Купить: {buy:,.2f} | Продать: {sell:,.2f}",
                        )
            except Exception as e:
                logger.error(f"Alert error user={user_id}: {e}")


async def check_expiring_subscriptions(bot: Bot) -> None:
    """
    Раз в час проверяет подписки с истечением через 3 дня и через 1 день.
    Каждому пользователю отправляет не более одного уведомления на порог.
    """
    import db as _db
    for threshold_days, notif_type in ((3, "3d"), (1, "1d")):
        expiring = await _db.subscriptions_expiring_soon(days=threshold_days)
        for row in expiring:
            uid      = row["user_id"]
            plan_key = row["plan"]
            days_left = max(1, int(row["days_left"] or 1))

            if await _db.expiry_notif_sent(uid, notif_type):
                continue

            plan = PLANS.get(plan_key, {})
            name = plan.get("name", plan_key.title())

            try:
                await bot.send_message(
                    uid,
                    f"⏰ <b>Подписка истекает через {days_left} {'день' if days_left == 1 else 'дня'}!</b>\n\n"
                    f"Твой план <b>{name}</b> закончится {row['expires_at'].strftime('%d.%m.%Y')}.\n\n"
                    "Продли сейчас — все настройки и алерты сохранятся.\n\n"
                    "👇 Нажми чтобы продлить:",
                    reply_markup=__import__("aiogram").types.InlineKeyboardMarkup(
                        inline_keyboard=[[
                            __import__("aiogram").types.InlineKeyboardButton(
                                text="⭐ Продлить подписку",
                                callback_data="sub:list",
                            )
                        ]]
                    ),
                    parse_mode="HTML",
                )
                await _db.expiry_notif_mark(uid, notif_type)
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning(f"Expiry notify uid={uid}: {e}")


async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан!")
        return

    # ── База данных ────────────────────────────────────────────────────────────
    await db.init()
    if db.ok():
        await blacklist.load_from_db()          # ЧС в память
        await account_manager.load_from_db()    # API-аккаунты (Bybit + OKX)
        await auto_reprice.load_from_db()       # Правила репрайсера
        await arbitrage.load_from_db()          # Арбитражные алерты

        # Владельцам (ADMIN_IDS) — пожизненный Team при старте
        from config import ADMIN_IDS
        for owner_id in ADMIN_IDS:
            sub = await db.subscription_get(owner_id)
            if not (sub and sub.get("plan") == "team" and sub.get("expires_at") is None):
                await db.subscription_set(owner_id, "team", None)
                logger.info(f"Owner lifetime Team granted at startup: uid={owner_id}")

    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=_make_storage())

    # Telegram-алерты об ошибках → личка администратора
    setup_reporter(bot, ADMIN_CHAT_ID)

    # Rate limiting — защита от спама
    _rl = RateLimitMiddleware(rate=10, period=3.0)
    dp.message.middleware(_rl)
    dp.callback_query.middleware(RateLimitMiddleware(rate=20, period=3.0))

    # ── Команды (кнопка ☰ рядом с полем ввода) ────────────────────────────
    await bot.set_my_commands([
        BotCommand(command="menu",        description="📱 Главное меню"),
        BotCommand(command="p2p",         description="📊 P2P курсы — все биржи"),
        BotCommand(command="alerts",      description="🔔 Алерты на спред"),
        BotCommand(command="ai",          description="🤖 AI Советник"),
        BotCommand(command="pnl",         description="📊 P&L трекер (Pro/Team)"),
        BotCommand(command="calc",        description="🧮 Калькулятор прибыли"),
        BotCommand(command="subscribe",   description="⭐ Подписка и тарифы"),
        BotCommand(command="ref",         description="👥 Реферальная программа"),
        BotCommand(command="digest",      description="☀️ Утренний дайджест"),
        BotCommand(command="whale",       description="🐋 Whale Tracker"),
        BotCommand(command="stopwhale",   description="🛑 Остановить Whale Tracker"),
        BotCommand(command="pay_confirm", description="💳 Подтвердить оплату USDT"),
    ])
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    for r in [
        admin.router, channel.router,              # админ-команды первыми
        subscription.router, pnl.router,           # монетизация и P&L
        start.router, maker.router, tracker.router,
        position_monitor.router, calculator.router,
        multipair.router, price_history.router,
        account_manager.router, auto_reprice.router,
        arbitrage.router, stats.router,
        blacklist.router, ad_schedule.router, export.router,
        whale_tracker.router, pattern_engine.router, ai_advisor.router,
        price_signal.router,
        referral.router,
        p2p.router, alerts.router,   # p2p последним (широкие фильтры)
    ]:
        dp.include_router(r)

    # Фоновые задачи
    asyncio.create_task(_loop(lambda: check_alerts_task(bot),                  60,   "alerts"))
    asyncio.create_task(_loop(lambda: tracker.check_trackers(bot),            300,   "trackers"))
    asyncio.create_task(_loop(lambda: position_monitor.check_positions(bot),  300,   "positions"))
    asyncio.create_task(_loop(price_history.collect_prices,                  1800,   "price_history"))
    asyncio.create_task(_loop(lambda: auto_reprice.run_repricer(bot),          60,   "repricer"))
    asyncio.create_task(_loop(lambda: arbitrage.check_arbitrage(bot),         120,   "arbitrage"))
    asyncio.create_task(_loop(lambda: ad_schedule.run_schedule(bot),           60,   "schedule"))
    asyncio.create_task(_loop(lambda: whale_tracker.check_whales(bot),        120,  "whales"))
    asyncio.create_task(_loop(lambda: digest.send_daily_digest(bot),           60,  "digest"))
    asyncio.create_task(_loop(lambda: check_expiring_subscriptions(bot),    3600,  "expiry"))
    asyncio.create_task(_loop(lambda: subscription.check_winback(bot),      3600,  "winback"))
    asyncio.create_task(_loop(lambda: channel.channel_scheduler(bot),         60,  "channel"))

    if WEBHOOK_HOST:
        # ── Webhook режим (production) ─────────────────────────────────────────
        webhook_url = f"{WEBHOOK_HOST.rstrip('/')}{WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            drop_pending_updates=True,
        )
        await start_webapp(
            port=WEBAPP_PORT,
            webhook_path=WEBHOOK_PATH,
            dp=dp,
            bot=bot,
            webhook_secret=WEBHOOK_SECRET or None,
        )
        logger.info(f"Бот запущен ✅ (Webhook: {webhook_url})")
        await asyncio.Event().wait()   # держим event loop живым
    else:
        # ── Polling режим (разработка) ─────────────────────────────────────────
        await bot.delete_webhook(drop_pending_updates=True)
        await start_webapp(port=WEBAPP_PORT)
        if WEBAPP_URL:
            logger.info(f"Mini App: {WEBAPP_URL}")
        else:
            logger.info(f"Mini App server :{WEBAPP_PORT}  |  ngrok http {WEBAPP_PORT}")
        logger.info("Бот запущен ✅ (Polling)")
        await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
