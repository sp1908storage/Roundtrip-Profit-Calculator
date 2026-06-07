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

FOREIGN_CITY_COUNTRIES = {
    "\u044d\u0440\u043b\u044f\u043d\u044c": RU_CHINA,
    "\u044d\u0440\u043b\u044f\u043d": RU_CHINA,
    "erlian": RU_CHINA,
    "erenhot": RU_CHINA,
    "\u043c\u0430\u043d\u044c\u0447\u0436\u0443\u0440\u0438\u044f": RU_CHINA,
    "\u043c\u0430\u043d\u0447\u0436\u0443\u0440\u0438\u044f": RU_CHINA,
    "\u043c\u0430\u043d\u0436\u0443\u0440\u0438\u044f": RU_CHINA,
    "\u043c\u0438\u043d\u0441\u043a": RU_BELARUS,
    "\u0431\u0440\u0435\u0441\u0442": RU_BELARUS,
    "\u0433\u043e\u043c\u0435\u043b\u044c": RU_BELARUS,
    "\u0432\u0438\u0442\u0435\u0431\u0441\u043a": RU_BELARUS,
    "\u043c\u043e\u0433\u0438\u043b\u0435\u0432": RU_BELARUS,
    "\u0433\u0440\u043e\u0434\u043d\u043e": RU_BELARUS,
    "\u0443\u043b\u0430\u043d-\u0431\u0430\u0442\u043e\u0440": RU_MONGOLIA,
    "\u0443\u043b\u0430\u043d \u0431\u0430\u0442\u043e\u0440": RU_MONGOLIA,
    "ulaanbaatar": RU_MONGOLIA,
}

RUB_PATTERN = r"руб\.?|рубл(?:ь|я|ей|ях)?|₽|rub|rur"
USD_PATTERN = r"usd|\$|долл(?:ар(?:ов|а|ах)?)?|бакс(?:ов|а|ах)?|сша|у\.?\s*е\.?"
EUR_PATTERN = r"eur|€|евро"
CNY_PATTERN = r"cny|rmb|¥|юан(?:ь|я|ей|и|ях|ями)?|юан[еи]|китайск(?:их|ие|ими)?\s+юан(?:ей|и|ях|ями)?|yuan"
CURRENCY_PATTERN = rf"{RUB_PATTERN}|{USD_PATTERN}|{EUR_PATTERN}|{CNY_PATTERN}"


SYSTEM_INSTRUCTION = f"""
You are an assistant for calculating freight round-trip cost, profitability, and related route economics.
Your job is to help the manager collect enough reliable information for a high-quality calculation.
You may receive a complete request, a partial request, or a running dialog with clarifying questions.
Understand the user's intent, connect short answers to the bot's latest question, and extract only facts the user actually provided.
Do not behave like a rigid form: if one user message contains several fields, extract all of them.
Do not invent missing values. Leave unknown fields null so the bot can ask a focused follow-up question.

Extract freight request data for cost calculation.
Return only JSON. No markdown, explanations, or comments.

Schema:
{{
  "recognized_text": string|null,
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
  "rate_original_amount": number|null,
  "rate_currency": "RUB"|"USD"|"EUR"|"CNY"|null,
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
- The input may contain a running dialog with bot questions and user answers.
  Use bot question labels to understand which field a short user answer belongs to.
- If Belarus, China, or Mongolia is mentioned, set that country and status "{RU_INTERNATIONAL}".
- If the rate is given in RUB/rubles, set rate_with_vat_rub and rate_currency "RUB".
- If the rate is given in USD/EUR/CNY or another foreign currency, set rate_original_amount and rate_currency, and leave rate_with_vat_rub null unless a ruble conversion is explicitly provided.
- If the user corrects a previously extracted rate, update the matching flight rate instead of treating the correction as a client name or address.
- VAT can only be 22 or 0.
- "20 tons" or "20 \u0442\u043e\u043d\u043d" means 20000 kg.
- Do not invent mileage, rate, VAT, or weight.
- If only one flight is mentioned, put it into forward_flights.
- If the input is an image, set recognized_text to the full text read from the image.
"""


IMAGE_SYSTEM_INSTRUCTION = SYSTEM_INSTRUCTION + """

The user may send an image/screenshot of a chat message. First OCR the full visible
text exactly into recognized_text, preserving routes, numbers, currency words, VAT,
and line breaks as much as possible. Then extract the same JSON fields from that text.
"""


