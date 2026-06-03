import json
from typing import Any

from .models import Direction, Flight, LoadingType, RoundTrip, TransportStatus
from .settings import get_settings


SYSTEM_INSTRUCTION = """
Ты извлекаешь данные для расчета грузоперевозки.
Если данных нет, ставь null. Не выдумывай пробег, ставку, НДС и вес.
"""


FLIGHT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_short": {"type": ["string", "null"]},
        "loading_address": {"type": ["string", "null"]},
        "distance_to_loading_km": {"type": ["number", "null"]},
        "unloading_address": {"type": ["string", "null"]},
        "rate_with_vat_rub": {"type": ["number", "null"]},
        "status": {
            "type": ["string", "null"],
            "enum": ["Внутрироссийская", "Международная", None],
        },
        "country": {
            "type": ["string", "null"],
            "enum": ["Россия", "Беларусь", "Китай", "Монголия", None],
        },
        "vat_percent": {"type": ["integer", "null"], "enum": [22, 0, None]},
        "distance_to_unloading_km": {"type": ["number", "null"]},
        "russian_territory_km": {"type": ["number", "null"]},
        "cargo_weight_kg": {"type": ["number", "null"]},
        "loading_type": {
            "type": ["string", "null"],
            "enum": ["сзади", "сверху/сбоку", None],
        },
    },
    "required": [
        "client_short",
        "loading_address",
        "distance_to_loading_km",
        "unloading_address",
        "rate_with_vat_rub",
        "status",
        "country",
        "vat_percent",
        "distance_to_unloading_km",
        "russian_territory_km",
        "cargo_weight_kg",
        "loading_type",
    ],
    "additionalProperties": False,
}


ROUND_TRIP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "forward_flights": {"type": "array", "items": FLIGHT_SCHEMA},
        "backhaul_flights": {"type": "array", "items": FLIGHT_SCHEMA},
    },
    "required": ["forward_flights", "backhaul_flights"],
    "additionalProperties": False,
}


def parse_with_ai_if_configured(text: str) -> RoundTrip:
    """Optional AI parser.

    Set OPENAI_API_KEY and install the OpenAI Python SDK to enable this.
    Without it, the bot continues in manual question mode.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        return RoundTrip()

    try:
        from openai import OpenAI
    except ImportError:
        return RoundTrip()

    client = OpenAI()
    response = client.responses.create(
        model=settings.openai_model,
        instructions=SYSTEM_INSTRUCTION,
        input=text,
        text={
            "format": {
                "type": "json_schema",
                "name": "round_trip_request",
                "strict": True,
                "schema": ROUND_TRIP_SCHEMA,
            }
        },
    )
    data = json.loads(response.output_text)
    return round_trip_from_dict(data)


def round_trip_from_dict(data: dict[str, Any]) -> RoundTrip:
    return RoundTrip(
        forward_flights=[
            flight_from_dict(Direction.FORWARD, item) for item in data.get("forward_flights", [])
        ],
        backhaul_flights=[
            flight_from_dict(Direction.BACKHAUL, item) for item in data.get("backhaul_flights", [])
        ],
    )


def flight_from_dict(direction: Direction, data: dict[str, Any]) -> Flight:
    status = data.get("status")
    loading_type = data.get("loading_type")
    return Flight(
        direction=direction,
        client_short=data.get("client_short"),
        loading_address=data.get("loading_address"),
        distance_to_loading_km=_optional_float(data.get("distance_to_loading_km")),
        unloading_address=data.get("unloading_address"),
        rate_with_vat_rub=_optional_float(data.get("rate_with_vat_rub")),
        status=TransportStatus(status) if status else None,
        country=data.get("country"),
        vat_percent=_optional_int(data.get("vat_percent")),
        distance_to_unloading_km=_optional_float(data.get("distance_to_unloading_km")),
        russian_territory_km=_optional_float(data.get("russian_territory_km")),
        cargo_weight_kg=_optional_float(data.get("cargo_weight_kg")) or 20_000,
        loading_type=LoadingType(loading_type) if loading_type else LoadingType.REAR,
    )


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)
