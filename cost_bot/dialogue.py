from collections.abc import Callable

from .ai_parser import parse_with_ai_if_configured
from .calculator import FlightCost, RoundTripCost, calculate_round_trip
from .config import COUNTRY_OPTIONS, DEFAULT_WEIGHT_KG
from .models import Direction, Flight, LoadingType, RoundTrip, TransportStatus
from .sheets import append_result, is_configured as sheets_is_configured
from .validators import validate_flight


Prompt = Callable[[str], str]


FIELD_LABELS = {
    "client_short": "Клиент кратко",
    "loading_address": "Адрес загрузки",
    "distance_to_loading_km": "Пробег до места загрузки, км",
    "unloading_address": "Адрес выгрузки",
    "rate_with_vat_rub": "Ставка в рублях с НДС",
    "status": "Статус перевозки",
    "country": "Страна",
    "vat_percent": "НДС",
    "distance_to_unloading_km": "Пробег до места выгрузки, км",
    "russian_territory_km": "Пробег по территории РФ, км",
    "cargo_weight_kg": "Вес груза, кг",
    "loading_type": "Загрузка",
}


def run_cli() -> None:
    print("Бот расчета себестоимости грузоперевозки")
    print("Вставьте описание заявки. Можно оставить пустым и заполнить вручную.")
    text = input("> ").strip()

    round_trip = parse_with_ai_if_configured(text) if text else RoundTrip()
    if not round_trip.forward_flights:
        round_trip.forward_flights.append(Flight(direction=Direction.FORWARD))

    collect_flights(round_trip.forward_flights, Direction.FORWARD, "прямом")

    if ask_yes_no("Есть рейс в обратном направлении?", default=False):
        if not round_trip.backhaul_flights:
            round_trip.backhaul_flights.append(Flight(direction=Direction.BACKHAUL))
        collect_flights(round_trip.backhaul_flights, Direction.BACKHAUL, "обратном")

    all_issues = []
    for idx, flight in enumerate(round_trip.flights, 1):
        for issue in validate_flight(flight):
            all_issues.append(f"Рейс {idx}, {FIELD_LABELS.get(issue.field, issue.field)}: {issue.message}")

    if all_issues:
        print("\nНужно исправить ошибки:")
        for issue in all_issues:
            print(f"- {issue}")
        return

    result = calculate_round_trip(round_trip)
    response_text = format_result(result)
    print(response_text)
    if sheets_is_configured():
        append_result(
            round_trip,
            result,
            response_text=response_text,
            request_source="CLI",
            request_user="CLI",
        )
        print("\nРезультат записан в Google Sheets.")


def collect_flights(flights: list[Flight], direction: Direction, direction_label: str) -> None:
    index = 0
    while index < len(flights):
        flight = flights[index]
        flight.direction = direction
        print(f"\nРейс {index + 1} в {direction_label} направлении")
        collect_flight(flight)
        index += 1
        if index == len(flights) and ask_yes_no(f"Еще рейс в {direction_label} направлении?", default=False):
            flights.append(Flight(direction=direction))


def collect_flight(flight: Flight) -> None:
    flight.client_short = ask_optional("Клиент кратко", flight.client_short)
    flight.loading_address = ask_text("Адрес загрузки", flight.loading_address)
    flight.distance_to_loading_km = ask_float("Пробег до места загрузки, км", flight.distance_to_loading_km)
    flight.unloading_address = ask_text("Адрес выгрузки", flight.unloading_address)
    flight.rate_with_vat_rub = ask_float("Ставка в рублях с НДС", flight.rate_with_vat_rub)
    flight.status = ask_status(flight.status)
    if flight.status == TransportStatus.INTERNATIONAL:
        flight.country = ask_country(flight.country)
    else:
        flight.country = "Россия"
        flight.russian_territory_km = None
    flight.vat_percent = ask_vat(flight.vat_percent)
    flight.distance_to_unloading_km = ask_float(
        "Пробег до места выгрузки, км",
        flight.distance_to_unloading_km,
    )
    if flight.status == TransportStatus.INTERNATIONAL:
        flight.russian_territory_km = ask_float("Пробег по территории РФ, км", flight.russian_territory_km)
    flight.cargo_weight_kg = ask_float("Вес груза, кг", flight.cargo_weight_kg or DEFAULT_WEIGHT_KG)
    flight.loading_type = ask_loading_type(flight.loading_type)


def ask_optional(label: str, current: str | None = None) -> str | None:
    value = input(default_prompt(label, current, "можно пусто")).strip()
    return value or current


def ask_text(label: str, current: str | None = None) -> str:
    while True:
        value = input(default_prompt(label, current)).strip()
        if value:
            return value
        if current:
            return current
        print("Поле обязательно.")


