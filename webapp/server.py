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

import db
from api import binance_p2p, bybit_p2p, okx_p2p, wallet_p2p, gemini
from handlers.price_history import get_history
from handlers.pattern_engine import _compute_patterns
from handlers.ai_advisor import _build_prompt
from handlers.tracker import _fetch_trader_ads
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
    content = (STATIC_DIR / "index.html").read_bytes()
    return web.Response(
        body=content,
        content_type="text/html",
        charset="utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


async def api_orderbook(request: web.Request) -> web.Response:
    ex    = request.match_info["exchange"]
    fiat  = request.match_info["fiat"]
    asset = request.match_info["asset"]
    pay   = request.rel_url.query.get("pay", "")

    # Фильтры стакана
    def _qf(key, cast, default):
        try: return cast(request.rel_url.query.get(key) or default)
        except: return default

    min_amount  = _qf("min_amount",  float, 0)
    third_party = request.rel_url.query.get("third_party", "")   # "yes"/"no"/""
    min_orders  = _qf("min_orders",  int,   0)
    min_comp    = _qf("min_comp",    float, 0)

    def _apply_filters(ads: list) -> list:
        if min_amount:
            ads = [a for a in ads if (a.get("max_amount") or 0) >= min_amount]
        if third_party == "yes":
            ads = [a for a in ads if a.get("third_party")]
        elif third_party == "no":
            ads = [a for a in ads if not a.get("third_party")]
        if min_orders:
            ads = [a for a in ads if (a.get("orders") or 0) >= min_orders]
        if min_comp:
            ads = [a for a in ads if (a.get("completion") or 0) >= min_comp]
        return ads

    try:
        buy_ads, sell_ads = await asyncio.gather(
            _fetch(ex, fiat, asset, "buy",  15, pay),
            _fetch(ex, fiat, asset, "sell", 15, pay),
        )
        buy_ads  = _apply_filters(_enrich(buy_ads))
        sell_ads = _apply_filters(_enrich(sell_ads))

        spread = {}
        if buy_ads and sell_ads:
            spread = calc_spread(buy_ads[0]["price"], sell_ads[0]["price"])

        return web.json_response({"buy": buy_ads[:10], "sell": sell_ads[:10], "spread": spread})
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


# ─── User-data helpers ────────────────────────────────────────────────────────

def _uid(request: web.Request) -> int | None:
    """Извлекает user_id из query string ?uid=xxx."""
    try:
        v = request.rel_url.query.get("uid") or "0"
        return int(v) or None
    except Exception:
        return None


# ─── Trackers API ──────────────────────────────────────────────────────────────

async def api_trackers_list(request: web.Request) -> web.Response:
    """GET /api/user/trackers?uid=xxx"""
    uid = _uid(request)
    if not uid:
        return web.json_response({"error": "uid required"}, status=400)
    rows = await db.trackers_get(uid)
    return web.json_response({"trackers": rows})


async def api_trackers_add(request: web.Request) -> web.Response:
    """POST /api/user/trackers  body: {uid, exchange, fiat, asset, nickname}"""
    try:
        body     = await request.json()
        uid      = int(body.get("uid") or 0)
        exchange = body["exchange"]
        fiat     = body["fiat"]
        asset    = body.get("asset", "USDT")
        nickname = body["nickname"].strip()
        if not uid or not nickname:
            return web.json_response({"error": "uid and nickname required"}, status=400)

        existing = await db.trackers_get(uid)
        if len(existing) >= 5:
            return web.json_response({"error": "Максимум 5 трекеров"}, status=400)

        ads = await _fetch_trader_ads(exchange, fiat, asset, nickname)
        if not ads["buy"] and not ads["sell"]:
            return web.json_response(
                {"error": f"Трейдер «{nickname}» не найден в топ-50 объявлений"},
                status=404,
            )

        buy_p  = ads["buy"]["price"]  if ads["buy"]  else None
        sell_p = ads["sell"]["price"] if ads["sell"] else None
        db_id  = await db.trackers_add(uid, exchange, fiat, asset, nickname, buy_p, sell_p)

        def _snap(ad):
            return {"price": ad["price"], "available": ad.get("available", 0)} if ad else None

        return web.json_response({
            "id": db_id, "exchange": exchange, "fiat": fiat,
            "asset": asset, "nickname": nickname,
            "buy": _snap(ads["buy"]), "sell": _snap(ads["sell"]),
        })
    except KeyError as e:
        return web.json_response({"error": f"Missing field: {e}"}, status=400)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_trackers_delete(request: web.Request) -> web.Response:
    """DELETE /api/user/trackers/{id}?uid=xxx"""
    uid = _uid(request)
    if not uid:
        return web.json_response({"error": "uid required"}, status=400)
    try:
        tid = int(request.match_info["id"])
        await db.trackers_delete(uid, tid)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ─── Alerts API ───────────────────────────────────────────────────────────────

async def api_alerts_list(request: web.Request) -> web.Response:
    """GET /api/user/alerts?uid=xxx"""
    uid = _uid(request)
    if not uid:
        return web.json_response({"error": "uid required"}, status=400)
    rows = await db.alerts_get(uid)
    return web.json_response({"alerts": rows})


async def api_alerts_add(request: web.Request) -> web.Response:
    """POST /api/user/alerts  body: {uid, exchange, fiat, asset, threshold, direction, pay}"""
    try:
        body      = await request.json()
        uid       = int(body.get("uid") or 0)
        exchange  = body["exchange"]
        fiat      = body["fiat"]
        asset     = body.get("asset", "USDT")
        threshold = float(body["threshold"])
        direction = body.get("direction", "above")
        pay       = body.get("pay", "")
        if not uid or threshold <= 0:
            return web.json_response({"error": "uid and threshold required"}, status=400)
        await db.alerts_add(uid, exchange, fiat, asset, threshold, direction, pay)
        return web.json_response({"ok": True})
    except KeyError as e:
        return web.json_response({"error": f"Missing field: {e}"}, status=400)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_alerts_delete(request: web.Request) -> web.Response:
    """DELETE /api/user/alerts/{id}?uid=xxx"""
    uid = _uid(request)
    if not uid:
        return web.json_response({"error": "uid required"}, status=400)
    try:
        aid = int(request.match_info["id"])
        await db.alerts_delete(uid, aid)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ─── Blacklist API ────────────────────────────────────────────────────────────

async def api_blacklist_list(request: web.Request) -> web.Response:
    """GET /api/user/blacklist?uid=xxx"""
    uid = _uid(request)
    if not uid:
        return web.json_response({"error": "uid required"}, status=400)
    bl = await db.blacklist_get(uid)
    return web.json_response({"blacklist": sorted(list(bl))})


async def api_blacklist_add(request: web.Request) -> web.Response:
    """POST /api/user/blacklist  body: {uid, nickname}"""
    try:
        body     = await request.json()
        uid      = int(body.get("uid") or 0)
        nickname = (body.get("nickname") or "").strip()
        if not uid or not nickname:
            return web.json_response({"error": "uid and nickname required"}, status=400)
        await db.blacklist_add(uid, nickname)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_blacklist_remove(request: web.Request) -> web.Response:
    """DELETE /api/user/blacklist/{nick}?uid=xxx"""
    uid      = _uid(request)
    nickname = request.match_info["nick"]
    if not uid:
        return web.json_response({"error": "uid required"}, status=400)
    try:
        await db.blacklist_remove(uid, nickname)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_multi(request: web.Request) -> web.Response:
    """GET /api/multi/{exchange}/{fiat} — USDT/BTC/ETH buy+sell параллельно."""
    exchange = request.match_info["exchange"]
    fiat     = request.match_info["fiat"]
    assets   = ["USDT", "BTC", "ETH"]
    try:
        coros = [
            asyncio.gather(
                _fetch(exchange, fiat, a, "buy",  rows=5),
                _fetch(exchange, fiat, a, "sell", rows=5),
                return_exceptions=True,
            )
            for a in assets
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        pairs = []
        for i, asset in enumerate(assets):
            res = results[i]
            if isinstance(res, Exception):
                pairs.append({"asset": asset, "buy": None, "sell": None,
                               "spread_pct": None, "buy_vol": 0, "sell_vol": 0})
                continue
            buy_ads, sell_ads = res
            if isinstance(buy_ads,  Exception): buy_ads  = []
            if isinstance(sell_ads, Exception): sell_ads = []

            buy_p  = buy_ads[0]["price"]  if buy_ads  else None
            sell_p = sell_ads[0]["price"] if sell_ads else None
            sp     = calc_spread(buy_p, sell_p) if buy_p and sell_p else {}
            pairs.append({
                "asset":      asset,
                "buy":        buy_p,
                "sell":       sell_p,
                "spread_pct": sp.get("spread_pct"),
                "spread_abs": sp.get("spread_abs"),
                "buy_vol":    round(sum(a.get("available", 0) for a in buy_ads),  4),
                "sell_vol":   round(sum(a.get("available", 0) for a in sell_ads), 4),
            })

        return web.json_response({"pairs": pairs, "exchange": exchange, "fiat": fiat})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_status(request: web.Request) -> web.Response:
    """GET /api/status/{fiat} — доступность всех 4 бирж (1 объявление USDT)."""
    fiat      = request.match_info["fiat"]
    exchanges = ["binance", "bybit", "okx", "wallet"]

    async def _check(ex: str) -> bool:
        try:
            ads = await _fetch(ex, fiat, "USDT", "buy", rows=1)
            return bool(ads)
        except Exception:
            return False

    results = await asyncio.gather(*[_check(ex) for ex in exchanges], return_exceptions=True)
    status  = {ex: (bool(r) if not isinstance(r, Exception) else False)
               for ex, r in zip(exchanges, results)}
    return web.json_response(status)


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
    app.router.add_get("/api/multi/{exchange}/{fiat}",                  api_multi)
    app.router.add_get("/api/status/{fiat}",                            api_status)
    # User data (trackers / alerts / blacklist)
    app.router.add_get("/api/user/trackers",                            api_trackers_list)
    app.router.add_post("/api/user/trackers",                           api_trackers_add)
    app.router.add_delete("/api/user/trackers/{id}",                    api_trackers_delete)
    app.router.add_get("/api/user/alerts",                              api_alerts_list)
    app.router.add_post("/api/user/alerts",                             api_alerts_add)
    app.router.add_delete("/api/user/alerts/{id}",                      api_alerts_delete)
    app.router.add_get("/api/user/blacklist",                           api_blacklist_list)
    app.router.add_post("/api/user/blacklist",                          api_blacklist_add)
    app.router.add_delete("/api/user/blacklist/{nick}",                 api_blacklist_remove)
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
