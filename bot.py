import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonCommands
from config import BOT_TOKEN, WEBAPP_URL, WEBAPP_PORT
from webapp.server import start_webapp
from handlers import (
    start, p2p, alerts, maker, tracker, calculator,
    position_monitor, price_history, multipair,
    account_manager, auto_reprice, arbitrage, stats,
    blacklist, ad_schedule, export,
    whale_tracker, pattern_engine, ai_advisor,
)
from api import binance_p2p, bybit_p2p
from utils.spread import calc_spread

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def _loop(coro_fn, interval: int, label: str):
    """Универсальный фоновый цикл."""
    while True:
        await asyncio.sleep(interval)
        try:
            await coro_fn()
        except Exception as e:
            logger.error(f"{label} error: {e}")


async def check_alerts_task(bot: Bot):
    all_alerts = alerts.get_all_alerts()
    for user_id, user_alerts in list(all_alerts.items()):
        for alert in user_alerts:
            try:
                fiat, asset, exchange = alert["fiat"], alert["asset"], alert["exchange"]
                threshold = alert["threshold"]
                if exchange == "binance":
                    buy  = await binance_p2p.get_best_price(asset, fiat, "BUY")
                    sell = await binance_p2p.get_best_price(asset, fiat, "SELL")
                else:
                    buy  = await bybit_p2p.get_best_price(asset, fiat, "1")
                    sell = await bybit_p2p.get_best_price(asset, fiat, "0")
                if buy and sell:
                    s = calc_spread(buy, sell)
                    if s["spread_pct"] >= threshold:
                        ex = "🟡 Binance" if exchange == "binance" else "🟠 Bybit"
                        await bot.send_message(
                            user_id,
                            f"🔔 Алерт сработал!\n{ex} {asset}/{fiat}\n"
                            f"Спред: {s['spread_pct']}% (порог {threshold}%)\n"
                            f"Купить: {buy:,.2f} | Продать: {sell:,.2f}",
                        )
            except Exception as e:
                logger.error(f"Alert error user={user_id}: {e}")


async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан!")
        return

    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())

    # ── Команды (кнопка ☰ рядом с полем ввода) ────────────────────────────
    await bot.set_my_commands([
        BotCommand(command="start",  description="📱 Главное меню"),
        BotCommand(command="p2p",    description="📊 P2P курсы"),
        BotCommand(command="calc",   description="🧮 Калькулятор прибыли"),
        BotCommand(command="whale",  description="🐋 Whale Tracker"),
        BotCommand(command="ai",     description="🤖 AI Советник"),
        BotCommand(command="alerts", description="🔔 Алерты на спред"),
    ])
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    for r in [
        start.router, maker.router, tracker.router,
        position_monitor.router, calculator.router,
        multipair.router, price_history.router,
        account_manager.router, auto_reprice.router,
        arbitrage.router, stats.router,
        blacklist.router, ad_schedule.router, export.router,
        whale_tracker.router, pattern_engine.router, ai_advisor.router,
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

    # Запускаем Mini App веб-сервер (aiohttp, тот же event loop)
    await start_webapp(port=WEBAPP_PORT)
    if WEBAPP_URL:
        logger.info(f"Mini App: {WEBAPP_URL}")
    else:
        logger.info(f"Mini App server on :{WEBAPP_PORT} (запусти ngrok http {WEBAPP_PORT}, добавь URL в .env)")

    logger.info("Бот запущен ✅")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
