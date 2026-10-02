# 11.06.2026
"""
26 Cropwise Operations — отчёт "Опрыскивание"
==========================================
wt=64 (3.1 Опрыскивание)

Логика:
- Площадь: covered_area из machine_task где end_time в [DAY_START, CUTOFF)
- DAY_START = report_date - 1 день + DAY_START_HOUR (по умолчанию 06:00)
- CUTOFF    = report_date + DAY_END_HOUR   (по умолчанию 06:00)
- Время:   work_duration - movements_on_road_duration из machine_task
- Скорость: average_speed_on_field (м/с) × 3.6 → км/ч
- Сред. выработка по сезону: sum(площадь за сезон) / sum(машин в дни с выработкой)
- Серверные фильтры: end_time_gt_eq/lt, date_start_lt_eq + date_end_gt_eq
- Все агрофирмы параллельно, один AsyncClient и Semaphore
"""

from __future__ import annotations


import asyncio
import json
import math
import os
import sys
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
    fetch_all_pages,
    fetch_mtfmi_map_hybrid_async,
    fetch_tasks_day_and_season,
    report_async_client,
    run_report_async,
)
from report_pipeline import run_parallel_refs_and_fetch
from svodki_timing import stage, timing_finish, timing_start
import httpx
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.pagebreak import Break
import pandas as pd
import requests
from excel_number_fmt import NF_DEC1, NF_DEC2, NF_INT, summary_cell_value_and_format
from excel_style import (
    DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT,
    pattern_fill_low_output_vs_plan,
    summary_rate_below_plan_fill,
    write_detail_machines_min_area_note,
)
from plans import (
    API_TOKEN,
    BASE_URL,
    TZ_MSK,
    FIRM_TO_FOLDER,
    FOLDER_TO_FIRM,
    HIRED_GROUP_ID,
    AVAILABLE_OWN,
    PLAN_RATE_PER_GROUP,
    SKIP_CROPS,
    WORK_GROUPS,
    VEG_CROP_ROWS,
    VEG_CROP_PLAN_HA,
    HARDCODED_PLAN,
)
WORK_TYPE_TITLE = "3.1 Опрыскивание"
_SPRAY_KEY = "3.1 Опрыскивание"
_VEG_SPRAY_KEY  = "3.2 Опрыскивание (овощи)"

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================
REPORT_DATE = date(2026, 9, 4)
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

SEASON_YEAR = 2026
SEASONS     = ["2026"]   # опрыскивание только сезон 2025

SEASON_START = date(2026, 1, 1)  # средняя выработка по сезону считается с этой даты
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

# Настройка временного диапазона суток
# DAY_START_HOUR — час начала суток (по умолчанию 6 = 06:00)
# DAY_END_HOUR   — час конца суток (по умолчанию 6 = следующий день 06:00)
# Примеры:
#   06:00 — 06:00 следующего дня: DAY_START_HOUR=6,  DAY_END_HOUR=6
#   06:00 — 17:59 того же дня:    DAY_START_HOUR=6,  DAY_END_HOUR=18
#   07:00 — 06:59 следующего дня: DAY_START_HOUR=7,  DAY_END_HOUR=7
DAY_START_HOUR: int = 6   # час начала суток
DAY_END_HOUR:   int = 6   # час конца суток (если == DAY_START_HOUR → следующий день)

WORK_TYPES: dict[int, str] = {
    1:   "Опрыскивание",
    64:  "3.1 Опрыскивание",
    65:  "Опрыскивание с нормой 101-200 л/га",
    66:  "Опрыскивание с нормой 201-300 л/га",
    153: "Опрыскивание с нормой 301-400 л/га",
}
# ID задач для обычного опрыскивания (3.1)
SPRAY_WT_IDS: set[int] = set(WORK_TYPES.keys())

# ID задач для опрыскивания овощей (3.2) — из WORK_GROUPS plans
VEGGIE_SPRAY_WT_IDS: set[int] = {
    wt_id
    for name, ids in WORK_GROUPS
    if name == _VEG_SPRAY_KEY
    for wt_id in ids
}

ALL_WT_IDS = SPRAY_WT_IDS | VEGGIE_SPRAY_WT_IDS

VEG_WT_IDS: set[int] = set(VEGGIE_SPRAY_WT_IDS) or {151}
VEG_REPORT_FIRMS: tuple[str, ...] = ("АФ Чистопольская",)

# Культуры, которые относятся к овощам (для 3.2 Опрыскивание овощи)
VEGGIE_CROPS: set[str] = {"Картофель", "Морковь", "Свекла столовая"}
# Агрофирма с овощами
VEGGIE_FIRM = "АФ Чистопольская"
MANUAL_OVERRIDES_ENV = "SVODKI_MANUAL_OVERRIDES_PATH"

_REGION_IDS: dict[str, int] = {
    "АФ Чистопольская":            7,
    "АгроРаздолье":                9,
    "Аксу-Агро":                   8,
    "НурлатАгро":                 39,
    "Дрожжаное Рассвет Агро":     11,
    "Актай":                      10,
    "Лениногорская Птицефабрика": 38,
}

_WORK_TYPE_KEY = "3.1 Опрыскивание"

# название → (region_id, план га/сутки, собств. агрегатов в наличии)
_BASE_AGROFIRMS_6: dict[str, tuple[int, float, int]] = {
    firm: (
        _REGION_IDS[firm],
        PLAN_RATE_PER_GROUP.get(_WORK_TYPE_KEY, 200.0),
        AVAILABLE_OWN.get(_WORK_TYPE_KEY, {}).get(firm, 0),
    )
    for firm in FIRM_TO_FOLDER
}


def _merge_manual_report6(
    base: dict[str, tuple[int, float, int]],
) -> dict[str, tuple[int, float, int]]:
    path = os.environ.get(MANUAL_OVERRIDES_ENV)
    if not path:
        return base
    p = Path(path)
    if not p.is_file():
        return base
    try:
        store = json.loads(p.read_text(encoding="utf-8"))
        block = store.get("6") or {}
    except Exception:
        return base
    out = {k: list(v) for k, v in base.items()}
    if isinstance(block.get("plan_rate_by_firm"), dict):
        for k, v in block["plan_rate_by_firm"].items():
            if k in out:
                try:
                    out[k][1] = float(v)
                except (TypeError, ValueError):
                    pass
    if isinstance(block.get("own_count_by_firm"), dict):
        for k, v in block["own_count_by_firm"].items():
            if k in out:
                try:
                    out[k][2] = int(v)
                except (TypeError, ValueError):
                    pass
    return {k: tuple(t) for k, t in out.items()}


AGROFIRMS = _merge_manual_report6(_BASE_AGROFIRMS_6)


def _veg_spray_plan_ha() -> dict[str, float]:
    raw = (HARDCODED_PLAN.get(_VEG_SPRAY_KEY) or {}).get("АФ Чистопольская") or {}
    out: dict[str, float] = {}
    for crop in VEG_CROP_ROWS:
        if crop == "Свекла":
            val = raw.get("Свекла столовая", raw.get("Свекла", VEG_CROP_PLAN_HA.get(crop, 0.0)))
        else:
            val = raw.get(crop, VEG_CROP_PLAN_HA.get(crop, 0.0))
        out[crop] = float(val or 0.0)
    return out


_BASE_VEG_SPRAY_AGROFIRMS: dict[str, tuple[int, float, int]] = {
    firm: (
        _REGION_IDS[firm],
        PLAN_RATE_PER_GROUP.get(_VEG_SPRAY_KEY, 200.0),
        AVAILABLE_OWN.get(_VEG_SPRAY_KEY, {}).get(firm, 0),
    )
    for firm in FIRM_TO_FOLDER
    if (AVAILABLE_OWN.get(_VEG_SPRAY_KEY, {}) or {}).get(firm, 0) > 0
}


def _merge_manual_veg_report6(
    base: dict[str, tuple[int, float, int]],
) -> dict[str, tuple[int, float, int]]:
    path = os.environ.get(MANUAL_OVERRIDES_ENV)
    if not path:
        return base
    p = Path(path)
    if not p.is_file():
        return base
    try:
        store = json.loads(p.read_text(encoding="utf-8"))
        block = store.get("6") or {}
    except Exception:
        return base
    out = {k: list(v) for k, v in base.items()}
    for plan_key, own_key in (
        ("veg_spray_plan_rate_by_firm", "veg_spray_own_count_by_firm"),
        ("veg_plan_rate_by_firm", "veg_own_count_by_firm"),
    ):
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


VEG_AGROFIRMS = _merge_manual_veg_report6(_BASE_VEG_SPRAY_AGROFIRMS)


def _build_manual_dict() -> dict:
    """
    Возвращает словарь в том же формате что эталон:
      plan_rate_by_firm, available_self_propelled_by_firm
    Значения берутся из уже смёрженного AGROFIRMS.
    """
    return {
        "plan_rate_by_firm":               {k: float(v[1]) for k, v in AGROFIRMS.items()},
        "available_self_propelled_by_firm": {k: int(v[2])  for k, v in AGROFIRMS.items()},
    }


FIRM_LABELS_TABLE: dict[str, str] = {
    "НурлатАгро": "Нурлат Агро",
}


def _firm_table_label(firm_key: str) -> str:
    return FIRM_LABELS_TABLE.get(firm_key, firm_key)

SEMAPHORE_LIMIT = 40
MIN_DETAIL_AREA_HA = 0.5
# Счётчик техники, детализация и время — как во 2-й сводке.
MIN_COUNT_AREA_HA = 10.0
machines_map: dict[int, dict] = {}
crop_map: dict[int, str] = {}
field_to_fg: dict[int, int] = {}
fg_to_folder: dict[int, int] = {}
folder_to_firm: dict[int, str] = {}
field_to_crop: dict[int, str] = {}
available_field_ids: set[int] = set()


