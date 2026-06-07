import asyncio
import logging
import re
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .ai_parser import (
    answer_dialog_with_ai_if_configured,
    parse_data_with_ai_if_configured,
    parse_image_request_with_ai_if_configured,
    parse_with_ai_if_configured,
)
from .calculator import calculate_round_trip
from .config import DEFAULT_COST_CONFIG
from .dialogue import format_result, money
from .models import RoundTrip, TransportStatus
from .settings import get_settings
from .sheets import (
    append_request_log,
    append_result,
    is_configured as sheets_is_configured,
    upload_request_image_to_drive,
)
from .telegram_dialog import TelegramDialogSession


LOGGER = logging.getLogger(__name__)
SEND_RETRY_DELAYS_SECONDS = (1.0, 3.0, 6.0)

START_TEXT = (
    "Пришлите описание прямого рейса или круго-рейса одним сообщением. "
    "Можно отправить текст или скриншот заявки. "
    "Я извлеку данные, проверю заявку и посчитаю себестоимость."
)

SESSIONS: dict[int, TelegramDialogSession] = {}
LAST_RESULTS: dict[int, tuple[TelegramDialogSession, object]] = {}

SHEETS_PUBLIC_ERROR = (
    "Запись в Google Sheets не удалась. "
    "Проверьте переменные Google credentials и доступ сервисного аккаунта к таблице."
)


def run_telegram_bot() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")

    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=30.0,
        media_write_timeout=60.0,
    )
    get_updates_request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=90.0,
        write_timeout=60.0,
        pool_timeout=30.0,
        media_write_timeout=60.0,
    )
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("new", new_request))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.run_polling(timeout=50)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await _is_allowed(update):
        return
    await _reply_text(
        update,
        START_TEXT + "\n\n/new - начать новый расчет\n/cancel - отменить текущий расчет"
    )


