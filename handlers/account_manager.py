"""
Управление API аккаунтами Bybit.
Хранит ключи в зашифрованном виде (AES через Fernet).

Безопасность:
• Ключи шифруются сразу при вводе и никогда не логируются
• Сообщения с ключами удаляются из чата
• При каждом API-вызове ключ расшифровывается только в памяти
• Пользователь должен создавать ключи БЕЗ права вывода средств
"""
from aiogram import Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from api import bybit_auth
from utils.encryption import encrypt, decrypt

router = Router()

# {user_id: [{"label", "api_key"(enc), "api_secret"(enc), "nickname", "active"}]}
_accounts: dict[int, list] = {}
MAX_ACCOUNTS = 5


class AccStates(StatesGroup):
    waiting_label  = State()
    waiting_key    = State()
    waiting_secret = State()


# ─── Helpers ───────────────────────────────────────────────────────────────────

def get_active_account(user_id: int) -> dict | None:
    for acc in _accounts.get(user_id, []):
        if acc.get("active"):
            return acc
    return None


def get_account_credentials(user_id: int) -> tuple[str | None, str | None]:
    """Возвращает (api_key, api_secret) активного аккаунта или (None, None)."""
    acc = get_active_account(user_id)
    if not acc:
        return None, None
    try:
        return decrypt(acc["api_key"]), decrypt(acc["api_secret"])
    except Exception:
        return None, None


def _accounts_kb(user_id: int) -> InlineKeyboardMarkup:
    accounts = _accounts.get(user_id, [])
    buttons  = []
    for i, acc in enumerate(accounts):
        mark = "✅ " if acc.get("active") else ""
        buttons.append([
            InlineKeyboardButton(
                text=f"{mark}🔑 {acc['label']} ({acc.get('nickname', '?')})",
                callback_data=f"acc:select:{i}",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"acc:del:{i}"),
        ])
    if len(accounts) < MAX_ACCOUNTS:
        buttons.append([InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="acc:add:start")])
    buttons.append([InlineKeyboardButton(text="ℹ️ Безопасность", callback_data="acc:security")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад",         callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─── Handlers ──────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "acc:list")
async def acc_list(callback: CallbackQuery):
    uid  = callback.from_user.id
    accs = _accounts.get(uid, [])
    active = get_active_account(uid)
    text = (
        "🔑 <b>API аккаунты Bybit</b>\n\n"
        f"Аккаунтов: {len(accs)}/{MAX_ACCOUNTS}\n"
        + (f"Активный: <b>{active['label']}</b> ({active.get('nickname','?')})\n" if active else "Нет активного аккаунта.\n")
        + "\nНажми на аккаунт чтобы активировать, 🗑 — удалить."
    )
    await callback.message.edit_text(text, reply_markup=_accounts_kb(uid), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "acc:security")
async def acc_security(callback: CallbackQuery):
    await callback.message.edit_text(
        "🔒 <b>Как мы защищаем твои ключи</b>\n\n"
        "1️⃣ <b>Шифрование AES-128</b>\n"
        "   Ключи хранятся только в зашифрованном виде.\n"
        "   Расшифровка только в памяти при API-вызове.\n\n"
        "2️⃣ <b>Немедленное удаление</b>\n"
        "   Сообщения с ключами удаляются из чата сразу.\n\n"
        "3️⃣ <b>Нулевое логирование</b>\n"
        "   API ключи никогда не попадают в логи.\n\n"
        "4️⃣ <b>Минимальные права</b>\n"
        "   Создавай ключ с правами только:\n"
        "   ✅ P2P торговля / чтение\n"
        "   ❌ БЕЗ вывода средств\n"
        "   ❌ БЕЗ спотовой торговли\n\n"
        "5️⃣ <b>IP-вайтлист (рекомендуется)</b>\n"
        "   В настройках Bybit укажи IP сервера бота.\n\n"
        "⚡ Даже если ключ утечёт — без права вывода\n"
        "никто не сможет взять твои деньги.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="acc:list")]
        ]),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "acc:add:start")
