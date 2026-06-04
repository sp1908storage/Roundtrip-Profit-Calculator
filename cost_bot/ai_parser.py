import base64
import json
import re
from typing import Any

from .models import Direction, Flight, LoadingType, RoundTrip, TransportStatus
from .settings import get_settings


RU_RUSSIA = "\u0420\u043e\u0441\u0441\u0438\u044f"
RU_BELARUS = "\u0411\u0435\u043b\u0430\u0440\u0443\u0441\u044c"
RU_CHINA = "\u041a\u0438\u0442\u0430\u0439"
RU_MONGOLIA = "\u041c\u043e\u043d\u0433\u043e\u043b\u0438\u044f"
RU_DOMESTIC = "\u0412\u043d\u0443\u0442\u0440\u0438\u0440\u043e\u0441\u0441\u0438\u0439\u0441\u043a\u0430\u044f"
RU_INTERNATIONAL = "\u041c\u0435\u0436\u0434\u0443\u043d\u0430\u0440\u043e\u0434\u043d\u0430\u044f"
RU_REAR = "\u0441\u0437\u0430\u0434\u0438"
RU_TOP_SIDE = "\u0441\u0432\u0435\u0440\u0445\u0443/\u0441\u0431\u043e\u043a\u0443"


SYSTEM_INSTRUCTION = f"""
You extract freight request data for cost calculation.
Return only JSON. No markdown, explanations, or comments.

Schema:
{{
  "forward_flights": [flight],
  "backhaul_flights": [flight]
}}

flight:
{{
  "client_short": string|null,
  "loading_address": string|null,
  "distance_to_loading_km": number|null,
  "unloading_address": string|null,
  "rate_with_vat_rub": number|null,
  "status": "{RU_DOMESTIC}"|"{RU_INTERNATIONAL}"|null,
  "country": "{RU_RUSSIA}"|"{RU_BELARUS}"|"{RU_CHINA}"|"{RU_MONGOLIA}"|null,
  "vat_percent": 22|0|null,
  "distance_to_unloading_km": number|null,
  "russian_territory_km": number|null,
  "cargo_weight_kg": number|null,
  "loading_type": "{RU_REAR}"|"{RU_TOP_SIDE}"|null
}}

Rules:
- If text contains "A - B", use A as loading_address and B as unloading_address.
- If Belarus, China, or Mongolia is mentioned, set that country and status "{RU_INTERNATIONAL}".
- VAT can only be 22 or 0.
- "20 tons" or "20 \u0442\u043e\u043d\u043d" means 20000 kg.
- Do not invent mileage, rate, VAT, or weight.
- If only one flight is mentioned, put it into forward_flights.
"""


IMAGE_SYSTEM_INSTRUCTION = SYSTEM_INSTRUCTION + """

The user may send an image/screenshot of a chat message. First read the text from
the image, then extract the same JSON fields from that text.
"""


COUNTRY_ALIASES = {
    "rossiya": RU_RUSSIA,
    "russia": RU_RUSSIA,
    "\u0440\u043e\u0441\u0441\u0438\u044f": RU_RUSSIA,
    "\u0440\u0444": RU_RUSSIA,
    "belarus": RU_BELARUS,
    "\u0431\u0435\u043b\u0430\u0440\u0443\u0441\u044c": RU_BELARUS,
    "\u0431\u0435\u043b\u043e\u0440\u0443\u0441\u0441\u0438\u044f": RU_BELARUS,
    "\u0440\u0431": RU_BELARUS,
    "china": RU_CHINA,
    "\u043a\u0438\u0442\u0430\u0439": RU_CHINA,
    "\u043a\u043d\u0440": RU_CHINA,
    "mongolia": RU_MONGOLIA,
    "\u043c\u043e\u043d\u0433\u043e\u043b\u0438\u044f": RU_MONGOLIA,
}


def parse_with_ai_if_configured(text: str) -> RoundTrip:
    settings = get_settings()
    if not settings.openai_api_key:
        return RoundTrip()

    client = _build_client()
    response_text = _chat_completion_json(
        client=client,
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": text},
        ],
    )
    data = json.loads(_extract_json_object(response_text))
    data = _postprocess_data(data, text)
    return round_trip_from_dict(data)


def parse_image_with_ai_if_configured(image_bytes: bytes, mime_type: str = "image/jpeg") -> RoundTrip:
    settings = get_settings()
    if not settings.openai_api_key:
        return RoundTrip()

    client = _build_client()
    image_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    response_text = _chat_completion_json(
        client=client,
        model=settings.openai_vision_model or settings.openai_model,
        messages=[
            {"role": "system", "content": IMAGE_SYSTEM_INSTRUCTION},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract freight request JSON from this image."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
    )
    data = json.loads(_extract_json_object(response_text))
    data = _postprocess_data(data, "")
    return round_trip_from_dict(data)


def _build_client():
    from openai import OpenAI

    settings = get_settings()
    client_kwargs = {"api_key": settings.openai_api_key}
    if settings.openai_base_url:
        client_kwargs["base_url"] = settings.openai_base_url
    return OpenAI(**client_kwargs)


def _chat_completion_json(client, model: str, messages: list[dict[str, Any]]) -> str:
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
        )
    except Exception:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
        )
    return response.choices[0].message.content or "{}"


