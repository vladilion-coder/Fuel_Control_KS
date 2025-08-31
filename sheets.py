import os
from datetime import datetime
from typing import Optional

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
import pytz

# ── .env ──────────────────────────────────────────────────────────────────────
load_dotenv()
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

# ── Google Sheets client ──────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

CREDS = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", SCOPES)
CLIENT = gspread.authorize(CREDS)

# ── Внутрішні хелпери ────────────────────────────────────────────────────────
def _open_ws(name: str):
    return CLIENT.open_by_key(SPREADSHEET_ID).worksheet(name)

def _to_float(x, default: float = 0.0) -> float:
    """Конвертує значення з таблиці у float, підтримує '123,45' і '123.45'."""
    if x is None:
        return float(default)
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if not s:
        return float(default)
    s = s.replace(" ", "").replace("\u00A0", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return float(default)

def _to_str_dot(v: float, ndigits: int = 2) -> str:
    """
    Перетворює число у рядок з крапкою як роздільником.
    Напр.: 735.4 -> "735.40"
    """
    return f"{float(v):.{ndigits}f}".replace(",", ".")

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def _now_kyiv_str() -> str:
    tz = pytz.timezone("Europe/Kyiv")
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

# ── Публічні утиліти для бота ────────────────────────────────────────────────
def get_objects():
    return _open_ws("Objects").get_all_records(numericise=False)

def get_objects_sheet():
    return _open_ws("Objects")

def get_objects_for_report():
    ws = _open_ws("Objects")
    rows = ws.get_all_values()
    if not rows:
        return []
    headers = rows[0]
    out = []
    for r in rows[1:]:
        item = {}
        for i, h in enumerate(headers):
            if i < len(r):
                item[h] = r[i]
            else:
                item[h] = ""
        out.append(item)
    return out

def append_log(
    object_id: str,
    prev_hours: float,
    new_hours: float,
    hours_delta: float,
    fuel_added: float,
    full_tank: bool,
    calculated_current_fuel: float,
    user_id: int,
    username: Optional[str],
):
    ws = _open_ws("Logs")
    ws.append_row(
        [
            _now_kyiv_str(),
            object_id,
            _to_str_dot(prev_hours),
            _to_str_dot(new_hours),
            _to_str_dot(hours_delta),
            _to_str_dot(fuel_added),
            "TRUE" if full_tank else "FALSE",
            _to_str_dot(calculated_current_fuel),
            int(user_id),
            username or "",
        ]
    )

# ── Операції для адмінів ─────────────────────────────────────────────────────
def add_object(object_id: str, capacity: float, usage_per_hour: float = 0.0):
    ws = get_objects_sheet()
    ws.append_row([
        object_id,
        _to_str_dot(0),
        _to_str_dot(capacity),
        _to_str_dot(0),
        _to_str_dot(usage_per_hour)
    ])

def delete_object(object_id: str) -> bool:
    ws = get_objects_sheet()
    records = ws.get_all_records()
    for idx, row in enumerate(records, start=2):
        if str(row.get("ObjectID")) == str(object_id):
            ws.delete_rows(idx)
            return True
    return False

def update_capacity(object_id: str, new_capacity: float) -> bool:
    ws = get_objects_sheet()
    records = ws.get_all_records()
    for idx, row in enumerate(records, start=2):
        if str(row.get("ObjectID")) == str(object_id):
            ws.update_cell(idx, 3, _to_str_dot(new_capacity))
            return True
    return False

def update_usage(object_id: str, new_usage_per_hour: float) -> bool:
    ws = get_objects_sheet()
    records = ws.get_all_records()
    for idx, row in enumerate(records, start=2):
        if str(row.get("ObjectID")) == str(object_id):
            ws.update_cell(idx, 5, _to_str_dot(new_usage_per_hour))
            return True
    return False

# ── Основна бізнес-логіка ────────────────────────────────────────────────────
def update_object_fuel_with_calc(
    object_id: str,
    new_engine_hours,
    fuel_added,
    full_tank: bool,
    user_id: int,
    username: Optional[str],
) -> bool:
    ws = get_objects_sheet()
    records = ws.get_all_records()

    for idx, row in enumerate(records, start=2):
        if str(row.get("ObjectID")) != str(object_id):
            continue

        prev_hours   = _to_float(row.get("EngineHours"), 0.0)
        capacity     = _to_float(row.get("FuelCapacity"), 0.0)
        prev_current = _to_float(row.get("CurrentFuel"), 0.0)
        usage        = _to_float(row.get("FuelUsagePerHour"), 0.0)

        new_hours = _to_float(new_engine_hours, prev_hours)
        fuel_add  = _to_float(fuel_added, 0.0)

        # ►► НОВА ПЕРЕВІРКА: нові мотогодини не можуть бути менші за поточні
        if new_hours < prev_hours:
            # робимо красиве відображення чинного значення
            prev_pretty = f"{prev_hours:.2f}".rstrip("0").rstrip(".")
            raise ValueError(
                f"Значення мотогодин ({new_hours}) менше за поточне у таблиці ({prev_pretty}). "
                f"Введіть значення не менше {prev_pretty}."
            )

        delta_hours = max(0.0, new_hours - prev_hours)
        burned = delta_hours * usage

        if full_tank:
            new_current = capacity
        else:
            new_current = _clamp(prev_current - burned + fuel_add, 0.0, capacity)

        # Запис у таблицю у форматі з крапкою
        ws.update_cell(idx, 2, _to_str_dot(new_hours))     # EngineHours
        ws.update_cell(idx, 4, _to_str_dot(new_current))   # CurrentFuel

        # Лог
        append_log(
            object_id=object_id,
            prev_hours=prev_hours,
            new_hours=new_hours,
            hours_delta=delta_hours,
            fuel_added=fuel_add,
            full_tank=full_tank,
            calculated_current_fuel=new_current,
            user_id=int(user_id),
            username=username,
        )
        return True

    return False