def ask_float(label: str, current: float | None = None) -> float:
    while True:
        value = input(default_prompt(label, current)).replace(",", ".").strip()
        if not value and current is not None:
            return current
        try:
            parsed = float(value)
        except ValueError:
            print("Введите число.")
            continue
        if parsed < 0:
            print("Число не может быть отрицательным.")
            continue
        return parsed


def ask_yes_no(label: str, default: bool) -> bool:
    suffix = "Д/н" if default else "д/Н"
    while True:
        value = input(f"{label} ({suffix}): ").strip().lower()
        if not value:
            return default
        if value in {"д", "да", "y", "yes"}:
            return True
        if value in {"н", "нет", "n", "no"}:
            return False
        print("Ответьте да или нет.")


def ask_status(current: TransportStatus | None = None) -> TransportStatus:
    options = [TransportStatus.DOMESTIC, TransportStatus.INTERNATIONAL]
    return ask_enum("Статус перевозки", options, current)


def ask_country(current: str | None = None) -> str:
    while True:
        value = input(default_prompt("Страна: Беларусь / Китай / Монголия", current)).strip()
        if not value and current:
            return current
        normalized = normalize_country(value)
        if normalized:
            return normalized
        print(f"Выберите страну: {', '.join(COUNTRY_OPTIONS)}.")


def ask_vat(current: int | None = None) -> int:
    while True:
        value = input(default_prompt("НДС: 22 или 0", current)).strip().replace("%", "")
        if not value and current is not None:
            return current
        if value in {"22", "0"}:
            return int(value)
        print("НДС должен быть 22 или 0.")


def ask_loading_type(current: LoadingType = LoadingType.REAR) -> LoadingType:
    options = [LoadingType.REAR, LoadingType.TOP_SIDE]
    return ask_enum("Загрузка", options, current)


def ask_enum(label: str, options: list, current):
    rendered = " / ".join(item.value for item in options)
    while True:
        value = input(default_prompt(f"{label}: {rendered}", current.value if current else None)).strip().lower()
        if not value and current:
            return current
        for option in options:
            if value == option.value.lower():
                return option
        print(f"Выберите одно из значений: {rendered}.")


def default_prompt(label: str, current, hint: str | None = None) -> str:
    parts = [label]
    if current not in (None, ""):
        parts.append(f"по умолчанию {current}")
    elif hint:
        parts.append(hint)
    return f"{' ('.join(parts) + ')' if len(parts) > 1 else parts[0]}: "


def normalize_country(value: str) -> str | None:
    value = value.strip().lower()
    aliases = {
        "беларусь": "Беларусь",
        "рб": "Беларусь",
        "китай": "Китай",
        "кнр": "Китай",
        "монголия": "Монголия",
    }
    return aliases.get(value)


def format_result(result: RoundTripCost) -> str:
    lines = ["\nРасчет себестоимости"]
    for idx, item in enumerate(result.flights, 1):
        lines.extend(format_flight_cost(idx, item))
    lines.extend(
        [
            "",
            f"Общая выручка: {money(result.total_revenue_rub)}",
            f"Общая себестоимость: {money(result.total_cost_rub)}",
            f"Расчетная прибыль круго-рейса: {money(result.total_profit_rub)}",
            "",
            "Комментарий менеджеру:",
            manager_comment(result),
        ]
    )
    return "\n".join(lines)


def format_flight_cost(index: int, item: FlightCost) -> list[str]:
    return [
        "",
        f"Рейс {index}:",
        f"- Выручка: {money(item.revenue_rub)}",
        f"- Топливо: {money(item.fuel_rub)}",
        f"- Обслуживание: {money(item.maintenance_rub)}",
        f"- Водитель и постоянные расходы: {money(item.driver_and_fixed_rub)}",
        f"- Платные дороги: {money(item.tolls_rub)}",
        f"- Доплата за тип загрузки: {money(item.loading_extra_rub)}",
        f"- Международные расходы: {money(item.international_extra_rub)}",
        f"- Себестоимость рейса: {money(item.total_cost_rub)}",
        f"- Прибыль рейса: {money(item.profit_rub)}",
    ]


def manager_comment(result: RoundTripCost) -> str:
    margin = result.total_profit_rub / result.total_revenue_rub * 100 if result.total_revenue_rub else 0
    if result.total_profit_rub < 0:
        return f"Рейс убыточный, маржинальность {margin:.1f}%. Нужно пересмотреть ставку или маршрут."
    if margin < 10:
        return f"Маржинальность низкая: {margin:.1f}%. Рекомендуется проверить риски и дополнительные расходы."
    return f"Расчетная маржинальность {margin:.1f}%. Можно передавать расчет на согласование."


def money(value: float) -> str:
    return f"{value:,.0f} руб.".replace(",", " ")
