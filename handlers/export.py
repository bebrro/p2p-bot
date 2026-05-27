"""
Экспорт P2P ордеров в CSV.

Файл открывается в Excel/Google Sheets без дополнительных настроек.
Кодировка UTF-8 с BOM — корректно отображает кириллицу в Excel.
В конце файла — итоговая сводка.
"""
import csv
import io
from datetime import datetime, timedelta
from aiogram import Router
from aiogram.types import CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from api import bybit_auth
from handlers.account_manager import get_account_credentials

router = Router()

PERIODS = {"7": "7 дней", "30": "30 дней", "90": "90 дней", "0": "Все сделки"}
STATUS_RU = {"20": "Ожидание", "30": "Выпуск крипты", "50": "Завершена", "60": "Отменена"}
SIDE_RU   = {"0": "Покупка", "1": "Продажа"}


def _export_kb(back: bool = False) -> InlineKeyboardMarkup:
    if back:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Другой период", callback_data="export:start")],
            [InlineKeyboardButton(text="⬅️ Назад",         callback_data="back:main")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="7 дней",  callback_data="export:csv:7"),
            InlineKeyboardButton(text="30 дней", callback_data="export:csv:30"),
            InlineKeyboardButton(text="90 дней", callback_data="export:csv:90"),
        ],
        [InlineKeyboardButton(text="📥 Все сделки", callback_data="export:csv:0")],
        [InlineKeyboardButton(text="⬅️ Назад",      callback_data="back:main")],
    ])


def _parse_date(created_at: str) -> datetime | None:
    if not created_at:
        return None
    try:
        # Bybit может отдавать миллисекунды или дату строкой
        if created_at.isdigit() and len(created_at) == 13:
            return datetime.fromtimestamp(int(created_at) / 1000)
        return datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            return datetime.strptime(created_at[:10], "%Y-%m-%d")
        except Exception:
            return None


def _build_csv(orders: list[dict]) -> bytes:
    output = io.StringIO()
    w = csv.writer(output, delimiter=";")

    # Заголовок
    w.writerow([
        "ID ордера", "Дата", "Тип", "Актив", "Валюта",
        "Цена", "Количество", "Сумма (фиат)", "Контрагент", "Статус",
    ])

    completed_vol = 0.0
    completed_cnt = 0

    for o in orders:
        side   = SIDE_RU.get(str(o.get("side", "")), str(o.get("side", "")))
        status = STATUS_RU.get(str(o.get("status", "")), str(o.get("status", "")))
        dt     = _parse_date(o.get("created_at", ""))
        date_s = dt.strftime("%Y-%m-%d %H:%M") if dt else ""

        amount = float(o.get("amount", 0))
        if str(o.get("status", "")) == "50":
            completed_vol += amount
            completed_cnt += 1

        w.writerow([
            o.get("order_id", ""),
            date_s,
            side,
            o.get("asset", ""),
            o.get("fiat", ""),
            f"{float(o.get('price', 0)):.2f}",
            f"{float(o.get('quantity', 0)):.4f}",
            f"{amount:.2f}",
            o.get("nickname", ""),
            status,
        ])

    # Пустая строка + итоги
    w.writerow([])
    w.writerow(["=== ИТОГИ ==="])
    w.writerow(["Всего ордеров",   len(orders)])
    w.writerow(["Завершено",       completed_cnt])
    w.writerow(["Оборот (фиат)",   f"{completed_vol:.2f}"])
    avg = completed_vol / completed_cnt if completed_cnt else 0
    w.writerow(["Средний ордер",   f"{avg:.2f}"])
    w.writerow(["Дата выгрузки",   datetime.now().strftime("%Y-%m-%d %H:%M")])

    # UTF-8 BOM — Excel открывает без танцев
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


@router.callback_query(lambda c: c.data == "export:start")
async def export_start(callback: CallbackQuery):
    uid = callback.from_user.id
    api_key, _ = get_account_credentials(uid)
    if not api_key:
        await callback.message.edit_text(
            "📥 <b>Экспорт сделок</b>\n\n"
            "Добавь API ключ Bybit чтобы получить доступ к своим ордерам.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Аккаунты", callback_data="acc:list")],
                [InlineKeyboardButton(text="⬅️ Назад",    callback_data="back:main")],
            ]),
            parse_mode="HTML",
        )
        return
    await callback.message.edit_text(
        "📥 <b>Экспорт сделок P2P</b>\n\n"
        "Выбери период. Файл придёт в формате <b>CSV</b> — открывается\n"
        "в Excel, Google Sheets, LibreOffice без дополнительных настроек.",
        reply_markup=_export_kb(),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("export:csv:"))
async def export_csv(callback: CallbackQuery):
    period  = callback.data.split(":")[2]
    uid     = callback.from_user.id
    api_key, api_secret = get_account_credentials(uid)
    if not api_key:
        await callback.answer("❌ Нет активного API ключа", show_alert=True)
        return

    await callback.message.edit_text("⏳ Загружаю ордера...")

    try:
        orders = await bybit_auth.get_p2p_orders(api_key, api_secret, status="10")
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка API: <code>{e}</code>",
            reply_markup=_export_kb(back=True),
            parse_mode="HTML",
        )
        return

    # Фильтр по периоду
    if period != "0":
        cutoff  = datetime.now() - timedelta(days=int(period))
        filtered = []
        for o in orders:
            dt = _parse_date(o.get("created_at", ""))
            if dt is None or dt >= cutoff:
                filtered.append(o)
        orders = filtered

    if not orders:
        await callback.message.edit_text(
            f"😔 Нет сделок за выбранный период ({PERIODS[period]}).",
            reply_markup=_export_kb(back=True),
        )
        return

    # Генерируем CSV
    csv_bytes = _build_csv(orders)
    completed = [o for o in orders if str(o.get("status", "")) == "50"]
    total_vol = sum(float(o.get("amount", 0)) for o in completed)

    filename = f"p2p_{PERIODS[period].replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.csv"

    await callback.message.answer_document(
        document=BufferedInputFile(csv_bytes, filename=filename),
        caption=(
            f"📥 P2P ордера — {PERIODS[period]}\n"
            f"Всего: {len(orders)} | Завершено: {len(completed)}\n"
            f"Оборот: {total_vol:,.2f}"
        ),
    )
    await callback.message.edit_text("✅ Файл отправлен выше.", reply_markup=_export_kb(back=True))
