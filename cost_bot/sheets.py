from .calculator import RoundTripCost
from .models import RoundTrip
from .settings import get_settings, require_existing_file


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


def append_result(round_trip: RoundTrip, result: RoundTripCost) -> None:
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured.")

    service = _build_sheets_service(settings.google_application_credentials)
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


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.google_sheets_spreadsheet_id and settings.google_application_credentials)


def _build_sheets_service(credentials_path: str | None):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials_file = require_existing_file(credentials_path, "GOOGLE_APPLICATION_CREDENTIALS")
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
