from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from .config import COUNTRY_OPTIONS, COUNTRY_RUSSIA, DEFAULT_WEIGHT_KG
from .models import Direction, Flight, LoadingType, RoundTrip, TransportStatus
from .validators import validate_flight


MAX_FORWARD_FLIGHTS = 3
MAX_BACKHAUL_FLIGHTS = 3

YES_WORDS = {"да", "д", "yes", "y", "ок", "ok", "ага", "верно"}
NO_WORDS = {"нет", "н", "no", "n", "не", "не надо"}
SKIP_WORDS = {"", "-", "пропустить", "не знаю", "незнаю", "ок", "оставить", "по умолчанию"}
FOREIGN_CURRENCY_RE = re.compile(
    r"(\busd\b|\$|долл?|доллар|у\.?\s*е\.?|\beur\b|€|евро|\bcny\b|\brmb\b|¥|юан|юань|юаней|yuan)",
    re.IGNORECASE,
)


class ClarificationNeeded(ValueError):
    pass


@dataclass(frozen=True)
class Prompt:
    field: str
    label: str
    parser: Callable[[str], object]
    optional: bool = False
    default: object | None = None
    choices: list[str] | None = None


@dataclass
class TelegramDialogSession:
    round_trip: RoundTrip
    source_text: str = ""
    message_type: str = "text"
    image_file_id: str | None = None
    image_cell_value: str | None = None
    request_id: str = field(default_factory=lambda: f"req-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid4().hex[:8]}")
    current_direction: Direction = Direction.FORWARD
    current_index: int = 0
    current_prompt: Prompt | None = None
    stage: str = "collect"
    is_ready: bool = False
    answered_fields: set[tuple[Direction, int, str]] = field(default_factory=set)
    foreign_rate_amounts: dict[tuple[Direction, int], float] = field(default_factory=dict)
    foreign_rate_currencies: dict[tuple[Direction, int], str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.round_trip.forward_flights = self.round_trip.forward_flights[:MAX_FORWARD_FLIGHTS]
        self.round_trip.backhaul_flights = self.round_trip.backhaul_flights[:MAX_BACKHAUL_FLIGHTS]
        if not self.round_trip.forward_flights:
            self.round_trip.forward_flights.append(Flight(direction=Direction.FORWARD))
        for flight in self.round_trip.forward_flights:
            flight.direction = Direction.FORWARD
        for flight in self.round_trip.backhaul_flights:
            flight.direction = Direction.BACKHAUL

    def start(self) -> list[str]:
        messages = [self.initial_summary()]
        messages.extend(self._advance())
        return messages

    def handle_answer(self, text: str) -> list[str]:
        text = text.strip()
        if self.is_ready:
            return ["Расчет уже сформирован. Для нового запроса отправьте новую заявку или команду /new."]

        if self.stage in {"another_forward", "has_backhaul", "another_backhaul"}:
            try:
                answer = parse_yes_no(text)
            except ValueError as exc:
                return [str(exc), self._render_prompt(self.current_prompt)]
            return self._handle_control_answer(answer)

        if self.current_prompt is None:
            return self._advance()

        try:
            value = self._parse_prompt_value(self.current_prompt, text)
        except ClarificationNeeded as exc:
            return [str(exc)]
        except ValueError as exc:
            return [f"Ошибка: {exc}", self._render_prompt(self.current_prompt)]

        setattr(self._current_flight(), self.current_prompt.field, value)
        self.answered_fields.add(self._field_key(self.current_prompt.field))
        self.current_prompt = None
        return self._advance()

    def initial_summary(self) -> str:
        parts = ["ОК, я начал разбор заявки."]
        if self.round_trip.forward_flights:
            parts.append("Прямое направление:")
            for index, flight in enumerate(self.round_trip.forward_flights, 1):
                parts.append(f"- {self._flight_summary(flight, index)}")
        if self.round_trip.backhaul_flights:
            parts.append("Обратное направление:")
            for index, flight in enumerate(self.round_trip.backhaul_flights, 1):
                parts.append(f"- {self._flight_summary(flight, index)}")
        else:
            parts.append("Обратной загрузки пока не вижу, уточню дальше.")
        parts.append("Дальше задам вопросы по одному.")
        return "\n".join(parts)

    def _flight_summary(self, flight: Flight, index: int) -> str:
        route = "маршрут не заполнен"
        if flight.loading_address and flight.unloading_address:
            route = f"{flight.loading_address} - {flight.unloading_address}"
        elif flight.loading_address:
            route = f"загрузка {flight.loading_address}"
        elif flight.unloading_address:
            route = f"выгрузка {flight.unloading_address}"

        rate = ""
        if flight.rate_with_vat_rub:
            if self._source_has_foreign_rate():
                rate = (
                    f", ставка указана как {amount(flight.rate_with_vat_rub)} "
                    f"{self._foreign_rate_currency()}, уточню курс для пересчета"
                )
            else:
                rate = f", ставка {money(flight.rate_with_vat_rub)} с НДС"
        else:
            rate = ", ставка не указана, посчитаю себестоимость без выручки"
        return f"рейс {index}: {route}{rate}"

    def _advance(self) -> list[str]:
        while True:
            self.current_prompt = self._next_field_prompt()
            if self.current_prompt:
                return [self._render_prompt(self.current_prompt)]

            fatal_issues = [issue for issue in validate_flight(self._current_flight()) if issue.fatal]
            if fatal_issues:
                issue = fatal_issues[0]
                self.answered_fields.discard(self._field_key(issue.field))
                self.current_prompt = self._prompt_for_field(issue.field)
                if self.current_prompt:
                    return [
                        f"Нужно исправить поле: {issue.message}",
                        self._render_prompt(self.current_prompt),
                    ]
                return [f"Нужно исправить ошибку: {issue.message}"]

            if self.current_direction == Direction.FORWARD:
                if self.current_index + 1 < len(self.round_trip.forward_flights):
                    self.current_index += 1
                    continue
                if len(self.round_trip.forward_flights) < MAX_FORWARD_FLIGHTS:
                    self.stage = "another_forward"
                    self.current_prompt = Prompt(
                        field="another_forward",
                        label=(
                            "Добавить еще прямой рейс "
                            f"(Можно добавить еще {MAX_FORWARD_FLIGHTS - len(self.round_trip.forward_flights)})"
                        ),
                        parser=parse_yes_no,
                    )
                    return [self._render_prompt(self.current_prompt)]
                return self._move_to_backhaul_or_finish()

            if self.current_index + 1 < len(self.round_trip.backhaul_flights):
                self.current_index += 1
                continue
            if len(self.round_trip.backhaul_flights) < MAX_BACKHAUL_FLIGHTS:
                self.stage = "another_backhaul"
                self.current_prompt = Prompt(
                    field="another_backhaul",
                    label=(
                        "Добавить еще обратный рейс "
                        f"(Можно добавить еще {MAX_BACKHAUL_FLIGHTS - len(self.round_trip.backhaul_flights)})"
                    ),
                    parser=parse_yes_no,
                )
                return [self._render_prompt(self.current_prompt)]
            return self._finish()

    def _move_to_backhaul_or_finish(self) -> list[str]:
        if self.round_trip.backhaul_flights:
            self.current_direction = Direction.BACKHAUL
            self.current_index = 0
            self.stage = "collect"
            return self._advance()

        self.stage = "has_backhaul"
        self.current_prompt = Prompt(
            field="has_backhaul",
            label="Есть обратная загрузка?",
            parser=parse_yes_no,
            default=False,
            choices=["да", "нет"],
        )
        return [self._render_prompt(self.current_prompt)]

    def _handle_control_answer(self, answer: bool) -> list[str]:
        if self.stage == "another_forward":
            if answer:
                self.round_trip.forward_flights.append(Flight(direction=Direction.FORWARD))
                self.current_index = len(self.round_trip.forward_flights) - 1
                self.stage = "collect"
                self.current_prompt = None
                return [f"Добавил прямой рейс {self.current_index + 1}.", *self._advance()]
            return self._move_to_backhaul_or_finish()

        if self.stage == "has_backhaul":
            if answer:
                self.round_trip.backhaul_flights.append(Flight(direction=Direction.BACKHAUL))
                self.current_direction = Direction.BACKHAUL
                self.current_index = 0
                self.stage = "collect"
                self.current_prompt = None
                return ["Добавил обратный рейс 1.", *self._advance()]
            return self._finish()

        if self.stage == "another_backhaul":
            if answer:
                self.round_trip.backhaul_flights.append(Flight(direction=Direction.BACKHAUL))
                self.current_index = len(self.round_trip.backhaul_flights) - 1
                self.stage = "collect"
                self.current_prompt = None
                return [f"Добавил обратный рейс {self.current_index + 1}.", *self._advance()]
            return self._finish()

        return self._advance()

    def _finish(self) -> list[str]:
        self.is_ready = True
        self.current_prompt = None
        self.stage = "ready"
        return ["Данные собраны. Считаю себестоимость и прибыль."]

    def _next_field_prompt(self) -> Prompt | None:
        flight = self._current_flight()
        for prompt in self._field_prompts(flight):
            if self._should_ask(prompt, flight):
                return prompt
        return None

    def _field_prompts(self, flight: Flight) -> list[Prompt]:
        prompts = [
            Prompt("client_short", "Клиент кратко", parse_optional_text, optional=True),
            Prompt(
                "loading_address",
                "Адрес загрузки. Лучше указать город, улицу, дом и строение, если известно",
                parse_required_text,
            ),
            Prompt(
                "distance_to_loading_km",
                "Пробег до места загрузки, км. Если машина уже на месте, можно ответить 'пропустить'",
                parse_non_negative_float,
                default=0.0,
            ),
            Prompt(
                "unloading_address",
                "Адрес выгрузки. Лучше указать город, улицу, дом и строение, если известно",
                parse_required_text,
            ),
            Prompt(
                "rate_with_vat_rub",
                "Ставка в рублях с НДС. Если ставки нет, можно ответить 'не знаю' или 'пропустить'",
                parse_non_negative_float,
                default=0.0,
            ),
            Prompt(
                "status",
                "Статус перевозки",
                parse_transport_status,
                choices=["внутрироссийская", "международная"],
            ),
        ]

        if flight.status == TransportStatus.INTERNATIONAL:
            prompts.append(
                Prompt(
                    "country",
                    "Страна: Беларусь / Китай / Монголия",
                    parse_country,
                    choices=COUNTRY_OPTIONS,
                )
            )

        prompts.extend(
            [
                Prompt("vat_percent", "НДС: 22 или 0", parse_vat, choices=["22", "0"]),
                Prompt("distance_to_unloading_km", "Пробег до места выгрузки, км", parse_non_negative_float),
            ]
        )

        if flight.status == TransportStatus.INTERNATIONAL:
            prompts.append(
                Prompt(
                    "russian_territory_km",
                    "Пробег по территории РФ, км",
                    parse_non_negative_float,
                )
            )

        prompts.extend(
            [
                Prompt(
                    "cargo_weight_kg",
                    "Вес груза, кг",
                    parse_weight,
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
        )
        return prompts

    def _should_ask(self, prompt: Prompt, flight: Flight) -> bool:
        if self._field_key(prompt.field) in self.answered_fields:
            return False

        value = getattr(flight, prompt.field)
        if prompt.optional:
            return value in (None, "")

        if prompt.field in {"distance_to_loading_km", "rate_with_vat_rub"}:
            if prompt.field == "rate_with_vat_rub" and self._rate_needs_currency_clarification(flight):
                return True
            return value in (None, "", 0, 0.0)

        if prompt.field in {"cargo_weight_kg", "loading_type"}:
            if self._default_field_was_seen(prompt.field, value):
                return False
            return True

        return value in (None, "")

    def _default_field_was_seen(self, field: str, value: object) -> bool:
        text = self.source_text.lower()
        if field == "cargo_weight_kg":
            return value not in (None, DEFAULT_WEIGHT_KG) or bool(
                re.search(r"\d+\s*(?:кг|kg|т|тонн)", text)
            )
        if field == "loading_type":
            return "сзади" in text or "сверху" in text or "сбоку" in text or "зад" in text
        return False

    def _prompt_for_field(self, field: str) -> Prompt | None:
        for prompt in self._field_prompts(self._current_flight()):
            if prompt.field == field:
                return prompt
        return None

    def _parse_prompt_value(self, prompt: Prompt, text: str) -> object:
        normalized = normalize_answer(text)
        current = getattr(self._current_flight(), prompt.field)

        if normalized in SKIP_WORDS:
            if prompt.default is not None:
                return prompt.default
            if prompt.optional:
                return current or None
            if current not in (None, ""):
                return current
            raise ValueError("Без этого поля расчет не получится. Укажите значение или напишите /cancel.")

        if prompt.field == "rate_with_vat_rub":
            foreign_amount = self._foreign_rate_amount()
            if contains_foreign_currency(text):
                parsed_amount = parse_non_negative_float(text)
                self.foreign_rate_amounts[self._flight_key()] = parsed_amount
                self.foreign_rate_currencies[self._flight_key()] = detect_currency(text)
                raise ClarificationNeeded(
                    f"Ставку понял как {amount(parsed_amount)} {self._foreign_rate_currency()}. "
                    "Укажите курс к рублю или сразу ставку в рублях."
                )
            if foreign_amount is not None:
                return parse_rate_with_currency_answer(text, foreign_amount)

        return prompt.parser(text)

    def _render_prompt(self, prompt: Prompt | None) -> str:
        if prompt is None:
            return ""
        prefix = self._current_flight_label()
        label = prompt.label
        if prompt.field == "rate_with_vat_rub":
            foreign_amount = self._foreign_rate_amount()
            if foreign_amount is not None:
                label = (
                    f"Ставка указана как {amount(foreign_amount)} {self._foreign_rate_currency()}. "
                    "Укажите курс к рублю для пересчета или сразу ставку в рублях"
                )
        parts = [f"{prefix}: {label}"]
        if prompt.default is not None:
            default = prompt.default.value if hasattr(prompt.default, "value") else prompt.default
            if isinstance(prompt.default, bool):
                default = "да" if prompt.default else "нет"
            parts.append(f"по умолчанию {default}")
        if prompt.optional:
            parts.append("можно ответить 'пропустить'")
        if prompt.choices:
            parts.append("варианты: " + " / ".join(str(item) for item in prompt.choices))
        details = f" ({'; '.join(parts[1:])})" if len(parts) > 1 else ""
        return f"{parts[0]}{details}?"

    def _current_flight(self) -> Flight:
        if self.current_direction == Direction.FORWARD:
            return self.round_trip.forward_flights[self.current_index]
        return self.round_trip.backhaul_flights[self.current_index]

    def _current_flight_label(self) -> str:
        direction = "прямой" if self.current_direction == Direction.FORWARD else "обратный"
        return f"{direction.capitalize()} рейс {self.current_index + 1}"

    def _field_key(self, field: str) -> tuple[Direction, int, str]:
        return self.current_direction, self.current_index, field

    def _flight_key(self) -> tuple[Direction, int]:
        return self.current_direction, self.current_index

    def _source_has_foreign_rate(self) -> bool:
        return contains_foreign_currency(self.source_text)

    def _foreign_rate_amount(self) -> float | None:
        if self._flight_key() in self.foreign_rate_amounts:
            return self.foreign_rate_amounts[self._flight_key()]
        if not self._source_has_foreign_rate():
            return None
        current = self._current_flight().rate_with_vat_rub
        return float(current) if current not in (None, "", 0, 0.0) else None

    def _foreign_rate_currency(self) -> str:
        return self.foreign_rate_currencies.get(self._flight_key()) or detect_currency(self.source_text)

    def _rate_needs_currency_clarification(self, flight: Flight) -> bool:
        return self._source_has_foreign_rate() and flight.rate_with_vat_rub not in (None, "", 0, 0.0)


def normalize_answer(value: str) -> str:
    return value.strip().lower().replace("ё", "е")


def parse_optional_text(value: str) -> str | None:
    value = value.strip()
    return value or None


def parse_required_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Введите текст.")
    return value


def parse_non_negative_float(value: str) -> float:
    cleaned = value.replace("\u00a0", " ").replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", cleaned.replace(" ", ""))
    if not match:
        raise ValueError("Введите число.")
    parsed = float(match.group(0))
    if parsed < 0:
        raise ValueError("Число не может быть отрицательным.")
    return parsed


def contains_foreign_currency(value: str) -> bool:
    return bool(FOREIGN_CURRENCY_RE.search(value))


def detect_currency(value: str) -> str:
    normalized = normalize_answer(value)
    if re.search(r"(\beur\b|€|евро)", normalized):
        return "EUR"
    if re.search(r"(\bcny\b|\brmb\b|¥|юан|юань|юаней|yuan)", normalized):
        return "CNY"
    if re.search(r"(\busd\b|\$|долл?|доллар|у\.?\s*е\.?)", normalized):
        return "USD"
    return "валюте"


def parse_rate_with_currency_answer(value: str, foreign_amount: float) -> float:
    parsed = parse_non_negative_float(value)
    normalized = normalize_answer(value)
    if re.search(r"(руб|rub|₽|р\b)", normalized):
        return parsed
    if "курс" in normalized or parsed <= 300:
        return foreign_amount * parsed
    return parsed


def parse_weight(value: str) -> float:
    parsed = parse_non_negative_float(value)
    normalized = normalize_answer(value)
    if "тон" in normalized or re.search(r"\d+\s*т\b", normalized):
        return parsed * 1000
    return parsed


def parse_vat(value: str) -> int:
    normalized = normalize_answer(value).replace("%", "")
    match = re.search(r"\b(22|0)\b", normalized)
    if not match:
        raise ValueError("НДС должен быть 22 или 0.")
    return int(match.group(1))


def parse_yes_no(value: str) -> bool:
    normalized = normalize_answer(value)
    if normalized in YES_WORDS:
        return True
    if normalized in NO_WORDS:
        return False
    raise ValueError("Ответьте 'да' или 'нет'.")


def parse_transport_status(value: str) -> TransportStatus:
    normalized = normalize_answer(value)
    if "меж" in normalized or any(country.lower() in normalized for country in COUNTRY_OPTIONS):
        return TransportStatus.INTERNATIONAL
    if "внут" in normalized or "рос" in normalized or "рф" in normalized:
        return TransportStatus.DOMESTIC
    raise ValueError("Выберите: внутрироссийская или международная.")


def parse_country(value: str) -> str:
    normalized = normalize_answer(value)
    aliases = {
        "беларусь": "Беларусь",
        "белоруссия": "Беларусь",
        "рб": "Беларусь",
        "китай": "Китай",
        "кнр": "Китай",
        "china": "Китай",
        "монголия": "Монголия",
        "mongolia": "Монголия",
    }
    country = aliases.get(normalized)
    if country:
        return country
    raise ValueError("Выберите страну: Беларусь, Китай или Монголия.")


def parse_loading_type(value: str) -> LoadingType:
    normalized = normalize_answer(value)
    if "верх" in normalized or "бок" in normalized:
        return LoadingType.TOP_SIDE
    if "зад" in normalized or "сзади" in normalized:
        return LoadingType.REAR
    raise ValueError("Выберите загрузку: сзади или сверху/сбоку.")


def money(value: float) -> str:
    return f"{value:,.0f} руб.".replace(",", " ")


def amount(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")
