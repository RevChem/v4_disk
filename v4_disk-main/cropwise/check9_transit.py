"""
check2_probable_transit.py
==========================
Отчёт: «Вероятный транзит»

Логика:
  Агро-задание попадает в отчёт, если ОДНОВРЕМЕННО:
    1. Вид работ НЕ входит в _EXCLUDED_GROUP_NAMES
       (вспашка, гребнеобразование, глубокорыхление, посев овощи, уборка)
    2. Суммарная обработанная площадь по ВСЕМ полям задания < AREA_THRESHOLD_HA (5 га)
    3. Ни один из machine_task_field_mapping_items НЕ имеет in_transit=True

Транзит определяется по флагу item['in_transit'] == True
(выяснено из дампа задачи 384511 от 28.05.2026).

Только агро-задания — машина НЕ является грузовой.
Транспортные виды работ (7.x / 8.x) исключены.

Колонки:
  Агрофирма | Машина | ФИО водителя | Вид работ |
  Начало задания | Окончание задания | Площадь (га) | Ссылка на задание

Excel обновляется каждый час в бесконечном цикле.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
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
        HIRED_GROUP_ID,
    )
except ImportError:
    try:
        from plans_1 import (
            API_TOKEN, BASE_URL, TZ_MSK,
            FIRM_ORDER, FIRM_TO_FOLDER, FOLDER_TO_FIRM,
            HIRED_GROUP_ID,
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
        FOLDER_TO_FIRM = {v: k for k, v in FIRM_TO_FOLDER.items()}

REPORT_DATE: date = date.today()
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

SEMAPHORE_LIMIT = 5
UPDATE_INTERVAL = 3600  # 1 час

OUTPUT_FILE = f"check2_probable_transit_{REPORT_DATE}.xlsx"

CROPWISE_TASK_BASE = "https://operations.cropwise.com/machines"

# =============================================================================
# ВИДЫ РАБОТ И ИСКЛЮЧЕНИЯ
# =============================================================================

WORK_GROUPS: list[tuple[str, list[int]]] = [
    ("1.1 Внесение минеральных удобрений", [55]),
    ("2.1 Боронование",                    [103, 81, 97, 12]),
    ("2.9 Дискование",                     [60]),
    ("2.4 Вспашка",                        [49, 57, 171]),
    ("2.11 Культивация",                   [20, 50, 52, 63, 96]),
    ("4.0 Посев",                          [3, 4, 69, 70, 71, 72, 119, 120, 159, 160, 161, 163]),
    ("4.1 Посев (овощи)",                  [68, 148, 172]),
    ("3.1 Опрыскивание",                   [1, 64, 65, 66, 153]),
    ("2.12 Прикатывание",                  [13, 67, 142]),
    ("2.7 Гребнеобразование",              [62]),
    ("2.6 Фрезерование",                   [73]),
    ("2.8 Глубокорыхление",                [11, 106, 121, 150]),
    ("5.0 Уборка",                         [6, 7, 75, 76, 77, 111, 118, 125, 126, 145]),
    ("5.4 Уборка картофеля, лука",         [76]),
]

# Виды работ, для которых проверка НЕ применяется
_EXCLUDED_GROUP_NAMES: set[str] = {
    "2.4 Вспашка",
    "2.7 Гребнеобразование",
    "2.8 Глубокорыхление",
    "4.1 Посев (овощи)",
    "5.0 Уборка",
    "5.4 Уборка картофеля, лука",
}
EXCLUDED_WORK_TYPE_IDS: frozenset[int] = frozenset(
    wt_id
    for name, ids in WORK_GROUPS
    if name in _EXCLUDED_GROUP_NAMES
    for wt_id in ids
)

# Порог площади: задание с суммарной площадью СТРОГО НИЖЕ порога — вероятный транзит
AREA_THRESHOLD_HA = 5.0
# Нижняя граница: задания с площадью НЕ ВЫШЕ этого порога игнорируем (нулевые и почти нулевые)
AREA_MIN_HA = 0.1

# =============================================================================
# ПРИЗНАК ГРУЗОВОЙ МАШИНЫ (TODO: уточнить)
# =============================================================================

def is_truck(machine: dict) -> bool:
    return False  # TODO: заменить на реальную логику

# =============================================================================
# HTTP
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


def get_all(endpoint: str, params: dict | None = None) -> list[dict]:
    p, results, from_id = dict(params or {}), [], 0
    for _ in range(500):
        r = session.get(
            f"{BASE_URL}/{endpoint}",
            params={**p, "from_id": from_id, "limit": 1000},
            timeout=120,
        )
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
        r = session.get(
            f"{BASE_URL}/{endpoint}",
            params={**p, "page": page},
            timeout=120,
        )
        r.raise_for_status()
        body  = r.json()
        chunk = body if isinstance(body, list) else body.get("data", [])
        results.extend(chunk)
        if len(chunk) < p["per_page"]:
            break
        page += 1
    return results


async def _fetch_mtfmi_one(
    client: httpx.AsyncClient,
    task_id: int,
    semaphore: asyncio.Semaphore,
) -> tuple[int, list[dict]]:
    items, from_id = [], 0
    url = f"{BASE_URL}/machine_task_field_mapping_items"
    while True:
        async with semaphore:
            r = await client.get(
                url,
                params={"machine_task_id": task_id, "from_id": from_id, "limit": 1000},
            )
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
    return task_id, items


async def fetch_mtfmi_async(task_ids: list[int]) -> dict[int, list[dict]]:
    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
    async with httpx.AsyncClient(
        headers={"X-User-Api-Token": API_TOKEN}, timeout=120
    ) as client:
        results = await asyncio.gather(*[
            _fetch_mtfmi_one(client, tid, sem) for tid in task_ids
        ])
    return dict(results)

# =============================================================================
# ДАТА / ВРЕМЯ
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
        except Exception:
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
    now          = datetime.now(tz=TZ_MSK)
    window_start = now - timedelta(hours=4)
    cutoff       = now
    return window_start, cutoff

# =============================================================================
# ИЕРАРХИЯ ПАПОК
# =============================================================================

def _folder_parent_map(gfs: list[dict]) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
    for gf in gfs:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = (gf.get("parent_id") or gf.get("parent_group_folder_id")
               or gf.get("group_folder_parent_id"))
        out[int(fid)] = int(pid) if pid is not None else None
    return out


def _resolve_firm(folder_id, parent_map, root_ids):
    if folder_id is None:
        return None
    cur, seen = int(folder_id), set()
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
        fgid = f.get("field_group_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(int(fgid), {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None
        root = _resolve_firm(rint, parent_map, root_ids)
        if root is not None:
            firm = FOLDER_TO_FIRM.get(root)
            if firm:
                out[int(fid)] = firm
    return out

# =============================================================================
# ЗАГРУЗКА СПРАВОЧНИКОВ
# =============================================================================

def load_reference_data():
    print("  Поля...")
    fields_all = get_all("fields")
    fgs_all    = get_all_page("field_groups")
    gfs_all    = get_all("group_folders")
    field_to_firm = build_field_to_firm(fields_all, fgs_all, gfs_all)

    print("  Машины...")
    machines_raw = get_all("machines")
    machine_name: dict[int, str] = {}
    machine_is_truck: dict[int, bool] = {}
    for m in machines_raw:
        mid = m.get("id")
        if mid is None:
            continue
        machine_name[int(mid)] = (
            m.get("name") or m.get("title") or m.get("model") or f"machine_{mid}"
        )
        machine_is_truck[int(mid)] = is_truck(m)

    print("  Водители...")
    operators_map: dict[int, str] = {}
    try:
        users_raw = get_all("users") or get_all_page("users")
        for u in users_raw:
            uid = u.get("id")
            if uid is None:
                continue
            operators_map[int(uid)] = (
                u.get("username") or u.get("email") or f"user_{uid}"
            )
    except Exception as e:
        print(f"    Не удалось загрузить /users: {e}")

    print("  Виды работ...")
    wt_raw = get_all("work_types") or get_all_page("work_types")
    work_type_map: dict[int, str] = {
        w["id"]: (w.get("name") or w.get("title") or f"wt_{w['id']}")
        for w in wt_raw
    }

    return (field_to_firm, machine_name, machine_is_truck, operators_map, work_type_map)

# =============================================================================
# СБОР ДАННЫХ
# =============================================================================

def collect_data(
    field_to_firm: dict,
    machine_name: dict,
    machine_is_truck: dict,
    operators_map: dict,
    work_type_map: dict,
) -> list[dict]:

    day_start, cutoff = shift_window(REPORT_DATE)
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str    = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    print(f"\n  Загружаем machine_tasks ({day_start_str} — {cutoff_str})...")
    all_tasks = get_all("machine_tasks", {
        "start_time_gt_eq": day_start_str,
        "start_time_lt":    cutoff_str,
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
    print(f"    Задач всего: {len(all_tasks)}")

    # Только агро (не грузовые)
    agro_tasks = [
        t for t in all_tasks
        if not machine_is_truck.get(
            int(t["machine_id"]) if t.get("machine_id") else -1, False
        )
    ]
    print(f"    Агро задач: {len(agro_tasks)}")

    # Исключаем транспортные виды работ 7.x / 8.x
    def _is_transport_work_type(task: dict) -> bool:
        wt_id   = task.get("work_type_id")
        wt_name = work_type_map.get(wt_id, "") if wt_id else ""
        return wt_name.strip().startswith(("7", "8"))

    agro_tasks = [t for t in agro_tasks if not _is_transport_work_type(t)]
    print(f"    Агро задач (без 7.x / 8.x): {len(agro_tasks)}")

    # Исключаем виды работ, для которых проверка площадью не применяется
    agro_tasks = [
        t for t in agro_tasks
        if t.get("work_type_id") not in EXCLUDED_WORK_TYPE_IDS
    ]
    print(f"    Агро задач (после исключения освобождённых видов): {len(agro_tasks)}")

    if not agro_tasks:
        return []

    # Загружаем маппинги к полям
    task_ids  = [t["id"] for t in agro_tasks]
    mtfmi_map = asyncio.run(fetch_mtfmi_async(task_ids))

    rows: list[dict] = []

    for task in agro_tasks:
        tid   = task["id"]
        mid   = task.get("machine_id")
        wt_id = task.get("work_type_id")

        wt_name       = work_type_map.get(wt_id, "—") if wt_id else "—"
        mach          = machine_name.get(int(mid), f"machine_{mid}") if mid else "—"

        try:
            op_id = int(task["driver_id"]) if task.get("driver_id") is not None else None
        except (TypeError, ValueError):
            op_id = None
        operator_name = operators_map.get(op_id, "—") if op_id else "—"

        items_for_task = mtfmi_map.get(tid, [])

        # ── Ключевая логика ──────────────────────────────────────────────────
        #
        # Транзит считается ПРОСТАВЛЕННЫМ, если хотя бы один item
        # содержит in_transit=True.
        #
        # Суммарная площадь — сумма covered_area по всем items.
        # (Здесь берём полное значение covered_area, без обрезки по hourly,
        #  потому что нас интересует итог задания, а не последние 4 часа.)
        #
        has_transit        = False
        total_covered_area = 0.0
        firm_from_field    = "—"

        for item in items_for_task:
            # Проверяем флаг транзита
            if item.get("in_transit") is True:
                has_transit = True

            # Суммируем площадь
            fid_raw = item.get("field_id")
            if not fid_raw:
                continue
            fid = int(fid_raw)
            cov = float(item.get("covered_area") or 0)
            total_covered_area += cov

            # Запоминаем агрофирму по первому полю с ненулевой площадью
            if firm_from_field == "—" and cov > 0:
                firm_from_field = field_to_firm.get(fid, "—")

        # Fallback: агрофирма из прямого field_id задания
        if firm_from_field == "—":
            direct_fid = task.get("field_id")
            if direct_fid:
                firm_from_field = field_to_firm.get(int(direct_fid), "—")

        # ── Условие попадания в отчёт ────────────────────────────────────────
        #
        # Вероятный транзит:
        #   0.1 га < площадь < AREA_THRESHOLD_HA  И  ни один item не имеет in_transit=True
        #
        if has_transit or total_covered_area >= AREA_THRESHOLD_HA or total_covered_area <= AREA_MIN_HA:
            continue
        # ────────────────────────────────────────────────────────────────────

        start_dt = parse_dt(task.get("start_time"))
        end_dt   = parse_dt(task.get("end_time"))

        link = ""
        if mid and tid:
            link = f"{CROPWISE_TASK_BASE}/{mid}/tasks/{tid}"

        rows.append({
            "firm":       firm_from_field,
            "machine":    mach,
            "operator":   operator_name,
            "work_type":  wt_name,
            "start":      start_dt.strftime("%d.%m %H:%M") if start_dt else "—",
            "end":        end_dt.strftime("%d.%m %H:%M") if end_dt else "—",
            "area":       round(total_covered_area, 2),
            "task_id":    tid,
            "machine_id": mid,
            "link":       link,
        })

    print(f"    Строк (вероятный транзит — {AREA_MIN_HA} га < площадь < {AREA_THRESHOLD_HA} га, без in_transit): {len(rows)}")
    return rows

# =============================================================================
# СТИЛИ
# =============================================================================

_C_HDR   = "2E4DA0"
_C_TITLE = "7B2C2C"
_C_TOTAL = "BDD7EE"
_C_ODD   = "FFFFFF"
_C_EVEN  = "FFF2CC"
_C_FIRM  = "D9E1F2"

HEADERS = [
    "Агрофирма",
    "Машина",
    "ФИО водителя",
    "Вид работ",
    "Начало задания",
    "Окончание задания",
    "Площадь (га)",
    "Ссылка на задание",
]
COL_W = [22, 28, 24, 34, 14, 14, 12, 18]


def _side() -> Border:
    t = Side(style="thin", color="BBBBBB")
    return Border(left=t, right=t, top=t, bottom=t)


def _cell(ws, row: int, col: int, value=None,
          bold=False, bg: str | None = None, color="000000",
          halign="left", wrap=False, fmt: str | None = None,
          size=9, hyperlink: str | None = None) -> None:
    c = ws.cell(row=row, column=col, value=value)
    c.font      = Font(name="Calibri", size=size, bold=bold,
                       color="1A6ABF" if hyperlink else color,
                       underline="single" if hyperlink else None)
    c.alignment = Alignment(horizontal=halign, vertical="center", wrap_text=wrap)
    c.border    = _side()
    if bg:
        c.fill = PatternFill("solid", fgColor=bg)
    if fmt:
        c.number_format = fmt
    if hyperlink:
        c.hyperlink = hyperlink

# =============================================================================
# ЗАПИСЬ EXCEL
# =============================================================================

def write_sheet(wb, sheet_name: str, rows: list[dict], generated_at: datetime) -> None:
    ws = wb.create_sheet(title=sheet_name[:31])

    for ci, w in enumerate(COL_W, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    r = 1
    ts_str = generated_at.strftime("%d.%m.%Y %H:%M")
    title_text = (
        f"Вероятный транзит  |  "
        f"{REPORT_DATE.strftime('%d.%m.%Y')}  |  обновлено {ts_str}"
    )
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
    c = ws.cell(row=r, column=1, value=title_text)
    c.font      = Font(name="Calibri", size=12, bold=True, color="FFFFFF")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill      = PatternFill("solid", fgColor=_C_TITLE)
    c.border    = _side()
    ws.row_dimensions[r].height = 26
    r += 1

    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, r, ci, h, bold=True, bg=_C_HDR, color="FFFFFF",
              halign="center", wrap=True, size=9)
    ws.row_dimensions[r].height = 36
    ws.freeze_panes = f"A{r+1}"
    r += 1

    if not rows:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
        c = ws.cell(row=r, column=1, value="Нарушений не обнаружено")
        c.font      = Font(name="Calibri", size=10, bold=True, color="375623")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.fill      = PatternFill("solid", fgColor="E2EFDA")
        c.border    = _side()
        ws.row_dimensions[r].height = 18
        return

    def sort_key(row):
        fi = FIRM_ORDER.index(row["firm"]) if row["firm"] in FIRM_ORDER else 999
        return (fi, row["machine"], row["operator"])

    rows_sorted = sorted(rows, key=sort_key)

    current_firm = None
    firm_row_cnt = 0
    total_cnt    = 0

    def write_firm_total(end_r, firm, cnt):
        _cell(ws, end_r, 1, f"Итого {firm}: {cnt} зад.", bold=True, bg=_C_FIRM,
              halign="right", size=9)
        for ci in range(2, len(HEADERS) + 1):
            _cell(ws, end_r, ci, "", bg=_C_FIRM)
        ws.row_dimensions[end_r].height = 14

    for i, row in enumerate(rows_sorted):
        firm = row["firm"]
        if firm != current_firm:
            if current_firm is not None:
                write_firm_total(r, current_firm, firm_row_cnt)
                r += 1
            current_firm = firm
            firm_row_cnt = 0

        bg = _C_ODD if i % 2 == 0 else _C_EVEN
        _cell(ws, r, 1, row["firm"],     bg=bg)
        _cell(ws, r, 2, row["machine"],  bg=bg, size=8)
        _cell(ws, r, 3, row["operator"], bg=bg)
        _cell(ws, r, 4, row["work_type"],bg=bg)
        _cell(ws, r, 5, row["start"],    bg=bg, halign="center")
        _cell(ws, r, 6, row["end"],      bg=bg, halign="center")
        _cell(ws, r, 7, row["area"],     bg=bg, halign="center", fmt="0.00")

        link = row.get("link", "")
        tid  = row.get("task_id")
        if link and tid:
            _cell(ws, r, 8, str(tid), bg=bg, halign="center", hyperlink=link)
        else:
            _cell(ws, r, 8, "—", bg=bg, halign="center")

        ws.row_dimensions[r].height = 14
        firm_row_cnt += 1
        total_cnt    += 1
        r += 1

    if current_firm is not None:
        write_firm_total(r, current_firm, firm_row_cnt)
        r += 1

    _cell(ws, r, 1, f"ИТОГО: {total_cnt} заданий", bold=True, bg=_C_TOTAL,
          halign="right", size=10)
    for ci in range(2, len(HEADERS) + 1):
        _cell(ws, r, ci, "", bg=_C_TOTAL)
    ws.row_dimensions[r].height = 16

    print(f"  Лист «{sheet_name}»: {total_cnt} строк")

# =============================================================================
# ОДИН ЦИКЛ
# =============================================================================

def run_once() -> None:
    now = datetime.now(tz=TZ_MSK)
    print(f"\n=== Вероятный транзит  |  {REPORT_DATE}  |  {now.strftime('%H:%M:%S')} ===\n")

    (field_to_firm, machine_name, machine_is_truck,
     operators_map, work_type_map) = load_reference_data()

    rows = collect_data(
        field_to_firm, machine_name, machine_is_truck,
        operators_map, work_type_map,
    )

    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    write_sheet(wb, "Вероятный транзит", rows, now)

    wb.save(OUTPUT_FILE)
    print(f"\n✓ Сохранено: {OUTPUT_FILE}")

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"\n[ОШИБКА] {exc}", file=sys.stderr)
        next_run = datetime.now(tz=TZ_MSK) + timedelta(seconds=UPDATE_INTERVAL)
        print(f"\nСледующее обновление в {next_run.strftime('%H:%M:%S')}. Ожидание...")
        time.sleep(UPDATE_INTERVAL)