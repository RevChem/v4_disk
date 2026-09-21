"""
Сводка по полевым работам (на дату смены)
==========================================
Один лист Excel со всеми видами работ.

Колонки:
  Агрофирма | Поле | ФИО водителя | Машина | Наименование агрегата |
  Подтип работ | Обработанная площадь, га |
  Норма выработки в час, га | Фактическая выработка в час, га |
  Продолжительность, ч

Данные берутся из того же API и тех же справочников.
Дата отчёта: сегодня (или REPORT_DATE из окружения).
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

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

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

try:
    from plans import (
        API_TOKEN, BASE_URL, TZ_MSK,
        FIRM_ORDER, FIRM_TO_FOLDER, FOLDER_TO_FIRM,
        WORK_GROUPS, SKIP_CROPS, HIRED_GROUP_ID,
        FIELD_NAME_FORCE_RULES,
    )
except ImportError:
    try:
        from plans_1 import (
            API_TOKEN, BASE_URL, TZ_MSK,
            FIRM_ORDER, FIRM_TO_FOLDER, FOLDER_TO_FIRM,
            WORK_GROUPS, SKIP_CROPS, HIRED_GROUP_ID,
            FIELD_NAME_FORCE_RULES,
        )
    except ImportError:
        API_TOKEN      = "6DayNcBDr9BWEZgst1Dy"
        BASE_URL       = "https://operations.cropwise.com/api/v3"
        TZ_MSK         = timezone(timedelta(hours=3))
        HIRED_GROUP_ID = 7
        FIRM_ORDER     = [
            "АФ Чистопольская", "АгроРаздолье", "Аксу-Агро",
            "НурлатАгро", "Дрожжаное Рассвет Агро", "Актай",
            "Лениногорская Птицефабрика",
        ]
        FIRM_TO_FOLDER = {
            "АФ Чистопольская": 3, "АгроРаздолье": 4, "Аксу-Агро": 5,
            "НурлатАгро": 11, "Дрожжаное Рассвет Агро": 6,
            "Актай": 7, "Лениногорская Птицефабрика": 9,
        }
        FOLDER_TO_FIRM     = {v: k for k, v in FIRM_TO_FOLDER.items()}
        WORK_GROUPS        = []
        SKIP_CROPS         = {
            "Залежь, лес", "Залежь", "Введение в оборот 2026 год",
            "Луговые травы", "Обмен", "В пользовании у фермера",
            "Введение в оборот 2027 год", "Введение в оборот",
            "Передано в аренду", "Дороги/лесополосы", "Овраги/неудобия",
        }
        FIELD_NAME_FORCE_RULES = []

# Дата отчёта
REPORT_DATE: date = date.today()
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

DAY_START_HOUR     = 6
SEMAPHORE_LIMIT    = 5
SEASON_YEAR        = REPORT_DATE.year

OUTPUT_FILE = f"svodka_polevye_raboty_{REPORT_DATE}.xlsx"

# =============================================================================
# HTTP
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


def get_all(endpoint: str, params: dict | None = None) -> list[dict]:
    p       = dict(params or {})
    results = []
    from_id = 0
    for _ in range(500):
        r = session.get(f"{BASE_URL}/{endpoint}",
                        params={**p, "from_id": from_id, "limit": 1000},
                        timeout=120)
        r.raise_for_status()
        body  = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        results.extend(chunk)
        meta    = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = last_id + 1
    return results


def get_all_page(endpoint: str, params: dict | None = None) -> list[dict]:
    p = dict(params or {})
    p.setdefault("per_page", 1000)
    results, page = [], 1
    while True:
        r = session.get(f"{BASE_URL}/{endpoint}",
                        params={**p, "page": page}, timeout=120)
        r.raise_for_status()
        body  = r.json()
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
    items, from_id = [], 0
    while True:
        async with semaphore:
            r = await client.get(url, params={**params,
                                              "from_id": from_id,
                                              "limit":   1000})
        body  = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        items.extend(chunk)
        meta    = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = last_id + 1
    return items


async def fetch_mtfmi_async(task_ids: list[int]) -> dict[int, list[dict]]:
    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
    url = f"{BASE_URL}/machine_task_field_mapping_items"
    async with httpx.AsyncClient(
        headers={"X-User-Api-Token": API_TOKEN}, timeout=120
    ) as client:
        results = await asyncio.gather(*[
            _fetch_pages_async(client, url, {"machine_task_id": tid}, sem)
            for tid in task_ids
        ])
    return {tid: items for tid, items in zip(task_ids, results)}

# =============================================================================
# ДАТЫ
# =============================================================================

def parse_dt(s) -> Optional[datetime]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        ts = float(s)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(s).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_MSK)
    else:
        dt = dt.astimezone(TZ_MSK)
    return dt


def shift_window(report_date: date):
    day_start = datetime(report_date.year, report_date.month, report_date.day,
                         DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    cutoff    = day_start + timedelta(days=1)
    return day_start, cutoff

# =============================================================================
# ИЕРАРХИЯ ПАПОК
# =============================================================================

def _norm(s: str) -> str:
    return (s or "").strip().casefold().replace(" ", "").replace("\u00a0", "")


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


def _resolve_firm(folder_id: int | None,
                  parent_map: dict[int, int | None],
                  root_ids: set[int]) -> int | None:
    if folder_id is None:
        return None
    cur  = int(folder_id)
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
    fgs_map    = {int(fg["id"]): fg for fg in fgs_all if fg.get("id") is not None}
    parent_map = _folder_parent_map(gfs_all)
    root_ids   = set(FOLDER_TO_FIRM.keys())

    out: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        raw_folder = f.get("group_folder_id")
        fgid       = f.get("field_group_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(int(fgid), {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None
        root = _resolve_firm(rint, parent_map, root_ids)
        if root is not None:
            firm = FOLDER_TO_FIRM.get(root)
            if firm:
                out[int(fid)] = firm

    for f in fields_all:
        fid  = f.get("id")
        blob = _norm(f"{f.get('name') or ''} {f.get('title') or ''}")
        for needle, firm_key in FIELD_NAME_FORCE_RULES:
            if needle in blob:
                out[int(fid)] = firm_key
                break
    return out

# =============================================================================
# ЗАГРУЗКА СПРАВОЧНИКОВ
# =============================================================================

def load_reference_data():
    print("  Поля...")
    fields_all = get_all("fields")
    fgs_all    = get_all_page("field_groups")   # как в field.py — постраничная загрузка
    gfs_all    = get_all("group_folders")

    field_name: dict[int, str]   = {}
    field_area: dict[int, float] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        fid = int(fid)
        field_name[fid] = f.get("name") or f.get("title") or f"field_{fid}"
        field_area[fid] = float(f.get("tillable_area") or f.get("calculated_area") or 0.0)

    field_to_firm = build_field_to_firm(fields_all, fgs_all, gfs_all)

    print("  Культуры...")
    crops_raw    = get_all_page("crops")
    crop_id_name = {c["id"]: (c.get("name") or c.get("title", "")) for c in crops_raw}
    his_all      = get_all("history_items", {"year": str(SEASON_YEAR)})
    field_to_crop: dict[int, str] = {}
    for hi in his_all:
        fid = hi.get("field_id"); cid = hi.get("crop_id")
        if not (fid and cid):
            continue
        cname = crop_id_name.get(cid, "")
        if not cname or cname in SKIP_CROPS:
            continue
        if hi.get("active"):
            field_to_crop[fid] = cname

    print("  Машины...")
    machines_all   = get_all("machines")
    machine_name:  dict[int, str] = {}
    for m in machines_all:
        mid = m.get("id")
        if mid is None:
            continue
        machine_name[mid] = (m.get("name") or m.get("title") or
                             m.get("model") or f"machine_{mid}")

    print("  Водители (users с driver=True)...")
    operators_map: dict[int, str] = {}
    try:
        users_raw = get_all("users") or get_all_page("users")
        for u in users_raw:
            uid = u.get("id")
            if uid is None:
                continue
            # username хранит ФИО водителя (подтверждено диагностикой API)
            display = u.get("username") or u.get("email") or f"user_{uid}"
            operators_map[int(uid)] = display
        drivers_count = sum(1 for u in users_raw if u.get("driver") is True)
        print(f"    Пользователей загружено: {len(operators_map)}, из них водителей: {drivers_count}")
    except Exception as e:
        print(f"    Не удалось загрузить /users: {e}")

    print("  Орудия (implements)...")
    impl_raw = get_all("implements") or get_all_page("implements")
    implements_map: dict[int, dict] = {
        impl["id"]: impl for impl in impl_raw if impl.get("id") is not None
    }
    print(f"    Орудий: {len(implements_map)}")

    print("  Виды работ...")
    wt_raw = get_all("work_types") or get_all_page("work_types")
    work_type_map: dict[int, str] = {
        w["id"]: (w.get("name") or w.get("title") or f"wt_{w['id']}")
        for w in wt_raw
    }

    return (field_name, field_area, field_to_firm, field_to_crop,
            machine_name, operators_map, implements_map, work_type_map)

# =============================================================================
# СБОР ДАННЫХ ЗА СМЕНУ
# =============================================================================

def collect_data(
    field_name:    dict[int, str],
    field_area:    dict[int, float],
    field_to_firm: dict[int, str],
    field_to_crop: dict[int, str],
    machine_name:  dict[int, str],
    operators_map: dict[int, str],
    implements_map: dict[int, dict],
    work_type_map: dict[int, str],
) -> list[dict]:
    """Возвращает все строки отчёта — все виды работ за смену."""

    day_start, cutoff = shift_window(REPORT_DATE)
    day_start_str     = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str        = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    print(f"\n  Загружаем machine_tasks ({day_start_str} — {cutoff_str})...")

    # Собираем все задачи за смену
    if WORK_GROUPS:
        all_tasks: list[dict] = []
        seen_wt: set[int] = set()
        for _grp, wt_ids in WORK_GROUPS:
            for wt_id in wt_ids:
                if wt_id in seen_wt:
                    continue
                seen_wt.add(wt_id)
                tasks = get_all("machine_tasks", {
                    "work_type_id":   wt_id,
                    "end_time_gt_eq": day_start_str,
                    "end_time_lt":    cutoff_str,
                })
                all_tasks.extend(tasks)
    else:
        all_tasks = get_all("machine_tasks", {
            "end_time_gt_eq": day_start_str,
            "end_time_lt":    cutoff_str,
        })

    # Дедупликация
    seen_ids: set[int] = set()
    unique: list[dict] = []
    for t in all_tasks:
        tid = t.get("id")
        if tid not in seen_ids:
            seen_ids.add(tid)
            unique.append(t)
    all_tasks = unique
    print(f"    Задач: {len(all_tasks)}")

    if not all_tasks:
        return []

    # MTFMI
    task_ids  = [t["id"] for t in all_tasks]
    mtfmi_map = asyncio.run(fetch_mtfmi_async(task_ids))

    rows: list[dict] = []

    for task in all_tasks:
        tid     = task["id"]
        mid     = task.get("machine_id")
        wt_id   = task.get("work_type_id")
        wt_name = work_type_map.get(wt_id, "—") if wt_id else "—"
        mach    = machine_name.get(mid, f"machine_{mid}") if mid else "—"

        # Водитель — поле driver_id (подтверждено диагностикой API)
        op_id_raw = task.get("driver_id")
        try:
            op_id = int(op_id_raw) if op_id_raw is not None else None
        except (TypeError, ValueError):
            op_id = None
        operator_name = operators_map.get(op_id, "—") if op_id else "—"

        # Орудие (наименование агрегата)
        impl_id  = task.get("implement_id")
        impl_rec = implements_map.get(impl_id) if impl_id else None
        implement_name = "—"
        if impl_rec:
            implement_name = str(
                impl_rec.get("custom_name") or impl_rec.get("name") or "—"
            ).strip() or "—"

        # Норма выработки в час — кастомное поле x_custom_norma_vyseva
        norm_raw = (task.get("x_custom_norma_vyseva")
                    or task.get("planned_productivity")
                    or task.get("production_rate"))
        try:
            norm_ha_h = round(float(norm_raw), 2) if norm_raw is not None else None
        except (TypeError, ValueError):
            norm_ha_h = None

        t_start = parse_dt(task.get("start_time") or task.get("started_at"))
        t_end   = parse_dt(task.get("end_time")   or task.get("finished_at"))

        # Длительность в часах (обрезаем по окну смены)
        duration_h = 0.0
        if t_start and t_end and t_end > t_start:
            s = max(t_start, day_start)
            e = min(t_end, cutoff)
            if e > s:
                duration_h = (e - s).total_seconds() / 3600.0

        items_for_task = mtfmi_map.get(tid, [])

        # Площадь по полям (fid приводим к int — как в new_1.py)
        field_cov: dict[int, float] = defaultdict(float)
        for item in items_for_task:
            fid_raw = item.get("field_id")
            if not fid_raw:
                continue
            fid = int(fid_raw)
            hourly = item.get("covered_area_hourly") or []
            cov = 0.0
            if hourly:
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and day_start <= h < cutoff:
                        cov += entry[1] or 0
            # Если hourly пустой или все записи вне окна — берём covered_area как запасной вариант
            if cov == 0:
                cov = float(item.get("covered_area") or 0)
            if cov > 0:
                field_cov[fid] += cov

        # Длительность из mtfmi по полю (для точности)
        mtfmi_duration: dict[int, float] = defaultdict(float)
        for item in items_for_task:
            fid_raw = item.get("field_id")
            if not fid_raw:
                continue
            fid = int(fid_raw)
            s_raw = item.get("start_time") or item.get("started_at")
            e_raw = item.get("end_time")   or item.get("finished_at")
            s_dt  = parse_dt(s_raw)
            e_dt  = parse_dt(e_raw)
            if s_dt and e_dt and e_dt > s_dt:
                s_cl = max(s_dt, day_start)
                e_cl = min(e_dt, cutoff)
                if e_cl > s_cl:
                    mtfmi_duration[fid] += (e_cl - s_cl).total_seconds() / 3600.0

        # Если field_cov пуст — пробуем взять field_id напрямую из задачи
        if not field_cov:
            direct_fid = task.get("field_id")
            if direct_fid:
                area_direct = float(task.get("completed_area") or task.get("covered_area") or 0)
                field_cov[int(direct_fid)] = area_direct

        if field_cov:
            for fid, area in field_cov.items():
                firm  = field_to_firm.get(fid, "—")
                crop  = field_to_crop.get(fid, "—")
                if crop in SKIP_CROPS:
                    continue
                fname = field_name.get(fid, f"field_{fid}")
                dur   = mtfmi_duration.get(fid) or duration_h
                # Фактическая выработка в час
                fact_ha_h = round(area / dur, 2) if dur > 0 and area > 0 else None
                rows.append({
                    "firm":       firm,
                    "field":      fname,
                    "operator":   operator_name,
                    "machine":    mach,
                    "implement":  implement_name,
                    "work_type":  wt_name,
                    "area":       round(area, 2),
                    "norm_ha_h":  norm_ha_h,
                    "fact_ha_h":  fact_ha_h,
                    "duration":   round(dur, 4),
                })
        else:
            fact_ha_h = None
            rows.append({
                "firm":       "—",
                "field":      "—",
                "operator":   operator_name,
                "machine":    mach,
                "implement":  implement_name,
                "work_type":  wt_name,
                "area":       0.0,
                "norm_ha_h":  norm_ha_h,
                "fact_ha_h":  fact_ha_h,
                "duration":   round(duration_h, 4),
            })

    return rows

# =============================================================================
# СТИЛИ
# =============================================================================

_C_HDR   = "2E4DA0"
_C_TITLE = "1E3A6E"
_C_TOTAL = "BDD7EE"
_C_ODD   = "FFFFFF"
_C_EVEN  = "EEF4FB"
_C_FIRM  = "D9E1F2"


def _side() -> Border:
    t = Side(style="thin", color="BBBBBB")
    return Border(left=t, right=t, top=t, bottom=t)


def _cell(ws, row: int, col: int, value=None,
          bold=False, bg: str | None = None, color="000000",
          halign="left", wrap=False, fmt: str | None = None,
          size=9) -> None:
    c = ws.cell(row=row, column=col, value=value)
    c.font      = Font(name="Calibri", size=size, bold=bold, color=color)
    c.alignment = Alignment(horizontal=halign, vertical="center",
                            wrap_text=wrap)
    c.border    = _side()
    if bg:
        c.fill = PatternFill("solid", fgColor=bg)
    if fmt:
        c.number_format = fmt


HEADERS = [
    "Агрофирма",
    "Поле",
    "ФИО водителя",
    "Машина",
    "Наименование агрегата",
    "Подтип работ",
    "Обработанная\nплощадь, га",
    "Норма выработки\nв час, га",
    "Факт. выработка\nв час, га",
    "Продолжительность, ч",
]
COL_W = [22, 16, 22, 28, 24, 34, 13, 14, 14, 16]

# =============================================================================
# ЗАПИСЬ ЛИСТА
# =============================================================================

def write_sheet(wb, sheet_name: str, rows: list[dict], title: str) -> None:
    ws = wb.create_sheet(title=sheet_name[:31])

    for ci, w in enumerate(COL_W, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    r = 1
    # Заголовок
    ws.merge_cells(start_row=r, start_column=1,
                   end_row=r, end_column=len(HEADERS))
    c = ws.cell(row=r, column=1,
                value=f"{title}  |  {REPORT_DATE.strftime('%d.%m.%Y')}")
    c.font      = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill      = PatternFill("solid", fgColor=_C_TITLE)
    c.border    = _side()
    ws.row_dimensions[r].height = 26
    r += 1

    # Шапка
    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, r, ci, h, bold=True, bg=_C_HDR, color="FFFFFF",
              halign="center", wrap=True, size=9)
    ws.row_dimensions[r].height = 36
    ws.freeze_panes = f"A{r+1}"
    r += 1

    # Сортировка: по агрофирме (FIRM_ORDER), затем поле, затем машина
    def sort_key(row):
        fi = FIRM_ORDER.index(row["firm"]) if row["firm"] in FIRM_ORDER else 999
        return (fi, row["field"], row["machine"])

    rows_sorted = sorted(rows, key=sort_key)

    current_firm   = None
    firm_area_sum  = 0.0
    firm_dur_sum   = 0.0

    totals_area = 0.0
    totals_dur  = 0.0

    def write_firm_total(ws, end_r, firm, area, dur):
        _cell(ws, end_r, 1, f"Итого {firm}", bold=True, bg=_C_FIRM,
              halign="right", size=9)
        for ci in range(2, 7):
            _cell(ws, end_r, ci, "", bg=_C_FIRM)
        _cell(ws, end_r, 7, round(area, 2), bold=True, bg=_C_FIRM,
              halign="right", fmt="# ##0.00", size=9)
        _cell(ws, end_r, 8, "", bg=_C_FIRM)
        _cell(ws, end_r, 9, "", bg=_C_FIRM)
        _cell(ws, end_r, 10, round(dur, 4), bold=True, bg=_C_FIRM,
              halign="right", fmt="# ##0.00", size=9)
        ws.row_dimensions[end_r].height = 14

    for i, row in enumerate(rows_sorted):
        firm = row["firm"]
        if firm != current_firm:
            if current_firm is not None:
                write_firm_total(ws, r, current_firm, firm_area_sum, firm_dur_sum)
                r += 1
            current_firm  = firm
            firm_area_sum = 0.0
            firm_dur_sum  = 0.0

        bg = _C_ODD if i % 2 == 0 else _C_EVEN
        norm_val = row["norm_ha_h"]
        fact_val = row["fact_ha_h"]
        _cell(ws, r, 1,  row["firm"],      bg=bg, halign="left")
        _cell(ws, r, 2,  row["field"],     bg=bg, halign="left")
        _cell(ws, r, 3,  row["operator"],  bg=bg, halign="left")
        _cell(ws, r, 4,  row["machine"],   bg=bg, halign="left", size=8)
        _cell(ws, r, 5,  row["implement"], bg=bg, halign="left", size=8)
        _cell(ws, r, 6,  row["work_type"], bg=bg, halign="left")
        _cell(ws, r, 7,  row["area"],      bg=bg, halign="right", fmt="# ##0.00")
        _cell(ws, r, 8,  norm_val if norm_val is not None else "—",
              bg=bg, halign="right",
              fmt="# ##0.00" if norm_val is not None else None)
        _cell(ws, r, 9,  fact_val if fact_val is not None else "—",
              bg=bg, halign="right",
              fmt="# ##0.00" if fact_val is not None else None)
        _cell(ws, r, 10, row["duration"],  bg=bg, halign="right", fmt="# ##0.00")
        ws.row_dimensions[r].height = 14

        firm_area_sum += row["area"]
        firm_dur_sum  += row["duration"]
        totals_area   += row["area"]
        totals_dur    += row["duration"]
        r += 1

    # Итог последней агрофирмы
    if current_firm is not None:
        write_firm_total(ws, r, current_firm, firm_area_sum, firm_dur_sum)
        r += 1

    # Итоговая строка
    _cell(ws, r, 1, "ИТОГО", bold=True, bg=_C_TOTAL, halign="right", size=10)
    for ci in range(2, 7):
        _cell(ws, r, ci, "", bg=_C_TOTAL)
    _cell(ws, r, 7, round(totals_area, 2), bold=True, bg=_C_TOTAL,
          halign="right", fmt="# ##0.00", size=10)
    _cell(ws, r, 8, "", bg=_C_TOTAL)
    _cell(ws, r, 9, "", bg=_C_TOTAL)
    _cell(ws, r, 10, round(totals_dur, 2), bold=True, bg=_C_TOTAL,
          halign="right", fmt="# ##0.00", size=10)
    ws.row_dimensions[r].height = 16

    print(f"  Лист «{sheet_name}»: {len(rows)} строк, "
          f"площадь {round(totals_area, 1)} га, "
          f"время {round(totals_dur, 1)} ч")

# =============================================================================
# MAIN
# =============================================================================

def build_report() -> None:
    print(f"=== Сводка полевых работ  |  {REPORT_DATE} ===\n")

    (field_name, field_area, field_to_firm, field_to_crop,
     machine_name, operators_map, implements_map, work_type_map) = load_reference_data()

    rows = collect_data(
        field_name, field_area, field_to_firm, field_to_crop,
        machine_name, operators_map, implements_map, work_type_map,
    )

    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    write_sheet(wb, "Полевые работы",
                rows,
                "Полевые работы — сводка по полям за смену")

    wb.save(OUTPUT_FILE)
    print(f"\n✓ Сохранено: {OUTPUT_FILE}")


if __name__ == "__main__":
    build_report()