async def acc_add_start(callback: CallbackQuery, state: FSMContext):
    if len(_accounts.get(callback.from_user.id, [])) >= MAX_ACCOUNTS:
        await callback.answer(f"❌ Максимум {MAX_ACCOUNTS} аккаунтов", show_alert=True)
        return
    await state.set_state(AccStates.waiting_label)
    await callback.message.edit_text(
        "🔑 <b>Добавление аккаунта Bybit</b>\n\n"
        "Шаг 1/3 — Введи название (например: <b>Основной</b> или <b>Работа</b>)",
        parse_mode="HTML",
    )


@router.message(AccStates.waiting_label)
async def acc_got_label(message: Message, state: FSMContext):
    await state.update_data(label=message.text.strip()[:30])
    await state.set_state(AccStates.waiting_key)
    await message.answer(
        "🔑 Шаг 2/3 — Введи <b>API Key</b>\n\n"
        "Как создать ключ на Bybit:\n"
        "Профиль → API → Создать ключ\n"
        "Права: ✅ P2P,  ❌ без вывода\n\n"
        "⚠️ Сообщение будет удалено сразу после отправки.",
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
        "⚠️ Сообщение будет удалено сразу после отправки.",
        parse_mode="HTML",
    )


@router.message(AccStates.waiting_secret)
async def acc_got_secret(message: Message, state: FSMContext):
    raw_secret = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    await state.clear()

    wait_msg = await message.answer("⏳ Проверяю ключ...")
    is_valid, result = await bybit_auth.verify_api_key(data["raw_key"], raw_secret)

    if not is_valid:
        await wait_msg.edit_text(
            f"❌ Ключ не прошёл проверку:\n<code>{result}</code>\n\n"
            "Проверь правильность ключа и права доступа.\n"
            "Попробуй ещё раз: /start → 🔑 Аккаунты",
            parse_mode="HTML",
        )
        return

    uid = message.from_user.id
    if uid not in _accounts:
        _accounts[uid] = []

    # Новый аккаунт сразу становится активным
    for acc in _accounts[uid]:
        acc["active"] = False

    _accounts[uid].append({
        "label":      data["label"],
        "api_key":    encrypt(data["raw_key"]),
        "api_secret": encrypt(raw_secret),
        "nickname":   result,
        "active":     True,
    })

    await wait_msg.edit_text(
        f"✅ Аккаунт <b>{data['label']}</b> добавлен!\n"
        f"Никнейм Bybit: <b>{result}</b>\n\n"
        "Ключ зашифрован и сохранён.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Мои аккаунты",   callback_data="acc:list")],
            [InlineKeyboardButton(text="🔄 Авто-переценка", callback_data="rp:list")],
            [InlineKeyboardButton(text="📊 Статистика",     callback_data="stats:view")],
        ]),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("acc:select:"))
async def acc_select(callback: CallbackQuery):
    idx  = int(callback.data.split(":")[2])
    uid  = callback.from_user.id
    accs = _accounts.get(uid, [])
    if 0 <= idx < len(accs):
        for i, a in enumerate(accs):
            a["active"] = (i == idx)
        await callback.answer(f"✅ Активен: {accs[idx]['label']}")
    await acc_list(callback)


@router.callback_query(lambda c: c.data and c.data.startswith("acc:del:"))
async def acc_del(callback: CallbackQuery):
    idx  = int(callback.data.split(":")[2])
    uid  = callback.from_user.id
    accs = _accounts.get(uid, [])
    if 0 <= idx < len(accs):
        accs.pop(idx)
        # Если удалили активный — активируем первый
        if accs and not any(a["active"] for a in accs):
            accs[0]["active"] = True
        await callback.answer("✅ Удалён")
    await acc_list(callback)
