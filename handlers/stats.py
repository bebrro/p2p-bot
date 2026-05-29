"""
Статистика P2P аккаунта Bybit.
Требует активного API ключа (достаточно прав только на чтение P2P).
"""
import asyncio
from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from api import bybit_auth
from handlers.account_manager import get_account_credentials

router = Router()

STATUS_LABELS = {
    "10": "все",
    "20": "ожидающие",
    "30": "выпуск",
    "50": "завершённые",
    "60": "отменённые",
}


# ─── Handlers ──────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "stats:view")
async def stats_view(callback: CallbackQuery):
    uid = callback.from_user.id
    api_key, api_secret, _ = get_account_credentials(uid)

    if not api_key:
        await callback.message.edit_text(
            "📊 <b>Статистика P2P</b>\n\n"
            "Для просмотра статистики добавь API ключ Bybit.\n\n"
            "Нужные права: P2P (чтение), <b>БЕЗ вывода средств</b>.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Добавить ключ", callback_data="acc:list")],
                [InlineKeyboardButton(text="⬅️ Назад",         callback_data="back:main")],
            ]),
            parse_mode="HTML",
        )
        return

    await callback.message.edit_text("⏳ Загружаю статистику...")

    try:
        profile, orders = await asyncio.gather(
            bybit_auth.get_p2p_stats(api_key, api_secret),
            bybit_auth.get_p2p_orders(api_key, api_secret, status="10"),
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка API: <code>{e}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")]
            ]),
            parse_mode="HTML",
        )
        return

    # Считаем оборот по завершённым ордерам
    completed  = [o for o in orders if o["status"] in ("50", "COMPLETED")]
    total_vol  = sum(o["amount"] for o in completed)
    avg_order  = total_vol / len(completed) if completed else 0
    completion = f"{profile['completion']:.1f}%" if profile["completion"] else "—"

    # Рейтинг из отзывов
    good = profile["good_reviews"]
    bad  = profile["bad_reviews"]
    total_rev = good + bad
    rating_s  = f"{good}/{total_rev} ({good/total_rev*100:.0f}%)" if total_rev else "—"

    lines = [
        "📊 <b>Статистика P2P (Bybit)</b>\n",
        f"👤 Никнейм: <b>{profile['nickname']}</b>",
        f"📦 Сделок за 30 дней: <b>{profile['orders_30d']}</b>",
        f"✅ Процент успешных: <b>{completion}</b>",
        f"⏱ Среднее время выпуска: <b>{profile['avg_release']}</b>",
        f"📊 Всего сделок: <b>{profile['total_orders']}</b>",
        f"⭐ Отзывы: <b>{rating_s}</b>",
    ]

    if completed:
        lines += [
            "",
            "💰 <b>Последние завершённые</b>",
            f"  Оборот: <b>{total_vol:,.2f}</b>",
            f"  Средний ордер: <b>{avg_order:,.2f}</b>",
            "",
        ]
        for o in completed[:5]:
            side_em = "📗" if o["side"] == "0" else "📕"
            date    = o["created_at"][:10] if o.get("created_at") else "—"
            lines.append(
                f"  {side_em} {o['asset']}/{o['fiat']} — "
                f"{o['amount']:,.2f} @ {o['price']:,.2f} ({date})"
            )

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Завершённые", callback_data="stats:orders:50"),
                InlineKeyboardButton(text="⏳ Ожидают",     callback_data="stats:orders:20"),
            ],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="stats:view")],
            [InlineKeyboardButton(text="⬅️ Назад",    callback_data="back:main")],
        ]),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("stats:orders:"))
async def stats_orders(callback: CallbackQuery):
    status  = callback.data.split(":")[2]
    uid     = callback.from_user.id
    api_key, api_secret, _ = get_account_credentials(uid)
    if not api_key:
        await callback.answer("❌ Нет активного API ключа", show_alert=True)
        return

    await callback.message.edit_text("⏳ Загружаю ордера...")
    try:
        orders = await bybit_auth.get_p2p_orders(api_key, api_secret, status=status)
    except Exception as e:
        await callback.answer(f"❌ {e}", show_alert=True)
        return

    label = STATUS_LABELS.get(status, status)
    lines = [f"📋 <b>Ордера — {label}</b>\n"]

    if not orders:
        lines.append("Нет ордеров.")
    else:
        for o in orders[:20]:
            side_em = "📗" if o["side"] == "0" else "📕"
            date    = o["created_at"][:10] if o.get("created_at") else "—"
            lines += [
                f"{side_em} <b>{o['asset']}/{o['fiat']}</b> — {o['amount']:,.2f}",
                f"   Цена: {o['price']:,.2f} | Контрагент: {o['nickname']} | {date}",
                "",
            ]

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Завершённые", callback_data="stats:orders:50"),
                InlineKeyboardButton(text="⏳ Ожидают",     callback_data="stats:orders:20"),
                InlineKeyboardButton(text="❌ Отменённые",  callback_data="stats:orders:60"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="stats:view")],
        ]),
        parse_mode="HTML",
    )
