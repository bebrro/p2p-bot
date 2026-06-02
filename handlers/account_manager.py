"""
Управление API аккаунтами (Bybit и OKX).
Хранит зашифрованные ключи в БД; при старте загружает в память.

Безопасность:
• Ключи шифруются через AES (Fernet) перед записью в БД
• Сообщения с ключами удаляются из чата сразу
• Ключи никогда не попадают в логи
• Пользователь создаёт ключи БЕЗ права вывода средств
"""
import logging
from aiogram import Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from api import bybit_auth, okx_auth, binance_auth
from utils.encryption import encrypt, decrypt
import db

logger = logging.getLogger(__name__)
router = Router()

# {user_id: [{"id", "exchange", "label", "api_key"(enc), "api_secret"(enc),
#             "extra"(enc), "nickname", "active"}]}
_accounts: dict[int, list] = {}
MAX_ACCOUNTS = 5   # per user total (all exchanges)

# Кому уже показали безопасный гайд (чтобы не вываливать стену каждый раз)
_guide_seen: set[int] = set()

_API_GUIDE = (
    "🔑 <b>Как добавить API-ключ безопасно</b>\n\n"
    "Ключ нужен, чтобы бот сам держал твои объявления в топе (репрайсер) и "
    "считал P&amp;L. <b>Доступ к деньгам боту НЕ нужен.</b>\n\n"
    "<b>🟠 Bybit:</b> Профиль → API → Create New Key → System-generated.\n"
    "<b>🔵 OKX:</b> Профиль → API → Create API Key (придумай Passphrase).\n\n"
    "<b>⚠️ Права — самое важное:</b>\n"
    "✅ Включи только <b>P2P / Trade</b> (управление объявлениями)\n"
    "❌ <b>Withdraw / Вывод — НИКОГДА не включай!</b>\n\n"
    "Без права вывода деньги в безопасности даже если ключ утечёт — максимум "
    "переставят цену объявления.\n\n"
    "🔒 Бот шифрует ключи и удаляет твои сообщения с ними из чата сразу."
)


def _guide_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Понятно — добавить ключ", callback_data="acc:add:start")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")],
    ])

EXCHANGE_NAMES = {"bybit": "🟠 Bybit", "okx": "🔵 OKX", "binance": "🟡 Binance"}


class AccStates(StatesGroup):
    waiting_exchange = State()   # не используется напрямую — заменён inline
    waiting_label    = State()
    waiting_key      = State()
    waiting_secret   = State()
    waiting_extra    = State()   # OKX passphrase


# ─── DB-sync helpers ──────────────────────────────────────────────────────────

async def load_from_db() -> None:
    """Вызвать один раз при старте из bot.py."""
    rows = await db.accounts_get_all()
    for r in rows:
        uid = r["user_id"]
        if uid not in _accounts:
            _accounts[uid] = []
        _accounts[uid].append({
            "id":       r["id"],
            "exchange": r["exchange"],
            "label":    r["label"],
            "api_key":  r["api_key"],
            "api_secret": r["api_secret"],
            "extra":    r.get("extra", ""),
            "nickname": r.get("nickname", ""),
            "active":   r["is_active"],
        })
    logger.info(f"Загружено {len(rows)} аккаунтов из БД")


# ─── Public helpers ────────────────────────────────────────────────────────────

def get_active_account(user_id: int, exchange: str = "bybit") -> dict | None:
    for acc in _accounts.get(user_id, []):
        if acc.get("active") and acc.get("exchange", "bybit") == exchange:
            return acc
    return None


def get_account_credentials(user_id: int,
                             exchange: str = "bybit") -> tuple[str | None, str | None, str | None]:
    """
    Возвращает (api_key, api_secret, extra) активного аккаунта для биржи.
    extra = passphrase для OKX, '' для Bybit.
    """
    acc = get_active_account(user_id, exchange)
    if not acc:
        return None, None, None
    try:
        extra = decrypt(acc["extra"]) if acc.get("extra") else ""
        return decrypt(acc["api_key"]), decrypt(acc["api_secret"]), extra
    except Exception:
        return None, None, None


