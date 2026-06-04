from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .ai_parser import parse_image_with_ai_if_configured, parse_with_ai_if_configured
from .calculator import calculate_round_trip
from .dialogue import format_result
from .settings import get_settings
from .sheets import append_result, is_configured as sheets_is_configured
from .telegram_dialog import TelegramDialogSession


START_TEXT = (
    "Пришлите описание прямого рейса или круго-рейса одним сообщением. "
    "Можно отправить текст или скриншот заявки. "
    "Я извлеку данные, проверю заявку и посчитаю себестоимость."
)

SESSIONS: dict[int, TelegramDialogSession] = {}


def run_telegram_bot() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("new", new_request))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.run_polling()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await _is_allowed(update):
        return
    await update.effective_message.reply_text(
        START_TEXT + "\n\n/new - начать новый расчет\n/cancel - отменить текущий расчет"
    )


async def new_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await _is_allowed(update):
        return
    _drop_session(update)
    await update.effective_message.reply_text("Готов к новой заявке. Пришлите текст или скриншот.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await _is_allowed(update):
        return
    _drop_session(update)
    await update.effective_message.reply_text("Текущий расчет отменен. Можно прислать новую заявку.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await _is_allowed(update):
        return

    chat_id = _chat_id(update)
    if chat_id is None:
        return

    text = update.effective_message.text or ""
    if chat_id in SESSIONS:
        await _continue_session(update, SESSIONS[chat_id], text)
        return

    round_trip = parse_with_ai_if_configured(text)
    session = TelegramDialogSession(round_trip=round_trip, source_text=text, message_type="text")
    SESSIONS[chat_id] = session
    await _send_messages(update, session.start())
    await _finish_if_ready(update, session)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await _is_allowed(update):
        return

    if not update.effective_message or not update.effective_message.photo:
        return

    chat_id = _chat_id(update)
    if chat_id is None:
        return

    try:
        photo = update.effective_message.photo[-1]
        telegram_file = await photo.get_file()
        image_bytes = bytes(await telegram_file.download_as_bytearray())
        round_trip = parse_image_with_ai_if_configured(image_bytes, mime_type="image/jpeg")
    except Exception as exc:
        await update.effective_message.reply_text(
            f"Не удалось обработать изображение через ИИ: {exc}"
        )
        return

    session = TelegramDialogSession(
        round_trip=round_trip,
        source_text=update.effective_message.caption or "",
        message_type="photo",
    )
    SESSIONS[chat_id] = session
    await _send_messages(update, session.start())
    await _finish_if_ready(update, session)


async def _continue_session(update: Update, session: TelegramDialogSession, text: str) -> None:
    await _send_messages(update, session.handle_answer(text))
    await _finish_if_ready(update, session)


async def _finish_if_ready(update: Update, session: TelegramDialogSession) -> None:
    if not session.is_ready:
        return

    try:
        result = calculate_round_trip(session.round_trip)
    except ValueError as exc:
        await update.effective_message.reply_text(f"Не удалось рассчитать: {exc}")
        _drop_session(update)
        return

    if sheets_is_configured():
        append_result(session.round_trip, result)

    await update.effective_message.reply_text(
        escape(format_result(result)),
        parse_mode=ParseMode.HTML,
    )
    _drop_session(update)


async def _send_messages(update: Update, messages: list[str]) -> None:
    for message in messages:
        if message:
            await update.effective_message.reply_text(message)


def _chat_id(update: Update) -> int | None:
    return update.effective_chat.id if update.effective_chat else None


def _drop_session(update: Update) -> None:
    chat_id = _chat_id(update)
    if chat_id is not None:
        SESSIONS.pop(chat_id, None)


async def _is_allowed(update: Update) -> bool:
    settings = get_settings()
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not settings.telegram_allowed_chat_ids or chat_id in settings.telegram_allowed_chat_ids:
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Доступ к боту не разрешен для этого чата.")
    return False
