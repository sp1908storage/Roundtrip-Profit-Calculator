import re
from datetime import datetime, timedelta, timezone
from io import BytesIO

from .calculator import RoundTripCost
from .models import Flight, RoundTrip
from .settings import get_settings, resolve_google_credentials_file


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "round_trip_id",
    "flight_no",
    "direction",
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
    "revenue_rub",
    "fuel_rub",
    "maintenance_rub",
    "driver_and_fixed_rub",
    "tolls_rub",
    "loading_extra_rub",
    "international_extra_rub",
    "total_cost_rub",
    "profit_rub",
    "total_revenue_rub",
    "total_cost_rub",
    "total_profit_rub",
    "Текст Ответа",
]

REQUESTS_WORKSHEET_NAME = "Запросы"
SUMMARY_WORKSHEET_NAME = "Расчеты Итог"

SUMMARY_HEADERS = [
    "ID запроса",
    "Дата и время",
    "Источник",
    "Пользователь",
    "round_trip_id",
    "Дата расчета",
    "Прямых рейсов",
    "Обратных рейсов",
    "Всего рейсов",
    "Есть обратная загрузка",
    "Маршрут",
    "Клиенты",
    "Страны",
    "Международных рейсов",
    "Общий пробег до загрузки, км",
    "Общий пробег до выгрузки, км",
    "Пробег по РФ, км",
    "Общий пробег, км",
    "Максимальный вес, кг",
    "Общая выручка, руб",
    "Топливо, руб",
    "Обслуживание, руб",
    "Водитель и постоянные расходы, руб",
    "Платные дороги, руб",
    "Доплата за тип загрузки, руб",
    "Международные расходы, руб",
    "Общая себестоимость, руб",
    "Расчетная прибыль, руб",
    "Рентабельность, %",
    "Текст Ответа",
]


