"""
Whale Tracker v2 — отслеживание крупных мерчантов в стакане.

Исправления v2:
• По умолчанию ВЫКЛЮЧЕН — пользователь сам нажимает ▶️ Запустить
• Первый скан только инициализирует состояние (без уведомлений)
• Кулдаун 20 мин на каждое уведомление об одном ките
• Кит считается "ушедшим" только после 2 скана отсутствия (нет ложных срабатываний)
• Выбор фиатов кнопками (KZT по умолчанию, не все сразу)
• При остановке — сброс инициализации (тихий старт при следующем включении)
"""
import asyncio
import time
from datetime import datetime
from aiogram import Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from api import binance_p2p, bybit_p2p
from config import FIATS, FIAT_FLAGS

router = Router()

# ── Константы ──────────────────────────────────────────────────────────────────
DEFAULT_THRESHOLD  = 50_000
VOLUME_CHANGE_PCT  = 30          # % изменения объёма для уведомления
NOTIF_COOLDOWN     = 20 * 60     # 20 мин между повторными уведомлениями об одном ките
ABSENT_BEFORE_GONE = 2           # сколько сканов отсутствия = "кит ушёл"
FIAT_ICONS = {"KZT": "🇰🇿", "RUB": "🇷🇺", "TRY": "🇹🇷", "USD": "🇺🇸"}

# ── Хранилище ─────────────────────────────────────────────────────────────────
# {user_id: {"enabled": bool, "threshold": float, "fiats": list, "exchanges": list}}
_user_settings: dict[int, dict] = {}

# {(ex, fiat, asset, side): {nick: {"volume", "price", "absent"}}}
_whale_state: dict[tuple, dict] = {}

# Пары прошедшие первый тихий скан
_initialized: set[tuple] = set()

# {(uid, ex, fiat, asset, side, nick, event): last_ts}
_last_notified: dict[tuple, float] = {}


class WhaleStates(StatesGroup):
    waiting_threshold = State()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _cfg(uid: int) -> dict:
    """Возвращает настройки, создаёт дефолт (выключен) если нет."""
    return _user_settings.setdefault(uid, {
        "enabled":   False,       # ← ВЫКЛЮЧЕН по умолчанию
        "threshold": DEFAULT_THRESHOLD,
        "fiats":     ["KZT"],     # ← только KZT по умолчанию
        "exchanges": ["binance", "bybit"],
    })


def _active_pairs(uid: int) -> list[tuple]:
    cfg = _cfg(uid)
    pairs = []
    for fiat in cfg["fiats"]:
        for ex in cfg["exchanges"]:
            for side in ("buy", "sell"):
                pairs.append((ex, fiat, "USDT", side))
    return pairs


def _can_notify(uid: int, pair: tuple, nick: str, event: str) -> bool:
    """Возвращает True и обновляет кулдаун, если уведомление можно слать."""
    key  = (uid, *pair, nick, event)
    now  = time.time()
    if now - _last_notified.get(key, 0) < NOTIF_COOLDOWN:
        return False
    _last_notified[key] = now
    return True


# ── UI ────────────────────────────────────────────────────────────────────────

def _main_kb(uid: int) -> InlineKeyboardMarkup:
    cfg          = _cfg(uid)
    on           = cfg["enabled"]
    thr          = cfg["threshold"]
    active_fiats = set(cfg["fiats"])

    thr_label = f"{thr/1000:.0f}k" if thr >= 1000 else str(int(thr))

    fiat_row = [
        InlineKeyboardButton(
            text=f"{FIAT_ICONS.get(f,'')}{f}{'✓' if f in active_fiats else ''}",
            callback_data=f"wt:fiat:{f}",
        )
        for f in FIATS
    ]

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="⏹ Остановить" if on else "▶️ Запустить",
                callback_data="wt:toggle",
            ),
            InlineKeyboardButton(text=f"⚖️ {thr_label}", callback_data="wt:threshold"),
        ],
        fiat_row,
        [InlineKeyboardButton(text="📊 Текущие киты", callback_data="wt:now")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")],
    ])


