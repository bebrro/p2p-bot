"""
Валидатор цен — health-check всех бирж перед деплоем.

Запуск:  python validate_prices.py

Что делает:
  • Дёргает все 4 биржи (Binance, Bybit, OKX, TG Wallet) по всем фиатам
  • Печатает таблицу: цена покупки / продажи / спред по каждой бирже
  • Считает лучший кросс-биржевой арбитраж
  • Помечает 🔴 биржи которые вернули пусто (мёртвый API)

Как пользоваться:
  Запусти → сверь цены глазами с реальными сайтами Binance/Bybit/OKX P2P.
  Если бот показывает то же что на сайте — данные корректны.
  Если биржа 🔴 или цена дикая — есть проблема с API.
"""
import asyncio
import sys

# UTF-8 вывод в Windows-консоли
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from api import binance_p2p, bybit_p2p, okx_p2p, wallet_p2p
from utils.spread import calc_spread
from utils.pricing import sane_price
from config import FIATS

ASSET = "USDT"

# (имя, функция_buy, функция_sell) — side-конвенции у бирж разные
_EXCHANGES = [
    ("🟡 Binance", lambda f: binance_p2p.get_best_price(ASSET, f, "BUY"),
                   lambda f: binance_p2p.get_best_price(ASSET, f, "SELL")),
    ("🟠 Bybit",   lambda f: bybit_p2p.get_best_price(ASSET, f, "1"),
                   lambda f: bybit_p2p.get_best_price(ASSET, f, "0")),
    ("🔵 OKX",     lambda f: okx_p2p.get_best_price(ASSET, f, "buy"),
                   lambda f: okx_p2p.get_best_price(ASSET, f, "sell")),
    ("💎 TG Wallet", lambda f: wallet_p2p.get_best_price(ASSET, f, "buy"),
                     lambda f: wallet_p2p.get_best_price(ASSET, f, "sell")),
]


def _v(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


async def _check_fiat(fiat: str) -> None:
    print(f"\n{'='*64}")
    print(f"  {ASSET}/{fiat}")
    print(f"{'='*64}")
    print(f"  {'Биржа':<14} {'Покупка':>10} {'Продажа':>10} {'Спред':>9}  Статус")
    print(f"  {'-'*58}")

    # Параллельно дёргаем все биржи (buy+sell)
    tasks = []
    for _, buy_fn, sell_fn in _EXCHANGES:
        tasks.append(buy_fn(fiat))
        tasks.append(sell_fn(fiat))
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_buys, all_sells = [], []
    for i, (name, _, _) in enumerate(_EXCHANGES):
        buy  = _v(results[i * 2])
        sell = _v(results[i * 2 + 1])

        # Статус
        if buy is None and sell is None:
            status = "🔴 МЁРТВ / пусто"
        elif buy is None or sell is None:
            status = "🟡 половина данных"
        elif not sane_price(buy, fiat) or not sane_price(sell, fiat):
            status = "🟠 цена вне диапазона!"
        else:
            status = "✅ ок"

        buy_s  = f"{buy:,.2f}"  if buy  else "—"
        sell_s = f"{sell:,.2f}" if sell else "—"
        if buy and sell:
            sp = calc_spread(buy, sell)
            sp_s = f"{sp['spread_pct']:+.2f}%"
            all_buys.append((name, buy))
            all_sells.append((name, sell))
        else:
            sp_s = "—"

        print(f"  {name:<14} {buy_s:>10} {sell_s:>10} {sp_s:>9}  {status}")

    # Лучший кросс-биржевой арбитраж
    if all_buys and all_sells:
        best_buy_ex,  best_buy  = min(all_buys,  key=lambda x: x[1])
        best_sell_ex, best_sell = max(all_sells, key=lambda x: x[1])
        if best_sell > best_buy:
            arb = calc_spread(best_buy, best_sell)
            print(f"  {'-'*58}")
            print(f"  🏆 Лучший арбитраж: купить на {best_buy_ex} ({best_buy:,.2f}) "
                  f"→ продать на {best_sell_ex} ({best_sell:,.2f})")
            print(f"     Спред: {arb['spread_pct']:+.2f}%  ({arb['spread_abs']:+,.2f} {fiat})")


async def main() -> None:
    print("\n🔍 ВАЛИДАТОР ЦЕН P2P — health-check всех бирж")
    print("   Сверь цены ниже с реальными сайтами P2P.\n")
    for fiat in FIATS:
        try:
            await _check_fiat(fiat)
        except Exception as e:
            print(f"\n  ❌ {fiat}: ошибка — {e}")

    print(f"\n{'='*64}")
    print("  Легенда: ✅ ок · 🟡 половина · 🟠 дикая цена · 🔴 мёртвый API")
    print(f"{'='*64}\n")

    from utils.http import close_session
    await close_session()


if __name__ == "__main__":
    asyncio.run(main())