def _all_accounts(user_id: int) -> list:
    return _accounts.get(user_id, [])


def upsert_account_memory(uid: int, acc: dict) -> None:
    """Добавляет/обновляет аккаунт в памяти (для вызова из server.py)."""
    if uid not in _accounts:
        _accounts[uid] = []
    for a in _accounts[uid]:
        if a.get("id") == acc.get("id"):
            a.update(acc)
            return
    _accounts[uid].append(acc)


def remove_account_memory(uid: int, account_id: int) -> None:
    """Удаляет аккаунт из памяти."""
    if uid in _accounts:
        _accounts[uid] = [a for a in _accounts[uid] if a.get("id") != account_id]


def _accounts_kb(user_id: int) -> InlineKeyboardMarkup:
    accs    = _all_accounts(user_id)
    buttons = []
    for i, acc in enumerate(accs):
        ex_name = EXCHANGE_NAMES.get(acc.get("exchange", "bybit"), acc.get("exchange", ""))
        mark    = "✅ " if acc.get("active") else ""
        buttons.append([
            InlineKeyboardButton(
                text=f"{mark}{ex_name} {acc['label']} ({acc.get('nickname', '?')})",
                callback_data=f"acc:select:{i}",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"acc:del:{i}"),
        ])
    if len(accs) < MAX_ACCOUNTS:
        buttons.append([InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="acc:add:start")])
    buttons.append([InlineKeyboardButton(text="🔒 Как добавить ключ безопасно", callback_data="acc:guide")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад",         callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─── Handlers ──────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data in ("acc:guide", "acc:list"))
async def acc_list(callback: CallbackQuery):
    uid    = callback.from_user.id
    accs   = _all_accounts(uid)

    # Первый заход (ключей ещё нет) или явный запрос гайда → пошаговая инструкция
    if callback.data == "acc:guide" or (uid not in _guide_seen and not accs):
        _guide_seen.add(uid)
        await callback.message.edit_text(_API_GUIDE, reply_markup=_guide_kb(),
                                         parse_mode="HTML", disable_web_page_preview=True)
        return

    bybit_active = get_active_account(uid, "bybit")
    okx_active   = get_active_account(uid, "okx")

    lines = [f"🔑 <b>API аккаунты</b>", f"Аккаунтов: {len(accs)}/{MAX_ACCOUNTS}"]
    if bybit_active:
        lines.append(f"Bybit активный: <b>{bybit_active['label']}</b>")
    if okx_active:
        lines.append(f"OKX активный: <b>{okx_active['label']}</b>")
    if not bybit_active and not okx_active:
        lines.append("Нет активных аккаунтов.")
    lines.append("\nНажми на аккаунт — активировать, 🗑 — удалить.")

    await callback.message.edit_text(
        "\n".join(lines), reply_markup=_accounts_kb(uid), parse_mode="HTML"
    )


@router.callback_query(lambda c: c.data == "acc:security")
async def acc_security(callback: CallbackQuery):
    await callback.message.edit_text(
        "🔒 <b>Безопасность API ключей</b>\n\n"
        "1️⃣ <b>Шифрование AES-128 (Fernet)</b>\n"
        "   Ключи хранятся только в зашифрованном виде в БД.\n"
        "   Расшифровка только в памяти при API-вызове.\n\n"
        "2️⃣ <b>Сообщения удаляются</b>\n"
        "   Сообщения с ключами исчезают из чата сразу.\n\n"
        "3️⃣ <b>Нулевое логирование</b>\n"
        "   API ключи никогда не попадают в логи.\n\n"
        "4️⃣ <b>Минимальные права</b>\n"
        "   <b>Bybit:</b> только ✅ P2P, без вывода\n"
        "   <b>OKX:</b> только ✅ Trade (C2C), без вывода\n"
        "   <b>Binance:</b> только ✅ Enable Reading, без вывода\n\n"
        "5️⃣ <b>IP-вайтлист (рекомендуется)</b>\n"
        "   Укажи IP Railway-сервера в настройках ключа.\n\n"
        "⚡ Без права вывода никто не заберёт средства\n"
        "даже при утечке ключа.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="acc:list")]
        ]),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "acc:add:start")
