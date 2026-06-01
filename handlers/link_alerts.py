"""
Алерты на СВЯЗКИ без карт в личку.

Бот сам пишет, когда по подписанному фиату появилась исполнимая связка
(межбиржевая или внутри биржи) с ЧИСТОЙ прибылью ≥ порога — не надо сидеть
в мини-аппе.

Переиспользует подписки арбитража (тот же fiat + threshold), отдельной
настройки не требует. Кулдаун против спама. Приманки (suspicious) не шлёт.
"""
import time
import logging

from handlers.arbitrage import get_all_arb_alerts

logger = logging.getLogger(__name__)

# "{uid}:{fiat}:link" → unix ts последнего уведомления
_last_fired: dict[str, float] = {}
COOLDOWN_SEC = 1800          # 30 минут между повторами по одному фиату


def _fmt(n) -> str:
    try:
        return f"{float(n):,.0f}".replace(",", " ")
    except Exception:
        return str(n)


def _build_msg(link: dict, fiat: str) -> str:
    b, s = link["buy"], link["sell"]
    cross = link.get("cross")
    title = "🔺 <b>Связка без карт · МЕЖБИРЖА</b>" if cross else "🔺 <b>Связка без карт</b>"
    b_ex = f"{b.get('ex_icon','')} {b.get('ex_name','')}".strip()
    s_ex = f"{s.get('ex_icon','')} {s.get('ex_name','')}".strip()
    banks = link.get("banks") or []
    banks_line = f"🏦 Банк: {', '.join(banks)}\n" if banks else ""
    net = link.get("profit_net", link.get("profit", 0))
    pct_net = link.get("pct_net", link.get("pct", 0))
    fee = link.get("fee_fiat", 0)
    lo, hi = link.get("lo"), link.get("hi")
    vol_line = f"{_fmt(lo)}{('–' + _fmt(hi)) if hi else '+'} {fiat}"
    return (
        f"{title}\n\n"
        f"📥 Купить USDT: <b>{_fmt(b.get('price'))}</b> {fiat}"
        f"{(' · ' + b_ex) if b_ex else ''}\n"
        f"   {b.get('nickname','')}\n"
        f"📤 Продать USDT: <b>{_fmt(s.get('price'))}</b> {fiat}"
        f"{(' · ' + s_ex) if s_ex else ''}\n"
        f"   {s.get('nickname','')}\n\n"
        f"{banks_line}"
        f"💵 Чистыми на обороте {_fmt(link.get('amount'))} {fiat} → "
        f"<b>+{_fmt(net)} {fiat}</b> ({'+' if pct_net >= 0 else ''}{pct_net}%)\n"
        f"   издержки −{_fmt(fee)} {fiat}\n"
        f"✅ Исполнимый объём: {vol_line}"
    )


async def check_link_alerts(bot) -> None:
    """Вызывается из bot.py каждые ~120 сек."""
    subs = get_all_arb_alerts()
    if not subs:
        return

    pairs: set[tuple] = set()
    for alerts in subs.values():
        for a in alerts:
            pairs.add((a["fiat"], a.get("asset", "USDT")))
    if not pairs:
        return

    # считаем связки по уникальным фиатам (тяжёлая операция — один раз на фиат)
    from webapp.server import best_link
    links: dict[tuple, dict] = {}
    for fiat, asset in pairs:
        try:
            lk = await best_link(fiat, asset)
            if lk:
                links[(fiat, asset)] = lk
        except Exception as e:
            logger.warning("link alert calc %s/%s: %s", fiat, asset, e)

    now = time.time()
    for uid, alerts in list(subs.items()):
        for a in alerts:
            link = links.get((a["fiat"], a.get("asset", "USDT")))
            if not link or link.get("suspicious"):
                continue
            pct = link.get("pct_net", link.get("pct", 0))
            if pct < a["threshold"]:
                continue
            key = f"{uid}:{a['fiat']}:link"
            if now - _last_fired.get(key, 0) < COOLDOWN_SEC:
                continue
            _last_fired[key] = now
            try:
                await bot.send_message(uid, _build_msg(link, a["fiat"]), parse_mode="HTML")
            except Exception as e:
                logger.error("link alert dm uid=%s: %s", uid, e)