async def new_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await _is_allowed(update):
        return
    _drop_session(update)
    await _reply_text(update, "Готов к новой заявке. Пришлите текст или скриншот.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await _is_allowed(update):
        return
    _drop_session(update)
    await _reply_text(update, "Текущий расчет отменен. Можно прислать новую заявку.")


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

    normalized = _normalize_text(text)
    if chat_id in LAST_RESULTS and not _looks_like_new_freight_request(normalized):
        ai_answer = _answer_result_dialog_with_ai(text, LAST_RESULTS[chat_id])
        if ai_answer:
            await _reply_text(update, ai_answer)
            return

    if chat_id in LAST_RESULTS and _looks_like_result_followup(text):
        await _reply_text(update, _answer_result_followup(text, LAST_RESULTS[chat_id]))
        return

    if _looks_like_casual_close(text):
        await _reply_text(update, _answer_casual_close(chat_id in LAST_RESULTS))
        return

    if _looks_like_orphaned_dialog_answer(text):
        await _reply_text(
            update,
            "Похоже, предыдущий диалог был прерван, например из-за перезапуска бота. "
            "Я не буду начинать новый расчет по одному ответу, чтобы не гонять вас по второму кругу. "
            "Пришлите исходную заявку заново одним сообщением или отправьте /new."
        )
        return

    try:
        round_trip = parse_with_ai_if_configured(text)
    except Exception:
        round_trip = None
    if round_trip is None:
        await _reply_text(
            update,
            "AI-разбор временно недоступен. Не страшно, соберу данные по шагам."
        )
        round_trip = RoundTrip()
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
        round_trip, recognized_text = parse_image_request_with_ai_if_configured(
            image_bytes,
            mime_type="image/jpeg",
        )
    except Exception:
        await _reply_text(
            update,
            "Не удалось обработать изображение через ИИ. Проверьте настройки модели и пришлите текст заявки, если нужно продолжить без скриншота."
        )
        return

    caption = update.effective_message.caption or ""
    source_text = _combine_image_source_text(recognized_text, caption)
    session = TelegramDialogSession(
        round_trip=round_trip,
        source_text=source_text,
        message_type="photo",
        image_file_id=photo.file_id,
    )
    session.image_cell_value = _upload_image_cell_value(session, image_bytes, "image/jpeg")
    SESSIONS[chat_id] = session
    await _safe_write_request_log(update, session, "диалог идет", "")
    await _send_messages(update, session.start())
    await _finish_if_ready(update, session)


async def _continue_session(update: Update, session: TelegramDialogSession, text: str) -> None:
    session.remember_user_answer(text)
    if session.should_handle_current_answer_directly(text):
        messages = session.handle_answer(text, remember=False)
        await _send_messages(update, messages)
        if not session.is_ready:
            await _safe_write_request_log(update, session, "диалог идет", "")
        await _finish_if_ready(update, session)
        return

    ai_updated = False
    if session.stage not in {"another_forward", "has_backhaul", "another_backhaul"}:
        ai_updated = _try_update_session_from_ai(session)
    if ai_updated and (session.current_prompt_is_satisfied() or _looks_like_rate_correction(text)):
        messages = session.continue_after_ai_update()
    else:
        messages = session.handle_answer(text, remember=False)
    await _send_messages(update, messages)
    if not session.is_ready:
        await _safe_write_request_log(update, session, "диалог идет", "")
    await _finish_if_ready(update, session)


async def _finish_if_ready(update: Update, session: TelegramDialogSession) -> None:
    if not session.is_ready:
        return

    try:
        result = calculate_round_trip(session.round_trip)
    except ValueError as exc:
        await _reply_text(update, f"Не удалось рассчитать: {exc}")
        await _safe_write_request_log(update, session, "ошибка", str(exc))
        _drop_session(update)
        return

    sheets_error = None
    missing_rate_text = ""
    if _has_missing_rate(session):
        missing_rate_text = (
            "Ставка не указана по одному или нескольким рейсам. "
            "Считаю выручку по ним как 0 руб., поэтому расчетная прибыль будет со знаком минус."
        )
    result_text = format_result(result)
    saved_response_text = f"{missing_rate_text}\n\n{result_text}" if missing_rate_text else result_text

    if sheets_is_configured():
        try:
            append_result(
                session.round_trip,
                result,
                response_text=saved_response_text,
                request_id=session.request_id,
                request_source="Telegram",
                request_user=_user_label(update),
            )
        except Exception:
            sheets_error = "ошибка записи результата в Google Sheets"
        await _safe_write_request_log(
            update,
            session,
            "расчет выполнен",
            sheets_error or "",
        )

    if missing_rate_text:
        await _reply_text(update, missing_rate_text)
    await _reply_text(
        update,
        escape(result_text),
        parse_mode=ParseMode.HTML,
    )
    if sheets_error:
        await _reply_text(update, f"Расчет готов, но {SHEETS_PUBLIC_ERROR}")
    chat_id = _chat_id(update)
    if chat_id is not None:
        LAST_RESULTS[chat_id] = (session, result)
    _drop_session(update)


async def _send_messages(update: Update, messages: list[str]) -> None:
    for message in messages:
        if message:
            await _reply_text(update, message)


async def _reply_text(update: Update, text: str, **kwargs) -> bool:
    message = update.effective_message
    if message is None:
        return False

    attempts = len(SEND_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(attempts):
        try:
            await message.reply_text(text, **kwargs)
            return True
        except (TimedOut, NetworkError) as exc:
            if attempt + 1 >= attempts:
                LOGGER.warning(
                    "Telegram send failed after %s attempts: %s",
                    attempts,
                    exc.__class__.__name__,
                )
                return False
            delay = SEND_RETRY_DELAYS_SECONDS[attempt]
            LOGGER.warning(
                "Telegram send timed out, retrying in %.1fs (%s/%s)",
                delay,
                attempt + 1,
                attempts,
            )
            await asyncio.sleep(delay)
    return False


def _chat_id(update: Update) -> int | None:
    return update.effective_chat.id if update.effective_chat else None


def _drop_session(update: Update) -> None:
    chat_id = _chat_id(update)
    if chat_id is not None:
        SESSIONS.pop(chat_id, None)


def _has_missing_rate(session: TelegramDialogSession) -> bool:
    return any((flight.rate_with_vat_rub or 0) == 0 for flight in session.round_trip.flights)


def _looks_like_result_followup(text: str) -> bool:
    normalized = _normalize_text(text)
    if "?" in normalized:
        return True
    return any(
        token in normalized
        for token in (
            "как считал",
            "как посчитал",
            "почему",
            "откуда",
            "платные дороги",
            "топливо",
            "обслуживание",
            "расход",
            "себестоимость",
            "прибыль",
        )
    )


def _looks_like_rate_correction(text: str) -> bool:
    normalized = _normalize_text(text)
    return "ставк" in normalized and bool(re.search(r"\d", normalized))


def _answer_result_followup(text: str, last_result: tuple[TelegramDialogSession, object]) -> str:
    del text
    _session, result = last_result
    config = DEFAULT_COST_CONFIG
    lines = [
        "Платные дороги сейчас считаются по простой формуле:",
        f"платные дороги = километры по РФ x {config.tolls_rub_per_km:g} руб./км.",
        "",
    ]
    total_tolls = 0.0
    for index, item in enumerate(result.flights, 1):
        flight = item.flight
        distance_to_loading = flight.distance_to_loading_km or 0
        distance_to_unloading = flight.distance_to_unloading_km or 0
        total_km = distance_to_loading + distance_to_unloading
        foreign_km = 0.0
        if flight.status == TransportStatus.INTERNATIONAL:
            russian_km = flight.russian_territory_km or 0
            foreign_km = max(distance_to_unloading - russian_km, 0)
        domestic_km = total_km - foreign_km
        total_tolls += item.tolls_rub
        lines.append(
            f"Рейс {index}: {domestic_km:g} км по РФ x {config.tolls_rub_per_km:g} = {money(item.tolls_rub)}"
        )
    lines.extend(
        [
            "",
            f"Итого платные дороги: {money(total_tolls)}",
            "Это пока демо-формула. Позже заменим её на реальные правила/тарифы по маршруту.",
        ]
    )
    return "\n".join(lines)


def _answer_result_dialog_with_ai(
    text: str,
    last_result: tuple[TelegramDialogSession, object],
) -> str | None:
    try:
        return answer_dialog_with_ai_if_configured(text, _result_dialog_context(last_result))
    except Exception as exc:
        LOGGER.warning("AI dialog answer failed: %s", exc.__class__.__name__)
        return None


def _result_dialog_context(last_result: tuple[TelegramDialogSession, object]) -> str:
    session, result = last_result
    parts = []
    if session.source_text:
        parts.append(f"Исходная заявка:\n{session.source_text}")
    if session.dialog_history:
        parts.append("Уточнения в диалоге:\n" + "\n\n".join(session.dialog_history[-8:]))
    parts.append("Последний расчет:\n" + format_result(result))
    return "\n\n".join(parts)


def _looks_like_casual_close(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    casual_tokens = (
        "спасибо",
        "благодарю",
        "супер",
        "отлично",
        "класс",
        "здорово",
        "хорошо",
        "понял",
        "поняла",
        "понятно",
        "ясно",
        "принял",
        "приняла",
        "принято",
        "ок",
        "окей",
        "спс",
    )
    if not any(token in normalized for token in casual_tokens):
        return False
    return not _looks_like_new_freight_request(normalized) and not _looks_like_result_followup(text)


def _answer_casual_close(has_last_result: bool) -> str:
    if has_last_result:
        return "Пожалуйста. Если понадобится еще расчет, пришлите новую заявку одним сообщением."
    return "Пожалуйста. Когда будет заявка, присылайте ее одним сообщением."


def _try_update_session_from_ai(session: TelegramDialogSession) -> bool:
    context = session.ai_context()
    if not context:
        return False
    try:
        data = parse_data_with_ai_if_configured(context)
    except Exception:
        return False
    return session.apply_ai_data(data, context)


def _looks_like_orphaned_dialog_answer(text: str) -> bool:
    normalized = _normalize_text(text)
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


def _normalize_text(text: str) -> str:
    return text.strip().lower().replace("ё", "е")


def _combine_image_source_text(recognized_text: str, caption: str) -> str:
    parts = []
    if recognized_text.strip():
        parts.append(recognized_text.strip())
    if caption.strip() and caption.strip() not in parts:
        parts.append(caption.strip())
    return "\n\n".join(parts)


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
        await _reply_text(update, SHEETS_PUBLIC_ERROR)


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
        await _reply_text(update, "Доступ к боту не разрешен для этого чата.")
    return False