async def acc_add_start(callback: CallbackQuery):
    uid = callback.from_user.id
    if len(_all_accounts(uid)) >= MAX_ACCOUNTS:
        await callback.answer(f"❌ Максимум {MAX_ACCOUNTS} аккаунтов", show_alert=True)
        return
    await callback.message.edit_text(
        "🔑 <b>Добавление аккаунта</b>\n\nВыбери биржу:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🟠 Bybit",   callback_data="acc:add:bybit"),
                InlineKeyboardButton(text="🔵 OKX",     callback_data="acc:add:okx"),
            ],
            [
                InlineKeyboardButton(text="🟡 Binance", callback_data="acc:add:binance"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="acc:list")],
        ]),
    )


@router.callback_query(lambda c: c.data in ("acc:add:bybit", "acc:add:okx", "acc:add:binance"))
async def acc_add_exchange(callback: CallbackQuery, state: FSMContext):
    exchange = callback.data.split(":")[-1]
    await state.set_state(AccStates.waiting_label)
    await state.update_data(exchange=exchange)
    ex_names = {"bybit": "Bybit", "okx": "OKX", "binance": "Binance"}
    ex_name = ex_names.get(exchange, exchange.title())
    steps = "3" if exchange == "okx" else "3"
    await callback.message.edit_text(
        f"🔑 <b>Добавление {ex_name}</b>\n\n"
        f"Шаг 1/{steps} — Введи <b>название</b> (например: <code>Основной</code>)",
        parse_mode="HTML",
    )


@router.message(AccStates.waiting_label)
async def acc_got_label(message: Message, state: FSMContext):
    await state.update_data(label=message.text.strip()[:30])
    data    = await state.get_data()
    exchange = data.get("exchange", "bybit")
    await state.set_state(AccStates.waiting_key)
    hints = {
        "bybit":   "Bybit: Профиль → API → Создать ключ → P2P ✅, вывод ❌",
        "okx":     "OKX: Профиль → API → Создать → Trade ✅, вывод ❌",
        "binance": "Binance: Профиль → Управление API → Создать → Enable Reading ✅, вывод ❌",
    }
    hint = hints.get(exchange, "")
    await message.answer(
        f"🔑 Шаг 2/3 — Введи <b>API Key</b>\n\n{hint}\n\n"
        "⚠️ Сообщение удалится сразу после отправки.",
        parse_mode="HTML",
    )


@router.message(AccStates.waiting_key)
async def acc_got_key(message: Message, state: FSMContext):
    raw_key = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    await state.update_data(raw_key=raw_key)
    await state.set_state(AccStates.waiting_secret)
    await message.answer(
        "🔑 Шаг 3/3 — Введи <b>API Secret</b>\n\n"
        "⚠️ Сообщение удалится сразу после отправки.",
        parse_mode="HTML",
    )


@router.message(AccStates.waiting_secret)
async def acc_got_secret(message: Message, state: FSMContext):
    raw_secret = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    data     = await state.get_data()
    exchange = data.get("exchange", "bybit")
    await state.update_data(raw_secret=raw_secret)

    if exchange == "okx":
        # OKX требует passphrase
        await state.set_state(AccStates.waiting_extra)
        await message.answer(
            "🔑 Доп. шаг — Введи <b>Passphrase</b>\n\n"
            "Это пароль который ты задал при создании API ключа на OKX.\n"
            "⚠️ Сообщение удалится сразу после отправки.",
            parse_mode="HTML",
        )
    else:
        # Bybit / Binance — passphrase не нужен, сразу проверяем
        await _finalize_account(message, state, passphrase="")


@router.message(AccStates.waiting_extra)
async def acc_got_extra(message: Message, state: FSMContext):
    passphrase = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    await _finalize_account(message, state, passphrase=passphrase)


