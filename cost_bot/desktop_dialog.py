from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Thread
from typing import Callable
from uuid import uuid4

from .calculator import RoundTripCost, calculate_round_trip
from .config import COUNTRY_OPTIONS, COUNTRY_RUSSIA, DEFAULT_WEIGHT_KG
from .dialogue import format_result
from .models import Direction, Flight, LoadingType, RoundTrip, TransportStatus
from .sheets import append_request_log, append_result, is_configured as sheets_is_configured
from .validators import validate_flight


@dataclass(frozen=True)
class Prompt:
    field: str
    label: str
    parser: Callable[[str], object]
    optional: bool = False
    default: object | None = None
    choices: list[str] | None = None


class DesktopDialogSession:
    def __init__(self) -> None:
        self.round_trip = RoundTrip(forward_flights=[Flight(direction=Direction.FORWARD)])
        self.request_id = f"desk-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid4().hex[:8]}"
        self.raw_entries: list[str] = []
        self.current_direction = Direction.FORWARD
        self.current_index = 0
        self.current_prompt: Prompt | None = None
        self.stage = "collect"
        self.result: RoundTripCost | None = None
        self.answered_fields: set[tuple[Direction, int, str]] = set()
        self._log_thread: Thread | None = None

    def start(self) -> str:
        self.current_prompt = self._next_field_prompt()
        self._safe_log_request("диалог идет")
        return (
            "Начинаем расчет круго-рейса.\n"
            "Отвечайте на вопросы по очереди. Для значений по умолчанию можно оставить поле пустым.\n\n"
            + self._render_prompt(self.current_prompt)
        )

    def handle(self, text: str) -> list[str]:
        text = text.strip()
        self.raw_entries.append(text or "(пусто)")
        if self.stage == "done":
            return ["Расчет завершен. Нажмите \"Новый расчет\", чтобы начать заново."]
        if self.stage == "save":
            return self._handle_save_answer(text)
        if self.current_prompt is None:
            self.current_prompt = self._next_field_prompt()
            return [self._render_prompt(self.current_prompt)]

        try:
            value = self._parse_current_prompt(text)
        except ValueError as exc:
            return [f"Ошибка: {exc}", self._render_prompt(self.current_prompt)]

        if self.stage in {"another_forward", "has_backhaul", "another_backhaul"}:
            return self.handle_control_prompt(bool(value))

        setattr(self._current_flight(), self.current_prompt.field, value)
        self.answered_fields.add(self._field_key(self.current_prompt.field))
        self.current_prompt = self._next_field_prompt()
        self._safe_log_request("диалог идет")
        if self.current_prompt:
            return [self._render_prompt(self.current_prompt)]
        return self._advance_after_flight()

    def _advance_after_flight(self) -> list[str]:
        issues = validate_flight(self._current_flight())
        fatal_issues = [issue for issue in issues if issue.fatal]
        if fatal_issues:
            self.current_prompt = self._next_field_prompt()
            messages = ["Нужно исправить ошибки:"]
            messages.extend(f"- {issue.message}" for issue in fatal_issues)
            if self.current_prompt:
                messages.append(self._render_prompt(self.current_prompt))
            return messages

        if self.current_direction == Direction.FORWARD:
            self.stage = "another_forward"
            self.current_prompt = Prompt(
                field="another_forward",
                label="Еще рейс в прямом направлении?",
                parser=lambda value: parse_yes_no(value, default=False),
                default=False,
                choices=["да", "нет"],
            )
            return [self._render_prompt(self.current_prompt)]

        self.stage = "another_backhaul"
        self.current_prompt = Prompt(
            field="another_backhaul",
            label="Еще рейс в обратном направлении?",
            parser=lambda value: parse_yes_no(value, default=False),
            default=False,
            choices=["да", "нет"],
        )
        return [self._render_prompt(self.current_prompt)]

    def _next_field_prompt(self) -> Prompt | None:
        flight = self._current_flight()
        prompts = [
            Prompt("client_short", "Клиент кратко", parse_optional_text, optional=True),
            Prompt("loading_address", "Адрес загрузки", parse_required_text),
            Prompt("loading_date", "Дата загрузки", parse_optional_text, optional=True),
            Prompt("distance_to_loading_km", "Пробег до места загрузки, км", parse_non_negative_float),
            Prompt("unloading_address", "Адрес выгрузки", parse_required_text),
            Prompt("unloading_date", "Дата выгрузки", parse_optional_text, optional=True),
            Prompt("rate_with_vat_rub", "Ставка в рублях с НДС", parse_non_negative_float),
            Prompt(
                "status",
                "Статус перевозки",
                parse_transport_status,
                choices=["Внутрироссийская", "Международная"],
            ),
            Prompt(
                "vat_percent",
                "НДС",
                parse_vat,
                choices=["22", "0"],
            ),
            Prompt("distance_to_unloading_km", "Пробег до места выгрузки, км", parse_non_negative_float),
            Prompt(
                "cargo_weight_kg",
                "Вес груза, кг",
                parse_non_negative_float,
                default=DEFAULT_WEIGHT_KG,
            ),
            Prompt(
                "loading_type",
                "Загрузка",
                parse_loading_type,
                default=LoadingType.REAR,
                choices=["сзади", "сверху/сбоку"],
            ),
        ]
        if flight.status == TransportStatus.INTERNATIONAL:
            prompts.insert(
                6,
                Prompt(
                    "country",
                    "Страна",
                    parse_country,
                    choices=COUNTRY_OPTIONS,
                ),
            )
            prompts.insert(
                -2,
                Prompt(
                    "russian_territory_km",
                    "Пробег по территории РФ, км",
                    parse_non_negative_float,
                ),
            )
        elif flight.status == TransportStatus.DOMESTIC:
            flight.country = COUNTRY_RUSSIA
            flight.russian_territory_km = None

        for prompt in prompts:
            value = getattr(flight, prompt.field)
            field_key = self._field_key(prompt.field)
            if field_key in self.answered_fields and prompt.optional:
                continue
            if field_key not in self.answered_fields:
                return prompt
            if value in (None, ""):
                return prompt
        return None

    def _handle_save_answer(self, text: str) -> list[str]:
        should_save = parse_yes_no(text, default=True)
        self.stage = "done"
        if not should_save:
            return ["Ок, расчет не записан в Google Sheets. Нажмите \"Новый расчет\" для следующей заявки."]
        if not self.result:
            return ["Расчет не найден. Нажмите \"Новый расчет\" и повторите ввод."]
        if not sheets_is_configured():
            return ["Google Sheets не настроен. Расчет не записан."]
        try:
            append_result(
                self.round_trip,
                self.result,
                response_text=format_result(self.result),
                request_id=self.request_id,
                request_source="Desktop",
                request_user="Desktop",
            )
        except Exception as exc:
            return [
                "Расчет готов, но запись в Google Sheets не удалась.",
                f"Ошибка Google Sheets: {exc}",
            ]
        return ["Расчет записан в Google Sheets. Нажмите \"Новый расчет\" для следующей заявки."]

    def _complete_and_calculate(self) -> list[str]:
        try:
            self.result = calculate_round_trip(self.round_trip)
        except ValueError as exc:
            self.stage = "done"
            self._safe_log_request("ошибка", str(exc))
            return [f"Не удалось рассчитать: {exc}"]
        self._safe_log_request("расчет выполнен")
        self.stage = "save"
        self.current_prompt = Prompt(
            field="save",
            label="Записать расчет в Google Sheets?",
            parser=lambda value: parse_yes_no(value, default=True),
            default=True,
            choices=["да", "нет"],
        )
        return [format_result(self.result), self._render_prompt(self.current_prompt)]

    def _current_flight(self) -> Flight:
        flights = self._direction_flights()
        return flights[self.current_index]

    def _field_key(self, field: str) -> tuple[Direction, int, str]:
        return (self.current_direction, self.current_index, field)

    def _direction_flights(self) -> list[Flight]:
        if self.current_direction == Direction.FORWARD:
            return self.round_trip.forward_flights
        return self.round_trip.backhaul_flights

    def _render_prompt(self, prompt: Prompt) -> str:
        parts = [prompt.label]
        if prompt.choices:
            rendered_choices = " / ".join(f"{idx + 1}. {choice}" for idx, choice in enumerate(prompt.choices))
            parts.append(f"Варианты: {rendered_choices}")
        if prompt.default is not None:
            default = prompt.default.value if hasattr(prompt.default, "value") else prompt.default
            parts.append(f"По умолчанию: {default}")
        if prompt.optional:
            parts.append("Можно оставить пустым.")
        return "\n".join(parts)

    def _parse_current_prompt(self, text: str) -> object:
        if self.current_prompt is None:
            raise ValueError("нет активного вопроса.")
        if not text.strip() and self.current_prompt.default is not None:
            return self.current_prompt.default
        return self.current_prompt.parser(text)

    def handle_control_prompt(self, value: bool) -> list[str]:
        if self.stage == "another_forward":
            if value:
                self.round_trip.forward_flights.append(Flight(direction=Direction.FORWARD))
                self.current_index += 1
                self.stage = "collect"
                self.current_prompt = self._next_field_prompt()
                self._safe_log_request("диалог идет")
                return ["Добавлен прямой рейс.", self._render_prompt(self.current_prompt)]
            self.stage = "has_backhaul"
            self.current_prompt = Prompt(
                field="has_backhaul",
                label="Есть рейс в обратном направлении? Если нет, посчитаю пустой возврат с таким же пробегом обратно",
                parser=lambda text: parse_yes_no(text, default=False),
                default=False,
                choices=["да", "нет"],
            )
            self._safe_log_request("диалог идет")
            return [self._render_prompt(self.current_prompt)]

        if self.stage == "has_backhaul":
            if value:
                self.current_direction = Direction.BACKHAUL
                self.current_index = 0
                self.round_trip.backhaul_flights.append(Flight(direction=Direction.BACKHAUL))
                self.stage = "collect"
                self.current_prompt = self._next_field_prompt()
                self._safe_log_request("диалог идет")
                return ["Добавлен обратный рейс.", self._render_prompt(self.current_prompt)]
            self.round_trip.backhaul_flights = [self._make_empty_return_flight()]
            return [
                "Обратной загрузки нет. Добавлен пустой возврат с таким же пробегом обратно.",
                *self._complete_and_calculate(),
            ]

        if self.stage == "another_backhaul":
            if value:
                self.round_trip.backhaul_flights.append(Flight(direction=Direction.BACKHAUL))
                self.current_index += 1
                self.stage = "collect"
                self.current_prompt = self._next_field_prompt()
                self._safe_log_request("диалог идет")
                return ["Добавлен обратный рейс.", self._render_prompt(self.current_prompt)]
            return self._complete_and_calculate()

        return ["Сейчас этот ответ не ожидается."]

    def _make_empty_return_flight(self) -> Flight:
        last_forward = self.round_trip.forward_flights[-1]
        first_forward = self.round_trip.forward_flights[0]
        return_distance = sum(flight.distance_to_unloading_km or 0 for flight in self.round_trip.forward_flights)
        russian_territory_km = None
        if last_forward.status == TransportStatus.INTERNATIONAL:
            russian_territory_km = sum(
                flight.russian_territory_km or 0
                for flight in self.round_trip.forward_flights
                if flight.status == TransportStatus.INTERNATIONAL
            )
        return Flight(
            direction=Direction.BACKHAUL,
            client_short="Пустой возврат",
            loading_address=last_forward.unloading_address,
            distance_to_loading_km=0.0,
            unloading_address=first_forward.loading_address,
            rate_with_vat_rub=0.0,
            status=last_forward.status,
            country=last_forward.country,
            vat_percent=last_forward.vat_percent if last_forward.vat_percent is not None else 22,
            distance_to_unloading_km=return_distance,
            russian_territory_km=russian_territory_km,
            cargo_weight_kg=DEFAULT_WEIGHT_KG,
            loading_type=LoadingType.REAR,
        )

    def _safe_log_request(self, calculation_status: str, error_comment: str = "") -> None:
        if not sheets_is_configured():
            return
        if self._log_thread and self._log_thread.is_alive():
            return

        raw_text = "\n".join(self.raw_entries) or "Desktop dialog started"
        round_trip = deepcopy(self.round_trip)

        def log_request() -> None:
            try:
                append_request_log(
                    request_id=self.request_id,
                    source="Desktop",
                    user="Desktop",
                    message_type="desktop",
                    raw_text=raw_text,
                    image_file_id=None,
                    ai_model="",
                    ai_status="без AI",
                    calculation_status=calculation_status,
                    error_comment=error_comment,
                    round_trip=round_trip,
                )
            except Exception:
                return

        self._log_thread = Thread(target=log_request, daemon=True)
        self._log_thread.start()