# =============================================================================
# ВЫЧИСЛЕНИЕ ВРЕМЕННОГО ДИАПАЗОНА
# =============================================================================

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


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


def seconds_to_hms(seconds: float) -> str:
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"


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
        if not last_id or meta.get("obtained_records", 0) < 100: break
        from_id = last_id + 1
    return results


def get_all_page(endpoint: str, params: dict | None = None) -> list[dict]:
    params = dict(params or {})
    params.setdefault("per_page", 1000)
    results, page = [], 1
    while True:
        r = session.get(f"{BASE_URL}/{endpoint}", params={**params, "page": page})
        r.raise_for_status()
        body = r.json()
        chunk = body if isinstance(body, list) else body.get("data", [])
        results.extend(chunk)
        if len(chunk) < params["per_page"]:
            break
        page += 1
    return results


def load_reference_data() -> None:
    global crop_map, field_to_fg, fg_to_folder, folder_to_firm, field_to_crop, available_field_ids

    from svodki_pipeline import run_reference_jobs

    print("  Загружаем справочники (crops, field_groups, history, inventory)...")
    raw = run_reference_jobs({
        "crops": lambda: get_all_page("crops"),
        "field_groups": lambda: get_all_page("field_groups"),
        "history": lambda: get_all_from_id("history_items", {"year": str(SEASON_YEAR)}),
        "ihi": lambda: get_all_from_id(
            "inventory_history_items", {"historyable_type": "Field"},
        ),
    })
    crops = raw["crops"]
    crop_map = {c["id"]: c.get("name") or c.get("title", "") for c in crops}
    print(f"    {len(crop_map)} культур")

    fgs = raw["field_groups"]
    fg_to_folder = {fg["id"]: fg.get("group_folder_id") for fg in fgs}
    folder_to_firm = dict(FOLDER_TO_FIRM)
    print(f"    {len(fgs)} групп")

    his = raw["history"]
    field_to_crop.clear()
    for hi in his:
        fid = hi.get("field_id")
        crop_id = hi.get("crop_id")
        active = hi.get("active")
        if not (fid and crop_id and crop_id in crop_map and active):
            continue
        crop_name = crop_map[crop_id]
        if crop_name in SKIP_CROPS:
            continue
        field_to_crop[fid] = crop_name
    print(f"    {len(field_to_crop)} активных полей с культурой (без SKIP_CROPS)")

    period_start = SEASON_START
    period_end   = REPORT_DATE
    available_field_ids.clear()
    for r in raw["ihi"]:
        if not r.get("available"):
            continue
        fid_r = r.get("historyable_id")
        if fid_r is None:
            continue
        try:
            ev_start = date.fromisoformat(str(r.get("event_start_at"))[:10]) if r.get("event_start_at") else None
        except (TypeError, ValueError):
            ev_start = None
        try:
            ev_end = date.fromisoformat(str(r.get("event_end_at"))[:10]) if r.get("event_end_at") else None
        except (TypeError, ValueError):
            ev_end = None
        if ev_start is None:
            continue
        if ev_start <= period_end and (ev_end is None or ev_end >= period_start):
            available_field_ids.add(int(fid_r))
    print(f"    {len(available_field_ids)} полей available в периоде {period_start}–{period_end}")


def get_firm_for_field(fid: int) -> str:
    folder = fg_to_folder.get(field_to_fg.get(fid))
    return folder_to_firm.get(folder, "")


def _field_ok(fid: int) -> bool:
    """Поле должно быть в field_to_crop (культура не в SKIP_CROPS) и в available_field_ids."""
    return fid in field_to_crop and fid in available_field_ids


def _firms_for_task_items(items: list[dict]) -> set[str]:
    """Агрофирмы задания по полям mtfmi (только поля, прошедшие _field_ok)."""
    firms: set[str] = set()
    for item in items:
        fid = item.get("field_id")
        if not fid:
            continue
        try:
            fid_i = int(fid)
        except (TypeError, ValueError):
            continue
        if not _field_ok(fid_i):
            continue
        firm = get_firm_for_field(fid_i)
        if firm:
            firms.add(firm)
    return firms


def _task_cov_by_firm(
    task: dict,
    items: list[dict],
    cutoff: datetime | None = None,
    window_start: datetime | None = None,
    item_allowed=None,
) -> dict[str, float]:
    """Площадь задания по агрофирмам — единая логика сводок 1/2/6 (task_area_shared)."""
    from task_area_shared import task_cov_by_firm as _shared_task_cov_by_firm

    return _shared_task_cov_by_firm(
        task,
        items,
        cutoff=cutoff,
        window_start=window_start,
        field_ok=_field_ok,
        firm_for_field=get_firm_for_field,
        parse_dt=parse_dt,
        item_allowed=item_allowed,
    )


def parse_dt(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_MSK)
        else:
            dt = dt.astimezone(TZ_MSK)
        return dt
    except (ValueError, TypeError):
        return None


def _map_veggie_crop(raw_name: str) -> str | None:
    n = str(raw_name or "").strip().lower().replace("-", " ").replace("_", " ")
    if not n:
        return None
    if "карто" in n:
        return "Картофель"
    if "лук" in n:
        return "Лук"
    if "морков" in n:
        return "Морковь"
    if "свек" in n:
        return "Свекла столовая"
    return None


def _normalize_veggie_crop_label(raw_name: str) -> str | None:
    mapped = _map_veggie_crop(raw_name)
    if not mapped:
        return None
    if mapped == "Свекла столовая":
        return "Свекла"
    return mapped


def _task_veggie_crop_label(task: dict) -> str | None:
    return None


def _item_area_in_window(
    item: dict,
    window_end: datetime | None,
    window_start: datetime | None = None,
) -> float:
    from task_area_shared import item_area_in_window as _shared_item_area

    return _shared_item_area(
        item, window_end, window_start, parse_dt=parse_dt,
    )


def _item_area_before_cutoff(item: dict, cutoff: datetime) -> float:
    return _item_area_in_window(item, cutoff)


def _task_shift_date(task: dict, cutoff: datetime) -> date | None:
    end_dt = parse_dt(task.get("end_time"))
    if end_dt is None:
        return None
    anchor = datetime(
        end_dt.year, end_dt.month, end_dt.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    return (end_dt - timedelta(days=1)).date() if end_dt < anchor else end_dt.date()


def _veggie_crop_from_items(
    task_items: list[dict],
    window_end: datetime | None = None,
) -> dict[str, float]:
    result: dict[str, float] = defaultdict(float)
    for item in task_items:
        fid = item.get("field_id")
        if not fid:
            continue
        try:
            fid_i = int(fid)
        except (TypeError, ValueError):
            continue
        if not _field_ok(fid_i):
            continue
        if get_firm_for_field(fid_i) != VEGGIE_FIRM:
            continue
        from mtfmi_batch_fetch import crop_of_item

        crop = _map_veggie_crop(crop_of_item(item, field_to_crop.get(fid_i, "")))
        if not crop:
            continue
        area = _item_area_in_window(item, window_end)
        if area > 0:
            result[crop] += area
    return result



def _task_implement_width(task: dict, implements_dict: dict[int, dict]) -> float:
    """Ширина захвата: implements.width по implement_id, иначе real_implement_width."""
    imp = implements_dict.get(task.get("implement_id"))
    w = float((imp.get("width") if imp else None) or 0)
    if w > 0:
        return w
    return float(task.get("real_implement_width") or 0)


# =============================================================================
# АСИНХРОННЫЕ ЗАПРОСЫ
# =============================================================================


def _field_veggie_crop_label(
    field_id: int | None,
    task: dict,
    item: dict | None = None,
) -> str | None:
    from mtfmi_batch_fetch import crop_of_item

    fallback = field_to_crop.get(int(field_id), "") if field_id is not None else ""
    raw = crop_of_item(item, fallback) if item is not None else fallback
    label = _normalize_veggie_crop_label(raw)
    if label:
        return label
    return _task_veggie_crop_label(task)


async def fetch_mtfmi(
    client:    httpx.AsyncClient,
    task_id:   int,
    semaphore: asyncio.Semaphore,
) -> tuple[int, list[dict]]:
    items = await fetch_all_pages(
        client, f"{BASE_URL}/machine_task_field_mapping_items",
        {"machine_task_id": task_id}, semaphore,
    )
    return task_id, items


def _resolve_group_task_season_years(group_key: str) -> list[int] | None:
    """Сезоны Cropwise для группы — из season_unified (редактор планов), не общий spray env."""
    plan_dir = Path(__file__).resolve().parent
    season_dir = plan_dir / "utils" / "season"
    for entry in (season_dir, plan_dir):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))
    try:
        from season_unified import fact_season_years_for_group

        years = fact_season_years_for_group(group_key, REPORT_DATE)
        if years:
            return list(years)
    except Exception:
        pass
    return None


def _resolve_task_season_years(
    task_season_years: list[int] | None = None,
    *,
    track_veg_crops: bool = False,
) -> set[str] | None:
    """
    Фильтр machine_task.season для опрыскивания.

    SVODKI_SPRAY_TASK_SEASON_YEARS — выбор сезонов именно этой группы работ.
    Для овощного прогона (3.2) не используем глобальный SEASON_TASK_YEAR_STR
    от посева/других сводок — иначе факт ~21k га обнуляется.
    """
    if task_season_years:
        return {str(y) for y in task_season_years}
    multi = os.environ.get("SVODKI_SPRAY_TASK_SEASON_YEARS", "").strip()
    if multi:
        return {y.strip() for y in multi.split(",") if y.strip()}
    if track_veg_crops:
        return None
    single = os.environ.get("SEASON_TASK_YEAR_STR", "").strip()
    if single:
        return {single}
    return None


