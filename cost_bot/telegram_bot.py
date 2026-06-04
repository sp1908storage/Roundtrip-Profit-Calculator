import re
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .ai_parser import parse_image_with_ai_if_configured, parse_with_ai_if_configured
from .calculator import calculate_round_trip
from .dialogue import format_result
from .settings import get_settings
from .sheets import (
    append_request_log,
    append_result,
    is_configured as sheets_is_configured,
    upload_request_image_to_drive,
)
from .telegram_dialog import TelegramDialogSession


START_TEXT = (
    "Пришлите описание прямого рейса или круго-рейса одним сообщением. "
    "Можно отправить текст или скриншот заявки. "
    "Я извлеку данные, проверю заявку и посчитаю себестоимость."
)

SESSIONS: dict[int, TelegramDialogSession] = {}

SHEETS_PUBLIC_ERROR = (
    "Запись в Google Sheets не удалась. "
    "Проверьте переменные Google credentials и доступ сервисного аккаунта к таблице."
)


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

    if _looks_like_orphaned_dialog_answer(text):
        await update.effective_message.reply_text(
            "Похоже, предыдущий диалог был прерван, например из-за перезапуска бота. "
            "Я не буду начинать новый расчет по одному ответу, чтобы не гонять вас по второму кругу. "
            "Пришлите исходную заявку заново одним сообщением или отправьте /new."
        )
        return

    round_trip = parse_with_ai_if_configured(text)
    session = TelegramDialogSession(round_trip=round_trip, source_text=text, message_type="text")
    SESSIONS[chat_id] = session
    await _safe_write_request_log(update, session, "диалог идет", "")
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
    except Exception:
        await update.effective_message.reply_text(
            "Не удалось обработать изображение через ИИ. Проверьте настройки модели и пришлите текст заявки, если нужно продолжить без скриншота."
        )
        return

    session = TelegramDialogSession(
        round_trip=round_trip,
        source_text=update.effective_message.caption or "",
        message_type="photo",
        image_file_id=photo.file_id,
    )
    session.image_cell_value = _upload_image_cell_value(session, image_bytes, "image/jpeg")
    SESSIONS[chat_id] = session
    await _safe_write_request_log(update, session, "диалог идет", "")
    await _send_messages(update, session.start())
    await _finish_if_ready(update, session)


async def _continue_session(update: Update, session: TelegramDialogSession, text: str) -> None:
    await _send_messages(update, session.handle_answer(text))
    if not session.is_ready:
        await _safe_write_request_log(update, session, "диалог идет", "")
    await _finish_if_ready(update, session)


async def _finish_if_ready(update: Update, session: TelegramDialogSession) -> None:
    if not session.is_ready:
        return

    try:
        result = calculate_round_trip(session.round_trip)
    except ValueError as exc:
        await update.effective_message.reply_text(f"Не удалось рассчитать: {exc}")
        await _safe_write_request_log(update, session, "ошибка", str(exc))
        _drop_session(update)
        return

    sheets_error = None
    if sheets_is_configured():
        try:
            append_result(session.round_trip, result)
        except Exception:
            sheets_error = "ошибка записи результата в Google Sheets"
        await _safe_write_request_log(
            update,
            session,
            "расчет выполнен",
            sheets_error or "",
        )

    if _has_missing_rate(session):
        await update.effective_message.reply_text(
            "Ставка не указана по одному или нескольким рейсам. "
            "Считаю выручку по ним как 0 руб., поэтому расчетная прибыль будет со знаком минус."
        )
    await update.effective_message.reply_text(
        escape(format_result(result)),
        parse_mode=ParseMode.HTML,
    )
    if sheets_error:
        await update.effective_message.reply_text(f"Расчет готов, но {SHEETS_PUBLIC_ERROR}")
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


def _has_missing_rate(session: TelegramDialogSession) -> bool:
    return any((flight.rate_with_vat_rub or 0) == 0 for flight in session.round_trip.flights)


def _looks_like_orphaned_dialog_answer(text: str) -> bool:
    normalized = text.strip().lower().replace("ё", "е")
    if not normalized:
        return True
    if normalized in {
        "да",
        "нет",
        "не знаю",
        "пропустить",
        "уже сообщили",
        "уже сообщил",
        "уже сделали",
        "я же уже сообщил",
        "я уже сообщил",
    }:
        return True
    if re.search(r"\b(уже|сообщил|сообщала|сообщали|сделали|писал|писала)\b", normalized):
        return True
    if _looks_like_new_freight_request(normalized):
        return False
    words = re.findall(r"[a-zа-я0-9]+", normalized)
    return len(words) <= 3


