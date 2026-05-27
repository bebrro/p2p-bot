"""
Расписание объявлений Bybit.

Бот переводит объявление online/offline автоматически по времени.
Поддерживает переход через полночь (например, 22:00-06:00).
Уведомляет пользователя при каждом переключении.
"""
from datetime import datetime, timezone, timedelta
from aiogram import Router
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from api import bybit_auth
from handlers.account_manager import get_account_credentials

router = Router()

# {user_id: [{item_id, label, on_h, on_m, off_h, off_m, utc_offset, enabled}]}
_schedules: dict[int, list] = {}
MAX_SCHEDULES = 5

# Запоминаем последнее состояние чтобы не спамить API
_last_state: dict[str, str] = {}   # "uid:item_id" → "online" | "offline"


class SchedStates(StatesGroup):
    waiting_item_id = State()
    waiting_times   = State()   # "09:00-22:00 +5"


# ─── UI ───────────────────────────────────────────────────────────────────────

def _sch_kb(user_id: int) -> InlineKeyboardMarkup:
    schs    = _schedules.get(user_id, [])
    buttons = []
    for i, s in enumerate(schs):
        mark = "✅" if s["enabled"] else "⏸"
        buttons.append([
            InlineKeyboardButton(
                text=(
                    f"{mark} {s['label']} "
                    f"{s['on_h']:02d}:{s['on_m']:02d}–"
                    f"{s['off_h']:02d}:{s['off_m']:02d} "
                    f"UTC{s['utc_offset']:+d}"
                ),
                callback_data=f"sch:toggle:{i}",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"sch:del:{i}"),
        ])
    if len(schs) < MAX_SCHEDULES:
        buttons.append([InlineKeyboardButton(text="➕ Добавить расписание", callback_data="sch:add:start")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(lambda c: c.data == "sch:list")
async def sch_list(callback: CallbackQuery):
    uid  = callback.from_user.id
    schs = _schedules.get(uid, [])
    await callback.message.edit_text(
        "⏰ <b>Расписание объявлений</b>\n\n"
        "Бот автоматически переводит твои объявления Bybit\n"
        "в режим online/offline по заданному расписанию.\n\n"
        f"Активных: {len(schs)}/{MAX_SCHEDULES}\n"
        "Нажми на расписание чтобы вкл/выкл, 🗑 — удалить.",
        reply_markup=_sch_kb(uid),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "sch:add:start")
async def sch_add_start(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    api_key, _ = get_account_credentials(uid)
    if not api_key:
        await callback.message.edit_text(
            "❌ Сначала добавь API ключ в 🔑 Аккаунты",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Аккаунты", callback_data="acc:list")]
            ]),
        )
        return
    await state.set_state(SchedStates.waiting_item_id)
    await callback.message.edit_text(
        "⏰ <b>Расписание — шаг 1/2</b>\n\n"
        "Введи <b>ID объявления Bybit</b>\n\n"
        "Где найти: Bybit → P2P → Мои объявления → нажми на объявление",
        parse_mode="HTML",
    )


@router.message(SchedStates.waiting_item_id)
async def sch_got_item(message: Message, state: FSMContext):
    await state.update_data(item_id=message.text.strip())
    await state.set_state(SchedStates.waiting_times)
    await message.answer(
        "⏰ <b>Расписание — шаг 2/2</b>\n\n"
        "Введи время и часовой пояс одной строкой:\n"
        "<code>ЧЧ:ММ-ЧЧ:ММ UTC_смещение</code>\n\n"
        "<b>Примеры:</b>\n"
        "<code>09:00-22:00 +5</code>  — Алматы (UTC+5)\n"
        "<code>10:00-23:00 +3</code>  — Москва (UTC+3)\n"
        "<code>22:00-06:00 +5</code>  — ночная смена\n"
        "<code>08:00-20:00 0</code>   — UTC",
        parse_mode="HTML",
    )


@router.message(SchedStates.waiting_times)
async def sch_got_times(message: Message, state: FSMContext):
    try:
        parts  = message.text.strip().split()
        times  = parts[0]
        offset = int(parts[1]) if len(parts) > 1 else 0
        on_s, off_s = times.split("-")
        on_h,  on_m  = map(int, on_s.split(":"))
        off_h, off_m = map(int, off_s.split(":"))
        if not (0 <= on_h < 24 and 0 <= on_m < 60 and
                0 <= off_h < 24 and 0 <= off_m < 60 and
                -12 <= offset <= 14):
            raise ValueError("out of range")
    except Exception:
        await message.answer(
            "❌ Неверный формат. Примеры:\n"
            "<code>09:00-22:00 +5</code>\n"
            "<code>22:00-06:00 +3</code>",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    await state.clear()

    uid     = message.from_user.id
    api_key, api_secret = get_account_credentials(uid)

    # Пытаемся получить название объявления
    label = data["item_id"]
    try:
        my_ads = await bybit_auth.get_my_ads(api_key, api_secret, status=10)
        ad = next((a for a in my_ads if a["id"] == data["item_id"]), None)
        if ad:
            label = f"{ad['asset']}/{ad['fiat']}"
        else:
            await message.answer(
                "❌ Объявление не найдено. Проверь ID.\n"
                "Объявление должно быть активным."
            )
            return
    except Exception as e:
        await message.answer(f"❌ Ошибка API: <code>{e}</code>", parse_mode="HTML")
        return

    if uid not in _schedules:
        _schedules[uid] = []

    _schedules[uid].append({
        "item_id":    data["item_id"],
        "label":      label,
        "on_h":       on_h,  "on_m":  on_m,
        "off_h":      off_h, "off_m": off_m,
        "utc_offset": offset,
        "enabled":    True,
    })

    await message.answer(
        f"✅ <b>Расписание добавлено!</b>\n\n"
        f"Объявление: <b>{label}</b>\n"
        f"Online:  {on_h:02d}:{on_m:02d}\n"
        f"Offline: {off_h:02d}:{off_m:02d}\n"
        f"Часовой пояс: UTC{offset:+d}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏰ Расписания", callback_data="sch:list")]
        ]),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("sch:toggle:"))
