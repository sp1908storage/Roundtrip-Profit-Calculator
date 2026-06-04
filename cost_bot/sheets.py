from datetime import datetime, timedelta, timezone

from .calculator import RoundTripCost
from .models import Flight, RoundTrip
from .settings import get_settings, resolve_google_credentials_file


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

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
]

REQUESTS_WORKSHEET_NAME = "Запросы"


def append_result(round_trip: RoundTrip, result: RoundTripCost) -> None:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured.")

    service = _build_sheets_service()
    worksheet = settings.google_sheets_worksheet_name
    _ensure_worksheet(service, settings.google_sheets_spreadsheet_id, worksheet)
    _ensure_headers(service, settings.google_sheets_spreadsheet_id, worksheet)

    round_trip_id = _make_round_trip_id()
    rows = [
        _flight_cost_row(round_trip_id, index, item, result)
        for index, item in enumerate(result.flights, 1)
    ]
    service.spreadsheets().values().append(
        spreadsheetId=settings.google_sheets_spreadsheet_id,
        range=_range(worksheet, "A1"),
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
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
    _set_exact(headers, row, "ID изображения", image_file_id or "")
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
        return

    service.spreadsheets().values().append(
        spreadsheetId=settings.google_sheets_spreadsheet_id,
        range=_range(REQUESTS_WORKSHEET_NAME, "A1"),
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


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
    _set_by_tokens(headers, row, prefix, ["загрузка"], flight.loading_type.value)


def _set_exact(headers: list[str], row: list, header: str, value) -> None:
    try:
        index = headers.index(header)
    except ValueError:
        return
    row[index] = _cell_value(value)


def _set_by_tokens(headers: list[str], row: list, prefix: str, tokens: list[str], value) -> None:
    prefix_normalized = _normalize_header(prefix)
    for index, header in enumerate(headers):
        normalized = _normalize_header(header)
        if normalized.startswith(prefix_normalized) and all(token in normalized for token in tokens):
            row[index] = _cell_value(value)
            return


def _cell_value(value):
    if value is None:
        return ""
    return value


def _normalize_header(value: str) -> str:
    return " ".join(value.lower().replace("ё", "е").split())


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
        range=_range(worksheet, "A1:AA1"),
    ).execute()
    values = response.get("values", [])
    if values and values[0] == HEADERS:
        return
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=_range(worksheet, "A1:AA1"),
        valueInputOption="USER_ENTERED",
        body={"values": [HEADERS]},
    ).execute()


def _flight_cost_row(round_trip_id: str, index: int, item, result: RoundTripCost) -> list:
    flight = item.flight
    return [
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
    ]


def _range(worksheet: str, cell_range: str) -> str:
    safe_name = worksheet.replace("'", "''")
    return f"'{safe_name}'!{cell_range}"


def _make_round_trip_id() -> str:
    from datetime import datetime, timezone
    from uuid import uuid4

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"rt-{timestamp}-{uuid4().hex[:8]}"