def append_result(
    round_trip: RoundTrip,
    result: RoundTripCost,
    response_text: str | None = None,
    request_id: str | None = None,
    request_source: str | None = None,
    request_user: str | None = None,
    request_datetime: str | None = None,
) -> None:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured.")

    service = _build_sheets_service()
    worksheet = settings.google_sheets_worksheet_name
    _ensure_worksheet(service, settings.google_sheets_spreadsheet_id, worksheet)
    headers = _ensure_result_headers(service, settings.google_sheets_spreadsheet_id, worksheet)

    round_trip_id = _make_round_trip_id()
    rows = []
    direction_numbers: dict[str, int] = {}
    for index, item in enumerate(result.flights, 1):
        direction = item.flight.direction.value
        direction_numbers[direction] = direction_numbers.get(direction, 0) + 1
        rows.append(
            _flight_cost_row(
                round_trip_id,
                index,
                item,
                result,
                headers,
                response_text,
                direction_numbers[direction],
            )
        )
    service.spreadsheets().values().append(
        spreadsheetId=settings.google_sheets_spreadsheet_id,
        range=_range(worksheet, "A1"),
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    _append_result_summary(
        service,
        settings.google_sheets_spreadsheet_id,
        round_trip_id,
        round_trip,
        result,
        response_text,
        request_id,
        request_source,
        request_user,
        request_datetime,
    )


def _append_result_summary(
    service,
    spreadsheet_id: str,
    round_trip_id: str,
    round_trip: RoundTrip,
    result: RoundTripCost,
    response_text: str | None,
    request_id: str | None,
    request_source: str | None,
    request_user: str | None,
    request_datetime: str | None,
) -> None:
    _ensure_worksheet(service, spreadsheet_id, SUMMARY_WORKSHEET_NAME)
    headers = _ensure_summary_headers(service, spreadsheet_id)
    request_metadata = _read_request_metadata(
        service,
        spreadsheet_id,
        request_id=request_id,
        fallback_source=request_source,
        fallback_user=request_user,
        fallback_datetime=request_datetime,
    )
    row = _summary_result_row(
        round_trip_id,
        round_trip,
        result,
        headers,
        response_text,
        request_metadata,
    )
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=_range(SUMMARY_WORKSHEET_NAME, "A1"),
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def append_request_log(
    *,
    request_id: str,
    source: str,
    user: str,
    message_type: str,
    raw_text: str,
    image_file_id: str | None,
    ai_model: str,
    ai_status: str,
    calculation_status: str,
    error_comment: str,
    round_trip: RoundTrip,
    image_cell_value: str | None = None,
) -> None:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured.")

    service = _build_sheets_service()
    _ensure_worksheet(service, settings.google_sheets_spreadsheet_id, REQUESTS_WORKSHEET_NAME)
    headers = _read_headers(
        service,
        settings.google_sheets_spreadsheet_id,
        REQUESTS_WORKSHEET_NAME,
    )
    if not headers:
        raise RuntimeError("Лист Запросы должен содержать заголовки в первой строке.")

    row = [""] * len(headers)
    _set_exact(headers, row, "ID запроса", request_id)
    _set_exact(headers, row, "Дата и время", _now_moscow_like())
    _set_exact(headers, row, "Источник", source)
    _set_exact(headers, row, "Пользователь", user)
    _set_exact(headers, row, "Тип сообщения", message_type)
    _set_exact(headers, row, "Исходный текст", raw_text)
    _set_exact(headers, row, "ID изображения", image_cell_value or image_file_id or "")
    _set_exact(headers, row, "Модель AI", ai_model)
    _set_exact(headers, row, "Статус AI", ai_status)
    _set_exact(headers, row, "Статус расчета", calculation_status)
    _set_exact(headers, row, "Комментарий ошибки", error_comment)

    for index, flight in enumerate(round_trip.forward_flights[:3], 1):
        _fill_request_flight(headers, row, f"Прямой {index}", flight)
    for index, flight in enumerate(round_trip.backhaul_flights[:3], 1):
        _fill_request_flight(headers, row, f"Обратный {index}", flight)

    existing_row = _find_row_by_first_column(
        service,
        settings.google_sheets_spreadsheet_id,
        REQUESTS_WORKSHEET_NAME,
        request_id,
    )
    if existing_row:
        end_column = _column_name(len(headers))
        service.spreadsheets().values().update(
            spreadsheetId=settings.google_sheets_spreadsheet_id,
            range=_range(REQUESTS_WORKSHEET_NAME, f"A{existing_row}:{end_column}{existing_row}"),
            valueInputOption="USER_ENTERED",
            body={"values": [row]},
        ).execute()
        _format_request_image_cell(
            service,
            settings.google_sheets_spreadsheet_id,
            REQUESTS_WORKSHEET_NAME,
            existing_row,
            headers,
            image_cell_value,
        )
        return

    response = service.spreadsheets().values().append(
        spreadsheetId=settings.google_sheets_spreadsheet_id,
        range=_range(REQUESTS_WORKSHEET_NAME, "A1"),
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    inserted_row = _row_number_from_updated_range(
        response.get("updates", {}).get("updatedRange", "")
    ) or _find_row_by_first_column(
        service,
        settings.google_sheets_spreadsheet_id,
        REQUESTS_WORKSHEET_NAME,
        request_id,
    )
    if inserted_row:
        _format_request_image_cell(
            service,
            settings.google_sheets_spreadsheet_id,
            REQUESTS_WORKSHEET_NAME,
            inserted_row,
            headers,
            image_cell_value,
        )


def upload_request_image_to_drive(
    *,
    image_bytes: bytes,
    mime_type: str,
    request_id: str,
) -> str | None:
    settings = get_settings()
    if not settings.google_drive_images_folder_id:
        return None

    drive_service = _build_drive_service()
    extension = _extension_from_mime_type(mime_type)
    metadata = {
        "name": f"{request_id}{extension}",
        "mimeType": mime_type,
        "parents": [settings.google_drive_images_folder_id],
    }

    from googleapiclient.http import MediaIoBaseUpload

    media = MediaIoBaseUpload(BytesIO(image_bytes), mimetype=mime_type, resumable=False)
    created_file = drive_service.files().create(
        body=metadata,
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    file_id = created_file["id"]

    drive_service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
        fields="id",
        supportsAllDrives=True,
    ).execute()

    image_url = f"https://drive.google.com/thumbnail?id={file_id}&sz=w600"
    return f'=IMAGE("{image_url}")'


def is_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.google_sheets_spreadsheet_id
        and (
            settings.google_application_credentials
            or settings.google_application_credentials_json
        )
    )


def _read_headers(service, spreadsheet_id: str, worksheet: str) -> list[str]:
    response = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=_range(worksheet, "1:1"),
    ).execute()
    values = response.get("values", [])
    return values[0] if values else []


