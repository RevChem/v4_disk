# 11.06.2026
"""
26 Cropwise Operations — отчёт "Культивация"
==========================================
wt=10,50,63,96 (культивация, сводная таблица и листы по фирмам) + wt=73 (фрезерование — только отдельные листы)

Логика:
- Агрофирма определяется по ПОЛЮ из mtfmi (field → field_group → group_folder → фирма),
  а НЕ по привязке машины к региону. Машина АксуАгро на поле АгроРаздолья
  записывается как АгроРаздолье.
- Площадь: covered_area_hourly из machine_task_field_mapping_items.
  Метка H означает работу в [H, H+1), поэтому фильтр: h < cutoff.
- DAY_START = report_date - 1 день + DAY_START_HOUR (по умолчанию 06:00)
- CUTOFF    = report_date + DAY_END_HOUR             (по умолчанию 06:00)
- Время:   work_duration - movements_on_road_duration, масштабируется по доле площади
- Коэффициент ширины: covered_area_by_track / work_distance_km / implements.width * 10 * 100
  track и distance масштабируются пропорционально доле часов в диапазоне
- Скорость: average_speed_on_field (м/с) × 3.6 → км/ч, округление до целых
- Сред. выработка по сезону: среднее(площадь_дня / машин_в_день) по всем дням сезона
- Один httpx.AsyncClient и один Semaphore на весь процесс
"""

from __future__ import annotations


import asyncio
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path
from datetime import date, datetime, timezone, timedelta

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
from report_pipeline import fetch_tasks_mtfmi_resources, run_parallel_refs_and_fetch
import httpx
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
HEADER_FILL_COLOR = "8093B3"

from openpyxl.utils import get_column_letter
import pandas as pd
import requests
from excel_number_fmt import NF_INT, NF_DEC1, NF_DEC2, summary_cell_value_and_format
from excel_style import (
    DETAIL_MACHINES_MIN_AREA_NOTE,
    DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT_REPORT_8_9,
    pattern_fill_low_output_vs_plan,
    summary_rate_below_plan_fill,
    write_detail_machines_min_area_note,
)
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
    FOLDER_TO_FIRM,
    FIELD_NAME_FORCE_RULES,
    HIRED_GROUP_ID,
    AVAILABLE_OWN,
    PLAN_RATE_PER_GROUP,
    SKIP_CROPS,
)

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================
REPORT_DATE = date(2026, 9, 4)
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

SEASON_START = date(2026, 3, 25)
_season_start_raw = (
    os.environ.get("SVODKI_SEASON_START_DATE")
    or os.environ.get("SEASON_START_DATE")
    or ""
).strip()
if _season_start_raw:
    try:
        SEASON_START = date.fromisoformat(_season_start_raw)
    except ValueError:
        pass

DAY_START_HOUR: int = 6
DAY_END_HOUR:   int = 6

# Основные типы работ (культивация) — входят в общую сводку
WORK_TYPES: dict[int, str] = {
    10:  "Культивация",
    50:  "Культивация междурядная",
    63:  "Культивация 11 км/ч 8-10 см",
    96:  "2.11 Культивация",
}
# ID типа "Фрезерование" — только отдельные листы (не в сводке культивации)
FREZER_WT_ID = 73

ALL_WT_IDS = set(WORK_TYPES.keys()) | {FREZER_WT_ID}
FREZER_WT_TITLE = "2.6 Фрезерование"

# Основные культивационные IDs (без фрезерования)
CULT_WT_IDS = {wt_id for wt_id in ALL_WT_IDS if wt_id != FREZER_WT_ID}

# Агрофирмы, для которых строятся листы фрезерования
FREZER_FIRMS: list[str] = ["АФ Чистопольская"]

# Плановая выработка для фрезерования (га/сутки) — берётся из plans.py
FREZER_PLAN_RATE: float = PLAN_RATE_PER_GROUP.get("2.6 Фрезерование", 6.0)

WORK_TYPE_TITLE = "Культивация"

_REGION_IDS: dict[str, int] = {
    "АФ Чистопольская":            7,
    "АгроРаздолье":                9,
    "Аксу-Агро":                   8,
    "НурлатАгро":                 39,
    "Дрожжаное Рассвет Агро":     11,
    "Актай":                      10,
    "Лениногорская Птицефабрика": 38,
}

_WORK_TYPE_KEY = "2.11 Культивация"

# название → (region_id, план га/сутки, собств. агрегатов в наличии)
AGROFIRMS: dict[str, tuple[int, float, int]] = {
    firm: (
        _REGION_IDS[firm],
        PLAN_RATE_PER_GROUP.get(_WORK_TYPE_KEY, 250.0),
        AVAILABLE_OWN.get(_WORK_TYPE_KEY, {}).get(firm, 0),
    )
    for firm in FIRM_TO_FOLDER
}

FIRM_DISPLAY_NAMES: dict[str, str] = {
    "АФ Чистопольская":           'ООО АФ "ЧИСТОПОЛЬСКАЯ"',
    "АгроРаздолье":               'ООО "АГРОРАЗДОЛЬЕ"',
    "Аксу-Агро":                  'ООО "АКСУ АГРО"',
    "НурлатАгро":                 'ООО "НУРЛАТ АГРО"',
    "Дрожжаное Рассвет Агро":     'ООО "ДРОЖЖАНОЕ РАССВЕТ АГРО"',
    "Актай":                      'ООО "АКТАЙ"',
    "Лениногорская Птицефабрика": 'ООО "ЛЕНИНОГОРСКАЯ ПТИЦЕФАБРИКА"',
}

FIRM_LABELS_TABLE: dict[str, str] = {
    "НурлатАгро": "Нурлат Агро",
}

SEMAPHORE_LIMIT = 40

MIN_DETAIL_AREA_HA = 0.5
# Машины с площадью ниже этого порога не учитываются в счётчиках техники,
# времени работы и детальных таблицах, но их площадь суммируется в общий объём.
MIN_COUNT_AREA_HA = 10.0
# Фрезерование может быть меньше 0.5 га за смену; не отсекаем его из отдельной таблицы.
FREZER_MIN_DETAIL_AREA_HA = 0.0

MANUAL_OVERRIDES_ENV = "SVODKI_MANUAL_OVERRIDES_PATH"

# Глобальный кэш машин
machines_map: dict[int, dict] = {}


def _merge_manual_report8() -> dict:
    base: dict = {
        "plan_rate_by_firm": {k: float(v[1]) for k, v in AGROFIRMS.items()},
        "own_count_by_firm":  {k: int(v[2])  for k, v in AGROFIRMS.items()},
    }
    path = os.environ.get(MANUAL_OVERRIDES_ENV)
    if not path:
        return base
    p = Path(path)
    if not p.is_file():
        return base
    try:
        store = json.loads(p.read_text(encoding="utf-8"))
        block = store.get("8") or {}
        if isinstance(block.get("plan_rate_by_firm"), dict):
            for k, v in block["plan_rate_by_firm"].items():
                if k in AGROFIRMS:
                    base["plan_rate_by_firm"][k] = float(v)
        if isinstance(block.get("own_count_by_firm"), dict):
            for k, v in block["own_count_by_firm"].items():
                if k in AGROFIRMS:
                    base["own_count_by_firm"][k] = int(v)
    except Exception:
        pass
    return base


