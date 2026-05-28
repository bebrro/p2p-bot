"""
Чёрный список мерчантов.

• Личный список — скрывает объявления конкретного мерчанта для тебя
• Краудсорс — если 3+ пользователя заблокировали одного, он попадает в общий список
• Добавить через кнопку 🚫 в списке объявлений или вручную по нику

DB: при старте загружаем все ЧС в память.
    is_blacklisted() остаётся синхронной — читает из памяти (быстро, p2p.py зависит).
    Запись в DB — fire-and-forget через asyncio.create_task.
"""
import asyncio
from aiogram import Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import db

router = Router()

# {user_id: set(nicknames)}
_blacklists: dict[int, set] = {}

# {nickname: set(user_ids)} — для краудсорсинга
_community_votes: dict[str, set] = {}

COMMUNITY_THRESHOLD = 3


class BLStates(StatesGroup):
    waiting_nickname = State()


# ─── Startup loader ───────────────────────────────────────────────────────────

async def load_from_db() -> None:
    """Вызвать один раз при старте из bot.py."""
    if not db.ok():
        return
    all_bl = await db.blacklist_get_all()
    _blacklists.clear()
    _blacklists.update(all_bl)

    raw_votes = await db.community_get_all_votes_raw()
    _community_votes.clear()
    for nick, uid in raw_votes:
        _community_votes.setdefault(nick, set()).add(uid)

    import logging
    logging.getLogger(__name__).info(
        f"✅ Blacklists loaded: {len(_blacklists)} users, "
        f"{len(_community_votes)} nicks in community votes"
    )


# ─── Public API (используется из p2p.py и других модулей) ─────────────────────

def is_blacklisted(user_id: int, nickname: str) -> bool:
    """True если мерчант заблокирован лично или по краудсорсу. SYNC — читает из памяти."""
    if nickname in _blacklists.get(user_id, set()):
        return True
    votes = _community_votes.get(nickname, set())
    if len(votes) >= COMMUNITY_THRESHOLD and user_id not in votes:
        return True
    return False


def add_to_blacklist(user_id: int, nickname: str) -> bool:
    """Добавляет в чёрный список. Returns True если добавлен впервые.
    SYNC — пишет в память сразу, DB запись — fire-and-forget."""
    if user_id not in _blacklists:
        _blacklists[user_id] = set()
    if nickname in _blacklists[user_id]:
        return False
    _blacklists[user_id].add(nickname)

    # Краудсорс голос
    _community_votes.setdefault(nickname, set()).add(user_id)

    # DB: fire-and-forget
    if db.ok():
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(db.blacklist_add(user_id, nickname))
            loop.create_task(db.community_vote(user_id, nickname))
        except RuntimeError:
            pass

    return True


def remove_from_blacklist(user_id: int, nickname: str) -> None:
    """SYNC — удаляет из памяти, DB удаление — fire-and-forget."""
    _blacklists.get(user_id, set()).discard(nickname)
    _community_votes.get(nickname, set()).discard(user_id)

    if db.ok():
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(db.blacklist_remove(user_id, nickname))
        except RuntimeError:
            pass


def get_blacklist(user_id: int) -> list[str]:
    return sorted(_blacklists.get(user_id, set()))


async def community_list() -> list[tuple]:
    """[(nickname, count), ...] по убыванию. Читает из DB если доступна."""
    if db.ok():
        all_votes = await db.community_get_all()
        return [(nick, cnt) for nick, cnt in all_votes if cnt >= COMMUNITY_THRESHOLD]
    return sorted(
        [(n, len(v)) for n, v in _community_votes.items() if len(v) >= COMMUNITY_THRESHOLD],
        key=lambda x: x[1], reverse=True,
    )


# ─── UI ───────────────────────────────────────────────────────────────────────

def _bl_kb(user_id: int) -> InlineKeyboardMarkup:
    bl      = get_blacklist(user_id)
    buttons = []
    for nick in bl[:20]:
        buttons.append([InlineKeyboardButton(
            text=f"🚫 {nick[:30]}  (нажми = разблок.)",
            callback_data=f"bl:unban:{nick[:50]}",
        )])
    buttons.append([InlineKeyboardButton(text="➕ Добавить вручную", callback_data="bl:add")])
    buttons.append([InlineKeyboardButton(text="👥 Общий список",     callback_data="bl:community")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад",            callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(lambda c: c.data == "bl:list")
async def bl_list(callback: CallbackQuery):
    uid  = callback.from_user.id
    bl   = get_blacklist(uid)
    comm = await community_list()
    text = (
        "🚫 <b>Чёрный список мерчантов</b>\n\n"
        f"Заблокировано тобой: <b>{len(bl)}</b>\n"
        f"В общем списке (3+ блокировок): <b>{len(comm)}</b>\n\n"
        "Нажми на никнейм чтобы разблокировать.\n"
        "Объявления заблокированных мерчантов скрыты везде в боте."
    )
    await callback.message.edit_text(text, reply_markup=_bl_kb(uid), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "bl:add")
async def bl_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BLStates.waiting_nickname)
    await callback.message.edit_text(
        "🚫 Введи никнейм мерчанта для блокировки:\n\n"
        "(точно как написано на бирже, регистр важен)"
    )


@router.message(BLStates.waiting_nickname)
async def bl_add_nick(message: Message, state: FSMContext):
    nick  = message.text.strip()
    await state.clear()
    uid   = message.from_user.id
    added = add_to_blacklist(uid, nick)
    if added:
        await message.answer(
            f"✅ <b>{nick}</b> добавлен в чёрный список.\n"
            "Его объявления больше не будут отображаться.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚫 Мой список", callback_data="bl:list")]
            ]),
        )
    else:
        await message.answer(
            f"ℹ️ <b>{nick}</b> уже в списке.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚫 Мой список", callback_data="bl:list")]
            ]),
        )


@router.callback_query(lambda c: c.data and c.data.startswith("bl:unban:"))
async def bl_unban(callback: CallbackQuery):
    nick = callback.data.split(":", 2)[2]
    remove_from_blacklist(callback.from_user.id, nick)
    await callback.answer(f"✅ {nick} разблокирован")
    await bl_list(callback)


@router.callback_query(lambda c: c.data == "bl:community")
async def bl_community(callback: CallbackQuery):
    comm = await community_list()
    if not comm:
        text = (
            f"👥 <b>Общий чёрный список</b>\n\n"
            f"Мерчанты попадают сюда когда {COMMUNITY_THRESHOLD}+ пользователей их блокируют.\n\n"
            "Список пока пуст — недостаточно данных."
        )
    else:
        lines = [
            f"👥 <b>Общий чёрный список</b>\n"
            f"Заблокированы {COMMUNITY_THRESHOLD}+ пользователями:\n"
        ]
        for nick, count in comm[:20]:
            lines.append(f"🚫 {nick} — {count} блокировок")
        text = "\n".join(lines)

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="bl:list")]
        ]),
        parse_mode="HTML",
    )
