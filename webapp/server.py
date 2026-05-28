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

from api import binance_p2p, bybit_p2p, okx_p2p, wallet_p2p, gemini
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


async def _fetch(exchange: str, fiat: str, asset: str, side: str,
                 rows: int = 10, pay: str = ""):
    pay_types = [pay] if pay else None
    if exchange == "binance":
        trade_type = "BUY" if side == "buy" else "SELL"
        return await binance_p2p.get_ads(
            asset=asset, fiat=fiat, trade_type=trade_type,
            rows=rows, pay_types=pay_types,
        )
    elif exchange == "bybit":
        bb_side = "1" if side == "buy" else "0"
        return await bybit_p2p.get_ads(
            asset=asset, fiat=fiat, side=bb_side,
            size=rows, pay_types=pay_types,
        )
    elif exchange == "okx":
        return await okx_p2p.get_ads(
            asset=asset, fiat=fiat, side=side,
            pay_types=pay_types, rows=rows,
        )
    elif exchange == "wallet":
        return await wallet_p2p.get_ads(
            asset=asset, fiat=fiat, side=side,
            pay_types=pay_types, rows=rows,
        )
    return []


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def index_handler(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "index.html")


async def api_orderbook(request: web.Request) -> web.Response:
    ex    = request.match_info["exchange"]
    fiat  = request.match_info["fiat"]
    asset = request.match_info["asset"]
    pay   = request.rel_url.query.get("pay", "")
    try:
        buy_ads, sell_ads = await asyncio.gather(
            _fetch(ex, fiat, asset, "buy",  10, pay),
            _fetch(ex, fiat, asset, "sell", 10, pay),
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
        hist   = await get_history(ex, fiat, asset)
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


def _build_chat_system(
    ex: str, fiat: str, asset: str,
    buy_ads: list, sell_ads: list,
    patterns,
) -> str:
    """Строит системный контекст для чата — вставляется перед первым вопросом."""
    from datetime import datetime
    ex_name = {"binance": "Binance", "bybit": "Bybit", "okx": "OKX", "wallet": "TG Wallet"}.get(ex, ex.title())
    now     = datetime.now()

    def fmt_ad(ad: dict) -> str:
        pay  = ", ".join(ad.get("pay_types", [])[:2]) or "—"
        comp = ad.get("completion", 0)
        return (
            f"  {ad.get('price','?'):,} | {ad.get('nickname','?')} | "
            f"{ad.get('orders',0)} сд | {comp}% | {pay}"
        )

    lines = [
        f"Ты опытный P2P-трейдер и советник. Биржа: {ex_name}. Пара: {asset}/{fiat}.",
        f"Время: {now.strftime('%H:%M')} {now.strftime('%d.%m.%Y')}.",
        "Отвечай по-русски, конкретно, с числами из стакана. Без воды.",
        "Можешь задавать уточняющие вопросы если нужно.",
        "",
        "=== ТЕКУЩИЙ СТАКАН ===",
    ]

    if buy_ads:
        lines.append(f"Покупают (BUY) — топ {min(5, len(buy_ads))}:")
        for ad in buy_ads[:5]:
            lines.append(fmt_ad(ad))

    if sell_ads:
        lines.append(f"Продают (SELL) — топ {min(5, len(sell_ads))}:")
        for ad in sell_ads[:5]:
            lines.append(fmt_ad(ad))

    if buy_ads and sell_ads:
        try:
            sp = calc_spread(buy_ads[0]["price"], sell_ads[0]["price"])
            lines.append(
                f"\nСпред: {sp['spread_pct']}%  |  "
                f"BUY best: {buy_ads[0]['price']:,}  |  SELL best: {sell_ads[0]['price']:,}"
            )
        except Exception:
            pass

    if patterns and patterns.get("points", 0) >= 5:
        lines.append("\n=== ИСТОРИЯ СПРЕДА ===")
        lines.append(f"  Средний: {patterns.get('avg_all','?')}%  |  Текущий: {patterns.get('current','?')}%")
        if patterns.get("trend"):
            tr    = patterns["trend"]
            direc = "↑ растёт" if tr["direction"] == "up" else "↓ падает"
            lines.append(f"  Тренд 3ч: {direc} ({tr['delta']}%)")

    return "\n".join(lines)


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
        hist     = await get_history(ex, fiat, asset)
        patterns = _compute_patterns(hist) if len(hist) >= 10 else None
        prompt   = _build_prompt(ex, fiat, asset, buy_ads, sell_ads, patterns)
        response = await gemini.ask(prompt)

        return web.json_response({"response": response})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_chat(request: web.Request) -> web.Response:
    """
    POST /api/chat
    Body: {exchange, fiat, asset, history: [...Gemini turns...], message: str}
    Returns: {response: str, history: [...updated turns...]}
    """
    try:
        body     = await request.json()
        ex       = body.get("exchange", "binance")
        fiat     = body.get("fiat",     "KZT")
        asset    = body.get("asset",    "USDT")
        history  = body.get("history",  [])   # [{role, parts:[{text}]}, ...]
        user_msg = body.get("message",  "").strip()

        if not user_msg:
            return web.json_response({"error": "empty message"}, status=400)

        # Первое сообщение: грузим стакан и прячем контекст внутри user-turn
        if not history:
            buy_ads, sell_ads = await asyncio.gather(
                _fetch(ex, fiat, asset, "buy",  8),
                _fetch(ex, fiat, asset, "sell", 8),
            )
            _enrich(buy_ads)
            _enrich(sell_ads)
            hist     = await get_history(ex, fiat, asset)
            patterns = _compute_patterns(hist) if len(hist) >= 10 else None
            context  = _build_chat_system(ex, fiat, asset, buy_ads, sell_ads, patterns)
            first_text = context + "\n\n— Вопрос пользователя:\n" + user_msg
            messages = [{"role": "user", "parts": [{"text": first_text}]}]
        else:
            messages = history + [{"role": "user", "parts": [{"text": user_msg}]}]

        response     = await gemini.chat(messages)
        full_history = messages + [{"role": "model", "parts": [{"text": response}]}]

        return web.json_response({"response": response, "history": full_history})

    except Exception as e:
        logger.error(f"api_chat: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def api_spread_compare(request: web.Request) -> web.Response:
    """GET /api/spread_compare/{fiat}/{asset} — все биржи параллельно."""
    fiat  = request.match_info["fiat"]
    asset = request.match_info["asset"]
    try:
        results = await asyncio.gather(
            binance_p2p.get_best_price(asset, fiat, "BUY"),
            binance_p2p.get_best_price(asset, fiat, "SELL"),
            bybit_p2p.get_best_price(asset, fiat, "1"),
            bybit_p2p.get_best_price(asset, fiat, "0"),
            okx_p2p.get_best_price(asset, fiat, "buy"),
            okx_p2p.get_best_price(asset, fiat, "sell"),
            wallet_p2p.get_best_price(asset, fiat, "buy"),
            wallet_p2p.get_best_price(asset, fiat, "sell"),
            return_exceptions=True,
        )
        def _v(x): return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None

        exchanges = [
            {"id": "binance", "name": "Binance", "icon": "🟡", "buy": _v(results[0]), "sell": _v(results[1])},
            {"id": "bybit",   "name": "Bybit",   "icon": "🟠", "buy": _v(results[2]), "sell": _v(results[3])},
            {"id": "okx",     "name": "OKX",     "icon": "🔵", "buy": _v(results[4]), "sell": _v(results[5])},
            {"id": "wallet",  "name": "Wallet",  "icon": "💎", "buy": _v(results[6]), "sell": _v(results[7])},
        ]
        for ex in exchanges:
            if ex["buy"] and ex["sell"]:
                s = calc_spread(ex["buy"], ex["sell"])
                ex["spread_pct"] = s["spread_pct"]
                ex["spread_abs"] = s["spread_abs"]
            else:
                ex["spread_pct"] = None
                ex["spread_abs"] = None

        arb = []
        for ex1 in exchanges:
            for ex2 in exchanges:
                if ex1["id"] == ex2["id"]: continue
                if ex1["buy"] and ex2["sell"]:
                    s = calc_spread(ex1["buy"], ex2["sell"])
                    if s["spread_pct"] > 0:
                        arb.append({
                            "from": ex1["name"], "to": ex2["name"],
                            "pct": s["spread_pct"], "abs": s["spread_abs"],
                        })
        arb.sort(key=lambda x: -x["pct"])
        return web.json_response({"exchanges": exchanges, "arbitrage": arb[:6]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_maker(request: web.Request) -> web.Response:
    """GET /api/maker/{exchange}/{fiat}/{asset}/{side}?pay="""
    exchange = request.match_info["exchange"]
    fiat     = request.match_info["fiat"]
    asset    = request.match_info["asset"]
    side     = request.match_info["side"]
    pay      = request.rel_url.query.get("pay", "")

    FIAT_STEP = {"KZT": 0.50, "RUB": 0.05, "TRY": 0.05, "USD": 0.001}
    step = FIAT_STEP.get(fiat, 0.01)

    try:
        ads = await _fetch(exchange, fiat, asset, side, rows=10, pay=pay)
        if not ads:
            return web.json_response({"ads": [], "recommendations": [], "step": step})

        recs = []
        if side == "buy":
            ads.sort(key=lambda x: x["price"], reverse=True)
            if len(ads) >= 1:
                recs.append({"pos": 1, "price": round(ads[0]["price"] + step, 4), "label": "выше лидера", "medal": "🥇"})
                recs.append({"pos": 2, "price": round(ads[0]["price"] - step, 4), "label": "ниже лидера",  "medal": "🥈"})
            if len(ads) >= 2:
                recs.append({"pos": 3, "price": round(ads[1]["price"] - step, 4), "label": "ниже 2-го",   "medal": "🥉"})
        else:
            ads.sort(key=lambda x: x["price"])
            if len(ads) >= 1:
                recs.append({"pos": 1, "price": round(ads[0]["price"] - step, 4), "label": "ниже лидера", "medal": "🥇"})
                recs.append({"pos": 2, "price": round(ads[0]["price"] + step, 4), "label": "выше лидера", "medal": "🥈"})
            if len(ads) >= 2:
                recs.append({"pos": 3, "price": round(ads[1]["price"] + step, 4), "label": "выше 2-го",   "medal": "🥉"})

        return web.json_response({"ads": ads[:5], "recommendations": recs, "step": step})
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
    app.router.add_get("/api/orderbook/{exchange}/{fiat}/{asset}",       api_orderbook)
    app.router.add_get("/api/history/{exchange}/{fiat}/{asset}",        api_history)
    app.router.add_get("/api/spread_compare/{fiat}/{asset}",            api_spread_compare)
    app.router.add_get("/api/maker/{exchange}/{fiat}/{asset}/{side}",   api_maker)
    app.router.add_post("/api/ai",                                       api_ai)
    app.router.add_post("/api/chat",                                     api_chat)

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