def _filter_tasks_by_season(
    tasks: list[dict],
    allowed_seasons: set[str] | None,
) -> list[dict]:
    if not allowed_seasons:
        return tasks
    return [t for t in tasks if str(t.get("season") or "") in allowed_seasons]


async def fetch_all_data(
    client:    httpx.AsyncClient,
    sem:       asyncio.Semaphore,
    day_start: datetime,
    cutoff:    datetime,
    *,
    wt_ids: set[int] | None = None,
    agrofirms: dict[str, tuple[int, float, int]] | None = None,
    min_detail_area: float | None = None,
    track_veg_crops: bool = False,
    task_season_years: list[int] | None = None,
) -> dict[str, dict]:
    """
    Логика данных как в 6.mainer.py:
    - задачи за смену и за сезон по work_type_id;
    - привязка к фирме только через mtfmi field -> folder;
    - деталь за смену и средняя по сезону.
    """
    wt_ids = set(wt_ids or SPRAY_WT_IDS)
    agrofirms = agrofirms or AGROFIRMS
    min_area = MIN_DETAIL_AREA_HA if min_detail_area is None else min_detail_area

    print("  Загружаем machine_tasks за смену/сезон (batch по work_type_id)...")

    season_start_dt = datetime(
        SEASON_START.year, SEASON_START.month, SEASON_START.day, tzinfo=TZ_MSK
    )
    tasks_today, tasks_season = await fetch_tasks_day_and_season(
        client, sem, BASE_URL, wt_ids, day_start, cutoff, season_start_dt,
        log_prefix="[r6] ",
    )
    allowed_seasons = _resolve_task_season_years(
        task_season_years, track_veg_crops=track_veg_crops,
    )
    if allowed_seasons:
        tasks_today = _filter_tasks_by_season(tasks_today, allowed_seasons)
        tasks_season = _filter_tasks_by_season(tasks_season, allowed_seasons)
        print(f"    Фильтр machine_task.season ∈ {sorted(allowed_seasons)}")

    print(f"    Заданий за смену: {len(tasks_today)}")
    print(f"    Заданий за сезон: {len(tasks_season)}")

    tasks_today_with_area = [t for t in tasks_today if (t.get("covered_area") or 0) > 0]
    all_with_area = {t["id"] for t in tasks_today_with_area}
    all_with_area.update({t["id"] for t in tasks_season if (t.get("covered_area") or 0) > 0})
    all_with_area_ids = sorted(all_with_area)
    print(
        f"  Загружаем mtfmi (единый проход): {len(all_with_area_ids)} "
        f"(смена: {len(tasks_today_with_area)}, сезон: {len(tasks_season)})..."
    )
    mtfmi_map = await fetch_mtfmi_map_hybrid_async(
        client, sem, BASE_URL, all_with_area_ids, log_prefix="[r6] ",
    )

    from report_resources_fetch import (
        collect_report_resource_ids,
        load_fields_list_async,
        load_implements_dict_async,
        load_machines_map_async,
    )

    global machines_map, field_to_fg
    all_tasks_for_res = tasks_today + tasks_season
    field_ids, machine_ids, implement_ids = collect_report_resource_ids(
        all_tasks_for_res, mtfmi_map,
    )
    print(
        f"  Ресурсы отчёта: полей={len(field_ids)}, "
        f"машин={len(machine_ids)}, орудий={len(implement_ids)}"
    )
    fields_all, machines_map, implements_dict = await asyncio.gather(
        load_fields_list_async(client, BASE_URL, field_ids, sem),
        load_machines_map_async(client, BASE_URL, machine_ids, sem),
        load_implements_dict_async(client, BASE_URL, implement_ids, sem),
    )
    field_to_fg = {
        int(f["id"]): f.get("field_group_id")
        for f in fields_all if f.get("id") is not None
    }

    task_to_firms_day: dict[int, dict[str, float]] = {}
    firm_area_day: dict[str, float] = defaultdict(float)
    for t in tasks_today_with_area:
        tid = t["id"]
        firm_cov = _task_cov_by_firm(
            t, mtfmi_map.get(tid, []), cutoff=cutoff, window_start=day_start,
        )
        task_to_firms_day[tid] = firm_cov
        for firm, cov in firm_cov.items():
            if firm in agrofirms:
                firm_area_day[firm] += cov

    per_firm_machine: dict[str, dict[int, dict]] = {firm: {} for firm in agrofirms}
    veg_fact_by_firm_crop: dict[str, dict[str, float]] = {f: defaultdict(float) for f in agrofirms}
    veg_today_by_firm_crop: dict[str, dict[str, float]] = {f: defaultdict(float) for f in agrofirms}
    spray_today_by_firm_crop: dict[str, dict[str, float]] = {f: defaultdict(float) for f in agrofirms}
    # Диагностика: поля, которые вошли в cov_total фирмы, но культура которых
    # не распозналась ни под одну из VEGGIE_CROPS (карто/лук/морков/свек) —
    # их площадь иначе молча выпадала бы из таблицы "Опрыскивание (овощи)".
    veg_unmatched_today: dict[str, list[tuple[int, str, float]]] = {f: [] for f in agrofirms}
    _cap = None
    _fact_group = "3.2 Опрыскивание (овощи)" if track_veg_crops else "3.1 Опрыскивание"
    try:
        from proverka.task_facts import bind_capture

        _cap = bind_capture()
    except Exception:
        _cap = None
    for t in tasks_today_with_area:
        tid = t["id"]
        mid = t["machine_id"]
        firm_cov_day = task_to_firms_day.get(tid, {})
        if not firm_cov_day:
            continue

        work_dur = t.get("work_duration") or 0
        road_dur = t.get("movements_on_road_duration") or 0
        work_net = max(work_dur - road_dur, 0)
        sp = t.get("average_speed_on_field")
        firms = set(firm_cov_day.keys())
        # Площадь на фирму — task.covered_area, распределённая по MTFMI (task_area_shared).
        # Время делим пропорционально доле площади фирмы в задании.
        task_raw_total = sum(firm_cov_day.values()) or 1.0

        for firm in firms:
            cov_for_firm = firm_cov_day[firm]
            dur_for_firm = work_net * (cov_for_firm / task_raw_total)

            pm = per_firm_machine[firm]
            if mid not in pm:
                pm[mid] = {
                    "cov_total": 0.0, "work_dur": 0.0,
                    "track_area": 0.0, "work_distance_km": 0.0,
                    "implement_width": 0.0, "speeds": [],
                }
            pm[mid]["cov_total"] += cov_for_firm
            pm[mid]["work_dur"] += dur_for_firm
            if _cap is not None and cov_for_firm > 0:
                try:
                    crops: list[str] = []
                    for _item in mtfmi_map.get(tid, []):
                        _fid = _item.get("field_id")
                        if not _fid:
                            continue
                        _fid_i = int(_fid)
                        if get_firm_for_field(_fid_i) != firm or not _field_ok(_fid_i):
                            continue
                        from mtfmi_batch_fetch import crop_of_item

                        _crop = crop_of_item(_item, str(field_to_crop.get(_fid_i) or ""))
                        if _crop and _crop not in crops:
                            crops.append(_crop)
                    _mrec = machines_map.get(mid) or {}
                    _cap[1](
                        (_fact_group, firm, mid),
                        group=_fact_group,
                        firm=firm,
                        crop=crops[0] if len(crops) == 1 else "",
                        task_id=int(tid),
                        machine_id=int(mid) if mid is not None else None,
                        machine=str(_mrec.get("name") or ""),
                        ha=cov_for_firm,
                        season=str(t.get("season") or ""),
                    )
                except Exception:
                    pass

            # Сырая площадь по культурам внутри задания+фирмы; при совпадении
            # с cov_for_firm масштаб ≈ 1 (площадь уже без подгонки к task.covered_area).
            raw_crop_cov: dict[str, float] = defaultdict(float)
            raw_unmatched_cov = 0.0
            for item in mtfmi_map.get(tid, []):
                fid = item.get("field_id")
                if not fid:
                    continue
                fid_i = int(fid)
                if get_firm_for_field(fid_i) != firm or not _field_ok(fid_i):
                    continue
                pm[mid]["track_area"] += item.get("covered_area_by_track") or 0
                pm[mid]["work_distance_km"] += (item.get("work_distance") or 0) / 1000
                if track_veg_crops:
                    item_cov = _item_area_in_window(item, cutoff, day_start)
                    if item_cov > 0:
                        veg_label = _field_veggie_crop_label(fid_i, t, item)
                        if veg_label:
                            raw_crop_cov[veg_label] += item_cov
                        else:
                            raw_unmatched_cov += item_cov
                            from mtfmi_batch_fetch import crop_of_item

                            veg_unmatched_today[firm].append(
                                (fid_i, crop_of_item(item, str(field_to_crop.get(fid_i, ""))), round(item_cov, 2))
                            )
                else:
                    from mtfmi_batch_fetch import crop_of_item

                    item_cov = _item_area_in_window(item, cutoff, day_start)
                    crop = crop_of_item(item, str(field_to_crop.get(fid_i) or ""))
                    if crop and item_cov > 0:
                        raw_crop_cov[crop] += item_cov

            if track_veg_crops:
                raw_total = sum(raw_crop_cov.values()) + raw_unmatched_cov
                if raw_total > 0 and cov_for_firm > 0:
                    scale = cov_for_firm / raw_total
                    for veg_label, raw_cov in raw_crop_cov.items():
                        veg_today_by_firm_crop[firm][veg_label] += raw_cov * scale
            else:
                raw_total = sum(raw_crop_cov.values())
                if raw_total > 0 and cov_for_firm > 0:
                    scale = cov_for_firm / raw_total
                    for crop, raw_cov in raw_crop_cov.items():
                        spray_today_by_firm_crop[firm][crop] += raw_cov * scale

            if not pm[mid]["implement_width"]:
                pm[mid]["implement_width"] = _task_implement_width(t, implements_dict)
            if sp and sp > 0:
                pm[mid]["speeds"].append(sp)

    season_missing_ids = [
        t["id"] for t in tasks_season
        if t["id"] not in mtfmi_map and (t.get("covered_area") or 0) > 0
    ]
    if season_missing_ids:
        print(f"  Догружаем mtfmi для {len(season_missing_ids)} сезонных заданий...")
        extra_map = await fetch_mtfmi_map_hybrid_async(
            client, sem, BASE_URL, season_missing_ids, log_prefix="[r6] ",
        )
        mtfmi_map.update(extra_map)

    season_area_by_firm_date:     dict[str, dict[date, float]]     = {f: defaultdict(float) for f in agrofirms}
    season_machines_by_firm_date: dict[str, dict[date, set[int]]]  = {f: defaultdict(set)   for f in agrofirms}
    veg_unmatched_season: dict[str, list[tuple[int, str, float]]] = {f: [] for f in agrofirms}

    for t in tasks_season:
        cov = float(t.get("covered_area") or 0)
        if cov <= 0.5:
            continue
        mid = t.get("machine_id")
        end_dt_raw = t.get("end_time", "")
        end_dt = None
        if end_dt_raw:
            try:
                s_str = str(end_dt_raw).strip()
                if s_str.endswith("Z"):
                    s_str = s_str[:-1] + "+00:00"
                _dt = datetime.fromisoformat(s_str)
                if _dt.tzinfo is None:
                    _dt = _dt.replace(tzinfo=TZ_MSK)
                else:
                    _dt = _dt.astimezone(TZ_MSK)
                end_dt = _dt
            except (ValueError, AttributeError):
                pass
        if end_dt is None:
            continue
        anchor = datetime(
            end_dt.year, end_dt.month, end_dt.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK
        )
        if end_dt < anchor:
            shift_date   = (end_dt - timedelta(days=1)).date()
            shift_start  = anchor - timedelta(days=1)
            shift_cutoff = anchor
        else:
            shift_date   = end_dt.date()
            shift_start  = anchor
            shift_cutoff = anchor + timedelta(days=1)
        upper: date = cutoff.date() - timedelta(days=1)
        if shift_date > upper:
            continue
        items = mtfmi_map.get(t["id"], [])
        # Распределяем сезонную площадь пропорционально полям каждой агрофирмы
        firm_cov_season = _task_cov_by_firm(t, items, cutoff=shift_cutoff, window_start=shift_start)
        for firm, firm_area in firm_cov_season.items():
            season_area_by_firm_date[firm][shift_date] += firm_area
            if mid is not None:
                season_machines_by_firm_date[firm][shift_date].add(int(mid))
        if track_veg_crops:
            raw_crop_cov_season: dict[tuple[str, str], float] = defaultdict(float)
            raw_unmatched_cov_season: dict[str, float] = defaultdict(float)
            for item in items:
                fid = item.get("field_id")
                if not fid:
                    continue
                fid_i = int(fid)
                firm = get_firm_for_field(fid_i)
                if firm not in agrofirms:
                    continue
                item_cov = _item_area_in_window(item, shift_cutoff, shift_start)
                if item_cov <= 0.5:
                    continue
                veg_label = _field_veggie_crop_label(fid_i, t, item)
                if veg_label:
                    raw_crop_cov_season[(firm, veg_label)] += item_cov
                else:
                    raw_unmatched_cov_season[firm] += item_cov
                    from mtfmi_batch_fetch import crop_of_item

                    veg_unmatched_season[firm].append(
                        (fid_i, crop_of_item(item, str(field_to_crop.get(fid_i, ""))), round(item_cov, 2))
                    )

            # Группируем сырые суммы по фирме (включая неопознанную площадь
            # в знаменатель — как в суточном блоке) и масштабируем под
            # firm_cov_season[firm], чтобы итог по культурам совпадал
            # с фактически учтённой площадью фирмы.
            by_firm_raw: dict[str, dict[str, float]] = defaultdict(dict)
            for (firm, veg_label), raw_cov in raw_crop_cov_season.items():
                by_firm_raw[firm][veg_label] = raw_cov
            all_firms_with_raw = set(by_firm_raw.keys()) | set(raw_unmatched_cov_season.keys())
            for firm in all_firms_with_raw:
                crop_map = by_firm_raw.get(firm, {})
                firm_total_target = firm_cov_season.get(firm, 0.0)
                raw_firm_total = sum(crop_map.values()) + raw_unmatched_cov_season.get(firm, 0.0)
                if raw_firm_total > 0 and firm_total_target > 0:
                    scale = firm_total_target / raw_firm_total
                    for veg_label, raw_cov in crop_map.items():
                        veg_fact_by_firm_crop[firm][veg_label] += raw_cov * scale

    all_stats: dict[str, dict] = {}
    for firm in agrofirms:
        pm = per_firm_machine[firm]
        pm_filtered = {
            mid: d for mid, d in pm.items()
            if round(d["cov_total"], 2) >= min_area
        }
        count_threshold = max(min_area, MIN_COUNT_AREA_HA)
        pm_count = {
            mid: d for mid, d in pm_filtered.items()
            if round(d["cov_total"], 2) >= count_threshold
        }
        detail_own: list[dict] = []
        detail_hired: list[dict] = []

        for mid, d in pm_count.items():
            m = machines_map.get(mid, {})
            group = m.get("machine_group_id")
            cov = round(d["cov_total"], 2)
            secs = d["work_dur"]
            rate = round(cov / (secs / 3600), 1) if secs else 0.0
            speed = round(sum(d["speeds"]) / len(d["speeds"]) * 3.6, 1) if d["speeds"] else 0.0
            w_real = d["implement_width"]
            l_pole = d["work_distance_km"]
            s_track = d["track_area"]
            coef = int(round(s_track / l_pole / w_real * 10 * 100)) if w_real and l_pole else 0

            row_data = {
                "Машина": m.get("name", f"machine_{mid}"),
                "Площадь, га": cov,
                "Время (сек)": secs,
                "Время на полях": seconds_to_hms(secs),
                "Ср. выр-ка га/час": rate,
                "Средняя скорость на полях км/ч": speed,
                "Коэффициент использования конструкционной ширины захвата, %": coef,
            }
            if group == HIRED_GROUP_ID:
                detail_hired.append(row_data)
            else:
                detail_own.append(row_data)

        detail_own.sort(key=lambda x: -x["Площадь, га"])
        detail_hired.sort(key=lambda x: -x["Площадь, га"])

        # Площадь за сутки — covered_area заданий (как Cropwise), без порога на машину.
        # Счётчики, время и детали — только техника >= 10 га.
        area_today = round(
            sum(
                d["cov_total"] for d in pm.values()
                if round(d["cov_total"], 2) >= min_area
            ),
            2,
        )
        engine_today = round(sum(d["work_dur"] for d in pm_count.values()) / 3600, 1)

        area_fd  = season_area_by_firm_date[firm]
        mach_fd  = season_machines_by_firm_date[firm]
        upper_bound: date = cutoff.date() - timedelta(days=1)
        stats_dates = [
            dt for dt, v in area_fd.items()
            if v > 0 and SEASON_START <= dt <= upper_bound
        ]
        season_sum   = sum(area_fd.get(dt, 0.0) for dt in stats_dates)
        machine_days = sum(
            len(mach_fd[dt])
            for dt in stats_dates
            if len(mach_fd[dt]) > 0
        )
        avg_season = round(season_sum / machine_days, 1) if machine_days else 0.0

        all_stats[firm] = {
            "machines_today":     len(pm_count),
            "area_today":         area_today,
            "engine_today":       engine_today,
            "avg_season":         avg_season,
            "season_sum":         season_sum,
            "season_machine_days": machine_days,
            "detail_own":         detail_own,
            "detail_hired":       detail_hired,
        }
        if track_veg_crops:
            all_stats[firm]["veg_fact_by_crop"] = {
                k: round(v, 2) for k, v in veg_fact_by_firm_crop[firm].items()
            }
            all_stats[firm]["veg_today_by_crop"] = {
                k: round(v, 2) for k, v in veg_today_by_firm_crop[firm].items()
            }
        else:
            all_stats[firm]["today_by_crop"] = {
                k: round(v, 2)
                for k, v in spray_today_by_firm_crop[firm].items()
                if round(v, 2) > 0
            }
            unmatched_today = veg_unmatched_today.get(firm, [])
            unmatched_season = veg_unmatched_season.get(firm, [])
            if unmatched_today or unmatched_season:
                sum_today = round(sum(c for _, _, c in unmatched_today), 2)
                sum_season = round(sum(c for _, _, c in unmatched_season), 2)
                print(
                    f"  [ОВОЩИ/{firm}] Поля с площадью, но без распознанной "
                    f"культуры (карто/лук/морков/свек): "
                    f"за сутки {sum_today} га ({len(unmatched_today)} записей), "
                    f"за сезон {sum_season} га ({len(unmatched_season)} записей)."
                )
                sample_today = sorted(set(unmatched_today), key=lambda x: -x[2])[:10]
                for fid_i, crop_name, area in sample_today:
                    print(f"      field_id={fid_i}  culture='{crop_name}'  area={area} га (сутки)")

    if _cap is not None:
        ok_pairs = {
            (firm, mid)
            for firm, pm in per_firm_machine.items()
            for mid, d in pm.items()
            if round(d["cov_total"], 2) >= min_area
        }
        _cap[2](_fact_group, ok_pairs)

    return all_stats




