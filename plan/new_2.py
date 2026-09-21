"""
Отчёт «Работы по внесению, посеву, культивации, дискованию, боронованию»
=========================================================================
Формат вывода соответствует Excel-отчёту из Cropwise:

  Агрофирма | Поле | Культура | Смена | Подтип работ | Машина | Водитель |
  Наименование агрегата | Норма выработки в час, га |
  Фактическая выработка в час, га | Обработанная площадь, га |
  Продолжительность работы в полях | Боронование+Подсолнечник

Смены:
  Утренняя  — 06:00 … 17:59 текущего дня
  Вечерняя  — 18:00 текущего дня … 05:59 следующего дня

Зависимости: openpyxl, requests, httpx
Константы агрофирм / токен берём из plans.py (или fallback ниже).
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
        API_TOKEN,
        BASE_URL,
        TZ_MSK,
        FIRM_ORDER,
        FIRM_TO_FOLDER,
        FOLDER_TO_FIRM,
        WORK_GROUPS,
        SKIP_CROPS,
        HIRED_GROUP_ID,
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
    FOLDER_TO_FIRM = {v: k for k, v in FIRM_TO_FOLDER.items()}
    WORK_GROUPS    = []
    SKIP_CROPS     = {
        "Залежь, лес", "Залежь", "Введение в оборот 2026 год",
        "Луговые травы", "Обмен", "В пользовании у фермера",
        "Введение в оборот 2027 год", "Введение в оборот",
        "Передано в аренду", "Дороги/лесополосы", "Овраги/неудобия",
    }
    FIELD_NAME_FORCE_RULES = []

# Дата смены — по умолчанию сегодня; можно задать через REPORT_DATE=ГГГГ-ММ-ДД
REPORT_DATE: date = date(2026, 5, 6)
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

SEASON_YEAR = REPORT_DATE.year

# Границы смен (часы, МСК)
MORNING_START_HOUR = 6    # 06:00 — начало утренней смены
MORNING_END_HOUR   = 18   # 18:00 — начало вечерней смены (утренняя до 17:59)

# Норма выработки по умолчанию (га/ч); если API не возвращает — ставим 25
DEFAULT_NORM_HA_H = 25.0

IDLE_THRESHOLD_MIN = 20

OUTPUT_FILE = f"report_{REPORT_DATE}.xlsx"

# Культуры, у которых в колонке «Боронование+Подсолнечник» ставим 1
SUNFLOWER_CROPS = {"Подсолнечник"}

# =============================================================================
# HTTP
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})
SEMAPHORE_LIMIT = 5


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
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    items, from_id = [], 0
    while True:
        async with semaphore:
            r = await client.get(
                url, params={**params, "from_id": from_id, "limit": 1000}
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
# ВСПОМОГАТЕЛЬНЫЕ: ДАТЫ / ВРЕМЯ
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


def shift_windows(report_date: date):
    """
    Возвращает словарь {'Утренняя смена': (start, end), 'Вечерняя смена': (start, end)}
    для заданной даты.
      Утренняя: 06:00 … 18:00 (не включая 18:00)
      Вечерняя: 18:00 … 06:00 следующего дня
    """
    morning_start = datetime(
        report_date.year, report_date.month, report_date.day,
        MORNING_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    evening_start = datetime(
        report_date.year, report_date.month, report_date.day,
        MORNING_END_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    next_morning  = morning_start + timedelta(days=1)
    return {
        "Утренняя смена": (morning_start, evening_start),
        "Вечерняя смена": (evening_start, next_morning),
    }


def classify_shift(t_start: Optional[datetime], t_end: Optional[datetime]) -> str:
    """
    Определяет смену по временным меткам задачи.
    Правило: смена определяется по большей части рабочего времени.
    Если данных нет — возвращает 'Утренняя смена'.
    """
    windows = shift_windows(REPORT_DATE)
    if t_start is None and t_end is None:
        return "Утренняя смена"

    best_shift = "Утренняя смена"
    best_overlap = timedelta(0)

    for shift_name, (ws, we) in windows.items():
        s = max(t_start or ws, ws)
        e = min(t_end   or we, we)
        overlap = max(timedelta(0), e - s)
        if overlap > best_overlap:
            best_overlap = overlap
            best_shift   = shift_name

    return best_shift


def fmt_duration(total_seconds: float) -> str:
    """Форматирует секунды в ЧЧ:ММ:СС, как в оригинальном Excel."""
    if total_seconds <= 0:
        return "00:00:00"
    h = int(total_seconds) // 3600
    m = (int(total_seconds) % 3600) // 60
    s = int(total_seconds) % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%d.%m %H:%M")


# =============================================================================
# ИЕРАРХИЯ ПАПОК → АГРОФИРМА
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


def _resolve_firm(
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
# РАСЧЁТ ПРОДОЛЖИТЕЛЬНОСТИ РАБОТЫ В ПОЛЯХ ПО mtfmi
# =============================================================================

def calc_work_duration_seconds(
    mtfmi_items: list[dict],
    shift_start: datetime,
    shift_end: datetime,
) -> float:
    """
    Суммарная продолжительность работы в полях за смену (в секундах).
    Обрезает каждый интервал по границам смены.
    """
    total = 0.0
    for item in mtfmi_items:
        s = parse_dt(item.get("start_time") or item.get("started_at"))
        e = parse_dt(item.get("end_time")   or item.get("finished_at"))
        if s and e and s < e:
            s = max(s, shift_start)
            e = min(e, shift_end)
            if s < e:
                total += (e - s).total_seconds()
    return total


# =============================================================================
# ЗАГРУЗКА СПРАВОЧНИКОВ
# =============================================================================

def load_reference_data():
    print("  Поля...")
    fields_all = get_all("fields")
    fgs_all    = get_all("field_groups")
    gfs_all    = get_all("group_folders")

    field_name:  dict[int, str]   = {}
    field_area:  dict[int, float] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        field_name[fid] = f.get("name") or f.get("title") or f"field_{fid}"
        field_area[fid] = float(
            f.get("tillable_area") or f.get("calculated_area") or 0.0
        )

    field_to_firm = build_field_to_firm(fields_all, fgs_all, gfs_all)

    print("  Культуры...")
    crops_raw    = get_all_page("crops")
    crop_id_name = {
        c["id"]: (c.get("name") or c.get("title", "")) for c in crops_raw
    }
    his_all = get_all("history_items", {"year": str(SEASON_YEAR)})
    field_to_crop: dict[int, str] = {}
    for hi in his_all:
        fid = hi.get("field_id")
        cid = hi.get("crop_id")
        if not (fid and cid):
            continue
        cname = crop_id_name.get(cid, "")
        if not cname or cname in SKIP_CROPS:
            continue
        if hi.get("active"):
            field_to_crop[fid] = cname

    print("  Машины и операторы...")
    machines_all   = get_all("machines")
    machine_name:  dict[int, str] = {}
    machine_group: dict[int, int] = {}
    machine_reg:   dict[int, str] = {}  # гос. номер
    machine_vin:   dict[int, str] = {}  # VIN / серийный номер

    for m in machines_all:
        mid = m.get("id")
        if mid is None:
            continue
        machine_name[mid]  = (
            m.get("name") or m.get("title") or m.get("model") or f"machine_{mid}"
        )
        machine_group[mid] = m.get("machine_group_id") or 0
        machine_reg[mid]   = m.get("registration_number") or m.get("reg_number") or ""
        machine_vin[mid]   = m.get("vin") or m.get("serial_number") or ""

    operators_map: dict[int, str] = {}
    for ep in ("workers", "operators", "users"):
        try:
            ops = get_all(ep) or get_all_page(ep)
            if ops:
                for o in ops:
                    oid = o.get("id")
                    if oid is None:
                        continue
                    name = (
                        o.get("full_name")
                        or o.get("name")
                        or f"{o.get('first_name','')} {o.get('last_name','')}".strip()
                        or f"{ep}_{oid}"
                    )
                    operators_map[oid] = name
                print(f"    Операторов из /{ep}: {len(operators_map)}")
                break
        except Exception:
            continue

    print("  Виды работ...")
    wt_raw = get_all("work_types") or get_all_page("work_types")
    work_type_map: dict[int, str] = {
        w["id"]: (w.get("name") or w.get("title") or f"wt_{w['id']}")
        for w in wt_raw
    }

    print("  Орудия (implements)...")
    impl_raw = get_all("implements") or get_all_page("implements")
    implements_map: dict[int, dict] = {
        impl["id"]: impl for impl in impl_raw if impl.get("id") is not None
    }
    print(f"    Орудий: {len(implements_map)}")

    return (
        field_name, field_area, field_to_firm, field_to_crop,
        machine_name, machine_group, machine_reg, machine_vin,
        operators_map, work_type_map, implements_map,
    )


# =============================================================================
# СБОРКА ДАННЫХ ЗА СУТКИ
# =============================================================================

def collect_day_data(
    field_to_firm:  dict[int, str],
    field_to_crop:  dict[int, str],
    field_area:     dict[int, float],
    field_name:     dict[int, str],
    machine_name:   dict[int, str],
    machine_group:  dict[int, int],
    machine_reg:    dict[int, str],
    machine_vin:    dict[int, str],
    operators_map:  dict[int, str],
    work_type_map:  dict[int, str],
    implements_map: dict[int, dict],
) -> list[dict]:
    """
    Собирает строки отчёта.
    Одна строка = машина × поле × смена (Утренняя / Вечерняя).
    """
    windows = shift_windows(REPORT_DATE)

    # Окно всего дня (06:00 → 06:00 следующего дня)
    day_start = list(windows.values())[0][0]
    day_end   = list(windows.values())[-1][1]
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    day_end_str   = day_end.strftime("%Y-%m-%dT%H:%M:%S")

    print(
        f"\n  Загружаем machine_tasks за {REPORT_DATE} "
        f"({day_start_str} — {day_end_str})..."
    )

    all_tasks: list[dict] = []
    wt_ids_seen: set[int] = set()
    for _group_name, wt_ids in WORK_GROUPS:
        for wt_id in wt_ids:
            if wt_id in wt_ids_seen:
                continue
            wt_ids_seen.add(wt_id)
            tasks = get_all("machine_tasks", {
                "work_type_id":   wt_id,
                "end_time_gt_eq": day_start_str,
                "end_time_lt":    day_end_str,
            })
            all_tasks.extend(tasks)

    if not all_tasks:
        print("  WORK_GROUPS пустой, загружаем все задачи за сутки...")
        all_tasks = get_all("machine_tasks", {
            "end_time_gt_eq": day_start_str,
            "end_time_lt":    day_end_str,
        })

    # Дедупликация
    seen_ids: set[int] = set()
    unique_tasks: list[dict] = []
    for t in all_tasks:
        tid = t.get("id")
        if tid not in seen_ids:
            seen_ids.add(tid)
            unique_tasks.append(t)
    all_tasks = unique_tasks
    print(f"    Задач за сутки: {len(all_tasks)}")
    if not all_tasks:
        return []

    # Загружаем mtfmi асинхронно
    task_ids  = [t["id"] for t in all_tasks]
    mtfmi_map = asyncio.run(fetch_mtfmi_async(task_ids))

    # Сезонная площадь по полям
    print("  Загружаем сезонный итог по полям...")
    season_start_str = f"{SEASON_YEAR}-01-01T00:00:00"
    season_area_by_field: dict[int, float] = defaultdict(float)
    for _group_name, wt_ids in WORK_GROUPS:
        for wt_id in wt_ids:
            s_tasks = get_all("machine_tasks", {
                "work_type_id":   wt_id,
                "end_time_gt_eq": season_start_str,
                "end_time_lt":    day_end_str,
            })
            for st in s_tasks:
                for item in mtfmi_map.get(st["id"], []):
                    fid = item.get("field_id")
                    if fid:
                        cov = float(item.get("covered_area") or 0)
                        season_area_by_field[fid] += cov

    if not WORK_GROUPS:
        for tid_s, items in mtfmi_map.items():
            for item in items:
                fid = item.get("field_id")
                if fid:
                    season_area_by_field[fid] += float(item.get("covered_area") or 0)

    rows: list[dict] = []

    for task in all_tasks:
        tid   = task["id"]
        mid   = task.get("machine_id")
        op_id = (
            task.get("operator_id")
            or task.get("worker_id")
            or task.get("user_id")
        )
        operator_name = operators_map.get(op_id, "—") if op_id else "—"
        wt_id         = task.get("work_type_id")
        work_type     = work_type_map.get(wt_id, "—") if wt_id else "—"

        # Машина: «Название гос.номер / гос.номер / VIN» — как в оригинальном Excel
        if mid:
            m_name = machine_name.get(mid, f"machine_{mid}")
            m_reg  = machine_reg.get(mid, "")
            m_vin  = machine_vin.get(mid, "")
            parts  = [m_name]
            if m_reg:
                parts.append(m_reg)
            if m_vin:
                parts.append(m_vin)
            machine_str = " / ".join(parts)
        else:
            machine_str = "—"

        t_start = parse_dt(task.get("start_time") or task.get("started_at"))
        t_end   = parse_dt(task.get("end_time")   or task.get("finished_at"))

        # Орудие (агрегат)
        impl_id  = task.get("implement_id")
        impl_rec = implements_map.get(impl_id) if impl_id else None
        implement_name = ""
        if impl_rec:
            implement_name = str(
                impl_rec.get("custom_name") or impl_rec.get("name") or ""
            ).strip()

        # Норма выработки га/ч
        norm_ha_h = float(
            task.get("norm_ha_per_hour") or task.get("planned_rate") or DEFAULT_NORM_HA_H
        )

        items_for_task = mtfmi_map.get(tid, [])

        # Определяем смену
        shift_name = classify_shift(t_start, t_end)
        shift_start, shift_end = windows[shift_name]

        # Продолжительность работы в полях за смену
        work_secs = calc_work_duration_seconds(items_for_task, shift_start, shift_end)
        work_hours = work_secs / 3600.0

        # Площадь по полям внутри смены
        field_cov: dict[int, float] = defaultdict(float)
        for item in items_for_task:
            fid = item.get("field_id")
            if not fid:
                continue
            hourly = item.get("covered_area_hourly") or []
            if hourly:
                cov = 0.0
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and shift_start <= h < shift_end:
                        cov += entry[1] or 0
            else:
                cov = float(item.get("covered_area") or 0)
            if cov > 0:
                field_cov[fid] += cov

        if not field_cov:
            rows.append(_make_row(
                firm="—",
                field_name="—",
                crop="—",
                shift=shift_name,
                work_type=work_type,
                machine=machine_str,
                operator=operator_name,
                implement=implement_name or "—",
                norm_ha_h=norm_ha_h,
                fact_ha_h=0.0,
                covered_shift=0.0,
                duration_str=fmt_duration(work_secs),
                is_sunflower=0,
            ))
            continue

        for fid, cov_shift in field_cov.items():
            crop = field_to_crop.get(fid, "—")
            if crop in SKIP_CROPS:
                continue
            firm  = field_to_firm.get(fid, "—")
            fname = field_name.get(fid, f"field_{fid}")

            # Фактическая выработка га/ч
            fact_ha_h = round(cov_shift / work_hours, 2) if work_hours > 0 else 0.0

            rows.append(_make_row(
                firm=firm,
                field_name=fname,
                crop=crop,
                shift=shift_name,
                work_type=work_type,
                machine=machine_str,
                operator=operator_name,
                implement=implement_name or "—",
                norm_ha_h=norm_ha_h,
                fact_ha_h=fact_ha_h,
                covered_shift=round(cov_shift, 2),
                duration_str=fmt_duration(work_secs),
                is_sunflower=1 if crop in SUNFLOWER_CROPS else 0,
            ))

    return rows


def _make_row(**kw) -> dict:
    return {
        "firm":          kw["firm"],
        "field_name":    kw["field_name"],
        "crop":          kw["crop"],
        "shift":         kw["shift"],
        "work_type":     kw["work_type"],
        "machine":       kw["machine"],
        "operator":      kw["operator"],
        "implement":     kw["implement"],
        "norm_ha_h":     kw["norm_ha_h"],
        "fact_ha_h":     kw["fact_ha_h"],
        "covered_shift": kw["covered_shift"],
        "duration_str":  kw["duration_str"],
        "is_sunflower":  kw["is_sunflower"],
    }


# =============================================================================
# СТИЛИ
# =============================================================================

_C_HDR   = "2E4DA0"   # синий заголовок колонок
_C_TITLE = "1E3A6E"   # тёмно-синий заголовок листа
_C_TOTAL = "BDD7EE"   # итоговая строка
_C_ODD   = "FFFFFF"
_C_EVEN  = "EEF4FB"
_C_MORNING = "E8F5E9"  # подсветка утренней смены
_C_EVENING = "FFF9C4"  # подсветка вечерней смены


def _side() -> Border:
    t = Side(style="thin", color="BBBBBB")
    return Border(left=t, right=t, top=t, bottom=t)


def _cell(
    ws, row: int, col: int, value=None,
    bold=False, bg: str | None = None, color="000000",
    halign="center", wrap=False, fmt: str | None = None,
    size=9,
) -> None:
    c = ws.cell(row=row, column=col, value=value)
    c.font      = Font(name="Calibri", size=size, bold=bold, color=color)
    c.alignment = Alignment(horizontal=halign, vertical="center", wrap_text=wrap)
    c.border    = _side()
    if bg:
        c.fill = PatternFill("solid", fgColor=bg)
    if fmt:
        c.number_format = fmt


# =============================================================================
# ЗАГОЛОВКИ И ШИРИНЫ КОЛОНОК
# =============================================================================

HEADERS = [
    "Агрофирма",
    "Поле",
    "Культура",
    "Смена",
    "Подтип работ",
    "Машина",
    "Водитель",
    "Наименование агрегата",
    "Норма выработки\nв час, га",
    "Фактическая выработка\nв час, га",
    "Обработанная\nплощадь, га",
    "Продолжительность\nработы в полях, ч",
    "Боронование+\nПодсолнечник",
]

COL_W = [20, 14, 16, 14, 28, 36, 26, 36, 11, 11, 11, 13, 10]


# =============================================================================
# ЗАПИСЬ ЛИСТА
# =============================================================================

def _write_sheet_rows(ws, rows: list[dict], r_start: int) -> int:
    """Записывает строки данных, возвращает номер следующей строки."""
    r = r_start
    for i, row in enumerate(rows):
        if row["shift"] == "Утренняя смена":
            base_bg = _C_MORNING if i % 2 == 0 else _C_ODD
        else:
            base_bg = _C_EVENING if i % 2 == 0 else _C_EVEN

        vals = [
            (row["firm"],          base_bg, "left",   None),         # 1  Агрофирма
            (row["field_name"],    base_bg, "left",   None),         # 2  Поле
            (row["crop"],          base_bg, "left",   None),         # 3  Культура
            (row["shift"],         base_bg, "center", None),         # 4  Смена
            (row["work_type"],     base_bg, "left",   None),         # 5  Подтип работ
            (row["machine"],       base_bg, "left",   None),         # 6  Машина
            (row["operator"],      base_bg, "left",   None),         # 7  Водитель
            (row["implement"],     base_bg, "left",   None),         # 8  Агрегат
            (row["norm_ha_h"],     base_bg, "right",  "# ##0.00"),   # 9  Норма
            (row["fact_ha_h"],     base_bg, "right",  "# ##0.00"),   # 10 Факт
            (row["covered_shift"], base_bg, "right",  "# ##0.00"),   # 11 Площадь
            (row["duration_str"],  base_bg, "center", None),         # 12 Продолжит.
            (row["is_sunflower"],  base_bg, "center", None),         # 13 Подсолн.
        ]
        for ci, (val, bg, align, fmt) in enumerate(vals, start=1):
            _cell(ws, r, ci, val, bg=bg, halign=align, fmt=fmt)
        ws.row_dimensions[r].height = 15
        r += 1
    return r


def write_firm_sheet(wb, firm_name: str, rows: list[dict]) -> None:
    ws = wb.create_sheet(title=firm_name[:31])

    for ci, w in enumerate(COL_W, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    r = 1
    # Заголовок листа
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
    c = ws.cell(
        row=r, column=1,
        value=(
            f"Работы по внесению, посеву, культивации, дискованию, боронованию"
            f" — {firm_name}  |  с 6:00 {REPORT_DATE.strftime('%d.%m')}"
            f" по 6:00 {(REPORT_DATE + timedelta(days=1)).strftime('%d.%m')}"
        ),
    )
    c.font      = Font(name="Calibri", size=12, bold=True, color="FFFFFF")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill      = PatternFill("solid", fgColor=_C_TITLE)
    c.border    = _side()
    ws.row_dimensions[r].height = 22
    r += 1

    # Шапка колонок
    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, r, ci, h, bold=True, bg=_C_HDR, color="FFFFFF",
              halign="center", wrap=True, size=9)
    ws.row_dimensions[r].height = 40
    ws.freeze_panes = f"A{r+1}"
    r += 1

    r = _write_sheet_rows(ws, rows, r)

    # Итоговые строки: посев и культивация отдельно (как в оригинале)
    def _total_row(label: str, filtered_rows: list[dict]) -> None:
        nonlocal r
        total_norm  = sum(
            row["norm_ha_h"] for row in filtered_rows
            if isinstance(row["norm_ha_h"], (int, float))
        )
        total_fact  = sum(
            row["fact_ha_h"] for row in filtered_rows
            if isinstance(row["fact_ha_h"], (int, float))
        )
        total_area  = sum(
            row["covered_shift"] for row in filtered_rows
            if isinstance(row["covered_shift"], (int, float))
        )
        summary = [
            label, "", "", "", "", "", "", "",
            round(total_norm, 2),
            round(total_fact, 2),
            round(total_area, 2),
            "", "",
        ]
        for ci, val in enumerate(summary, start=1):
            fmt = "# ##0.00" if ci in (9, 10, 11) and isinstance(val, float) else None
            _cell(ws, r, ci, val, bold=True, bg=_C_TOTAL,
                  halign="right" if ci in (9, 10, 11) else "left", fmt=fmt)
        ws.row_dimensions[r].height = 16
        r += 1

    sow_rows  = [x for x in rows if "посев" in x["work_type"].lower()]
    cult_rows = [x for x in rows if "культив" in x["work_type"].lower()]
    if sow_rows:
        _total_row("Итого посев", sow_rows)
    if cult_rows:
        _total_row("Итого культивация", cult_rows)

    # Общая сводка по сезону
    all_sow_area  = sum(
        x["covered_shift"] for x in rows
        if "посев" in x["work_type"].lower() and isinstance(x["covered_shift"], (int, float))
    )
    all_cult_area = sum(
        x["covered_shift"] for x in rows
        if "культив" in x["work_type"].lower() and isinstance(x["covered_shift"], (int, float))
    )

    r += 1
    if all_sow_area:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8)
        _cell(ws, r, 1, "Общий посев", bold=True, halign="right")
        _cell(ws, r, 9, "", bold=True)
        _cell(ws, r, 10, "", bold=True)
        _cell(ws, r, 11, round(all_sow_area, 2), bold=True,
              halign="right", fmt="# ##0.00")
        ws.row_dimensions[r].height = 14
        r += 1

    if all_cult_area:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8)
        _cell(ws, r, 1, "Общая культивация", bold=True, halign="right")
        _cell(ws, r, 9, "", bold=True)
        _cell(ws, r, 10, "", bold=True)
        _cell(ws, r, 11, round(all_cult_area, 2), bold=True,
              halign="right", fmt="# ##0.00")
        ws.row_dimensions[r].height = 14


def write_summary_sheet(wb, all_rows: list[dict]) -> None:
    """Сводный лист — все агрофирмы вместе, сортировка: агрофирма → поле → смена."""
    ws = wb.create_sheet(title="Все агрофирмы")
    wb.move_sheet("Все агрофирмы", offset=-len(wb.sheetnames) + 1)

    for ci, w in enumerate(COL_W, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    r = 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
    c = ws.cell(
        row=r, column=1,
        value=(
            f"Работы по внесению, посеву, культивации, дискованию, боронованию"
            f"  |  с 6:00 {REPORT_DATE.strftime('%d.%m')}"
            f" по 6:00 {(REPORT_DATE + timedelta(days=1)).strftime('%d.%m')}"
        ),
    )
    c.font      = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill      = PatternFill("solid", fgColor=_C_TITLE)
    c.border    = _side()
    ws.row_dimensions[r].height = 24
    r += 1

    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, r, ci, h, bold=True, bg=_C_HDR, color="FFFFFF",
              halign="center", wrap=True, size=9)
    ws.row_dimensions[r].height = 40
    ws.freeze_panes = f"A{r+1}"
    r += 1

    # Сортировка: агрофирма (по FIRM_ORDER) → поле → смена (утренняя первая)
    shift_order = {"Утренняя смена": 0, "Вечерняя смена": 1}
    sorted_rows = sorted(
        all_rows,
        key=lambda x: (
            FIRM_ORDER.index(x["firm"]) if x["firm"] in FIRM_ORDER else 999,
            x["field_name"],
            shift_order.get(x["shift"], 2),
            x["operator"],
        ),
    )

    _write_sheet_rows(ws, sorted_rows, r)


# =============================================================================
# MAIN
# =============================================================================

def build_report() -> None:
    print(
        f"=== Отчёт по работам  |  "
        f"с 6:00 {REPORT_DATE.strftime('%d.%m')} "
        f"по 6:00 {(REPORT_DATE + timedelta(days=1)).strftime('%d.%m')} ===\n"
    )

    (
        field_name, field_area, field_to_firm, field_to_crop,
        machine_name, machine_group, machine_reg, machine_vin,
        operators_map, work_type_map, implements_map,
    ) = load_reference_data()

    all_rows = collect_day_data(
        field_to_firm, field_to_crop, field_area, field_name,
        machine_name, machine_group, machine_reg, machine_vin,
        operators_map, work_type_map, implements_map,
    )

    print(f"\n  Итого строк: {len(all_rows)}")

    by_firm: dict[str, list[dict]] = defaultdict(list)
    for row in all_rows:
        by_firm[row["firm"]].append(row)

    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    write_summary_sheet(wb, all_rows)

    for firm in FIRM_ORDER:
        rows_firm = by_firm.get(firm, [])
        print(f"  → {firm}: {len(rows_firm)} строк")
        write_firm_sheet(wb, firm, rows_firm)

    for firm, rows_firm in by_firm.items():
        if firm not in FIRM_ORDER:
            write_firm_sheet(wb, firm or "Неизвестно", rows_firm)

    wb.save(OUTPUT_FILE)
    print(f"\n✓ Сохранено: {OUTPUT_FILE}")


if __name__ == "__main__":
    build_report()