def _find_row_by_first_column(service, spreadsheet_id: str, worksheet: str, value: str) -> int | None:
    response = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=_range(worksheet, "A:A"),
    ).execute()
    for index, row in enumerate(response.get("values", []), 1):
        if row and row[0] == value:
            return index
    return None


def _read_request_metadata(
    service,
    spreadsheet_id: str,
    *,
    request_id: str | None,
    fallback_source: str | None = None,
    fallback_user: str | None = None,
    fallback_datetime: str | None = None,
) -> dict[str, str]:
    metadata = {
        "request_id": request_id or "",
        "request_datetime": fallback_datetime or _now_moscow_like(),
        "request_source": fallback_source or "",
        "request_user": fallback_user or "",
    }
    if not request_id:
        return metadata

    try:
        response = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=_range(REQUESTS_WORKSHEET_NAME, "A:ZZ"),
        ).execute()
    except Exception:
        return metadata

    values = response.get("values", [])
    if not values:
        return metadata
    headers = values[0]
    id_index = _find_header_index(headers, ["id", "запрос"])
    if id_index is None:
        id_index = 0

    for row in values[1:]:
        if len(row) <= id_index or row[id_index] != request_id:
            continue
        metadata["request_datetime"] = _row_value_by_header(
            headers,
            row,
            exact="Дата и время",
            tokens=["дата", "время"],
            fallback=metadata["request_datetime"],
        )
        metadata["request_source"] = _row_value_by_header(
            headers,
            row,
            exact="Источник",
            tokens=["источник"],
            fallback=metadata["request_source"],
        )
        metadata["request_user"] = _row_value_by_header(
            headers,
            row,
            exact="Пользователь",
            tokens=["пользователь"],
            fallback=metadata["request_user"],
        )
        return metadata
    return metadata


def _find_header_index(headers: list[str], tokens: list[str]) -> int | None:
    for index, header in enumerate(headers):
        normalized = _normalize_header(header)
        if all(token in normalized for token in tokens):
            return index
    return None


def _row_value_by_header(
    headers: list[str],
    row: list,
    *,
    exact: str,
    tokens: list[str],
    fallback: str,
) -> str:
    try:
        index = headers.index(exact)
    except ValueError:
        index = _find_header_index(headers, tokens)
    if index is None or len(row) <= index:
        return fallback
    return str(row[index])


def _fill_request_flight(headers: list[str], row: list, prefix: str, flight: Flight) -> None:
    _set_by_tokens(headers, row, prefix, ["клиент"], flight.client_short or "")
    _set_by_tokens(headers, row, prefix, ["адрес", "загруз"], flight.loading_address or "")
    _set_by_tokens(headers, row, prefix, ["пробег", "загруз"], flight.distance_to_loading_km)
    _set_by_tokens(headers, row, prefix, ["адрес", "выгруз"], flight.unloading_address or "")
    _set_by_tokens(headers, row, prefix, ["ставка", "руб"], flight.rate_with_vat_rub)
    _set_by_tokens(
        headers,
        row,
        prefix,
        ["статус", "перевоз"],
        flight.status.value if flight.status else "",
    )
    _set_by_tokens(headers, row, prefix, ["зарубежная", "страна"], flight.country or "")
    _set_by_tokens(headers, row, prefix, ["ндс", "0%"], flight.vat_percent)
    _set_by_tokens(headers, row, prefix, ["пробег", "выгруз"], flight.distance_to_unloading_km)
    _set_by_tokens(headers, row, prefix, ["пробег", "рф"], flight.russian_territory_km)
    _set_by_tokens(headers, row, prefix, ["вес"], flight.cargo_weight_kg)
    _set_loading_type(headers, row, prefix, flight.loading_type.value)


def _set_exact(headers: list[str], row: list, header: str, value) -> None:
    try:
        index = headers.index(header)
    except ValueError:
        return
    row[index] = _cell_value(value)


def _set_by_tokens(headers: list[str], row: list, prefix: str, tokens: list[str], value) -> None:
    prefix_tokens = _normalize_header(prefix).split()
    for index, header in enumerate(headers):
        normalized = _normalize_header(header)
        if all(token in normalized for token in prefix_tokens) and all(token in normalized for token in tokens):
            row[index] = _cell_value(value)
            return


