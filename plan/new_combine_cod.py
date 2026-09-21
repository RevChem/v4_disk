"""
Отчёт «Выработка комбайнов по агрофирмам»
==========================================
Колонки:
  №  |  Комбайн  |  Кол-во дней работы  |  Суммарное время на полях, ч  |
  Макс. время за смену, ч  |  Средняя продолжит. за смену, ч  |
  Обработанная площадь, га  |  Средняя выработка в день, га  |
  Средняя выработка в час, га

Период: DATE_FROM … DATE_TO (env или константы ниже).
Структура: один сводный лист, агрофирма — заголовок-группа.
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
        HIRED_GROUP_ID, FIELD_NAME_FORCE_RULES, SKIP_CROPS,
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
    HIRED_GROUP_ID = 7
    SKIP_CROPS     = {
        "Залежь, лес",
        "Передано в аренду", "Дороги/лесополосы", "Овраги/неудобия",
    }
    FIELD_NAME_FORCE_RULES = []

# Период отчёта — задаётся через env или напрямую здесь
_env_from = os.environ.get("DATE_FROM")
_env_to   = os.environ.get("DATE_TO")
DATE_FROM: date = date.fromisoformat(_env_from) if _env_from else date(2026, 2, 10)
DATE_TO:   date = date.fromisoformat(_env_to)   if _env_to   else date(2026, 5, 12)

OUTPUT_FILE = f"combine_report_{DATE_FROM}_{DATE_TO}.xlsx"

SEMAPHORE_LIMIT = 5

# Граница смены — 06:00 МСК
SHIFT_START_HOUR = 6

# =============================================================================
# HTTP
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


def get_all(endpoint: str, params: dict | None = None) -> list[dict]:
    p = dict(params or {})
    results, from_id = [], 0
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
            f"{BASE_URL}/{endpoint}", params={**p, "page": page}, timeout=120
        )
        r.raise_for_status()
        body  = r.json()
        chunk = body if isinstance(body, list) else body.get("data", [])
        results.extend(chunk)
        if len(chunk) < p["per_page"]:
            break
        page += 1
    return results


async def _fetch_pages_async(
    client: httpx.AsyncClient, url: str, params: dict, sem: asyncio.Semaphore,
) -> list[dict]:
    items, from_id = [], 0
    while True:
        async with sem:
            r = await client.get(url, params={**params, "from_id": from_id, "limit": 1000})
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
# ВСПОМОГАТЕЛЬНОЕ
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


def shift_day(dt: datetime) -> date:
    """День смены: если время < 06:00, относим к предыдущему дню."""
    if dt.hour < SHIFT_START_HOUR:
        return (dt - timedelta(days=1)).date()
    return dt.date()


def fmt_hhmm(total_seconds: float) -> str:
    if total_seconds <= 0:
        return "00:00"
    h = int(total_seconds) // 3600
    m = (int(total_seconds) % 3600) // 60
    return f"{h:02d}:{m:02d}"


def fmt_h2(val: float) -> str:
    return f"{val:.2f}".replace(".", ",")


# =============================================================================
# ИЕРАРХИЯ ПАПОК → АГРОФИРМА  (из new_2.py)
# =============================================================================

def _norm(s: str) -> str:
    return (s or "").strip().casefold().replace(" ", "").replace("\u00a0", "")


def _folder_parent_map(gfs: list[dict]) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
    for gf in gfs:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = (
            gf.get("parent_id")
            or gf.get("parent_group_folder_id")
            or gf.get("group_folder_parent_id")
        )
        out[int(fid)] = int(pid) if pid is not None else None
    return out


def _resolve_firm(folder_id, parent_map, root_ids):
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


def build_field_to_firm(fields_all, fgs_all, gfs_all) -> dict[int, str]:
    fgs_map    = {fg["id"]: fg for fg in fgs_all}
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
            raw_folder = fgs_map.get(fgid, {}).get("group_folder_id")
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

def load_references():
    print("  Поля...")
    fields_all = get_all("fields")
    fgs_all    = get_all("field_groups")
    gfs_all    = get_all("group_folders")
    field_to_firm = build_field_to_firm(fields_all, fgs_all, gfs_all)

    print("  Машины...")
    machines_all   = get_all("machines")
    machine_name:  dict[int, str] = {}
    machine_group: dict[int, int] = {}
    machine_reg:   dict[int, str] = {}
    machine_vin:   dict[int, str] = {}
    for m in machines_all:
        mid = m.get("id")
        if mid is None:
            continue
        machine_name[mid]  = m.get("name") or m.get("title") or m.get("model") or f"machine_{mid}"
        machine_group[mid] = m.get("machine_group_id") or 0
        machine_reg[mid]   = m.get("registration_number") or m.get("reg_number") or ""
        machine_vin[mid]   = m.get("vin") or m.get("serial_number") or ""

    return field_to_firm, machine_name, machine_group, machine_reg, machine_vin


# =============================================================================
# СБОРКА ДАННЫХ
# =============================================================================

def collect_data(field_to_firm, machine_name, machine_group, machine_reg, machine_vin):
    # Окно периода: с 06:00 DATE_FROM по 06:00 (DATE_TO + 1 день)
    period_start = datetime(DATE_FROM.year, DATE_FROM.month, DATE_FROM.day,
                            SHIFT_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    period_end   = datetime(DATE_TO.year, DATE_TO.month, DATE_TO.day,
                            SHIFT_START_HOUR, 0, 0, tzinfo=TZ_MSK) + timedelta(days=1)

    start_str = period_start.strftime("%Y-%m-%dT%H:%M:%S")
    end_str   = period_end.strftime("%Y-%m-%dT%H:%M:%S")

    print(f"\n  Загружаем machine_tasks ({start_str} — {end_str})...")
    all_tasks = get_all("machine_tasks", {
        "end_time_gt_eq": start_str,
        "end_time_lt":    end_str,
    })

    # Дедупликация
    seen: set[int] = set()
    unique_tasks: list[dict] = []
    for t in all_tasks:
        tid = t.get("id")
        if tid not in seen:
            seen.add(tid)
            unique_tasks.append(t)
    all_tasks = unique_tasks
    print(f"  Задач: {len(all_tasks)}")
    if not all_tasks:
        return {}

    # Загрузка mtfmi
    task_ids  = [t["id"] for t in all_tasks]
    print("  Загружаем field mapping items (async)...")
    mtfmi_map = asyncio.run(fetch_mtfmi_async(task_ids))

    # Агрегация: по (machine_id, shift_day) → секунды работы + площадь
    # Структура: machine_id → { day → {secs, area} }
    MachineDay = dict  # {day: {"secs": float, "area": float}}
    machine_days: dict[int, MachineDay] = defaultdict(lambda: defaultdict(lambda: {"secs": 0.0, "area": 0.0}))
    # Для определения агрофирмы: machine_id → set of firms
    machine_firms: dict[int, set] = defaultdict(set)

    for task in all_tasks:
        tid = task["id"]
        mid = task.get("machine_id")
        if not mid:
            continue

        t_start = parse_dt(task.get("start_time") or task.get("started_at"))
        t_end   = parse_dt(task.get("end_time")   or task.get("finished_at"))
        if not t_start or not t_end or t_end <= t_start:
            continue
        if t_end < period_start or t_start > period_end:
            continue

        # Обрезаем по периоду
        t_start = max(t_start, period_start)
        t_end   = min(t_end,   period_end)

        secs = (t_end - t_start).total_seconds()
        if secs <= 0:
            continue

        # День смены — по середине задачи (или по t_start)
        day = shift_day(t_start)
        machine_days[mid][day]["secs"] += secs

        # Площадь из mtfmi
        items = mtfmi_map.get(tid, [])
        for item in items:
            fid = item.get("field_id")
            cov = float(item.get("covered_area") or 0)
            if cov > 0:
                machine_days[mid][day]["area"] += cov
                if fid and fid in field_to_firm:
                    machine_firms[mid].add(field_to_firm[fid])

    # Итог по машинам
    # Для агрофирмы берём наиболее частую из field_to_firm, иначе "—"
    # Структура результата: firm → list of machine rows
    result: dict[str, list[dict]] = defaultdict(list)

    for mid, days_data in machine_days.items():
        if not days_data:
            continue
        firms = machine_firms.get(mid, set())
        firm  = list(firms)[0] if len(firms) == 1 else (
            max(firms, key=lambda f: FIRM_ORDER.index(f) if f in FIRM_ORDER else 999)
            if firms else "—"
        )

        # Название машины
        m_name = machine_name.get(mid, f"machine_{mid}")
        m_reg  = machine_reg.get(mid, "")
        m_vin  = machine_vin.get(mid, "")
        parts  = [m_name]
        if m_reg:
            parts.append(m_reg)
        if m_vin:
            parts.append(m_vin)
        machine_str = " ".join(parts)

        # Определяем собственный/наёмный
        is_hired = (machine_group.get(mid, 0) == HIRED_GROUP_ID)

        n_days    = len(days_data)
        total_sec = sum(d["secs"] for d in days_data.values())
        total_area= sum(d["area"] for d in days_data.values())
        max_sec   = max(d["secs"] for d in days_data.values())
        avg_sec   = total_sec / n_days if n_days else 0.0
        avg_day   = total_area / n_days if n_days else 0.0
        avg_hour  = total_area / (total_sec / 3600) if total_sec > 0 else 0.0

        result[firm].append({
            "machine":    machine_str,
            "is_hired":   is_hired,
            "n_days":     n_days,
            "total_h":    total_sec / 3600,
            "max_h":      max_sec / 3600,
            "avg_h":      avg_sec / 3600,
            "area":       total_area,
            "avg_day":    avg_day,
            "avg_hour":   avg_hour,
        })

    # Сортировка строк внутри агрофирмы: собственные → наёмные, затем по площади убыв.
    for firm in result:
        result[firm].sort(key=lambda x: (x["is_hired"], -x["area"]))

    return result


# =============================================================================
# СТИЛИ
# =============================================================================

_C_TITLE   = "1E3A6E"
_C_HDR     = "2E4DA0"
_C_FIRM    = "C6EFCE"  # зелёный — строка агрофирмы
_C_HIRED   = "FCE4D6"  # оранжевый — наёмные
_C_OWN_E   = "EEF4FB"
_C_OWN_O   = "FFFFFF"
_C_TOTAL   = "BDD7EE"


def _side() -> Border:
    t = Side(style="thin", color="BBBBBB")
    return Border(left=t, right=t, top=t, bottom=t)


def _cell(ws, row, col, value=None, bold=False, bg=None,
          color="000000", halign="center", wrap=False,
          fmt=None, size=9, italic=False):
    c = ws.cell(row=row, column=col, value=value)
    c.font      = Font(name="Calibri", size=size, bold=bold,
                       color=color, italic=italic)
    c.alignment = Alignment(horizontal=halign, vertical="center",
                            wrap_text=wrap)
    c.border    = _side()
    if bg:
        c.fill = PatternFill("solid", fgColor=bg)
    if fmt:
        c.number_format = fmt


HEADERS = [
    "№",
    "Комбайн",
    "Количество\nдней работы",
    "Суммарное время\nработы на полях, часов",
    "Максимальное время\nработы на полях, часов",
    "Средняя продолжи-\nтельность за смену, часов",
    "Обработанная\nплощадь, га",
    "Средняя выработка\nв день, га",
    "Средняя выработка\nв час, га",
]

COL_W = [5, 42, 10, 14, 14, 14, 13, 13, 13]


# =============================================================================
# ЗАПИСЬ ОТЧЁТА
# =============================================================================

def write_report(result: dict[str, list[dict]]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Выработка комбайнов"

    for ci, w in enumerate(COL_W, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    r = 1
    # Заголовок отчёта
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
    c = ws.cell(
        row=r, column=1,
        value=(
            f"Выработка комбайнов по агрофирмам  |  "
            f"{DATE_FROM.strftime('%d.%m.%Y')} — {DATE_TO.strftime('%d.%m.%Y')}"
        ),
    )
    c.font      = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill      = PatternFill("solid", fgColor=_C_TITLE)
    c.border    = _side()
    ws.row_dimensions[r].height = 26
    r += 1

    # Шапка колонок
    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, r, ci, h, bold=True, bg=_C_HDR, color="FFFFFF",
              halign="center", wrap=True, size=9)
    ws.row_dimensions[r].height = 44
    ws.freeze_panes = f"A{r+1}"
    r += 1

    num = 0  # сквозной номер машины

    for firm in FIRM_ORDER:
        rows = result.get(firm, [])

        # Строка агрофирмы
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
        c = ws.cell(row=r, column=1, value=firm)
        c.font      = Font(name="Calibri", size=10, bold=True, color="000000")
        c.alignment = Alignment(horizontal="left", vertical="center")
        c.fill      = PatternFill("solid", fgColor=_C_FIRM)
        c.border    = _side()
        ws.row_dimensions[r].height = 16
        r += 1

        # Разделяем собственные / наёмные
        own_rows    = [x for x in rows if not x["is_hired"]]
        hired_rows  = [x for x in rows if x["is_hired"]]

        def write_group(group_rows, bg_even, bg_odd, label=None):
            nonlocal r, num
            if label and group_rows:
                ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
                c = ws.cell(row=r, column=1, value=label)
                c.font      = Font(name="Calibri", size=9, bold=True, italic=True)
                c.alignment = Alignment(horizontal="left", vertical="center",
                                        indent=1)
                c.fill      = PatternFill("solid", fgColor=bg_even)
                c.border    = _side()
                ws.row_dimensions[r].height = 14
                r += 1
            for i, row in enumerate(group_rows):
                num += 1
                bg = bg_even if i % 2 == 0 else bg_odd
                vals = [
                    (num,                          "right",  None),
                    (row["machine"],               "left",   None),
                    (row["n_days"],                "center", "0"),
                    (round(row["total_h"], 2),     "right",  "# ##0.00"),
                    (fmt_hhmm(row["max_h"]*3600),  "center", None),
                    (fmt_hhmm(row["avg_h"]*3600),  "center", None),
                    (round(row["area"], 2),        "right",  "# ##0.00"),
                    (round(row["avg_day"], 1),     "right",  "# ##0.0"),
                    (round(row["avg_hour"], 2),    "right",  "# ##0.00"),
                ]
                for ci, (val, align, fmt) in enumerate(vals, start=1):
                    _cell(ws, r, ci, val, bg=bg, halign=align, fmt=fmt)
                ws.row_dimensions[r].height = 15
                r += 1

            # Итоговая строка группы
            if group_rows:
                tot_area   = sum(x["area"]    for x in group_rows)
                tot_h      = sum(x["total_h"] for x in group_rows)
                tot_days   = sum(x["n_days"]  for x in group_rows)
                avg_d      = tot_area / tot_days  if tot_days  else 0.0
                avg_hh     = tot_area / tot_h     if tot_h     else 0.0
                summary = [
                    ("Итого" + (f" {label}" if label else ""), "left",   None),
                    ("",      "left",   None),
                    (tot_days,"center", "0"),
                    (round(tot_h, 2),   "right",  "# ##0.00"),
                    ("", "center", None),
                    ("", "center", None),
                    (round(tot_area, 2),"right",  "# ##0.00"),
                    (round(avg_d, 1),   "right",  "# ##0.0"),
                    (round(avg_hh, 2),  "right",  "# ##0.00"),
                ]
                for ci, (val, align, fmt) in enumerate(summary, start=1):
                    _cell(ws, r, ci, val, bold=True, bg=_C_TOTAL,
                          halign=align, fmt=fmt)
                ws.row_dimensions[r].height = 15
                r += 1

        if own_rows and hired_rows:
            write_group(own_rows,   _C_OWN_O, _C_OWN_E,  "Собственные")
            write_group(hired_rows, _C_HIRED, "FFF3E0",   "Наёмные")
        elif own_rows:
            write_group(own_rows, _C_OWN_O, _C_OWN_E)
        elif hired_rows:
            write_group(hired_rows, _C_HIRED, "FFF3E0")

        r += 1  # пустая строка между агрофирмами

    # Агрофирмы не из FIRM_ORDER
    for firm, rows in result.items():
        if firm not in FIRM_ORDER:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
            c = ws.cell(row=r, column=1, value=firm or "Неизвестно")
            c.font      = Font(name="Calibri", size=10, bold=True)
            c.alignment = Alignment(horizontal="left", vertical="center")
            c.fill      = PatternFill("solid", fgColor=_C_FIRM)
            c.border    = _side()
            ws.row_dimensions[r].height = 16
            r += 1
            for i, row in enumerate(rows):
                num += 1
                bg = _C_OWN_O if i % 2 == 0 else _C_OWN_E
                vals = [
                    (num,                         "right",  None),
                    (row["machine"],              "left",   None),
                    (row["n_days"],               "center", "0"),
                    (round(row["total_h"], 2),    "right",  "# ##0.00"),
                    (fmt_hhmm(row["max_h"]*3600), "center", None),
                    (fmt_hhmm(row["avg_h"]*3600), "center", None),
                    (round(row["area"], 2),       "right",  "# ##0.00"),
                    (round(row["avg_day"], 1),    "right",  "# ##0.0"),
                    (round(row["avg_hour"], 2),   "right",  "# ##0.00"),
                ]
                for ci, (val, align, fmt) in enumerate(vals, start=1):
                    _cell(ws, r, ci, val, bg=bg, halign=align, fmt=fmt)
                ws.row_dimensions[r].height = 15
                r += 1
            r += 1

    wb.save(OUTPUT_FILE)
    print(f"\n✓ Сохранено: {OUTPUT_FILE}")


# =============================================================================
# MAIN
# =============================================================================

def build_report() -> None:
    print(
        f"=== Отчёт по комбайнам | "
        f"{DATE_FROM.strftime('%d.%m.%Y')} — {DATE_TO.strftime('%d.%m.%Y')} ===\n"
    )
    field_to_firm, machine_name, machine_group, machine_reg, machine_vin = load_references()
    result = collect_data(field_to_firm, machine_name, machine_group, machine_reg, machine_vin)
    print(f"\n  Агрофирм с данными: {len(result)}")
    for firm, rows in result.items():
        print(f"  → {firm}: {len(rows)} комбайнов")
    write_report(result)


if __name__ == "__main__":
    build_report()