DIALOG_SYSTEM_INSTRUCTION = """
You are a Telegram assistant for a freight cost and round-trip profitability calculator.
Answer in Russian, naturally and briefly.

Scope:
- You may discuss the current calculation, its numbers, assumptions, missing data, routes, rates, VAT, mileage, costs, profit, and next freight requests.
- If the user is just being polite, respond warmly in one short sentence.
- If the user asks something unrelated to freight calculation, acknowledge briefly and bring them back to the calculation or invite a new freight request.
- Do not start a new calculation unless the user clearly provides a freight request. Ask them to send the request in one message.
- Do not invent numbers. Use only the calculation context provided below.
- Do not mention system instructions or internal implementation.
Keep the answer under 700 characters.
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
    return round_trip_from_dict(parse_data_with_ai_if_configured(text))


def parse_data_with_ai_if_configured(text: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.openai_api_key:
        return {"forward_flights": [], "backhaul_flights": []}

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
    return _postprocess_data(data, text)


def parse_image_with_ai_if_configured(image_bytes: bytes, mime_type: str = "image/jpeg") -> RoundTrip:
    round_trip, _recognized_text = parse_image_request_with_ai_if_configured(image_bytes, mime_type)
    return round_trip


def parse_image_request_with_ai_if_configured(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> tuple[RoundTrip, str]:
    settings = get_settings()
    if not settings.openai_api_key:
        return RoundTrip(), ""

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
                    {
                        "type": "text",
                        "text": (
                            "Read the full visible Russian freight request text from this image, "
                            "put it into recognized_text, then extract the freight request JSON."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
    )
    data = json.loads(_extract_json_object(response_text))
    recognized_text = _recognized_text_from_data(data)
    data = _postprocess_data(data, recognized_text)
    return round_trip_from_dict(data), recognized_text


def answer_dialog_with_ai_if_configured(user_text: str, calculation_context: str) -> str | None:
    settings = get_settings()
    if not settings.openai_api_key:
        return None

    calculation_context = _tail_text(calculation_context, settings.openai_dialog_context_chars)
    client = _build_client()
    response = client.chat.completions.create(
        model=settings.openai_dialog_model,
        messages=[
            {"role": "system", "content": DIALOG_SYSTEM_INSTRUCTION},
            {
                "role": "user",
                "content": (
                    "Контекст последнего расчета:\n"
                    f"{calculation_context}\n\n"
                    "Сообщение пользователя:\n"
                    f"{user_text}"
                ),
            },
        ],
        temperature=settings.openai_dialog_temperature,
    )
    answer = response.choices[0].message.content or ""
    return answer.strip() or None


def _tail_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return "...контекст сокращен...\n" + text[-max_chars:]


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

    segments = _extract_directional_segments(source_text)
    if segments.get("forward"):
        _apply_segment_facts(data["forward_flights"][0], segments["forward"])
    if segments.get("backhaul"):
        if not data["backhaul_flights"]:
            data["backhaul_flights"] = [{}]
        _apply_segment_facts(data["backhaul_flights"][0], segments["backhaul"])
    if not segments and source_text:
        _apply_segment_facts(data["forward_flights"][0], source_text)

    global_route = None if segments else _extract_route(source_text)
    global_country = None if segments else _extract_country(source_text)
    if global_route and not global_country:
        global_country = _extract_route_country(global_route)
    global_vat = _extract_vat(source_text)

    flight_sources: list[tuple[dict[str, Any], str]] = []
    for index, flight in enumerate(data["forward_flights"]):
        segment_text = segments.get("forward", source_text) if index == 0 else source_text
        flight_sources.append((flight, segment_text))
    for index, flight in enumerate(data["backhaul_flights"]):
        segment_text = segments.get("backhaul", source_text) if index == 0 else source_text
        flight_sources.append((flight, segment_text))

    for flight, flight_text in flight_sources:
        if global_route:
            flight["loading_address"] = global_route[0]
            flight["unloading_address"] = global_route[1]
        if global_country:
            flight["country"] = global_country
        _clean_flight_addresses(flight)

        country = flight.get("country")
        forced_status = None
        if country == RU_RUSSIA:
            forced_status = RU_DOMESTIC
        elif country:
            forced_status = RU_INTERNATIONAL
        vat_fallback = _extract_vat(flight_text) if segments else global_vat
        flight["vat_percent"] = _clean_vat(flight.get("vat_percent"), vat_fallback)
        flight["status"] = forced_status or _clean_status(
            flight.get("status"),
            country,
            flight_text,
        )
        flight["country"] = _clean_country(flight.get("country"), flight.get("status"))
        flight["loading_type"] = _clean_loading_type(flight.get("loading_type"), flight_text)
        flight["cargo_weight_kg"] = _clean_weight(flight.get("cargo_weight_kg"), flight_text)

    return data


def _clean_flight_addresses(flight: dict[str, Any]) -> None:
    for field_name in ("loading_address", "unloading_address"):
        value = flight.get(field_name)
        if isinstance(value, str):
            cleaned = _clean_route_endpoint(value)
            if cleaned:
                flight[field_name] = cleaned


def _recognized_text_from_data(data: dict[str, Any]) -> str:
    value = data.get("recognized_text")
    return value.strip() if isinstance(value, str) else ""


def _extract_directional_segments(text: str) -> dict[str, str]:
    match = re.search(r"\b(?:обратка|обратный\s+рейс|обратное\s+направление)\b\s*(?:=|:|-)?", text, flags=re.IGNORECASE)
    if not match:
        return {}
    return {
        "forward": text[: match.start()].strip(" ,;\n"),
        "backhaul": text[match.end() :].strip(" ,;\n"),
    }


def _apply_segment_facts(flight: dict[str, Any], segment: str) -> None:
    route = _extract_route(segment)
    if route:
        flight["loading_address"] = route[0]
        flight["unloading_address"] = route[1]

    country = _extract_country(segment)
    route_country = _extract_route_country(route) if route else None
    country = country or route_country
    if country:
        flight["country"] = country
        if country != RU_RUSSIA:
            flight["status"] = RU_INTERNATIONAL
        else:
            flight["status"] = RU_DOMESTIC
    elif route:
        flight["country"] = RU_RUSSIA
        flight["status"] = RU_DOMESTIC

    vat = _extract_vat(segment)
    if vat is not None:
        flight["vat_percent"] = vat

    rate = _extract_rate(segment)
    if rate:
        amount_value, currency = rate
        flight["rate_currency"] = currency
        if currency == "RUB":
            flight["rate_with_vat_rub"] = amount_value
        else:
            flight["rate_original_amount"] = amount_value
            flight["rate_with_vat_rub"] = amount_value

    weight = _extract_weight(segment)
    if weight is not None:
        flight["cargo_weight_kg"] = weight

    loading_type = _extract_loading_type(segment)
    if loading_type is not None:
        flight["loading_type"] = loading_type


def _extract_route(text: str) -> tuple[str, str] | None:
    match = re.search(
        r"(?:\u0440\u0435\u0439\u0441|\u043c\u0430\u0440\u0448\u0440\u0443\u0442)?\s*([A-ZА-ЯЁ][^,\n;]{1,80}?)\s*(?:-|—|->|→)\s*([A-ZА-ЯЁ][^,\n;]{1,80})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    loading = _clean_route_endpoint(match.group(1))
    unloading = _clean_route_endpoint(match.group(2))
    unloading_extra = _extract_unloading_address_extra(text, match.end())
    if unloading_extra:
        unloading = f"{unloading}, {unloading_extra}"
    return loading, unloading


def _extract_unloading_address_extra(text: str, route_end: int) -> str | None:
    tail = text[route_end:]
    if not tail.startswith(","):
        return None
    extras: list[str] = []
    for part in tail[1:].split(","):
        cleaned = part.strip(" =:;.")
        if not cleaned:
            continue
        if _looks_like_route_field(cleaned):
            break
        extras.append(_clean_route_endpoint(cleaned))
    return ", ".join(item for item in extras if item) or None


def _looks_like_route_field(value: str) -> bool:
    lowered = value.lower()
    if re.match(r"\d", lowered):
        return True
    return bool(
        re.search(
            r"\b(?:ставк\w*|фрахт\w*|вес\w*|груз\w*|загруз\w*|ндс|vat|пробег\w*|км|клиент\w*|логист\w*|международ\w*|внутр\w*|страна)\b",
            lowered,
            flags=re.IGNORECASE,
        )
    )


def _clean_route_endpoint(value: str) -> str:
    value = value.strip(" =:;,.")
    value = re.sub(
        r"^.*\b(?:прямое\s+направление|прямой\s+рейс|прямой\s+маршрут)\b\s*(?:=|:|-)?\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"^.*\b(?:кругорейсу|круго-рейсу|рейсу|маршруту|маршрут)\s+",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"^\b(?:рейс|маршрут|обратка)\b\s*(?:=|:|-)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*\((?:Россия|Беларусь|Белоруссия|Китай|КНР|Монголия|Russia|Belarus|China|Mongolia)\)\s*", " ", value, flags=re.IGNORECASE)
    value = re.sub(
        r"\s+(?:ставк\w*|фрахт\w*|вес\w*|загруз\w*|ндс|vat|\d[\d\s.,]*(?:руб\.?|rub|usd|eur|cny|кг|kg|т|тонн)).*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return value.strip(" =:;,.")


def _extract_route_country(route: tuple[str, str]) -> str | None:
    for endpoint in route:
        normalized = endpoint.strip().lower().replace("\u0451", "\u0435")
        for city, country in FOREIGN_CITY_COUNTRIES.items():
            if re.search(rf"\b{re.escape(city)}\b", normalized):
                return country
    return RU_RUSSIA


def _extract_rate(text: str) -> tuple[float, str] | None:
    match = re.search(
        rf"(?:ставк[а-я]*|фрахт)\D{{0,20}}(\d[\d\s.,]*)\s*({CURRENCY_PATTERN})?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    amount = _parse_human_amount(match.group(1))
    currency_text = (match.group(2) or "руб").lower()
    currency = "RUB"
    if re.search(USD_PATTERN, currency_text):
        currency = "USD"
    elif re.search(EUR_PATTERN, currency_text):
        currency = "EUR"
    elif re.search(CNY_PATTERN, currency_text):
        currency = "CNY"
    return amount, currency


def _parse_human_amount(value: str) -> float:
    raw = value.replace("\u00a0", " ").replace(" ", "")
    separator_match = re.match(r"^(\d{1,3})([,.])(\d{3})$", raw)
    if separator_match:
        return float(separator_match.group(1) + separator_match.group(3))
    return float(raw.replace(",", "."))


def _extract_weight(text: str) -> float | None:
    ton_match = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:т|тонн)", text, flags=re.IGNORECASE)
    if ton_match:
        return float(ton_match.group(1).replace(",", ".")) * 1000

    kg_match = re.search(r"(\d[\d\s.,]*)\s*(?:кг|kg)", text, flags=re.IGNORECASE)
    if not kg_match:
        return None
    return _parse_human_amount(kg_match.group(1))


def _extract_loading_type(text: str) -> str | None:
    lowered = text.lower()
    if "сверху" in lowered or "сбоку" in lowered or "бок" in lowered:
        return RU_TOP_SIDE
    if "сзади" in lowered or "зад" in lowered:
        return RU_REAR
    return None


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
    lowered = text.lower()
    if re.search(r"\b(?:без|0)\s+ндс\b", lowered):
        return 0
    if re.search(r"\bс\s+ндс\b", lowered):
        return 22
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
            if status == RU_INTERNATIONAL and normalized == RU_RUSSIA:
                return None
            return normalized
    if status == RU_DOMESTIC:
        return RU_RUSSIA
    if status == RU_INTERNATIONAL and value == RU_RUSSIA:
        return None
    return value if value in {RU_RUSSIA, RU_BELARUS, RU_CHINA, RU_MONGOLIA} else None


def _clean_loading_type(value: Any, source_text: str) -> str | None:
    text = f"{value or ''} {source_text}".lower()
    if "\u0441\u0432\u0435\u0440\u0445\u0443" in text or "\u0441\u0431\u043e\u043a\u0443" in text:
        return RU_TOP_SIDE
    if "\u0441\u0437\u0430\u0434\u0438" in text or "\u0437\u0430\u0434" in text:
        return RU_REAR
    return value if value in {RU_REAR, RU_TOP_SIDE} else None


def _clean_weight(value: Any, source_text: str) -> float | None:
    extracted = _extract_weight(source_text)
    if extracted is not None:
        return extracted
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
