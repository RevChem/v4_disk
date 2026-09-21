"""
Meeting report 25: посев, культивация (+ дискование + боронование подсолнечник).

Режимы (SVODKI_REPORT25_MODE):
  morning — совещание 8:30: предыдущий день 06:00 → текущий день 05:59
  day     — совещание 16:30: текущий день 06:00 → 16:00

Посев — все виды посева (4.0 + 4.1).
Культивация — культивация и дискование; боронование только подсолнечник.
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

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

from plans_1 import (
    API_TOKEN,
    BASE_URL,
    FIELD_NAME_FORCE_RULES,
    FIRM_ORDER,
    FIRM_TO_FOLDER,
    FOLDER_TO_FIRM,
    SKIP_CROPS,
    TZ_MSK,
    WORK_GROUPS,
)

REPORT_DATE = date.today()
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

DAY_START_HOUR = 6
DAY_END_HOUR = 16
MORNING_MEETING_HOUR = 8
MORNING_MEETING_MINUTE = 30
DAY_MEETING_HOUR = 16
DAY_MEETING_MINUTE = 30


def _report25_mode() -> str:
    mode = (os.environ.get("SVODKI_REPORT25_MODE") or "").strip().lower()
    if mode in {"morning", "day"}:
        return mode
    return _infer_report25_mode()


def _infer_report25_mode(now_msk: datetime | None = None) -> str:
    """До 08:30 — утреннее совещание, с 08:30 — дневное."""
    now = now_msk or datetime.now(TZ_MSK)
    cutoff = MORNING_MEETING_HOUR * 60 + MORNING_MEETING_MINUTE
    now_min = now.hour * 60 + now.minute
    return "day" if now_min >= cutoff else "morning"


def _output_basename(mode: str) -> str:
    return f"25_meeting_works_{REPORT_DATE}_{mode}.xlsx"


OUTPUT_FILE = _output_basename(_report25_mode())
SEASON_YEAR = REPORT_DATE.year
SEMAPHORE_LIMIT = 20
# Как в 1_main_ammonium__1_.py — порог на mtfmi-запись и фильтр полей.
MIN_ITEM_AREA_HA = 0.5
PERIOD_START_DATE = date(REPORT_DATE.year, 3, 1)

SEED_GROUPS = {"4.0 Посев", "4.1 Посев (овощи)"}
CULT_GROUPS = {"2.11 Культивация", "2.9 Дискование"}
HARROW_GROUPS = {"2.1 Боронование"}

FIRM_LABELS = {
    "АФ Чистопольская": "ЧИСТОПОЛЬСКАЯ",
    "АгроРаздолье": "АГРОРАЗДОЛЬЕ",
    "Аксу-Агро": "АКСУ",
    "Актай": "АКТАЙ",
    "Дрожжаное Рассвет Агро": "ДРА",
    "НурлатАгро": "НУРЛАТ",
    "Лениногорская Птицефабрика": "ЛЕНИНОГОРСКАЯ",
}


session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


def get_all(endpoint: str, params: dict | None = None) -> list[dict]:
    p = dict(params or {})
    results: list[dict] = []
    from_id = 0
    for _ in range(500):
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
        from_id = int(last_id) + 1
    return results


def get_all_page(endpoint: str, params: dict | None = None) -> list[dict]:
    p = dict(params or {})
    p.setdefault("per_page", 1000)
    results: list[dict] = []
    page = 1
    while True:
        r = session.get(f"{BASE_URL}/{endpoint}", params={**p, "page": page}, timeout=120)
        r.raise_for_status()
        body = r.json()
        chunk = body if isinstance(body, list) else body.get("data", [])
        results.extend(chunk)
        if len(chunk) < p["per_page"]:
            break
        page += 1
    return results


async def _fetch_pages_async(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    items: list[dict] = []
    from_id = 0
    while True:
        async with semaphore:
            r = await client.get(url, params={**params, "from_id": from_id, "limit": 1000})
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
        from_id = int(last_id) + 1
    return items


async def fetch_mtfmi_async(task_ids: list[int]) -> dict[int, list[dict]]:
    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
    url = f"{BASE_URL}/machine_task_field_mapping_items"
    async with httpx.AsyncClient(
        headers={"X-User-Api-Token": API_TOKEN},
        timeout=120,
    ) as client:
        results = await asyncio.gather(
            *[_fetch_pages_async(client, url, {"machine_task_id": tid}, sem) for tid in task_ids]
        )
    return {tid: items for tid, items in zip(task_ids, results)}


def parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_MSK)
    return dt.astimezone(TZ_MSK)


def report_window() -> tuple[str, datetime, datetime]:
    mode = _report25_mode()

    if mode == "day":
        start = datetime(
            REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day,
            DAY_START_HOUR, 0, tzinfo=TZ_MSK,
        )
        end = datetime(
            REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day,
            DAY_END_HOUR, 0, tzinfo=TZ_MSK,
        )
        return mode, start, end

    start_date = REPORT_DATE - timedelta(days=1)
    start = datetime(
        start_date.year, start_date.month, start_date.day,
        DAY_START_HOUR, 0, tzinfo=TZ_MSK,
    )
    end = datetime(
        REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day,
        DAY_START_HOUR, 0, tzinfo=TZ_MSK,
    )
    return mode, start, end


def _title_period_text(mode: str, start: datetime, end: datetime) -> str:
    if mode == "morning":
        end_show = end - timedelta(minutes=1)
        return (
            f"с {start.strftime('%H:%M')} {start.strftime('%d.%m')} "
            f"по {end_show.strftime('%H:%M')} {end_show.strftime('%d.%m')}"
        )
    return (
        f"с {start.strftime('%H:%M')} {start.strftime('%d.%m')} "
        f"по {end.strftime('%H:%M')} {end.strftime('%d.%m')}"
    )


def _norm(s: str) -> str:
    return (s or "").strip().casefold().replace(" ", "").replace("\u00a0", "")


def _folder_parent_map(gfs: list[dict]) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
    for gf in gfs:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = gf.get("parent_id") or gf.get("parent_group_folder_id") or gf.get("group_folder_parent_id")
        out[int(fid)] = int(pid) if pid is not None else None
    return out


def _group_folder_name_firm_hints(group_folders: list[dict]) -> dict[int, str]:
    hints: dict[int, str] = {}
    for gf in group_folders:
        fid = gf.get("id")
        if fid is None:
            continue
        blob = _norm(f"{gf.get('name') or ''} {gf.get('title') or ''}")
        if not blob:
            continue
        if any(x in blob for x in ("лениногор", "птицефабрик", "лпф", "lpf")):
            hints[int(fid)] = "Лениногорская Птицефабрика"
    return hints


def _resolve_to_holding_root(
    folder_id: int | None,
    parent_map: dict[int, int | None],
    root_ids: set[int],
) -> int | None:
    if folder_id is None:
        return None
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


def _folder_id_chain(folder_id: int | None, parent_map: dict[int, int | None]) -> list[int]:
    if folder_id is None:
        return []
    out: list[int] = []
    cur = int(folder_id)
    seen: set[int] = set()
    for _ in range(64):
        if cur in seen:
            break
        seen.add(cur)
        out.append(cur)
        nxt = parent_map.get(cur)
        if nxt is None:
            break
        cur = int(nxt)
    return out


def build_field_to_firm(fields_all: list[dict], fgs_all: list[dict], gfs_all: list[dict]) -> dict[int, str]:
    """Привязка поля к агрофирме — как в 1_main_ammonium__1_.py."""
    fgs_map = {fg["id"]: fg for fg in fgs_all}
    parent_map = _folder_parent_map(gfs_all)
    root_ids = set(FIRM_TO_FOLDER.values())
    gf_hints = _group_folder_name_firm_hints(gfs_all)
    out: dict[int, str] = {}

    for field in fields_all:
        fid = field.get("id")
        if fid is None:
            continue
        fgid = field.get("field_group_id")
        raw_folder = field.get("group_folder_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(fgid, {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None
        firm_key = None
        for cid in _folder_id_chain(rint, parent_map):
            if cid in gf_hints:
                firm_key = gf_hints[cid]
                break
        if firm_key is None:
            root = _resolve_to_holding_root(rint, parent_map, root_ids)
            if root is not None:
                firm_key = FOLDER_TO_FIRM.get(root)
        if firm_key:
            out[int(fid)] = firm_key

    for field in fields_all:
        fid = field.get("id")
        if fid is None:
            continue
        blob = _norm(f"{field.get('name') or ''} {field.get('title') or ''}")
        for needle, firm_key in FIELD_NAME_FORCE_RULES:
            if needle in blob:
                out[int(fid)] = firm_key
                break
    return out


def _load_available_field_ids() -> set[int]:
    """Поля available=True в периоде [PERIOD_START_DATE, REPORT_DATE] — как в сводке 1."""
    period_start = PERIOD_START_DATE
    period_end = REPORT_DATE
    available: set[int] = set()
    for row in get_all(
        "inventory_history_items",
        {"historyable_type": "Field"},
    ):
        if not row.get("available"):
            continue
        fid = row.get("historyable_id")
        if fid is None:
            continue
        raw_start = row.get("event_start_at")
        raw_end = row.get("event_end_at")
        try:
            ev_start = date.fromisoformat(str(raw_start)[:10]) if raw_start else None
        except ValueError:
            ev_start = None
        try:
            ev_end = date.fromisoformat(str(raw_end)[:10]) if raw_end else None
        except ValueError:
            ev_end = None
        if ev_start is None:
            continue
        if ev_start <= period_end and (ev_end is None or ev_end >= period_start):
            available.add(int(fid))
    return available


def load_reference_data() -> tuple[dict[int, str], dict[int, str], set[int]]:
    print("  Загружаем поля и культуры...")
    fields_all = get_all("fields")
    fgs_all = get_all("field_groups")
    gfs_all = get_all("group_folders")
    field_to_firm = build_field_to_firm(fields_all, fgs_all, gfs_all)

    crops_raw = get_all_page("crops")
    crop_id_name = {c["id"]: (c.get("name") or c.get("title", "")) for c in crops_raw}
    field_to_crop: dict[int, str] = {}
    for hi in get_all("history_items", {"year": str(SEASON_YEAR)}):
        fid = hi.get("field_id")
        cid = hi.get("crop_id")
        if not (fid and cid):
            continue
        cname = crop_id_name.get(cid, "")
        if cname and cname not in SKIP_CROPS and hi.get("active"):
            field_to_crop[int(fid)] = cname

    print("  Загружаем inventory_history_items (доступные поля)...")
    available_field_ids = _load_available_field_ids()
    print(f"    Доступных полей в сезонном периоде: {len(available_field_ids)}")
    return field_to_firm, field_to_crop, available_field_ids


def _ids_for_groups(group_names: set[str]) -> set[int]:
    ids: set[int] = set()
    for name, group_ids in WORK_GROUPS:
        if name in group_names:
            ids.update(int(x) for x in group_ids)
    return ids


SEED_IDS = _ids_for_groups(SEED_GROUPS)
CULT_IDS = _ids_for_groups(CULT_GROUPS)
HARROW_IDS = _ids_for_groups(HARROW_GROUPS)
ALL_WT_IDS = SEED_IDS | CULT_IDS | HARROW_IDS


def _is_cult_work_type(wt_id: int) -> bool:
    return wt_id in CULT_IDS or wt_id in HARROW_IDS


def operation_for(wt_id: int, crop: str) -> str | None:
    crop_l = (crop or "").casefold()
    if wt_id in SEED_IDS:
        return "Итого посев"
    if wt_id in CULT_IDS:
        return "Итого культивация"
    if wt_id in HARROW_IDS and "подсолнеч" in crop_l:
        return "Итого культивация"
    return None


def _shift_window(end_time_raw) -> tuple[date, datetime, datetime] | None:
    """Смена 06:00–06:00 — как в 1_main_ammonium__1_.py."""
    end_dt = parse_dt(end_time_raw)
    if end_dt is None:
        return None
    cal_date = end_dt.date()
    anchor = datetime(
        cal_date.year, cal_date.month, cal_date.day,
        DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    if end_dt < anchor:
        cal_date = cal_date - timedelta(days=1)
        anchor = datetime(
            cal_date.year, cal_date.month, cal_date.day,
            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
        )
    return cal_date, anchor, anchor + timedelta(days=1)


def _tasks_api_cutoff(mode: str, day_start: datetime, cutoff: datetime) -> datetime:
    """День: до 06:00 след. дня (задания после 16:00). Утро: как 1_main_ammonium."""
    if mode == "day":
        return day_start + timedelta(days=1)
    return cutoff


def _task_in_report_window(
    task: dict,
    day_start: datetime,
    cutoff: datetime,
) -> bool:
    """Смена пересекается с окном [day_start, cutoff) — дневной режим 06:00–16:00."""
    sw = _shift_window(task.get("end_time"))
    if sw is None:
        return False
    _, shift_ds, shift_co = sw
    return shift_ds < cutoff and shift_co > day_start


def _task_in_shift_window(
    task: dict,
    day_start: datetime,
    cutoff: datetime,
) -> bool:
    """Смена целиком в окне [day_start, cutoff) — утро: опрыскивание и культивация."""
    sw = _shift_window(task.get("end_time"))
    if sw is None:
        return False
    _, shift_ds, shift_co = sw
    return shift_ds >= day_start and shift_co <= cutoff


def _seed_task_in_window(
    task: dict,
    day_start: datetime,
    cutoff: datetime,
    mode: str,
) -> bool:
    """
    Посев — как fetch_all_crop_stats в 1_main_dop.py:
    утро: end_time в [day_start, cutoff); день: пересечение смены с окном 06:00–16:00.
    """
    end_dt = parse_dt(task.get("end_time"))
    if end_dt is None:
        return mode == "morning"
    if mode == "day":
        return _task_in_report_window(task, day_start, cutoff)
    return day_start <= end_dt < cutoff


def _cov_hourly_before(hourly: list, before_dt: datetime) -> float:
    cov = 0.0
    for entry in hourly:
        h = parse_dt(entry[0])
        if h and h < before_dt:
            cov += float(entry[1] or 0)
    return cov


def _cov_seeding_item(item: dict, shift_cutoff: datetime) -> float:
    """Площадь посева за сутки — полная смена (h < shift_cutoff), как в 1_main_dop."""
    hourly = item.get("covered_area_hourly") or []
    if hourly:
        return _cov_hourly_before(hourly, shift_cutoff)
    return float(item.get("covered_area") or 0.0)


def _cov_cult_item(
    item: dict,
    cutoff: datetime,
    *,
    day_start: datetime,
) -> float:
    """Культивация, дневной режим: только hourly в окне [day_start, cutoff)."""
    hourly = item.get("covered_area_hourly") or []
    cov = 0.0
    for entry in hourly:
        h = parse_dt(entry[0])
        if not h or h >= cutoff:
            continue
        if h < day_start:
            continue
        cov += float(entry[1] or 0)
    return cov


def _cov_spray_item(
    item: dict,
    cutoff: datetime,
    *,
    day_start: datetime | None = None,
) -> float:
    """
    Опрыскивание и культивация (утро) — как fetch_day_stats_for_work_types:
    h < cutoff без нижней границы, fallback на covered_area;
    день: hourly только в [day_start, cutoff).
    """
    hourly = item.get("covered_area_hourly") or []
    cov = 0.0
    if hourly:
        for entry in hourly:
            h = parse_dt(entry[0])
            if not h or h >= cutoff:
                continue
            if day_start is not None and h < day_start:
                continue
            cov += float(entry[1] or 0)
    elif day_start is None:
        cov = float(item.get("covered_area") or 0.0)
    return cov


def collect_totals(
    field_to_firm: dict[int, str],
    field_to_crop: dict[int, str],
    available_field_ids: set[int],
) -> dict[str, dict[str, float]]:
    mode, day_start, cutoff = report_window()
    api_cutoff = _tasks_api_cutoff(mode, day_start, cutoff)
    season_start_str = datetime(REPORT_DATE.year, 1, 1, tzinfo=TZ_MSK).strftime("%Y-%m-%dT%H:%M:%S")
    api_cutoff_str = api_cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    print(
        f"  Загружаем задания: сезон с {season_start_str}, "
        f"API end_time_lt {api_cutoff_str}, окно "
        f"{day_start:%Y-%m-%d %H:%M} .. {cutoff:%Y-%m-%d %H:%M}, mode={mode}"
    )

    tasks_season: list[dict] = []
    seen_tasks: set[int] = set()
    for wt_id in sorted(ALL_WT_IDS):
        for task in get_all(
            "machine_tasks",
            {
                "work_type_id": wt_id,
                "end_time_gt_eq": season_start_str,
                "end_time_lt": api_cutoff_str,
            },
        ):
            tid = task.get("id")
            if tid is None or int(tid) in seen_tasks:
                continue
            seen_tasks.add(int(tid))
            tasks_season.append(task)

    today_seed_ids: set[int] = set()
    today_cult_ids: set[int] = set()
    no_shift_seed_ids: set[int] = set()

    for task in tasks_season:
        tid = int(task["id"])
        wt_id = int(task.get("work_type_id") or 0)
        if wt_id in SEED_IDS:
            if parse_dt(task.get("end_time")) is None:
                if mode == "morning":
                    no_shift_seed_ids.add(tid)
            elif _seed_task_in_window(task, day_start, cutoff, mode):
                today_seed_ids.add(tid)
        elif _is_cult_work_type(wt_id):
            if mode == "morning":
                if _task_in_shift_window(task, day_start, cutoff):
                    today_cult_ids.add(tid)
            elif _task_in_report_window(task, day_start, cutoff):
                today_cult_ids.add(tid)

    all_today_ids = today_seed_ids | today_cult_ids | no_shift_seed_ids
    print(
        f"  Заданий загружено: {len(tasks_season)}, в окне отчёта: {len(all_today_ids)} "
        f"(посев={len(today_seed_ids)}, посев_без_end={len(no_shift_seed_ids)}, "
        f"культ={len(today_cult_ids)})"
    )
    if not all_today_ids:
        return defaultdict(lambda: defaultdict(float))

    mtfmi_map = asyncio.run(
        fetch_mtfmi_async([int(t["id"]) for t in tasks_season if int(t["id"]) in all_today_ids])
    )
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    def _accumulate(task: dict, cov_fn) -> None:
        tid = int(task["id"])
        wt_id = int(task.get("work_type_id") or 0)
        for item in mtfmi_map.get(tid, []):
            fid = item.get("field_id")
            if not fid:
                continue
            fid_i = int(fid)
            if fid_i not in available_field_ids:
                continue
            if fid_i not in field_to_crop:
                continue
            crop = field_to_crop[fid_i]
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue
            cov = cov_fn(item)
            if cov <= MIN_ITEM_AREA_HA:
                continue
            operation = operation_for(wt_id, crop)
            if operation is None:
                continue
            totals[firm][operation] += cov

    for task in tasks_season:
        tid = int(task["id"])
        wt_id = int(task.get("work_type_id") or 0)

        if tid in no_shift_seed_ids and wt_id in SEED_IDS:
            _accumulate(task, lambda item: float(item.get("covered_area") or 0.0))
            continue

        if tid in today_seed_ids and wt_id in SEED_IDS:
            sw = _shift_window(task.get("end_time"))
            shift_co = sw[2] if sw else cutoff
            if mode == "morning":
                _accumulate(task, lambda item, sc=shift_co: _cov_seeding_item(item, sc))
            else:
                _accumulate(
                    task,
                    lambda item, ds=day_start, cu=cutoff: _cov_cult_item(
                        item, cu, day_start=ds
                    ),
                )
            continue

        if tid in today_cult_ids and _is_cult_work_type(wt_id):
            if mode == "morning":
                _accumulate(
                    task,
                    lambda item, cu=cutoff: _cov_spray_item(item, cu),
                )
            else:
                _accumulate(
                    task,
                    lambda item, ds=day_start, cu=cutoff: _cov_cult_item(
                        item, cu, day_start=ds
                    ),
                )

    return totals


def _fmt_area(value: float) -> str:
    rounded = round(float(value), 1)
    text = f"{rounded:,.1f}".replace(",", " ").replace(".", ",")
    return text


def _cell(
    ws,
    row: int,
    col: int,
    value,
    *,
    bold: bool = False,
    size: int = 14,
    color: str = "000000",
    bg: str | None = None,
    align: str = "center",
    wrap: bool = True,
    border: bool = True,
) -> None:
    c = ws.cell(row=row, column=col, value=value)
    c.font = Font(name="Times New Roman", size=size, bold=bold, color=color)
    c.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wrap)
    if bg:
        c.fill = PatternFill("solid", fgColor=bg)
    if border:
        side = Side(style="thin", color="000000")
        c.border = Border(left=side, right=side, top=side, bottom=side)


def write_workbook(totals: dict[str, dict[str, float]]) -> None:
    mode, start, end = report_window()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сводка 25"
    ws.sheet_view.showGridLines = False

    # Книжная ориентация: ширины подобраны так, чтобы A:C целиком входили
    # на A4 при конвертации Excel -> PDF.
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 18

    title = (
        "Работы по посеву, культивации, дискованию, боронование (подсолнечник) — "
        + _title_period_text(mode, start, end)
        + (
            f" (утреннее совещание {MORNING_MEETING_HOUR:02d}:{MORNING_MEETING_MINUTE:02d})"
            if mode == "morning"
            else f" (дневное совещание {DAY_MEETING_HOUR:02d}:{DAY_MEETING_MINUTE:02d})"
        )
    )
    ws.merge_cells("A1:C1")
    _cell(ws, 1, 1, title, bold=True, size=12, border=False)
    ws.row_dimensions[1].height = 42

    headers = ["ОРГАНИЗАЦИЯ", "ОПЕРАЦИЯ", "Обработанная\nплощадь, га"]
    for col, header in enumerate(headers, start=1):
        _cell(ws, 2, col, header, bold=False, size=13, color="FFFFFF", bg="8093B3")
    ws.row_dimensions[2].height = 50

    operations = ["Итого посев", "Итого культивация"]
    grand = {op: 0.0 for op in operations}
    row = 3
    firms = [f for f in FIRM_ORDER if any(totals.get(f, {}).get(op, 0) > 0 for op in operations)]
    for firm in firms:
        present = [(op, totals.get(firm, {}).get(op, 0.0)) for op in operations if totals.get(firm, {}).get(op, 0) > 0]
        if not present:
            continue
        start_row = row
        for op, area in present:
            _cell(ws, row, 2, op, size=13, align="left")
            _cell(ws, row, 3, _fmt_area(area), bold=True, size=13)
            ws.row_dimensions[row].height = 22
            grand[op] += area
            row += 1
        if row - start_row > 1:
            ws.merge_cells(start_row=start_row, start_column=1, end_row=row - 1, end_column=1)
        _cell(ws, start_row, 1, FIRM_LABELS.get(firm, firm).upper(), size=13)

    row += 1
    for op, label in (
        ("Итого посев", "ОБЩИЙ ПОСЕВ"),
        ("Итого культивация", "ОБЩАЯ КУЛЬТИВАЦИЯ"),
    ):
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        _cell(ws, row, 1, label, bold=True, size=13, align="right", border=False)
        _cell(ws, row, 3, _fmt_area(grand[op]) if grand[op] else "", bold=True, size=13)
        row += 1

    ws.page_setup.orientation = ws.ORIENTATION_PORTRAIT
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = True
    ws.page_margins.left = 0.15
    ws.page_margins.right = 0.15
    ws.page_margins.top = 0.35
    ws.page_margins.bottom = 0.35
    ws.print_area = f"A1:C{row}"

    wb.save(OUTPUT_FILE)
    print(f"\nСохранено: {OUTPUT_FILE}")
    meeting = (
        f"{MORNING_MEETING_HOUR:02d}:{MORNING_MEETING_MINUTE:02d}"
        if mode == "morning"
        else f"{DAY_MEETING_HOUR:02d}:{DAY_MEETING_MINUTE:02d}"
    )
    print(f"  Режим: {mode} (совещание {meeting})")


def build_report() -> None:
    mode, start, end = report_window()
    print(
        f"=== Отчёт 25 ({mode}): {start:%Y-%m-%d %H:%M} — {end:%Y-%m-%d %H:%M} ==="
    )
    field_to_firm, field_to_crop, available_field_ids = load_reference_data()
    totals = collect_totals(field_to_firm, field_to_crop, available_field_ids)
    write_workbook(totals)


if __name__ == "__main__":
    build_report()
