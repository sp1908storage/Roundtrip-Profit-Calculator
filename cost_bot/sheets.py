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
    prefix_tokens = _normalize_header(prefix).split()
    for index, header in enumerate(headers):
        normalized = _normalize_header(header)
        if all(token in normalized for token in prefix_tokens) and all(token in normalized for token in tokens):
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


def _extension_from_mime_type(mime_type: str) -> str:
    if mime_type == "image/png":
        return ".png"
    if mime_type == "image/webp":
        return ".webp"
    return ".jpg"