def _firm_table_label(firm_key: str) -> str:
    return FIRM_LABELS_TABLE.get(firm_key, firm_key)


def _firm_sheet_title_line(firm_key: str) -> str:
    return FIRM_LABELS_TABLE.get(
        firm_key, FIRM_DISPLAY_NAMES.get(firm_key, firm_key)
    )


# =============================================================================
# ВЫЧИСЛЕНИЕ ВРЕМЕННОГО ДИАПАЗОНА
# =============================================================================

def compute_time_range(
    report_date:    date,
    day_start_hour: int = DAY_START_HOUR,
    day_end_hour:   int = DAY_END_HOUR,
) -> tuple[datetime, datetime]:
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


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


def _norm_field_label(name: str) -> str:
    return (
        (name or "")
        .strip()
        .casefold()
        .replace(" ", "")
        .replace("\u00a0", "")
    )


def _firm_from_field_name(field_rec: dict) -> str | None:
    blob = _norm_field_label(
        f"{field_rec.get('name') or ''} {field_rec.get('title') or ''}"
    )
    if not blob:
        return None
    for needle, firm_key in FIELD_NAME_FORCE_RULES:
        if needle in blob:
            return firm_key
    return None


def _folder_parent_map(group_folders: list[dict]) -> dict[int, int | None]:
    parent: dict[int, int | None] = {}
    for gf in group_folders:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = gf.get("parent_id") or gf.get("parent_group_folder_id") or gf.get("group_folder_parent_id")
        parent[int(fid)] = int(pid) if pid is not None else None
    return parent


def _group_folder_name_firm_hints(group_folders: list[dict]) -> dict[int, str]:
    hints: dict[int, str] = {}
    for gf in group_folders:
        fid = gf.get("id")
        if fid is None:
            continue
        blob = _norm_field_label(f"{gf.get('name') or ''} {gf.get('title') or ''}")
        if not blob:
            continue
        if any(x in blob for x in ("лениногор", "птицефабрик", "лпф", "lpf")):
            hints[int(fid)] = "Лениногорская Птицефабрика"
    return hints