async def sch_toggle(callback: CallbackQuery):
    idx  = int(callback.data.split(":")[2])
    uid  = callback.from_user.id
    schs = _schedules.get(uid, [])
    if 0 <= idx < len(schs):
        schs[idx]["enabled"] = not schs[idx]["enabled"]
        await callback.answer("✅ " + ("Включено" if schs[idx]["enabled"] else "Отключено"))
    await sch_list(callback)


@router.callback_query(lambda c: c.data and c.data.startswith("sch:del:"))
async def sch_del(callback: CallbackQuery):
    idx  = int(callback.data.split(":")[2])
    uid  = callback.from_user.id
    schs = _schedules.get(uid, [])
    if 0 <= idx < len(schs):
        schs.pop(idx)
        await callback.answer("✅ Удалено")
    await sch_list(callback)


# ─── Background task ───────────────────────────────────────────────────────────

async def run_schedule(bot) -> None:
    """Вызывается каждые 60 секунд из bot.py."""
    import logging
    logger = logging.getLogger(__name__)

    now_utc = datetime.now(timezone.utc)

    for user_id, schs in list(_schedules.items()):
        for sch in schs:
            if not sch["enabled"]:
                continue
            try:
                user_tz   = timezone(timedelta(hours=sch["utc_offset"]))
                now_local = now_utc.astimezone(user_tz)
                cur_min   = now_local.hour * 60 + now_local.minute
                on_min    = sch["on_h"]  * 60 + sch["on_m"]
                off_min   = sch["off_h"] * 60 + sch["off_m"]

                # Поддержка перехода через полночь (22:00-06:00)
                if on_min < off_min:
                    should_online = on_min <= cur_min < off_min
                else:
                    should_online = cur_min >= on_min or cur_min < off_min

                state_key  = f"{user_id}:{sch['item_id']}"
                new_state  = "online" if should_online else "offline"
                if _last_state.get(state_key) == new_state:
                    continue   # уже в нужном состоянии, не трогаем

                api_key, api_secret = get_account_credentials(user_id)
                if not api_key:
                    continue

                if should_online:
                    res = await bybit_auth.set_ad_online(api_key, api_secret, sch["item_id"])
                else:
                    res = await bybit_auth.set_ad_offline(api_key, api_secret, sch["item_id"])

                if res.get("retCode", -1) == 0:
                    _last_state[state_key] = new_state
                    action = "🟢 online" if should_online else "🔴 offline"
                    logger.info(f"Schedule: uid={user_id} {sch['label']} → {new_state}")
                    await bot.send_message(
                        user_id,
                        f"⏰ <b>Расписание</b>\n"
                        f"{sch['label']} переведено {action}",
                        parse_mode="HTML",
                    )
                else:
                    logger.warning(f"Schedule API err uid={user_id}: {res.get('retMsg')}")
            except Exception as e:
                logger.error(f"Schedule error uid={user_id}: {e}")
