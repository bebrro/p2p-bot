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
import re
import time as _time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

_start_time = _time.monotonic()

import db
from api import binance_p2p, bybit_p2p, okx_p2p, wallet_p2p, gemini
from api import bybit_auth, okx_auth
from handlers.price_history import get_history
from handlers.pattern_engine import _compute_patterns
from handlers.ai_advisor import _build_prompt
from handlers.tracker import _fetch_trader_ads
from handlers.account_manager import (
    get_account_credentials, upsert_account_memory, remove_account_memory,
)
from handlers.auto_reprice import add_rule_memory, remove_rule_memory
from handlers.pnl import calc_pnl
from utils.spread import calc_spread
from utils.pricing import pick_best_price
from utils.scam_detector import risk_score, risk_badge, risk_tooltip
from utils.encryption import encrypt
from utils.subscription import get_plan_key, get_limits, format_expires
from utils.desc_parser import parse_description
from utils import ai_desc
from config import DISABLED_EXCHANGES, SUSPICIOUS_SPREAD_PCT

logger     = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


# ─── Биржи: единый сбор цен со статусами ──────────────────────────────────────

# id → (имя, иконка, fn_buy(asset,fiat), fn_sell(asset,fiat))
_EX_META = [
    ("binance", "Binance",   "🟡",
     lambda a, f: binance_p2p.get_best_price(a, f, "BUY"),
     lambda a, f: binance_p2p.get_best_price(a, f, "SELL")),
    ("bybit",   "Bybit",     "🟠",
     lambda a, f: bybit_p2p.get_best_price(a, f, "1"),
     lambda a, f: bybit_p2p.get_best_price(a, f, "0")),
    ("okx",     "OKX",       "🔵",
     lambda a, f: okx_p2p.get_best_price(a, f, "buy"),
     lambda a, f: okx_p2p.get_best_price(a, f, "sell")),
    ("wallet",  "TG Wallet", "💎",
     lambda a, f: wallet_p2p.get_best_price(a, f, "buy"),
     lambda a, f: wallet_p2p.get_best_price(a, f, "sell")),
]


async def _gather_exchanges(asset: str, fiat: str) -> list[dict]:
    """
    Собирает цены по всем биржам со статусом каждой.
    Отключённые (DISABLED_EXCHANGES) НЕ дёргаем — не ждём таймаут мёртвого API.

    status: 'ok' | 'no_data' (жива, но пусто) | 'disabled' (временно выключена)
    suspicious: True если спред > SUSPICIOUS_SPREAD_PCT (возможны приманки)
    """
    tasks, meta = [], []
    for ex_id, name, icon, buy_fn, sell_fn in _EX_META:
        if ex_id in DISABLED_EXCHANGES:
            continue
        meta.append((ex_id, name, icon))
        tasks.append(buy_fn(asset, fiat))
        tasks.append(sell_fn(asset, fiat))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    def _v(x):
        return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None

    exchanges = []
    for i, (ex_id, name, icon) in enumerate(meta):
        buy, sell = _v(results[i * 2]), _v(results[i * 2 + 1])
        ex = {"id": ex_id, "name": name, "icon": icon, "buy": buy, "sell": sell}
        if buy and sell:
            s = calc_spread(buy, sell)
            ex["spread_pct"] = round(s["spread_pct"], 2)
            ex["spread_abs"] = round(s["spread_abs"], 2)
            ex["suspicious"] = s["spread_pct"] > SUSPICIOUS_SPREAD_PCT
            ex["status"]     = "ok"
        else:
            ex["spread_pct"] = ex["spread_abs"] = None
            ex["suspicious"] = False
            ex["status"]     = "no_data"
        exchanges.append(ex)

    # Отключённые биржи — добавляем как «временно недоступно» (UI не покажет «Ошибка»)
    for ex_id, name, icon, _b, _s in _EX_META:
        if ex_id in DISABLED_EXCHANGES:
            exchanges.append({
                "id": ex_id, "name": name, "icon": icon,
                "buy": None, "sell": None, "spread_pct": None,
                "spread_abs": None, "suspicious": False, "status": "disabled",
            })
    return exchanges


# Референсный оборот по фиатам (≈ $1000) — для демо «сколько бы заработал».
# Круглые суммы, понятные трейдеру.
_REF_VOLUME = {
    "KZT": 500_000, "RUB": 100_000, "TRY": 35_000, "USD": 1_000,
    "THB": 33_000, "IDR": 16_000_000, "VND": 25_000_000,
    "INR": 85_000, "AED": 3_700, "NGN": 1_600_000, "BRL": 5_700,
    "GEL": 2_700, "AMD": 390_000, "AZN": 1_700, "UZS": 13_000_000, "KGS": 87_000,
}


def _earn_demo(arb: list[dict], fiat: str) -> dict | None:
    """
    «Момент вау»: сколько бы заработал на референсном обороте по лучшему
    РЕАЛИСТИЧНОМУ (не подозрительному) спреду. None если нет возможностей.
    """
    # предпочитаем не-подозрительный арб (credible число), иначе лучший
    credible = next((a for a in arb if not a.get("suspicious")), arb[0] if arb else None)
    if not credible or credible.get("pct", 0) <= 0:
        return None
    vol = _REF_VOLUME.get(fiat, 1_000)
    return {
        "amount": vol,
        "profit": round(vol * credible["pct"] / 100),
        "pct":    credible["pct"],
        "fiat":   fiat,
        "route":  f'{credible["from"]} → {credible["to"]}',
    }