def _set_generic_by_tokens(headers: list[str], row: list, tokens: list[str], value) -> None:
    for index, header in enumerate(headers):
        normalized = _normalize_header(header)
        if any(prefix in normalized for prefix in ("прям", "обратн")):
            continue
        if all(token in normalized for token in tokens):
            row[index] = _cell_value(value)
            return


def _set_summary_by_tokens(headers: list[str], row: list, tokens: list[str], value) -> None:
    for index, header in enumerate(headers):
        normalized = _normalize_header(header)
        if "прямой рейс" in normalized or "обратный рейс" in normalized:
            continue
        if all(token in normalized for token in tokens):
            row[index] = _cell_value(value)
            return


def _set_loading_type(headers: list[str], row: list, prefix: str, value) -> None:
    prefix_tokens = _normalize_header(prefix).split()
    for index, header in enumerate(headers):
        normalized = _normalize_header(header)
        if prefix_tokens and not all(token in normalized for token in prefix_tokens):
            continue
        if not prefix_tokens and any(prefix in normalized for prefix in ("прям", "обратн")):
            continue
        if "loading_type" in normalized or (
            "загруз" in normalized
            and not any(blocked in normalized for blocked in ("адрес", "пробег", "мест"))
        ):
            row[index] = _cell_value(value)
            return


def _cell_value(value):
    if value is None:
        return ""
    return value


def _normalize_header(value: str) -> str:
    return " ".join(value.lower().replace("ё", "е").split())


def _join_unique(values) -> str:
    result = []
    seen = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        normalized = _normalize_header(text)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(text)
    return ", ".join(result)


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _now_moscow_like() -> str:
    moscow_tz = timezone(timedelta(hours=3))
    return datetime.now(moscow_tz).strftime("%Y-%m-%d %H:%M:%S")


def _build_sheets_service(credentials_path: str | None = None):
    del credentials_path
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials_file = resolve_google_credentials_file()
    credentials = service_account.Credentials.from_service_account_file(
        str(credentials_file),
        scopes=SCOPES,
    )
    return build("sheets", "v4", credentials=credentials)


def _build_drive_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials_file = resolve_google_credentials_file()
    credentials = service_account.Credentials.from_service_account_file(
        str(credentials_file),
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=credentials)


def _format_request_image_cell(
    service,
    spreadsheet_id: str,
    worksheet: str,
    row_number: int,
    headers: list[str],
    image_cell_value: str | None,
) -> None:
    if not image_cell_value or not str(image_cell_value).startswith("=IMAGE("):
        return
    try:
        image_column_index = headers.index("ID изображения")
    except ValueError:
        return

    sheet_id = _worksheet_id(service, spreadsheet_id, worksheet)
    if sheet_id is None:
        return

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row_number - 1,
                            "endIndex": row_number,
                        },
                        "properties": {"pixelSize": 140},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": image_column_index,
                            "endIndex": image_column_index + 1,
                        },
                        "properties": {"pixelSize": 180},
                        "fields": "pixelSize",
                    }
                },
            ]
        },
    ).execute()


def _worksheet_id(service, spreadsheet_id: str, worksheet: str) -> int | None:
    spreadsheet = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties(sheetId,title)",
    ).execute()
    for sheet in spreadsheet.get("sheets", []):
        properties = sheet["properties"]
        if properties["title"] == worksheet:
            return properties["sheetId"]
    return None


def _row_number_from_updated_range(updated_range: str) -> int | None:
    match = re.search(r"(?:!|^)[A-Z]+(\d+)", updated_range)
    if match:
        return int(match.group(1))
    return None


def _ensure_worksheet(service, spreadsheet_id: str, worksheet: str) -> None:
    spreadsheet = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties.title",
    ).execute()
    existing_titles = {
        sheet["properties"]["title"]
        for sheet in spreadsheet.get("sheets", [])
    }
    if worksheet in existing_titles:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": worksheet,
                        }
                    }
                }
            ]
        },
    ).execute()


def _ensure_headers(service, spreadsheet_id: str, worksheet: str) -> None:
    response = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=_range(worksheet, f"A1:{_column_name(len(HEADERS))}1"),
    ).execute()
    values = response.get("values", [])
    if values and values[0] == HEADERS:
        return
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=_range(worksheet, f"A1:{_column_name(len(HEADERS))}1"),
        valueInputOption="USER_ENTERED",
        body={"values": [HEADERS]},
    ).execute()