def _status_text(uid: int) -> str:
    cfg       = _cfg(uid)
    on        = cfg["enabled"]
    fiats_str = " | ".join(f"{FIAT_ICONS.get(f,'')}{f}" for f in cfg["fiats"]) or "—"
    ex_str    = " + ".join(
        "🟡 Binance" if e == "binance" else "🟠 Bybit"
        for e in cfg["exchanges"]
    )
    status = "✅ Работает" if on else "⏸ Остановлен"
    return (
        f"🐋 <b>Whale Tracker</b>\n\n"
        f"Уведомляю при появлении / уходе крупных мерчантов.\n"
        f"Порог: <b>{cfg['threshold']:,.0f}</b> фиата\n\n"
        f"Статус: {status}\n"
        f"Фиаты: {fiats_str}\n"
        f"Биржи: {ex_str}\n\n"
        f"<i>Отметь нужные фиаты → нажми ▶️ Запустить</i>"
    )


# ── Handlers ──────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "wt:list")
async def wt_list(callback: CallbackQuery):
    uid = callback.from_user.id
    _cfg(uid)   # создать запись (выключенную) если пользователь впервые
    await callback.message.edit_text(
        _status_text(uid),
        reply_markup=_main_kb(uid),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "wt:toggle")
async def wt_toggle(callback: CallbackQuery):
    uid = callback.from_user.id
    cfg = _cfg(uid)
    cfg["enabled"] = not cfg["enabled"]

    if not cfg["enabled"]:
        # Остановка — сбрасываем инициализацию для тихого старта при следующем включении
        for pair in _active_pairs(uid):
            _initialized.discard(pair)

    status = "✅ Запущен! Первый скан — тихий (состояние), уведомления со второго." \
             if cfg["enabled"] else "⏸ Остановлен."
    await callback.answer(status, show_alert=cfg["enabled"])
    await callback.message.edit_text(
        _status_text(uid),
        reply_markup=_main_kb(uid),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("wt:fiat:"))
async def wt_toggle_fiat(callback: CallbackQuery):
    uid  = callback.from_user.id
    cfg  = _cfg(uid)
    fiat = callback.data.split(":")[2]

    fiats = cfg["fiats"]
    if fiat in fiats:
        if len(fiats) == 1:
            await callback.answer("❗ Оставь хотя бы один фиат!", show_alert=True)
            return
        fiats.remove(fiat)
        # Убираем инициализацию для этого фиата
        for ex in cfg["exchanges"]:
            for side in ("buy", "sell"):
                _initialized.discard((ex, fiat, "USDT", side))
        await callback.answer(f"❌ {fiat} убран")
    else:
        fiats.append(fiat)
        await callback.answer(f"✅ {fiat} добавлен")

    await callback.message.edit_text(
        _status_text(uid),
        reply_markup=_main_kb(uid),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "wt:threshold")
async def wt_threshold(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WhaleStates.waiting_threshold)
    cfg = _cfg(callback.from_user.id)
    await callback.message.edit_text(
        f"⚖️ <b>Порог объёма кита</b>\n\n"
        f"Текущий: <b>{cfg['threshold']:,.0f}</b> фиата\n\n"
        f"Введи новое значение (число):\n"
        f"Например: <code>30000</code> или <code>100000</code>",
        parse_mode="HTML",
    )


@router.message(WhaleStates.waiting_threshold)
async def wt_got_threshold(message: Message, state: FSMContext):
    await state.clear()
    try:
        val = float(message.text.replace(" ", "").replace(",", "."))
        if val <= 0:
            raise ValueError
    except Exception:
        await message.answer(
            "❌ Введи положительное число, например: <code>50000</code>",
            parse_mode="HTML",
        )
        return
    _cfg(message.from_user.id)["threshold"] = val
    await message.answer(
        f"✅ Порог установлен: <b>{val:,.0f}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🐋 Whale Tracker", callback_data="wt:list")]
        ]),
    )