# Канонизация банков для сопоставления (Kaspi / KaspiBank / Каспи → kaspi).
# Без общего банка платёж контрагент→контрагенту невозможен.
_BANK_CANON = {
    "kaspi": "kaspi", "каспи": "kaspi",
    "halyk": "halyk", "халык": "halyk", "халик": "halyk",
    "jusan": "jusan", "жусан": "jusan",
    "forte": "forte", "форте": "forte",
    "freedom": "freedom", "фридом": "freedom", "фрідом": "freedom",
    "altyn": "altyn", "алтын": "altyn",
    "centercredit": "bcc", "bcc": "bcc", "центркредит": "bcc",
    "sber": "sber", "сбер": "sber",
    "tinkoff": "tinkoff", "тинь": "tinkoff", "тинк": "tinkoff",
    "tbank": "tinkoff", "тбанк": "tinkoff",
    "raiffeisen": "raif", "райф": "raif",
    "alfa": "alfa", "альфа": "alfa", "vtb": "vtb", "втб": "vtb",
    "sbp": "sbp", "сбп": "sbp", "fps": "sbp",
    "ziraat": "ziraat", "garanti": "garanti", "akbank": "akbank",
    "papara": "papara", "vakif": "vakif", "ininal": "ininal",
}


def _norm_bank(name: str) -> str:
    s = re.sub(r"[^0-9a-zа-яё]", "", (name or "").lower())
    # Сначала ищем алиас по полной строке («тбанк» → tinkoff, «каспибанк» → kaspi)
    for k, v in _BANK_CANON.items():
        if k in s:
            return v
    # Фолбэк: чистим служебные слова
    s2 = s.replace("bank", "").replace("банк", "").replace("finance", "").replace("финанс", "")
    return s2 or s or "?"


def _bank_set(a: dict) -> set:
    return {_norm_bank(x) for x in (a.get("pay_types") or []) if x}


def _common_bank_labels(a: dict, b: dict) -> list:
    nb = _bank_set(b)
    out, seen = [], set()
    for x in (a.get("pay_types") or []):
        n = _norm_bank(x)
        if x and n in nb and n not in seen:
            seen.add(n)
            out.append(x)
    return out[:3]


def _find_link(buys: list, sells: list, fiat: str) -> dict | None:
    """
    Ищет ИСПОЛНИМУЮ связку «без карт». Работает и внутри биржи, и МЕЖ биржами
    (если у ног проставлен exchange). Требования к обеим ногам:
      • явно принимают 3-х лиц (third_party is True)
      • не скам/ловушка
      • положительный спред (продать дороже чем купить)
      • лимиты пересекаются (один объём на обеих)
      • есть ОБЩИЙ банк (иначе платёж контрагент→контрагенту не провести)
    """
    def ok(a):
        if a.get("third_party") is not True:
            return False
        if a.get("scam_recruit") or a.get("trap"):
            return False
        return (a.get("price") or 0) > 0

    B = [a for a in buys if ok(a)]
    S = [a for a in sells if ok(a)]
    if not B or not S:
        return None

    INF = 1e18
    best = None
    for b in B:
        bn   = _bank_set(b)
        bmin = b.get("min_amount") or 0
        bmax = b.get("eff_max") or b.get("max_amount") or 0   # реальный потолок (с учётом запаса)
        for s in S:
            if s["price"] <= b["price"]:
                continue
            # Общий банк нужен, КРОМЕ случая когда хоть одна нога принимает любой банк
            if not (bn & _bank_set(s)) and not b.get("any_bank") and not s.get("any_bank"):
                continue
            smax = s.get("eff_max") or s.get("max_amount") or 0
            lo = max(bmin, s.get("min_amount") or 0)
            hi = min(bmax if bmax > 0 else INF, smax or INF)
            if hi <= 0 or lo > hi:
                continue                       # лимиты не пересекаются
            sp = calc_spread(b["price"], s["price"])
            if best is None or sp["spread_pct"] > best["pct"]:
                best = {"pct": sp["spread_pct"], "abs": sp["spread_abs"],
                        "b": b, "s": s, "lo": lo, "hi": hi}

    if best is None:
        return None

    bb, bs = best["b"], best["s"]
    lo, hi = best["lo"], best["hi"]
    vol    = hi if hi < INF else max(lo, _REF_VOLUME.get(fiat, 1_000))

    # Какой банк показать (учитывая «любой банк»)
    if bb.get("any_bank") and bs.get("any_bank"):
        banks = ["любой банк"]
    elif bb.get("any_bank"):
        banks = (bs.get("pay_types") or [])[:3] or ["любой банк"]
    elif bs.get("any_bank"):
        banks = (bb.get("pay_types") or [])[:3] or ["любой банк"]
    else:
        banks = _common_bank_labels(bb, bs)

    def _leg(a):
        return {
            "nickname":  a.get("nickname", "—"),
            "price":     a["price"],
            "min":       a.get("min_amount", 0),
            "max":       a.get("eff_max") or a.get("max_amount", 0),
            "pay":       (a.get("pay_types") or [])[:3],
            "confirm3p": a.get("third_party") is True,
            "ex_name":   a.get("ex_name"),
            "ex_icon":   a.get("ex_icon"),
        }

    return {
        "buy":        _leg(bb),
        "sell":       _leg(bs),
        "pct":        best["pct"],
        "abs":        best["abs"],
        "profit":     round(vol * best["pct"] / 100),
        "amount":     round(vol),
        "lo":         round(lo),
        "hi":         round(hi) if hi < INF else None,
        "banks":      banks,
        "cross":      bool(bb.get("exchange") and bs.get("exchange")
                          and bb.get("exchange") != bs.get("exchange")),
        "fiat":       fiat,
        "suspicious": best["pct"] > SUSPICIOUS_SPREAD_PCT,
    }


