from dataclasses import dataclass

from .config import COUNTRY_OPTIONS, COUNTRY_RUSSIA, MAX_BACKHAUL_WEIGHT_KG, VAT_OPTIONS
from .models import Direction, Flight, TransportStatus


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    message: str
    fatal: bool = False


def missing_fields(flight: Flight) -> list[str]:
    required = [
        "loading_address",
        "distance_to_loading_km",
        "unloading_address",
        "status",
        "vat_percent",
        "distance_to_unloading_km",
    ]
    missing = [name for name in required if getattr(flight, name) in (None, "")]
    if flight.status == TransportStatus.INTERNATIONAL:
        if not flight.country:
            missing.append("country")
        if flight.russian_territory_km is None:
            missing.append("russian_territory_km")
    return missing


def validate_flight(flight: Flight) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    for field_name in missing_fields(flight):
        issues.append(ValidationIssue(field_name, "Поле обязательно для расчета."))

    if flight.cargo_weight_kg <= 0:
        issues.append(ValidationIssue("cargo_weight_kg", "Вес должен быть больше 0.", True))

    if flight.cargo_weight_kg > MAX_BACKHAUL_WEIGHT_KG:
        issues.append(
            ValidationIssue(
                "cargo_weight_kg",
                f"Вес груза не должен превышать {MAX_BACKHAUL_WEIGHT_KG} кг.",
                True,
            )
        )

    if flight.vat_percent is not None and flight.vat_percent not in VAT_OPTIONS:
        issues.append(ValidationIssue("vat_percent", "НДС должен быть 22% или 0%.", True))

    if flight.status == TransportStatus.DOMESTIC and flight.country and flight.country != COUNTRY_RUSSIA:
        issues.append(
            ValidationIssue(
                "country",
                "Для внутрироссийской перевозки страна должна быть Россия.",
                True,
            )
        )

    if flight.status == TransportStatus.INTERNATIONAL:
        if flight.country == COUNTRY_RUSSIA:
            issues.append(ValidationIssue("country", "Для международной перевозки нужна зарубежная страна.", True))
        if flight.country and flight.country not in COUNTRY_OPTIONS:
            issues.append(
                ValidationIssue(
                    "country",
                    f"Поддерживаемые страны: {', '.join(COUNTRY_OPTIONS)}.",
                    True,
                )
            )
        if (
            flight.russian_territory_km is not None
            and flight.distance_to_unloading_km is not None
            and flight.russian_territory_km > flight.distance_to_unloading_km
        ):
            issues.append(
                ValidationIssue(
                    "russian_territory_km",
                    "Пробег по РФ не может быть больше общего пробега до выгрузки.",
                    True,
                )
            )

    for name in ("distance_to_loading_km", "distance_to_unloading_km", "russian_territory_km"):
        value = getattr(flight, name)
        if value is not None and value < 0:
            issues.append(ValidationIssue(name, "Пробег не может быть отрицательным.", True))

    if flight.rate_with_vat_rub is not None and flight.rate_with_vat_rub < 0:
        issues.append(ValidationIssue("rate_with_vat_rub", "Ставка не может быть отрицательной.", True))

    return issues


def route_has_land_road(flight: Flight) -> bool:
    """Placeholder for map API integration."""
    return bool(flight.loading_address and flight.unloading_address)


def direction_is_allowed(flight: Flight, prohibited_pairs: set[tuple[str, str]]) -> bool:
    if not flight.country:
        return True
    return (flight.country, flight.direction.value) not in prohibited_pairs
