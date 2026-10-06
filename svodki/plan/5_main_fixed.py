# 11.06.2026
"""
26 2 ropwise Operations — отчёт "Посев"
=====================================
Виды посева: wt=3,4,69,70,71,72,119,120
Овощи:       wt=68,148,172

Логика:
- Площадь: covered_area из machine_task где end_time в [DAY_START, CUTOFF)
- DAY_START = report_date - 1 день + DAY_START_HOUR (по умолчанию 06:00)
- CUTOFF    = report_date + DAY_END_HOUR   (по умолчанию 06:00)
- Время:   work_duration - movements_on_road_duration из machine_task
- Скорость: average_speed_on_field (м/с) × 3.6 → км/ч, только при covered_area > 0
- Сред. выработка по сезону: среднее(площадь_дня / машин_в_день) по всем дням сезона
- Серверные фильтры: end_time_gt_eq/lt, date_start_lt_eq + date_end_gt_eq/no_date_end
- Все агрофирмы обрабатываются параллельно
- Один httpx.AsyncClient и один Semaphore на весь процесс
"""

from __future__ import annotations


import asyncio
import importlib.util
import json
import math
import os
import sys
import traceback
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import plans  # noqa: F401  # bootstrap utils/* (plan_paths via plans.py)
from cropwise_paging import CROPWISE_PAGE_LIMIT, from_id_should_continue
from report_fetch_orchestration import (
    TasksMtfmiFetchOptions,
    fetch_all_pages,
    fetch_all_tasks_and_mtfmi as _fetch_tasks_mtfmi_shared,
    report_async_client,
    run_report_async,
)
from report_pipeline import run_parallel_refs_and_fetch
import httpx
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import pandas as pd
import requests
from plans import *
from excel_number_fmt import NF_DEC1, NF_DEC2, NF_INT, summary_cell_value_and_format
from detail_sheet_pagination import (
    DetailPaginationConfig,
    DetailPageContext,
    compute_firm_single_page,
    write_paginated_section,
)
from plans import (
    API_TOKEN,
    BASE_URL,
    TZ_MSK,
    FIRM_TO_FOLDER,
    FIRM_ORDER,
    HIRED_GROUP_ID,
    AVAILABLE_OWN,
    PLAN_RATE_PER_GROUP,
    PLAN_SEEDING_HA,
    VEG_WT_FALLBACK_CROP,
    SKIP_CROPS,
    build_field_to_crop,
)

_SEED_KEY = "4.0 Посев"
_VEG_KEY  = "4.1 Посев (овощи)"

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================
REPORT_DATE = date(2026, 9, 5)
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])
SEASON_YEAR = REPORT_DATE.year
# Cropwise: посев озимых — machine_task.season == год отчёта + 1 (как 2_main_ideal_compact).
SEASON_TASK_YEAR_STR = str(SEASON_YEAR + 1)

DAY_START_HOUR: int = 6   # час начала суток
DAY_END_HOUR:   int = 6   # час конца суток (если == DAY_START_HOUR → следующий день)

# Основные виды посева — берём ID из WORK_GROUPS по ключу "4.0 Посев"
ALL_WT_IDS: set[int] = {
    wt_id
    for group_name, wt_ids in WORK_GROUPS
    if group_name == _SEED_KEY
    for wt_id in wt_ids
}
WORK_TYPES: set[int] = ALL_WT_IDS

# Овощные виды посева/посадки — берём ID из WORK_GROUPS по ключу "4.1 Посев (овощи)"
VEG_WT_IDS: set[int] = {
    wt_id
    for group_name, wt_ids in WORK_GROUPS
    if group_name == _VEG_KEY
    for wt_id in wt_ids
}
VEG_WORK_TYPES: set[int] = VEG_WT_IDS
MANUAL_OVERRIDES_ENV = "SVODKI_MANUAL_OVERRIDES_PATH"

# region_id только для совместимости (для запросов по machine_region_mapping_items)
_REGION_IDS: dict[str, int] = {
    "АФ Чистопольская":            7,
    "АгроРаздолье":                9,
    "Аксу-Агро":                   8,
    "НурлатАгро":                 39,
    "Дрожжаное Рассвет Агро":     11,
    "Актай":                      10,
    "Лениногорская Птицефабрика": 38,
}

# название → (region_id, план га/сутки, собств. агрегатов в наличии)
_BASE_SEED_AGROFIRMS: dict[str, tuple[int, float, int]] = {
    firm: (
        _REGION_IDS[firm],
        PLAN_RATE_PER_GROUP.get(_SEED_KEY, 200.0),
        AVAILABLE_OWN.get(_SEED_KEY, {}).get(firm, 0),
    )
    for firm in FIRM_TO_FOLDER
}

# Плановая выработка и кол-во агрегатов для овощей
_BASE_VEG_AGROFIRMS: dict[str, tuple[int, float, int]] = {
    firm: (
        _REGION_IDS[firm],
        PLAN_RATE_PER_GROUP.get(_VEG_KEY, 10.0),
        AVAILABLE_OWN.get(_VEG_KEY, {}).get(firm, 0),
    )
    for firm in FIRM_TO_FOLDER
}


def _merge_manual_report5(
    seed_base: dict[str, tuple[int, float, int]],
    veg_base: dict[str, tuple[int, float, int]],
) -> tuple[dict[str, tuple[int, float, int]], dict[str, tuple[int, float, int]]]:
    path = os.environ.get(MANUAL_OVERRIDES_ENV)
    if not path:
        return seed_base, veg_base
    p = Path(path)
    if not p.is_file():
        return seed_base, veg_base
    try:
        store = json.loads(p.read_text(encoding="utf-8"))
        block = store.get("5") or {}
    except Exception:
        return seed_base, veg_base

    def _apply(
        base: dict[str, tuple[int, float, int]],
        plan_key: str,
        own_key: str,
    ) -> dict[str, tuple[int, float, int]]:
        out = {k: list(v) for k, v in base.items()}
        if isinstance(block.get(plan_key), dict):
            for k, v in block[plan_key].items():
                if k in out:
                    try:
                        out[k][1] = float(v)
                    except (TypeError, ValueError):
                        pass
        if isinstance(block.get(own_key), dict):
            for k, v in block[own_key].items():
                if k in out:
                    try:
                        out[k][2] = int(v)
                    except (TypeError, ValueError):
                        pass
        return {k: tuple(t) for k, t in out.items()}

    seed = _apply(
        seed_base,
        "seeding_plan_rate_by_firm",
        "seeding_own_count_by_firm",
    )
    veg = _apply(
        veg_base,
        "veg_plan_rate_by_firm",
        "veg_own_count_by_firm",
    )
    return seed, veg


AGROFIRMS, VEG_AGROFIRMS = _merge_manual_report5(_BASE_SEED_AGROFIRMS, _BASE_VEG_AGROFIRMS)
VEG_REPORT_FIRMS: tuple[str, ...] = ("АФ Чистопольская",)

FIRM_LABELS_TABLE: dict[str, str] = {
    "НурлатАгро": "Нурлат Агро",
}


def _firm_table_label(firm_key: str) -> str:
    return FIRM_LABELS_TABLE.get(firm_key, firm_key)

VEG_CROP_ROWS: tuple[str, ...] = VEG_CROP_ROWS  # из plans

# План по овощным культурам (га) — в plans.py как VEG_CROP_PLAN_HA (импорт выше).
# Раньше здесь был HARDCODED_PLAN[_VEG_KEY], но ключи HARDCODED_PLAN — тройки (группа, фирма, культура),
# из-за этого возникает KeyError.


def _normalize_crop_name(raw: str) -> str:
    s = str(raw or "").strip().lower().replace("ё", "е")
    for ch in "-_/,.":
        s = s.replace(ch, " ")
    return " ".join(s.split())


def _map_veg_crop_label(raw_name: str) -> str | None:
    n = _normalize_crop_name(raw_name)
    if not n:
        return None
    if "карто" in n:
        return "Картофель"
    if "лук" in n:
        return "Лук"
    if "морков" in n:
        return "Морковь"
    if "свек" in n:
        return "Свекла"
    return None


def _task_crop_name(task: dict, crop_names_by_id: dict[int, str] | None = None) -> str:
    cid = task.get("crop_id")
    if crop_names_by_id and cid is not None:
        try:
            mapped = crop_names_by_id.get(int(cid), "")
            if mapped:
                return mapped
        except (TypeError, ValueError):
            pass
    name = str(
        task.get("crop_name")
        or task.get("crop_title")
        or task.get("culture_name")
        or task.get("culture_title")
        or task.get("field_crop_name")
        or task.get("field_crop_title")
        or ""
    ).strip()
    if name:
        return name
    wt = task.get("work_type_id")
    try:
        if wt is not None:
            mapped = VEG_WT_FALLBACK_CROP.get(int(wt))
            if mapped:
                return mapped
    except (TypeError, ValueError):
        pass
    return ""


def _task_work_type_id(task: dict) -> int | None:
    raw = task.get("work_type_id")
    if raw is None:
        wt_obj = task.get("work_type")
        if isinstance(wt_obj, dict):
            raw = wt_obj.get("id")
    try:
        if raw is None:
            return None
        return int(raw)
    except (TypeError, ValueError):
        return None


def _task_veg_label(
    task: dict,
    crop_names_by_id: dict[int, str] | None = None,
    field_id: int | None = None,
    field_crop_by_id: dict[int, str] | None = None,
) -> str | None:
    # 1. Пробуем определить культуру по данным самого задания (crop_id / crop_name и т.п.)
    crop_name = _task_crop_name(task, crop_names_by_id)
    by_crop = _map_veg_crop_label(crop_name)
    if by_crop:
        return by_crop

    # 2. Вариант 3: смотрим культуру из поля (field_id → crop_name из справочника полей)
    if field_id is not None and field_crop_by_id:
        field_crop_name = field_crop_by_id.get(field_id, "")
        if field_crop_name:
            by_field = _map_veg_crop_label(field_crop_name)
            if by_field:
                return by_field

    # 3. Fallback по work_type_id (только если культура не определена выше)
    wt_id = _task_work_type_id(task)
    if wt_id is not None:
        return VEG_WT_FALLBACK_CROP.get(wt_id)
    return None