# =============================================================================
# СТИЛИ И КОЛОНКИ
# =============================================================================

DETAIL_COLS = [
    "№", "Машина", "Площадь, га", "Время на полях",
    "Ср. выр-ка га/час", "Средняя скорость на полях км/ч",
    "Коэффициент использования конструкционной ширины захвата, %",
]
DETAIL_COL_WIDTHS = [9, 48, 22, 22, 22, 22, 22]

# Разрывы страниц по высоте (как в сводке 3). Тексты шапки страницы дублирует PDF-постобработка
# (логотип + фирма + «ВИД РАБОТ…»), поэтому в xlsx строку «ВИД РАБОТ» не пишем.
DETAIL_PAGE_BODY_HEIGHT_PT = 505.0
DETAIL_SECTION_TITLE_HEIGHT_PT = 26.0
DETAIL_HEADER_HEIGHT_PT = 52.0
DETAIL_DATA_ROW_HEIGHT_PT = 24.0
DETAIL_DATA_ROW_MAX_HEIGHT_PT = 36.0
DETAIL_TOTAL_ROW_HEIGHT_PT = 24.0
DETAIL_SECTION_GAP_HEIGHT_PT = 12.0
DETAIL_ATOMIC_SECTION_EXTRA_BUFFER_PT = 80.0
DETAIL_SINGLE_PAGE_SLACK_PT = 32.0
DETAIL_PAGE_SAFE_RESERVE_PT = 36.0
DETAIL_WRAP_LINE_HEIGHT_PT = 14.0
DETAIL_PAGE_TITLE_ROW_HEIGHT_PT = 22.0

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
        "align_c":       Alignment(horizontal="center", vertical="center",
                                    wrap_text=True),
        "align_l":       Alignment(horizontal="left", vertical="center", wrap_text=True),
        "fill_hdr":      PatternFill("solid", fgColor="8093B3"),
        # По требованию: обычные строки без заголовков/итого должны быть белыми.
        "fill_own":      PatternFill("solid", fgColor="FFFFFF"),
        "fill_hired":    PatternFill("solid", fgColor="FFFFFF"),
        "fill_total":    PatternFill("solid", fgColor="BDD7EE"),
        # Вся "обычная" часть таблицы должна быть белой.
        "fill_sec_own":  PatternFill("solid", fgColor="FFFFFF"),
        "fill_sec_hire": PatternFill("solid", fgColor="FFFFFF"),
        "font_hdr":      Font(bold=True, color="FFFFFF"),
        "font_total":    Font(bold=True),
        "font_title":    Font(bold=True, size=13),
        "font_sec":      Font(bold=True, size=12, color="111827"),
        "fill_alert_prod": pattern_fill_low_output_vs_plan(),
    }