def _triangle(buy_ads: list, sell_ads: list, fiat: str) -> dict | None:
    """Связка внутри одной биржи (для стакана)."""
    return _find_link(buy_ads, sell_ads, fiat)


def _build_arbitrage(exchanges: list[dict]) -> list[dict]:
    """Кросс-биржевой арбитраж только по живым биржам (status='ok')."""
    live = [e for e in exchanges if e.get("status") == "ok"]
    arb = []
    for ex1 in live:
        for ex2 in live:
            if ex1["id"] == ex2["id"]:
                continue
            if ex1["buy"] and ex2["sell"]:
                s = calc_spread(ex1["buy"], ex2["sell"])
                if s["spread_pct"] > 0:
                    arb.append({
                        "from": ex1["name"], "to": ex2["name"],
                        "from_id": ex1["id"], "to_id": ex2["id"],
                        "pct": round(s["spread_pct"], 2),
                        "abs": round(s["spread_abs"], 2),
                        "suspicious": s["spread_pct"] > SUSPICIOUS_SPREAD_PCT,
                    })
    arb.sort(key=lambda x: -x["pct"])
    return arb


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _enrich(ads: list) -> list:
    """Добавляет поля риска, парсит описание (3-е лица и пр.), нормализует completion."""
    for ad in ads:
        ad["risk_score"]   = risk_score(ad)
        ad["risk_badge"]   = risk_badge(ad)
        ad["risk_tooltip"] = risk_tooltip(ad)
        c = ad.get("completion", 0)
        if isinstance(c, float) and 0 < c <= 1:
            ad["completion"] = round(c * 100, 1)
        ad["pay_types"] = list(dict.fromkeys(ad.get("pay_types", [])))
        # Реальный максимум = min(заявленный лимит, доступный объём × цена).
        # Мерчант может писать «до 8 млн», а в наличии всего 378 USDT —
        # тогда реально максимум ≈ 378×цена. Так же считает сам Binance.
        avail = ad.get("available") or 0
        price = ad.get("price") or 0
        stated = ad.get("max_amount") or 0
        cap = avail * price if (avail > 0 and price > 0) else 0
        if cap > 0 and (stated <= 0 or cap < stated):
            ad["eff_max"]      = round(cap, 2)
            ad["max_capped"]   = stated > 0 and cap < stated  # лимит урезан запасом
        else:
            ad["eff_max"]      = stated
            ad["max_capped"]   = False
        # Парсим описание: 3-е лица, точная сумма, ограничения времени и т.д.
        info = parse_description(ad.get("description", ""))
        ad["third_party"]  = info["third_party"]    # True | False | None
        ad["exact_amount"] = info["exact_amount"]
        ad["smart_flags"]  = info["flags"]
        ad["scam_recruit"] = info.get("scam_recruit", False)
        ad["trap"]         = info.get("trap", False)
        ad["any_bank"]     = info.get("any_bank", False)
        ad["ai_desc"]      = False
    return ads


def _apply_third_party_flag(ad: dict) -> None:
    """Пересобирает плашку 3-х лиц в smart_flags по текущему ad['third_party']."""
    flags = [f for f in (ad.get("smart_flags") or [])
             if ("3-х лиц" not in f and "3-е лиц" not in f)]
    if ad.get("third_party") is True:
        flags.insert(0, "✅ 3-е лица ок")
    elif ad.get("third_party") is False:
        flags.insert(0, "❌ нет 3-х лиц")
    ad["smart_flags"] = flags


async def _ai_overlay(ads: list) -> list:
    """
    Накладывает семантику Gemini ПОВЕРХ regex-результата (ads уже после _enrich).
    AI понимает формулировки, которые regex покрыть не может («1/3»,
    «на дов лицо но от 1 го» и т.п.). Если AI выключен/упал — остаётся regex.
    """
    if not ai_desc.enabled():
        return ads
    try:
        res = await ai_desc.classify([a.get("description", "") for a in ads])
    except Exception as e:
        logger.warning(f"_ai_overlay: {e}")
        return ads
    for a in ads:
        r = res.get((a.get("description") or "").strip())
        if not r:
            continue                       # нет описания / AI не классифицировал — regex
        a["third_party"]  = r["third_party"]
        a["scam_recruit"] = r["scam_recruit"]
        a["trap"]         = r["trap"]
        a["any_bank"]     = r["any_bank"]
        a["ai_desc"]      = True
        _apply_third_party_flag(a)
    return ads