def _ensure_result_headers(service, spreadsheet_id: str, worksheet: str) -> list[str]:
    response = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=_range(worksheet, "1:1"),
    ).execute()
    values = response.get("values", [])
    if not values:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=_range(worksheet, f"A1:{_column_name(len(HEADERS))}1"),
            valueInputOption="USER_ENTERED",
            body={"values": [HEADERS]},
        ).execute()
        return HEADERS.copy()

    headers = values[0]
    if "Текст Ответа" not in headers:
        headers = [*headers, "Текст Ответа"]
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=_range(worksheet, f"A1:{_column_name(len(headers))}1"),
            valueInputOption="USER_ENTERED",
            body={"values": [headers]},
        ).execute()
    return headers


def _ensure_summary_headers(service, spreadsheet_id: str) -> list[str]:
    response = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=_range(SUMMARY_WORKSHEET_NAME, "1:1"),
    ).execute()
    values = response.get("values", [])
    if not values:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=_range(SUMMARY_WORKSHEET_NAME, f"A1:{_column_name(len(SUMMARY_HEADERS))}1"),
            valueInputOption="USER_ENTERED",
            body={"values": [SUMMARY_HEADERS]},
        ).execute()
        return SUMMARY_HEADERS.copy()

    headers = values[0]
    if "Текст Ответа" not in headers:
        headers = [*headers, "Текст Ответа"]
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=_range(SUMMARY_WORKSHEET_NAME, f"A1:{_column_name(len(headers))}1"),
            valueInputOption="USER_ENTERED",
            body={"values": [headers]},
        ).execute()
    return headers


