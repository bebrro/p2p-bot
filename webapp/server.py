"""
Mini App HTTP-сервер (aiohttp.web).
Запускается внутри того же event loop что и бот — никаких лишних потоков.

Эндпоинты:
  GET  /                                    → index.html
  GET  /api/orderbook/{exchange}/{fiat}/{asset}
  GET  /api/history/{exchange}/{fiat}/{asset}
  POST /api/ai                              body: {exchange, fiat, asset}
"""
import asyncio
import logging
from pathlib import Path

from aiohttp import web

from api import binance_p2p, bybit_p2p, gemini
from handlers.price_history import get_history
from handlers.pattern_engine import _compute_patterns
from handlers.ai_advisor import _build_prompt
from utils.spread import calc_spread
from utils.scam_detector import risk_score, risk_badge, risk_tooltip

logger     = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _enrich(ads: list) -> list:
    """Добавляет поля риска и нормализует completion."""
    for ad in ads:
        ad["risk_score"]   = risk_score(ad)
        ad["risk_badge"]   = risk_badge(ad)
        ad["risk_tooltip"] = risk_tooltip(ad)
        c = ad.get("completion", 0)
        if isinstance(c, float) and 0 < c <= 1:
            ad["completion"] = round(c * 100, 1)
        ad["pay_types"] = list(dict.fromkeys(ad.get("pay_types", [])))
    return ads


async def _fetch(exchange: str, fiat: str, asset: str, side: str, rows: int = 10):
    if exchange == "binance":
        trade_type = "BUY" if side == "buy" else "SELL"
        return await binance_p2p.get_ads(asset=asset, fiat=fiat, trade_type=trade_type, rows=rows)
    else:
        bb_side = "1" if side == "buy" else "0"
        return await bybit_p2p.get_ads(asset=asset, fiat=fiat, side=bb_side, size=rows)


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def index_handler(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "index.html")


async def api_orderbook(request: web.Request) -> web.Response:
    ex    = request.match_info["exchange"]
    fiat  = request.match_info["fiat"]
    asset = request.match_info["asset"]
    try:
        buy_ads, sell_ads = await asyncio.gather(
            _fetch(ex, fiat, asset, "buy",  10),
            _fetch(ex, fiat, asset, "sell", 10),
        )
        _enrich(buy_ads)
        _enrich(sell_ads)

        spread = {}
        if buy_ads and sell_ads:
            spread = calc_spread(buy_ads[0]["price"], sell_ads[0]["price"])

        return web.json_response({"buy": buy_ads, "sell": sell_ads, "spread": spread})
    except Exception as e:
        logger.error(f"api_orderbook {ex}/{fiat}/{asset}: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def api_history(request: web.Request) -> web.Response:
    ex    = request.match_info["exchange"]
    fiat  = request.match_info["fiat"]
    asset = request.match_info["asset"]
    try:
        hist   = list(get_history(ex, fiat, asset))
        points = []
        for ts, buy, sell in hist:
            sp = calc_spread(buy, sell)
            points.append({
                "ts":         ts.strftime("%d.%m %H:%M"),
                "buy":        buy,
                "sell":       sell,
                "spread_pct": sp["spread_pct"],
            })
        return web.json_response({"points": points})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_ai(request: web.Request) -> web.Response:
    try:
        body  = await request.json()
        ex    = body.get("exchange", "binance")
        fiat  = body.get("fiat",     "KZT")
        asset = body.get("asset",    "USDT")

        buy_ads, sell_ads = await asyncio.gather(
            _fetch(ex, fiat, asset, "buy",  8),
            _fetch(ex, fiat, asset, "sell", 8),
        )
        hist     = list(get_history(ex, fiat, asset))
        patterns = _compute_patterns(hist) if len(hist) >= 10 else None
        prompt   = _build_prompt(ex, fiat, asset, buy_ads, sell_ads, patterns)
        response = await gemini.ask(prompt)

        return web.json_response({"response": response})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ─── App factory ──────────────────────────────────────────────────────────────

@web.middleware
async def ngrok_middleware(request: web.Request, handler):
    """Добавляет заголовок чтобы ngrok не показывал страницу предупреждения."""
    response = await handler(request)
    response.headers["ngrok-skip-browser-warning"] = "true"
    return response


def create_app() -> web.Application:
    app = web.Application(middlewares=[ngrok_middleware])

    # API routes (регистрировать ДО статики)
    app.router.add_get("/api/orderbook/{exchange}/{fiat}/{asset}", api_orderbook)
    app.router.add_get("/api/history/{exchange}/{fiat}/{asset}",  api_history)
    app.router.add_post("/api/ai",                                api_ai)

    # Root → index.html
    app.router.add_get("/",           index_handler)
    app.router.add_get("/index.html", index_handler)

    return app


async def start_webapp(port: int = 8080) -> web.AppRunner:
    """Запускает веб-сервер в текущем event loop. Вызывать из main() бота."""
    app    = create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Mini App server: http://0.0.0.0:{port}  |  ngrok: ngrok http {port}")
    return runner