@router.callback_query(lambda c: c.data == "wt:now")
async def wt_now(callback: CallbackQuery):
    uid = callback.from_user.id
    cfg = _cfg(uid)
    await callback.message.edit_text("⏳ Сканирую стаканы...")

    pairs   = _active_pairs(uid)
    results = await asyncio.gather(*[
        _fetch_whales(ex, fiat, asset, side, cfg["threshold"])
        for ex, fiat, asset, side in pairs
    ], return_exceptions=True)

    lines     = [f"🐋 <b>Текущие киты</b> (> {cfg['threshold']:,.0f})\n"]
    found_any = False

    for (ex, fiat, asset, side), whales in zip(pairs, results):
        if isinstance(whales, Exception) or not whales:
            continue
        found_any = True
        ex_em  = "🟡" if ex == "binance" else "🟠"
        side_s = "покупка" if side == "buy" else "продажа"
        lines.append(f"\n{ex_em} {asset}/{fiat} | {side_s}")
        for nick, info in sorted(whales.items(), key=lambda x: x[1]["volume"], reverse=True)[:5]:
            lines.append(f"  🐋 <b>{nick}</b> — {info['volume']/1000:.1f}k @ {info['price']:,.2f}")

    if not found_any:
        lines.append("Китов не обнаружено.\nСнизь порог или добавь другие фиаты.")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="wt:now")],
            [InlineKeyboardButton(text="⬅️ Назад",    callback_data="wt:list")],
        ]),
        parse_mode="HTML",
    )


# ── Fetch ─────────────────────────────────────────────────────────────────────

async def _fetch_whales(
    exchange: str, fiat: str, asset: str, side: str, threshold: float
) -> dict:
    """Возвращает {nickname: {volume, price}} для мерчантов выше порога."""
    try:
        if exchange == "binance":
            ads = await binance_p2p.get_ads(
                asset=asset, fiat=fiat,
                trade_type="BUY" if side == "buy" else "SELL",
                rows=30,
            )
        else:
            ads = await bybit_p2p.get_ads(
                asset=asset, fiat=fiat,
                side="1" if side == "buy" else "0",
                size=30,
            )
        whales = {}
        for ad in ads:
            fiat_vol = ad["available"] * ad["price"]
            if fiat_vol >= threshold:
                whales[ad["nickname"]] = {
                    "volume": fiat_vol,
                    "price":  ad["price"],
                }
        return whales
    except Exception:
        return {}


# ── Background check ──────────────────────────────────────────────────────────