def _resolve_to_holding_root(
    folder_id:  int | None,
    parent_map: dict[int, int | None],
    root_ids:   set[int],
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


def _build_field_to_firm(
    fields_all: list[dict],
    fgs_all:    list[dict],
    gfs_all:    list[dict],
) -> dict[int, str]:
    fgs_map    = {fg["id"]: fg for fg in fgs_all}
    parent_map = _folder_parent_map(gfs_all)
    root_ids   = set(FIRM_TO_FOLDER.values())
    gf_hints   = _group_folder_name_firm_hints(gfs_all)

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
        for cid in _folder_id_chain(rint, parent_map):
            if cid in gf_hints:
                firm_key = gf_hints[cid]
                break
        if firm_key is None:
            root = _resolve_to_holding_root(rint, parent_map, root_ids)
            if root is not None:
                firm_key = FOLDER_TO_FIRM.get(root)
        if firm_key:
            field_to_firm[int(fid)] = firm_key

    forced = 0
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        fk = _firm_from_field_name(f)
        if fk:
            field_to_firm[int(fid)] = fk
            forced += 1

    print(
        f"  Полей с привязкой: {len(field_to_firm)} "
        f"(из {len(fields_all)} запрошенных, принудительно по имени: {forced})"
    )
    return field_to_firm


def _chunked(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _fetch_history_items_for_fields(field_ids: set[int] | None, year: int) -> list[dict]:
    season_year = str(year)
    if not field_ids:
        return get_all_from_id("history_items", {"year": season_year})
    print(
        f"  [API] history_items year={season_year} для {len(field_ids)} полей...",
        flush=True,
    )
    results: list[dict] = []
    for batch in _chunked(sorted(field_ids), 300):
        rows = get_all_from_id(
            "history_items",
            {"year": season_year, "field_id": ",".join(str(i) for i in batch)},
        )
        results.extend(rows)
    if not results:
        print(
            "  [API] history_items по field_id пусто — fallback по году",
            flush=True,
        )
        return get_all_from_id("history_items", {"year": season_year})
    return results


def _fetch_inventory_history_for_fields(field_ids: set[int] | None) -> list[dict]:
    base_params = {"historyable_type": "Field"}
    if not field_ids:
        return get_all_from_id("inventory_history_items", base_params)
    print(
        f"  [API] inventory_history_items для {len(field_ids)} полей...",
        flush=True,
    )
    results: list[dict] = []
    for batch in _chunked(sorted(field_ids), 300):
        rows = get_all_from_id(
            "inventory_history_items",
            {**base_params, "historyable_id": ",".join(str(i) for i in batch)},
        )
        results.extend(rows)
    if not results:
        print(
            "  [API] inventory_history_items по historyable_id пусто — fallback полный справочник",
            flush=True,
        )
        return get_all_from_id("inventory_history_items", base_params)
    return results


def load_field_filters(
    report_date: date,
    field_ids: set[int] | None = None,
) -> tuple[dict[int, str], set[int]]:
    """
    Активные поля с культурой и available в периоде — как в 1-й сводке.
    field_ids — только поля из заданий/mtfmi (батч history_items, без полного справочника).
    """
    season_year = report_date.year
    if field_ids:
        print(f"  Загружаем history_items (год {season_year}, {len(field_ids)} полей)...")
    else:
        print(f"  Загружаем history_items (год {season_year})...")
    from svodki_pipeline import run_reference_jobs

    ref = run_reference_jobs({
        "crops": lambda: get_all_from_id("crops"),
        "history": lambda: _fetch_history_items_for_fields(field_ids, season_year),
        "ihi": lambda: _fetch_inventory_history_for_fields(field_ids),
    })
    crops = {c["id"]: (c.get("name") or c.get("title", "")) for c in ref["crops"]}
    field_to_crop: dict[int, str] = {}
    for hi in ref["history"]:
        fid = hi.get("field_id")
        crop_id = hi.get("crop_id")
        if not (fid and crop_id and crop_id in crops and hi.get("active")):
            continue
        crop_name = crops[crop_id]
        if crop_name in SKIP_CROPS:
            continue
        field_to_crop[int(fid)] = crop_name
    print(f"  Активных полей с культурой: {len(field_to_crop)}")

    period_start = SEASON_START
    period_end = report_date
    available_field_ids: set[int] = set()
    for r in ref["ihi"]:
        if not r.get("available"):
            continue
        fid_r = r.get("historyable_id")
        if fid_r is None:
            continue
        if field_ids is not None and int(fid_r) not in field_ids:
            continue
        try:
            ev_start = date.fromisoformat(str(r.get("event_start_at"))[:10])
        except (TypeError, ValueError):
            continue
        raw_end = r.get("event_end_at")
        try:
            ev_end = date.fromisoformat(str(raw_end)[:10]) if raw_end else None
        except (TypeError, ValueError):
            ev_end = None
        if ev_start <= period_end and (ev_end is None or ev_end >= period_start):
            available_field_ids.add(int(fid_r))
    print(
        f"  Полей available=True в периоде {period_start}–{period_end}: "
        f"{len(available_field_ids)}"
    )
    return field_to_crop, available_field_ids


def _cov_in_window(item: dict, cutoff: datetime, *, use_fallback: bool) -> float:
    hourly = item.get("covered_area_hourly") or []
    cov = 0.0
    for entry in hourly:
        h = parse_dt(entry[0])
        if h and h < cutoff:
            cov += float(entry[1] or 0.0)
    if cov <= 0 and use_fallback and not hourly:
        cov = float(item.get("covered_area") or 0.0)
    return cov


def parse_dt(s) -> datetime | None:
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


def seconds_to_hms(seconds: float) -> str:
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"


def get_all_from_id(endpoint: str, extra_params: dict | None = None) -> list[dict]:
    params  = dict(extra_params or {})
    results = []
    from_id = 0
    while True:
        r = session.get(f"{BASE_URL}/{endpoint}",
                        params={**params, "from_id": from_id, "limit": CROPWISE_PAGE_LIMIT})
        r.raise_for_status()
        body  = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
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


async def fetch_all_tasks_and_mtfmi(
    client:        httpx.AsyncClient,
    semaphore:     asyncio.Semaphore,
    day_start:     datetime,
    cutoff:        datetime,
    season_start:  datetime,
    work_type_ids: set[int],
) -> tuple[list[dict], list[dict], dict[int, list[dict]]]:
    return await _fetch_tasks_mtfmi_shared(
        client, semaphore, BASE_URL,
        day_start, cutoff, season_start, work_type_ids,
        TasksMtfmiFetchOptions(
            dedupe=True,
            season_start_label=SEASON_START.strftime("%d.%m.%Y"),
            mtfmi_log_prefix="[r8] ",
        ),
    )


# =============================================================================
# ПОДСЧЁТ — агрофирма определяется по полю
# =============================================================================

def _empty_stats() -> dict:
    return {
        "machines_today":      0,
        "hired_today":         0,
        "area_today":          0.0,
        "engine_today":        0.0,
        "avg_season":          0.0,
        "season_sum":          0.0,
        "season_machine_days": 0,
        "season_by_day":       {},
        "detail_own":          [],
        "detail_hired":        [],
    }


def _aggregate_tasks_for_firms(
    tasks_today:      list[dict],
    tasks_season:     list[dict],
    mtfmi_map:        dict[int, list[dict]],
    season_start:     datetime,
    day_start:        datetime,
    cutoff:           datetime,
    field_to_firm:    dict[int, str],
    implements_dict:  dict[int, dict],
    filter_wt_ids:       set[int] | None = None,
    min_count_area_ha:   float | None = None,
    exempt_min_count:    bool = False,
    field_to_crop:       dict[int, str] | None = None,
    available_field_ids: set[int] | None = None,
) -> dict[str, dict]:
    """
    Универсальная функция агрегации статистики по агрофирмам.
    filter_wt_ids — если задан, обрабатываются только задачи с этим work_type_id.
    min_count_area_ha — порог для счётчиков/деталей; по умолчанию MIN_COUNT_AREA_HA (10 га).
    exempt_min_count — как MIN_COUNT_AREA_EXEMPT_GROUPS в 1-й сводке (фрезерование):
      площадь за сутки по полям, без порога 10 га на технику/время.
    """
    use_field_filters = field_to_crop is not None or available_field_ids is not None
    use_fallback_cov = exempt_min_count or use_field_filters
    min_detail_area = FREZER_MIN_DETAIL_AREA_HA if exempt_min_count else MIN_DETAIL_AREA_HA
    count_threshold = (
        MIN_COUNT_AREA_HA if min_count_area_ha is None else float(min_count_area_ha)
    )
    # Фильтрация по типу работ
    def _filter(tasks: list[dict]) -> list[dict]:
        if filter_wt_ids is None:
            return tasks
        return [t for t in tasks if t.get("work_type_id") in filter_wt_ids]

    _season_by_group: dict[str, set[str] | None] = {}

    def _allowed_for(group: str) -> set[str] | None:
        if group not in _season_by_group:
            try:
                _sdir = Path(__file__).resolve().parent / "utils" / "season"
                if not _sdir.is_dir():
                    _sdir = Path(__file__).resolve().parents[1] / "svodki" / "plan" / "utils" / "season"
                if _sdir.is_dir() and str(_sdir) not in sys.path:
                    sys.path.insert(0, str(_sdir))
                from season_unified import task_season_years_set_for_group

                _season_by_group[group] = task_season_years_set_for_group(group, REPORT_DATE)
            except Exception:
                _season_by_group[group] = None
        return _season_by_group[group]

    def _editor_season_ok(task: dict) -> bool:
        try:
            wt = int(task.get("work_type_id"))
        except (TypeError, ValueError):
            return False
        group = FREZER_WT_TITLE if wt == FREZER_WT_ID else "2.11 Культивация"
        allowed = _allowed_for(group)
        if not allowed:
            return True
        return str(task.get("season") or task.get("year") or "").strip() in allowed

    f_tasks_today  = [t for t in _filter(tasks_today) if _editor_season_ok(t)]
    f_tasks_season = [t for t in _filter(tasks_season) if _editor_season_ok(t)]
    _cap = None
    _fact_cult = (not exempt_min_count) and filter_wt_ids == CULT_WT_IDS
    if _fact_cult:
        try:
            from proverka.task_facts import bind_capture

            _cap = bind_capture()
        except Exception:
            _cap = None

    today_area_by_field: dict[int, float] = defaultdict(float)
    if exempt_min_count:
        for t in f_tasks_today:
            for item in mtfmi_map.get(t["id"], []):
                fid = item.get("field_id")
                if fid is None:
                    continue
                fid_i = int(fid)
                if available_field_ids is not None and fid_i not in available_field_ids:
                    continue
                if field_to_crop is not None and fid_i not in field_to_crop:
                    continue
                if not field_to_firm.get(fid_i):
                    continue
                cov = _cov_in_window(item, cutoff, use_fallback=use_fallback_cov)
                if cov > min_detail_area:
                    today_area_by_field[fid_i] += cov

    # per_firm_machine[firm][machine_id] → агрегированные данные
    per_firm_machine: dict[str, dict[int, dict]] = {
        firm: {} for firm in AGROFIRMS
    }

    for t in f_tasks_today:
        mid   = t.get("machine_id")
        items = mtfmi_map.get(t["id"], [])

        # Группируем items по агрофирме через поле
        firm_items: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            fid  = item.get("field_id")
            if fid is None:
                continue
            fid_i = int(fid)
            if available_field_ids is not None and fid_i not in available_field_ids:
                continue
            if field_to_crop is not None and fid_i not in field_to_crop:
                continue
            firm = field_to_firm.get(fid_i)
            if firm:
                firm_items[firm].append(item)

        if not firm_items:
            continue

        # Для масштабирования времени: общая площадь task-а в диапазоне
        total_cov_in_window = 0.0
        for item in items:
            for entry in (item.get("covered_area_hourly") or []):
                h = parse_dt(entry[0])
                if h and h < cutoff:
                    total_cov_in_window += entry[1] or 0

        for firm, f_items in firm_items.items():
            cov              = 0.0
            track_area       = 0.0
            work_distance_km = 0.0

            for item in f_items:
                hourly         = item.get("covered_area_hourly") or []
                cov_item_in    = _cov_in_window(item, cutoff, use_fallback=use_fallback_cov)
                cov_item_total = sum(float(e[1] or 0) for e in hourly)
                if cov_item_total <= 0 and use_fallback_cov:
                    cov_item_total = float(item.get("covered_area") or 0.0)
                cov += cov_item_in
                if _cap is not None and cov_item_in > 0:
                    fid_note = int(item.get("field_id") or 0)
                    crop_note = ""
                    if field_to_crop is not None and fid_note:
                        from mtfmi_batch_fetch import crop_of_item

                        crop_note = crop_of_item(item, str(field_to_crop.get(fid_note) or ""))
                    _cap[1](
                        ("2.11 Культивация", firm, mid),
                        group="2.11 Культивация",
                        firm=firm,
                        crop=crop_note,
                        field_id=fid_note or None,
                        task_id=int(t["id"]) if t.get("id") is not None else None,
                        machine_id=int(mid) if mid is not None else None,
                        ha=cov_item_in,
                        season=str(t.get("season") or ""),
                    )
                ratio = cov_item_in / cov_item_total if cov_item_total > 0 else 0.0
                track_area       += (item.get("covered_area_by_track") or 0) * ratio
                work_distance_km += ((item.get("work_distance") or 0) / 1000) * ratio

            if cov <= 0:
                continue

            pm = per_firm_machine[firm]
            if mid not in pm:
                pm[mid] = {
                    "cov_total":        0.0,
                    "work_dur":         0.0,
                    "track_area":       0.0,
                    "work_distance_km": 0.0,
                    "implement_width":  0.0,
                    "speeds":           [],
                }

            pm[mid]["cov_total"]        += cov
            pm[mid]["track_area"]       += track_area
            pm[mid]["work_distance_km"] += work_distance_km

            # Время масштабируем по доле площади этой фирмы в общей площади task-а
            work_dur  = t.get("work_duration") or 0
            road_move = t.get("movements_on_road_duration") or 0
            work_net  = max(work_dur - road_move, 0)
            if total_cov_in_window > 0:
                pm[mid]["work_dur"] += work_net * (cov / total_cov_in_window)
            else:
                pm[mid]["work_dur"] += work_net

            # Ширина захвата — из implements через implement_id задачи
            if not pm[mid]["implement_width"]:
                imp = implements_dict.get(t.get("implement_id"))
                w   = (imp.get("width") if imp else None) or 0
                pm[mid]["implement_width"] = float(w)

            sp = t.get("average_speed_on_field")
            if sp and sp > 0:
                pm[mid]["speeds"].append(sp)

    # Сезонная статистика:
    season_daily_area: dict[str, dict[str, float]] = {f: defaultdict(float) for f in AGROFIRMS}
    season_daily_machines: dict[str, dict[str, set[int]]] = {
        f: defaultdict(set) for f in AGROFIRMS
    }

    for t in f_tasks_season:
        mid = t.get("machine_id")
        end_dt = parse_dt(t.get("end_time"))
        if not end_dt:
            continue
        anchor = datetime(
            end_dt.year, end_dt.month, end_dt.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK
        )
        if end_dt < anchor:
            shift_date   = (end_dt - timedelta(days=1)).date().isoformat()
            shift_cutoff = anchor
        else:
            shift_date   = end_dt.date().isoformat()
            shift_cutoff = anchor + timedelta(days=1)

        items = mtfmi_map.get(t["id"], [])
        if not items:
            continue

        task_cov = float(t.get("covered_area") or 0)
        for item in items:
            fid  = item.get("field_id")
            if fid is None:
                continue
            fid_i = int(fid)
            if available_field_ids is not None and fid_i not in available_field_ids:
                continue
            if field_to_crop is not None and fid_i not in field_to_crop:
                continue
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue

            hourly   = item.get("covered_area_hourly") or []
            item_cov = 0.0
            for entry in hourly:
                h = parse_dt(entry[0])
                if h and h < shift_cutoff:
                    item_cov += float(entry[1] or 0.0)
            if item_cov <= 0 and use_fallback_cov and not hourly:
                item_cov = float(item.get("covered_area") or 0.0)
            if item_cov <= min_detail_area:
                continue

            season_daily_area[firm][shift_date] += item_cov
            if mid is not None:
                season_daily_machines[firm][shift_date].add(int(mid))

    # Собираем результат
    result: dict[str, dict] = {}

    # Исключаем текущий день — как в эталоне (upper = TODAY - 1)
    upper = (cutoff.date() - timedelta(days=1)).isoformat()

    for firm in AGROFIRMS:
        pm = per_firm_machine[firm]
        da = season_daily_area[firm]

        # Только дни до upper включительно (текущий день исключён)
        season_sum   = sum(v for d, v in da.items() if d <= upper)
        machine_days = sum(
            len(season_daily_machines[firm].get(day, set()))
            for day, area in da.items()
            if area > 0 and day <= upper
        )
        avg_season = round(season_sum / machine_days, 1) if machine_days else 0.0

        pm_filtered = {
            mid: d for mid, d in pm.items()
            if round(d["cov_total"], 2) > min_detail_area
        }

        if exempt_min_count:
            pm_counted = dict(pm)
        else:
            # Машины с площадью >= count_threshold — в деталях и счётчиках;
            # ниже порога — только в суммарной площади.
            pm_counted = {
                mid: d for mid, d in pm_filtered.items()
                if round(d["cov_total"], 2) >= count_threshold
            }

        today_by_field = {
            fid: cov for fid, cov in today_area_by_field.items()
            if field_to_firm.get(fid) == firm
        }
        area_from_fields = round(sum(today_by_field.values()), 2)

        if exempt_min_count:
            if not pm and area_from_fields <= 0:
                stats = _empty_stats()
                stats["avg_season"]          = avg_season
                stats["season_sum"]          = season_sum
                stats["season_machine_days"] = machine_days
                stats["season_by_day"]       = dict(da)
                result[firm] = stats
                continue
        elif not pm_filtered:
            stats = _empty_stats()
            stats["avg_season"]          = avg_season
            stats["season_sum"]          = season_sum
            stats["season_machine_days"] = machine_days
            stats["season_by_day"]       = dict(da)
            result[firm] = stats
            continue

        detail_own:   list[dict] = []
        detail_hired: list[dict] = []

        detail_source = pm if exempt_min_count else pm_counted
        for mid, d in detail_source.items():
            m     = machines_map.get(mid, {})
            group = m.get("machine_group_id")
            cov   = round(d["cov_total"], 2)
            secs  = d["work_dur"]
            rate  = round(cov / (secs / 3600), 1) if secs else 0.0
            # Скорость — округление до целых
            speed = round(sum(d["speeds"]) / len(d["speeds"]) * 3.6) \
                    if d["speeds"] else 0

            # Коэффициент ширины захвата
            w_real  = d["implement_width"]
            l_pole  = d["work_distance_km"]
            s_chist = d["track_area"]
            coef    = round(s_chist / l_pole / w_real * 10 * 100) \
                      if w_real and l_pole else 0

            row = {
                "Машина":        m.get("name", f"machine_{mid}"),
                "Площадь, га":   cov,
                "Время (сек)":   secs,
                "Время на полях": seconds_to_hms(secs),
                "Ср. выр-ка га/час": rate,
                "Средняя скорость на полях км/ч": speed,
                "Коэффициент использования конструкционной ширины захвата, %": coef,
            }
            if group == 7:
                detail_hired.append(row)
            else:
                detail_own.append(row)

        detail_own.sort(key=lambda x:   -x["Площадь, га"])
        detail_hired.sort(key=lambda x: -x["Площадь, га"])

        if exempt_min_count:
            area_pm = round(sum(d["cov_total"] for d in pm.values()), 2)
            area_today = area_from_fields if area_from_fields > 0 else area_pm
            engine_today = sum(d["work_dur"] for d in pm.values())
            machines_n   = len(pm)
        else:
            area_today   = sum(d["cov_total"] for d in pm_filtered.values())
            engine_today = sum(d["work_dur"]  for d in pm_counted.values())
            machines_n   = len(pm_counted)

        result[firm] = {
            "machines_today":      machines_n,
            "hired_today":         sum(1 for d in detail_hired),
            "area_today":          round(area_today, 2),
            "engine_today":        round(engine_today / 3600, 1),
            "avg_season":          avg_season,
            "season_sum":          season_sum,
            "season_machine_days": machine_days,
            "season_by_day":       dict(da),
            "detail_own":          detail_own,
            "detail_hired":        detail_hired,
        }

    if _cap is not None:
        ok_pairs = {
            (firm, mid)
            for firm, pm in per_firm_machine.items()
            for mid, d in pm.items()
            if round(d["cov_total"], 2) > min_detail_area
        }
        _cap[2]("2.11 Культивация", ok_pairs)

    return result


def build_all_firm_stats(
    tasks_today:         list[dict],
    tasks_season:        list[dict],
    mtfmi_map:           dict[int, list[dict]],
    season_start:        datetime,
    day_start:           datetime,
    cutoff:              datetime,
    field_to_firm:       dict[int, str],
    field_to_crop:       dict[int, str] | None = None,
    available_field_ids: set[int] | None = None,
    implements_dict:     dict[int, dict] | None = None,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """
    Возвращает:
      cult_details — культивация (wt 10, 50, 63, 96), без фрезерования
      frezer_details — фрезерование (wt=73), с фильтром полей по культуре/available
    """
    if implements_dict is None:
        implements_dict = {}

    cult_details = _aggregate_tasks_for_firms(
        tasks_today, tasks_season, mtfmi_map,
        season_start, day_start, cutoff, field_to_firm, implements_dict,
        filter_wt_ids=CULT_WT_IDS,
        min_count_area_ha=MIN_COUNT_AREA_HA,
        field_to_crop=field_to_crop,
        available_field_ids=available_field_ids,
    )
    frezer_details = _aggregate_tasks_for_firms(
        tasks_today, tasks_season, mtfmi_map,
        season_start, day_start, cutoff, field_to_firm, implements_dict,
        filter_wt_ids={FREZER_WT_ID},
        exempt_min_count=True,
        field_to_crop=field_to_crop,
        available_field_ids=available_field_ids,
    )
    return cult_details, frezer_details


# =============================================================================
# СТИЛИ И КОЛОНКИ
# =============================================================================

DETAIL_COLS = [
    "№", "Машина", "Площадь, га", "Время на полях",
    "Ср. выр-ка га/час", "Средняя скорость на полях км/ч",
    "Коэффициент использования конструкционной ширины захвата, %",
]

SUMMARY_COLS = [
    "№ п/п", "Агрофирма",
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


def _make_styles() -> dict:
    thin   = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    return {
        "border":        border,
        "align_c":       Alignment(horizontal="center", vertical="center", wrap_text=True),
        "align_l":       Alignment(vertical="center", wrap_text=True),
        "fill_hdr":      PatternFill("solid", fgColor=HEADER_FILL_COLOR),
        "fill_own":      PatternFill("solid", fgColor="FFFFFF"),
        "fill_hired":    PatternFill("solid", fgColor="FFFFFF"),
        "fill_total":    PatternFill("solid", fgColor="BDD7EE"),
        "fill_sec_own":  PatternFill("solid", fgColor="FFFFFF"),
        "fill_sec_hire": PatternFill("solid", fgColor="FFFFFF"),
        "font_hdr":      Font(bold=True, color="FFFFFF", size=13),
        "font_data":     Font(size=13),
        "font_total":    Font(bold=True, size=13),
        "font_title":    Font(bold=True, size=13),
        "font_sec":      Font(bold=True, size=12, color="111827"),
        "fill_alert_prod": pattern_fill_low_output_vs_plan(),
    }


def _style_cell(cell, s, fill=None, font=None, align=None) -> None:
    cell.border = s["border"]
    if fill:  cell.fill      = fill
    if font:
        cell.font = font
    else:
        cell.font = s["font_data"]
    if align: cell.alignment = align


def _estimate_wrapped_lines(value: object, col_width: float | None) -> int:
    text = str(value or "").strip()
    if not text:
        return 1
    width = float(col_width or 10)
    chars_per_line = max(1, int(width * 0.85))
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
    start_row:   int,
    end_row:     int,
    max_col:     int,
    min_height:  float = 20.0,
    line_height: float = 16.0,
    max_height:  float = 200.0,
) -> None:
    for r in range(start_row, end_row + 1):
        if ws.cell(row=r, column=1).value == DETAIL_MACHINES_MIN_AREA_NOTE:
            ws.row_dimensions[r].height = DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT_REPORT_8_9
            continue
        max_lines = 1
        for c in range(1, max_col + 1):
            cell = ws.cell(row=r, column=c)
            if cell.value in (None, ""):
                continue
            col_letter = get_column_letter(c)
            col_width  = ws.column_dimensions[col_letter].width
            max_lines  = max(max_lines, _estimate_wrapped_lines(cell.value, col_width))
        target  = min(max_height, max(min_height, max_lines * line_height + 4))
        current = ws.row_dimensions[r].height
        ws.row_dimensions[r].height = max(current or 0.0, target)


# =============================================================================
# ЗАПИСЬ ЛИСТА ДЕТАЛИЗАЦИИ ПО АГРОФИРМЕ
# =============================================================================

DETAIL_COL_WIDTHS_8 = [7, 46, 14, 16, 18, 20, 44]
DETAIL_PAGINATION_CFG_8 = DetailPaginationConfig(
    page_body_height_pt=505.0,
    page_safe_reserve_pt=36.0,
    section_title_height_pt=26.0,
    header_height_pt=48.0,
    data_row_height_pt=24.0,
    data_row_max_height_pt=36.0,
    total_row_height_pt=24.0,
    section_gap_height_pt=12.0,
    note_height_pt=DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT_REPORT_8_9,
)


def write_detail_sheet(
    wb:          openpyxl.Workbook,
    firm_name:   str,
    report_date: date,
    day_start:   datetime,
    cutoff:      datetime,
    stats:       dict,
    sheet_title: str | None = None,
) -> None:
    title = sheet_title if sheet_title is not None else _firm_table_label(firm_name)
    ws  = wb.create_sheet(title=title[:28])
    s   = _make_styles()
    n   = len(DETAIL_COLS)
    detail_own = list(stats.get("detail_own") or [])
    detail_hired = list(stats.get("detail_hired") or [])
    if not detail_own and not detail_hired:
        return

    for ci, w in enumerate(DETAIL_COL_WIDTHS_8, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    cfg = DETAIL_PAGINATION_CFG_8
    ctx = DetailPageContext(
        ws, n, DETAIL_COL_WIDTHS_8, cfg,
        on_new_page=lambda: _write_note(ctx, s, n),
    )

    def _detail_vals(idx: int, rd: dict) -> list[object]:
        return [
            idx, rd["Машина"], rd["Площадь, га"],
            rd["Время на полях"], rd["Ср. выр-ка га/час"],
            rd["Средняя скорость на полях км/ч"],
            rd["Коэффициент использования конструкционной ширины захвата, %"],
        ]

    def _write_note(page_ctx: DetailPageContext, styles: dict, n_cols: int) -> None:
        page_ctx.ensure_space(cfg.note_height_pt)
        page_ctx.row = write_detail_machines_min_area_note(
            ws, page_ctx.row, n_cols, border=styles["border"],
            height_pt=cfg.note_height_pt,
        )
        page_ctx.page_used_pt += cfg.note_height_pt

    _write_note(ctx, s, n)
    firm_single_page = compute_firm_single_page(
        ctx, detail_own, detail_hired, _detail_vals, include_note=True,
    )
    same_firm_both_sections = bool(detail_own and detail_hired)

    def _make_section_writer(sec_fill, row_fill, sec_title: str):
        def _caption(title: str, relaxed: bool) -> None:
            ctx.ensure_space(
                cfg.section_title_height_pt + cfg.header_height_pt,
                relaxed=relaxed,
            )
            sec_row = ctx.row
            ws.merge_cells(start_row=ctx.row, start_column=1, end_row=ctx.row, end_column=n)
            c = ws.cell(row=ctx.row, column=1, value=title)
            c.font = s["font_sec"]
            c.fill = sec_fill
            c.alignment = s["align_c"]
            ws.row_dimensions[sec_row].height = cfg.section_title_height_pt
            ctx.row += 1
            hdr_row = ctx.row
            for ci, col in enumerate(DETAIL_COLS, start=1):
                c = ws.cell(row=ctx.row, column=ci, value=col)
                _style_cell(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_c"])
            ws.row_dimensions[hdr_row].height = cfg.header_height_pt
            ctx.row += 1
            ctx.page_used_pt += cfg.section_title_height_pt + cfg.header_height_pt

        def _data_row(r: int, vals: list[object]) -> float:
            for ci, val in enumerate(vals, start=1):
                c = ws.cell(row=r, column=ci, value=val)
                _style_cell(
                    c, s, fill=row_fill,
                    align=s["align_l"] if ci == 2 else s["align_c"],
                )
            h = ctx.row_height(vals)
            ws.row_dimensions[r].height = h
            return h

        def _total_row() -> float:
            rows_data = detail_own if sec_title == "СОБСТВЕННЫЕ" else detail_hired
            tc  = round(sum(rd["Площадь, га"] for rd in rows_data), 2)
            ts  = sum(rd["Время (сек)"] for rd in rows_data)
            tr  = round(tc / (ts / 3600), 1) if ts else 0.0
            asp = round(sum(rd["Средняя скорость на полях км/ч"] for rd in rows_data) / len(rows_data))
            acf = round(
                sum(rd["Коэффициент использования конструкционной ширины захвата, %"]
                    for rd in rows_data) / len(rows_data)
            )
            r = ctx.row
            for ci, val in enumerate(
                ["Итого", "", tc, seconds_to_hms(ts), tr, asp, acf], start=1
            ):
                c = ws.cell(row=r, column=ci, value=val)
                _style_cell(c, s, fill=s["fill_total"], font=s["font_total"], align=s["align_c"])
            ws.row_dimensions[r].height = cfg.total_row_height_pt
            return cfg.total_row_height_pt

        return _caption, _data_row, _total_row

    cap_own, data_own, tot_own = _make_section_writer(
        s["fill_sec_own"], s["fill_own"], "СОБСТВЕННЫЕ",
    )
    if detail_own:
        write_paginated_section(
            ctx,
            sec_title="СОБСТВЕННЫЕ",
            rows=detail_own,
            detail_vals=_detail_vals,
            write_caption_and_header=cap_own,
            write_data_row=data_own,
            write_total_row=tot_own,
            firm_single_page=firm_single_page,
            same_firm_both_sections=same_firm_both_sections,
        )

    if detail_hired:
        if detail_own:
            ctx.add_section_gap(firm_single_page=firm_single_page)
        cap_hire, data_hire, tot_hire = _make_section_writer(
            s["fill_sec_hire"], s["fill_hired"], "НАЁМНЫЕ",
        )
        write_paginated_section(
            ctx,
            sec_title="НАЁМНЫЕ",
            rows=detail_hired,
            detail_vals=_detail_vals,
            write_caption_and_header=cap_hire,
            write_data_row=data_hire,
            write_total_row=tot_hire,
            continue_on_page=True,
            firm_single_page=firm_single_page,
            same_firm_both_sections=same_firm_both_sections,
        )


# =============================================================================
# ЗАПИСЬ ЛИСТА ФРЕЗЕРОВАНИЯ (все агрофирмы в одной таблице)
# =============================================================================

def write_frezer_summary_sheet(
    wb:           openpyxl.Workbook,
    report_date:  date,
    day_start:    datetime,
    cutoff:       datetime,
    firm_names:   list[str],
    frezer_details: dict[str, dict],
) -> None:
    ws  = wb.create_sheet(title="Фрезерование")
    s   = _make_styles()
    n   = len(DETAIL_COLS)
    row = 1

    def write_firm_section(firm_name: str, stats: dict) -> None:
        nonlocal row
        own_rows = list(stats.get("detail_own") or [])
        hired_rows = list(stats.get("detail_hired") or [])
        if not own_rows and not hired_rows:
            return

        def write_section(sec_title: str, rows_data: list[dict], row_fill, sec_fill) -> None:
            nonlocal row
            if not rows_data:
                return
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n)
            c = ws.cell(row=row, column=1, value=sec_title)
            c.font = s["font_sec"]
            c.fill = sec_fill
            c.alignment = s["align_c"]
            ws.row_dimensions[row].height = 26
            row += 1

            hdr_row = row
            for ci, col in enumerate(DETAIL_COLS, start=1):
                c = ws.cell(row=row, column=ci, value=col)
                _style_cell(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_c"])
            ws.row_dimensions[hdr_row].height = 48
            row += 1

            for i, rd in enumerate(rows_data, start=1):
                vals = [
                    i, rd["Машина"], rd["Площадь, га"],
                    rd["Время на полях"], rd["Ср. выр-ка га/час"],
                    rd["Средняя скорость на полях км/ч"],
                    rd["Коэффициент использования конструкционной ширины захвата, %"],
                ]
                mname = str(rd.get("Машина") or "")
                for ci, val in enumerate(vals, start=1):
                    c = ws.cell(row=row, column=ci, value=val)
                    _style_cell(c, s, fill=row_fill, align=s["align_l"] if ci == 2 else s["align_c"])
                ws.row_dimensions[row].height = 52 if len(mname) > 42 else 36
                row += 1

            tc = round(sum(rd["Площадь, га"] for rd in rows_data), 2)
            ts = sum(rd["Время (сек)"] for rd in rows_data)
            tr = round(tc / (ts / 3600), 1) if ts else 0.0
            asp = round(sum(rd["Средняя скорость на полях км/ч"] for rd in rows_data) / len(rows_data))
            acf = round(sum(rd["Коэффициент использования конструкционной ширины захвата, %"] for rd in rows_data) / len(rows_data))
            total_row_idx = row
            for ci, val in enumerate(["Итого", "", tc, seconds_to_hms(ts), tr, asp, acf], start=1):
                c = ws.cell(row=row, column=ci, value=val)
                _style_cell(c, s, fill=s["fill_total"], font=s["font_total"], align=s["align_c"])
            ws.row_dimensions[total_row_idx].height = 24
            row += 1

        write_section("СОБСТВЕННЫЕ", own_rows, s["fill_own"], s["fill_sec_own"])
        if own_rows and hired_rows:
            row += 1
        write_section("НАЁМНЫЕ", hired_rows, s["fill_hired"], s["fill_sec_hire"])
        row += 1

    for firm_name in firm_names:
        write_firm_section(firm_name, frezer_details[firm_name])

    ws.column_dimensions["A"].width = 7
    ws.column_dimensions["B"].width = 46
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 16
    ws.column_dimensions["E"].width = 18
    ws.column_dimensions["F"].width = 20
    ws.column_dimensions["G"].width = 44
    _auto_fit_row_heights(
        ws, start_row=1, end_row=max(1, row - 1), max_col=n,
        min_height=24.0, line_height=18.0,
    )


# =============================================================================
# СВОДНЫЙ ЛИСТ
# =============================================================================

def _build_summary_rows(
    firm_names:  list[str],
    all_details: dict[str, dict],
    manual:      dict | None = None,
    plan_rate_override: float | None = None,
) -> list[dict]:
    merged    = manual if manual is not None else _merge_manual_report8()
    plan_map  = merged.get("plan_rate_by_firm") or {}
    own_map   = merged.get("own_count_by_firm") or {}

    rows = []
    for i, firm_name in enumerate(firm_names, start=1):
        st        = all_details[firm_name]
        n_mach    = st["machines_today"]
        area      = st["area_today"]
        eng       = st["engine_today"]
        avg_s     = float(st.get("avg_season", 0.0) or 0.0)
        _, plan_d, own_d = AGROFIRMS[firm_name]
        plan_rate = plan_rate_override if plan_rate_override is not None else float(plan_map.get(firm_name, plan_d))
        own_count = int(own_map.get(firm_name, own_d))

        rows.append({
            "№ п/п":                                                              i,
            "Агрофирма":                                                          _firm_table_label(firm_name),
            "Плановая выработка, га/сутки":                                       plan_rate,
            "Количество задействованной техники":                                  n_mach,
            "Общий объем выполненной работы за сутки, га":                        area,
            "Выработка за сутки/1 ед агрегата, га":
                round(area / n_mach, 2) if n_mach else 0.0,
            "Средняя выработка по сезону, га/сутки":                              avg_s,
            "Общее время работы на полях, машино-часы за сутки":                  eng,
            "Среднее время эффективной работы на поле на 1 агрегат., час.":
                round(eng / n_mach, 1) if n_mach else 0.0,
            "Часовая выработка в расчёте на 1 агрегат, га":
                round(area / eng, 1)   if eng    else 0.0,
            "Всего собств. агрегатов в наличии":                                  own_count,
        })

    total_mach = sum(r["Количество задействованной техники"]            for r in rows)
    total_area = sum(r["Общий объем выполненной работы за сутки, га"]   for r in rows)
    total_eng  = sum(r["Общее время работы на полях, машино-часы за сутки"] for r in rows)
    total_own  = sum(r["Всего собств. агрегатов в наличии"]             for r in rows)

    # Средняя по сезону — как в эталоне: суммарная площадь / суммарные машино-дни
    total_season_sum  = sum(all_details[n].get("season_sum", 0.0)          for n in firm_names)
    total_machine_days = sum(all_details[n].get("season_machine_days", 0)  for n in firm_names)
    avg_season_total  = round(total_season_sum / total_machine_days, 1) if total_machine_days else 0.0

    rows.append({
        "№ п/п":                                                              "",
        "Агрофирма":                                                          "Итого",
        "Плановая выработка, га/сутки":
            rows[0]["Плановая выработка, га/сутки"] if rows else 0,
        "Количество задействованной техники":                                  total_mach,
        "Общий объем выполненной работы за сутки, га":                        round(total_area, 2),
        "Выработка за сутки/1 ед агрегата, га":
            round(total_area / total_mach, 2) if total_mach else 0.0,
        "Средняя выработка по сезону, га/сутки":                              avg_season_total,
        "Общее время работы на полях, машино-часы за сутки":                  round(total_eng, 1),
        "Среднее время эффективной работы на поле на 1 агрегат., час.":
            round(total_eng / total_mach, 1) if total_mach else 0.0,
        "Часовая выработка в расчёте на 1 агрегат, га":
            round(total_area / total_eng, 1) if total_eng else 0.0,
        "Всего собств. агрегатов в наличии":                                  total_own,
    })
    return rows


def write_summary_sheet(
    wb:          openpyxl.Workbook,
    report_date: date,
    day_start:   datetime,
    cutoff:      datetime,
    firm_names:  list[str],
    all_details: dict[str, dict],
    manual:      dict | None = None,
) -> None:
    ws = wb.create_sheet(title="Сводка", index=0)
    s  = _make_styles()
    n  = len(SUMMARY_COLS)
    row = 1

    for ci, col in enumerate(SUMMARY_COLS, start=1):
        c = ws.cell(row=row, column=ci, value=col)
        _style_cell(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_c"])
    row += 1

    summary_rows = _build_summary_rows(firm_names, all_details, manual)
    _sk_plan = "Плановая выработка, га/сутки"
    _sk_mach = "Количество задействованной техники"
    _sk_rate = "Выработка за сутки/1 ед агрегата, га"
    for row_data in summary_rows:
        is_total = row_data.get("Агрофирма") == "Итого"
        fill     = s["fill_total"] if is_total else s["fill_own"]
        font     = s["font_total"] if is_total else None
        for ci, col in enumerate(SUMMARY_COLS, start=1):
            raw = row_data.get(col, "")
            val, nf = summary_cell_value_and_format(col, raw)
            c = ws.cell(row=row, column=ci, value=val)
            if nf:
                c.number_format = nf
            cell_fill = summary_rate_below_plan_fill(
                col,
                row_data,
                fill,
                s["fill_alert_prod"],
                plan_key=_sk_plan,
                machines_key=_sk_mach,
                rate_key=_sk_rate,
            )
            _style_cell(c, s, fill=cell_fill, font=font,
                        align=s["align_l"] if ci == 2 else s["align_c"])
        row += 1

    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 35
    for ci in range(3, n + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 20
    _auto_fit_row_heights(ws, start_row=1, end_row=max(1, row - 1), max_col=n)


def write_frezer_perf_sheet(
    wb: openpyxl.Workbook,
    firm_names: list[str],
    frezer_details: dict[str, dict],
    manual: dict | None = None,
) -> None:
    """
    Дополнительный лист фрезерования в оформлении, аналогичном первой сводке
    (сводная таблица по агрофирмам + строка Итого).
    """
    ws = wb.create_sheet(title="Выраб. — 2.6 Фрезерование")
    s = _make_styles()
    n = len(SUMMARY_COLS)
    row = 1

    for ci, col in enumerate(SUMMARY_COLS, start=1):
        c = ws.cell(row=row, column=ci, value=col)
        _style_cell(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_c"])
    row += 1

    summary_rows = _build_summary_rows(firm_names, frezer_details, manual,
                                        plan_rate_override=FREZER_PLAN_RATE)
    _sk_plan = "Плановая выработка, га/сутки"
    _sk_mach = "Количество задействованной техники"
    _sk_rate = "Выработка за сутки/1 ед агрегата, га"
    for row_data in summary_rows:
        is_total = row_data.get("Агрофирма") == "Итого"
        fill = s["fill_total"] if is_total else s["fill_own"]
        font = s["font_total"] if is_total else None
        for ci, col in enumerate(SUMMARY_COLS, start=1):
            raw = row_data.get(col, "")
            val, nf = summary_cell_value_and_format(col, raw)
            c = ws.cell(row=row, column=ci, value=val)
            if nf:
                c.number_format = nf
            cell_fill = summary_rate_below_plan_fill(
                col,
                row_data,
                fill,
                s["fill_alert_prod"],
                plan_key=_sk_plan,
                machines_key=_sk_mach,
                rate_key=_sk_rate,
            )
            _style_cell(c, s, fill=cell_fill, font=font, align=s["align_l"] if ci == 2 else s["align_c"])
        row += 1

    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 35
    for ci in range(3, n + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 20
    _auto_fit_row_heights(ws, start_row=1, end_row=max(1, row - 1), max_col=n)


# =============================================================================
# ПОСТРОЕНИЕ ОТЧЁТА
# =============================================================================

async def build_report_async(report_date: date = REPORT_DATE) -> None:
    global machines_map

    day_start, cutoff = compute_time_range(report_date, DAY_START_HOUR, DAY_END_HOUR)
    season_start = datetime(
        SEASON_START.year, SEASON_START.month, SEASON_START.day, tzinfo=TZ_MSK
    )

    print(f"Отчёт '{WORK_TYPE_TITLE}'  |  Дата: {report_date}")
    print(f"  Суточный диапазон: {day_start} — {cutoff}")
    print(f"  Сезон (средняя выработка): с {SEASON_START.strftime('%d.%m.%Y')}\n")

    firm_names = list(AGROFIRMS.keys())

    from report_resources_fetch import collect_report_resource_ids

    mtfmi_opt = TasksMtfmiFetchOptions(
        dedupe=True,
        season_start_label=SEASON_START.strftime("%d.%m.%Y"),
        mtfmi_log_prefix="[r8] ",
    )

    async def _prefetch_r8():
        print(f"\n  Загружаем задания, mtfmi и ресурсы отчёта (все агрофирмы, один проход)...")
        return await fetch_tasks_mtfmi_resources(
            API_TOKEN, BASE_URL, day_start, cutoff, season_start, ALL_WT_IDS,
            _build_field_to_firm, get_all_from_id,
            semaphore_limit=SEMAPHORE_LIMIT,
            mtfmi_options=mtfmi_opt,
            log_label="r8",
        )

    _, bundle = await run_parallel_refs_and_fetch(
        lambda: get_all_from_id("crops"), _prefetch_r8, label="r8-bundle",
    )
    tasks_today = bundle.tasks_today
    tasks_season = bundle.tasks_season
    mtfmi_map = bundle.mtfmi_map
    field_to_firm = bundle.field_to_firm
    machines_map = bundle.machines_map
    implements_dict = bundle.implements_dict
    field_ids, _, _ = collect_report_resource_ids(tasks_today + tasks_season, mtfmi_map)

    field_to_crop, available_field_ids = load_field_filters(report_date, field_ids)

    print("  Распределяем задания по агрофирмам через поля...")
    cult_details, frezer_details = build_all_firm_stats(
        tasks_today, tasks_season, mtfmi_map,
        season_start, day_start, cutoff, field_to_firm,
        field_to_crop, available_field_ids, implements_dict,
    )

    manual_r8 = _merge_manual_report8()

    chist = "АФ Чистопольская"
    fz_st = frezer_details.get(chist, {})
    print(
        f"\nФрезерование ({chist}): "
        f"площадь={fz_st.get('area_today', 0)}, "
        f"машин={fz_st.get('machines_today', 0)}, "
        f"строк детализации own={len(fz_st.get('detail_own') or [])}, "
        f"hired={len(fz_st.get('detail_hired') or [])}"
    )

    # Консольный вывод
    summary_rows = _build_summary_rows(firm_names, cult_details, manual_r8)
    df = pd.DataFrame(summary_rows)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 300)
    pd.set_option("display.float_format", "{:.2f}".format)
    print("\nСВОДКА (культивация, без фрезерования):")
    print(df.to_string(index=False))

    output_file = f"8. report_cultivation_{report_date}.xlsx"
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    # Лист 1: Сводка — только культивация (wt 10, 50, 63, 96)
    write_summary_sheet(wb, report_date, day_start, cutoff,
                        firm_names, cult_details, manual_r8)

    # Лист 2: Дополнительная таблица фрезерования в стиле первой сводки.
    write_frezer_perf_sheet(wb, FREZER_FIRMS, frezer_details, manual_r8)

    # Листы детализации по каждой агрофирме — только культивация
    for firm_name in firm_names:
        write_detail_sheet(
            wb, firm_name, report_date, day_start, cutoff,
            cult_details[firm_name],
        )

    # Лист фрезерования: только Чистопольская
    write_frezer_summary_sheet(
        wb, report_date, day_start, cutoff,
        FREZER_FIRMS, frezer_details,
    )

    wb.save(output_file)
    print(f"\nСохранено: {output_file}")


def build_report(report_date: date = REPORT_DATE) -> None:
    run_report_async(build_report_async(report_date))


if __name__ == "__main__":
    build_report()