def _summary_result_row(
    round_trip_id: str,
    round_trip: RoundTrip,
    result: RoundTripCost,
    headers: list[str] | None = None,
    response_text: str | None = None,
    request_metadata: dict[str, str] | None = None,
) -> list:
    request_metadata = request_metadata or {}
    request_id = request_metadata.get("request_id", "")
    request_datetime = request_metadata.get("request_datetime", _now_moscow_like())
    request_source = request_metadata.get("request_source", "")
    request_user = request_metadata.get("request_user", "")
    calculation_datetime = _now_moscow_like()
    forward_count = len(round_trip.forward_flights)
    backhaul_count = len(round_trip.backhaul_flights)
    flights = [item.flight for item in result.flights]
    route = " | ".join(
        f"{flight.loading_address or '?'} -> {flight.unloading_address or '?'}"
        for flight in flights
    )
    clients = _join_unique(flight.client_short for flight in flights)
    countries = _join_unique(flight.country for flight in flights)
    international_count = sum(
        1
        for flight in flights
        if flight.status and "международ" in _normalize_header(flight.status.value)
    )
    distance_to_loading = sum(flight.distance_to_loading_km or 0 for flight in flights)
    distance_to_unloading = sum(flight.distance_to_unloading_km or 0 for flight in flights)
    russian_territory = sum(flight.russian_territory_km or 0 for flight in flights)
    max_weight = max((flight.cargo_weight_kg or 0 for flight in flights), default=0)
    fuel = sum(item.fuel_rub for item in result.flights)
    maintenance = sum(item.maintenance_rub for item in result.flights)
    driver_and_fixed = sum(item.driver_and_fixed_rub for item in result.flights)
    tolls = sum(item.tolls_rub for item in result.flights)
    loading_extra = sum(item.loading_extra_rub for item in result.flights)
    international_extra = sum(item.international_extra_rub for item in result.flights)
    margin_percent = (
        result.total_profit_rub / result.total_revenue_rub * 100
        if result.total_revenue_rub
        else ""
    )
    default_row = [
        request_id,
        request_datetime,
        request_source,
        request_user,
        round_trip_id,
        calculation_datetime,
        forward_count,
        backhaul_count,
        len(result.flights),
        "да" if backhaul_count else "нет",
        route,
        clients,
        countries,
        international_count,
        distance_to_loading,
        distance_to_unloading,
        russian_territory,
        distance_to_loading + distance_to_unloading,
        max_weight,
        result.total_revenue_rub,
        fuel,
        maintenance,
        driver_and_fixed,
        tolls,
        loading_extra,
        international_extra,
        result.total_cost_rub,
        result.total_profit_rub,
        margin_percent,
        response_text or "",
    ]
    if not headers:
        return default_row

    row = [""] * len(headers)
    summary_values = dict(zip(SUMMARY_HEADERS, default_row, strict=False))
    for header, value in summary_values.items():
        _set_exact(headers, row, header, value)

    _set_exact(headers, row, "ID запроса", request_id)
    _set_exact(headers, row, "Дата и время", request_datetime)
    _set_exact(headers, row, "Источник", request_source)
    _set_exact(headers, row, "Пользователь", request_user)
    _set_summary_by_tokens(headers, row, ["id", "запрос"], request_id)
    _set_summary_by_tokens(headers, row, ["дата", "время"], request_datetime)
    _set_summary_by_tokens(headers, row, ["источник"], request_source)
    _set_summary_by_tokens(headers, row, ["пользователь"], request_user)
    _set_summary_by_tokens(headers, row, ["id", "рейс"], round_trip_id)
    _set_exact(headers, row, "Дата расчета", calculation_datetime)
    _set_summary_by_tokens(headers, row, ["дата", "расчет"], calculation_datetime)
    _set_summary_by_tokens(headers, row, ["прям", "рейс"], forward_count)
    _set_summary_by_tokens(headers, row, ["обратн", "рейс"], backhaul_count)
    _set_summary_by_tokens(headers, row, ["всего", "рейс"], len(result.flights))
    _set_summary_by_tokens(headers, row, ["есть", "обрат"], "да" if backhaul_count else "нет")
    _set_summary_by_tokens(headers, row, ["маршрут"], route)
    _set_summary_by_tokens(headers, row, ["клиент"], clients)
    _set_summary_by_tokens(headers, row, ["стран"], countries)
    _set_summary_by_tokens(headers, row, ["международ", "рейс"], international_count)
    _set_summary_by_tokens(headers, row, ["пробег", "загруз"], distance_to_loading)
    _set_summary_by_tokens(headers, row, ["пробег", "выгруз"], distance_to_unloading)
    _set_summary_by_tokens(headers, row, ["пробег", "рф"], russian_territory)
    _set_summary_by_tokens(headers, row, ["общ", "пробег"], distance_to_loading + distance_to_unloading)
    _set_summary_by_tokens(headers, row, ["макс", "вес"], max_weight)
    _set_summary_by_tokens(headers, row, ["выруч"], result.total_revenue_rub)
    _set_summary_by_tokens(headers, row, ["топлив"], fuel)
    _set_summary_by_tokens(headers, row, ["обслуж"], maintenance)
    _set_summary_by_tokens(headers, row, ["водител"], driver_and_fixed)
    _set_summary_by_tokens(headers, row, ["платн"], tolls)
    _set_summary_by_tokens(headers, row, ["доплат", "загруз"], loading_extra)
    _set_summary_by_tokens(headers, row, ["международ", "расход"], international_extra)
    _set_summary_by_tokens(headers, row, ["себестоим"], result.total_cost_rub)
    _set_summary_by_tokens(headers, row, ["прибыл"], result.total_profit_rub)
    _set_summary_by_tokens(headers, row, ["рентаб"], margin_percent)
    _set_exact(headers, row, "Текст Ответа", response_text or "")
    _set_summary_by_tokens(headers, row, ["текст", "ответ"], response_text or "")

    for index, flight in enumerate(round_trip.forward_flights[:3], 1):
        _fill_request_flight(headers, row, f"Прямой {index}", flight)
    for index, flight in enumerate(round_trip.backhaul_flights[:3], 1):
        _fill_request_flight(headers, row, f"Обратный {index}", flight)
    return row