def _extract_json_object(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("AI response does not contain JSON object.")
    return text[start : end + 1]


def _postprocess_data(data: dict[str, Any], source_text: str) -> dict[str, Any]:
    if not isinstance(data.get("forward_flights"), list):
        data["forward_flights"] = []
    if not isinstance(data.get("backhaul_flights"), list):
        data["backhaul_flights"] = []
    if not data["forward_flights"]:
        data["forward_flights"] = [{}]

    route = _extract_route(source_text)
    country_from_text = _extract_country(source_text)
    forced_status = RU_INTERNATIONAL if country_from_text and country_from_text != RU_RUSSIA else None
    vat_from_text = _extract_vat(source_text)

    for flight in [*data["forward_flights"], *data["backhaul_flights"]]:
        if route:
            flight["loading_address"] = route[0]
            flight["unloading_address"] = route[1]
        if country_from_text:
            flight["country"] = country_from_text

        flight["vat_percent"] = _clean_vat(flight.get("vat_percent"), vat_from_text)
        flight["status"] = forced_status or _clean_status(
            flight.get("status"),
            flight.get("country"),
            source_text,
        )
        flight["country"] = _clean_country(flight.get("country"), flight.get("status"))
        flight["loading_type"] = _clean_loading_type(flight.get("loading_type"), source_text)
        flight["cargo_weight_kg"] = _clean_weight(flight.get("cargo_weight_kg"), source_text)

    return data


def _extract_route(text: str) -> tuple[str, str] | None:
    match = re.search(
        r"(?:\u0440\u0435\u0439\u0441|\u043c\u0430\u0440\u0448\u0440\u0443\u0442)?\s*([A-ZА-ЯЁ][^,\n;]{1,80}?)\s*(?:-|—|->|→)\s*([A-ZА-ЯЁ][^,\n;]{1,80})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    loading = re.sub(
        r"^(\u0440\u0435\u0439\u0441|\u043c\u0430\u0440\u0448\u0440\u0443\u0442)\s+",
        "",
        match.group(1).strip(),
        flags=re.IGNORECASE,
    )
    unloading = match.group(2).strip()
    return loading, unloading


def _extract_country(text: str) -> str | None:
    lowered = text.lower()
    for alias, country in COUNTRY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return country
    return None


def _extract_vat(text: str) -> int | None:
    match = re.search(r"(?:\u043d\u0434\u0441|vat)\s*(22|0)\s*%?", text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _clean_vat(value: Any, fallback: int | None) -> int | None:
    if fallback in {0, 22}:
        return fallback
    parsed = _optional_int(value)
    return parsed if parsed in {0, 22} else None


def _clean_status(value: Any, country: Any, source_text: str) -> str | None:
    text = str(value or "").lower()
    if "international" in text or "\u043c\u0435\u0436" in text:
        return RU_INTERNATIONAL
    if "domestic" in text or "\u0432\u043d\u0443\u0442" in text:
        return RU_DOMESTIC
    if country and country != RU_RUSSIA:
        return RU_INTERNATIONAL
    lowered = source_text.lower()
    if "\u043c\u0435\u0436\u0434\u0443\u043d\u0430\u0440\u043e\u0434" in lowered:
        return RU_INTERNATIONAL
    if "\u0432\u043d\u0443\u0442\u0440\u0438\u0440\u043e\u0441\u0441\u0438\u0439" in lowered:
        return RU_DOMESTIC
    return None


def _clean_country(value: Any, status: Any) -> str | None:
    if isinstance(value, str):
        normalized = COUNTRY_ALIASES.get(value.strip().lower())
        if normalized:
            return normalized
    if status == RU_DOMESTIC:
        return RU_RUSSIA
    return value if value in {RU_RUSSIA, RU_BELARUS, RU_CHINA, RU_MONGOLIA} else None


def _clean_loading_type(value: Any, source_text: str) -> str | None:
    text = f"{value or ''} {source_text}".lower()
    if "\u0441\u0432\u0435\u0440\u0445\u0443" in text or "\u0441\u0431\u043e\u043a\u0443" in text:
        return RU_TOP_SIDE
    if "\u0441\u0437\u0430\u0434\u0438" in text or "\u0437\u0430\u0434" in text:
        return RU_REAR
    return value if value in {RU_REAR, RU_TOP_SIDE} else None


def _clean_weight(value: Any, source_text: str) -> float | None:
    ton_match = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:\u0442|\u0442\u043e\u043d\u043d)", source_text, flags=re.IGNORECASE)
    if ton_match:
        return float(ton_match.group(1).replace(",", ".")) * 1000
    return _optional_float(value)


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