def _align_summary_data(ci: int, col_key: str, raw_val: object, s: dict) -> Alignment:
    """Без переноса для «Итого», иначе wrap даёт «итог»/«о»."""
    if str(raw_val).strip() == "Итого" and col_key == "Агрофирма":
        return Alignment(vertical="center", horizontal="left", wrap_text=False)
    if ci == 2:
        return s["align_l"]
    return s["align_c"]


def _font_detail_hdr(ci: int) -> Font:
    if ci >= 5:
        return Font(bold=True, color="FFFFFF", size=9)
    return Font(bold=True, color="FFFFFF", size=10)


def _font_summary_hdr(ci: int) -> Font:
    if ci <= 2:
        return Font(bold=True, color="FFFFFF", size=10)
    return Font(bold=True, color="FFFFFF", size=9)


def _sc(cell, s, fill=None, font=None, align=None) -> None:
    cell.border = s["border"]
    if fill:  cell.fill      = fill
    if font:  cell.font      = font
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


def _estimate_wrapped_lines(value: object, col_width: float | None) -> int:
    text = str(value or "").strip()
    if not text:
        return 1
    width = float(col_width or 10)
    chars_per_line = max(1, int(width * 0.65))
    parts = text.splitlines() or [text]
    lines = 0
    for part in parts:
        p = part.strip()
        if not p:
            lines += 1
            continue
        lines += max(1, math.ceil(len(p) / chars_per_line))
    return max(1, lines)


def _estimate_row_height_by_values(
    values: list[object],
    col_widths: list[float],
    min_height: float,
    line_height: float = DETAIL_WRAP_LINE_HEIGHT_PT,
    max_height: float = DETAIL_DATA_ROW_MAX_HEIGHT_PT,
) -> float:
    max_lines = 1
    for idx, value in enumerate(values):
        if value in (None, ""):
            continue
        width = col_widths[idx] if idx < len(col_widths) else 10.0
        max_lines = max(max_lines, _estimate_wrapped_lines(value, width))
    return min(max_height, max(min_height, max_lines * line_height + 4))


def _detail_data_row_height(values: list[object]) -> float:
    return _estimate_row_height_by_values(
        values,
        DETAIL_COL_WIDTHS,
        min_height=DETAIL_DATA_ROW_HEIGHT_PT,
        line_height=DETAIL_WRAP_LINE_HEIGHT_PT,
        max_height=DETAIL_DATA_ROW_MAX_HEIGHT_PT,
    )


# =============================================================================
# ЗАПИСЬ ЛИСТА ДЕТАЛИЗАЦИИ
# =============================================================================