def _flight_cost_row(
    round_trip_id: str,
    index: int,
    item,
    result: RoundTripCost,
    headers: list[str] | None = None,
    response_text: str | None = None,
    direction_index: int | None = None,
) -> list:
    flight = item.flight
    default_row = [
        round_trip_id,
        index,
        flight.direction.value,
        flight.client_short or "",
        flight.loading_address or "",
        flight.distance_to_loading_km or 0,
        flight.unloading_address or "",
        flight.rate_with_vat_rub or 0,
        flight.status.value if flight.status else "",
        flight.country or "",
        flight.vat_percent if flight.vat_percent is not None else "",
        flight.distance_to_unloading_km or 0,
        flight.russian_territory_km if flight.russian_territory_km is not None else "",
        flight.cargo_weight_kg,
        flight.loading_type.value,
        item.revenue_rub,
        item.fuel_rub,
        item.maintenance_rub,
        item.driver_and_fixed_rub,
        item.tolls_rub,
        item.loading_extra_rub,
        item.international_extra_rub,
        item.total_cost_rub,
        item.profit_rub,
        result.total_revenue_rub,
        result.total_cost_rub,
        result.total_profit_rub,
        response_text if index == 1 else "",
    ]
    if not headers:
        return default_row

    row = [""] * len(headers)
    english_values = dict(zip(HEADERS, default_row, strict=False))
    for header, value in english_values.items():
        _set_exact(headers, row, header, value)

    direction_label = "Прямой" if flight.direction.value == "direct" else "Обратный"
    request_prefix = f"{direction_label} {direction_index or index}"
    _fill_request_flight(headers, row, request_prefix, flight)
    _set_generic_by_tokens(headers, row, ["id", "рейс"], round_trip_id)
    _set_generic_by_tokens(headers, row, ["номер", "рейс"], index)
    _set_generic_by_tokens(headers, row, ["направ"], flight.direction.value)
    _set_generic_by_tokens(headers, row, ["клиент"], flight.client_short or "")
    _set_generic_by_tokens(headers, row, ["адрес", "загруз"], flight.loading_address or "")
    _set_generic_by_tokens(headers, row, ["пробег", "загруз"], flight.distance_to_loading_km or 0)
    _set_generic_by_tokens(headers, row, ["адрес", "выгруз"], flight.unloading_address or "")
    _set_generic_by_tokens(headers, row, ["ставк"], flight.rate_with_vat_rub or 0)
    _set_generic_by_tokens(headers, row, ["статус"], flight.status.value if flight.status else "")
    _set_generic_by_tokens(headers, row, ["стран"], flight.country or "")
    _set_generic_by_tokens(headers, row, ["ндс"], flight.vat_percent if flight.vat_percent is not None else "")
    _set_generic_by_tokens(headers, row, ["пробег", "выгруз"], flight.distance_to_unloading_km or 0)
    _set_generic_by_tokens(headers, row, ["пробег", "рф"], flight.russian_territory_km)
    _set_generic_by_tokens(headers, row, ["вес"], flight.cargo_weight_kg)
    _set_loading_type(headers, row, "", flight.loading_type.value)
    _set_generic_by_tokens(headers, row, ["выруч"], item.revenue_rub)
    _set_generic_by_tokens(headers, row, ["топлив"], item.fuel_rub)
    _set_generic_by_tokens(headers, row, ["обслуж"], item.maintenance_rub)
    _set_generic_by_tokens(headers, row, ["водител"], item.driver_and_fixed_rub)
    _set_generic_by_tokens(headers, row, ["платн"], item.tolls_rub)
    _set_generic_by_tokens(headers, row, ["международ"], item.international_extra_rub)
    _set_generic_by_tokens(headers, row, ["себестоим"], item.total_cost_rub)
    _set_generic_by_tokens(headers, row, ["прибыл"], item.profit_rub)
    _set_generic_by_tokens(headers, row, ["общ", "выруч"], result.total_revenue_rub)
    _set_generic_by_tokens(headers, row, ["общ", "себестоим"], result.total_cost_rub)
    _set_generic_by_tokens(headers, row, ["общ", "прибыл"], result.total_profit_rub)
    _set_exact(headers, row, "Текст Ответа", response_text if index == 1 else "")
    _set_generic_by_tokens(headers, row, ["текст", "ответ"], response_text if index == 1 else "")

    if not any(token in _normalize_header(" ".join(headers)) for token in ("направ", "direction")):
        _set_generic_by_tokens(headers, row, ["рейс"], f"{direction_label} {index}")
    return row


def _range(worksheet: str, cell_range: str) -> str:
    safe_name = worksheet.replace("'", "''")
    return f"'{safe_name}'!{cell_range}"


def _make_round_trip_id() -> str:
    from datetime import datetime, timezone
    from uuid import uuid4

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"rt-{timestamp}-{uuid4().hex[:8]}"


def _extension_from_mime_type(mime_type: str) -> str:
    if mime_type == "image/png":
        return ".png"
    if mime_type == "image/webp":
        return ".webp"
    return ".jpg"