async def check_whales(bot) -> None:
    """Вызывается каждые 120 секунд из bot.py"""
    import logging
    logger = logging.getLogger(__name__)

    # Собираем активных пользователей и уникальные пары
    all_pairs: set[tuple]          = set()
    active_users: list[tuple]      = []
    user_pairs:   dict[int, set]   = {}   # uid → set of pairs

    for uid, cfg in list(_user_settings.items()):
        if not cfg.get("enabled", False):
            continue
        pairs = set(_active_pairs(uid))
        active_users.append((uid, cfg))
        user_pairs[uid] = pairs
        all_pairs |= pairs

    if not active_users or not all_pairs:
        return

    min_threshold = min(cfg["threshold"] for _, cfg in active_users)

    # Параллельный запрос всех пар
    pairs_list = list(all_pairs)
    raw = await asyncio.gather(
        *[_fetch_whales(ex, fiat, asset, side, min_threshold)
          for ex, fiat, asset, side in pairs_list],
        return_exceptions=True,
    )
    fresh = {
        pair: res for pair, res in zip(pairs_list, raw)
        if isinstance(res, dict)
    }

    now_s = datetime.now().strftime("%H:%M")

    for pair, new_whales in fresh.items():
        ex, fiat, asset, side = pair
        ex_em  = "🟡 Binance" if ex == "binance" else "🟠 Bybit"
        side_s = "покупка"    if side == "buy"    else "продажа"
        label  = f"{ex_em} | {asset}/{fiat} | {side_s}"

        # ── Первый скан: тихая инициализация ──────────────────────────────
        if pair not in _initialized:
            _whale_state[pair] = {
                nick: {**info, "absent": 0}
                for nick, info in new_whales.items()
            }
            _initialized.add(pair)
            logger.info(f"[WhaleTracker] init {pair}: {len(new_whales)} whales")
            continue

        old_state = _whale_state.get(pair, {})

        # ── Вычисляем изменения ────────────────────────────────────────────

        # Новые (появились)
        appeared = {n: v for n, v in new_whales.items() if n not in old_state}

        # Строим обновлённое состояние
        updated: dict = {}
        for nick, info in new_whales.items():
            updated[nick] = {**info, "absent": 0}
        for nick, old_info in old_state.items():
            if nick not in new_whales:
                updated[nick] = {**old_info, "absent": old_info.get("absent", 0) + 1}

        # Ушедшие = absent ровно ABSENT_BEFORE_GONE (не 1, не 3 — ровно порог)
        departed = {
            nick: info
            for nick, info in updated.items()
            if info.get("absent", 0) == ABSENT_BEFORE_GONE
        }

        # Резкое изменение объёма (только у тех кто в стакане)
        changed: dict = {}
        for nick in new_whales:
            if nick in old_state and old_state[nick].get("absent", 0) == 0:
                old_vol = old_state[nick]["volume"]
                new_vol = new_whales[nick]["volume"]
                if old_vol > 0:
                    pct = abs(new_vol - old_vol) / old_vol * 100
                    if pct >= VOLUME_CHANGE_PCT:
                        changed[nick] = {
                            "old":   old_vol,
                            "new":   new_vol,
                            "pct":   pct,
                            "price": new_whales[nick]["price"],
                        }

        # Чистим тех кто долго отсутствует из памяти
        _whale_state[pair] = {
            nick: info for nick, info in updated.items()
            if info.get("absent", 0) <= ABSENT_BEFORE_GONE + 3
        }

        # ── Уведомляем ────────────────────────────────────────────────────
        for uid, cfg in active_users:
            if pair not in user_pairs.get(uid, set()):
                continue   # эта пара не в настройках пользователя
            threshold = cfg["threshold"]

            # Появился
            for nick, info in appeared.items():
                if info["volume"] < threshold:
                    continue
                if not _can_notify(uid, pair, nick, "appeared"):
                    continue
                try:
                    await bot.send_message(
                        uid,
                        f"🐋 <b>Кит появился!</b> [{now_s}]\n"
                        f"{label}\n\n"
                        f"👤 <b>{nick}</b>\n"
                        f"💰 Цена: {info['price']:,.2f}\n"
                        f"📦 Объём: {info['volume']/1000:.1f}k {fiat}\n\n"
                        f"⚡ Крупный игрок вошёл в стакан.",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"[whale appeared] uid={uid}: {e}")

            # Ушёл
            for nick, info in departed.items():
                if info["volume"] < threshold:
                    continue
                if not _can_notify(uid, pair, nick, "departed"):
                    continue
                try:
                    await bot.send_message(
                        uid,
                        f"🦅 <b>Кит ушёл!</b> [{now_s}]\n"
                        f"{label}\n\n"
                        f"👤 <b>{nick}</b> покинул стакан\n"
                        f"📦 Был объём: {info['volume']/1000:.1f}k {fiat} @ {info['price']:,.2f}\n\n"
                        f"💡 <b>Окно возможностей:</b>\n"
                        f"Место свободно — рассмотри выставить рядом с {info['price']:,.2f}",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"[whale departed] uid={uid}: {e}")

            # Изменил объём
            for nick, info in changed.items():
                if max(info["old"], info["new"]) < threshold:
                    continue
                if not _can_notify(uid, pair, nick, "changed"):
                    continue
                direction = "📈 вырос" if info["new"] > info["old"] else "📉 упал"
                try:
                    await bot.send_message(
                        uid,
                        f"🐋 <b>Кит изменил объём!</b> [{now_s}]\n"
                        f"{label}\n\n"
                        f"👤 <b>{nick}</b>\n"
                        f"💰 Цена: {info['price']:,.2f}\n"
                        f"📦 Объём {direction} на {info['pct']:.0f}%\n"
                        f"   {info['old']/1000:.1f}k → {info['new']/1000:.1f}k {fiat}",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"[whale changed] uid={uid}: {e}")