def write_detail_sheet(
    wb:           openpyxl.Workbook,
    firm_name:    str,
    report_date:  date,
    day_start:    datetime,
    cutoff:       datetime,
    detail_own:   list[dict],
    detail_hired: list[dict],
    *,
    sheet_title: str | None = None,
) -> None:
    if not detail_own and not detail_hired:
        return

    from firm_sheet_names import firm_sheet_tab_name

    ws = wb.create_sheet(title=(sheet_title or firm_sheet_tab_name(firm_name))[:31])
    s  = _make_styles()
    n  = len(DETAIL_COLS)
    row = 1

    for ci, w in enumerate(DETAIL_COL_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    page_used_pt = 0.0
    _page_body_h = float(globals().get("DETAIL_PAGE_BODY_HEIGHT_PT", 465.0))
    _page_safe_r = float(globals().get("DETAIL_PAGE_SAFE_RESERVE_PT", 60.0))

    def _write_machines_min_area_note() -> None:
        nonlocal row, page_used_pt
        _ensure_page_space(DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT)
        row = write_detail_machines_min_area_note(ws, row, n, border=s["border"])
        page_used_pt += DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT

    def _write_page_titles() -> None:
        _write_machines_min_area_note()

    def _ensure_page_space(required_height_pt: float, *, relaxed: bool = False) -> bool:
        nonlocal row, page_used_pt
        if required_height_pt <= 0:
            return False
        limit = page_limit_relaxed if relaxed else page_limit
        if page_used_pt > 0 and page_used_pt + required_height_pt > limit:
            try:
                ws.row_breaks.append(Break(id=max(1, row - 1)))
            except Exception:
                pass
            page_used_pt = 0.0
            _write_page_titles()
            return True
        return False

    def _write_section_caption_and_header(
        sec_title, sec_fill, *, relaxed_header: bool = False
    ) -> None:
        nonlocal row, page_used_pt
        _ensure_page_space(
            DETAIL_SECTION_TITLE_HEIGHT_PT + DETAIL_HEADER_HEIGHT_PT,
            relaxed=relaxed_header,
        )
        sec_row = row
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n)
        c = ws.cell(row=row, column=1, value=sec_title)
        c.font      = s["font_sec"]
        c.fill      = sec_fill
        c.alignment = s["align_c"]
        c.border    = s["border"]
        ws.row_dimensions[sec_row].height = DETAIL_SECTION_TITLE_HEIGHT_PT
        row += 1

        hdr_row = row
        for ci, col in enumerate(DETAIL_COLS, start=1):
            c = ws.cell(row=row, column=ci, value=col)
            _sc(c, s, fill=s["fill_hdr"],
                font=_font_detail_hdr(ci), align=s["align_c"])
        ws.row_dimensions[hdr_row].height = DETAIL_HEADER_HEIGHT_PT
        row += 1
        page_used_pt += DETAIL_SECTION_TITLE_HEIGHT_PT + DETAIL_HEADER_HEIGHT_PT

    def _add_section_gap() -> None:
        nonlocal row, page_used_pt
        if firm_single_page:
            ws.row_dimensions[row].height = DETAIL_SECTION_GAP_HEIGHT_PT
            row += 1
            page_used_pt += DETAIL_SECTION_GAP_HEIGHT_PT
            return
        if _ensure_page_space(DETAIL_SECTION_GAP_HEIGHT_PT):
            return
        ws.row_dimensions[row].height = DETAIL_SECTION_GAP_HEIGHT_PT
        row += 1
        page_used_pt += DETAIL_SECTION_GAP_HEIGHT_PT

    def _estimate_section_block_height(rows: list[dict]) -> float:
        if not rows:
            return 0.0
        block_h = DETAIL_SECTION_TITLE_HEIGHT_PT + DETAIL_HEADER_HEIGHT_PT
        for idx, rd in enumerate(rows, start=1):
            vals = [
                idx, rd["Машина"], rd["Площадь, га"],
                rd["Время на полях"], rd["Ср. выр-ка га/час"],
                rd["Средняя скорость на полях км/ч"],
                rd[
                    "Коэффициент использования "
                    "конструкционной ширины захвата, %"
                ],
            ]
            block_h += _detail_data_row_height(vals)
        block_h += DETAIL_TOTAL_ROW_HEIGHT_PT
        return block_h

    page_limit = max(1.0, _page_body_h - _page_safe_r)
    page_limit_relaxed = page_limit + DETAIL_SINGLE_PAGE_SLACK_PT

    def _firm_sheet_block_height() -> float:
        block_h = DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT
        if detail_own:
            block_h += _estimate_section_block_height(detail_own)
        if detail_own and detail_hired:
            block_h += DETAIL_SECTION_GAP_HEIGHT_PT
        if detail_hired:
            block_h += _estimate_section_block_height(detail_hired)
        return block_h

    firm_block_h = _firm_sheet_block_height()
    firm_single_page = firm_block_h <= page_limit_relaxed
    same_firm_both_sections = bool(detail_own and detail_hired)

    def write_section(
        sec_title,
        rows,
        row_fill,
        sec_fill,
        *,
        continue_on_page: bool = False,
    ) -> None:
        nonlocal row, page_used_pt
        if not rows:
            return

        atomic_section = len(rows) <= 1
        continuing_same_page = page_used_pt > 0
        relaxed_header = continue_on_page or (
            same_firm_both_sections and continuing_same_page
        )

        def _detail_vals(idx: int, rd: dict) -> list[object]:
            return [
                idx, rd["Машина"], rd["Площадь, га"],
                rd["Время на полях"], rd["Ср. выр-ка га/час"],
                rd["Средняя скорость на полях км/ч"],
                rd[
                    "Коэффициент использования "
                    "конструкционной ширины захвата, %"
                ],
            ]

        section_block_h = _estimate_section_block_height(rows)
        single_page_section = (
            firm_single_page
            or (
                not continuing_same_page
                and len(rows) > 1
                and section_block_h <= page_limit_relaxed
                and (page_used_pt + section_block_h) <= page_limit_relaxed
            )
        )

        def _min_section_start_height() -> float:
            need = DETAIL_SECTION_TITLE_HEIGHT_PT + DETAIL_HEADER_HEIGHT_PT
            need += _detail_data_row_height(_detail_vals(1, rows[0]))
            if len(rows) == 1:
                need += DETAIL_TOTAL_ROW_HEIGHT_PT
            return need

        if atomic_section and len(rows) == 1:
            one_vals = _detail_vals(1, rows[0])
            one_h = _detail_data_row_height(one_vals)
            one_h = max(one_h, DETAIL_DATA_ROW_HEIGHT_PT)
            atomic_buffer = (
                0.0
                if (firm_single_page or continuing_same_page or continue_on_page)
                else DETAIL_ATOMIC_SECTION_EXTRA_BUFFER_PT
            )
            full_section_need = (
                DETAIL_SECTION_TITLE_HEIGHT_PT
                + DETAIL_HEADER_HEIGHT_PT
                + one_h
                + DETAIL_TOTAL_ROW_HEIGHT_PT
                + atomic_buffer
            )
            if not (firm_single_page or continue_on_page):
                _ensure_page_space(full_section_need, relaxed=relaxed_header)
        elif len(rows) > 1:
            if single_page_section:
                if not (firm_single_page or continue_on_page):
                    _ensure_page_space(section_block_h, relaxed=relaxed_header)
            elif not continue_on_page:
                first_vals = _detail_vals(1, rows[0])
                first_h = _detail_data_row_height(first_vals)
                _ensure_page_space(
                    DETAIL_SECTION_TITLE_HEIGHT_PT + DETAIL_HEADER_HEIGHT_PT + first_h,
                    relaxed=relaxed_header,
                )

        if page_used_pt > 0:
            # Не оставляем «сироту»: заголовок секции только вместе с ≥1 строкой данных.
            _ensure_page_space(_min_section_start_height(), relaxed=False)

        _write_section_caption_and_header(
            sec_title, sec_fill, relaxed_header=False
        )

        use_row_pagination = len(rows) > 1 and not single_page_section

        for idx, rd in enumerate(rows, start=1):
            vals = _detail_vals(idx, rd)
            data_row_height = _detail_data_row_height(vals)
            rows_left = len(rows) - idx

            if use_row_pagination and page_used_pt > 0:
                next_need = data_row_height
                if rows_left == 0:
                    next_need += DETAIL_TOTAL_ROW_HEIGHT_PT
                need_new_page = page_used_pt + next_need > page_limit
                if need_new_page:
                    try:
                        ws.row_breaks.append(Break(id=max(1, row - 1)))
                    except Exception:
                        pass
                    page_used_pt = 0.0
                    _write_page_titles()
                    _write_section_caption_and_header(
                        sec_title, sec_fill, relaxed_header=relaxed_header
                    )

            for ci, val in enumerate(vals, start=1):
                if ci == 3:
                    val = _fmt_grouped_2(val)
                c = ws.cell(row=row, column=ci, value=val)
                _sc(
                    c,
                    s,
                    fill=row_fill,
                    align=s["align_l"] if ci == 2 else s["align_c"],
                )
            ws.row_dimensions[row].height = data_row_height
            row += 1
            page_used_pt += data_row_height

        tc  = round(sum(rd["Площадь, га"] for rd in rows), 2)
        ts  = sum(rd["Время (сек)"] for rd in rows)
        tr  = round(tc / (ts / 3600), 1) if ts else 0.0
        asp = round(
            sum(rd["Средняя скорость на полях км/ч"] for rd in rows) / len(rows), 1
        )
        acf = round(
            sum(
                rd[
                    "Коэффициент использования "
                    "конструкционной ширины захвата, %"
                ]
                for rd in rows
            )
            / len(rows)
        )
        if not atomic_section and not (firm_single_page or continue_on_page):
            _ensure_page_space(DETAIL_TOTAL_ROW_HEIGHT_PT, relaxed=relaxed_header)
        for ci, val in enumerate(
            ["Итого", "", tc, seconds_to_hms(ts), tr, asp, acf], start=1
        ):
            if ci == 3:
                val = _fmt_grouped_2(val)
            c = ws.cell(row=row, column=ci, value=val)
            itogo_nowrap = ci == 1 and val == "Итого"
            align = (
                Alignment(horizontal="center", vertical="center", wrap_text=False)
                if itogo_nowrap
                else s["align_c"]
            )
            _sc(c, s, fill=s["fill_total"], font=s["font_total"], align=align)
        ws.row_dimensions[row].height = DETAIL_TOTAL_ROW_HEIGHT_PT
        row += 1
        page_used_pt += DETAIL_TOTAL_ROW_HEIGHT_PT

    _firm = _firm_table_label(firm_name)

    _write_machines_min_area_note()

    if detail_own:
        write_section(
            f"СОБСТВЕННЫЕ — {_firm}",
            detail_own,
            s["fill_own"],
            s["fill_sec_own"],
        )

    if detail_hired:
        if detail_own:
            # Требование: если «СОБСТВЕННЫЕ» и «НАЁМНЫЕ» одной фирмы, то на одной странице
            # остаётся максимум возможного объёма; переносим продолжение только когда
            # не хватает места (то есть НЕ уводим весь блок «НАЁМНЫЕ» на новую страницу,
            # даже если он целиком помещается на следующей).
            _add_section_gap()
        write_section(
            f"НАЁМНЫЕ — {_firm}",
            detail_hired,
            s["fill_hired"],
            s["fill_sec_hire"],
            continue_on_page=True,
        )


# =============================================================================
# СВОДНЫЙ ЛИСТ
# =============================================================================

def _build_summary_rows(
    firm_names:  list[str],
    all_details: dict[str, dict],
    manual:      dict | None = None,
    agrofirms:   dict[str, tuple[int, float, int]] | None = None,
) -> list[dict]:
    merged    = manual if manual is not None else _build_manual_dict()
    plan_map  = merged.get("plan_rate_by_firm") or {}
    avail_map = merged.get("available_self_propelled_by_firm") or {}
    firms_cfg = agrofirms or AGROFIRMS

    rows = []
    for i, firm_name in enumerate(firm_names, start=1):
        st         = all_details[firm_name]
        n_mach     = st["machines_today"]
        area       = st["area_today"]
        eng        = st["engine_today"]
        avg_s      = float(st.get("avg_season", 0.0) or 0.0)
        _, plan_d, own_default = firms_cfg[firm_name]
        if firms_cfg is not AGROFIRMS:
            plan_rate = float(plan_d)
            own_count = int(own_default)
        else:
            plan_rate = float(plan_map.get(firm_name, plan_d))
            own_count = int(avail_map.get(firm_name, own_default))
        rows.append({
            "№ п/п":                                                       i,
            "Агрофирма":                                                   _firm_table_label(firm_name),
            "Плановая выработка, га/сутки":                                plan_rate,
            "Количество задействованной техники":                           n_mach,
            "Общий объем выполненной работы за сутки, га":                 area,
            "Выработка за сутки/1 ед агрегата, га":
                round(area / n_mach, 2) if n_mach else 0.0,
            "Средняя выработка по сезону, га/сутки":                       avg_s,
            "Общее время работы на полях, машино-часы за сутки":           eng,
            "Среднее время эффективной работы на поле на 1 агрегат., час.":
                round(eng / n_mach, 1) if n_mach else 0.0,
            "Часовая выработка в расчёте на 1 агрегат, га":
                round(area / eng, 1)   if eng    else 0.0,
            "Всего собств. агрегатов в наличии":                           own_count,
        })

    tm   = sum(r["Количество задействованной техники"]            for r in rows)
    ta   = sum(r["Общий объем выполненной работы за сутки, га"]   for r in rows)
    te   = sum(r["Общее время работы на полях, машино-часы за сутки"] for r in rows)
    to   = sum(r["Всего собств. агрегатов в наличии"]             for r in rows)

    # "Итого" = sum(площадь за сезон) / sum(машино-дней) по всем фирмам — как в эталоне
    total_season_sum = sum(
        float(all_details[firm_name].get("season_sum", 0.0) or 0.0) for firm_name in firm_names
    )
    total_machine_days = sum(
        int(all_details[firm_name].get("season_machine_days", 0) or 0) for firm_name in firm_names
    )
    avg_season_total = (
        round(total_season_sum / total_machine_days, 1) if total_machine_days else 0.0
    )

    rows.append({
        "№ п/п":                                                       "",
        "Агрофирма":                                                   "Итого",
        "Плановая выработка, га/сутки":
            rows[0]["Плановая выработка, га/сутки"] if rows else 0,
        "Количество задействованной техники":                           tm,
        "Общий объем выполненной работы за сутки, га":                 round(ta, 2),
        "Выработка за сутки/1 ед агрегата, га":
            round(ta / tm, 2) if tm else 0.0,
        "Средняя выработка по сезону, га/сутки":                       avg_season_total,
        "Общее время работы на полях, машино-часы за сутки":           round(te, 1),
        "Среднее время эффективной работы на поле на 1 агрегат., час.":
            round(te / tm, 1) if tm else 0.0,
        "Часовая выработка в расчёте на 1 агрегат, га":
            round(ta / te, 1) if te else 0.0,
        "Всего собств. агрегатов в наличии":                           to,
    })
    return rows