async def _finalize_account(message: Message, state: FSMContext, passphrase: str):
    data     = await state.get_data()
    await state.clear()

    exchange   = data.get("exchange", "bybit")
    label      = data["label"]
    raw_key    = data["raw_key"]
    raw_secret = data["raw_secret"]

    wait_msg = await message.answer("⏳ Проверяю ключ...")

    # Верификация
    if exchange == "bybit":
        is_valid, result = await bybit_auth.verify_api_key(raw_key, raw_secret)
    elif exchange == "okx":
        is_valid, result = await okx_auth.verify_api_key(raw_key, raw_secret, passphrase)
    elif exchange == "binance":
        is_valid, result = await binance_auth.verify_api_key(raw_key, raw_secret)
    else:
        is_valid, result = False, f"Неизвестная биржа: {exchange}"

    if not is_valid:
        await wait_msg.edit_text(
            f"❌ Ключ не прошёл проверку:\n<code>{result}</code>\n\n"
            "Проверь правильность ключа и права доступа.",
            parse_mode="HTML",
        )
        return

    uid     = message.from_user.id
    enc_key = encrypt(raw_key)
    enc_sec = encrypt(raw_secret)
    enc_ext = encrypt(passphrase) if passphrase else ""

    # Сохраняем в БД
    acc_id = await db.accounts_add(uid, exchange, label, enc_key, enc_sec, enc_ext, result)

    # Обновляем память
    if uid not in _accounts:
        _accounts[uid] = []
    new_acc = {
        "id":         acc_id,
        "exchange":   exchange,
        "label":      label,
        "api_key":    enc_key,
        "api_secret": enc_sec,
        "extra":      enc_ext,
        "nickname":   result,
        "active":     False,
    }
    _accounts[uid].append(new_acc)

    # Если это первый аккаунт для данной биржи — делаем активным
    same_ex = [a for a in _accounts[uid] if a.get("exchange") == exchange and a is not new_acc]
    if not any(a.get("active") for a in same_ex):
        new_acc["active"] = True
        await db.accounts_set_active(uid, exchange, acc_id)

    ex_name = EXCHANGE_NAMES.get(exchange, exchange.title())
    await wait_msg.edit_text(
        f"✅ Аккаунт <b>{label}</b> добавлен!\n"
        f"Биржа: {ex_name} | Никнейм: <b>{result}</b>\n\n"
        "Ключ зашифрован и сохранён в БД.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Мои аккаунты",   callback_data="acc:list")],
            [InlineKeyboardButton(text="🔄 Авто-переценка", callback_data="rp:list")],
        ]),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("acc:select:"))
async def acc_select(callback: CallbackQuery):
    idx  = int(callback.data.split(":")[2])
    uid  = callback.from_user.id
    accs = _all_accounts(uid)
    if 0 <= idx < len(accs):
        selected = accs[idx]
        exchange = selected.get("exchange", "bybit")
        # В памяти деактивируем все для этой биржи, активируем выбранный
        for a in accs:
            if a.get("exchange") == exchange:
                a["active"] = False
        selected["active"] = True
        # В БД
        if db.ok():
            await db.accounts_set_active(uid, exchange, selected["id"])
        await callback.answer(f"✅ Активен: {selected['label']}")
    await acc_list(callback)


@router.callback_query(lambda c: c.data and c.data.startswith("acc:del:"))
async def acc_del(callback: CallbackQuery):
    idx  = int(callback.data.split(":")[2])
    uid  = callback.from_user.id
    accs = _all_accounts(uid)
    if 0 <= idx < len(accs):
        removed  = accs.pop(idx)
        exchange = removed.get("exchange", "bybit")
        # Если удалили активный — активируем первый оставшийся для этой биржи
        same = [a for a in accs if a.get("exchange") == exchange]
        if same and not any(a["active"] for a in same):
            same[0]["active"] = True
            if db.ok():
                await db.accounts_set_active(uid, exchange, same[0]["id"])
        # Удаляем из БД
        if db.ok() and removed.get("id"):
            await db.accounts_delete(uid, removed["id"])
        await callback.answer("✅ Удалён")
    await acc_list(callback)