def _tradeable_at(ad: dict, amount: float) -> bool:
    """
    True если сумму `amount` (в фиате) реально можно сделать в этом объявлении:
    она попадает в лимит [min_amount .. max_amount].
    Если max неизвестен (0) — проверяем только нижнюю границу.
    """
    lo = ad.get("min_amount") or 0
    hi = ad.get("eff_max") or ad.get("max_amount") or 0
    if hi <= 0:
        return lo <= amount
    return lo <= amount <= hi


async def _fetch(exchange: str, fiat: str, asset: str, side: str,
                 rows: int = 10, pay: str = "", amount: float = 0):
    # Отключённую биржу не дёргаем — мгновенно пусто (без таймаута мёртвого API)
    if exchange in DISABLED_EXCHANGES:
        return []
    pay_types = [pay] if pay else None
    if exchange == "binance":
        trade_type = "BUY" if side == "buy" else "SELL"
        return await binance_p2p.get_ads(
            asset=asset, fiat=fiat, trade_type=trade_type,
            rows=rows, pay_types=pay_types, amount=amount,
        )
    elif exchange == "bybit":
        bb_side = "1" if side == "buy" else "0"
        return await bybit_p2p.get_ads(
            asset=asset, fiat=fiat, side=bb_side,
            size=rows, pay_types=pay_types,
            amount=(str(int(amount)) if amount and amount > 0 else ""),
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
            # Показываем объявления, где сумму РЕАЛЬНО можно сделать:
            # лимит [min_ad .. max_ad] включает твою сумму.
            ads = [a for a in ads if _tradeable_at(a, min_amount)]
        if third_party == "yes":
            # «принимает 3-х лиц»: прячем только тех, кто ЯВНО написал «не принимаю»
            ads = [a for a in ads if a.get("third_party") is not False]
        elif third_party == "no":
            # «без 3-х лиц»: прячем только тех, кто ЯВНО принимает
            ads = [a for a in ads if a.get("third_party") is not True]
        if min_orders:
            ads = [a for a in ads if (a.get("orders") or 0) >= min_orders]
        if min_comp:
            ads = [a for a in ads if (a.get("completion") or 0) >= min_comp]
        return ads

    try:
        buy_ads, sell_ads = await asyncio.gather(
            _fetch(ex, fiat, asset, "buy",  15, pay, min_amount),
            _fetch(ex, fiat, asset, "sell", 15, pay, min_amount),
        )
        buy_en  = _enrich(buy_ads)
        sell_en = _enrich(sell_ads)
        await _ai_overlay(buy_en + sell_en)     # один батч на обе стороны

        # Связка «без карт» — считаем из всех объявлений (своё условие по 3-м лицам)
        triangle = _triangle(buy_en, sell_en, fiat)

        buy_ads  = _apply_filters(buy_en)
        sell_ads = _apply_filters(sell_en)

        # Помеченные (скам/ловушка) — вниз списка, чтобы не ломали топ-цену стакана
        def _sink_flagged(ads):
            return sorted(ads, key=lambda a: 1 if (a.get("scam_recruit") or a.get("trap")) else 0)
        buy_ads  = _sink_flagged(buy_ads)
        sell_ads = _sink_flagged(sell_ads)

        # Топ-цена и спред — по ДЕ-БАЙТЕНЫМ ценам (без приманок-выбросов),
        # чтобы заголовок стакана не ломался одиночным фейк-объявлением.
        best_buy  = pick_best_price(buy_ads,  buy_side=True,  fiat=fiat, asset=asset)
        best_sell = pick_best_price(sell_ads, buy_side=False, fiat=fiat, asset=asset)
        spread = {}
        if best_buy and best_sell:
            spread = calc_spread(best_buy, best_sell)

        return web.json_response({
            "buy": buy_ads[:10], "sell": sell_ads[:10],
            "spread": spread, "triangle": triangle,
            "best_buy": best_buy, "best_sell": best_sell,
        })
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
        exchanges = await _gather_exchanges(asset, fiat)
        arb       = _build_arbitrage(exchanges)
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

    FIAT_STEP = {
        "KZT": 0.50, "RUB": 0.05, "TRY": 0.05, "USD": 0.001,
        "THB": 0.01, "IDR": 1, "VND": 1, "INR": 0.01, "AED": 0.001,
        "NGN": 0.1, "BRL": 0.001, "GEL": 0.001, "AMD": 0.1, "AZN": 0.001,
        "UZS": 1, "KGS": 0.01,
    }
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


# ─── Антискам API (чек / ник / репорт) ──────────────────────────────────────
_RECEIPT_PROMPT = (
    "Ты эксперт по безопасности P2P-сделок с криптой. На изображении — "
    "«чек об оплате», который прислал контрагент. P2P-мошенники присылают "
    "ПОДДЕЛЬНЫЕ чеки, чтобы продавец отпустил USDT не получив реальных денег.\n\n"
    "Проанализируй скриншот на признаки подделки и ответь на русском СТРОГО так:\n\n"
    "🔎 <b>Признаки редактирования:</b> [шрифты, выравнивание, артефакты сжатия, "
    "несовпадение стиля — или 'явных нет']\n"
    "📋 <b>Нестыковки:</b> [время/дата/сумма/баланс/реквизиты/статус — или 'не вижу']\n"
    "🏦 <b>Похоже на реальный интерфейс банка/приложения:</b> [да/нет/трудно сказать]\n"
    "⚖️ <b>Вердикт:</b> 🟢 похоже на настоящий | 🟡 подозрительно | 🔴 признаки подделки\n\n"
    "Будь конкретен, но не выдумывай. В конце ОБЯЗАТЕЛЬНО добавь дословно:\n"
    "«⚠️ Скриншот — НЕ доказательство. Отпускай USDT только когда деньги "
    "реально пришли на твой счёт (проверь в своём банке/приложении).»"
)


async def api_antiscam_receipt(request: web.Request) -> web.Response:
    """POST /api/antiscam/receipt  Body: {image: base64}  → AI-анализ чека."""
    try:
        body = await request.json()
        img  = (body.get("image") or "").strip()
        if img.lower().startswith("data:") and "," in img:
            img = img.split(",", 1)[1]      # срезаем data:image/...;base64,
        if not img:
            return web.json_response({"error": "no image"}, status=400)
        result = await gemini.vision(img, _RECEIPT_PROMPT, mime="image/jpeg")
        return web.json_response({"result": result})
    except Exception as e:
        logger.error(f"api_antiscam_receipt: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def api_antiscam_nick(request: web.Request) -> web.Response:
    """GET /api/antiscam/nick/{nick}  → проверка ника по базе кидал."""
    nick = (request.match_info.get("nick") or "").strip()
    if len(nick) < 2:
        return web.json_response({"error": "short"}, status=400)
    votes = 0
    try:
        if db.ok():
            nl = nick.lower()
            for n, cnt in await db.community_get_all():
                if (n or "").strip().lower() == nl:
                    votes = int(cnt); break
    except Exception as e:
        logger.warning(f"antiscam nick: {e}")
    if votes >= 3:
        level, verdict = "danger", f"ОПАСНО — отмечен как кидала {votes} раз"
    elif votes >= 1:
        level, verdict = "warn", f"Осторожно — есть {votes} жалоб(ы)"
    else:
        level, verdict = "ok", "Жалоб в базе нет (но это не гарантия)"
    return web.json_response({"nick": nick, "votes": votes,
                              "level": level, "verdict": verdict})


async def api_antiscam_report(request: web.Request) -> web.Response:
    """POST /api/antiscam/report?uid=  Body: {nick}  → добавить в базу кидал."""
    try:
        body = await request.json()
        nick = (body.get("nick") or "").strip()
        if len(nick) < 2:
            return web.json_response({"error": "short"}, status=400)
        uid   = _uid(request) or 0
        total = await db.community_vote(uid, nick) if db.ok() else 0
        return web.json_response({"nick": nick, "total": total})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ─── Whale-трекер API (крупные заявки) ───────────────────────────────────────
async def api_whales(request: web.Request) -> web.Response:
    """GET /api/whales/{fiat}?asset=USDT&threshold=50000  → крупные мерчанты."""
    fiat  = request.match_info["fiat"]
    asset = request.rel_url.query.get("asset", "USDT")
    try:
        threshold = float(request.rel_url.query.get("threshold") or 50_000)
    except Exception:
        threshold = 50_000

    tasks, meta = [], []
    for ex_id, name, icon, _b, _s in _EX_META:
        if ex_id in DISABLED_EXCHANGES:
            continue
        for side in ("buy", "sell"):
            meta.append((ex_id, name, icon, side))
            tasks.append(_fetch(ex_id, fiat, asset, side, rows=30))
    results = await asyncio.gather(*tasks, return_exceptions=True)

    whales = []
    for (ex_id, name, icon, side), ads in zip(meta, results):
        if isinstance(ads, Exception) or not ads:
            continue
        for ad in ads:
            vol = (ad.get("available") or 0) * (ad.get("price") or 0)
            if vol >= threshold:
                whales.append({
                    "ex_name":   name,
                    "ex_icon":   icon,
                    "side":      side,                       # buy | sell
                    "nickname":  ad.get("nickname", "—"),
                    "price":     ad.get("price", 0),
                    "available": round(ad.get("available", 0), 2),
                    "volume":    round(vol),
                })
    whales.sort(key=lambda x: x["volume"], reverse=True)
    return web.json_response({"whales": whales[:30],
                              "threshold": threshold, "fiat": fiat, "asset": asset})


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

        sub      = await db.subscription_get(uid)
        plan_key = get_plan_key(sub)
        limits   = get_limits(plan_key)
        existing = await db.trackers_get(uid)
        if len(existing) >= limits["trackers"]:
            return web.json_response({
                "error": f"Лимит трекеров для плана {plan_key}: {limits['trackers']}. Обнови подписку /subscribe"
            }, status=403)

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
        sub      = await db.subscription_get(uid)
        plan_key = get_plan_key(sub)
        limits   = get_limits(plan_key)
        existing_alerts = await db.alerts_get(uid)
        if len(existing_alerts) >= limits["alerts"]:
            return web.json_response({
                "error": f"Лимит алертов ({limits['alerts']}) для плана {plan_key}. Обнови подписку /subscribe"
            }, status=403)
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


# ─── Accounts API (Mini App repricer) ─────────────────────────────────────────

async def api_accounts_list(request: web.Request) -> web.Response:
    """GET /api/user/accounts?uid=xxx  — список аккаунтов (без ключей)."""
    uid = _uid(request)
    if not uid:
        return web.json_response({"error": "uid required"}, status=400)
    rows = await db.accounts_get(uid)
    safe = [
        {"id": r["id"], "exchange": r["exchange"], "label": r["label"],
         "nickname": r.get("nickname", ""), "active": r["is_active"]}
        for r in rows
    ]
    return web.json_response({"accounts": safe})


async def api_accounts_add(request: web.Request) -> web.Response:
    """POST /api/user/accounts  body:{uid,exchange,label,api_key,api_secret,extra?}"""
    try:
        body       = await request.json()
        uid        = int(body.get("uid") or 0)
        exchange   = (body.get("exchange") or "bybit").lower()
        label      = (body.get("label")    or "").strip()[:30]
        raw_key    = (body.get("api_key")  or "").strip()
        raw_secret = (body.get("api_secret") or "").strip()
        raw_extra  = (body.get("extra")    or "").strip()   # passphrase for OKX

        if not uid or not raw_key or not raw_secret:
            return web.json_response({"error": "uid, api_key, api_secret required"}, status=400)
        if not label:
            label = exchange.title()

        # Верификация ключа
        if exchange == "bybit":
            ok_flag, nick = await bybit_auth.verify_api_key(raw_key, raw_secret)
        elif exchange == "okx":
            ok_flag, nick = await okx_auth.verify_api_key(raw_key, raw_secret, raw_extra)
        else:
            return web.json_response({"error": f"Exchange {exchange!r} not supported"}, status=400)

        if not ok_flag:
            return web.json_response({"error": f"Ключ не прошёл проверку: {nick}"}, status=422)

        enc_key    = encrypt(raw_key)
        enc_secret = encrypt(raw_secret)
        enc_extra  = encrypt(raw_extra) if raw_extra else ""

        acc_id = await db.accounts_add(uid, exchange, label, enc_key, enc_secret, enc_extra, nick)

        new_acc = {
            "id": acc_id, "exchange": exchange, "label": label,
            "api_key": enc_key, "api_secret": enc_secret, "extra": enc_extra,
            "nickname": nick, "active": False,
        }
        # Если первый для этой биржи — делаем активным
        existing = await db.accounts_get(uid)
        same_ex  = [r for r in existing if r["exchange"] == exchange and r["id"] != acc_id]
        if not any(r["is_active"] for r in same_ex):
            new_acc["active"] = True
            await db.accounts_set_active(uid, exchange, acc_id)

        upsert_account_memory(uid, new_acc)
        return web.json_response({
            "id": acc_id, "exchange": exchange,
            "label": label, "nickname": nick, "active": new_acc["active"],
        })
    except Exception as e:
        logger.error(f"api_accounts_add: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def api_accounts_activate(request: web.Request) -> web.Response:
    """POST /api/user/accounts/{id}/activate?uid=xxx"""
    uid = _uid(request)
    if not uid:
        return web.json_response({"error": "uid required"}, status=400)
    try:
        acc_id   = int(request.match_info["id"])
        rows     = await db.accounts_get(uid)
        target   = next((r for r in rows if r["id"] == acc_id), None)
        if not target:
            return web.json_response({"error": "not found"}, status=404)
        exchange = target["exchange"]
        await db.accounts_set_active(uid, exchange, acc_id)
        # обновляем память
        from handlers.account_manager import _accounts
        for a in _accounts.get(uid, []):
            if a.get("exchange") == exchange:
                a["active"] = (a.get("id") == acc_id)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_accounts_delete(request: web.Request) -> web.Response:
    """DELETE /api/user/accounts/{id}?uid=xxx"""
    uid = _uid(request)
    if not uid:
        return web.json_response({"error": "uid required"}, status=400)
    try:
        acc_id = int(request.match_info["id"])
        await db.accounts_delete(uid, acc_id)
        remove_account_memory(uid, acc_id)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_myads(request: web.Request) -> web.Response:
    """GET /api/user/myads?uid=xxx&exchange=bybit  — свои P2P объявления."""
    uid      = _uid(request)
    exchange = request.rel_url.query.get("exchange", "bybit").lower()
    if not uid:
        return web.json_response({"error": "uid required"}, status=400)
    api_key, api_secret, extra = get_account_credentials(uid, exchange)
    if not api_key:
        return web.json_response({"error": f"Нет активного аккаунта {exchange}"}, status=404)
    try:
        if exchange == "bybit":
            ads = await bybit_auth.get_my_ads(api_key, api_secret, status=1)
        elif exchange == "okx":
            ads = await okx_auth.get_my_ads(api_key, api_secret, extra)
        else:
            return web.json_response({"error": "unsupported exchange"}, status=400)
        return web.json_response({"ads": ads})
    except Exception as e:
        logger.error(f"api_myads {exchange}: {e}")
        return web.json_response({"error": str(e)}, status=500)


# ─── Repricer API (Mini App) ───────────────────────────────────────────────────

async def api_repricer_list(request: web.Request) -> web.Response:
    """GET /api/user/repricer?uid=xxx"""
    uid = _uid(request)
    if not uid:
        return web.json_response({"error": "uid required"}, status=400)
    rows = await db.repricer_get(uid)
    return web.json_response({"rules": rows})


async def api_repricer_add(request: web.Request) -> web.Response:
    """POST /api/user/repricer
    body:{uid,exchange,ad_id,target_pos,delta,min_price,max_price}
    """
    try:
        body       = await request.json()
        uid        = int(body.get("uid") or 0)
        exchange   = (body.get("exchange") or "bybit").lower()
        ad_id      = str(body.get("ad_id") or "").strip()
        target_pos = int(body.get("target_pos") or 1)
        delta      = float(body.get("delta") or 0.5)
        min_price  = float(body.get("min_price") or 0)
        max_price  = float(body.get("max_price") or 0)

        if not uid or not ad_id or min_price <= 0 or max_price <= min_price:
            return web.json_response({"error": "Проверь параметры"}, status=400)

        # Проверяем лимит репрайсера по плану
        sub      = await db.subscription_get(uid)
        plan_key = get_plan_key(sub)
        limits   = get_limits(plan_key)
        existing_rules = await db.repricer_get(uid)
        if len(existing_rules) >= limits["repricer_rules"]:
            return web.json_response({
                "error": f"Лимит правил ({limits['repricer_rules']}) для плана {plan_key}. Обнови подписку /subscribe"
            }, status=403)

        api_key, api_secret, extra = get_account_credentials(uid, exchange)
        if not api_key:
            return web.json_response({"error": f"Нет активного аккаунта {exchange}"}, status=404)

        # Проверяем что объявление существует
        if exchange == "bybit":
            ads = await bybit_auth.get_my_ads(api_key, api_secret, status=10)
        else:
            ads = await okx_auth.get_my_ads(api_key, api_secret, extra)

        ad = next((a for a in ads if str(a["id"]) == str(ad_id)), None)
        if not ad:
            return web.json_response({"error": "Объявление не найдено"}, status=404)

        db_id = await db.repricer_add(
            uid, exchange, ad_id, ad["fiat"], ad["asset"], ad["side"],
            target_pos, delta, min_price, max_price, ad["price"],
        )

        rule = {
            "db_id":       db_id,
            "exchange":    exchange,
            "item_id":     ad_id,
            "fiat":        ad["fiat"],
            "asset":       ad["asset"],
            "side":        ad["side"],
            "target_pos":  target_pos,
            "delta":       delta,
            "min_price":   min_price,
            "max_price":   max_price,
            "current_price": ad["price"],
        }
        add_rule_memory(uid, rule)

        return web.json_response({
            "id": db_id, "exchange": exchange, "ad_id": ad_id,
            "fiat": ad["fiat"], "asset": ad["asset"], "side": ad["side"],
            "target_pos": target_pos, "delta": delta,
            "min_price": min_price, "max_price": max_price,
            "current_price": ad["price"],
        })
    except Exception as e:
        logger.error(f"api_repricer_add: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def api_repricer_delete(request: web.Request) -> web.Response:
    """DELETE /api/user/repricer/{id}?uid=xxx"""
    uid = _uid(request)
    if not uid:
        return web.json_response({"error": "uid required"}, status=400)
    try:
        rid = int(request.match_info["id"])
        await db.repricer_delete(uid, rid)
        remove_rule_memory(uid, rid)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ─── Subscription & P&L API ───────────────────────────────────────────────────

async def api_subscription(request: web.Request) -> web.Response:
    """GET /api/user/subscription?uid=xxx  — текущий план пользователя."""
    uid = _uid(request)
    if not uid:
        return web.json_response({"error": "uid required"}, status=400)
    try:
        sub      = await db.subscription_get(uid)
        plan_key = get_plan_key(sub)
        expires  = format_expires(sub) if sub else ""
        expires_iso = None
        if sub and sub.get("expires_at"):
            ea = sub["expires_at"]
            expires_iso = ea.isoformat() if hasattr(ea, "isoformat") else str(ea)
        return web.json_response({
            "plan":              plan_key,
            "expires_at":        expires_iso,
            "expires_formatted": expires,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_pnl(request: web.Request) -> web.Response:
    """GET /api/user/pnl?uid=xxx  — P&L статистика (только Pro/Team)."""
    uid = _uid(request)
    if not uid:
        return web.json_response({"error": "uid required"}, status=400)
    try:
        sub      = await db.subscription_get(uid)
        plan_key = get_plan_key(sub)
        if plan_key == "free":
            return web.json_response({"plan": "free", "has_access": False, "stats": None})

        api_key, api_secret, _ = get_account_credentials(uid, "bybit")
        if not api_key:
            return web.json_response({"error": "No active Bybit account"}, status=404)

        from api import bybit_auth as _ba
        orders = await _ba.get_p2p_orders(api_key, api_secret, status="50")
        stats  = calc_pnl(orders) if orders else None
        return web.json_response({"plan": plan_key, "has_access": True, "stats": stats})
    except Exception as e:
        logger.error(f"api_pnl uid={uid}: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def api_xtriangle(request: web.Request) -> web.Response:
    """GET /api/xtriangle/{fiat}/{asset} — МЕЖБИРЖЕВАЯ связка без карт.
    Собирает объявления со всех живых бирж, ищет лучшую исполнимую пару
    (купить на одной, продать на другой) с совпадением 3-х лиц + банка + лимитов."""
    fiat  = request.match_info["fiat"]
    asset = request.match_info["asset"]
    try:
        tasks, meta = [], []
        for ex_id, name, icon, _b, _s in _EX_META:
            if ex_id in DISABLED_EXCHANGES:
                continue
            meta.append((ex_id, name, icon))
            tasks.append(_fetch(ex_id, fiat, asset, "buy",  15))
            tasks.append(_fetch(ex_id, fiat, asset, "sell", 15))
        res = await asyncio.gather(*tasks, return_exceptions=True)

        all_buys, all_sells = [], []
        for i, (ex_id, name, icon) in enumerate(meta):
            b, s = res[i * 2], res[i * 2 + 1]
            if isinstance(b, list):
                eb = _enrich(b)
                for a in eb:
                    a["exchange"], a["ex_name"], a["ex_icon"] = ex_id, name, icon
                all_buys += eb
            if isinstance(s, list):
                es = _enrich(s)
                for a in es:
                    a["exchange"], a["ex_name"], a["ex_icon"] = ex_id, name, icon
                all_sells += es

        await _ai_overlay(all_buys + all_sells)   # семантика 3-х лиц/банка для связки
        link = _find_link(all_buys, all_sells, fiat)
        return web.json_response({"triangle": link})
    except Exception as e:
        logger.error(f"api_xtriangle: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def api_dashboard(request: web.Request) -> web.Response:
    """GET /api/dashboard?fiat=KZT&asset=USDT&uid=xxx — агрегированный дашборд."""
    fiat  = request.rel_url.query.get("fiat",  "KZT")
    asset = request.rel_url.query.get("asset", "USDT")
    uid   = _uid(request)
    try:
        exchanges = await _gather_exchanges(asset, fiat)
        arb       = _build_arbitrage(exchanges)

        tools: dict = {}
        if uid and db.ok():
            a_rows, t_rows, r_rows = await asyncio.gather(
                db.alerts_get(uid),
                db.trackers_get(uid),
                db.repricer_get(uid),
                return_exceptions=True,
            )
            tools = {
                "alerts":   len(a_rows) if isinstance(a_rows, list) else 0,
                "trackers": len(t_rows) if isinstance(t_rows, list) else 0,
                "repricer": len(r_rows) if isinstance(r_rows, list) else 0,
            }

        return web.json_response({
            "fiat":      fiat,
            "asset":     asset,
            "exchanges": exchanges,
            "best_arb":  arb[0] if arb else None,
            "earn":      _earn_demo(arb, fiat),
            "tools":     tools,
            "ts":        datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error(f"api_dashboard: {e}")
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
    # Accounts (repricer mini app)
    app.router.add_get("/api/user/accounts",                            api_accounts_list)
    app.router.add_post("/api/user/accounts",                           api_accounts_add)
    app.router.add_post("/api/user/accounts/{id}/activate",             api_accounts_activate)
    app.router.add_delete("/api/user/accounts/{id}",                    api_accounts_delete)
    app.router.add_get("/api/user/myads",                               api_myads)
    # Repricer rules
    app.router.add_get("/api/user/repricer",                            api_repricer_list)
    app.router.add_post("/api/user/repricer",                           api_repricer_add)
    app.router.add_delete("/api/user/repricer/{id}",                    api_repricer_delete)
    app.router.add_post("/api/ai",                                       api_ai)
    app.router.add_post("/api/chat",                                     api_chat)
    # Subscription & P&L
    app.router.add_get("/api/user/subscription",                         api_subscription)
    app.router.add_get("/api/user/pnl",                                  api_pnl)
    app.router.add_get("/api/dashboard",                                  api_dashboard)
    app.router.add_get("/api/xtriangle/{fiat}/{asset}",                   api_xtriangle)

    app.router.add_post("/api/antiscam/receipt",                          api_antiscam_receipt)
    app.router.add_get("/api/antiscam/nick/{nick}",                       api_antiscam_nick)
    app.router.add_post("/api/antiscam/report",                           api_antiscam_report)
    app.router.add_get("/api/whales/{fiat}",                              api_whales)

    # Health check
    app.router.add_get("/health",     health_handler)
    # Root → index.html
    app.router.add_get("/",           index_handler)
    app.router.add_get("/index.html", index_handler)

    return app


async def health_handler(request: web.Request) -> web.Response:
    """GET /health — liveness probe для мониторинга и Docker healthcheck."""
    uptime = round(_time.monotonic() - _start_time)
    return web.json_response({
        "status":    "ok",
        "db":        db.ok(),
        "uptime_s":  uptime,
        "ts":        datetime.now(timezone.utc).isoformat(),
    })


async def start_webapp(
    port: int = 8080,
    webhook_path: str | None = None,
    dp=None,
    bot=None,
    webhook_secret: str | None = None,
) -> web.AppRunner:
    """
    Запускает Mini App сервер в текущем event loop.
    Если переданы webhook_path + dp + bot — регистрирует Telegram webhook на том же порту.
    """
    app = create_app()

    if webhook_path and dp and bot:
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
        SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
            secret_token=webhook_secret or None,
        ).register(app, path=webhook_path)
        logger.info(f"Webhook registered at {webhook_path}")

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Mini App server: http://0.0.0.0:{port}  |  ngrok: ngrok http {port}")
    return runner