def _style_cell(c, s: dict, *, fill=None, font=None, align=None) -> None:
    _sc(c, s, fill=fill, font=font, align=align)


def _write_summary_block(
    ws,
    start_row: int,
    title: str,
    rows: list[dict],
    s: dict,
) -> int:
    n = len(SUMMARY_COLS)
    ws.merge_cells(start_row=start_row, start_column=1, end_row=start_row, end_column=n)
    c = ws.cell(row=start_row, column=1, value=title)
    c.font = s["font_sec"]
    c.alignment = s["align_c"]
    c.border = s["border"]
    ws.row_dimensions[start_row].height = 28
    start_row += 1

    for ci, col in enumerate(SUMMARY_COLS, start=1):
        c = ws.cell(row=start_row, column=ci, value=col)
        _sc(c, s, fill=s["fill_hdr"], font=_font_summary_hdr(ci), align=s["align_c"])
    ws.row_dimensions[start_row].height = 56
    start_row += 1

    _sk_plan = "Плановая выработка, га/сутки"
    _sk_mach = "Количество задействованной техники"
    _sk_rate = "Выработка за сутки/1 ед агрегата, га"
    for row_data in rows:
        is_total = row_data.get("Агрофирма") == "Итого"
        fill = s["fill_total"] if is_total else s["fill_own"]
        font = s["font_total"] if is_total else None
        for ci, col in enumerate(SUMMARY_COLS, start=1):
            raw = row_data.get(col, "")
            val, nf = summary_cell_value_and_format(col, raw)
            c = ws.cell(row=start_row, column=ci, value=val)
            if nf:
                c.number_format = nf
            cell_fill = summary_rate_below_plan_fill(
                col, row_data, fill, s["fill_alert_prod"],
                plan_key=_sk_plan, machines_key=_sk_mach, rate_key=_sk_rate,
            )
            _sc(c, s, fill=cell_fill, font=font, align=_align_summary_data(ci, col, raw, s))
        lbl = str(row_data.get("Агрофирма") or "")
        ws.row_dimensions[start_row].height = 32 if len(lbl) > 34 else 26
        start_row += 1
    return start_row + 1


