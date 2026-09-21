"""
Отчёт «Рейтинг механизаторов»
==============================
Сравнивает фиксацию простоев в чат-боте (Downtime API mech.chistai.ru)
с объективными остановками техники по телеметрии Cropwise за период.

Режимы (REPORT27_METRIC):
  count (по умолчанию, сводка 27) — количество остановок
  duration (сводка 27.1) — суммарное время остановок

Колонки (count):
  № | Агрофирма | ФИО | Остановки в чат-боте | Остановки Cropwise (>10 мин) |
  Коэффициент (бот / телеметрия) | Агрооперации

Колонки (duration):
  № | ФИО | Общее время остановок в чат-боте |
  Общее время остановок Cropwise (>10 мин) | Коэффициент

В режиме duration (27.1) один сотрудник — одна строка: времена по всем
агрофирмам суммируются, агрофирма берётся по наибольшему времени Cropwise.

Период: DATE_FROM … DATE_TO (смены с 06:00 МСК).
Коэффициент >= 0,20 подсвечивается зелёным.

Источники Cropwise:
  - machine_task_field_mapping_items.stops_timetable — простои на поле
  - machine_task.stops_on_road_timetable — простои вне поля
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx
import openpyxl
import requests
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from plans import (
    API_TOKEN,
    BASE_URL,
    TZ_MSK,
    FIRM_ORDER,
    FIRM_TO_REGION,
    FOLDER_TO_FIRM,
    FIELD_NAME_FORCE_RULES,
)

REGION_TO_FIRM: dict[int, str] = {int(rid): firm for firm, rid in FIRM_TO_REGION.items()}
TASK_LOOKBACK_DAYS = 14

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

API_TOKEN = (
    (os.environ.get("CROPWISE_API_TOKEN") or "").strip()
    or (os.environ.get("API_TOKEN") or "").strip()
    or API_TOKEN
)

DOWNTIME_API_KEY = (
    (os.environ.get("SVODKI_DOWNTIME_API_KEY") or "").strip()
    or (os.environ.get("DOWNTIME_API_KEY") or "").strip()
    or "sk_74220500d6ee8ea412496a62fecdfc5f09f8f922"
)
DOWNTIME_URL = (
    (os.environ.get("SVODKI_DOWNTIME_URL") or "").strip()
    or (os.environ.get("DOWNTIME_URL") or "").strip()
    or "https://mech.chistai.ru/v1/downtime"
)

_DOWNTIME_KEY_FILE_CANDIDATES = (
    Path(os.environ.get("UPLOAD_DIR", "uploads")) / ".downtime_api_key",
    Path("/app/uploads/.downtime_api_key"),
)


def _effective_downtime_api_key() -> str:
    for key in ("SVODKI_DOWNTIME_API_KEY", "DOWNTIME_API_KEY"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    for path in _DOWNTIME_KEY_FILE_CANDIDATES:
        try:
            if path.is_file():
                val = path.read_text(encoding="utf-8").strip()
                if val:
                    return val
        except OSError:
            continue
    return DOWNTIME_API_KEY

DATE_TO: date = date.today()
if os.environ.get("REPORT_DATE"):
    DATE_TO = date.fromisoformat(os.environ["REPORT_DATE"].strip())
if os.environ.get("DATE_TO"):
    DATE_TO = date.fromisoformat(os.environ["DATE_TO"].strip())

DATE_FROM: date = DATE_TO.replace(day=1)
if os.environ.get("DATE_FROM"):
    DATE_FROM = date.fromisoformat(os.environ["DATE_FROM"].strip())
if DATE_FROM > DATE_TO:
    DATE_FROM, DATE_TO = DATE_TO, DATE_FROM

STOP_MIN_SEC = 600
GREEN_COEF = 0.20
SEMAPHORE_LIMIT = 20
COEF_NUMBER_FORMAT = "0%"

# count — сводка 27; duration — сводка 27.1 (общее время остановок).
METRIC_MODE: str = "count"

OUTPUT_FILE = (
    f"mechanizator_rating_{DATE_FROM.isoformat()}_{DATE_TO.isoformat()}.xlsx"
)

THIN = Side(style="thin", color="000000")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEADER_FILL = PatternFill("solid", fgColor="D9E1F2")
GREEN_FILL = PatternFill("solid", fgColor="E2EFDA")
TITLE_FONT = Font(bold=True, size=13)
HEADER_FONT = Font(bold=True, size=10)
NOTE_FONT = Font(italic=True, size=10)
FIRM_FONT = Font(bold=True, size=11)


# =============================================================================
# HTTP
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


def get_all(endpoint: str, params: dict | None = None) -> list[dict]:
    p = dict(params or {})
    results: list[dict] = []
    from_id = 0
    while True:
        r = session.get(
            f"{BASE_URL}/{endpoint}",
            params={**p, "from_id": from_id, "limit": 1000},
            timeout=120,
        )
        r.raise_for_status()
        body = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        results.extend(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = last_id + 1
    return results


def _norm_fio(value: str) -> str:
    return " ".join((value or "").split())


def _fio_merge_key(value: str) -> str:
    """Ключ склейки ФИО: без регистра и «ё», схлопнутые пробелы."""
    return _norm_fio(value).casefold().replace("ё", "е")


def _parse_dt(raw) -> datetime | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T", 1) if "T" not in s else s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_MSK)
    else:
        dt = dt.astimezone(TZ_MSK)
    return dt


def _interval_sec(interval) -> float:
    if not interval or len(interval) < 2:
        return 0.0
    s = _parse_dt(interval[0])
    e = _parse_dt(interval[1])
    if not s or not e or e <= s:
        return 0.0
    return (e - s).total_seconds()


def _qualifying_stop_secs(timetable, win_start: datetime, win_end: datetime) -> list[float]:
    """Длительности остановок ≥ STOP_MIN_SEC, пересекающихся с окном отчёта."""
    out: list[float] = []
    for interval in timetable or []:
        sec = _interval_sec(interval)
        if sec < STOP_MIN_SEC:
            continue
        s = _parse_dt(interval[0])
        e = _parse_dt(interval[1])
        if not s or not e:
            continue
        if e <= win_start or s >= win_end:
            continue
        clipped_s = max(s, win_start)
        clipped_e = min(e, win_end)
        clipped = (clipped_e - clipped_s).total_seconds()
        if clipped > 0:
            out.append(clipped)
    return out


def _count_stops(timetable, win_start: datetime, win_end: datetime) -> int:
    return len(_qualifying_stop_secs(timetable, win_start, win_end))


def _sum_stop_secs(timetable, win_start: datetime, win_end: datetime) -> float:
    return float(sum(_qualifying_stop_secs(timetable, win_start, win_end)))


def _fmt_duration(seconds: float | int | None) -> str:
    total = int(round(float(seconds or 0)))
    if total < 0:
        total = 0
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"


def _downtime_duration_sec(rec: dict) -> int:
    raw = rec.get("duration")
    try:
        sec = int(raw or 0)
    except (TypeError, ValueError):
        sec = 0
    if sec > 0:
        return sec
    started = _downtime_started_at(rec)
    ended = None
    for key in ("end_datetime", "ended_at", "end_time"):
        ended = _parse_dt(rec.get(key))
        if ended is not None:
            break
    if started and ended and ended > started:
        return int((ended - started).total_seconds())
    return 0


# =============================================================================
# DOWNTIME (PostgreSQL чат-бота или HTTP API)
# =============================================================================

def _resolve_dop_database_url() -> str:
    for key in ("DOP_DATABASE_URL", "SVODKI_DOP_DATABASE_URL"):
        url = (os.environ.get(key) or "").strip()
        if url:
            return url
    host = (os.environ.get("DOP_DB_HOST") or "").strip()
    if not host:
        return ""
    port = (os.environ.get("DOP_DB_PORT") or "5432").strip()
    name = (os.environ.get("DOP_DB_NAME") or "project").strip()
    user = (os.environ.get("DOP_DB_USER") or "postgres").strip()
    password = (os.environ.get("DOP_DB_PASSWORD") or "").strip()
    if not password:
        return ""
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{quote_plus(name)}"
    )


def _downtime_api_urls() -> list[str]:
    urls: list[str] = []
    for key in ("SVODKI_DOWNTIME_URL", "DOWNTIME_URL"):
        raw = (os.environ.get(key) or "").strip()
        if raw and raw not in urls:
            urls.append(raw)
    for fallback in (
        "http://host.docker.internal:8454/v1/downtime",
        "http://127.0.0.1:8454/v1/downtime",
        "https://mech.chistai.ru/v1/downtime",
    ):
        if fallback not in urls:
            urls.append(fallback)
    return urls


def load_downtime_from_db(db_url: str, date_from: date, date_to: date) -> list[dict]:
    import psycopg2
    from psycopg2.extras import RealDictCursor

    shift_start = dt_time(6, 0)
    date_to_real = date_to + timedelta(days=1)
    sql = """
        SELECT
            d.id,
            d.fio,
            d.start_date,
            d.start_time,
            d.end_date,
            d.end_time,
            d.duration,
            d.reason,
            d.vehicle_id,
            d.machine_task_id,
            COALESCE(v.agrofirm, d.agrofirm) AS agrofirm
        FROM downtime d
        LEFT JOIN vehicles v ON v.id = d.vehicle_id
        WHERE
            (d.start_date > %s OR (d.start_date = %s AND d.start_time >= %s))
            AND
            (d.start_date < %s OR (d.start_date = %s AND d.start_time < %s))
        ORDER BY d.start_date DESC, d.start_time DESC
    """
    params = (
        date_from,
        date_from,
        shift_start,
        date_to_real,
        date_to_real,
        shift_start,
    )
    conn = psycopg2.connect(db_url, connect_timeout=15)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    records: list[dict] = []
    for row in rows:
        start_dt = datetime.combine(row["start_date"], row["start_time"])
        end_dt = datetime.combine(row["end_date"], row["end_time"])
        records.append(
            {
                "id": row["id"],
                "fio": row["fio"],
                "start_datetime": start_dt.isoformat(sep=" "),
                "end_datetime": end_dt.isoformat(sep=" "),
                "duration": int(row["duration"] or 0),
                "reason": row["reason"],
                "vehicle_id": row["vehicle_id"],
                "machine_task_id": int(row["machine_task_id"] or 0),
                "agrofirm": row["agrofirm"] or "",
            }
        )
    return records


def load_downtime_via_api(date_from: date, date_to: date) -> list[dict]:
    api_key = _effective_downtime_api_key()
    if not api_key:
        return []

    headers = {"X-API-Key": api_key}
    all_records: list[dict] = []
    seen: set[tuple] = set()
    last_error: str | None = None

    for api_url in _downtime_api_urls():
        page_records: list[dict] = []
        page = 1
        while page <= 500:
            try:
                r = requests.get(
                    api_url,
                    headers=headers,
                    params={
                        "date_from": date_from.isoformat(),
                        "date_to": date_to.isoformat(),
                        "page": page,
                        "limit": 200,
                    },
                    timeout=60,
                )
                r.raise_for_status()
            except requests.RequestException as exc:
                last_error = f"{api_url} стр.{page}: {exc}"
                break
            body = r.json()
            data = body.get("data", []) or []
            if not data:
                break
            added = 0
            for rec in data:
                key = _dedup_key(rec)
                if key in seen:
                    continue
                seen.add(key)
                page_records.append(rec)
                added += 1
            total = int(body.get("total") or 0)
            if total and len(page_records) >= total:
                break
            if added == 0:
                break
            page += 1
        if page_records:
            all_records.extend(page_records)
            print(f"    Downtime API ({api_url}): {len(page_records)} записей", flush=True)
            break
        if last_error:
            print(f"[27] {last_error}", file=sys.stderr, flush=True)

    return all_records


def _downtime_started_at(rec: dict) -> datetime | None:
    for key in ("start_datetime", "started_at", "start_time", "date"):
        dt = _parse_dt(rec.get(key))
        if dt is not None:
            return dt
    return None


def _dedup_key(rec: dict) -> tuple:
    rid = rec.get("id")
    if rid is not None:
        return ("id", rid)
    started = rec.get("start_datetime") or rec.get("started_at") or rec.get("date") or ""
    return (
        "composite",
        rec.get("vehicle_id") or rec.get("vehicle") or "",
        rec.get("fio") or "",
        started,
        rec.get("duration") or 0,
        rec.get("reason") or "",
    )


def load_downtime_period(
    date_from: date,
    date_to: date,
    win_start: datetime,
    win_end: datetime,
) -> list[dict]:
    all_records: list[dict] = []
    source = ""
    db_url = _resolve_dop_database_url()

    all_records = load_downtime_via_api(date_from, date_to)
    if all_records:
        source = "HTTP API"

    if not all_records and db_url:
        try:
            all_records = load_downtime_from_db(db_url, date_from, date_to)
            source = "PostgreSQL чат-бота"
            print(f"    Downtime DB: загружено {len(all_records)}", flush=True)
        except Exception as exc:
            print(f"[27] Downtime DB: {exc}", file=sys.stderr, flush=True)

    if not all_records:
        if not db_url and not _effective_downtime_api_key():
            print(
                "[27] Нет данных чат-бота: задайте DOP_DATABASE_URL "
                "(прямой доступ к БД dop) или DOWNTIME_API_KEY.",
                flush=True,
            )
        return []

    firm_set = set(FIRM_ORDER)
    by_firm = [r for r in all_records if (r.get("agrofirm") or "") in firm_set]
    in_window = [r for r in by_firm if _downtime_in_window(r, win_start, win_end)]
    print(
        f"    Downtime ({source or 'источник'}): всего {len(all_records)}, "
        f"по агрофирмам {len(by_firm)}, в окне {len(in_window)}",
        flush=True,
    )
    return in_window


# =============================================================================
# СПРАВОЧНИКИ
# =============================================================================

def _folder_parent_map(gfs_all: list[dict]) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
    for gf in gfs_all:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = gf.get("parent_id") or gf.get("group_folder_parent_id")
        out[int(fid)] = int(pid) if pid is not None else None
    return out


def _resolve_root(folder_id: int | None, parent_map: dict[int, int | None]) -> int | None:
    if folder_id is None:
        return None
    root_ids = set(FOLDER_TO_FIRM.keys())
    cur = int(folder_id)
    seen: set[int] = set()
    for _ in range(64):
        if cur in root_ids:
            return cur
        if cur in seen:
            return None
        seen.add(cur)
        nxt = parent_map.get(cur)
        if nxt is None:
            return None
        cur = int(nxt)
    return None


def build_field_to_firm(fields_all, fgs_all, gfs_all) -> dict[int, str]:
    fgs_map = {fg["id"]: fg for fg in fgs_all}
    parent_map = _folder_parent_map(gfs_all)
    out: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        raw_folder = f.get("group_folder_id")
        fgid = f.get("field_group_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(fgid, {}).get("group_folder_id")
        root = _resolve_root(int(raw_folder) if raw_folder is not None else None, parent_map)
        if root is not None:
            firm = FOLDER_TO_FIRM.get(root)
            if firm:
                out[int(fid)] = firm
    for f in fields_all:
        fid = f.get("id")
        blob = (f"{f.get('name') or ''} {f.get('title') or ''}").casefold().replace(" ", "")
        for needle, firm_key in FIELD_NAME_FORCE_RULES:
            if needle in blob:
                out[int(fid)] = firm_key
                break
    return out


def build_machine_to_firm() -> dict[int, str]:
    """machine_id → агрофирма по machine_region_mapping_items (как «Привязана к регионам»)."""
    out: dict[int, str] = {}
    for row in get_all("machine_region_mapping_items"):
        mid = row.get("machine_id")
        region_id = row.get("machine_region_id")
        if mid is None or region_id is None:
            continue
        firm = REGION_TO_FIRM.get(int(region_id))
        if firm:
            out[int(mid)] = firm
    return out


def build_users_map(users: list[dict]) -> dict[int, str]:
    out: dict[int, str] = {}
    for u in users:
        uid = u.get("id")
        if uid is None:
            continue
        parts = [u.get("last_name"), u.get("first_name"), u.get("middle_name")]
        fio = _norm_fio(" ".join(p for p in parts if p))
        if not fio:
            fio = _norm_fio(u.get("username") or u.get("name") or "")
        if fio:
            out[int(uid)] = fio
    return out


# =============================================================================
# CROPWISE: ОСТАНОВКИ
# =============================================================================

async def _fetch_mtfmi(client: httpx.AsyncClient, task_id: int, sem: asyncio.Semaphore) -> list[dict]:
    items: list[dict] = []
    from_id = 0
    url = f"{BASE_URL}/machine_task_field_mapping_items"
    while True:
        async with sem:
            r = await client.get(
                url,
                params={"machine_task_id": task_id, "from_id": from_id, "limit": 1000},
            )
        r.raise_for_status()
        body = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        items.extend(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = last_id + 1
    return items


def _task_firm(task: dict, machine_to_firm: dict[int, str]) -> str | None:
    mid = task.get("machine_id")
    if mid is None:
        return None
    return machine_to_firm.get(int(mid))


async def collect_cropwise_stops(
    tasks: list[dict],
    work_types: dict[int, str],
    machine_to_firm: dict[int, str],
    users_map: dict[int, str],
    win_start: datetime,
    win_end: datetime,
) -> tuple[
    Counter[tuple[str, str]],
    Counter[tuple[str, str]],
    dict[tuple[str, str], set[str]],
]:
    """Возвращает (cropwise_counts, cropwise_secs, work_types_by_key)."""
    crop_counts: Counter[tuple[str, str]] = Counter()
    crop_secs: Counter[tuple[str, str]] = Counter()
    work_types_by_key: dict[tuple[str, str], set[str]] = defaultdict(set)

    relevant = [t for t in tasks if t.get("driver_id")]
    if not relevant:
        return crop_counts, crop_secs, work_types_by_key

    from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks

    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
    task_ids = [int(t["id"]) for t in relevant]

    async with httpx.AsyncClient(
        headers={"X-User-Api-Token": API_TOKEN},
        timeout=120,
    ) as client:
        mtfmi_map = await fetch_mtfmi_map_for_tasks(
            client, sem, BASE_URL, task_ids, log_prefix="[r27] ",
        )

    for task in relevant:
        mtfmi = mtfmi_map.get(int(task["id"]), [])
        driver_id = int(task["driver_id"])
        fio = users_map.get(driver_id)
        if not fio:
            continue
        firm = _task_firm(task, machine_to_firm)
        if not firm:
            continue
        wt_name = work_types.get(task.get("work_type_id"), "") or "—"

        road_secs = _qualifying_stop_secs(
            task.get("stops_on_road_timetable"), win_start, win_end,
        )
        field_secs: list[float] = []
        for item in mtfmi:
            field_secs.extend(
                _qualifying_stop_secs(item.get("stops_timetable"), win_start, win_end)
            )
        all_secs = road_secs + field_secs
        if not all_secs:
            continue

        key = (firm, fio)
        crop_counts[key] += len(all_secs)
        crop_secs[key] += int(sum(all_secs))
        if wt_name and wt_name != "—":
            work_types_by_key[key].add(wt_name)

    return crop_counts, crop_secs, work_types_by_key


# =============================================================================
# АГРЕГАЦИЯ
# =============================================================================

@dataclass
class DriverRow:
    agrofirm: str
    fio: str
    bot_count: int = 0
    cropwise_count: int = 0
    bot_secs: int = 0
    cropwise_secs: int = 0
    work_types: set[str] = field(default_factory=set)

    @property
    def coefficient(self) -> float | None:
        if METRIC_MODE == "duration":
            if self.cropwise_secs <= 0:
                return None
            return self.bot_secs / self.cropwise_secs
        if self.cropwise_count <= 0:
            return None
        return self.bot_count / self.cropwise_count

    @property
    def bot_metric(self) -> int:
        return self.bot_secs if METRIC_MODE == "duration" else self.bot_count

    @property
    def cropwise_metric(self) -> int:
        return self.cropwise_secs if METRIC_MODE == "duration" else self.cropwise_count


def _apply_period_from_env() -> None:
    """Период из REPORT27_* / REPORT_WINDOW / DATE_FROM/DATE_TO."""
    global DATE_FROM, DATE_TO, OUTPUT_FILE, METRIC_MODE

    raw_metric = (os.environ.get("REPORT27_METRIC") or "count").strip().lower()
    METRIC_MODE = (
        "duration"
        if raw_metric in {"duration", "time", "sec", "seconds", "длительность"}
        else "count"
    )

    r27_from_raw = (os.environ.get("REPORT27_DATE_FROM") or "").strip()
    r27_to_raw = (os.environ.get("REPORT27_DATE_TO") or "").strip()
    if r27_from_raw and r27_to_raw:
        DATE_FROM = date.fromisoformat(r27_from_raw[:10])
        DATE_TO = date.fromisoformat(r27_to_raw[:10])
    else:
        ws_raw = os.environ.get("REPORT_WINDOW_START", "").strip()
        we_raw = os.environ.get("REPORT_WINDOW_END", "").strip()
        if ws_raw and we_raw:
            ws = _parse_window_iso(ws_raw)
            we = _parse_window_iso(we_raw)
            if ws and we and ws < we:
                DATE_FROM = ws.date()
                end_label = we - timedelta(seconds=1)
                DATE_TO = end_label.date() if end_label.date() >= DATE_FROM else we.date()
            else:
                _apply_period_from_env_dates_only()
        else:
            _apply_period_from_env_dates_only()

    if DATE_FROM > DATE_TO:
        DATE_FROM, DATE_TO = DATE_TO, DATE_FROM

    prefix = (
        "mechanizator_rating_time"
        if METRIC_MODE == "duration"
        else "mechanizator_rating"
    )
    OUTPUT_FILE = f"{prefix}_{DATE_FROM.isoformat()}_{DATE_TO.isoformat()}.xlsx"


def _apply_period_from_env_dates_only() -> None:
    global DATE_FROM, DATE_TO
    if os.environ.get("DATE_TO"):
        DATE_TO = date.fromisoformat(os.environ["DATE_TO"].strip())
    elif os.environ.get("REPORT_DATE"):
        DATE_TO = date.fromisoformat(os.environ["REPORT_DATE"].strip())

    if os.environ.get("DATE_FROM"):
        DATE_FROM = date.fromisoformat(os.environ["DATE_FROM"].strip())
    else:
        DATE_FROM = DATE_TO.replace(day=1)


def _coef_display_value(coef: float | None) -> float | None:
    if coef is None:
        return None
    return coef


def _write_coef_cell(cell, coef: float | None) -> None:
    cell.value = _coef_display_value(coef)
    cell.border = BORDER
    if coef is not None:
        cell.number_format = COEF_NUMBER_FORMAT


def _parse_window_iso(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=TZ_MSK)
        except ValueError:
            continue
    return _parse_dt(text)


def _resolve_window() -> tuple[datetime, datetime]:
    ws_raw = os.environ.get("REPORT_WINDOW_START", "").strip()
    we_raw = os.environ.get("REPORT_WINDOW_END", "").strip()
    if ws_raw and we_raw:
        ws = _parse_window_iso(ws_raw)
        we = _parse_window_iso(we_raw)
        if ws and we and ws < we:
            return ws, we
    win_start = datetime(
        DATE_FROM.year, DATE_FROM.month, DATE_FROM.day, 6, 0, 0, tzinfo=TZ_MSK,
    )
    win_end = datetime(
        DATE_TO.year, DATE_TO.month, DATE_TO.day, 6, 0, 0, tzinfo=TZ_MSK,
    ) + timedelta(days=1)
    return win_start, win_end


def _downtime_in_window(rec: dict, win_start: datetime, win_end: datetime) -> bool:
    ts = _downtime_started_at(rec)
    if ts is None:
        return False
    return win_start <= ts < win_end


def aggregate_rows(
    downtime_records: list[dict],
    crop_counts: Counter[tuple[str, str]],
    crop_secs: Counter[tuple[str, str]],
    work_types_by_key: dict[tuple[str, str], set[str]],
) -> dict[tuple[str, str], DriverRow]:
    rows: dict[tuple[str, str], DriverRow] = {}

    for rec in downtime_records:
        fio = _norm_fio(rec.get("fio") or "")
        firm = (rec.get("agrofirm") or "").strip()
        if not fio or not firm:
            continue
        key = (firm, fio)
        row = rows.setdefault(key, DriverRow(agrofirm=firm, fio=fio))
        row.bot_count += 1
        row.bot_secs += _downtime_duration_sec(rec)

    for (firm, fio), cnt in crop_counts.items():
        key = (firm, fio)
        row = rows.setdefault(key, DriverRow(agrofirm=firm, fio=fio))
        row.cropwise_count += cnt
        row.cropwise_secs += int(crop_secs.get((firm, fio), 0))
        row.work_types.update(work_types_by_key.get((firm, fio), set()))

    # Пары, где есть только секунды (на случай рассинхрона счётчиков).
    for (firm, fio), secs in crop_secs.items():
        if (firm, fio) in crop_counts:
            continue
        key = (firm, fio)
        row = rows.setdefault(key, DriverRow(agrofirm=firm, fio=fio))
        row.cropwise_secs += int(secs)
        row.work_types.update(work_types_by_key.get((firm, fio), set()))

    return rows


def _firm_rank(firm: str) -> int:
    try:
        return FIRM_ORDER.index(firm)
    except ValueError:
        return len(FIRM_ORDER) + 1


def merge_rows_by_fio(
    rows: dict[tuple[str, str], DriverRow],
) -> dict[tuple[str, str], DriverRow]:
    """
    Сводка 27.1: один сотрудник — одна строка.
    Суммирует время/счётчики по всем агрофирмам; агрофирму выбирает
    по наибольшему времени Cropwise (затем бот, затем порядок FIRM_ORDER).
    """
    buckets: dict[str, list[DriverRow]] = defaultdict(list)
    for row in rows.values():
        key = _fio_merge_key(row.fio)
        if not key:
            continue
        buckets[key].append(row)

    merged: dict[tuple[str, str], DriverRow] = {}
    for group in buckets.values():
        if len(group) == 1:
            only = group[0]
            merged[(only.agrofirm, only.fio)] = only
            continue

        primary = max(
            group,
            key=lambda r: (
                r.cropwise_secs,
                r.bot_secs,
                r.cropwise_count,
                r.bot_count,
                -_firm_rank(r.agrofirm),
            ),
        )
        out = DriverRow(agrofirm=primary.agrofirm, fio=primary.fio)
        for row in group:
            out.bot_count += row.bot_count
            out.cropwise_count += row.cropwise_count
            out.bot_secs += row.bot_secs
            out.cropwise_secs += row.cropwise_secs
            out.work_types.update(row.work_types)
            # Более «полное» написание ФИО (длиннее) — для отображения.
            if len(row.fio) > len(out.fio):
                out.fio = row.fio
        merged[(out.agrofirm, out.fio)] = out
    return merged


# =============================================================================
# EXCEL
# =============================================================================

def _fmt_period() -> str:
    for from_key, to_key in (
        ("REPORT27_DATE_FROM", "REPORT27_DATE_TO"),
        ("DATE_FROM", "DATE_TO"),
    ):
        raw_from = (os.environ.get(from_key) or "").strip()
        raw_to = (os.environ.get(to_key) or "").strip()
        if raw_from and raw_to:
            try:
                d_from = date.fromisoformat(raw_from[:10])
                d_to = date.fromisoformat(raw_to[:10])
                if d_from <= d_to:
                    return f"{d_from.strftime('%d.%m.%Y')}–{d_to.strftime('%d.%m.%Y')}"
            except ValueError:
                pass

    ws, we = _resolve_window()
    end_label = we - timedelta(seconds=1)
    if end_label.date() < ws.date():
        end_label = we
    if ws.hour == 6 and ws.minute == 0 and we.hour == 6 and we.minute == 0:
        end_day = (we - timedelta(days=1)).date() if we.hour == 6 else end_label.date()
        if end_day >= ws.date():
            return f"{ws.strftime('%d.%m.%Y')}–{end_day.strftime('%d.%m.%Y')}"
    return f"{ws.strftime('%d.%m.%Y %H:%M')}–{end_label.strftime('%d.%m.%Y %H:%M')}"


def _work_types_str(types: set[str]) -> tuple[str, str]:
    if not types:
        return "", ""
    ordered = sorted(types, key=lambda x: x.lower())
    joined = ", ".join(ordered)
    multiline = "\n".join(ordered)
    return multiline, joined


def _write_header_row(ws, row: int, headers: list[str]) -> None:
    for col, text in enumerate(headers, 1):
        cell = ws.cell(row=row, column=col, value=text)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _write_data_row(
    ws,
    row: int,
    values: list,
    green: bool = False,
    *,
    coef_col: int | None = 6,
) -> None:
    for col, val in enumerate(values, 1):
        cell = ws.cell(row=row, column=col, value=val)
        cell.border = BORDER
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        if green:
            cell.fill = GREEN_FILL
        if coef_col is not None and col == coef_col and isinstance(val, (int, float)):
            cell.number_format = COEF_NUMBER_FORMAT


def _metric_headers() -> tuple[str, str]:
    if METRIC_MODE == "duration":
        return (
            "Общее время указанных остановок в чат-боте",
            "Общее время остановок по Кропвайс (более 10 мин.)",
        )
    return (
        "Количество указанных остановок в чат-боте, шт",
        "Количество остановок по Кропвайс (более 10 мин.)",
    )


def _format_metric_value(value: int) -> int | str:
    if METRIC_MODE == "duration":
        return _fmt_duration(value)
    return int(value)


def _report_headers() -> list[str]:
    bot_hdr, crop_hdr = _metric_headers()
    if METRIC_MODE == "duration":
        # 27.1 — без агрофирмы и агроопераций (один сотрудник = одна строка).
        return [
            "№",
            "ФИО",
            bot_hdr,
            crop_hdr,
            "Коэффициент отражения остановок (бот / телеметрия), %",
        ]
    return [
        "№",
        "Агрофирма",
        "ФИО",
        bot_hdr,
        crop_hdr,
        "Коэффициент отражения остановок (бот / телеметрия), %",
        "Агрооперации",
        "Агрооперация",
    ]


def _row_values(idx: int, row: DriverRow, *, bot_override: int | None = None) -> list:
    bot_val = row.bot_metric if bot_override is None else bot_override
    crop_val = row.cropwise_metric
    coef: float | int | None = 0 if bot_override is not None else row.coefficient
    if METRIC_MODE == "duration":
        return [
            idx,
            row.fio,
            _format_metric_value(bot_val),
            _format_metric_value(crop_val),
            coef,
        ]
    wt_ml, wt_one = _work_types_str(row.work_types)
    return [
        idx,
        row.agrofirm,
        row.fio,
        _format_metric_value(bot_val),
        _format_metric_value(crop_val),
        coef,
        wt_ml,
        wt_one,
    ]


def build_workbook(rows: dict[tuple[str, str], DriverRow]) -> openpyxl.Workbook:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = (
        "Рейтинг по времени"
        if METRIC_MODE == "duration"
        else "Рейтинг механизаторов"
    )

    headers = _report_headers()
    coef_col = 5 if METRIC_MODE == "duration" else 6
    bot_col = 3 if METRIC_MODE == "duration" else 4
    crop_col = 4 if METRIC_MODE == "duration" else 5
    total_merge_end = 2 if METRIC_MODE == "duration" else 3

    r = 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(headers))
    title_prefix = (
        "Рейтинг механизаторов по времени простоев в чат-боте за "
        if METRIC_MODE == "duration"
        else "Рейтинг механизаторов по работе в чат-боте за "
    )
    title = ws.cell(r, 1, f"{title_prefix}{_fmt_period()}")
    title.font = TITLE_FONT
    title.alignment = Alignment(horizontal="left")

    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(headers))
    note_text = (
        "Зеленым отмечены механизаторы, у которых доля времени простоев в чат-боте "
        "составила более 20% от объективного времени остановок техники."
        if METRIC_MODE == "duration"
        else
        "Зеленым отмечены механизаторы, которые зафиксировали в чат-боте "
        "более 20% простоев (от объективных остановок техники)."
    )
    note = ws.cell(r, 1, note_text)
    note.font = NOTE_FONT
    note.alignment = Alignment(wrap_text=True)

    for firm in FIRM_ORDER:
        zero_rows = sorted(
            [
                row for row in rows.values()
                if row.agrofirm == firm
                and row.bot_metric == 0
                and row.cropwise_metric > 0
            ],
            key=lambda x: x.fio,
        )
        bot_rows = sorted(
            [
                row for row in rows.values()
                if row.agrofirm == firm and row.bot_metric > 0
            ],
            key=lambda x: (-(x.coefficient or 0), -x.bot_metric, x.fio),
        )
        if not zero_rows and not bot_rows:
            continue

        r += 1
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(headers))
        ws.cell(r, 1, firm).font = FIRM_FONT
        r += 1

        if zero_rows:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(headers))
            ws.cell(
                r,
                1,
                "Механизаторы, не вносившие данные в чат-бот (0% фиксации)",
            ).font = NOTE_FONT
            r += 1
            _write_header_row(ws, r, headers)
            r += 1
            for idx, row in enumerate(zero_rows, 1):
                _write_data_row(
                    ws,
                    r,
                    _row_values(idx, row, bot_override=0),
                    coef_col=coef_col,
                )
                r += 1

        if bot_rows:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(headers))
            ws.cell(
                r,
                1,
                "Механизаторы, которые пользовались чат-ботом для фиксации простоев техники",
            ).font = NOTE_FONT
            r += 1
            _write_header_row(ws, r, headers)
            r += 1
            total_bot = 0
            total_crop = 0
            for idx, row in enumerate(bot_rows, 1):
                coef = row.coefficient
                green = coef is not None and coef >= GREEN_COEF
                total_bot += row.bot_metric
                total_crop += row.cropwise_metric
                _write_data_row(
                    ws,
                    r,
                    _row_values(idx, row),
                    green=green,
                    coef_col=coef_col,
                )
                r += 1
            total_coef = (total_bot / total_crop) if total_crop > 0 else None
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=total_merge_end)
            title_cell = ws.cell(
                r,
                1,
                f"Всего {len(bot_rows)} водителей ({firm})",
            )
            title_cell.font = FIRM_FONT
            title_cell.border = BORDER
            title_cell.alignment = Alignment(vertical="center")
            c_bot = ws.cell(r, bot_col, _format_metric_value(total_bot))
            c_bot.border = BORDER
            c_crop = ws.cell(r, crop_col, _format_metric_value(total_crop))
            c_crop.border = BORDER
            if total_coef is not None:
                c_coef = ws.cell(r, coef_col)
                _write_coef_cell(c_coef, total_coef)
            r += 1

    if METRIC_MODE == "duration":
        widths = [6, 34, 28, 28, 18]
    else:
        widths = [6, 22, 30, 22, 22, 16, 28, 28]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A5"
    return wb


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    _apply_period_from_env()
    win_start, win_end = _resolve_window()
    mode_label = "время остановок" if METRIC_MODE == "duration" else "количество остановок"
    print(f"Рейтинг механизаторов ({mode_label}) за {_fmt_period()}")
    print(
        f"  Период env: DATE_FROM={DATE_FROM} DATE_TO={DATE_TO} "
        f"REPORT27={os.environ.get('REPORT27_DATE_FROM')}..{os.environ.get('REPORT27_DATE_TO')} "
        f"METRIC={METRIC_MODE}",
        flush=True,
    )
    print(f"  Окно: {win_start.isoformat()} — {win_end.isoformat()}")

    print("  Справочники Cropwise...")
    users = get_all("users")
    work_types_list = get_all("work_types")
    work_types = {
        int(w["id"]): (w.get("name") or w.get("title") or "—")
        for w in work_types_list
        if w.get("id") is not None
    }
    users_map = build_users_map(users)
    machine_to_firm = build_machine_to_firm()
    print(f"    машин с регионом: {len(machine_to_firm)}")

    task_fetch_from = win_start - timedelta(days=TASK_LOOKBACK_DAYS)
    print("  Machine tasks...")
    tasks = get_all(
        "machine_tasks",
        {
            "start_time_gt_eq": task_fetch_from.isoformat(),
            "start_time_lt_eq": win_end.isoformat(),
        },
    )
    print(f"    задач (с lookback {TASK_LOOKBACK_DAYS} дн.): {len(tasks)}")

    print("  Простои Cropwise (телеметрия)...")
    crop_counts, crop_secs, work_types_by_key = asyncio.run(
        collect_cropwise_stops(
            tasks, work_types, machine_to_firm, users_map, win_start, win_end,
        )
    )
    print(f"    пар (агрофирма, ФИО) с остановками: {len(crop_counts)}")

    print("  Простои чат-бота...")
    downtime = load_downtime_period(DATE_FROM, DATE_TO, win_start, win_end)
    print(f"    записей: {len(downtime)}")

    rows = aggregate_rows(downtime, crop_counts, crop_secs, work_types_by_key)
    if METRIC_MODE == "duration":
        before = len(rows)
        rows = merge_rows_by_fio(rows)
        print(
            f"  Склейка по ФИО (27.1): {before} → {len(rows)} строк "
            f"(один сотрудник — одна строка, время суммировано)",
            flush=True,
        )
    print(f"  Строк в отчёте: {len(rows)}")

    wb = build_workbook(rows)
    wb.save(OUTPUT_FILE)
    print(f"Готово: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
