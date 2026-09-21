"""
Отчёт «Работы по внесению, посеву, культивации, дискованию, боронованию»
=========================================================================
Формат вывода соответствует Excel-отчёту из Cropwise.

Отличие от базового отчёта:
  - Агрегируются данные ТОЛЬКО тех машин, суммарная обработанная площадь
    которых за сутки составила МЕНЕЕ 10 га.
  - Колонка «Боронование+Подсолнечник» заменена на пустую колонку «Комментарии».

Поля вывода:
  Агрофирма | Поле | Культура | Смена | Подтип работ | Машина | Водитель |
  Наименование агрегата | Норма выработки в час, га |
  Фактическая выработка в час, га | Обработанная площадь, га |
  Продолжительность работы в полях | Комментарии

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
from openpyxl.worksheet.pagebreak import Break

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

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

# Дата смены — по умолчанию сегодня; можно задать через REPORT_DATE=ГГГГ-ММ-ДД
REPORT_DATE: date = date.today()
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

SEASON_YEAR = REPORT_DATE.year

# Границы смен (часы, МСК)
MORNING_START_HOUR = 6    # 06:00 — начало утренней смены
MORNING_END_HOUR   = 18   # 18:00 — начало вечерней смены (утренняя до 17:59)

# Норма выработки по умолчанию (га/ч); если API не возвращает — ставим 25
DEFAULT_NORM_HA_H = 25.0

IDLE_THRESHOLD_MIN = 20

OUTPUT_FILE = f"report_under10ha_{REPORT_DATE}.xlsx"

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


async def fetch_mtfmi_and_resources_async(
    task_ids: list[int],
    tasks: list[dict],
    fgs_all: list[dict],
    gfs_all: list[dict],
) -> tuple[
    dict[int, list[dict]],
    dict[int, str],
    dict[int, str],
    dict[int, str],
    dict[int, str],
    dict[int, str],
    dict[int, dict],
]:
    """mtfmi + fields/machines/implements через mass_request (один httpx-клиент)."""
    from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks
    from report_resources_fetch import (
        collect_report_resource_ids,
        load_fields_list_async,
        load_implements_dict_async,
        load_machines_map_async,
    )

    if not task_ids:
        return {}, {}, {}, {}, {}, {}, {}

    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
    headers = {"X-User-Api-Token": API_TOKEN}
    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        mtfmi_map = await fetch_mtfmi_map_for_tasks(
            client, sem, BASE_URL, task_ids, log_prefix="[r24] ",
        )
        field_ids, machine_ids, implement_ids = collect_report_resource_ids(
            tasks, mtfmi_map,
        )
        print(
            f"  Ресурсы отчёта: полей={len(field_ids)}, "
            f"машин={len(machine_ids)}, орудий={len(implement_ids)}",
            flush=True,
        )
        fields_all, machines_map, implements_map = await asyncio.gather(
            load_fields_list_async(client, BASE_URL, field_ids, sem),
            load_machines_map_async(client, BASE_URL, machine_ids, sem),
            load_implements_dict_async(client, BASE_URL, implement_ids, sem),
        )

    field_to_firm = build_field_to_firm(fields_all, fgs_all, gfs_all)
    field_name = {
        int(f["id"]): f.get("name") or f.get("title") or f"field_{f['id']}"
        for f in fields_all
        if f.get("id") is not None
    }
    machine_name: dict[int, str] = {}
    machine_reg: dict[int, str] = {}
    machine_vin: dict[int, str] = {}
    for mid, m in machines_map.items():
        machine_name[mid] = (
            m.get("name") or m.get("title") or m.get("model") or f"machine_{mid}"
        )
        machine_reg[mid] = m.get("registration_number") or m.get("reg_number") or ""
        machine_vin[mid] = m.get("vin") or m.get("serial_number") or ""

    return (
        mtfmi_map,
        field_name,
        field_to_firm,
        machine_name,
        machine_reg,
        machine_vin,
        implements_map,
    )


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


def parse_hourly_dt(s) -> Optional[datetime]:
    """
    Парсит временную метку из covered_area_hourly.

    ВАЖНО: в отличие от start_time/end_time (где смещение часового пояса
    указано явно, например '...+03:00'), метки covered_area_hourly
    приходят от API как "наивная" строка БЕЗ смещения, и по факту это
    время в UTC, а не в МСК. Если распарсить их через parse_dt() (который
    трактует наивную строку как уже московское время), первые часы работы
    "уезжают" на 3 часа назад и ошибочно выпадают за границу суточного
    окна отчёта — площадь занижается.

    Подтверждено диагностикой: у задачи со start_time='...06:00:00+03:00'
    первая ненулевая точка covered_area_hourly='...04:00:00' физически не
    может быть раньше старта задачи, если это уже МСК. Но если считать её
    UTC, то это '07:00:00' МСК — на час позже старта, что логично.
    """
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    s = s.replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)   # <-- ключевое отличие от parse_dt()
    return dt.astimezone(TZ_MSK)


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

def calc_work_duration_seconds(task: dict) -> float:
    """
    Продолжительность работы в полях (в секундах) = work_duration - movements_on_road_duration.
    Оба поля берутся напрямую из machine_task (как в отчёте 5.main).
    """
    work_dur  = task.get("work_duration") or 0
    road_move = task.get("movements_on_road_duration") or 0
    return float(max(work_dur - road_move, 0))


# =============================================================================
# ЗАГРУЗКА СПРАВОЧНИКОВ
# =============================================================================

def build_field_to_crop(
    crop_id_name: dict[int, str],
    field_ids: set[int] | None = None,
) -> dict[int, str]:
    his_all = get_all("history_items", {"year": str(SEASON_YEAR)})
    field_to_crop: dict[int, str] = {}
    for hi in his_all:
        fid = hi.get("field_id")
        cid = hi.get("crop_id")
        if not (fid and cid):
            continue
        if field_ids is not None and int(fid) not in field_ids:
            continue
        cname = crop_id_name.get(cid, "")
        if not cname or cname in SKIP_CROPS:
            continue
        if hi.get("active"):
            field_to_crop[int(fid)] = cname
    return field_to_crop


def load_static_reference_data() -> tuple[
    list[dict],
    list[dict],
    dict[int, str],
    dict[int, str],
    dict[int, str],
]:
    """Лёгкие справочники; fields/machines/implements — после tasks через mass_request."""
    print("  field_groups / group_folders...")
    fgs_all = get_all("field_groups")
    gfs_all = get_all("group_folders")

    print("  Культуры...")
    crops_raw = get_all_page("crops")
    crop_id_name = {
        c["id"]: (c.get("name") or c.get("title", "")) for c in crops_raw
    }

    print("  Операторы...")
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

    return fgs_all, gfs_all, crop_id_name, operators_map, work_type_map


# =============================================================================
# СБОРКА ДАННЫХ ЗА СУТКИ
# =============================================================================

def collect_day_data(
    fgs_all: list[dict],
    gfs_all: list[dict],
    crop_id_name: dict[int, str],
    operators_map: dict[int, str],
    work_type_map: dict[int, str],
) -> list[dict]:
    """
    Собирает строки отчёта.
    Одна строка = машина × поле за суточное окно (06:00 → 06:00 следующего дня).
    """
    # Суточное окно: REPORT_DATE − 1 день 06:00 → REPORT_DATE 06:00
    # Например: REPORT_DATE=17.05 → окно 16.05 06:00 … 17.05 05:59
    _day_end_date = REPORT_DATE
    _day_start_date = _day_end_date - timedelta(days=1)
    day_start = datetime(
        _day_start_date.year, _day_start_date.month, _day_start_date.day,
        MORNING_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    day_end = datetime(
        _day_end_date.year, _day_end_date.month, _day_end_date.day,
        MORNING_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    day_end_str   = day_end.strftime("%Y-%m-%dT%H:%M:%S")

    print(
        f"\n  Загружаем machine_tasks за {REPORT_DATE} "
        f"({day_start_str} — {day_end_str})..."
    )

    all_tasks: list[dict] = []
    wt_ids_all: set[int] = set()
    for _group_name, wt_ids in WORK_GROUPS:
        wt_ids_all.update(wt_ids)

    mt_params = {
        "end_time_gt_eq": day_start_str,
        "end_time_lt":    day_end_str,
    }
    if wt_ids_all:
        from machine_tasks_batch_fetch import fetch_machine_tasks_by_wt_sync

        all_tasks = fetch_machine_tasks_by_wt_sync(
            get_all, wt_ids_all, mt_params, log_prefix="[r24] ",
        )
    else:
        all_tasks = get_all("machine_tasks", mt_params)

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

    task_ids = [t["id"] for t in all_tasks]
    print("  mtfmi + fields/machines/implements (mass_request)...")
    (
        mtfmi_map,
        field_name,
        field_to_firm,
        machine_name,
        machine_reg,
        machine_vin,
        implements_map,
    ) = asyncio.run(
        fetch_mtfmi_and_resources_async(task_ids, all_tasks, fgs_all, gfs_all)
    )
    field_ids = set(field_name)
    field_to_crop = build_field_to_crop(crop_id_name, field_ids)

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

        # Продолжительность работы в полях: work_duration - movements_on_road_duration
        work_secs  = calc_work_duration_seconds(task)
        work_hours = work_secs / 3600.0

        # Площадь по полям за всё суточное окно (day_start → day_end)
        field_cov: dict[int, float] = defaultdict(float)
        for item in items_for_task:
            fid = item.get("field_id")
            if not fid:
                continue
            hourly = item.get("covered_area_hourly") or []
            if hourly:
                cov = 0.0
                for entry in hourly:
                    h = parse_hourly_dt(entry[0])
                    if h and day_start <= h < day_end:
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
                work_type=work_type,
                wt_id=wt_id or 0,
                machine=machine_str,
                operator=operator_name,
                implement=implement_name or "—",
                norm_ha_h=norm_ha_h,
                fact_ha_h=0.0,
                covered_shift=0.0,
                duration_str=fmt_duration(work_secs),
                comment="",
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
                work_type=work_type,
                wt_id=wt_id or 0,
                machine=machine_str,
                operator=operator_name,
                implement=implement_name or "—",
                norm_ha_h=norm_ha_h,
                fact_ha_h=fact_ha_h,
                covered_shift=round(cov_shift, 2),
                duration_str=fmt_duration(work_secs),
                comment="",
            ))

    return rows


def _make_row(**kw) -> dict:
    return {
        "firm":          kw["firm"],
        "field_name":    kw["field_name"],
        "crop":          kw["crop"],
        "work_type":     kw["work_type"],
        "wt_id":         kw.get("wt_id", 0),
        "machine":       kw["machine"],
        "operator":      kw["operator"],
        "implement":     kw["implement"],
        "norm_ha_h":     kw["norm_ha_h"],
        "fact_ha_h":     kw["fact_ha_h"],
        "covered_shift": kw["covered_shift"],
        "duration_str":  kw["duration_str"],
        "comment":       "",
    }


# =============================================================================
# СТИЛИ
# =============================================================================

_C_HDR   = "8093B3"   # как в сводках 5–8
_C_TITLE = "7B8EB1"
_C_TOTAL = "BDD7EE"   # итоговая строка
_C_ODD   = "FFFFFF"
_C_EVEN  = "FFFFFF"


def _side() -> Border:
    t = Side(style="thin", color="000000")
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


def _area_text(value: object) -> str:
    """Площадь пишем текстом, чтобы LibreOffice/PDF не выравнивал число вправо."""
    if not isinstance(value, (int, float)):
        return "" if value is None else str(value)
    return f"{float(value):,.2f}".replace(",", " ")


# =============================================================================
# ЗАГОЛОВКИ И ШИРИНЫ КОЛОНОК
# =============================================================================

HEADERS = [
    "Культура",
    "Подтип работ",
    "Машина",
    "Обработанная\nплощадь, га",
    "Продолжительность\nработы в полях, ч",
    "Комментарии",
]

COL_W = [20, 32, 44, 14, 18, 34]
DETAIL_ROWS_PER_PAGE = 28


def _setup_print_layout(ws, *, header_row: int) -> None:
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = f"A{header_row + 1}"
    ws.print_title_rows = f"{header_row}:{header_row}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = 0.25
    ws.page_margins.right = 0.25
    ws.page_margins.top = 0.35
    ws.page_margins.bottom = 0.35
    ws.page_margins.header = 0.15
    ws.page_margins.footer = 0.15


# =============================================================================
# ЗАПИСЬ ЛИСТА
# =============================================================================

def _write_sheet_rows(ws, rows: list[dict], r_start: int) -> int:
    """Записывает строки данных, возвращает номер следующей строки."""
    r = r_start
    rows_on_page = 0
    for i, row in enumerate(rows):
        if rows_on_page >= DETAIL_ROWS_PER_PAGE:
            ws.row_breaks.append(Break(id=r - 1))
            rows_on_page = 0
        base_bg = _C_ODD if i % 2 == 0 else _C_EVEN

        vals = [
            (row["crop"],          base_bg, "left",   None),         # 1  Культура
            (row["work_type"],     base_bg, "left",   None),         # 2  Подтип работ
            (row["machine"],       base_bg, "left",   None),         # 3  Машина
            (_area_text(row["covered_shift"]), base_bg, "center", None),  # 4  Площадь
            (row["duration_str"],  base_bg, "center", None),         # 5  Продолжит.
            (row["comment"],       base_bg, "center", None),         # 6  Комментарии
        ]
        for ci, (val, bg, align, fmt) in enumerate(vals, start=1):
            _cell(ws, r, ci, val, bg=bg, halign=align, fmt=fmt, wrap=ci in {1, 2, 3, 6})
        ws.row_dimensions[r].height = 24
        r += 1
        rows_on_page += 1
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
            f" — {firm_name}  |  с 6:00 {(REPORT_DATE - timedelta(days=1)).strftime('%d.%m')}"
            f" по 6:00 {REPORT_DATE.strftime('%d.%m')}"
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
              halign="center", wrap=True, size=10)
    ws.row_dimensions[r].height = 48
    _setup_print_layout(ws, header_row=r)
    r += 1

    r = _write_sheet_rows(ws, rows, r)

    # Итоговые строки: посев и культивация отдельно (как в оригинале)
    def _total_row(label: str, filtered_rows: list[dict]) -> None:
        nonlocal r
        total_area = sum(
            row["covered_shift"] for row in filtered_rows
            if isinstance(row["covered_shift"], (int, float))
        )
        summary = [
            label, "", "",
            round(total_area, 2),
            "", "",
        ]
        for ci, val in enumerate(summary, start=1):
            if ci == 4 and isinstance(val, (int, float)):
                val = _area_text(val)
            fmt = None
            _cell(ws, r, ci, val, bold=True, bg=_C_TOTAL,
                  halign="center" if ci == 4 else "left", fmt=fmt)
        ws.row_dimensions[r].height = 16
        r += 1

    sow_rows  = [x for x in rows if "посев" in x["work_type"].lower()]
    cult_rows = [x for x in rows if "культив" in x["work_type"].lower()]
    if (sow_rows or cult_rows) and rows:
        ws.row_breaks.append(Break(id=max(1, r - 1)))
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
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        _cell(ws, r, 1, "Общий посев", bold=True, halign="right")
        _cell(ws, r, 4, _area_text(round(all_sow_area, 2)), bold=True,
              halign="center")
        ws.row_dimensions[r].height = 14
        r += 1

    if all_cult_area:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        _cell(ws, r, 1, "Общая культивация", bold=True, halign="right")
        _cell(ws, r, 4, _area_text(round(all_cult_area, 2)), bold=True,
              halign="center")
        ws.row_dimensions[r].height = 14

    ws.auto_filter.ref = f"A2:F{max(2, r)}"
    ws.print_area = f"A1:F{max(2, r)}"


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
            f"  |  с 6:00 {(REPORT_DATE - timedelta(days=1)).strftime('%d.%m')}"
            f" по 6:00 {REPORT_DATE.strftime('%d.%m')}"
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
              halign="center", wrap=True, size=10)
    ws.row_dimensions[r].height = 48
    _setup_print_layout(ws, header_row=r)
    r += 1

    # Сортировка: культура → подтип работ → машина
    sorted_rows = sorted(
        all_rows,
        key=lambda x: (x["crop"], x["work_type"], x["machine"]),
    )

    end_row = _write_sheet_rows(ws, sorted_rows, r) - 1
    ws.auto_filter.ref = f"A2:F{max(2, end_row)}"
    ws.print_area = f"A1:F{max(2, end_row)}"


# =============================================================================
# MAIN
# =============================================================================

def build_report() -> None:
    print(
        f"=== Отчёт по работам  |  "
        f"с 6:00 {(REPORT_DATE - timedelta(days=1)).strftime('%d.%m')} "
        f"по 6:00 {REPORT_DATE.strftime('%d.%m')} ===\n"
    )

    fgs_all, gfs_all, crop_id_name, operators_map, work_type_map = (
        load_static_reference_data()
    )

    all_rows = collect_day_data(
        fgs_all, gfs_all, crop_id_name, operators_map, work_type_map,
    )

    print(f"\n  Итого строк до фильтрации: {len(all_rows)}")

    # Полностью игнорируем задачи следующих групп — они не включаются в отчёт
    # ни при каких условиях (независимо от площади и других параметров машины).
    _IGNORE_GROUPS = {
        "4.1 Посев (овощи)",
        "5.4 Уборка картофеля, лука",
        "2.7 Гребнеобразование",
        "2.8 Глубокорыхление",
        "2.4 Вспашка",
        "5.0 Уборка",
    }
    _ignore_wt_ids: set[int] = {
        wt_id
        for gname, wt_ids in WORK_GROUPS
        if gname in _IGNORE_GROUPS
        for wt_id in wt_ids
    }
    if _ignore_wt_ids:
        before_ignore = len(all_rows)
        all_rows = [row for row in all_rows if row["wt_id"] not in _ignore_wt_ids]
        print(f"  Исключено задач (игнорируемые группы): {before_ignore - len(all_rows)}")

    # Суммарная площадь по паре (машина, вид работы): порог 0.5 <= total < 10 га.
    MachineWt = tuple  # (machine_str, wt_id)
    area_by_key: dict[MachineWt, float] = defaultdict(float)
    for row in all_rows:
        area = row["covered_shift"]
        if not isinstance(area, (int, float)):
            continue
        area_by_key[(row["machine"], row["wt_id"])] += area

    def _row_passes(row: dict) -> bool:
        area = row["covered_shift"]
        if not isinstance(area, (int, float)):
            return False
        total = area_by_key[(row["machine"], row["wt_id"])]
        return 0.5 <= total < 10.0

    all_rows = [row for row in all_rows if _row_passes(row)]

    print(f"  Строк после фильтрации (0.5 ≤ площадь < 10 га): {len(all_rows)}")

    # ------------------------------------------------------------------
    # Объединение строк с одинаковыми (культура, вид работы, машина).
    # Площадь суммируется; продолжительность суммируется;
    # fact_ha_h пересчитывается из итоговых значений.
    # Остальные поля берутся из первой встреченной строки.
    # ------------------------------------------------------------------
    def _parse_duration_secs(s: str) -> float:
        """'ЧЧ:ММ:СС' -> секунды."""
        try:
            parts = s.split(":")
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except Exception:
            return 0.0

    merged: dict[tuple, dict] = {}
    for row in all_rows:
        key = (row["crop"], row["work_type"], row["machine"])
        if key not in merged:
            merged[key] = dict(row)
            merged[key]["_dur_secs"] = _parse_duration_secs(row["duration_str"])
        else:
            base = merged[key]
            base["covered_shift"] = round(
                (base["covered_shift"] or 0.0) + (row["covered_shift"] or 0.0), 2
            )
            base["_dur_secs"] += _parse_duration_secs(row["duration_str"])

    # Пересчитываем производные поля после слияния
    for row in merged.values():
        dur_secs = row.pop("_dur_secs", 0.0)
        row["duration_str"] = fmt_duration(dur_secs)
        work_hours = dur_secs / 3600.0
        cov = row["covered_shift"] or 0.0
        row["fact_ha_h"] = round(cov / work_hours, 2) if work_hours > 0 else 0.0

    all_rows = list(merged.values())
    print(f"  Строк после объединения (crop+work_type+machine): {len(all_rows)}")

    # Фильтр: исключаем строки, у которых продолжительность работы в полях < 5 минут
    # И обработанная площадь < 0.5 га (оба условия должны выполняться одновременно).
    def _short_and_small(row: dict) -> bool:
        """Возвращает True, если строку нужно исключить."""
        dur_secs = _parse_duration_secs(row["duration_str"])
        area     = row["covered_shift"] if isinstance(row["covered_shift"], (int, float)) else 0.0
        return dur_secs < 300 and area < 0.5   # 300 сек = 5 минут

    before_short = len(all_rows)
    all_rows = [row for row in all_rows if not _short_and_small(row)]
    print(
        f"  Строк после фильтра (< 5 мин и < 0.5 га): "
        f"{len(all_rows)} (отсеяно: {before_short - len(all_rows)})"
    )

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