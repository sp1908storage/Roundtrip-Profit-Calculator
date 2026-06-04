import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None
    openai_model: str
    telegram_bot_token: str | None
    telegram_allowed_chat_ids: set[int]
    google_sheets_spreadsheet_id: str | None
    google_sheets_worksheet_name: str
    google_application_credentials: str | None
    google_application_credentials_json: str | None


def get_settings() -> Settings:
    return Settings(
        openai_api_key=_optional_env("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5"),
        telegram_bot_token=_optional_env("TELEGRAM_BOT_TOKEN"),
        telegram_allowed_chat_ids=_parse_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")),
        google_sheets_spreadsheet_id=_normalize_spreadsheet_id(
            _optional_env("GOOGLE_SHEETS_SPREADSHEET_ID")
        ),
        google_sheets_worksheet_name=os.getenv("GOOGLE_SHEETS_WORKSHEET_NAME", "Расчеты"),
        google_application_credentials=_optional_env("GOOGLE_APPLICATION_CREDENTIALS"),
        google_application_credentials_json=_optional_env("GOOGLE_APPLICATION_CREDENTIALS_JSON"),
    )


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if not value:
        return None
    return value.strip().strip('"') or None


def _parse_chat_ids(value: str) -> set[int]:
    chat_ids: set[int] = set()
    for item in value.replace(";", ",").split(","):
        item = item.strip()
        if item:
            chat_ids.add(int(item))
    return chat_ids


def _normalize_spreadsheet_id(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    match = re.search(r"/spreadsheets/d/([^/]+)", value)
    if match:
        return match.group(1)
    return value.split("/edit", 1)[0].split("?", 1)[0].split("#", 1)[0]


def require_existing_file(path: str | None, label: str) -> Path:
    if not path:
        raise RuntimeError(f"{label} is not configured.")
    file_path = Path(path)
    if not file_path.exists():
        raise RuntimeError(f"{label} does not exist: {file_path}")
    return file_path


def resolve_google_credentials_file() -> Path:
    settings = get_settings()
    if settings.google_application_credentials_json:
        return _write_temp_credentials(settings.google_application_credentials_json)
    return require_existing_file(
        settings.google_application_credentials,
        "GOOGLE_APPLICATION_CREDENTIALS",
    )


def _write_temp_credentials(credentials_json: str) -> Path:
    credentials_json = credentials_json.strip()
    if not credentials_json.startswith("{"):
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS_JSON must contain raw JSON.")
    temp_dir = Path(tempfile.gettempdir()) / "roundtrip_profit_calculator"
    temp_dir.mkdir(parents=True, exist_ok=True)
    credentials_file = temp_dir / "google-service-account.json"
    credentials_file.write_text(credentials_json, encoding="utf-8")
    return credentials_file