def parse_optional_text(value: str) -> str | None:
    return value.strip() or None


def parse_required_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("поле обязательно.")
    return value


def parse_non_negative_float(value: str) -> float:
    value = value.strip().replace(",", ".")
    if not value:
        raise ValueError("введите число.")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError("введите число.") from exc
    if parsed < 0:
        raise ValueError("число не может быть отрицательным.")
    return parsed


def parse_yes_no(value: str, default: bool) -> bool:
    value = value.strip().lower()
    if not value:
        return default
    if value in {"1", "д", "да", "y", "yes"}:
        return True
    if value in {"2", "н", "нет", "n", "no"}:
        return False
    raise ValueError("ответьте да или нет.")


def parse_transport_status(value: str) -> TransportStatus:
    value = value.strip().lower()
    if value in {"1", "внутрироссийская", "рф", "россия"}:
        return TransportStatus.DOMESTIC
    if value in {"2", "международная", "международный"}:
        return TransportStatus.INTERNATIONAL
    raise ValueError("выберите внутрироссийская или международная.")


def parse_vat(value: str) -> int:
    value = value.strip().replace("%", "")
    if value in {"22", "1"}:
        return 22
    if value in {"0", "2"}:
        return 0
    raise ValueError("НДС должен быть 22 или 0.")


def parse_country(value: str) -> str:
    value = value.strip().lower()
    aliases = {
        "1": "Беларусь",
        "беларусь": "Беларусь",
        "рб": "Беларусь",
        "2": "Китай",
        "китай": "Китай",
        "кнр": "Китай",
        "3": "Монголия",
        "монголия": "Монголия",
    }
    if value in aliases:
        return aliases[value]
    raise ValueError("выберите страну: Беларусь, Китай или Монголия.")


def parse_loading_type(value: str) -> LoadingType:
    value = value.strip().lower()
    if not value or value in {"1", "сзади", "задняя"}:
        return LoadingType.REAR
    if value in {"2", "сверху", "сбоку", "сверху/сбоку"}:
        return LoadingType.TOP_SIDE
    raise ValueError("выберите загрузку: сзади или сверху/сбоку.")