def _task_plan_area(task: dict) -> float:
    for key in ("planned_area", "area", "field_area", "target_area"):
        try:
            v = float(task.get(key) or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
    return 0.0


SEMAPHORE_LIMIT = 40
machines_map: dict[int, dict] = {}


# =============================================================================
# ВЫЧИСЛЕНИЕ ВРЕМЕННОГО ДИАПАЗОНА
# =============================================================================

REPORT_WINDOW_START_RAW = os.environ.get("REPORT_WINDOW_START", "").strip()
REPORT_WINDOW_END_RAW = os.environ.get("REPORT_WINDOW_END", "").strip()


def _parse_report_window(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=TZ_MSK)
        except ValueError:
            continue
    return parse_dt(text)


def compute_time_range(
    report_date:    date,
    day_start_hour: int = DAY_START_HOUR,
    day_end_hour:   int = DAY_END_HOUR,
) -> tuple[datetime, datetime]:
    """
    Возвращает (day_start, cutoff) как datetime с TZ_MSK.

    Если day_start_hour == day_end_hour → суточный диапазон
        day_start = (report_date - 1 день) в day_start_hour:00
        cutoff    =  report_date           в day_end_hour:00

    Если day_start_hour != day_end_hour → дневная смена того же дня
        day_start = report_date в day_start_hour:00
        cutoff    = report_date в day_end_hour:00
    """
    if day_start_hour == day_end_hour:
        day_start = datetime(
            report_date.year, report_date.month, report_date.day,
            day_start_hour, 0, 0, tzinfo=TZ_MSK
        ) - timedelta(days=1)
        cutoff = datetime(
            report_date.year, report_date.month, report_date.day,
            day_end_hour, 0, 0, tzinfo=TZ_MSK
        )
    else:
        day_start = datetime(
            report_date.year, report_date.month, report_date.day,
            day_start_hour, 0, 0, tzinfo=TZ_MSK
        )
        cutoff = datetime(
            report_date.year, report_date.month, report_date.day,
            day_end_hour, 0, 0, tzinfo=TZ_MSK
        )
    return day_start, cutoff


def resolve_time_range(
    report_date: date,
    day_start_hour: int = DAY_START_HOUR,
    day_end_hour: int = DAY_END_HOUR,
) -> tuple[datetime, datetime]:
    """Суточное окно с учётом REPORT_WINDOW_* (автогенерация по расписанию)."""
    day_start, cutoff = compute_time_range(report_date, day_start_hour, day_end_hour)
    ws = _parse_report_window(REPORT_WINDOW_START_RAW)
    we = _parse_report_window(REPORT_WINDOW_END_RAW)
    if ws is not None and we is not None and ws < we:
        return ws, we
    return day_start, cutoff


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


def seconds_to_hms(seconds: float) -> str:
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    sec = total % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"


def get_all_from_id(endpoint: str,
                    extra_params: dict | None = None) -> list[dict]:
    params  = dict(extra_params or {})
    results = []
    from_id = 0
    while True:
        r = session.get(f"{BASE_URL}/{endpoint}",
                        params={**params, "from_id": from_id, "limit": CROPWISE_PAGE_LIMIT})
        r.raise_for_status()
        body  = r.json()
        chunk = body.get("data", [])
        if not chunk: break
        results.extend(chunk)
        meta    = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not from_id_should_continue(meta, chunk):
            break
        from_id = last_id + 1
    return results


# =============================================================================
# АСИНХРОННЫЕ ЗАПРОСЫ
# =============================================================================

def parse_dt(s) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        ts = float(s)
        if ts > 1e12:
            ts /= 1000.0
        try:
            from datetime import timezone as _tz
            return datetime.fromtimestamp(ts, tz=_tz.utc).astimezone(TZ_MSK)
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


async def fetch_mtfmi(
    client:    httpx.AsyncClient,
    task_id:   int,
    semaphore: asyncio.Semaphore,
) -> tuple[int, list[dict]]:
    items = await fetch_all_pages(
        client,
        f"{BASE_URL}/machine_task_field_mapping_items",
        {"machine_task_id": task_id},
        semaphore,
    )
    return task_id, items


def _load_field_to_firm() -> dict[int, str]:
    """Строит field_id → firm_name (полный справочник fields)."""
    print("  Загружаем fields / field_groups / group_folders для field_to_firm...")
    return _build_field_to_firm(
        get_all_from_id("fields"),
        get_all_from_id("field_groups"),
        get_all_from_id("group_folders"),
    )


def _build_field_to_firm(
    fields_all: list[dict],
    fgs_all:    list[dict],
    gfs_all:    list[dict],
) -> dict[int, str]:
    from plans import FIELD_NAME_FORCE_RULES

    def _norm(name: str) -> str:
        return (name or "").strip().casefold().replace(" ", "").replace("\u00a0", "")

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

    def _gf_hints(gfs: list[dict]) -> dict[int, str]:
        hints: dict[int, str] = {}
        for gf in gfs:
            fid = gf.get("id")
            if fid is None:
                continue
            blob = _norm(f"{gf.get('name') or ''} {gf.get('title') or ''}")
            if any(x in blob for x in ("лениногор", "птицефабрик", "лпф", "lpf")):
                hints[int(fid)] = "Лениногорская Птицефабрика"
        return hints

    def _chain(folder_id, parent_map):
        if folder_id is None:
            return []
        out, cur, seen = [], int(folder_id), set()
        for _ in range(64):
            if cur in seen:
                break
            seen.add(cur); out.append(cur)
            nxt = parent_map.get(cur)
            if nxt is None:
                break
            cur = int(nxt)
        return out

    def _resolve_root(folder_id, parent_map, root_ids):
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

    fgs_map    = {fg["id"]: fg for fg in fgs_all}
    parent_map = _folder_parent_map(gfs_all)
    root_ids   = set(FIRM_TO_FOLDER.values())
    hints      = _gf_hints(gfs_all)

    field_to_firm: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        fgid       = f.get("field_group_id")
        raw_folder = f.get("group_folder_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(fgid, {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None

        firm_key = None
        for cid in _chain(rint, parent_map):
            if cid in hints:
                firm_key = hints[cid]
                break
        if firm_key is None:
            root = _resolve_root(rint, parent_map, root_ids)
            if root is not None:
                firm_key = FOLDER_TO_FIRM.get(root)
        if firm_key:
            field_to_firm[int(fid)] = firm_key

    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        blob = _norm(f"{f.get('name') or ''} {f.get('title') or ''}")
        for needle, fk in FIELD_NAME_FORCE_RULES:
            if needle in blob:
                field_to_firm[int(fid)] = fk
                break

    print(f"  Полей с привязкой к фирме: {len(field_to_firm)}")
    return field_to_firm


def _load_field_to_firm_full() -> dict[int, str]:
    return _load_field_to_firm()


def _task_season_value(task: dict) -> str:
    return str(task.get("season") or task.get("year") or "").strip()


def _editor_season_strings(group_name: str) -> set[str]:
    """Сезоны группы из редактора планов — и для суток, и для сезона."""
    try:
        season_dir = Path(__file__).resolve().parent / "utils" / "season"
        if not season_dir.is_dir():
            season_dir = Path(__file__).resolve().parents[1] / "svodki" / "plan" / "utils" / "season"
        if season_dir.is_dir() and str(season_dir) not in sys.path:
            sys.path.insert(0, str(season_dir))
        from season_unified import task_season_years_set_for_group

        years = task_season_years_set_for_group(group_name, REPORT_DATE)
        if years:
            return set(years)
    except Exception:
        pass
    return {str(REPORT_DATE.year), str(REPORT_DATE.year + 1)}


def _filter_tasks_by_cropwise_season(
    tasks: list[dict],
    allowed: set[str] | None = None,
) -> list[dict]:
    """Задания сезонов редактора планов (сутки и сезон — один список)."""
    years = allowed if allowed else ({SEASON_TASK_YEAR_STR} if SEASON_TASK_YEAR_STR else set())
    if not years:
        return list(tasks)
    return [t for t in tasks if _task_season_value(t) in years]


def _log_task_season_counts(tasks: list[dict], label: str, allowed: set[str]) -> None:
    from collections import Counter

    counts = Counter(_task_season_value(t) or "<пусто>" for t in tasks)
    need = "+".join(sorted(allowed))
    print(f"  [{label}] сезон заданий (нужен {need}): {dict(counts)}")


async def fetch_all_tasks_and_mtfmi(
    client:        httpx.AsyncClient,
    semaphore:     asyncio.Semaphore,
    day_start:     datetime,
    cutoff:        datetime,
    season_start:  datetime,
    work_type_ids: set[int],
    label:         str = "",
) -> tuple[list[dict], list[dict], dict[int, list[dict]]]:
    lp = f"[{label}] " if label else ""
    return await _fetch_tasks_mtfmi_shared(
        client, semaphore, BASE_URL,
        day_start, cutoff, season_start, work_type_ids,
        TasksMtfmiFetchOptions(
            log_prefix=lp,
            mtfmi_log_prefix=lp,
            print_mtfmi_banner=False,
            season_start_label=season_start.strftime("%d.%m.%Y"),
        ),
    )


# =============================================================================
# ПОДСЧЁТ ПО ВСЕМ АГРОФИРМАМ (через поля из MTFMI, как в файле 3)
# =============================================================================

def _field_ok_for_seeding_fact(
    fid_i: int,
    *,
    fact_by_all_fields: bool,
    available_field_ids: set[int] | None,
    field_crop_by_id: dict[int, str] | None,
) -> bool:
    """Как FACT_BY_ALL_FIELDS_GROUPS в 2_main_ideal_compact для «4.0 Посев»."""
    if fact_by_all_fields:
        return True
    if available_field_ids is not None and fid_i not in available_field_ids:
        return False
    if field_crop_by_id is not None and fid_i not in field_crop_by_id:
        return False
    return True


def build_all_firm_stats(
    tasks_today:      list[dict],
    tasks_season:     list[dict],
    mtfmi_map:        dict[int, list[dict]],
    field_to_firm:    dict[int, str],
    day_start:        datetime,
    cutoff:           datetime,
    report_date:      date,
    work_type_ids:    set[int],
    min_area_ha:      float,
    crop_names_by_id: dict[int, str] | None = None,
    min_count_area_ha: float = 0.0,
    field_crop_by_id: dict[int, str] | None = None,
    available_field_ids: set[int] | None = None,
    fact_by_all_fields: bool = False,
) -> dict[str, dict]:
    """
    Распределяет задания по агрофирмам через field_to_firm из MTFMI.
    Площадь за сутки — covered_area_hourly < cutoff (как в файле 3).

    Фильтрация (если fact_by_all_fields=False):
    - Поле должно быть в available_field_ids (если передан).
    - Поле должно быть в field_crop_by_id.

    Для посева (fact_by_all_fields=True, как 2_main_ideal_compact): фирма из
    полной карты полей, без обязательной культуры в sowing-карте и без available.
    """
    all_firms = list(AGROFIRMS.keys())

    # per_firm_machine[firm][machine_id] → агрегация за сутки
    per_firm_machine: dict[str, dict[int, dict]] = {f: {} for f in all_firms}

    for t in tasks_today:
        if t.get("work_type_id") not in work_type_ids:
            continue
        mid   = t.get("machine_id")
        items = mtfmi_map.get(t["id"], [])

        # Общая площадь задания в окне (для масштабирования времени)
        total_cov_in_window = 0.0
        for item in items:
            for entry in (item.get("covered_area_hourly") or []):
                h = parse_dt(entry[0])
                if h and h < cutoff:
                    total_cov_in_window += entry[1] or 0

        # Площадь по фирмам через covered_area_hourly < cutoff
        firm_cov: dict[str, float] = defaultdict(float)
        for item in items:
            fid  = item.get("field_id")
            if fid is None:
                continue
            fid_i = int(fid)
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue
            if not _field_ok_for_seeding_fact(
                fid_i,
                fact_by_all_fields=fact_by_all_fields,
                available_field_ids=available_field_ids,
                field_crop_by_id=field_crop_by_id,
            ):
                continue
            cov_item = 0.0
            for entry in (item.get("covered_area_hourly") or []):
                h = parse_dt(entry[0])
                if h and h < cutoff:
                    cov_item += entry[1] or 0
            if cov_item <= 0:
                # fallback если hourly пустой
                cov_item = float(item.get("covered_area") or 0)
            if cov_item > 0:
                firm_cov[firm] += cov_item

        for firm, cov in firm_cov.items():
            if cov <= 0:
                continue
            pm = per_firm_machine[firm]
            if mid not in pm:
                pm[mid] = {"cov_total": 0.0, "work_dur": 0.0,
                           "work_area": 0.0, "speeds": []}
            pm[mid]["cov_total"] += cov

            work_dur  = t.get("work_duration") or 0
            road_move = t.get("movements_on_road_duration") or 0
            work_net  = max(work_dur - road_move, 0)
            # Масштабируем время пропорционально доле площади фирмы
            if total_cov_in_window > 0:
                pm[mid]["work_dur"] += work_net * (cov / total_cov_in_window)
            else:
                pm[mid]["work_dur"] += work_net

            pm[mid]["work_area"] += t.get("work_area") or 0
            sp = t.get("average_speed_on_field")
            if sp and sp > 0:
                pm[mid]["speeds"].append(sp)

    # Сезонная статистика через MTFMI + covered_area_hourly (как в файле 3)
    season_daily_area:     dict[str, dict[str, float]]    = {f: defaultdict(float) for f in all_firms}
    season_daily_machines: dict[str, dict[str, set[int]]] = {f: defaultdict(set)   for f in all_firms}

    # Овощные факты за сезон и сутки
    veg_fact_by_firm_crop:  dict[str, dict[str, float]] = {f: defaultdict(float) for f in all_firms}
    veg_today_by_firm_crop: dict[str, dict[str, float]] = {f: defaultdict(float) for f in all_firms}

    for t in tasks_season:
        if t.get("work_type_id") not in work_type_ids:
            continue
        mid    = t.get("machine_id")
        end_dt = parse_dt(t.get("end_time"))
        if not end_dt:
            continue
        anchor = datetime(end_dt.year, end_dt.month, end_dt.day,
                          DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
        if end_dt < anchor:
            shift_date   = (end_dt - timedelta(days=1)).date().isoformat()
            shift_cutoff = anchor
        else:
            shift_date   = end_dt.date().isoformat()
            shift_cutoff = anchor + timedelta(days=1)

        # Текущую смену в сезонную среднюю не включаем
        if date.fromisoformat(shift_date) >= report_date:
            continue

        items = mtfmi_map.get(t["id"], [])
        for item in items:
            fid  = item.get("field_id")
            if fid is None:
                continue
            fid_i = int(fid)
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue
            if not _field_ok_for_seeding_fact(
                fid_i,
                fact_by_all_fields=fact_by_all_fields,
                available_field_ids=available_field_ids,
                field_crop_by_id=field_crop_by_id,
            ):
                continue
            cov = 0.0
            for entry in (item.get("covered_area_hourly") or []):
                h = parse_dt(entry[0])
                if h and h < shift_cutoff:
                    cov += float(entry[1] or 0)
            if cov <= 0.5:
                continue
            season_daily_area[firm][shift_date] += cov
            if mid is not None:
                season_daily_machines[firm][shift_date].add(int(mid))

        # Овощные факты за сезон — культура строки из history_item_id.
        from mtfmi_batch_fetch import crop_of_item

        for item in items:
            fid  = item.get("field_id")
            if fid is None:
                continue
            fid_i = int(fid)
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue
            if available_field_ids is not None and fid_i not in available_field_ids:
                continue
            raw_crop = crop_of_item(item, "")
            veg_label = _map_veg_crop_label(raw_crop) if raw_crop else None
            if not veg_label:
                continue
            cov = float(item.get("covered_area") or 0)
            if cov > 0:
                veg_fact_by_firm_crop[firm][veg_label] += cov

    # Овощные факты за сутки — культура строки из history_item_id.
    from mtfmi_batch_fetch import crop_of_item

    for t in tasks_today:
        if t.get("work_type_id") not in work_type_ids:
            continue
        items = mtfmi_map.get(t["id"], [])
        for item in items:
            fid  = item.get("field_id")
            if fid is None:
                continue
            fid_i = int(fid)
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue
            if available_field_ids is not None and fid_i not in available_field_ids:
                continue
            raw_crop = crop_of_item(item, "")
            veg_label = _map_veg_crop_label(raw_crop) if raw_crop else None
            if not veg_label:
                continue
            cov = 0.0
            for entry in (item.get("covered_area_hourly") or []):
                h = parse_dt(entry[0])
                if h and h < cutoff:
                    cov += float(entry[1] or 0)
            if cov <= 0:
                cov = float(item.get("covered_area") or 0)
            if cov > 0:
                veg_today_by_firm_crop[firm][veg_label] += cov

    # Собираем результат по каждой фирме
    result: dict[str, dict] = {}
    for firm in all_firms:
        pm = per_firm_machine[firm]
        da = season_daily_area[firm]

        season_sum   = sum(da.values())
        machine_days = sum(
            len(season_daily_machines[firm].get(day, set()))
            for day, area in da.items() if area > 0
        )
        avg_season = round(season_sum / machine_days, 1) if machine_days else 0.0

        pm_filtered = {mid: d for mid, d in pm.items()
                       if round(d["cov_total"], 2) >= min_area_ha}
        _count_threshold = max(min_area_ha, min_count_area_ha)
        pm_counted = {
            mid: d for mid, d in pm_filtered.items()
            if round(d["cov_total"], 2) >= _count_threshold
        }

        detail_own:   list[dict] = []
        detail_hired: list[dict] = []
        for mid, d in pm_counted.items():
            m     = machines_map.get(mid, {})
            group = m.get("machine_group_id")
            cov   = round(d["cov_total"], 2)
            secs  = d["work_dur"]
            rate  = round(cov / (secs / 3600), 1) if secs else 0.0
            speed = round(sum(d["speeds"]) / len(d["speeds"]) * 3.6, 1) \
                    if d["speeds"] else 0.0
            coef  = round(d["cov_total"] / d["work_area"] * 100, 0) \
                    if d["work_area"] else 0.0
            row = {
                "Машина":        m.get("name", f"machine_{mid}"),
                "Площадь, га":   cov,
                "Время (сек)":   secs,
                "Время на полях": seconds_to_hms(secs),
                "Ср. выр-ка га/час": rate,
                "Средняя скорость на полях км/ч": speed,
                "Коэффициент использования конструкционной ширины захвата, %": int(coef),
            }
            if group == HIRED_GROUP_ID:
                detail_hired.append(row)
            else:
                detail_own.append(row)

        detail_own.sort(key=lambda x:   -x["Площадь, га"])
        detail_hired.sort(key=lambda x: -x["Площадь, га"])

        area_today   = sum(d["cov_total"] for d in pm_filtered.values())
        engine_today = sum(d["work_dur"]  for d in pm_counted.values())

        result[firm] = {
            "season_area_total":  round(season_sum, 2),
            "machine_days_total": machine_days,
            "machines_today":     len(pm_counted),
            "area_today":         round(area_today, 2),
            "engine_today":       round(engine_today / 3600, 1),
            "avg_season":         avg_season,
            "detail_own":         detail_own,
            "detail_hired":       detail_hired,
            "veg_plan_by_crop":   {},
            "veg_fact_by_crop":   {k: round(v, 2) for k, v in veg_fact_by_firm_crop[firm].items()},
            "veg_today_by_crop":  {k: round(v, 2) for k, v in veg_today_by_firm_crop[firm].items()},
        }

    return result


# =============================================================================
# СТИЛИ
# =============================================================================

def _make_styles() -> dict:
    thin   = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    return {
        "border":        border,
        "align_c":       Alignment(horizontal="center", vertical="center",
                                    wrap_text=True),
        "align_l":       Alignment(horizontal="left", vertical="center", wrap_text=True),
        "align_hdr":     Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=True,
        ),
        "fill_hdr":      PatternFill("solid", fgColor="8093B3"),
        # По требованию: обычные строки без заголовков/итого должны быть белыми.
        "fill_own":      PatternFill("solid", fgColor="FFFFFF"),
        "fill_hired":    PatternFill("solid", fgColor="FFFFFF"),
        "fill_total":    PatternFill("solid", fgColor="BDD7EE"),
        # Вся "обычная" часть таблицы должна быть белой.
        # Цвет оставляем только для заголовка столбцов и строки итогов.
        "fill_sec_own":  PatternFill("solid", fgColor="FFFFFF"),
        "fill_sec_hire": PatternFill("solid", fgColor="FFFFFF"),
        "font_hdr":      Font(bold=True, color="FFFFFF", size=13),
        "font_data":     Font(size=13),
        "font_total":    Font(bold=True, size=13),
        "font_title":    Font(bold=True, size=13),
        "font_sec":      Font(bold=True, size=12, color="111827"),
    }


def _align_summary_data(ci: int, col_key: str, raw_val: object, s: dict):
    """Без переноса для «Итого», иначе wrap ломает слово на «итог»/«о»."""
    val = raw_val
    if str(val).strip() == "Итого" and col_key == "Агрофирма":
        return Alignment(vertical="center", horizontal="left", wrap_text=False)
    if ci == 2:
        return Alignment(vertical="center", horizontal="left", wrap_text=False)
    return s["align_c"]


DETAIL_COLS = [
    "№",
    "Машина",
    "Площадь,\nга",
    "Время работы\nна полях, ч",
    "Ср. выр-ка\nга/час",
    "Средняя скорость\nна полях, км/ч",
    "Коэффициент использования\nконструкционной ширины\nзахвата, %",
]
MIN_DETAIL_AREA_HA = 0.5
# Порог для таблиц «Собственные» / «Наёмные» и счётчика техники (как в сводке 7).
MIN_COUNT_AREA_HA = 10.0

# --- Отдельные листы посева по озимым культурам (как «Вспашка — Рапс озимый» в 9_main) ---
CROP_RAPESEED_WINTER = "Рапс озимый"
CROP_WHEAT_WINTER = "Пшеница озимая"

CROP_SEEDING_SUMMARY_COLS = [
    "Агрофирмы",
    "План, га",
    "Факт общий, га",
    "Остаток, га",
    "Доля факта от плана, %",
    "Факт за сутки, га",
    "Общее время работы на полях, час",
    "Среднее время эффективной работы на поле на 1 агрегат., час.",
    "Часовая выработка в расчете на 1 агрегат, га",
]

_CROP_SEEDING_SHEETS: tuple[tuple[str, str, str], ...] = (
    (CROP_RAPESEED_WINTER, "4.0 Посев — Рапс озимый", "Посев Рапс озимый"),
    (CROP_WHEAT_WINTER, "4.0 Посев — Пшеница озимая", "Посев Пшеница озимая"),
)


def _safe_float(v: object) -> float:
    try:
        x = float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) else 0.0


def _safe_hhmm(hours_val: object) -> str:
    h = _safe_float(hours_val)
    return seconds_to_hms(max(0, int(round(h * 3600))))


def _estimate_wrapped_lines(value: object, col_width: float | None) -> int:
    text = str(value or "").strip()
    if not text:
        return 1
    width = float(col_width or 10)
    chars_per_line = max(1, int(width * 0.78))
    parts = text.splitlines() or [text]
    lines = 0
    for part in parts:
        p = part.strip()
        if not p:
            lines += 1
            continue
        lines += max(1, math.ceil(len(p) / chars_per_line))
    return max(1, lines)


def _auto_fit_row_heights(
    ws,
    start_row: int,
    end_row: int,
    max_col: int,
    min_height: float = 20.0,
    line_height: float = 16.0,
    max_height: float = 200.0,
) -> None:
    for r in range(start_row, end_row + 1):
        max_lines = 1
        for c in range(1, max_col + 1):
            cell = ws.cell(row=r, column=c)
            if cell.value in (None, ""):
                continue
            col_letter = get_column_letter(c)
            col_width = ws.column_dimensions[col_letter].width
            max_lines = max(max_lines, _estimate_wrapped_lines(cell.value, col_width))
        target = min(max_height, max(min_height, max_lines * line_height + 4))
        current = ws.row_dimensions[r].height
        ws.row_dimensions[r].height = max(current or 0.0, target)


def build_crop_seeding_day_stats(
    tasks_today: list[dict],
    mtfmi_map: dict[int, list[dict]],
    field_to_firm: dict[int, str],
    field_crop_by_id: dict[int, str],
    available_field_ids: set[int] | None,
    cutoff: datetime,
    target_crop: str,
    crop_for_field,
    min_area_ha: float = MIN_DETAIL_AREA_HA,
    *,
    fact_by_all_fields: bool = False,
) -> dict[str, dict]:
    """Суточная выработка и моточасы посева только по полям заданной культуры."""
    all_firms = list(AGROFIRMS.keys())
    per_firm_machine: dict[str, dict[int, dict]] = {f: {} for f in all_firms}

    for t in tasks_today:
        mid = t.get("machine_id")
        items = mtfmi_map.get(t["id"], [])

        total_cov_in_window = 0.0
        for item in items:
            for entry in (item.get("covered_area_hourly") or []):
                h = parse_dt(entry[0])
                if h and h < cutoff:
                    total_cov_in_window += entry[1] or 0

        firm_cov: dict[str, float] = defaultdict(float)
        for item in items:
            fid = item.get("field_id")
            if fid is None:
                continue
            fid_i = int(fid)
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue
            if not _field_ok_for_seeding_fact(
                fid_i,
                fact_by_all_fields=fact_by_all_fields,
                available_field_ids=available_field_ids,
                field_crop_by_id=field_crop_by_id,
            ):
                continue
            from mtfmi_batch_fetch import crop_of_item

            raw_crop = crop_of_item(item, "")
            if not raw_crop or crop_for_field(raw_crop, for_today=True) != target_crop:
                continue

            cov_item = 0.0
            for entry in (item.get("covered_area_hourly") or []):
                h = parse_dt(entry[0])
                if h and h < cutoff:
                    cov_item += entry[1] or 0
            if cov_item <= 0 and not (item.get("covered_area_hourly") or []):
                cov_item = float(item.get("covered_area") or 0)
            if cov_item > 0:
                firm_cov[firm] += cov_item

        for firm, cov in firm_cov.items():
            if cov <= 0:
                continue
            pm = per_firm_machine[firm]
            if mid not in pm:
                pm[mid] = {"cov_total": 0.0, "work_dur": 0.0}
            pm[mid]["cov_total"] += cov

            work_dur = t.get("work_duration") or 0
            road_move = t.get("movements_on_road_duration") or 0
            work_net = max(work_dur - road_move, 0)
            if total_cov_in_window > 0:
                pm[mid]["work_dur"] += work_net * (cov / total_cov_in_window)
            else:
                pm[mid]["work_dur"] += work_net

    result: dict[str, dict] = {}
    for firm in all_firms:
        pm = per_firm_machine[firm]
        pm_filtered = {
            mid: d for mid, d in pm.items()
            if round(d["cov_total"], 2) >= min_area_ha
        }
        result[firm] = {
            "area_today": round(sum(d["cov_total"] for d in pm_filtered.values()), 2),
            "engine_today": round(
                sum(d["work_dur"] for d in pm_filtered.values()) / 3600, 4
            ),
            "machines_today": len(pm_filtered),
        }
    return result


def write_crop_seeding_summary_sheet(
    wb: openpyxl.Workbook,
    crop_name: str,
    title: str,
    sheet_title: str,
    crop_firm_stats: dict[str, dict],
    crop_day_stats: dict[str, dict],
    *,
    index: int,
) -> None:
    """Лист «4.0 Посев — <культура>» по образцу «2.4 Вспашка — Рапс озимый» (9_main)."""
    styles = _make_styles()
    n = len(CROP_SEEDING_SUMMARY_COLS)
    ws = wb.create_sheet(title=sheet_title[:31], index=index)
    row = 1

    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n)
    c0 = ws.cell(row=row, column=1, value=title)
    c0.font = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
    c0.fill = styles["fill_hdr"]
    c0.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    c0.border = styles["border"]
    ws.row_dimensions[row].height = 28
    row += 1

    for ci, col_name in enumerate(CROP_SEEDING_SUMMARY_COLS, start=1):
        cc = ws.cell(row=row, column=ci, value=col_name)
        _style_cell(
            cc, styles,
            fill=styles["fill_hdr"],
            font=Font(bold=True, color="FFFFFF", size=12),
            align=Alignment(horizontal="center", vertical="center", wrap_text=True),
        )
    ws.row_dimensions[row].height = 66
    row += 1

    num_fmt_by_col = {
        2: NF_INT,
        3: NF_DEC2,
        4: NF_DEC2,
        5: NF_DEC1,
        6: NF_DEC2,
        7: "@",
        8: "@",
        9: NF_DEC1,
    }

    t_plan = t_fact_season = t_fact_today = t_hours = 0.0
    t_mach = 0

    for firm in FIRM_ORDER:
        crop_stats = crop_firm_stats.get(firm, {})
        day_stats = crop_day_stats.get(firm, {})

        plan = _safe_float((PLAN_SEEDING_HA.get(firm) or {}).get(crop_name))
        fact_season = _safe_float((crop_stats.get("fact_by_crop") or {}).get(crop_name))
        fact_today = _safe_float((crop_stats.get("today_by_crop") or {}).get(crop_name))
        hours_today = _safe_float(day_stats.get("engine_today"))
        n_mach = int(_safe_float(day_stats.get("machines_today")))

        ostatok_ga = round(plan - fact_season, 2)
        fact_pct = round(fact_season / plan * 100, 1) if plan else 0.0
        eff_h = round(hours_today / n_mach, 4) if n_mach else 0.0
        rate_h = round(fact_today / hours_today, 1) if hours_today else 0.0

        vals = [
            firm,
            plan,
            round(fact_season, 2),
            ostatok_ga,
            fact_pct,
            round(fact_today, 2),
            _safe_hhmm(hours_today),
            _safe_hhmm(eff_h),
            rate_h,
        ]
        for ci, v in enumerate(vals, start=1):
            cell = ws.cell(row=row, column=ci, value=v)
            cell.number_format = num_fmt_by_col.get(ci, "General")
            _style_cell(
                cell, styles,
                fill=PatternFill("solid", fgColor="FFFFFF"),
                font=Font(size=12),
                align=Alignment(
                    horizontal="left" if ci == 1 else "center",
                    vertical="center",
                    wrap_text=True,
                ),
            )

        t_plan += plan
        t_fact_season += fact_season
        t_fact_today += fact_today
        t_hours += hours_today
        t_mach += n_mach
        row += 1

    tot_ostatok_ga = round(t_plan - t_fact_season, 2)
    tot_fact_pct = round(t_fact_season / t_plan * 100, 1) if t_plan else 0.0
    tot_eff_h = round(t_hours / t_mach, 4) if t_mach else 0.0
    tot_rate_h = round(t_fact_today / t_hours, 1) if t_hours else 0.0

    totals = [
        "Итого",
        round(t_plan, 2),
        round(t_fact_season, 2),
        tot_ostatok_ga,
        tot_fact_pct,
        round(t_fact_today, 2),
        _safe_hhmm(t_hours),
        _safe_hhmm(tot_eff_h),
        tot_rate_h,
    ]
    for ci, v in enumerate(totals, start=1):
        cell = ws.cell(row=row, column=ci, value=v)
        cell.number_format = num_fmt_by_col.get(ci, "General")
        _style_cell(
            cell, styles,
            fill=styles["fill_total"],
            font=styles["font_total"],
            align=Alignment(
                horizontal="left" if ci == 1 else "center",
                vertical="center",
                wrap_text=False,
            ),
        )
    row += 1

    ws.column_dimensions["A"].width = 30
    for ci in range(2, n + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 20

    _auto_fit_row_heights(ws, start_row=1, end_row=row - 1, max_col=n)
    hr = ws.row_dimensions[2].height or 0
    ws.row_dimensions[2].height = max(hr, 66.0)


def write_crop_seeding_summary_sheets(
    wb: openpyxl.Workbook,
    crop_firm_stats: dict[str, dict],
    crop_day_stats_by_crop: dict[str, dict[str, dict]],
) -> None:
    """Вставляет листы озимого рапса и пшеницы сразу после «Сводка»."""
    if "Сводка" not in wb.sheetnames:
        return
    base_idx = wb.sheetnames.index("Сводка") + 1
    for offset, (crop_name, title, sheet_title) in enumerate(_CROP_SEEDING_SHEETS):
        write_crop_seeding_summary_sheet(
            wb,
            crop_name,
            title,
            sheet_title,
            crop_firm_stats,
            crop_day_stats_by_crop.get(crop_name, {}),
            index=base_idx + offset,
        )


def reorder_report5_leading_sheets(wb: openpyxl.Workbook) -> list[str]:
    """PDF стр. 1–3: «Сводка», посев рапса озимого, посев пшеницы озимой.

    Доп. таблицы 1_main_dop вставляются в начало книги и сдвигают листы культур;
    перед сохранением возвращаем нужный порядок.
    """
    leading = ["Сводка"] + [sheet_title for _, _, sheet_title in _CROP_SEEDING_SHEETS]
    present = [name for name in leading if name in wb.sheetnames]
    if not present:
        return list(wb.sheetnames)
    rest = [name for name in wb.sheetnames if name not in present]
    new_order = present + rest
    if new_order == list(wb.sheetnames):
        return new_order
    by_title = {ws.title: ws for ws in wb._sheets}
    wb._sheets = [by_title[name] for name in new_order]
    return new_order


DETAIL_COL_WIDTHS_5 = [10, 52, 15, 16, 17, 22, 38]
DETAIL_PAGINATION_CFG_5 = DetailPaginationConfig(
    page_body_height_pt=505.0,
    page_safe_reserve_pt=36.0,
    section_title_height_pt=26.0,
    header_height_pt=78.0,
    data_row_height_pt=24.0,
    data_row_max_height_pt=36.0,
    total_row_height_pt=30.0,
    section_gap_height_pt=12.0,
    note_height_pt=34.0,
)

SUMMARY_COL_KEYS = [
    "№ п/п",
    "Агрофирма",
    "Плановая выработка, га/сутки",
    "Количество задействованной техники",
    "Общий объем выполненной работы за сутки, га",
    "Выработка за сутки/1 ед агрегата, га",
    "Средняя выработка по сезону, га/сутки",
    "Общее время работы на полях, машино-часы за сутки",
    "Среднее время эффективной работы на поле на 1 агрегат., час.",
    "Часовая выработка в расчёте на 1 агрегат, га",
    "Всего собств. агрегатов в наличии",
]

SUMMARY_COLS = [
    "№ п/п",
    "Агрофирма",
    "Плановая выработка,\nга/сутки",
    "Количество\nзадействованной\nтехники",
    "Общий объем выполненной\nработы за сутки, га",
    "Выработка за сутки/1 \nед агрегата, га",
    "Средняя выработка\nпо сезону, га/сутки",
    "Общее время работы\nна полях, машино-\nчасы за сутки",
    "Среднее время эффективной\nработы на поле на\n1 агрегат, час.",
    "Часовая выработка\nв расчёте на\n1 агрегат, га",
    "Всего собств. агрегатов\nв наличии",
]


def _style_cell(cell, s, fill=None, font=None, align=None) -> None:
    cell.border = s["border"]
    if fill:  cell.fill      = fill
    if font:
        cell.font = font
    else:
        cell.font = s["font_data"]
    if align: cell.alignment = align
    # Единый формат чисел: разделение тысяч пробелом.
    if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
        if not cell.number_format or cell.number_format in {"General", "0"}:
            v = float(cell.value)
            if abs(v - round(v)) < 1e-9:
                cell.number_format = NF_INT
            elif abs(v - round(v, 1)) < 1e-9:
                cell.number_format = NF_DEC1
            else:
                cell.number_format = NF_DEC2


def _fmt_grouped_2(raw) -> str:
    try:
        return f"{float(raw):,.2f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(raw)


def _summary_display_value(col_key: str, raw):
    if col_key in {
        "Общее время работы на полях, машино-часы за сутки",
        "Среднее время эффективной работы на поле на 1 агрегат., час.",
    }:
        try:
            return seconds_to_hms(float(raw) * 3600.0), "@"
        except (TypeError, ValueError):
            return raw, None
    return summary_cell_value_and_format(col_key, raw)


def _write_summary_block(
    ws,
    start_row: int,
    title:     str,
    rows:      list[dict],
    s:         dict,
) -> int:
    n = len(SUMMARY_COLS)

    ws.merge_cells(start_row=start_row, start_column=1,
                   end_row=start_row, end_column=n)
    c = ws.cell(row=start_row, column=1, value=title)
    c.font      = s["font_title"]
    c.alignment = s["align_c"]
    start_row  += 1

    for ci, col in enumerate(SUMMARY_COLS, start=1):
        c = ws.cell(row=start_row, column=ci, value=col)
        _style_cell(c, s, fill=s["fill_hdr"],
                    font=s["font_hdr"], align=s["align_hdr"])
    ws.row_dimensions[start_row].height = 78
    start_row += 1

    for row_data in rows:
        is_total = row_data.get("Агрофирма") == "Итого"
        fill     = s["fill_total"] if is_total else s["fill_own"]
        font     = s["font_total"] if is_total else None
        for ci, col in enumerate(SUMMARY_COLS, start=1):
            key = SUMMARY_COL_KEYS[ci - 1]
            raw = row_data.get(key, "")
            val, nf = _summary_display_value(key, raw)
            c = ws.cell(row=start_row, column=ci, value=val)
            if nf:
                c.number_format = nf
            _style_cell(
                c, s, fill=fill, font=font,
                align=_align_summary_data(ci, key, val, s),
            )
        lbl = str(row_data.get("Агрофирма") or "")
        ws.row_dimensions[start_row].height = 32 if len(lbl) > 34 else 26
        start_row += 1

    return start_row + 1


def _write_detail_block(
    ws,
    start_row:    int,
    title:        str,
    detail_own:   list[dict],
    detail_hired: list[dict],
    s:            dict,
    tech_min_area_ha: float,
) -> int:
    n = len(DETAIL_COLS)
    cfg = DETAIL_PAGINATION_CFG_5

    ws.merge_cells(start_row=start_row, start_column=1,
                   end_row=start_row, end_column=n)
    c = ws.cell(row=start_row, column=1, value=title)
    c.font      = s["font_title"]
    c.alignment = s["align_c"]
    start_row  += 2

    note_text = f"* В таблицу включена техника с площадью ≥ {tech_min_area_ha} га"
    ws.merge_cells(start_row=start_row, start_column=1,
                   end_row=start_row, end_column=n)
    c = ws.cell(row=start_row, column=1, value=note_text)
    c.font      = Font(size=10, italic=True)
    c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    c.border    = s["border"]
    ws.row_dimensions[start_row].height = cfg.note_height_pt
    start_row  += 1

    for ci, w in enumerate(DETAIL_COL_WIDTHS_5, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    ctx = DetailPageContext(ws, n, DETAIL_COL_WIDTHS_5, cfg, start_row=start_row)

    def _detail_vals(idx: int, rd: dict) -> list[object]:
        return [
            idx, rd["Машина"], rd["Площадь, га"],
            rd["Время на полях"], rd["Ср. выр-ка га/час"],
            rd["Средняя скорость на полях км/ч"],
            rd["Коэффициент использования конструкционной ширины захвата, %"],
        ]

    firm_single_page = compute_firm_single_page(
        ctx, detail_own, detail_hired, _detail_vals, include_note=False,
    )
    same_firm_both_sections = bool(detail_own and detail_hired)

    def _make_section_writer(sec_fill, row_fill, rows_data: list[dict]):
        def _caption(sec_title: str, relaxed: bool) -> None:
            ctx.ensure_space(
                cfg.section_title_height_pt + cfg.header_height_pt,
                relaxed=relaxed,
            )
            ws.merge_cells(start_row=ctx.row, start_column=1,
                           end_row=ctx.row, end_column=n)
            c = ws.cell(row=ctx.row, column=1, value=sec_title)
            c.font      = s["font_sec"]
            c.fill      = sec_fill
            c.alignment = s["align_c"]
            ws.row_dimensions[ctx.row].height = cfg.section_title_height_pt
            ctx.row += 1
            hdr_row = ctx.row
            for ci, col in enumerate(DETAIL_COLS, start=1):
                c = ws.cell(row=ctx.row, column=ci, value=col)
                _style_cell(c, s, fill=s["fill_hdr"],
                            font=s["font_hdr"], align=s["align_hdr"])
            ws.row_dimensions[hdr_row].height = cfg.header_height_pt
            ctx.row += 1
            ctx.page_used_pt += cfg.section_title_height_pt + cfg.header_height_pt

        def _data_row(r: int, vals: list[object]) -> float:
            for ci, val in enumerate(vals, start=1):
                if ci == 3:
                    val = _fmt_grouped_2(val)
                c = ws.cell(row=r, column=ci, value=val)
                _style_cell(c, s, fill=row_fill,
                            align=s["align_l"] if ci == 2 else s["align_c"])
            h = ctx.row_height(vals)
            ws.row_dimensions[r].height = h
            return h

        def _total_row() -> float:
            tc  = round(sum(rd["Площадь, га"] for rd in rows_data), 2)
            ts  = sum(rd["Время (сек)"] for rd in rows_data)
            tr  = round(tc / (ts / 3600), 1) if ts else 0.0
            asp = round(
                sum(rd["Средняя скорость на полях км/ч"] for rd in rows_data)
                / len(rows_data), 1,
            )
            acf = round(
                sum(rd["Коэффициент использования конструкционной ширины захвата, %"]
                    for rd in rows_data) / len(rows_data),
            )
            r = ctx.row
            for ci, val in enumerate(
                ["Итого", "", tc, seconds_to_hms(ts), tr, asp, acf], start=1
            ):
                if ci == 3:
                    val = _fmt_grouped_2(val)
                c = ws.cell(row=r, column=ci, value=val)
                align = s["align_l"] if ci <= 2 else s["align_c"]
                _style_cell(c, s, fill=s["fill_total"],
                            font=s["font_total"], align=align)
            ws.row_dimensions[r].height = cfg.total_row_height_pt
            return cfg.total_row_height_pt

        return _caption, _data_row, _total_row

    if detail_own:
        cap, data, tot = _make_section_writer(s["fill_sec_own"], s["fill_own"], detail_own)
        write_paginated_section(
            ctx,
            sec_title="СОБСТВЕННЫЕ",
            rows=detail_own,
            detail_vals=_detail_vals,
            write_caption_and_header=cap,
            write_data_row=data,
            write_total_row=tot,
            firm_single_page=firm_single_page,
            same_firm_both_sections=same_firm_both_sections,
        )

    if detail_hired:
        if detail_own:
            ctx.add_section_gap(firm_single_page=firm_single_page)
        cap, data, tot = _make_section_writer(s["fill_sec_hire"], s["fill_hired"], detail_hired)
        write_paginated_section(
            ctx,
            sec_title="НАЁМНЫЕ",
            rows=detail_hired,
            detail_vals=_detail_vals,
            write_caption_and_header=cap,
            write_data_row=data,
            write_total_row=tot,
            continue_on_page=True,
            firm_single_page=firm_single_page,
            same_firm_both_sections=same_firm_both_sections,
        )

    return ctx.row + 1


def _set_col_widths(ws, n_cols: int) -> None:
    """Ширины листов детализации: №/Итого, машина, коэфф. захвата — с запасом под PDF."""
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 52
    _detail_num_widths = {3: 15, 4: 16, 5: 17, 6: 22, 7: 38}
    for ci in range(3, n_cols + 1):
        ws.column_dimensions[get_column_letter(ci)].width = _detail_num_widths.get(ci, 18)


# =============================================================================
# ЗАПИСЬ ЛИСТОВ
# =============================================================================

def write_seeding_sheet(
    wb:          openpyxl.Workbook,
    firm_name:   str,
    report_date: date,
    day_start:   datetime,
    cutoff:      datetime,
    stats:       dict,
) -> None:
    if not stats.get("detail_own") and not stats.get("detail_hired"):
        return

    from firm_sheet_names import firm_sheet_tab_name

    ws = wb.create_sheet(title=firm_sheet_tab_name(firm_name))
    s  = _make_styles()
    # Заголовок фирмы/вида работы уже накладывается на PDF на сервере; в таблице не дублируем.
    row = 1

    n = len(DETAIL_COLS)

    row = _write_detail_block(
        ws,
        row,
        "КУЛЬТУРЫ",
        stats["detail_own"],
        stats["detail_hired"],
        s,
        tech_min_area_ha=MIN_COUNT_AREA_HA,
    )
    _set_col_widths(ws, n)


def write_veg_sheet(
    wb:          openpyxl.Workbook,
    firm_name:   str,
    report_date: date,
    day_start:   datetime,
    cutoff:      datetime,
    stats:       dict,
) -> None:
    if not stats.get("detail_own") and not stats.get("detail_hired"):
        return

    sheet_title = f"Овощи {_firm_table_label(firm_name)[:20]}"
    ws = wb.create_sheet(title=sheet_title)
    s  = _make_styles()
    row = 1

    n = len(DETAIL_COLS)

    row = _write_detail_block(
        ws,
        row,
        "Посев / посадка овощных культур",
        stats["detail_own"],
        stats["detail_hired"],
        s,
        tech_min_area_ha=MIN_DETAIL_AREA_HA,
    )
    _set_col_widths(ws, n)


def _build_summary_rows(
    firm_names:  list[str],
    agrofirms:   dict[str, tuple[int, float, int]],
    all_details: dict[str, dict],
    *,
    include_inactive_firms: bool = False,
) -> list[dict]:
    rows = []
    for i, firm_name in enumerate(firm_names, start=1):
        st         = all_details[firm_name]
        n_mach     = st["machines_today"]
        area       = st["area_today"]
        eng        = st["engine_today"]
        avg_s      = st["avg_season"]
        _, plan_rate, own_count = agrofirms[firm_name]
        # В блоке «КУЛЬТУРЫ» для донора сводки 1 нужны все АФ (иначе XLSX-fill не сопоставляет строки).
        if not include_inactive_firms:
            if float(area or 0.0) <= 0 and int(n_mach or 0) <= 0:
                continue
        rows.append({
            "№ п/п":                                                  i,
            "Агрофирма":                                              _firm_table_label(firm_name),
            "Плановая выработка, га/сутки":                           plan_rate,
            "Количество задействованной техники":                      n_mach,
            "Общий объем выполненной работы за сутки, га":            area,
            "Выработка за сутки/1 ед агрегата, га":
                round(area / n_mach, 2) if n_mach else 0.0,
            "Средняя выработка по сезону, га/сутки":                  avg_s,
            "Общее время работы на полях, машино-часы за сутки":      eng,
            "Среднее время эффективной работы на поле на 1 агрегат., час.":
                round(eng / n_mach, 1) if n_mach else 0.0,
            "Часовая выработка в расчёте на 1 агрегат, га":
                round(area / eng, 1)   if eng    else 0.0,
            "Всего собств. агрегатов в наличии":                      own_count,
        })

    total_mach = sum(r["Количество задействованной техники"]           for r in rows)
    total_area = sum(r["Общий объем выполненной работы за сутки, га"]  for r in rows)
    total_eng  = sum(r["Общее время работы на полях, машино-часы за сутки"] for r in rows)
    total_own  = sum(r["Всего собств. агрегатов в наличии"]            for r in rows)
    plan_rate  = rows[0]["Плановая выработка, га/сутки"] if rows else 0

    # ← ИСПРАВЛЕНИЕ: считаем через сырые данные, а не среднее средних
    total_season_area  = sum(all_details[n].get("season_area_total", 0.0) for n in firm_names)
    total_machine_days = sum(all_details[n].get("machine_days_total", 0)  for n in firm_names)
    avg_season_total   = round(total_season_area / total_machine_days, 1) if total_machine_days else 0.0

    rows.append({
        "№ п/п":                                                  "",
        "Агрофирма":                                              "Итого",
        "Плановая выработка, га/сутки":                           plan_rate,
        "Количество задействованной техники":                      total_mach,
        "Общий объем выполненной работы за сутки, га":            round(total_area, 2),
        "Выработка за сутки/1 ед агрегата, га":
            round(total_area / total_mach, 2) if total_mach else 0.0,
        "Средняя выработка по сезону, га/сутки":                  avg_season_total,  # ← было: среднее средних
        "Общее время работы на полях, машино-часы за сутки":      round(total_eng, 1),
        "Среднее время эффективной работы на поле на 1 агрегат., час.":
            round(total_eng / total_mach, 1) if total_mach else 0.0,
        "Часовая выработка в расчёте на 1 агрегат, га":
            round(total_area / total_eng, 1) if total_eng  else 0.0,
        "Всего собств. агрегатов в наличии":                      total_own,
    })
    return rows


def _write_veg_crop_compact_block(
    ws,
    start_row: int,
    firm_name: str,
    firm_stats: dict,
    report_date: date,
    season_start: datetime,
    s: dict,
) -> int:
    headers = [
        "№ п/п",
        "Культура",
        "План, га",
        "Факт, га",
        "Остаток га",
        "%",
        "Факт, сутки га",
        "Прогнозное время завершения операции",
    ]
    n_h = len(headers)

    # Верхняя строка как в шаблоне: отдельная ячейка "Овощи" + название агрофирмы.
    ws.merge_cells(start_row=start_row, start_column=2, end_row=start_row, end_column=n_h)
    c = ws.cell(row=start_row, column=1, value="Овощи")
    c.font = s["font_title"]
    c.alignment = s["align_c"]
    c2 = ws.cell(row=start_row, column=2, value=_firm_table_label(firm_name))
    c2.font = s["font_title"]
    c2.alignment = s["align_l"]
    _style_cell(c, s, fill=s["fill_own"], align=s["align_c"])
    _style_cell(c2, s, fill=s["fill_own"], align=s["align_l"])
    for ci in range(3, n_h + 1):
        _style_cell(ws.cell(row=start_row, column=ci), s, fill=s["fill_own"], align=s["align_l"])
    ws.row_dimensions[start_row].height = 36
    start_row += 1

    for ci, h in enumerate(headers, start=1):
        c = ws.cell(row=start_row, column=ci, value=h)
        _style_cell(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
    # Выше: «Овощи» + фирма; здесь — длинные заголовки (прогноз, %).
    ws.row_dimensions[start_row].height = 56
    start_row += 1

    days_elapsed = max((report_date - season_start.date()).days, 1)
    fact_map = dict(firm_stats.get("veg_fact_by_crop") or {})
    today_map = dict(firm_stats.get("veg_today_by_crop") or {})

    for i, crop in enumerate(VEG_CROP_ROWS, start=1):
        plan = float(VEG_CROP_PLAN_HA.get(crop, 0.0) or 0.0)
        fact = float(fact_map.get(crop, 0.0) or 0.0)
        today = float(today_map.get(crop, 0.0) or 0.0)
        rem = max(plan - fact, 0.0)
        pct = (fact / plan) if plan > 0 else 0.0
        avg_daily = fact / days_elapsed if fact > 0 else 0.0
        rate = today if today > 0 else avg_daily
        forecast = ""
        if rem <= 0 and plan > 0:
            forecast = report_date.strftime("%d.%m.%Y")
        elif rate > 0:
            forecast = (report_date + timedelta(days=max(1, math.ceil(rem / rate)))).strftime("%d.%m.%Y")

        vals = [i, crop, round(plan, 2), round(fact, 2), round(rem, 2), pct, round(today, 2), forecast]
        for ci, v in enumerate(vals, start=1):
            c = ws.cell(row=start_row, column=ci, value=v)
            _style_cell(c, s, fill=s["fill_own"], align=s["align_c"] if ci == 1 else (s["align_l"] if ci == 2 else s["align_c"]))
            if ci in (3, 4, 5, 7):
                c.number_format = NF_DEC2
            elif ci == 6:
                c.number_format = "0.0%"
        start_row += 1

    total_plan = sum(float(VEG_CROP_PLAN_HA.get(c, 0.0) or 0.0) for c in VEG_CROP_ROWS)
    total_fact = sum(float(fact_map.get(c, 0.0) or 0.0) for c in VEG_CROP_ROWS)
    total_today = sum(float(today_map.get(c, 0.0) or 0.0) for c in VEG_CROP_ROWS)
    total_rem = max(total_plan - total_fact, 0.0)
    total_pct = (total_fact / total_plan) if total_plan > 0 else 0.0
    total_vals = ["", "Итого", round(total_plan, 2), round(total_fact, 2), round(total_rem, 2), total_pct, round(total_today, 2), ""]

    for ci, v in enumerate(total_vals, start=1):
        c = ws.cell(row=start_row, column=ci, value=v)
        _style_cell(c, s, fill=s["fill_total"], font=s["font_total"], align=s["align_l"] if ci == 2 else s["align_c"])
        if ci in (3, 4, 5, 7):
            c.number_format = NF_DEC2
        elif ci == 6:
            c.number_format = "0.0%"

    return start_row + 2


def write_main_summary_sheet(
    wb:              openpyxl.Workbook,
    report_date:     date,
    day_start:       datetime,
    cutoff:          datetime,
    season_start:    datetime,
    seeding_firm_names: list[str],
    veg_firm_names:  list[str],
    seeding_details: dict[str, dict],
    veg_details:     dict[str, dict],
) -> None:
    ws = wb.create_sheet(title="Сводка", index=0)
    s  = _make_styles()
    n  = len(SUMMARY_COLS)

    # Верхний текстовый блок в xlsx не пишем: только таблицы.
    row = 1

    seeding_rows = _build_summary_rows(
        seeding_firm_names, AGROFIRMS, seeding_details, include_inactive_firms=True,
    )
    row = _write_summary_block(ws, row, "КУЛЬТУРЫ", seeding_rows, s)

    # Порядок на стр.1: КУЛЬТУРЫ → посев/посадка овощей → компактный блок культур.
    veg_rows = _build_summary_rows(veg_firm_names, VEG_AGROFIRMS, veg_details)
    row = _write_summary_block(
        ws, row, "Посев / посадка овощных культур", veg_rows, s
    )

    if veg_firm_names:
        firm_for_compact = veg_firm_names[0]
        row = _write_veg_crop_compact_block(
            ws,
            row,
            firm_for_compact,
            veg_details.get(firm_for_compact, {}),
            report_date,
            season_start,
            s,
        )

    # Сводка: колонка A — «Овощи» / № / культуры (раньше 5 — слишком узко); 7 — прогноз ×2.
    _summary_widths = {
        1: 14,
        2: 36,
        3: 15,
        4: 20,
        5: 18,
        6: 16,
        7: 40,
        8: 26,
        9: 28,
        10: 22,
        11: 20,
    }
    for ci in range(1, n + 1):
        ws.column_dimensions[get_column_letter(ci)].width = _summary_widths.get(ci, 20)



# =============================================================================
# ПОСТРОЕНИЕ ОТЧЁТА
# =============================================================================

def _load_dop_module():
    dop_path = Path(__file__).resolve().parent / "1_main_dop.py"
    if not dop_path.is_file():
        raise FileNotFoundError("1_main_dop.py")
    spec = importlib.util.spec_from_file_location("report5_dop_module", str(dop_path))
    if spec is None or spec.loader is None:
        raise ImportError("1_main_dop.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _append_dop_report5_tables(
    wb: openpyxl.Workbook,
    report_date: date,
    seeding_day_stats: dict[str, dict] | None = None,
    crop_firm_stats: dict[str, dict] | None = None,
) -> None:
    """Первые листы — таблицы посева из 1_main_dop (рапс, пшеница, иные культуры)."""
    try:
        mod = _load_dop_module()
        append_fn = getattr(mod, "append_tables_to_workbook_async", None)
        if not callable(append_fn):
            print("Доп. таблицы: append_tables_to_workbook_async не найден.")
            return
        await append_fn(
            wb, report_date, seeding_day_stats, firm_stats=crop_firm_stats,
        )
        print("Доп. таблицы (1_main_dop) добавлены первыми листами.")
    except FileNotFoundError:
        print("Доп. таблицы: 1_main_dop.py не найден, пропуск.")
    except Exception as e:
        print(f"Доп. таблицы: ошибка подключения 1_main_dop.py: {e}")
        traceback.print_exc()


async def _fetch_crop_seeding_stats(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    day_start: datetime,
    cutoff: datetime,
    season_start: datetime,
    report_date: date,
    field_crop_by_id: dict[int, str],
    field_to_firm: dict[int, str],
    available_field_ids: set[int],
    tasks_today_seed: list[dict],
    mtfmi_seed: dict[int, list[dict]],
) -> tuple[dict[str, dict], dict[str, dict[str, dict]]]:
    """Факт по культурам (1_main_dop) + суточные моточасы по озимому рапсу/пшенице."""
    try:
        dop_mod = _load_dop_module()
    except (FileNotFoundError, ImportError) as exc:
        print(f"  Статистика по культурам: {exc}")
        return {}, {}

    print("  Загружаем факт посева по культурам (1_main_dop)...")
    crop_firm_stats = await dop_mod.fetch_all_crop_stats(
        client,
        semaphore,
        day_start,
        cutoff,
        season_start,
        field_crop_by_id,
        field_to_firm,
        available_field_ids=available_field_ids,
        report_date=report_date,
        field_to_firm_all=field_to_firm,
    )
    crop_for_field = dop_mod._crop_for_seeding_fact
    crop_day_stats_by_crop = {
        crop_name: build_crop_seeding_day_stats(
            tasks_today_seed,
            mtfmi_seed,
            field_to_firm,
            field_crop_by_id,
            available_field_ids,
            cutoff,
            crop_name,
            crop_for_field,
            fact_by_all_fields=True,
        )
        for crop_name in (CROP_RAPESEED_WINTER, CROP_WHEAT_WINTER)
    }
    return crop_firm_stats, crop_day_stats_by_crop


async def build_report_async(report_date: date = REPORT_DATE) -> None:
    global machines_map

    day_start, cutoff = resolve_time_range(report_date, DAY_START_HOUR, DAY_END_HOUR)
    season_start = datetime(SEASON_YEAR, 1, 1, tzinfo=TZ_MSK)

    print(f"Отчёт 'Посев'  |  Дата: {report_date}")
    print(f"  Суточный диапазон: {day_start} — {cutoff}\n")

    field_to_firm: dict[int, str] = {}
    firm_names     = list(AGROFIRMS.keys())
    veg_firm_names = [name for name in VEG_REPORT_FIRMS if name in VEG_AGROFIRMS]
    period_start = date(SEASON_YEAR, 1, 1)

    def _sync_refs_r5() -> tuple[dict[int, str], dict[int, str], set[int]]:
        crops = get_all_from_id("crops")
        crop_names_by_id: dict[int, str] = {
            int(c["id"]): str(c.get("name") or c.get("title") or "").strip()
            for c in crops if c.get("id") is not None
        }
        print(f"  Строим field_crop_by_id из history_items (годы {SEASON_YEAR}, {SEASON_YEAR + 1})...")
        all_history_items: list[dict] = []
        for crop_year in (SEASON_YEAR, SEASON_YEAR + 1):
            chunk = get_all_from_id("history_items", {"year": str(crop_year)})
            all_history_items.extend(chunk)
            print(f"    history_items year={crop_year}: {len(chunk)} записей")
        field_crop_by_id = build_field_to_crop(
            crop_map=crop_names_by_id,
            history_items=all_history_items,
            report_date=report_date,
            period_start=period_start,
            skip_crops=SKIP_CROPS,
            season_year=SEASON_YEAR + 1,
        )
        print(f"  Полей с культурой посева (без SKIP_CROPS): {len(field_crop_by_id)}")
        available_field_ids: set[int] = set()
        for r in get_all_from_id("inventory_history_items", {"historyable_type": "Field"}):
            if not r.get("available"):
                continue
            fid_r = r.get("historyable_id")
            if fid_r is None:
                continue
            try:
                ev_start = date.fromisoformat(str(r.get("event_start_at"))[:10]) if r.get("event_start_at") else None
            except ValueError:
                ev_start = None
            try:
                ev_end = date.fromisoformat(str(r.get("event_end_at"))[:10]) if r.get("event_end_at") else None
            except ValueError:
                ev_end = None
            if ev_start is None:
                continue
            if ev_start <= report_date and (ev_end is None or ev_end >= period_start):
                available_field_ids.add(int(fid_r))
        print(f"  Полей available в периоде {period_start}–{report_date}: {len(available_field_ids)}")
        return crop_names_by_id, field_crop_by_id, available_field_ids

    async def _prefetch_tasks_mtfmi_r5():
        async with report_async_client(API_TOKEN, semaphore_limit=SEMAPHORE_LIMIT, timeout=120) as (client, semaphore):
            return await asyncio.gather(
                fetch_all_tasks_and_mtfmi(
                    client, semaphore, day_start, cutoff, season_start,
                    ALL_WT_IDS, label="Посев",
                ),
                fetch_all_tasks_and_mtfmi(
                    client, semaphore, day_start, cutoff, season_start,
                    VEG_WT_IDS, label="Овощи",
                ),
            )

    (crop_names_by_id, field_crop_by_id, available_field_ids), task_parts = (
        await run_parallel_refs_and_fetch(
            _sync_refs_r5, _prefetch_tasks_mtfmi_r5, label="r5-tasks+mtfmi",
        )
    )
    (tasks_today_seed, tasks_season_seed, mtfmi_seed), (
        tasks_today_veg, tasks_season_veg, mtfmi_veg,
    ) = task_parts

    async with report_async_client(API_TOKEN, semaphore_limit=SEMAPHORE_LIMIT, timeout=120) as (client, semaphore):
        seed_seasons = _editor_season_strings(_SEED_KEY)
        veg_seasons = _editor_season_strings(_VEG_KEY)
        _log_task_season_counts(tasks_today_seed + tasks_season_seed, "Посев", seed_seasons)
        tasks_today_seed = _filter_tasks_by_cropwise_season(tasks_today_seed, seed_seasons)
        tasks_season_seed = _filter_tasks_by_cropwise_season(tasks_season_seed, seed_seasons)
        _log_task_season_counts(tasks_today_veg + tasks_season_veg, "Овощи", veg_seasons)
        tasks_today_veg = _filter_tasks_by_cropwise_season(tasks_today_veg, veg_seasons)
        tasks_season_veg = _filter_tasks_by_cropwise_season(tasks_season_veg, veg_seasons)

        from report_resources_fetch import load_report_resources

        all_tasks = (
            tasks_today_seed + tasks_season_seed
            + tasks_today_veg + tasks_season_veg
        )
        mtfmi_merged = dict(mtfmi_seed)
        mtfmi_merged.update(mtfmi_veg)
        field_to_firm, machines_map, _implements = await load_report_resources(
            client, BASE_URL, semaphore, all_tasks, mtfmi_merged,
            get_all_from_id, _build_field_to_firm,
        )

        crop_firm_stats, crop_day_stats_by_crop = await _fetch_crop_seeding_stats(
            client,
            semaphore,
            day_start,
            cutoff,
            season_start,
            report_date,
            field_crop_by_id,
            field_to_firm,
            available_field_ids,
            tasks_today_seed,
            mtfmi_seed,
        )

    print("  Распределяем задания по агрофирмам через поля...")
    seeding_details = build_all_firm_stats(
        tasks_today_seed, tasks_season_seed, mtfmi_seed,
        field_to_firm, day_start, cutoff, report_date,
        ALL_WT_IDS, MIN_DETAIL_AREA_HA, crop_names_by_id,
        min_count_area_ha=MIN_DETAIL_AREA_HA,
        field_crop_by_id=field_crop_by_id,
        available_field_ids=available_field_ids,
        fact_by_all_fields=True,
    )
    veg_details_all = build_all_firm_stats(
        tasks_today_veg, tasks_season_veg, mtfmi_veg,
        field_to_firm, day_start, cutoff, report_date,
        VEG_WT_IDS, MIN_DETAIL_AREA_HA, crop_names_by_id,
        field_crop_by_id=field_crop_by_id,
        available_field_ids=available_field_ids,
    )
    veg_details: dict[str, dict] = {name: veg_details_all.get(name, {}) for name in veg_firm_names}

    seed_rows = _build_summary_rows(firm_names, AGROFIRMS, seeding_details)
    df = pd.DataFrame(seed_rows)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 300)
    pd.set_option("display.float_format", "{:.2f}".format)
    print("\nПОСЕВ:")
    print(df.to_string(index=False))

    veg_r = _build_summary_rows(veg_firm_names, VEG_AGROFIRMS, veg_details)
    df2   = pd.DataFrame(veg_r)
    print("\nОВОЩИ:")
    print(df2.to_string(index=False))

    output_file = f"5. report_seeding_{report_date}.xlsx"
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    write_main_summary_sheet(
        wb, report_date, day_start, cutoff, season_start,
        firm_names, veg_firm_names, seeding_details, veg_details,
    )

    for firm_name in firm_names:
        write_seeding_sheet(wb, firm_name, report_date, day_start, cutoff,
                             seeding_details[firm_name])

    for firm_name in veg_firm_names:
        write_veg_sheet(wb, firm_name, report_date, day_start, cutoff,
                         veg_details[firm_name])

    await _append_dop_report5_tables(
        wb, report_date, seeding_details, crop_firm_stats=crop_firm_stats,
    )

    if crop_firm_stats:
        write_crop_seeding_summary_sheets(wb, crop_firm_stats, crop_day_stats_by_crop)
        print("  Листы «Посев Рапс озимый» и «Посев Пшеница озимая» добавлены после «Сводка».")

    front = reorder_report5_leading_sheets(wb)
    print(
        "  Порядок первых листов PDF: "
        + ", ".join(front[:3] if len(front) >= 3 else front)
        + ("…" if len(front) > 3 else "")
    )

    seed_total_ha = round(
        sum(float((seeding_details.get(f) or {}).get("area_today") or 0) for f in firm_names),
        2,
    )
    veg_total_ha = round(
        sum(float((veg_details.get(f) or {}).get("area_today") or 0) for f in veg_firm_names),
        2,
    )
    wb.save(output_file)
    try:
        from svodki_sanity import emit_calc_total_ha, publish_cross_op_snapshot

        cross_ops: dict[str, float] = {}
        if seed_total_ha >= 0.01:
            cross_ops["4.0 Посев"] = seed_total_ha
        if veg_total_ha >= 0.01:
            cross_ops["4.1 Посев (овощи)"] = veg_total_ha
        if cross_ops:
            publish_cross_op_snapshot(cross_ops)
        if seed_total_ha + veg_total_ha >= 0.01:
            emit_calc_total_ha(round(seed_total_ha + veg_total_ha, 2))
    except Exception:
        pass
    print(f"\nСохранено: {output_file}")


def build_report(report_date: date = REPORT_DATE) -> None:
    run_report_async(build_report_async(report_date))


if __name__ == "__main__":
    build_report()
