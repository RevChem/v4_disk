"""Срез «Намолот» для кнопки Namolot: живой API 1С и группировка по агрофирме."""
from __future__ import annotations

import base64
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.config import get_namolot_config

# Тонны в отчётах = quantity / 1000 (как в 10_main_harvest).
QTY_TO_TONNES = 1.0 / 1000.0
MAX_PERIOD_DAYS = 200
BRIEF_MAX_DAYS = 400
SHIFT_HOUR = 6
TZ_MSK = timezone(timedelta(hours=3))
_CACHE_TTL_SEC = 15 * 60
_YEAR_SUFFIX_RE = re.compile(r"\s*\d{2,4}\s*г?\.?\s*$")

# Организация из Namolot → агрофирма холдинга.
# Номер «Агрофирма N» важнее названия: в 1С строка часто без кавычек и в другом порядке слов.
_NUMBER_TO_FIRM: dict[int, str] = {
    2: "АФ Чистопольская",
    3: "АгроРаздолье",
    4: "Аксу-Агро",
    5: "Дрожжаное Рассвет Агро",
    14: "НурлатАгро",
    15: "НурлатАгро",
}
FIRM_ORDER: tuple[str, ...] = (
    "АФ Чистопольская",
    "АгроРаздолье",
    "Аксу-Агро",
    "Актай",
    "Дрожжаное Рассвет Агро",
    "Лениногорская Птицефабрика",
    "НурлатАгро",
)
OTHER_FIRM = "Прочие"

_ORG_NUM_RE = re.compile(r"агрофирм\w*\s+(\d+)(?:\s*/\s*(\d+))?")
_WORD_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"чистополь"), "АФ Чистопольская"),
    (re.compile(r"(^|\s)полив($|\s)"), "АФ Чистопольская"),
    (re.compile(r"раздолье"), "АгроРаздолье"),
    (re.compile(r"(^|\s)аксу($|\s)"), "Аксу-Агро"),
    (re.compile(r"(^|\s)актай($|\s)"), "Актай"),
    (re.compile(r"дрожжан"), "Дрожжаное Рассвет Агро"),
    (re.compile(r"лениногор"), "Лениногорская Птицефабрика"),
)

COLUMNS: tuple[tuple[str, str], ...] = (
    ("spp", "Поле"),
    ("spp_guid", "ID поля"),
    ("combine_harvester", "Комбайн/Трактор"),
    ("combine_harvester_guid", "ID комбайна"),
    ("combine_harvester_id", "Госзнак"),
    ("machine_operator", "Механизатор"),
    ("machine_operator_guid", "ID механизатора"),
    ("quantity", "Намолот"),
    ("date", "Дата"),
    ("organization", "Организация"),
    ("organization_guid", "ID организации"),
    ("crop", "Культура"),
    ("crop_guid", "ID культуры"),
)
DATE_FIELDS = {"date"}

_cache_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


