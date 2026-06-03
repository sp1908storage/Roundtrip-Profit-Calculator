from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .ai_parser import parse_with_ai_if_configured
from .calculator import calculate_round_trip
from .dialogue import FIELD_LABELS, format_result
from .models import Direction, Flight
from .settings import get_settings
from .sheets import append_result, is_configured as sheets_is_configured
from .validators import missing_fields, validate_flight


START_TEXT = (
    "Пришлите описание прямого рейса или круго-рейса одним сообщением. "
    "Я извлеку данные, проверю заявку и посчитаю себестоимость."
)


def run_telegram_bot() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.run_polling()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await _is_allowed(update):
        return
    await update.effective_message.reply_text(START_TEXT)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await _is_allowed(update):
        return

    text = update.effective_message.text or ""
    round_trip = parse_with_ai_if_configured(text)
    if not round_trip.forward_flights:
        round_trip.forward_flights.append(Flight(direction=Direction.FORWARD))

    issues = []
    for index, flight in enumerate(round_trip.flights, 1):
        for field in missing_fields(flight):
            issues.append(f"Рейс {index}: не заполнено поле {FIELD_LABELS.get(field, field)}")
        for issue in validate_flight(flight):
            if issue.fatal:
                issues.append(f"Рейс {index}: {FIELD_LABELS.get(issue.field, issue.field)} - {issue.message}")

    if issues:
        await update.effective_message.reply_text(
            "Не хватает данных для расчета:\n" + "\n".join(f"- {item}" for item in issues)
        )
        return

    try:
        result = calculate_round_trip(round_trip)
    except ValueError as exc:
        await update.effective_message.reply_text(f"Не удалось рассчитать: {exc}")
        return

    if sheets_is_configured():
        append_result(round_trip, result)

    await update.effective_message.reply_text(
        escape(format_result(result)),
        parse_mode=ParseMode.HTML,
    )


async def _is_allowed(update: Update) -> bool:
    settings = get_settings()
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not settings.telegram_allowed_chat_ids or chat_id in settings.telegram_allowed_chat_ids:
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Доступ к боту не разрешен для этого чата.")
    return False

