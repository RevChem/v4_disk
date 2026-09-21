"""
Meeting report 25: посев, культивация (+ дискование + боронование подсолнечник), опрыскивание.

Период: смена текущего дня (REPORT_DATE 06:00 МСК -> REPORT_DATE+1 06:00 МСК).
Логика площади — как в 2_main_ideal_compact: covered_area_hourly до cutoff смены,
без разбивки на утреннее/дневное окно.
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
from openpyxl.utils import get_column_letter

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

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

REPORT_DATE = date(2026, 5, 22)
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

OUTPUT_FILE = f"25_meeting_works_{REPORT_DATE}.xlsx"
SEASON_YEAR = REPORT_DATE.year
SEMAPHORE_LIMIT = 20
MIN_ITEM_AREA_HA = 0.5
PERIOD_START_DATE = date(REPORT_DATE.year, 3, 1)

DAY_START_HOUR = 6  # смена: REPORT_DATE 06:00 -> REPORT_DATE+1 06:00

# Группы операций из plans_1
# В посев входит весь посев
SEED_GROUPS   = {"4.0 Посев"}
# В культивацию: культивация, дискование, боронование только подсолнечник
CULT_GROUPS   = {"2.11 Культивация", "2.9 Дискование"}
HARROW_GROUPS = {"2.1 Боронование"}
# В опрыскивание: всё опрыскивание
SPRAY_GROUPS  = {"3.1 Опрыскивание"}

# Подписи агрофирм в отчёте
FIRM_LABELS = {
    "АФ Чистопольская":           "ЧИСТОПОЛЬСКАЯ",
    "АгроРаздолье":               "АГРОРАЗДОЛЬЕ",
    "Аксу-Агро":                  "АКСУ",
    "Актай":                      "АКТАЙ",
    "Дрожжаное Рассвет Агро":     "ДРА",
    "НурлатАгро":                 "НУРЛАТ",
    "Лениногорская Птицефабрика": "ЛЕНИНОГОРСКАЯ",
}

# =============================================================================
# HTTP
# =============================================================================

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


# =============================================================================
# АСИНХРОННАЯ ЗАГРУЗКА MTFMI
# =============================================================================

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


# =============================================================================
# УТИЛИТЫ
# =============================================================================

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


def _norm(s: str) -> str:
    return (s or "").strip().casefold().replace(" ", "").replace("\u00a0", "")


# =============================================================================
# ИЕРАРХИЯ ПАПОК (привязка поля к агрофирме)
# =============================================================================

def _folder_parent_map(gfs: list[dict]) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
    for gf in gfs:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = (gf.get("parent_id")
               or gf.get("parent_group_folder_id")
               or gf.get("group_folder_parent_id"))
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


def build_field_to_firm(
    fields_all: list[dict],
    fgs_all: list[dict],
    gfs_all: list[dict],
) -> dict[int, str]:
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

    # Принудительные правила по имени поля (перезаписывают)
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


# =============================================================================
# ЗАГРУЗКА СПРАВОЧНИКОВ
# =============================================================================

def _load_available_field_ids() -> set[int]:
    """Поля available=True хотя бы в один день периода [PERIOD_START_DATE, REPORT_DATE]."""
    available: set[int] = set()
    for row in get_all("inventory_history_items", {"historyable_type": "Field"}):
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
        if ev_start <= REPORT_DATE and (ev_end is None or ev_end >= PERIOD_START_DATE):
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


# =============================================================================
# НАБОРЫ ID ПО ОПЕРАЦИЯМ (из plans_1.WORK_GROUPS)
# =============================================================================

def _ids_for_groups(group_names: set[str]) -> set[int]:
    ids: set[int] = set()
    for name, group_ids in WORK_GROUPS:
        if name in group_names:
            ids.update(int(x) for x in group_ids)
    return ids


SEED_IDS   = _ids_for_groups(SEED_GROUPS)
CULT_IDS   = _ids_for_groups(CULT_GROUPS)
HARROW_IDS = _ids_for_groups(HARROW_GROUPS)
SPRAY_IDS  = _ids_for_groups(SPRAY_GROUPS)
ALL_WT_IDS = SEED_IDS | CULT_IDS | HARROW_IDS | SPRAY_IDS


def operation_for(wt_id: int, crop: str) -> str | None:
    """
    Посев    — всё из SEED_IDS (включая овощи).
    Культивация — CULT_IDS + HARROW_IDS только для подсолнечника.
    Опрыскивание — всё из SPRAY_IDS.
    """
    if wt_id in SEED_IDS:
        return "Итого посев"
    if wt_id in SPRAY_IDS:
        return "Итого опрыскивание"
    if wt_id in CULT_IDS:
        return "Итого культивация"
    if wt_id in HARROW_IDS and "подсолнеч" in (crop or "").casefold():
        return "Итого культивация"
    return None


# =============================================================================
# ОПРЕДЕЛЕНИЕ СМЕНЫ (как в 2_main_ideal_compact)
# =============================================================================

def _shift_date_and_cutoff(end_time_raw) -> tuple[date, datetime, datetime] | None:
    """
    Возвращает (shift_date, day_start, cutoff).
    Смена дня D = [D 06:00, D+1 06:00).
    Если end_time < D 06:00 — задание относится к смене D-1.
    """
    end_dt = parse_dt(end_time_raw)
    if end_dt is None:
        return None
    cal_date = end_dt.date()
    anchor = datetime(cal_date.year, cal_date.month, cal_date.day,
                      DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    if end_dt < anchor:
        cal_date = cal_date - timedelta(days=1)
        anchor = datetime(cal_date.year, cal_date.month, cal_date.day,
                          DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    day_start = anchor
    cutoff = anchor + timedelta(days=1)
    return cal_date, day_start, cutoff


# =============================================================================
# СБОР ДАННЫХ
# =============================================================================

def collect_totals(
    field_to_firm: dict[int, str],
    field_to_crop: dict[int, str],
    available_field_ids: set[int],
) -> dict[str, dict[str, float]]:
    """
    Собирает суммарные площади за смену REPORT_DATE (06:00 -> 06:00).
    Площадь — covered_area_hourly до cutoff смены, fallback на covered_area
    (как в 2_main_ideal_compact).
    """
    season_start_str = (
        datetime(REPORT_DATE.year, 1, 1, tzinfo=TZ_MSK).strftime("%Y-%m-%dT%H:%M:%S")
    )
    # Запрашиваем до cutoff смены REPORT_DATE (REPORT_DATE+1 06:00)
    api_cutoff = datetime(REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day,
                          DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK) + timedelta(days=1)
    api_cutoff_str = api_cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    print(f"  Загружаем задания: сезон с {season_start_str}, end_time_lt {api_cutoff_str}")

    tasks_season: list[dict] = []
    seen_tasks: set[int] = set()
    for wt_id in sorted(ALL_WT_IDS):
        for task in get_all(
            "machine_tasks",
            {
                "work_type_id":   wt_id,
                "end_time_gt_eq": season_start_str,
                "end_time_lt":    api_cutoff_str,
            },
        ):
            tid = task.get("id")
            if tid is None or int(tid) in seen_tasks:
                continue
            seen_tasks.add(int(tid))
            tasks_season.append(task)

    # Разделяем задания: попавшие в смену REPORT_DATE и без end_time
    today_task_cutoff: dict[int, datetime] = {}  # tid -> cutoff смены
    no_shift_task_ids: set[int] = set()

    for task in tasks_season:
        tid = int(task["id"])
        result = _shift_date_and_cutoff(task.get("end_time"))
        if result is None:
            no_shift_task_ids.add(tid)
        elif result[0] == REPORT_DATE:
            today_task_cutoff[tid] = result[2]

    all_ids = set(today_task_cutoff) | no_shift_task_ids
    print(
        f"  Всего заданий за сезон: {len(tasks_season)}, "
        f"в смене {REPORT_DATE}: {len(today_task_cutoff)}, "
        f"без end_time: {len(no_shift_task_ids)}"
    )

    if not all_ids:
        return defaultdict(lambda: defaultdict(float))

    mtfmi_map = asyncio.run(
        fetch_mtfmi_async([int(t["id"]) for t in tasks_season if int(t["id"]) in all_ids])
    )

    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for task in tasks_season:
        tid = int(task["id"])
        wt_id = int(task.get("work_type_id") or 0)

        if tid not in all_ids:
            continue

        use_fallback = tid in no_shift_task_ids
        task_cutoff = today_task_cutoff.get(tid)

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
            if crop in SKIP_CROPS:
                continue
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue

            # Площадь: hourly до cutoff смены, fallback на covered_area
            if use_fallback:
                cov = float(item.get("covered_area") or 0)
            else:
                hourly = item.get("covered_area_hourly") or []
                cov = 0.0
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and h < task_cutoff:
                        cov += float(entry[1] or 0)
                if not hourly:
                    cov = float(item.get("covered_area") or 0)

            if cov <= MIN_ITEM_AREA_HA:
                continue

            operation = operation_for(wt_id, crop)
            if operation is None:
                continue
            totals[firm][operation] += cov

    return totals


# =============================================================================
# ЗАПИСЬ EXCEL
# =============================================================================

def _fmt_area(value: float) -> str:
    rounded = round(float(value), 1)
    return f"{rounded:,.1f}".replace(",", " ").replace(".", ",")


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
    next_day = REPORT_DATE + timedelta(days=1)
    period_text = (
        f"смена {REPORT_DATE.strftime('%d.%m.%Y')} "
        f"({DAY_START_HOUR:02d}:00 {REPORT_DATE.strftime('%d.%m')} — "
        f"{DAY_START_HOUR:02d}:00 {next_day.strftime('%d.%m')})"
    )
    title = (
        "Работы по посеву, культивации, дискованию, боронование (подсолнечник), опрыскивание — "
        + period_text
    )

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сводка 25"
    ws.sheet_view.showGridLines = False

    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 18

    ws.merge_cells("A1:C1")
    _cell(ws, 1, 1, title, bold=True, size=12, border=False)
    ws.row_dimensions[1].height = 42

    headers = ["ОРГАНИЗАЦИЯ", "ОПЕРАЦИЯ", "Обработанная\nплощадь, га"]
    for col, header in enumerate(headers, start=1):
        _cell(ws, 2, col, header, bold=False, size=13, color="FFFFFF", bg="8093B3")
    ws.row_dimensions[2].height = 50

    operations = ["Итого посев", "Итого культивация", "Итого опрыскивание"]
    grand = {op: 0.0 for op in operations}
    row = 3
    firms = [
        f for f in FIRM_ORDER
        if any(totals.get(f, {}).get(op, 0) > 0 for op in operations)
    ]
    for firm in firms:
        present = [
            (op, totals.get(firm, {}).get(op, 0.0))
            for op in operations
            if totals.get(firm, {}).get(op, 0) > 0
        ]
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
            ws.merge_cells(
                start_row=start_row, start_column=1,
                end_row=row - 1, end_column=1,
            )
        _cell(ws, start_row, 1, FIRM_LABELS.get(firm, firm).upper(), size=13)

    row += 1
    for op, label in (
        ("Итого посев",        "ОБЩИЙ ПОСЕВ"),
        ("Итого культивация",  "ОБЩАЯ КУЛЬТИВАЦИЯ"),
        ("Итого опрыскивание", "ОБЩЕЕ ОПРЫСКИВАНИЕ"),
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


# =============================================================================
# ТОЧКА ВХОДА
# =============================================================================

def build_report() -> None:
    print(f"=== Отчёт 25: посев / культивация / опрыскивание — смена {REPORT_DATE} ===")
    field_to_firm, field_to_crop, available_field_ids = load_reference_data()
    totals = collect_totals(field_to_firm, field_to_crop, available_field_ids)
    write_workbook(totals)


if __name__ == "__main__":
    build_report()