def _norm_org(text: object) -> str:
    raw = str(text or "").casefold().replace("ё", "е")
    raw = raw.replace("«", " ").replace("»", " ").replace('"', " ").replace("'", " ")
    raw = raw.replace("/", " / ")
    raw = re.sub(r"[^0-9a-zа-я/\s]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def holding_firm(organization: object) -> str:
    """Агрофирма холдинга по строке organization. Неузнанное — «Прочие»."""
    raw = _norm_org(organization)
    if not raw:
        return OTHER_FIRM
    match = _ORG_NUM_RE.search(raw)
    if match:
        number = int(match.group(1))
        firm = _NUMBER_TO_FIRM.get(number)
        if firm:
            return firm
    for pattern, firm in _WORD_RULES:
        if pattern.search(raw):
            return firm
    return OTHER_FIRM


def _round_t(kg: float) -> float:
    return round(float(kg) * QTY_TO_TONNES, 3)


def summarize(records: list[dict]) -> dict:
    """Тонны и число записей: агрофирма → организация → культура."""
    bucket: dict[str, dict[str, dict[str, list]]] = {}
    total_kg = 0.0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        try:
            qty = float(rec.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        total_kg += qty
        firm = holding_firm(rec.get("organization"))
        org = str(rec.get("organization") or "").strip() or "без организации"
        crop = str(rec.get("crop") or "").strip() or "—"
        org_map = bucket.setdefault(firm, {})
        crop_map = org_map.setdefault(org, {})
        cell = crop_map.setdefault(crop, [0.0, 0])
        cell[0] += qty
        cell[1] += 1

    def crops_out(crop_map: dict[str, list]) -> list[dict]:
        rows = [
            {"crop": name, "tonnes": _round_t(kg), "records": count}
            for name, (kg, count) in crop_map.items()
        ]
        rows.sort(key=lambda row: (-row["tonnes"], row["crop"]))
        return rows

    order = list(FIRM_ORDER)
    if OTHER_FIRM in bucket:
        order.append(OTHER_FIRM)
    firms = []
    for firm in order:
        orgs = bucket.get(firm)
        if not orgs:
            continue
        org_rows = []
        firm_kg = 0.0
        firm_records = 0
        for org, crop_map in orgs.items():
            org_kg = sum(cell[0] for cell in crop_map.values())
            org_records = sum(cell[1] for cell in crop_map.values())
            firm_kg += org_kg
            firm_records += org_records
            org_rows.append(
                {
                    "organization": org,
                    "tonnes": _round_t(org_kg),
                    "records": org_records,
                    "crops": crops_out(crop_map),
                }
            )
        org_rows.sort(key=lambda row: (-row["tonnes"], row["organization"]))
        firms.append(
            {
                "firm": firm,
                "tonnes": _round_t(firm_kg),
                "records": firm_records,
                "organizations": org_rows,
            }
        )
    return {
        "records": sum(firm["records"] for firm in firms),
        "tonnes": _round_t(total_kg),
        "firms": firms,
    }


def msk_wall(value) -> datetime | None:
    """Время записи как настенные часы MSK. Наивные строки 1С уже в MSK."""
    if value is None or value == "" or value == 0:
        return None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return msk_wall(int(value.strip()))
    if isinstance(value, (int, float)):
        ts = value / 1000 if abs(value) > 10**12 else float(value)
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK).replace(tzinfo=None)
        except (ValueError, OSError, OverflowError):
            return None
    parsed = parse_date_value(value)
    if not isinstance(parsed, datetime):
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(TZ_MSK).replace(tzinfo=None)


def _surname(operator: str) -> str:
    parts = operator.split()
    return parts[0] if parts else ""


def record_rows(records: list[dict]) -> list[dict]:
    """Компактные строки для фильтра: агрофирма, культура, госномер, фамилия, тонны."""
    rows: list[dict] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        try:
            qty = float(rec.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        operator = str(rec.get("machine_operator") or "").strip()
        wall = msk_wall(rec.get("date"))
        rows.append(
            {
                "at": wall.strftime("%Y-%m-%dT%H:%M:%S") if wall else None,
                "firm": holding_firm(rec.get("organization")),
                "crop": str(rec.get("crop") or "").strip() or "—",
                "plate": str(rec.get("combine_harvester_id") or "").strip(),
                "operator": operator,
                "surname": _surname(operator),
                "tonnes": _round_t(qty),
                "field": str(rec.get("spp") or "").strip(),
            }
        )
    return rows


def _auth_header(username: str, password: str) -> dict[str, str]:
    if not (username and password):
        return {}
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _check_period(date_begin: date, date_end: date, *, max_days: int = MAX_PERIOD_DAYS) -> None:
    if date_end < date_begin:
        raise ValueError("Дата окончания раньше даты начала")
    if (date_end - date_begin).days > max_days:
        raise ValueError(f"Период длиннее {max_days} дней")


def fetch_live(date_begin: date, date_end: date, *, max_days: int = MAX_PERIOD_DAYS) -> list[dict]:
    """Прямой GET в Namolot. Кэш сводок не используется."""
    _check_period(date_begin, date_end, max_days=max_days)
    key = (date_begin.isoformat(), date_end.isoformat())
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL_SEC:
            return hit[1]

    base, user, password = get_namolot_config()
    if not password:
        raise ValueError("Не задан NAMOLOT_PASSWORD")
    response = requests.get(
        base,
        params={"date_begin": key[0], "date_end": key[1]},
        headers=_auth_header(user, password),
        timeout=(10, 180),
        proxies={"http": None, "https": None},
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("Namolot API вернул не список записей")
    records = [row for row in data if isinstance(row, dict)]
    with _cache_lock:
        _cache[key] = (time.monotonic(), records)
    return records


def build_view(date_begin: date, date_end: date) -> dict:
    records = fetch_live(date_begin, date_end)
    rows = record_rows(records)
    return {
        "date_begin": date_begin.isoformat(),
        "date_end": date_end.isoformat(),
        "records": len(rows),
        "tonnes": round(sum(row["tonnes"] for row in rows), 3),
        "rows": rows,
    }


def parse_date_value(value):
    """Дата записи Namolot для Excel. Пустое и 0 — не дата."""
    if value is None or value == "" or value == 0:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.lstrip("-").isdigit():
            return parse_date_value(int(stripped))
        try:
            return datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
                try:
                    return datetime.strptime(stripped, fmt)
                except ValueError:
                    continue
            return value
    if isinstance(value, (int, float)):
        ts = value / 1000 if abs(value) > 10**12 else value
        try:
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError, OverflowError):
            return None
    return value


def workbook_bytes(records: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Намолот"
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for col_idx, (_field, header) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    date_cols: set[int] = set()
    for row_idx, item in enumerate(records, start=2):
        for col_idx, (field, _header) in enumerate(COLUMNS, start=1):
            value = item.get(field, "")
            if field in DATE_FIELDS:
                value = parse_date_value(value)
                if isinstance(value, datetime):
                    date_cols.add(col_idx)
            ws.cell(row=row_idx, column=col_idx, value=value)

    for col_idx in date_cols:
        for row in range(2, ws.max_row + 1):
            cell = ws.cell(row=row, column=col_idx)
            if isinstance(cell.value, datetime):
                cell.number_format = "DD.MM.YYYY HH:MM:SS"

    for col_idx, (_field, header) in enumerate(COLUMNS, start=1):
        max_len = len(header)
        limit = min(ws.max_row, 200)
        for row in range(2, limit + 1):
            val = ws.cell(row=row, column=col_idx).value
            if val is not None:
                max_len = max(max_len, len(str(val)))
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 45)
    ws.freeze_panes = "A2"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _crop_key(text: object) -> str:
    raw = re.sub(r"\s+", " ", str(text or "").strip())
    raw = _YEAR_SUFFIX_RE.sub("", raw).strip()
    return raw.casefold().replace("ё", "е")


def same_crop(record_crop: object, wanted: str) -> bool:
    got = _crop_key(record_crop)
    need = _crop_key(wanted)
    if not got or not need:
        return False
    return got == need or got.startswith(need + " ")


def shift_cutoff(report_date: date) -> datetime:
    """06:00 даты сводки: записи этой смены идут до 05:59."""
    return datetime(report_date.year, report_date.month, report_date.day, SHIFT_HOUR)


def harvest_season_start(report_date: date) -> date:
    from app.legacy.manual_overrides import combine_season_start_for_date

    return combine_season_start_for_date(report_date)


def _shown_end(cutoff: datetime) -> str:
    return (cutoff - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")


def _select_brief(records: list[dict], report_date: date, firm: str, crop: str) -> tuple[dict, list[dict]]:
    """Тонны одной агрофирмы и культуры и сырые записи этого среза."""
    cutoff = shift_cutoff(report_date)
    day0 = cutoff - timedelta(days=1)
    season_day = harvest_season_start(report_date)
    season0 = datetime(season_day.year, season_day.month, season_day.day, SHIFT_HOUR)
    day_kg = season_kg = 0.0
    day_n = season_n = 0
    kept: list[dict] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if holding_firm(rec.get("organization")) != firm:
            continue
        if not same_crop(rec.get("crop"), crop):
            continue
        wall = msk_wall(rec.get("date"))
        if wall is None or wall < season0 or wall >= cutoff:
            continue
        try:
            qty = float(rec.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        season_kg += qty
        season_n += 1
        if day0 <= wall < cutoff:
            day_kg += qty
            day_n += 1
        kept.append(rec)
    summary = {
        "firm": firm,
        "crop": crop,
        "report_date": report_date.isoformat(),
        "day": {
            "from": day0.strftime("%Y-%m-%dT%H:%M:%S"),
            "to": _shown_end(cutoff),
            "tonnes": _round_t(day_kg),
            "records": day_n,
        },
        "season": {
            "from": season0.strftime("%Y-%m-%dT%H:%M:%S"),
            "to": _shown_end(cutoff),
            "tonnes": _round_t(season_kg),
            "records": season_n,
        },
    }
    return summary, kept


def brief_from_records(records: list[dict], report_date: date, firm: str, crop: str) -> dict:
    summary, kept = _select_brief(records, report_date, firm, crop)
    summary["rows"] = record_rows(kept)
    return summary


def _require_firm_crop(firm: str, crop: str) -> tuple[str, str]:
    firm = str(firm or "").strip()
    crop = str(crop or "").strip()
    if not firm or not crop:
        raise ValueError("Нужны агрофирма и культура")
    return firm, crop


def build_brief(report_date: date, firm: str, crop: str) -> dict:
    firm, crop = _require_firm_crop(firm, crop)
    start = harvest_season_start(report_date)
    records = fetch_live(start, report_date, max_days=BRIEF_MAX_DAYS)
    return brief_from_records(records, report_date, firm, crop)


def build_brief_excel(report_date: date, firm: str, crop: str) -> bytes:
    firm, crop = _require_firm_crop(firm, crop)
    start = harvest_season_start(report_date)
    records = fetch_live(start, report_date, max_days=BRIEF_MAX_DAYS)
    _summary, kept = _select_brief(records, report_date, firm, crop)
    return workbook_bytes(kept)


def build_excel(date_begin: date, date_end: date) -> bytes:
    return workbook_bytes(fetch_live(date_begin, date_end))


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