def _write_veg_spray_crop_compact_block(
    ws,
    start_row: int,
    firm_name: str,
    firm_stats: dict,
    report_date: date,
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
    plan_map = _veg_spray_plan_ha()
    fact_map = dict(firm_stats.get("veg_fact_by_crop") or {})
    today_map = dict(firm_stats.get("veg_today_by_crop") or {})
    season_start = date(SEASON_START.year, SEASON_START.month, SEASON_START.day)
    crop_labels = {
        "Свекла": "Свекла столовая",
    }

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

    for ci, header in enumerate(headers, start=1):
        c = ws.cell(row=start_row, column=ci, value=header)
        _style_cell(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_c"])
    ws.row_dimensions[start_row].height = 56
    start_row += 1

    days_elapsed = max((report_date - season_start).days, 1)
    for i, crop in enumerate(VEG_CROP_ROWS, start=1):
        crop_label = crop_labels.get(crop, crop)
        plan = float(plan_map.get(crop, plan_map.get(crop_label, 0.0)) or 0.0)
        fact = float(fact_map.get(crop, fact_map.get(crop_label, 0.0)) or 0.0)
        today = float(today_map.get(crop, today_map.get(crop_label, 0.0)) or 0.0)
        rem = max(plan - fact, 0.0)
        pct = (fact / plan) if plan > 0 else 0.0
        avg_daily = fact / days_elapsed if fact > 0 else 0.0
        rate = today if today > 0 else avg_daily
        forecast = ""
        if rem <= 0 and plan > 0:
            forecast = report_date.strftime("%d.%m.%Y")
        elif rate > 0:
            forecast = (report_date + timedelta(days=max(1, math.ceil(rem / rate)))).strftime("%d.%m.%Y")
        vals = [i, crop_label, round(plan, 2), round(fact, 2), round(rem, 2), pct, round(today, 2), forecast]
        for ci, val in enumerate(vals, start=1):
            c = ws.cell(row=start_row, column=ci, value=val)
            _style_cell(
                c, s, fill=s["fill_own"],
                align=s["align_c"] if ci == 1 else (s["align_l"] if ci == 2 else s["align_c"]),
            )
            if ci in (3, 4, 5, 7):
                c.number_format = NF_DEC2
            elif ci == 6:
                c.number_format = "0.0%"
        start_row += 1
    return start_row + 1


def write_summary_sheet(
    wb:          openpyxl.Workbook,
    report_date: date,
    day_start:   datetime,
    cutoff:      datetime,
    firm_names:  list[str],
    all_details: dict[str, dict],
    manual:      dict | None = None,
    veg_firm_names: list[str] | None = None,
    veg_details: dict[str, dict] | None = None,
) -> None:
    ws = wb.create_sheet(title="Сводка", index=0)
    s  = _make_styles()
    n  = len(SUMMARY_COLS)
    row = 1

    seeding_rows = _build_summary_rows(firm_names, all_details, manual)
    row = _write_summary_block(ws, row, "3.1 Опрыскивание", seeding_rows, s)

    if veg_firm_names and veg_details:
        firm_for_compact = veg_firm_names[0]
        row = _write_veg_spray_crop_compact_block(
            ws, row, firm_for_compact, veg_details.get(firm_for_compact, {}), report_date, s,
        )
        veg_rows = _build_summary_rows(
            veg_firm_names, veg_details, manual=None, agrofirms=VEG_AGROFIRMS,
        )
        row = _write_summary_block(ws, row, "Опрыскивание овощных культур", veg_rows, s)

    # Ширины: при блоке овощей — как в отчёте 5 (колонка «Культура» + все колонки сводки).
    if veg_firm_names and veg_details:
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
    else:
        ws.column_dimensions["A"].width = 9
        ws.column_dimensions["B"].width = 38
        for ci in range(3, n + 1):
            ws.column_dimensions[get_column_letter(ci)].width = 22


VEGGIE_CROP_ORDER: list[str] = ["Картофель", "Морковь", "Свекла столовая"]

VEGGIE_SPRAY_COLS = [
    "Опрыскивание овощей",
    "План, га",
    "Факт, га",
    "Остаток га",
    "%",
    "Факт, сутки га",
    "Прогнозное время завершения операции",
]


def write_veggie_spray_sheet(
    wb:          openpyxl.Workbook,
    report_date: date,
    veg_details: dict[str, dict],
) -> None:
    """Отдельная таблица «3.2 Опрыскивание (овощи)» — только АФ Чистопольская."""
    ws = wb.create_sheet(title="Опрыскивание (овощи)")
    s  = _make_styles()

    firm_stats = veg_details.get(VEGGIE_FIRM, {})
    today_map  = dict(firm_stats.get("veg_today_by_crop") or {})
    season_map = dict(firm_stats.get("veg_fact_by_crop") or {})
    veggie_plans = _veg_spray_plan_ha()

    n = len(VEGGIE_SPRAY_COLS)
    season_start = date(SEASON_START.year, SEASON_START.month, SEASON_START.day)
    days_elapsed = max((report_date - season_start).days, 1)

    title_text = f"Опрыскивание овощей {_firm_table_label(VEGGIE_FIRM)}"
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n)
    c_title = ws.cell(row=1, column=1, value=title_text)
    c_title.font = s["font_title"]
    c_title.alignment = s["align_l"]
    _style_cell(c_title, s, fill=s["fill_own"], align=s["align_l"])
    for ci in range(2, n + 1):
        _style_cell(ws.cell(row=1, column=ci), s, fill=s["fill_own"], align=s["align_l"])
    ws.row_dimensions[1].height = 36

    for ci, col in enumerate(VEGGIE_SPRAY_COLS, start=1):
        c = ws.cell(row=2, column=ci, value=col)
        _style_cell(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_c"])
    ws.row_dimensions[2].height = 56

    data_row = 3
    for crop in VEGGIE_CROP_ORDER:
        plan  = float(veggie_plans.get(crop, 0.0) or 0.0)
        if crop == "Свекла столовая":
            plan = float(veggie_plans.get("Свекла", plan) or plan)
        fact  = float(season_map.get(crop, season_map.get("Свекла", 0.0)) or 0.0)
        today = float(today_map.get(crop, today_map.get("Свекла", 0.0)) or 0.0)
        rem   = max(plan - fact, 0.0)
        pct   = (fact / plan) if plan > 0 else 0.0
        avg_daily = fact / days_elapsed if fact > 0 else 0.0
        rate      = today if today > 0 else avg_daily
        if rem <= 0 and plan > 0:
            forecast = report_date.strftime("%d.%m.%Y")
        elif rate > 0:
            forecast = (report_date + timedelta(days=max(1, math.ceil(rem / rate)))).strftime("%d.%m.%Y")
        else:
            forecast = ""

        vals = [crop, round(plan, 2), round(fact, 2), round(rem, 2), pct, round(today, 2), forecast]
        for ci, v in enumerate(vals, start=1):
            c = ws.cell(row=data_row, column=ci, value=v)
            _style_cell(c, s, fill=s["fill_own"], align=s["align_l"] if ci == 1 else s["align_c"])
            if ci in (2, 3, 4, 6):
                c.number_format = NF_DEC2
            elif ci == 5:
                c.number_format = "0.0%"
        ws.row_dimensions[data_row].height = 26
        data_row += 1

    total_plan  = sum(float(veggie_plans.get(c, 0.0) or 0.0) for c in VEGGIE_CROP_ORDER)
    total_fact  = sum(float(season_map.get(c, 0.0) or 0.0) for c in VEGGIE_CROP_ORDER)
    total_today = sum(float(today_map.get(c, 0.0) or 0.0) for c in VEGGIE_CROP_ORDER)
    total_rem   = max(total_plan - total_fact, 0.0)
    total_pct   = (total_fact / total_plan) if total_plan > 0 else 0.0
    total_vals  = ["Итого", round(total_plan, 2), round(total_fact, 2),
                   round(total_rem, 2), total_pct, round(total_today, 2), ""]
    for ci, v in enumerate(total_vals, start=1):
        c = ws.cell(row=data_row, column=ci, value=v)
        _style_cell(c, s, fill=s["fill_total"], font=s["font_total"],
            align=s["align_l"] if ci == 1 else s["align_c"])
        if ci in (2, 3, 4, 6):
            c.number_format = NF_DEC2
        elif ci == 5:
            c.number_format = "0.0%"
    ws.row_dimensions[data_row].height = 26

    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 16
    ws.column_dimensions["D"].width = 16
    ws.column_dimensions["E"].width = 10
    ws.column_dimensions["F"].width = 18
    ws.column_dimensions["G"].width = 40


# =============================================================================
# ПОСТРОЕНИЕ ОТЧЁТА
# =============================================================================

async def build_report_async(report_date: date = REPORT_DATE) -> None:
    day_start, cutoff = compute_time_range(
        report_date, DAY_START_HOUR, DAY_END_HOUR
    )

    print(f"Отчёт '{WORK_TYPE_TITLE}'  |  Дата: {report_date}")
    print(f"  Суточный диапазон: {day_start} — {cutoff}\n")

    firm_names = list(AGROFIRMS.keys())
    field_task_years = _resolve_group_task_season_years(_SPRAY_KEY)
    veg_task_years = _resolve_group_task_season_years(_VEG_SPRAY_KEY)

    async def _fetch_spray_data():
        async with report_async_client(API_TOKEN, semaphore_limit=SEMAPHORE_LIMIT) as (client, semaphore):
            return await asyncio.gather(
                fetch_all_data(
                    client, semaphore, day_start, cutoff,
                    task_season_years=field_task_years,
                ),
                fetch_all_data(
                    client, semaphore, day_start, cutoff,
                    wt_ids=VEG_WT_IDS,
                    agrofirms=VEG_AGROFIRMS,
                    min_detail_area=0.0,
                    track_veg_crops=True,
                    task_season_years=veg_task_years,
                ),
            )

    print(f"  Обрабатываем {len(firm_names)} агрофирм...")
    timing_start()
    with stage("справочники ∥ задания+mtfmi (опрыск+овощи)"):
        _, (all_details, veg_details) = await run_parallel_refs_and_fetch(
            load_reference_data, _fetch_spray_data, label="r6-spray+veg",
        )

    manual_r6 = _build_manual_dict()
    veg_firm_names = [name for name in VEG_REPORT_FIRMS if name in VEG_AGROFIRMS]

    rows = _build_summary_rows(firm_names, all_details, manual_r6)
    df   = pd.DataFrame(rows)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 300)
    pd.set_option("display.float_format", "{:.2f}".format)
    print("\n")
    print(df.to_string(index=False))

    output_file = f"6. report_spraying_{report_date}.xlsx"
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    write_summary_sheet(
        wb, report_date, day_start, cutoff,
        firm_names, all_details, manual_r6,
        veg_firm_names=veg_firm_names,
        veg_details=veg_details,
    )

    for firm_name in firm_names:
        write_detail_sheet(
            wb, firm_name, report_date, day_start, cutoff,
            all_details[firm_name]["detail_own"],
            all_details[firm_name]["detail_hired"],
        )

    for firm_name in veg_firm_names:
        stats = veg_details.get(firm_name, {})
        write_detail_sheet(
            wb, firm_name, report_date, day_start, cutoff,
            stats.get("detail_own", []),
            stats.get("detail_hired", []),
            sheet_title=f"Овощи опрыск {_firm_table_label(firm_name)[:14]}",
        )

    with stage("Excel: сохранение"):
        wb.save(output_file)
    print(f"\nСохранено: {output_file}")
    timing_finish()


def build_report(report_date: date = REPORT_DATE) -> None:
    run_report_async(build_report_async(report_date))


def _print_firm_crops(title: str, by_firm: dict[str, dict[str, float]]) -> None:
    print(f"\n{title}")
    any_row = False
    for firm, crops in by_firm.items():
        rows = sorted(
            ((crop, ha) for crop, ha in crops.items() if ha >= 0.01),
            key=lambda pair: -pair[1],
        )
        if not rows:
            continue
        any_row = True
        print(f"  {firm}")
        for crop, ha in rows:
            print(f"    {crop}: {ha:.2f}")
    if not any_row:
        print("  нет")


async def compare_spray_crops(report_date: date = REPORT_DATE) -> None:
    """Культура опрыскивания за смену: поле за год и history_item строки площади."""
    global field_to_fg

    day_start, cutoff = compute_time_range(report_date, DAY_START_HOUR, DAY_END_HOUR)
    print(f"Сравнение культуры опрыскивания  |  Дата: {report_date}")
    print(f"  Суточный диапазон: {day_start} — {cutoff}\n")
    load_reference_data()
    fields = get_all_from_id("fields")
    field_to_fg = {
        int(f["id"]): f.get("field_group_id")
        for f in fields
        if f.get("id") is not None
    }

    headers = {"X-User-Api-Token": API_TOKEN}
    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    window = {"end_time_gt_eq": day_start_str, "end_time_lt": cutoff_str}

    from machine_tasks_batch_fetch import fetch_machine_tasks_by_wt_async
    from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks
    from mtaomi_batch_fetch import mass_request_async

    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        spray_tasks, veg_tasks = await asyncio.gather(
            fetch_machine_tasks_by_wt_async(
                fetch_all_pages, client, BASE_URL, SPRAY_WT_IDS, window, semaphore,
            ),
            fetch_machine_tasks_by_wt_async(
                fetch_all_pages, client, BASE_URL, VEGGIE_SPRAY_WT_IDS, window, semaphore,
            ),
        )
        spray_tasks = _filter_tasks_by_season(
            spray_tasks, _resolve_task_season_years(None, track_veg_crops=False),
        )
        veg_tasks = _filter_tasks_by_season(
            veg_tasks, _resolve_task_season_years(None, track_veg_crops=True),
        )
        groups = (
            ("3.1 Опрыскивание", [t for t in spray_tasks if (t.get("covered_area") or 0) > 0]),
            ("3.2 Опрыскивание (овощи)", [t for t in veg_tasks if (t.get("covered_area") or 0) > 0]),
        )
        task_ids = sorted({t["id"] for _name, tasks in groups for t in tasks})
        print(f"  Заданий с площадью: 3.1={len(groups[0][1])}, 3.2={len(groups[1][1])}")
        mtfmi_map = await fetch_mtfmi_map_for_tasks(
            client, semaphore, BASE_URL, task_ids, log_prefix="[r6] ",
        ) if task_ids else {}
        hist_ids = {
            int(item["history_item_id"])
            for items in mtfmi_map.values()
            for item in items
            if item.get("history_item_id") is not None
        }
        history = await mass_request_async(
            client, BASE_URL, "history_items", sorted(hist_ids), semaphore,
        )
    hist_crop = {}
    for hi in history:
        if hi.get("id") is None or hi.get("crop_id") is None:
            continue
        name = str(crop_map.get(int(hi["crop_id"])) or crop_map.get(hi["crop_id"]) or "").strip()
        if name:
            hist_crop[int(hi["id"])] = name

    def _accumulate(tasks: list[dict], *, veg: bool) -> tuple[dict, dict]:
        old: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        new: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for task in tasks:
            for item in mtfmi_map.get(task["id"], []):
                fid = item.get("field_id")
                if fid is None:
                    continue
                fid_i = int(fid)
                if fid_i not in available_field_ids:
                    continue
                firm = get_firm_for_field(fid_i)
                if not firm:
                    continue
                ha = _item_area_in_window(item, cutoff, day_start)
                if ha <= 0:
                    continue
                old_name = str(field_to_crop.get(fid_i) or field_to_crop.get(fid) or "").strip()
                if old_name and fid_i in field_to_crop:
                    label = _normalize_veggie_crop_label(old_name) if veg else old_name
                    if label:
                        old[firm][label] += ha
                hid = item.get("history_item_id")
                new_name = hist_crop.get(int(hid), "") if hid is not None else ""
                if new_name and new_name not in SKIP_CROPS:
                    label = _normalize_veggie_crop_label(new_name) if veg else new_name
                    if label:
                        new[firm][label] += ha
        return (
            {firm: dict(crops) for firm, crops in old.items()},
            {firm: dict(crops) for firm, crops in new.items()},
        )

    for title, tasks in groups:
        old_ha, new_ha = _accumulate(tasks, veg=title.startswith("3.2"))
        _print_firm_crops(f"{title} — старая (поле, активный год)", old_ha)
        _print_firm_crops(f"{title} — новая (history_item строки площади)", new_ha)


if __name__ == "__main__":
    if "--compare" in sys.argv:
        run_report_async(compare_spray_crops())
    else:
        build_report()