def _looks_like_new_freight_request(normalized: str) -> bool:
    if re.search(r"\S+\s*(?:-|—|->|→)\s*\S+", normalized):
        return True
    freight_words = (
        "рейс",
        "маршрут",
        "заявк",
        "расчет",
        "рассчитать",
        "посчитать",
        "ставк",
        "ндс",
        "загруз",
        "выгруз",
        "доставка",
        "перевоз",
        "кругорейс",
        "круго-рейс",
    )
    if any(word in normalized for word in freight_words):
        return True
    has_rate = bool(re.search(r"\d[\d\s]*(?:руб|₽|usd|eur|cny|юан|евро|долл)", normalized))
    has_route_hint = len(re.findall(r"[a-zа-я]{3,}", normalized)) >= 2
    return has_rate and has_route_hint


async def _write_request_log(
    update: Update,
    session: TelegramDialogSession,
    calculation_status: str,
    error_comment: str,
) -> None:
    if not sheets_is_configured():
        return
    settings = get_settings()
    ai_model = (
        (settings.openai_vision_model or settings.openai_model)
        if session.message_type == "photo"
        else settings.openai_model
    )
    append_request_log(
        request_id=session.request_id,
        source="Telegram",
        user=_user_label(update),
        message_type=session.message_type,
        raw_text=session.source_text,
        image_file_id=session.image_file_id,
        image_cell_value=session.image_cell_value,
        ai_model=ai_model,
        ai_status="разобрано",
        calculation_status=calculation_status,
        error_comment=error_comment,
        round_trip=session.round_trip,
    )


def _upload_image_cell_value(
    session: TelegramDialogSession,
    image_bytes: bytes,
    mime_type: str,
) -> str | None:
    if not sheets_is_configured():
        return None
    settings = get_settings()
    if not settings.google_drive_images_folder_id:
        return "Картинка не загружена: не задан GOOGLE_DRIVE_IMAGES_FOLDER_ID"
    try:
        return upload_request_image_to_drive(
            image_bytes=image_bytes,
            mime_type=mime_type,
            request_id=session.request_id,
        )
    except Exception as exc:
        return (
            "Картинка не загружена в Drive: "
            f"{_safe_error_summary(exc)}. "
            "Проверьте доступ сервисного аккаунта к папке и Drive API."
        )


async def _safe_write_request_log(
    update: Update,
    session: TelegramDialogSession,
    calculation_status: str,
    error_comment: str,
) -> None:
    try:
        await _write_request_log(update, session, calculation_status, error_comment)
    except Exception:
        await update.effective_message.reply_text(SHEETS_PUBLIC_ERROR)


def _safe_error_summary(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    if "storageQuotaExceeded" in text or "Service Accounts do not have storage quota" in text:
        return (
            "у сервисного аккаунта нет Drive-хранилища; "
            "для загрузки картинок нужен Shared Drive или OAuth-доступ пользователя"
        )
    if "accessNotConfigured" in text or "Drive API" in text and "disabled" in text:
        return "не включен Google Drive API для проекта сервисного аккаунта"
    if "File not found" in text or "notFound" in text:
        return "папка Drive не найдена или не доступна сервисному аккаунту"
    text = re.sub(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", "[hidden-key]", text)
    text = re.sub(r"\{.{200,}\}", "[hidden-json]", text)
    if len(text) > 220:
        text = text[:217] + "..."
    return text or exc.__class__.__name__


def _user_label(update: Update) -> str:
    user = update.effective_user
    if not user:
        return ""
    parts = []
    if user.full_name:
        parts.append(user.full_name)
    if user.username:
        parts.append(f"@{user.username}")
    parts.append(f"id:{user.id}")
    return " ".join(parts)


async def _is_allowed(update: Update) -> bool:
    settings = get_settings()
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not settings.telegram_allowed_chat_ids or chat_id in settings.telegram_allowed_chat_ids:
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Доступ к боту не разрешен для этого чата.")
    return False
