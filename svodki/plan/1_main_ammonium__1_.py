# 11.06.2026
from __future__ import annotations


import asyncio
import importlib.util
import json
import math
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import plans  # noqa: F401  # bootstrap utils/* (plan_paths via plans.py)
try:
    from season_calendar import season_year_for_date
except ImportError:  # pragma: no cover - season_calendar не в sys.path
    def season_year_for_date(d):
        return d.year + (1 if d.month >= 7 else 0)

try:
    from svodki_timing import stage, stage_add, timing_finish, timing_start
except ImportError:  # таймеры необязательны: без модуля работаем как раньше
    from contextlib import contextmanager as _cm

    @_cm
    def stage(name):  # type: ignore[misc]
        yield

    def stage_add(name, seconds):  # type: ignore[misc]
        pass

    def timing_start():  # type: ignore[misc]
        pass

    def timing_finish():  # type: ignore[misc]
        pass


def _tprint(label: str, t0: float) -> float:
    """Печатает «[timing] label: N с» (формат ловит tools/benchmark_svodki_timing.py), возвращает now."""
    now = time.monotonic()
    print(f"  [timing] {label}: {now - t0:.1f} с", flush=True)
    return now

from cropwise_paging import CROPWISE_PAGE_LIMIT, from_id_should_continue
import httpx
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
import requests

try:
    from excel_number_fmt import NF_DATE, NF_DEC1, NF_DEC2, NF_INT
except ImportError:
    NF_INT = "# ##0"
    NF_DEC1 = "# ##0.0"
    NF_DEC2 = "# ##0.00"
    NF_DATE = "DD.MM.YYYY"

try:
    from report1_sheet_order import reorder_report1_workbook_sheets as _reorder_r1_sheets
except ImportError:
    _reorder_r1_sheets = None


from task_area_shared import (
    CORN_SILAGE_CROP,
    HARVESTER_GROUP_IDS,
    SILAGE_CORN_MOWING_WT_IDS,
    harvest_area_booked_on_close,
    harvest_item_area_for_shift,
)
from plans import (
    API_TOKEN,
    BASE_URL,
    TZ_MSK,
    FIRM_TO_FOLDER,
    FOLDER_TO_FIRM,
    FIRM_ORDER,
    FIRM_TO_REGION,
    AGROFIRMS_META,
    AVAILABLE_OWN,
    PLAN_11_HA,
    PLAN_11_OVERRIDES,
    HARDCODED_PLAN,
    SKIP_CROPS,
    collapse_crop_areas,
    firm_group_plan_ha,
    is_crop_user_excluded,
    plan_ha_for_report,
    other_crops_as_rows,
    crop_report_mode,
    crop_sort_key_for_group,
    order_crops_by_group_rank,
    CROP_MODE_OTHER,
    PREFERRED_CROP_ORDER,
    PLAN_RATE_PER_GROUP,
    HIRED_GROUP_ID,
    MIN_DETAIL_AREA_HA,
    FIELD_NAME_FORCE_RULES,
    WORK_GROUPS,
    build_field_to_crop_general,
)

_VEG_KEY = "4.1 Посев (овощи)"
_VEG_CROP_NAME_MAP: dict[str, str] = {"Свекла столовая": "Свекла"}
_VEG_CROP_PLAN_HA: dict[str, float] = {}
for _firm, _firm_crops in HARDCODED_PLAN.get(_VEG_KEY, {}).items():
    for _crop, _val in _firm_crops.items():
        # Овощи, убранные в «Редакторе планов» (не считать / ранг 0 / скрытие по АФ), в план не входят.
        if is_crop_user_excluded(_VEG_KEY, _firm, _crop):
            continue
        _key = _VEG_CROP_NAME_MAP.get(_crop, _crop)
        _VEG_CROP_PLAN_HA[_key] = _VEG_CROP_PLAN_HA.get(_key, 0.0) + float(_val or 0.0)

def _default_report_date() -> date:
    """Сегодня по Москве (без зашитой даты; явная дата — переменная REPORT_DATE)."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    return _dt.now(_tz(_td(hours=3))).date()


REPORT_DATE  = _default_report_date()
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

from season_unified import (
    clear_season_cache,
    filter_tasks_by_group_seasons,
    period_range_for_group,
    resolve_group_season_config,
    task_season_str_for_group,
    task_season_years_set_for_group,
)

SEASON           = str(REPORT_DATE.year)
WORK_TYPE_ID     = 55
WORK_TYPE_NAME   = "1.1 Внесение минеральных удобрений"
_fert_season = resolve_group_season_config(WORK_TYPE_NAME, REPORT_DATE)
PERIOD_START_DATE = date.fromisoformat(_fert_season["period_start"])
PERIOD_START_STR  = PERIOD_START_DATE.strftime("%Y-%m-%d")

DAY_START_HOUR  = 6
DAY_END_HOUR    = 6
# TZ_MSK импортируется из plans
SEMAPHORE_LIMIT = 20

# Машины с площадью ниже этого порога не учитываются в счётчиках техники и деталях
# (кроме MIN_COUNT_AREA_EXEMPT_GROUPS / DETAIL_AREA_NO_THRESHOLD_GROUPS).
# Площадь за сутки — порог на машину-день (>= MIN_DETAIL_AREA_HA), как в 2_main_ideal.
MIN_COUNT_AREA_HA = 10.0

# Как во 2-й сводке: для этих групп порог 10 га на технику/время не применяется.
MIN_COUNT_AREA_EXEMPT_GROUPS: frozenset[str] = frozenset({
    "2.8 Глубокорыхление",
    "4.0 Посев",
    "4.1 Посев (овощи)",
    "3.2 Опрыскивание (овощи)",
    "5.4 Уборка картофеля, лука",
    "2.6 Фрезерование",
    "2.7 Гребнеобразование",
})

PLOW_GROUP_NAME = "2.4 Вспашка"
DEEP_GROUP_NAME = "2.8 Глубокорыхление"
SEEDING_GROUP_NAME = "4.0 Посев"
TILLAGE_GROUPS = frozenset({PLOW_GROUP_NAME, DEEP_GROUP_NAME})
# Посев / боронование / прикатывание — фирма из полной карты полей (как 2_main FACT_BY_ALL_FIELDS).
FACT_BY_ALL_FIELDS_GROUPS = frozenset({
    "2.1 Боронование",
    SEEDING_GROUP_NAME,
    "2.12 Прикатывание",
})
# Группы, для которых сводка 1 показывает только общий итог по агрофирме
# (лист «Выраб. — <группа>»), БЕЗ разбивки по культурам — в отличие от
# «1.1 Внесение минеральных удобрений» (свой расчёт, вне этой функции) и
# «4.0 Посев» (разбивка по культурам приходит отдельно из 1_main_dop.py).
# available_field_ids/field_to_crop построены специально под окно и культуры
# группы «1.1» (load_crop_field_ids/inventory_history_items с PERIOD_START_DATE
# от 1.1) — применять их как фильтр к этим группам неверно: часть реально
# обработанных полей (напр. под культивацию/опрыскивание до официального
# оформления культуры) отсекается, и итоговая площадь по агрофирме занижается.
# Поэтому здесь, как и в FACT_BY_ALL_FIELDS_GROUPS/TILLAGE_GROUPS, для
# включения поля в площадь достаточно того, что у него определена агрофирма.
FIRM_ONLY_NO_CROP_GROUPS = frozenset({
    "2.6 Фрезерование",
    "2.7 Гребнеобразование",
    # "2.8 Глубокорыхление" уже покрыта TILLAGE_GROUPS выше.
    "2.9 Дискование",
    "2.11 Культивация",
    "3.1 Опрыскивание",
})
SPRAY_GROUP_NAME = "3.1 Опрыскивание"
VEG_SPRAY_GROUP_NAME = "3.2 Опрыскивание (овощи)"
HARVEST_GRAIN_GROUP_NAME = "5.0 Уборка"
VEG_HARVEST_GROUP_NAME = "5.4 Уборка картофеля, лука"
VEG_HARVEST_WT_IDS = frozenset(
    wt_id
    for gn, wt_ids in WORK_GROUPS
    if gn == VEG_HARVEST_GROUP_NAME
    for wt_id in wt_ids
)
_HARVEST_ALL_WT_IDS: frozenset[int] = frozenset(
    wt_id
    for gn, wt_ids in WORK_GROUPS
    if gn == HARVEST_GRAIN_GROUP_NAME
    for wt_id in wt_ids
)
# Площадь уборки 5.0 — как 10_main: группа «5.0» без ID из «5.4».
GRAIN_AREA_WT_IDS: frozenset[int] = frozenset(
    wt for wt in _HARVEST_ALL_WT_IDS if wt not in VEG_HARVEST_WT_IDS
)
# Площадь + скашивание на полях «Кукуруза на силос» — как AREA_TASK_WT_IDS в 10_main.
AREA_TASK_WT_IDS: frozenset[int] = GRAIN_AREA_WT_IDS | SILAGE_CORN_MOWING_WT_IDS
# Счётчик комбайнов / grain_area — как 11_main: без овощных и без wt=6/7.
GRAIN_HARVEST_WT_IDS: frozenset[int] = frozenset(
    wt for wt in _HARVEST_ALL_WT_IDS
    if wt not in VEG_HARVEST_WT_IDS and wt not in {6, 7}
)
# Порог 0.5 га не применяется вовсе (площадь и счётчик) — как 2_main_ideal.
DETAIL_AREA_NO_THRESHOLD_GROUPS: frozenset[str] = frozenset({
    "2.6 Фрезерование",
    "2.7 Гребнеобразование",
    "2.8 Глубокорыхление",
    PLOW_GROUP_NAME,            # "2.4 Вспашка"
    HARVEST_GRAIN_GROUP_NAME,    # "5.0 Уборка"
})
PLOW_PERIOD_START = date(REPORT_DATE.year, 3, 14)
SVODKA1_PLOW_SEASON_FIX = "2026-08-06b"
_season_start_raw = (
    os.environ.get("SVODKI_SEASON_START_DATE")
    or os.environ.get("SEASON_START_DATE")
    or ""
).strip()
if _season_start_raw:
    try:
        PLOW_PERIOD_START = date.fromisoformat(_season_start_raw)
    except ValueError:
        pass


def _plow_task_seasons(report_date: date) -> set[str]:
    """Сезоны факта вспашки из редактора планов: и сутки, и сезон.

    Как опрыскивание: если выбраны 2026 и 2027, считаются оба.
    """
    try:
        from season_unified import fact_season_years_for_group

        years = fact_season_years_for_group("2.4 Вспашка", report_date)
        if years:
            return {str(y) for y in years}
    except Exception:
        pass
    raw = (
        os.environ.get("SVODKI_GRP_2_4_Вспашка_TASK_SEASON_YEARS")
        or os.environ.get("SVODKI_GRP_2_4_Вспашка_HISTORY_YEARS")
        or ""
    )
    found: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            found.add(part)
    if found:
        return found
    cur = season_year_for_date(report_date)
    return {str(cur - 1), str(cur)}


PLOW_TASK_SEASONS = _plow_task_seasons(REPORT_DATE)
PLOW_TASK_SEASON = "+".join(sorted(PLOW_TASK_SEASONS))


def refresh_season_config() -> None:
    """Перечитать season_ranges.json (редактор планов / proverka env)."""
    global PERIOD_START_DATE, PERIOD_START_STR, PLOW_PERIOD_START, PLOW_TASK_SEASON, PLOW_TASK_SEASONS
    clear_season_cache()
    fert = resolve_group_season_config(WORK_TYPE_NAME, REPORT_DATE, reload=True)
    PERIOD_START_DATE = date.fromisoformat(fert["period_start"])
    PERIOD_START_STR = PERIOD_START_DATE.strftime("%Y-%m-%d")
    PLOW_PERIOD_START = date(REPORT_DATE.year, 3, 14)
    PLOW_TASK_SEASONS = _plow_task_seasons(REPORT_DATE)
    PLOW_TASK_SEASON = "+".join(sorted(PLOW_TASK_SEASONS))
    if _season_start_raw:
        try:
            PLOW_PERIOD_START = date.fromisoformat(_season_start_raw)
        except ValueError:
            pass
# Быстрый первичный источник field_to_crop — agro_operations (без полной выгрузки history_items).
_CROP_LOOKUP_WT_IDS: frozenset[int] = frozenset({6, 7, 111, 118, 125, 126, WORK_TYPE_ID}) | VEG_HARVEST_WT_IDS
SPRAY_TASK_GROUPS = frozenset({SPRAY_GROUP_NAME, VEG_SPRAY_GROUP_NAME})


def _allowed_task_seasons_for_group(group_name: str) -> set[str] | None:
    """Сезоны редактора планов: и сутки, и общая сезонная площадь."""
    years = task_season_years_set_for_group(group_name, REPORT_DATE)
    if years:
        return years
    return None


VEG_SPRAY_CROPS = frozenset({
    "Картофель", "Морковь", "Свекла столовая",
})


def _detail_area_min_ha(group_name: str) -> float:
    """Порог 0.5 га на машину-день; 0 — без порога (вспашка/уборка и др.)."""
    if group_name in DETAIL_AREA_NO_THRESHOLD_GROUPS:
        return 0.0
    return MIN_DETAIL_AREA_HA


def _count_threshold_for_group(group_name: str) -> float:
    """Порог площади на машину для счётчиков — как _machine_count_min_ha в 2_main."""
    if group_name in DETAIL_AREA_NO_THRESHOLD_GROUPS:
        return 0.0
    if group_name in MIN_COUNT_AREA_EXEMPT_GROUPS:
        return MIN_DETAIL_AREA_HA
    return MIN_COUNT_AREA_HA


def _machine_qualifies_for_area(total: float, min_ha: float) -> bool:
    tc = round(float(total), 2)
    if min_ha <= 0:
        return tc > 0
    return tc >= min_ha


def _group_period_end_if_set(group_name: str) -> date | None:
    """Конец окна, если в редакторе сохранены свои даты сезонов этой операции."""
    try:
        cfg = resolve_group_season_config(group_name, REPORT_DATE)
        if cfg.get("has_own_ranges") and cfg.get("period_end"):
            return date.fromisoformat(str(cfg["period_end"])[:10])
    except Exception:
        return None
    return None


def _group_period_start(group_name: str) -> date:
    """Нижняя граница смен — единый season_unified (редактор планов)."""
    try:
        return period_range_for_group(group_name, REPORT_DATE)[0]
    except Exception:
        if group_name == PLOW_GROUP_NAME:
            return PLOW_PERIOD_START
        if group_name in (
            WORK_TYPE_NAME,
            SPRAY_GROUP_NAME,
            VEG_SPRAY_GROUP_NAME,
        ):
            return PERIOD_START_DATE
        if group_name == HARVEST_GRAIN_GROUP_NAME:
            return date.fromisoformat(
                resolve_group_season_config(HARVEST_GRAIN_GROUP_NAME, REPORT_DATE)["period_start"]
            )
        return date(REPORT_DATE.year, 1, 1)


def _row_matches_group_crop(group_name: str, item: dict) -> bool:
    """Овощное опрыскивание и уборка зерна отделяются культурой строки."""
    crop = _history_crop(item)
    if group_name == SPRAY_GROUP_NAME:
        return bool(crop and crop not in VEG_SPRAY_CROPS)
    if group_name == VEG_SPRAY_GROUP_NAME:
        return bool(crop and crop in VEG_SPRAY_CROPS)
    if group_name == HARVEST_GRAIN_GROUP_NAME:
        return not (crop and crop in VEG_SPRAY_CROPS)
    return True


def _spray_field_ok(fid_i: int, group_name: str) -> bool:
    # Вспашка/глубокорыхление — все поля с фирмой (как 2_main / сводка 9), без available.
    # Культура строки не требует членства поля в карте культур.
    if group_name in TILLAGE_GROUPS or group_name in SPRAY_TASK_GROUPS:
        return True
    return fid_i in available_field_ids


def _item_area_upto_cutoff(item: dict, t_to: datetime) -> float:
    """Площадь MTFMI с меткой < t_to — как 10_main_harvest."""
    hourly = item.get("covered_area_hourly") or []
    if not hourly:
        return float(item.get("covered_area") or 0.0)
    cov = 0.0
    for entry in hourly:
        h = parse_dt(entry[0])
        if h and h < t_to:
            cov += float(entry[1] or 0)
    return cov


def _is_harvest_combine(
    machine_id: int | None,
    machines_map: dict[int, dict],
) -> bool:
    if machine_id is None:
        return False
    grp = machines_map.get(int(machine_id), {}).get("machine_group_id")
    if grp is None:
        return False
    grp_i = int(grp)
    return grp_i in HARVESTER_GROUP_IDS or grp_i == HIRED_GROUP_ID


def _load_harvest_maps_from_main10() -> tuple[dict[int, str], dict[int, str]] | None:
    """field_to_crop и field_to_firm — как prepare_harvest_cache в 10_main."""
    main10_path = _resolve_main10_path()
    if main10_path is None:
        return None
    mod10 = _load_py_module("main10_harvest_maps_r1", main10_path)
    _configure_harvest_module(mod10, REPORT_DATE)
    return mod10.get_field_to_crop(), mod10.load_field_to_firm()


def _harvest_perf_10_style(
    tasks_with_area: list[dict],
    mtfmi_map: dict[int, list[dict]],
    today_task_ids: set[int],
    cutoff: datetime,
    machines_map: dict[int, dict],
    *,
    day_start: datetime,
    field_to_crop_map: dict[int, str] | None = None,
    field_to_firm_map: dict[int, str] | None = None,
) -> tuple[
    dict[str, float],
    dict[str, set[int]],
    dict[str, dict[int, float]],
]:
    """Площадь и комбайны за сутки — как process() в _populate_harvest_cache_tables.

    Лишние гектары на листе «Выраб. — 5.0» относительно «Общий намолот» давали:
    задания чужого сезона, скашивание по культуре поля (не строки) и часы
    скашивания у заданий, закрытых вне смены.
    """
    del field_to_crop_map, today_task_ids

    def _firm_for_field(fid_i: int) -> str:
        # Как cache.field_to_firm в 10_main: поле вне карты в итог не входит.
        if field_to_firm_map is not None:
            return field_to_firm_map.get(fid_i) or ""
        return get_firm_for_field(fid_i) or ""

    per_firm_area: dict[str, float] = defaultdict(float)
    per_firm_machines: dict[str, set[int]] = defaultdict(set)
    per_firm_mid_area: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))

    tasks = filter_tasks_by_group_seasons(
        tasks_with_area, HARVEST_GRAIN_GROUP_NAME, REPORT_DATE,
    )
    for t in tasks:
        mid = t.get("machine_id")
        if mid is None or not _is_harvest_combine(mid, machines_map):
            continue
        wt = t.get("work_type_id")
        try:
            wt_i = int(wt)
        except (TypeError, ValueError):
            continue
        if wt_i not in AREA_TASK_WT_IDS:
            continue
        is_mowing = wt_i in SILAGE_CORN_MOWING_WT_IDS
        task_end = parse_dt(t.get("end_time"))
        if task_end is None or not (day_start <= task_end < cutoff):
            continue
        for item in mtfmi_map.get(t["id"], []):
            fid = item.get("field_id")
            if fid is None:
                continue
            fid_i = int(fid)
            firm = _firm_for_field(fid_i)
            if not firm:
                continue
            from task_area_shared import history_crop_name

            crop = history_crop_name(item, "", mowing=is_mowing)
            if is_mowing and crop != CORN_SILAGE_CROP:
                continue
            cov = harvest_area_booked_on_close(
                item, day_start=day_start, cutoff=cutoff,
                task_end=task_end, parse_dt=parse_dt,
            )
            if cov <= 0:
                continue
            per_firm_area[firm] += cov
            mid_i = int(mid)
            per_firm_machines[firm].add(mid_i)
            per_firm_mid_area[firm][mid_i] += cov

    return per_firm_area, per_firm_machines, per_firm_mid_area


def _is_hired_machine_group(group) -> bool:
    """API может отдавать machine_group_id строкой — сравниваем как int."""
    if group is None:
        return False
    try:
        return int(group) == int(HIRED_GROUP_ID)
    except (TypeError, ValueError):
        return False


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_group_set(name: str) -> set[str]:
    raw = os.environ.get(name, "")
    return {part.strip() for part in raw.split("|") if part.strip()}


# Группы «Выраб. — …», которые заполняются из доноров 3–8 (REPORT1_SKIP_PERF_GROUPS), без своего API-расчёта.
REPORT1_SKIP_PERF_GROUPS: set[str] = _env_group_set("REPORT1_SKIP_PERF_GROUPS")


# Тяжёлые диагностические листы и подробный лог выключены по умолчанию:
# они полезны при разборе расхождений, но заметно замедляют обычный запуск.
REPORT1_ENABLE_DEBUG_SHEETS = _env_flag("REPORT1_DEBUG_SHEETS", False)
REPORT1_VERBOSE_OP_LOGS = _env_flag("REPORT1_VERBOSE_OP_LOGS", False)
# По умолчанию листы не скрываем: даже при нулевых показателях
# выводим таблицы, чтобы структура 1-й сводки не "пропадала" в PDF.
REPORT1_HIDE_EMPTY_TABLES = False
# Лист "Сводка по культурам" можно отключить: иногда он выглядит лишним.
# Для отключения установите REPORT1_INCLUDE_CROP_SUMMARY=0.
REPORT1_INCLUDE_CROP_SUMMARY = True


# WORK_GROUPS, AVAILABLE_OWN, PLAN_11_HA, PLAN_11_OVERRIDES,
# PLAN_RATE_PER_GROUP — импортируются из plans.py

# Алиасы для обратной совместимости внутри скрипта
AGROFIRMS = AGROFIRMS_META          # dict[str, tuple[folder_id, region_id, own_count)]
PLAN_HA   = PLAN_11_HA              # снимок плана 1.1; сводка читает plans.HARDCODED_PLAN

PLAN_OUTPUT: dict[str, float] = {k: 25.0 for k in AGROFIRMS}

REPORT1_PRE_HARVEST_PERF_GROUPS: tuple[str, ...] = (
    "2.1 Боронование",
    "2.9 Дискование",
    "2.6 Фрезерование",
    "2.11 Культивация",
)
REPORT1_POST_HARVEST_PERF_GROUPS: tuple[str, ...] = (
    "4.0 Посев",
    "3.1 Опрыскивание",
    "3.2 Опрыскивание (овощи)",
    "2.12 Прикатывание",
    "2.7 Гребнеобразование",
)
REPORT1_HARVEST_PERF_GROUP = HARVEST_GRAIN_GROUP_NAME
REPORT1_PERF_GROUP_ORDER: tuple[str, ...] = (
    *REPORT1_PRE_HARVEST_PERF_GROUPS,
    REPORT1_HARVEST_PERF_GROUP,
    *REPORT1_POST_HARVEST_PERF_GROUPS,
    # «4.1 Посев (овощи)» и «5.4 Уборка картофеля, лука» — отдельные листы «Выраб.» не выводим.
)

# Листы «Выраб. — …»: в таблице только эта АФ (остальные этими работами не занимаются).
REPORT1_PERF_CHISTOPOL_ONLY_GROUPS: frozenset[str] = frozenset({
    "2.6 Фрезерование",
    "2.7 Гребнеобразование",
    "4.1 Посев (овощи)",
    "3.2 Опрыскивание (овощи)",
})


def get_plan_rate_for_group(group_name: str, firm: str) -> float:
    if group_name in PLAN_RATE_PER_GROUP:
        return PLAN_RATE_PER_GROUP[group_name]
    return PLAN_OUTPUT.get(firm, 25.0)

# Пороги из manual_overrides.json → «1» (задаются в приложении «Планы/ожидания»)
REPORT1_PLAN_HA = 25.0
REPORT1_FORECAST_ALERT_DAYS = 45
REPORT1_FORECAST_ALERT_DATE: date | None = None
# Количество собственных агрегатов в наличии по умолчанию из AGROFIRMS (3-й элемент кортежа)
REPORT1_OWN_COUNT_BY_FIRM: dict[str, int] = {k: v[2] for k, v in AGROFIRMS.items()}
REPORT1_GROUP_MANUAL_KEYS: dict[str, str] = {
    "1.1 Внесение минеральных удобрений": "fertilizer_own_count_by_firm",
    "3.1 Опрыскивание": "spraying_own_count_by_firm",
    "3.2 Опрыскивание (овощи)": "veg_spraying_own_count_by_firm",
    "2.1 Боронование": "boron_own_count_by_firm",
    "2.11 Культивация": "cultivation_own_count_by_firm",
    "4.0 Посев": "seeding_own_count_by_firm",
}
REPORT1_GROUP_OWN_COUNTS: dict[str, dict[str, int]] = {
    group_name: {}
    for group_name in REPORT1_GROUP_MANUAL_KEYS
}


def _load_report1_thresholds() -> tuple[float, int, date | None, dict[str, dict[str, int]], dict[str, int]]:
    plan = 25.0
    days = 45
    alert_date: date | None = None
    group_own_counts: dict[str, dict[str, int]] = {
        group_name: {}
        for group_name in REPORT1_GROUP_MANUAL_KEYS
    }
    # Количество собственных агрегатов в наличии — берём из AGROFIRMS как базовые значения
    own_count_by_firm: dict[str, int] = {k: v[2] for k, v in AGROFIRMS.items()}
    p = os.environ.get("SVODKI_MANUAL_OVERRIDES_PATH", "").strip()
    if p:
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            o = data.get("1") or {}
            if "plan_output_ha_per_day" in o:
                plan = float(o["plan_output_ha_per_day"])
            if "forecast_alert_days" in o:
                days = int(o["forecast_alert_days"])
            raw_date = str(o.get("forecast_alert_date") or "").strip()
            if raw_date:
                alert_date = date.fromisoformat(raw_date)
            # Переопределяем количество собственных агрегатов из manual_overrides
            raw_own = o.get("own_count_by_firm")
            if isinstance(raw_own, dict):
                for firm in AGROFIRMS:
                    raw = raw_own.get(firm)
                    if raw is not None:
                        try:
                            own_count_by_firm[firm] = int(round(float(raw)))
                        except (TypeError, ValueError):
                            pass
            for group_name, section_key in REPORT1_GROUP_MANUAL_KEYS.items():
                raw_map = o.get(section_key)
                if not isinstance(raw_map, dict):
                    continue
                normalized: dict[str, int] = {}
                for firm in AGROFIRMS:
                    raw = raw_map.get(firm)
                    if raw is None:
                        continue
                    try:
                        normalized[firm] = int(round(float(raw)))
                    except (TypeError, ValueError):
                        continue
                group_own_counts[group_name] = normalized
        except Exception:
            pass
    plan = float(os.environ.get("SVODKI_REPORT1_PLAN_HA", str(plan)))
    days = int(os.environ.get("SVODKI_REPORT1_FORECAST_ALERT_DAYS", str(days)))
    env_date = os.environ.get("SVODKI_REPORT1_FORECAST_ALERT_DATE", "").strip()
    if env_date:
        try:
            alert_date = date.fromisoformat(env_date)
        except ValueError:
            pass
    return plan, days, alert_date, group_own_counts, own_count_by_firm


def apply_report1_thresholds() -> None:
    """Вызывать в начале сборки отчёта: подставляет плановую выработку по всем фирмам."""
    global REPORT1_PLAN_HA, REPORT1_FORECAST_ALERT_DAYS, REPORT1_FORECAST_ALERT_DATE
    global REPORT1_GROUP_OWN_COUNTS, REPORT1_OWN_COUNT_BY_FIRM
    plan, days, alert_date, group_own_counts, own_count_by_firm = _load_report1_thresholds()
    REPORT1_PLAN_HA = plan
    REPORT1_FORECAST_ALERT_DAYS = days
    REPORT1_FORECAST_ALERT_DATE = alert_date
    REPORT1_GROUP_OWN_COUNTS = group_own_counts
    REPORT1_OWN_COUNT_BY_FIRM = own_count_by_firm
    for k in PLAN_OUTPUT:
        PLAN_OUTPUT[k] = plan


def _fleet_for_group(
    group_name: str,
) -> dict[str, int]:
    """
    Возвращает словарь firm → количество собственных агрегатов в наличии.
    Базовые значения берутся из AVAILABLE_OWN по типу работ (если есть),
    иначе из AGROFIRMS (3-й элемент кортежа),
    могут быть переопределены через manual_overrides → секция «1» → own_count_by_firm,
    а также через REPORT1_GROUP_OWN_COUNTS для конкретных групп работ.
    """
    if group_name in AVAILABLE_OWN:
        own = dict(AVAILABLE_OWN[group_name])
    else:
        own = dict(REPORT1_OWN_COUNT_BY_FIRM)
    group_override = REPORT1_GROUP_OWN_COUNTS.get(group_name) or {}
    if group_override:
        for firm, cnt in group_override.items():
            own[firm] = cnt
    return own


def compute_forecast_end_date(plan_val: float, fact_val: float, today_val: float) -> date | None:
    """Прогнозная дата завершения (как в старом макете: max(средняя по сезону, факт за сутки))."""
    remainder = max(float(plan_val) - float(fact_val), 0.0)
    if remainder <= 0:
        return REPORT_DATE
    days_elapsed = max((REPORT_DATE - PERIOD_START_DATE).days, 1)
    avg_daily = float(fact_val) / days_elapsed if fact_val > 0 else 0.0
    daily_rate = max(avg_daily, float(today_val)) if today_val > 0 else avg_daily
    if daily_rate <= 0:
        return None
    days_left = remainder / daily_rate
    return REPORT_DATE + timedelta(days=int(days_left) + 1)


def forecast_date_is_alert(end: date | None) -> bool:
    if end is None:
        return False
    if REPORT1_FORECAST_ALERT_DATE is not None:
        return end > REPORT1_FORECAST_ALERT_DATE
    return (end - REPORT_DATE).days > REPORT1_FORECAST_ALERT_DAYS



# HIRED_GROUP_ID импортируется из plans (= 7)

# Расширенный набор культур для первой сводки и таблиц внесения удобрений.
# Базовый набор культур для широких листов (свод по агрофирмам/листы фирм).
SUMMARY_CROPS = [
    "Рапс озимый",
    "Пшеница озимая",
    "Многолетние травы",
]

# Расширенный набор культур — только для первой таблицы "Сводка по культурам".
CROP_SUMMARY_CROPS = [
    "Рапс озимый",
    "Пшеница озимая",
    "Рапс яровой",
    "Пшеница яровая",
    "Подсолнечник",
    "Соя",
    "Кукуруза на зерно",
    "Кукуруза на силос",
    "Тритикале озимая",
    "Однолетние травы",
    "Картофель",
    "Морковь",
    "Свекла столовая",
]

# Культуры для листов внесения удобрений.
FERT_TABLE_SLIDES: list[tuple[str, list[str]]] = [
    ("Удобрения — Рапс", ["Рапс озимый", "Рапс яровой"]),
    ("Удобрения — Пшеница", ["Пшеница озимая", "Пшеница яровая"]),
    ("Удобрения — Подсолнечник и соя", ["Подсолнечник", "Соя"]),
    ("Удобрения — Кукуруза", ["Кукуруза на зерно", "Кукуруза на силос"]),
    ("Удобрения — Заготовка корма", ["Многолетние травы", "Тритикале озимая", "Однолетние травы"]),
    ("Удобрения — Пар", ["Пар"]),
    ("Удобрения — Овощи", ["Картофель", "Морковь", "Свекла столовая"]),
]
FERT_TABLE_CROPS = [crop for _, crops in FERT_TABLE_SLIDES for crop in crops]

# Фиксированные удобрения для листов внесения — ключевые подстроки в названии
# fert_id определяется динамически в _resolve_fixed_fert_ids() после загрузки справочника
FERT_NITRATE_KEY  = "селитра"   # Аммиачная селитра
FERT_SULFATE_KEY  = "сульфат"   # Сульфат аммония
# Глобальные ID: заполняются в _resolve_fixed_fert_ids()
FERT_NITRATE_ID:  int | None = None
FERT_SULFATE_ID:  int | None = None
FERT_FIXED_COLS: list[tuple[int | None, str]] = []  # [(id, label), ...]

# fert_data по всем культурам: crop → firm → fert_id → {season, today}
all_crops_fert_data: dict[str, dict[str, dict[int, dict[str, float]]]] = {}
# fact_total по всем культурам из agro_operations: crop → firm → area
all_crops_fact_agro: dict[str, dict[str, float]] = {}
# fact_today по всем культурам: crop → firm → area  
all_crops_fact_today: dict[str, dict[str, float]] = {}

# Справочник удобрений: id → name (заполняется в load_reference_data)
fertilizer_name_map: dict[int, str] = {}

# Упорядоченный список (fert_id, fert_name) для колонок Рапс озимый
# (заполняется в fetch_fertilizer_11_mtfmi после сбора данных)
raps_fert_order: list[tuple[int, str]] = []

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})

_HTTP_GET_CACHE: dict[tuple, list] = {}


def get_all_from_id(endpoint: str, extra_params: dict | None = None) -> list[dict]:
    cache_key = (endpoint, tuple(sorted((extra_params or {}).items())))
    cached = _HTTP_GET_CACHE.get(cache_key)
    if cached is not None:
        return cached
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
    _HTTP_GET_CACHE[cache_key] = results
    return results


def get_all_page(endpoint: str, params: dict | None = None) -> list[dict]:
    cache_key = (f"page:{endpoint}", tuple(sorted((params or {}).items())))
    cached = _HTTP_GET_CACHE.get(cache_key)
    if cached is not None:
        return cached
    params = dict(params or {})
    params.setdefault("per_page", 1000)
    results, page = [], 1
    while True:
        r = session.get(f"{BASE_URL}/{endpoint}", params={**params, "page": page})
        r.raise_for_status()
        body  = r.json()
        chunk = body if isinstance(body, list) else body.get("data", [])
        results.extend(chunk)
        if len(chunk) < params["per_page"]:
            break
        page += 1
    _HTTP_GET_CACHE[cache_key] = results
    return results

def compute_time_range(report_date: date) -> tuple[datetime, datetime]:
    if DAY_START_HOUR == DAY_END_HOUR:
        day_start = datetime(report_date.year, report_date.month, report_date.day,
                             DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK) - timedelta(days=1)
        cutoff    = datetime(report_date.year, report_date.month, report_date.day,
                             DAY_END_HOUR, 0, 0, tzinfo=TZ_MSK)
    else:
        day_start = datetime(report_date.year, report_date.month, report_date.day,
                             DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
        cutoff    = datetime(report_date.year, report_date.month, report_date.day,
                             DAY_END_HOUR, 0, 0, tzinfo=TZ_MSK)
    return day_start, cutoff


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
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_MSK)
    else:
        dt = dt.astimezone(TZ_MSK)
    return dt


def seconds_to_hhmm(seconds: float) -> str:
    """Время на полях: часы:минуты:секунды, для PDF/Excel."""
    total = int(max(0.0, seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def decimal_hours_to_hhmm(hours: float | None) -> str:
    """Десятичные часы машин (API) → «14:54:00» для колонок «…, час»."""
    if hours is None or hours <= 0:
        return "00:00:00"
    return seconds_to_hhmm(round(float(hours) * 3600))


def _fmt_ru_grouped(value: float | int | None, decimals: int = 0) -> str:
    """
    Форматирует число как текст с разделителем тысяч пробелом:
    15000 -> "15 000", 2945.99 -> "2 945,99".
    Используется там, где PDF/LO может игнорировать number_format.
    """
    if value is None:
        value = 0
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = 0.0
    if decimals <= 0:
        return f"{int(round(num)):,}".replace(",", " ")
    formatted = f"{num:,.{decimals}f}".replace(",", " ").replace(".", ",")
    return formatted


async def fetch_pages(client: httpx.AsyncClient, url: str,
                      params: dict, sem: asyncio.Semaphore) -> list[dict]:
    items, from_id = [], 0
    while True:
        async with sem:
            r     = await client.get(url, params={**params, "from_id": from_id, "limit": CROPWISE_PAGE_LIMIT})
            body  = r.json()
            chunk = body.get("data", [])
        if not chunk:
            break
        items.extend(chunk)
        meta    = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not from_id_should_continue(meta, chunk):
            break
        from_id = last_id + 1
    return items


async def fetch_mtfmi(client: httpx.AsyncClient, task_id: int,
                      sem: asyncio.Semaphore) -> tuple[int, list[dict]]:
    items = await fetch_pages(client, f"{BASE_URL}/machine_task_field_mapping_items",
                              {"machine_task_id": task_id}, sem)
    return task_id, items

machines_map:   dict[int, dict]  = {}
implements_map: dict[int, dict]  = {}
crop_map:       dict[int, str]   = {}
field_to_fg:    dict[int, int]   = {}
fg_to_folder:   dict[int, int]   = {}
folder_to_firm: dict[int, str]   = {}
field_to_crop:  dict[int, str]   = {}
# Культура внесения МУ: history более нового сезона заменяет прошлый год
# (иначе активный «Рапс яровой» 2026 перекрывает пшеницу/сою/картофель 2027).
fert_field_to_crop: dict[int, str] = {}
field_area_map: dict[int, float] = {}
field_name_map: dict[int, str]   = {}  # field_id → название поля
field_id_resolved_firm: dict[int, str] = {}
# Поля, активные (available=True) хотя бы в один день сезонного периода
available_field_ids: set[int] = set()
# Поля с активной культурой (history_items year, не SKIP_CROPS) — как 3_main.load_crop_field_ids
crop_field_ids: set[int] = set()
# Справочники грузятся в потоке параллельно с API. Пока событие не взведено,
# расчёт групп/1.1 не должен читать field_to_crop, available_field_ids и crop_field_ids:
# быстрая группа иначе тихо получает нули.
_reference_ready = threading.Event()
_reference_ready.set()
# True на время пересечения load_reference_data с fetch: карты полей уже
# сброшены снаружи, повторный сброс внутри затёр бы дописки групп.
_skip_field_map_reset = False


async def _wait_reference_data(label: str) -> None:
    if _reference_ready.is_set():
        return
    t = time.monotonic()
    await asyncio.to_thread(_reference_ready.wait)
    dt = time.monotonic() - t
    if dt >= 0.05:
        print(f"  [timing] ожидание справочников ({label}): {dt:.1f} с", flush=True)


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


def _rebuild_field_firm_resolution(
    fields: list[dict],
    fgs: list[dict],
    *,
    merge: bool = False,
) -> None:
    """Как в 3.main_real.load_field_to_firm: подъём по group_folders + правила по имени поля."""
    global field_id_resolved_firm
    fgs_map = {fg["id"]: fg for fg in fgs}
    gfs_all = get_all_from_id("group_folders")
    parent_map = _folder_parent_map(gfs_all)
    root_ids = set(FIRM_TO_FOLDER.values())
    gf_hints = _group_folder_name_firm_hints(gfs_all)
    resolved_before = len(field_id_resolved_firm)
    if not merge:
        field_id_resolved_firm = {}
    for f in fields:
        fid = f.get("id")
        if fid is None:
            continue
        fgid = f.get("field_group_id")
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
            field_id_resolved_firm[int(fid)] = firm_key
    forced = 0
    for f in fields:
        fid = f.get("id")
        if fid is None:
            continue
        fk = _firm_from_field_name(f)
        if fk:
            field_id_resolved_firm[int(fid)] = fk
            forced += 1
    resolved_total = len(field_id_resolved_firm)
    if merge:
        print(
            f"    Полей с привязкой к агрофирме (иерархия папок, как в отчёте 3): "
            f"{resolved_total} (+{resolved_total - resolved_before} новых, "
            f"принудительно по имени: {forced})"
        )
    else:
        print(
            f"    Полей с привязкой к агрофирме (иерархия папок, как в отчёте 3): "
            f"{resolved_total} (принудительно по имени: {forced})"
        )


def reset_field_firm_resolution() -> None:
    """Один раз очистить карту поле→фирма перед параллельной загрузкой 1.1 и групп."""
    field_id_resolved_firm.clear()


def _crop_name_from_op(op: dict) -> str:
    for key in ("crop_name", "crop_title", "culture_name", "culture_title"):
        v = str(op.get(key) or "").strip()
        if v:
            return v
    return ""


_fert_ops_season_cache: list[dict] | None = None


def get_fert_agro_operations() -> list[dict]:
    """agro_operations wt=55 за сезон — кэш для load_reference и fetch_fertilizer_11_mtfmi."""
    global _fert_ops_season_cache
    if _fert_ops_season_cache is None:
        _fert_ops_season_cache = get_all_from_id(
            "agro_operations", {"work_type_id": WORK_TYPE_ID, "season": SEASON},
        )
    return _fert_ops_season_cache


def load_crop_field_ids(report_year: int) -> set[int]:
    """Как 3_main_real.load_crop_field_ids: active history_items, без SKIP_CROPS."""
    print(f"  Загружаем crops и history_items year={report_year} (фильтр SKIP_CROPS для 1.1)...")
    crops_map = {
        c["id"]: c.get("name") or c.get("title", "")
        for c in get_all_from_id("crops")
    }
    his_all = get_all_from_id("history_items", {"year": report_year})
    valid_field_ids: set[int] = set()
    for hi in his_all:
        fid = hi.get("field_id")
        crop_id = hi.get("crop_id")
        active = hi.get("active")
        if not (fid and crop_id and crop_id in crops_map and active):
            continue
        crop_name = crops_map[crop_id]
        if crop_name in SKIP_CROPS:
            continue
        valid_field_ids.add(int(fid))
        # Дополняем field_to_crop, если культуры ещё нет (для разбивки по культурам).
        if int(fid) not in field_to_crop:
            field_to_crop[int(fid)] = crop_name
    print(f"    Полей с активной культурой (не SKIP_CROPS): {len(valid_field_ids)}")
    return valid_field_ids


def _build_fert_field_to_crop(years: list[int]) -> None:
    """Культура 1.1 — history более нового сезона поверх прошлого года."""
    global fert_field_to_crop, crop_map
    if not years:
        fert_field_to_crop = {}
        return
    if not crop_map:
        crops = get_all_page("crops")
        crop_map = {c["id"]: c.get("name") or c.get("title", "") for c in crops}
    history_by_year: dict[int, list[dict]] = {}
    for crop_year in years:
        history_by_year[int(crop_year)] = get_all_from_id(
            "history_items", {"year": str(crop_year)},
        )
    fert_field_to_crop = build_field_to_crop_general(
        crop_map={int(k): v for k, v in crop_map.items()},
        history_by_year=history_by_year,
        priority_year=max(int(y) for y in years),
        skip_crops=SKIP_CROPS,
        newest_year_wins=True,
    )
    print(
        f"    field_to_crop для 1.1 (сезон {max(years)} заменяет прошлый): "
        f"{len(fert_field_to_crop)} полей"
    )


def _history_crop(item: dict | None) -> str:
    """Культура строки работы: только mtfmi.history_item_id."""
    if not item:
        return ""
    from mtfmi_batch_fetch import crop_of_item

    return str(crop_of_item(item, "") or "")


def _history_crop_for_field_items(items: list[dict], fid: int) -> str:
    """Первая культура history_item среди строк этого поля в задании."""
    for item in items:
        raw = item.get("field_id")
        if raw is None:
            continue
        try:
            if int(raw) != int(fid):
                continue
        except (TypeError, ValueError):
            continue
        name = _history_crop(item)
        if name:
            return name
    from mtfmi_batch_fetch import UNDEFINED_CROP

    return UNDEFINED_CROP


def load_reference_data() -> None:
    global crop_map
    global field_to_fg, fg_to_folder, folder_to_firm
    global field_to_crop, field_area_map, fertilizer_name_map, field_name_map
    global field_id_resolved_firm, available_field_ids, crop_field_ids

    print("  Загружаем fertilizers...")
    ferts = get_all_from_id("fertilizers")
    fertilizer_name_map = {
        f["id"]: (f.get("name") or f.get("title") or f"fert_{f['id']}")
        for f in ferts
    }
    print(f"    {len(fertilizer_name_map)} удобрений")

    print("  Загружаем field_groups (fields — после tasks/mtfmi через mass_request)...")
    fgs          = get_all_page("field_groups")
    fg_to_folder = {fg["id"]: fg.get("group_folder_id") for fg in fgs}
    folder_to_firm = dict(FOLDER_TO_FIRM)
    print(f"    {len(fgs)} групп")

    if not _skip_field_map_reset:
        field_to_fg = {}
        field_area_map = {}
        field_name_map = {}

    print("  Загружаем inventory_history_items (фильтр available)...")
    ihi_all = get_all_from_id("inventory_history_items", {"historyable_type": "Field"})
    print(f"    Всего записей inventory_history_items: {len(ihi_all)}")
    period_start = PERIOD_START_DATE
    period_end   = REPORT_DATE
    _avail: set[int] = set()
    for r in ihi_all:
        if not r.get("available"):
            continue
        fid_r = r.get("historyable_id")
        if fid_r is None:
            continue
        raw_start = r.get("event_start_at")
        raw_end   = r.get("event_end_at")
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
            _avail.add(int(fid_r))
    available_field_ids = _avail
    print(f"    Полей available=True в периоде {period_start}–{period_end}: {len(available_field_ids)}")

    field_to_crop.clear()
    print(f"  Загружаем agro_operations (быстрый поиск культуры), wt={sorted(_CROP_LOOKUP_WT_IDS)}...")
    from machine_tasks_batch_fetch import fetch_by_work_type_ids_sync

    _crop_op_date: dict[int, str] = {}
    other_wts = sorted(wt for wt in _CROP_LOOKUP_WT_IDS if wt != WORK_TYPE_ID)
    ops_by_wt: dict[int, list[dict]] = defaultdict(list)
    for op in get_fert_agro_operations():
        wt = op.get("work_type_id")
        if wt is not None:
            ops_by_wt[int(wt)].append(op)
    for op in fetch_by_work_type_ids_sync(
        get_all_from_id, "agro_operations", other_wts, {"season": SEASON},
    ):
        wt = op.get("work_type_id")
        if wt is not None:
            ops_by_wt[int(wt)].append(op)
    for wt_id in sorted(_CROP_LOOKUP_WT_IDS):
        for op in ops_by_wt.get(wt_id, []):
            fid = op.get("field_id")
            if fid is None:
                continue
            name = _crop_name_from_op(op)
            if not name or name in SKIP_CROPS:
                continue
            fid_i = int(fid)
            d = str(op.get("completed_date") or op.get("completed_datetime") or "")
            if fid_i in field_to_crop and d <= _crop_op_date.get(fid_i, ""):
                continue
            field_to_crop[fid_i] = name
            _crop_op_date[fid_i] = d
    print(f"    field_to_crop из agro_operations: {len(field_to_crop)} полей")

    missing = available_field_ids - set(field_to_crop)
    if missing:
        season_year = season_year_for_date(REPORT_DATE)
        print(
            f"    Активных полей без культуры из agro_operations: {len(missing)} — "
            f"добираем history_items (fallback, сезон {season_year})..."
        )
        from svodki_pipeline import run_reference_jobs

        hist_years = (int(SEASON), season_year)
        jobs: dict[str, object] = {"crops": lambda: get_all_page("crops")}
        for crop_year in hist_years:
            jobs[f"history:{crop_year}"] = (
                lambda yy=crop_year: get_all_from_id("history_items", {"year": str(yy)})
            )
        print("  Загружаем crops и history_items (fallback)...")
        raw = run_reference_jobs(jobs)
        crops = raw["crops"]
        crop_map = {c["id"]: c.get("name") or c.get("title", "") for c in crops}
        print(f"    {len(crop_map)} культур")
        history_by_year: dict[int, list[dict]] = {}
        for crop_year in hist_years:
            chunk = raw[f"history:{crop_year}"]
            history_by_year[crop_year] = chunk
            print(f"    history_items year={crop_year}: {len(chunk)} записей")
        extra = build_field_to_crop_general(
            crop_map={int(k): v for k, v in crop_map.items()},
            history_by_year=history_by_year,
            priority_year=season_year,
            skip_crops=SKIP_CROPS,
        )
        added = 0
        for fid_i, crop_name in extra.items():
            if fid_i in missing and fid_i not in field_to_crop:
                field_to_crop[fid_i] = crop_name
                added += 1
        print(f"    Добавлено из history_items: {added} полей")
    else:
        print("    Все активные поля покрыты agro_operations — history_items/crops не запрашивались.")
    print(f"    Итого field_to_crop: {len(field_to_crop)} полей")

    # Фильтр полей для 1.1 — active history_items сезонов группы (2026+2027),
    # не только календарный год отчёта: поле с культурой лишь в 2027 иначе выпадает.
    fert_years = [
        int(y)
        for y in (
            resolve_group_season_config(WORK_TYPE_NAME, REPORT_DATE).get("history_years")
            or [season_year_for_date(REPORT_DATE) - 1, season_year_for_date(REPORT_DATE)]
        )
    ]
    crop_field_ids = set()
    for fert_year in fert_years:
        crop_field_ids |= load_crop_field_ids(fert_year)
    _build_fert_field_to_crop(fert_years)

    _resolve_fixed_fert_ids()




def get_firm_for_field(fid: int) -> str:
    return field_id_resolved_firm.get(int(fid), "")


def _resolve_fixed_fert_ids() -> None:
    """После загрузки fertilizer_name_map находит ID Селитры и Сульфата по подстроке в названии."""
    global FERT_NITRATE_ID, FERT_SULFATE_ID, FERT_FIXED_COLS
    for fid, fname in fertilizer_name_map.items():
        fl = fname.lower()
        if FERT_NITRATE_ID is None and FERT_NITRATE_KEY in fl:
            FERT_NITRATE_ID = fid
        if FERT_SULFATE_ID is None and FERT_SULFATE_KEY in fl:
            FERT_SULFATE_ID = fid
    FERT_FIXED_COLS = [
        (FERT_NITRATE_ID, "Селитра"),
        (FERT_SULFATE_ID, "Сульфат"),
    ]
    print(f"    Удобрения: Селитра id={FERT_NITRATE_ID}, Сульфат id={FERT_SULFATE_ID}")


# =============================================================================
# ПЛАН
# =============================================================================

def build_plan_ha() -> dict[str, dict[str, float]]:
    auto_plan: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for fid, crop_name in field_to_crop.items():
        firm = get_firm_for_field(fid)
        if not firm:
            continue
        auto_plan[firm][crop_name] += field_area_map.get(fid, 0.0)

    result: dict[str, dict[str, float]] = {}
    for firm in AGROFIRMS:
        result[firm] = {
            crop: round(area, 1)
            for crop, area in auto_plan.get(firm, {}).items()
        }
        # Для сводки 1.1 рапс и многолетние должны браться из согласованных планов,
        # а не суммироваться с авто-ключами (например, "Рапс яровой").
        for crop in list(result[firm].keys()):
            lc = crop.lower()
            if "рапс" in lc or "многолетние" in lc:
                result[firm].pop(crop, None)
    # Овощи из «4.1 Посев», только если в плане 1.1 этой культуры нет.
    agreed = plans.HARDCODED_PLAN.get(WORK_TYPE_NAME) or {}
    if not isinstance(agreed, dict):
        agreed = {}
    agreed_names = {
        str(crop)
        for crops in agreed.values()
        if isinstance(crops, dict)
        for crop in crops
    }
    veg_covered = set(agreed_names)
    for alias, canonical in _VEG_CROP_NAME_MAP.items():
        if alias in agreed_names:
            veg_covered.add(canonical)
    if _VEG_CROP_PLAN_HA:
        for crop, total_plan_val in _VEG_CROP_PLAN_HA.items():
            if crop in veg_covered:
                continue
            auto_total = sum(
                auto_plan.get(firm, {}).get(crop, 0.0) for firm in AGROFIRMS
            )
            for firm in AGROFIRMS:
                firm_auto = auto_plan.get(firm, {}).get(crop, 0.0)
                if auto_total > 0 and firm_auto > 0:
                    result[firm][crop] = round(total_plan_val * firm_auto / auto_total, 1)
                elif auto_total <= 0 and crop in result.get(firm, {}):
                    # нет авто-данных, но культура есть — оставляем как есть
                    pass
    # План редактора по всей группе 1.1 перекрывает автоплощадь и овощной посев.
    for firm in AGROFIRMS:
        firm_plan = agreed.get(firm)
        if isinstance(firm_plan, dict):
            for crop, plan_val in firm_plan.items():
                try:
                    result[firm][str(crop)] = float(plan_val or 0)
                except (TypeError, ValueError):
                    continue
        for crop, plan_val in PLAN_11_OVERRIDES.get(firm, {}).items():
            result[firm][crop] = plan_val
    return result


# =============================================================================
# 1.1 Внесение удобрений — те же данные, что в 1_main.py (1).py (mtfmi)
# =============================================================================

async def fetch_fertilizer_11_mtfmi(
    client:    httpx.AsyncClient,
    sem:       asyncio.Semaphore,
    day_start: datetime,
    cutoff:    datetime,
    *,
    merge_firm_map: bool = False,
) -> tuple[
    dict[str, dict[str, float]],   # fact_total: firm → crop → га (mtfmi)
    dict[str, dict],                # day_stats
    dict[str, dict[int, dict]],    # fert_data: firm → fert_id → {season, today}
    dict[int, float],              # cmp_mtfmi: field_id → га из machine_tasks/mtfmi
    dict[int, dict],               # cmp_agro:  field_id → {area, ferts: {fert_id: га}, firm, crop}
    list[dict],                    # today_ops_detail: детализация суточных операций
]:
    """
    machine_tasks wt=55 + mtfmi — выработка как в 3_main_real.build_all_firm_stats.
    Дополнительно fert_data / cmp_* по agro_operations для листов удобрений.
    """
    url = f"{BASE_URL}/machine_tasks"
    season_start_dt = datetime(
        PERIOD_START_DATE.year, PERIOD_START_DATE.month, PERIOD_START_DATE.day,
        tzinfo=TZ_MSK,
    )
    season_start_str = season_start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")

    _t = time.monotonic()
    print("    [1.1] Загружаем machine_tasks за сутки и сезон (как 3_main_real)...")
    tasks_today, tasks_season = await asyncio.gather(
        fetch_pages(client, url, {
            "work_type_id": WORK_TYPE_ID,
            "end_time_gt_eq": day_start_str,
            "end_time_lt": cutoff_str,
        }, sem),
        fetch_pages(client, url, {
            "work_type_id": WORK_TYPE_ID,
            "end_time_gt_eq": season_start_str,
            "end_time_lt": cutoff_str,
        }, sem),
    )

    _t = _tprint("1.1 machine_tasks", _t)
    # Как сводка 2: и сутки, и «Факт общий» — только сезоны из редактора планов.
    # Иначе при выборе одного 2027 в итог попадают задания сезона 2026 из того же окна дат.
    _fert_seasons = task_season_years_set_for_group(WORK_TYPE_NAME, REPORT_DATE)
    _n_today, _n_season = len(tasks_today), len(tasks_season)
    tasks_today = filter_tasks_by_group_seasons(tasks_today, WORK_TYPE_NAME, REPORT_DATE)
    tasks_season = filter_tasks_by_group_seasons(tasks_season, WORK_TYPE_NAME, REPORT_DATE)
    _need = "+".join(sorted(_fert_seasons or [])) or "все"
    print(
        f"    [1.1] Заданий за сутки: {_n_today} → {len(tasks_today)}, "
        f"за сезон (с {PERIOD_START_DATE}): {_n_season} → {len(tasks_season)} "
        f"(сезон заданий: {_need})"
    )

    all_task_ids = list({t["id"] for t in tasks_today + tasks_season})
    if not all_task_ids:
        empty_day_stats = {firm: _empty_stats() for firm in AGROFIRMS}
        return (
            {firm: {} for firm in AGROFIRMS},
            empty_day_stats,
            {firm: {} for firm in AGROFIRMS},
            {},
            {},
            [],
        )

    print("    [1.1] Загружаем mtfmi для всех заданий...")
    from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks

    mtfmi_map = await fetch_mtfmi_map_for_tasks(
        client, sem, BASE_URL, all_task_ids, log_prefix="    [1.1] ",
    )
    _t = _tprint("1.1 mtfmi", _t)

    from report_resources_fetch import (
        collect_report_resource_ids,
        load_fields_list_async,
        load_implements_dict_async,
        load_machines_map_async,
    )

    all_tasks = tasks_today + tasks_season
    field_ids, machine_ids, implement_ids = collect_report_resource_ids(
        all_tasks, mtfmi_map,
    )
    # sync-HTTP (requests) с кэшем: в потоке, чтобы не вешать event loop, пока
    # параллельно качаются остальные группы.
    for op in await asyncio.to_thread(get_fert_agro_operations):
        fid = op.get("field_id")
        if fid is not None:
            field_ids.add(int(fid))
    print(
        f"    [1.1] Ресурсы: полей={len(field_ids)}, "
        f"машин={len(machine_ids)}, орудий={len(implement_ids)}"
    )
    fields_all, _new_machines, _new_implements = await asyncio.gather(
        load_fields_list_async(client, BASE_URL, field_ids, sem),
        load_machines_map_async(client, BASE_URL, machine_ids, sem),
        load_implements_dict_async(client, BASE_URL, implement_ids, sem),
    )
    # In-place (не переприсваивание): параллельно группы делают machines_map.update(...),
    # иначе завершение 1.1 «позже» затёрло бы их машины/орудия.
    machines_map.update(_new_machines)
    implements_map.update(_new_implements)
    _t = _tprint("1.1 поля/машины/орудия", _t)
    field_to_fg.update({
        int(f["id"]): f.get("field_group_id")
        for f in fields_all if f.get("id") is not None
    })
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        fid_i = int(fid)
        field_area_map[fid_i] = f.get("tillable_area") or f.get("calculated_area") or 0.0
        field_name_map[fid_i] = f.get("name") or f.get("title") or f"field_{fid_i}"
    try:
        from svodki_sanity import register_field_areas as _reg_fa
        _reg_fa(field_area_map)
    except ImportError:
        pass
    # merge_firm_map=True — когда группы качаются параллельно с 1.1: вызывающий один раз
    # сбрасывает карту (reset_field_firm_resolution) до старта, иначе полный пересбор
    # здесь затёр бы поля, уже домерженные группами. По умолчанию — прежнее поведение.
    _rebuild_field_firm_resolution(
        fields_all, get_all_page("field_groups"), merge=merge_firm_map,
    )
    await _wait_reference_data("1.1")

    DAY_START_HOUR = 6
    collect_debug_sheets = REPORT1_ENABLE_DEBUG_SHEETS
    today_task_ids = {t["id"] for t in tasks_today}
    print(f"    [1.1] Заданий за текущую смену (API end_time): {len(today_task_ids)}")

    per_firm_machine: dict[str, dict[int, dict]] = {firm: {} for firm in AGROFIRMS}
    season_daily_area: dict[str, dict[str, float]] = {firm: defaultdict(float) for firm in AGROFIRMS}
    season_daily_machines: dict[str, dict[str, set[int]]] = {
        firm: defaultdict(set) for firm in AGROFIRMS
    }
    season_field_cov: dict[str, dict[int, float]] = {
        firm: defaultdict(float) for firm in AGROFIRMS
    }
    season_crop_cov: dict[str, dict[str, float]] = {
        firm: defaultdict(float) for firm in AGROFIRMS
    }
    cmp_mtfmi: dict[int, float] = defaultdict(float)

    # --- Сутки: как 3_main build_all_firm_stats (tasks_today) ---
    _cap = None
    try:
        from proverka.task_facts import bind_capture

        _cap = bind_capture()
    except Exception:
        _cap = None
    for t in tasks_today:
        mid = t.get("machine_id")
        items = mtfmi_map.get(t["id"], [])
        firm_items: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            fid = item.get("field_id")
            if fid is None:
                continue
            fid_i = int(fid)
            firm = get_firm_for_field(fid_i)
            if firm:
                firm_items[firm].append(item)
        if not firm_items:
            continue

        total_cov_in_window = 0.0
        for item in items:
            for entry in (item.get("covered_area_hourly") or []):
                h = parse_dt(entry[0])
                if h and h < cutoff:
                    total_cov_in_window += entry[1] or 0

        for firm, f_items in firm_items.items():
            cov = 0.0
            track_area = 0.0
            work_distance_km = 0.0
            field_cov_today: dict[int, float] = defaultdict(float)
            for item in f_items:
                hourly = item.get("covered_area_hourly") or []
                cov_item_total = sum(e[1] or 0 for e in hourly)
                cov_item_in = 0.0
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and h < cutoff:
                        cov_item_in += entry[1] or 0
                cov += cov_item_in
                fid = item.get("field_id")
                if fid and cov_item_in > 0:
                    field_cov_today[int(fid)] += cov_item_in
                ratio = cov_item_in / cov_item_total if cov_item_total > 0 else 0.0
                track_area += (item.get("covered_area_by_track") or 0) * ratio
                work_distance_km += ((item.get("work_distance") or 0) / 1000) * ratio
            if cov <= 0:
                continue

            pm = per_firm_machine[firm]
            if mid not in pm:
                pm[mid] = {
                    "cov_total": 0.0,
                    "work_dur": 0.0,
                    "track_area": 0.0,
                    "work_distance_km": 0.0,
                    "implement_width": 0.0,
                    "speeds": [],
                    "field_cov": defaultdict(float),
                }
            pm[mid]["cov_total"] += cov
            pm[mid]["track_area"] += track_area
            pm[mid]["work_distance_km"] += work_distance_km
            work_dur = t.get("work_duration") or 0
            road_move = t.get("movements_on_road_duration") or 0
            work_net = max(work_dur - road_move, 0)
            if total_cov_in_window > 0:
                pm[mid]["work_dur"] += work_net * (cov / total_cov_in_window)
            else:
                pm[mid]["work_dur"] += work_net
            if not pm[mid]["implement_width"]:
                imp = implements_map.get(t.get("implement_id"))
                w = (imp.get("width") if imp else None) or 0
                pm[mid]["implement_width"] = float(w)
            sp = t.get("average_speed_on_field")
            if sp and sp > 0:
                pm[mid]["speeds"].append(sp)
            for fid, fid_cov in field_cov_today.items():
                pm[mid]["field_cov"][fid] += fid_cov
                crop_name = _history_crop_for_field_items(f_items, int(fid))
                pm[mid].setdefault("crop_cov", defaultdict(float))
                pm[mid]["crop_cov"][crop_name] += fid_cov
                if _cap is not None and fid_cov > 0:
                    mrec = machines_map.get(mid) or {}
                    _cap[1](
                        (WORK_TYPE_NAME, firm, mid),
                        group=WORK_TYPE_NAME,
                        firm=firm,
                        crop=crop_name,
                        field_id=int(fid),
                        field=str(field_name_map.get(int(fid)) or ""),
                        task_id=int(t["id"]),
                        machine_id=int(mid) if mid is not None else None,
                        machine=str(mrec.get("name") or ""),
                        ha=fid_cov,
                        season=str(t.get("season") or ""),
                    )

    # --- Сезон: как 3_main — любая cov>0, без порога 0.5/10 ---
    for t in tasks_season:
        mid = t.get("machine_id")
        end_dt = parse_dt(t.get("end_time"))
        if not end_dt:
            continue
        anchor = datetime(
            end_dt.year, end_dt.month, end_dt.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
        )
        if end_dt < anchor:
            shift_date = (end_dt - timedelta(days=1)).date()
            shift_cutoff = anchor
        else:
            shift_date = end_dt.date()
            shift_cutoff = anchor + timedelta(days=1)
        if shift_date < PERIOD_START_DATE:
            continue

        day_key = shift_date.isoformat()
        items = mtfmi_map.get(t["id"], [])
        if not items:
            continue
        for item in items:
            fid = item.get("field_id")
            if fid is None:
                continue
            fid_i = int(fid)
            firm = get_firm_for_field(fid_i)
            if not firm:
                continue
            cov = 0.0
            for entry in (item.get("covered_area_hourly") or []):
                h = parse_dt(entry[0])
                if h and h < shift_cutoff:
                    cov += float(entry[1] or 0.0)
            if cov <= 0:
                continue
            season_daily_area[firm][day_key] += cov
            if mid is not None:
                season_daily_machines[firm][day_key].add(int(mid))
            # Факт по культурам — дни строго до даты отчёта (листы сводки).
            if shift_date < REPORT_DATE:
                season_field_cov[firm][fid_i] += cov
                from mtfmi_batch_fetch import UNDEFINED_CROP

                season_crop_cov[firm][_history_crop(item) or UNDEFINED_CROP] += cov
                if collect_debug_sheets:
                    cmp_mtfmi[fid_i] += cov

    today_area_by_field: dict[int, float] = defaultdict(float)
    _ok_mu: set[tuple] = set()
    for firm in AGROFIRMS:
        for mid, d in per_firm_machine[firm].items():
            if round(d["cov_total"], 2) < MIN_DETAIL_AREA_HA:
                continue
            _ok_mu.add((firm, mid))
            for fid, fid_cov in d["field_cov"].items():
                if fid_cov > 0:
                    today_area_by_field[int(fid)] += float(fid_cov)
    if _cap is not None:
        _cap[2](WORK_TYPE_NAME, _ok_mu)
    today_field_ids = set(today_area_by_field)
    print(f"    [1.1] Полей в сегодняшних machine_tasks: {len(today_field_ids)}")

    fact_total: dict[str, dict[str, float]] = {}
    for firm in AGROFIRMS:
        fact_total[firm] = {
            c: round(v, 4) for c, v in season_crop_cov[firm].items() if v
        }

    season_cov_total = sum(sum(by_fid.values()) for by_fid in season_field_cov.values())
    print(
        f"    [1.1] season_field_cov итого (mtfmi, shift_date < {REPORT_DATE}): "
        f"{season_cov_total:,.2f} га"
    )

    day_stats: dict[str, dict] = {}
    for firm in AGROFIRMS:
        pm = per_firm_machine[firm]
        da = season_daily_area[firm]
        season_sum = sum(da.values())
        # Как 3_main: все машины с cov>0 в день, без порога 10 га.
        machine_days = sum(
            len(season_daily_machines[firm].get(day, set()))
            for day, area in da.items()
            if area > 0
        )
        avg_season = round(season_sum / machine_days, 1) if machine_days else 0.0

        area_by_crop: dict[str, float] = defaultdict(float)
        today_by_field: dict[int, float] = defaultdict(float)
        for fid, fid_cov in today_area_by_field.items():
            if fid_cov <= 0 or get_firm_for_field(fid) != firm:
                continue
            today_by_field[fid] += fid_cov
        for mid, d in pm.items():
            if round(d["cov_total"], 2) < MIN_DETAIL_AREA_HA:
                continue
            for crop, ha in (d.get("crop_cov") or {}).items():
                if ha:
                    area_by_crop[crop] += ha

        if not pm:
            empty = _empty_stats()
            empty["avg_season"] = avg_season
            empty["season_sum"] = season_sum
            empty["machine_days"] = machine_days
            day_stats[firm] = empty
            continue

        detail_own: list[dict] = []
        detail_hired: list[dict] = []
        # Счётчик/детали/время — >= 10 га; площадь за сутки — >= 0.5 га (3_main).
        for mid, d in pm.items():
            if round(d["cov_total"], 2) < MIN_COUNT_AREA_HA:
                continue
            m = machines_map.get(mid, {})
            group = m.get("machine_group_id")
            cov = round(d["cov_total"], 2)
            secs = d["work_dur"]
            rate = round(cov / (secs / 3600), 2) if secs else 0.0
            speed = round(sum(d["speeds"]) / len(d["speeds"]) * 3.6) if d["speeds"] else 0
            w_real = d["implement_width"]
            l_pole = d["work_distance_km"]
            s_chist = d["track_area"]
            coef = round(s_chist / l_pole / w_real * 10 * 100) if w_real and l_pole else 0
            row_data = {
                "machine_id": mid,
                "machine_name": m.get("name", f"machine_{mid}"),
                "group": group,
                "area": cov,
                "track_area": round(s_chist, 2),
                "speed": speed,
                "work_secs": secs,
                "rate": rate,
                "coef": coef,
            }
            if _is_hired_machine_group(group):
                detail_hired.append(row_data)
            else:
                detail_own.append(row_data)

        area_today = round(
            sum(
                d["cov_total"] for d in pm.values()
                if round(d["cov_total"], 2) >= MIN_DETAIL_AREA_HA
            ),
            2,
        )
        hours_today = sum(
            d["work_dur"] for d in pm.values()
            if round(d["cov_total"], 2) >= MIN_COUNT_AREA_HA
        ) / 3600

        day_stats[firm] = {
            "n_mach": len(detail_own) + len(detail_hired),
            "n_hired": len(detail_hired),
            "area_today": area_today,
            "hours_today": round(hours_today, 1),
            "avg_season": avg_season,
            "season_sum": season_sum,
            "machine_days": machine_days,
            "area_by_crop": dict(area_by_crop),
            "today_by_field": dict(today_by_field),
            "detail_own": sorted(detail_own, key=lambda x: -x["area"]),
            "detail_hired": sorted(detail_hired, key=lambda x: -x["area"]),
        }
 
    # -------------------------------------------------------------------------
    # Подсчёт внесения удобрений по всем культурам FERT_TABLE_CROPS из agro_operations.
    # -------------------------------------------------------------------------
    print("    [1.1] Загружаем agro_operations для подсчёта удобрений по всем культурам...")
    fert_data: dict[str, dict[int, dict[str, float]]] = {
        firm: defaultdict(lambda: {"season": 0.0, "today": 0.0})
        for firm in AGROFIRMS
    }
    all_raps_fert_ids: dict[int, float] = defaultdict(float)
 
    _all_crops_fd: dict[str, dict[str, dict[int, dict[str, float]]]] = {
        crop: {firm: defaultdict(lambda: {"season": 0.0, "today": 0.0}) for firm in AGROFIRMS}
        for crop in FERT_TABLE_CROPS
    }
    _all_crops_fact:  dict[str, dict[str, float]] = {crop: defaultdict(float) for crop in FERT_TABLE_CROPS}
    _all_crops_today: dict[str, dict[str, float]] = {crop: defaultdict(float) for crop in FERT_TABLE_CROPS}
 
    fact_agro_fields:       dict[str, dict[int, float]] = {firm: {} for firm in AGROFIRMS}
    fact_agro_today_fields: dict[str, dict[int, float]] = {firm: {} for firm in AGROFIRMS}
 
    cmp_agro: dict[int, dict] = {}
    today_ops_detail: list[dict] = []
 
    try:
        ops_all = get_fert_agro_operations()
        print(f"    [1.1] agro_operations загружено всего за сезон: {len(ops_all)}")
 
        SKIP_STATUSES = {"canceled"}
        ops: list[dict] = []
        for op in ops_all:
            status = op.get("status", "")
            if status in SKIP_STATUSES:
                continue
            completed = float(op.get("completed_area") or 0.0)
            covered   = float(op.get("covered_area")   or 0.0)
            if status == "planned":
                if covered <= 0:
                    continue
            else:
                if completed <= 0 and covered <= 0:
                    continue
            ops.append(op)
        print(f"    [1.1] После фильтрации (covered>0 или completed>0): {len(ops)}")
 
        seen_op_ids: set[int] = set()
        all_fert_ops: list[dict] = []
        today_ops_area_by_field: dict[int, float] = defaultdict(float)
 
        from mtfmi_batch_fetch import crop_of_history_id, remember_history_crops

        hist_ids = {
            int(op["history_item_id"])
            for op in ops
            if op.get("history_item_id") is not None
        }
        if hist_ids:
            await remember_history_crops(
                client, BASE_URL,
                {0: [{"history_item_id": hid} for hid in hist_ids]},
                sem,
            )

        for op in ops:
            op_id = op.get("id")
            if op_id and op_id in seen_op_ids:
                continue
            if op_id:
                seen_op_ids.add(op_id)
 
            field_id = op.get("field_id")
            if not field_id:
                continue
            crop = crop_of_history_id(op.get("history_item_id"))
            if crop not in FERT_TABLE_CROPS:
                continue
            firm = get_firm_for_field(field_id)
            if not firm:
                continue
 
            area = float(op.get("completed_area") or 0.0)
            if area <= 0:
                area = float(op.get("covered_area") or 0.0)
            if area <= 0:
                continue
 
            op_dt = parse_dt(
                op.get("completed_datetime")
                or op.get("actual_start_datetime")
            )
            if op_dt and op_dt < datetime(PERIOD_START_DATE.year, PERIOD_START_DATE.month,
                                          PERIOD_START_DATE.day, tzinfo=TZ_MSK):
                continue
 
            status = op.get("status", "")
            if status in ("done", "in_progress"):
                op_in_shift = bool(op_dt and day_start <= op_dt < cutoff)
                is_today = op_in_shift
            else:  # planned
                # Для planned суточный объём определяем только по mtfmi за смену.
                is_today = field_id in today_field_ids
 
            fert_ids_in_op: list[int] = []
            for ami in (op.get("application_mix_items") or []):
                if ami.get("applicable_type") == "Fertilizer":
                    fid_fert = ami.get("applicable_id")
                    if fid_fert and fid_fert not in fert_ids_in_op:
                        fert_ids_in_op.append(fid_fert)
 
            if not fert_ids_in_op:
                if REPORT1_VERBOSE_OP_LOGS:
                    print(
                        f"      [SKIP] op_id={op_id} firm={firm} field={field_id} "
                        f"status={status} area={area} — нет удобрений в application_mix_items "
                        f"(raw items: {op.get('application_mix_items')})"
                    )
                continue
 
            all_fert_ops.append({
                "op_id": op_id,
                "field_id": field_id,
                "firm": firm,
                "crop": crop,
                "status": status,
                "op_dt": op_dt,
                "area": area,
                "is_today": is_today,
                "fert_ids_in_op": fert_ids_in_op,
                "raw_op": op,
            })
            if is_today:
                today_ops_area_by_field[field_id] += area
 
        for op_info in all_fert_ops:
            op_id          = op_info["op_id"]
            field_id       = op_info["field_id"]
            firm           = op_info["firm"]
            crop           = op_info["crop"]
            status         = op_info["status"]
            op_dt          = op_info["op_dt"]
            area           = op_info["area"]
            is_today       = op_info["is_today"]
            fert_ids_in_op = op_info["fert_ids_in_op"]
            op             = op_info["raw_op"]
 
            today_area = 0.0
            if is_today:
                # Суточная площадь берётся из mtfmi (covered_area_hourly < cutoff),
                # масштабируется пропорционально доле операции по данному полю — как в compact.
                field_today_total = today_area_by_field.get(field_id, 0.0)
                field_ops_total   = today_ops_area_by_field.get(field_id, 0.0)
                if field_today_total > 0 and field_ops_total > 0:
                    today_area = field_today_total * (area / field_ops_total)
 
            share_count = max(len(fert_ids_in_op), 1)
            area_share  = area / share_count
            today_share = today_area / share_count if today_area > 0 else 0.0
            for fid_fert in fert_ids_in_op:
                if crop == "Рапс озимый":
                    fert_data[firm][fid_fert]["season"] += area_share
                    if is_today and today_area > 0:
                        fert_data[firm][fid_fert]["today"] += today_share
                    all_raps_fert_ids[fid_fert] += area_share
                _all_crops_fd[crop][firm][fid_fert]["season"] += area_share
                if is_today and today_area > 0:
                    _all_crops_fd[crop][firm][fid_fert]["today"] += today_share
 
            # В сезонный факт включаем только операции строго до даты отчёта,
            # как season_field_cov (shift_date < REPORT_DATE).
            op_date = op_dt.date() if op_dt else None
            if op_date is None or op_date < REPORT_DATE:
                if field_id not in fact_agro_fields[firm]:
                    fact_agro_fields[firm][field_id] = area
                else:
                    fact_agro_fields[firm][field_id] += area
            if is_today and today_area > 0:
                if field_id not in fact_agro_today_fields[firm]:
                    fact_agro_today_fields[firm][field_id] = today_area
 
            _all_crops_fact[crop][firm]  += area
            if is_today and today_area > 0:
                _all_crops_today[crop][firm] += today_area
 
            if REPORT1_VERBOSE_OP_LOGS:
                fert_names_str = ", ".join(
                    fertilizer_name_map.get(f, str(f)) for f in fert_ids_in_op
                )
                print(
                    f"      [agro_op] op_id={op_id} firm={firm} field={field_id} "
                    f"area={area} today={is_today} ferts=[{fert_names_str}]"
                )
 
            if collect_debug_sheets and is_today:
                today_ops_detail.append({
                    "firm":       firm,
                    "field_id":   field_id,
                    "field_name": field_name_map.get(field_id, f"field_{field_id}"),
                    "crop":       crop,
                    "op_id":      op_id,
                    "status":     op.get("status", ""),
                    "area_op":    area,
                    "area_today": today_area,
                    "ferts":      list(fert_ids_in_op),
                    "op_dt":      op_dt,
                })
 
            if collect_debug_sheets:
                if field_id not in cmp_agro:
                    cmp_agro[field_id] = {
                        "area":  0.0,
                        "ferts": defaultdict(float),
                        "firm":  firm,
                        "crop":  crop,
                        "ops":   [],
                    }
                cmp_agro[field_id]["area"] += area
                cmp_agro[field_id]["ops"].append({
                    "op_id":    op_id,
                    "area":     area,
                    "ferts":    list(fert_ids_in_op),
                    "is_today": is_today,
                })
                for fid_fert in fert_ids_in_op:
                    cmp_agro[field_id]["ferts"][fid_fert] += area_share
 
    except Exception as e:
        print(f"    [1.1] Ошибка при загрузке agro_operations: {e}")
 
    for firm in fert_data:
        for fid_fert in fert_data[firm]:
            fert_data[firm][fid_fert]["season"] = round(fert_data[firm][fid_fert]["season"], 2)
            fert_data[firm][fid_fert]["today"]  = round(fert_data[firm][fid_fert]["today"],  2)
 
    global all_crops_fert_data, all_crops_fact_agro, all_crops_fact_today
    all_crops_fert_data = {
        crop: {
            firm: {
                fid: {"season": round(d["season"], 2), "today": round(d["today"], 2)}
                for fid, d in _all_crops_fd[crop][firm].items()
            }
            for firm in AGROFIRMS
        }
        for crop in FERT_TABLE_CROPS
    }
    all_crops_fact_agro  = {crop: dict(v) for crop, v in _all_crops_fact.items()}
    all_crops_fact_today = {crop: dict(v) for crop, v in _all_crops_today.items()}
 
    global raps_fert_order
    raps_fert_order = [
        (fid, fertilizer_name_map.get(fid, f"fert_{fid}"))
        for fid, _ in sorted(all_raps_fert_ids.items(), key=lambda x: -x[1])
    ]
    print(f"    [1.1] Удобрений по Рапс озимый: {len(raps_fert_order)}")
    if REPORT1_VERBOSE_OP_LOGS:
        for fid, fname in raps_fert_order:
            print(f"      {fid}: {fname} — {round(all_raps_fert_ids[fid], 1)} га")
 
    fert_data_plain: dict[str, dict[int, dict[str, float]]] = {
        firm: dict(fert_data[firm]) for firm in AGROFIRMS
    }
 
    # fact_total["Рапс озимый"] уже заполнен через season_field_cov (mtfmi) —
    # перезапись через fact_agro_fields (agro_operations) убрана, так как
    # agro_operations суммирует несколько операций на одно поле и даёт завышенный итог.

    return fact_total, day_stats, fert_data_plain, dict(cmp_mtfmi), cmp_agro, today_ops_detail
 


# =============================================================================
# ДАННЫЕ ЗА СУТКИ — обобщённая функция для любого набора work_type_id
# =============================================================================

def _harvest_cutoff_buffer_hours() -> int:
    """Как CUTOFF_BUFFER_HOURS в 10_main_harvest (по умолчанию 12 ч)."""
    raw = (os.environ.get("SVODKI_REPORT10_CUTOFF_BUFFER_HOURS") or "12").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 12


_GROUP_TIMING: dict[str, dict[str, float]] = {}


def _gmark(group_name: str, key: str, t0: float) -> float:
    """Накопить время фазы группы (tasks / mtfmi / res); возвращает now."""
    now = time.monotonic()
    d = _GROUP_TIMING.setdefault(group_name, {})
    d[key] = d.get(key, 0.0) + (now - t0)
    return now


async def fetch_day_stats_for_work_types(
    client:        httpx.AsyncClient,
    sem:           asyncio.Semaphore,
    day_start:     datetime,
    cutoff:        datetime,
    work_type_ids: list[int],
    group_name:    str,
) -> dict[str, dict]:
    """Обёртка с таймером: одна строка «[timing] группа …» с разбивкой по фазам."""
    _GROUP_TIMING[group_name] = {}
    t0 = time.monotonic()
    try:
        return await _fetch_day_stats_for_work_types_impl(
            client, sem, day_start, cutoff, work_type_ids, group_name,
        )
    finally:
        total = time.monotonic() - t0
        d = _GROUP_TIMING.get(group_name, {})
        net = d.get("tasks", 0.0) + d.get("mtfmi", 0.0) + d.get("res", 0.0)
        print(
            f"  [timing] группа {group_name}: всего {total:.1f} с "
            f"(machine_tasks {d.get('tasks', 0.0):.1f}, mtfmi {d.get('mtfmi', 0.0):.1f}, "
            f"поля/машины {d.get('res', 0.0):.1f}, расчёт ~{max(total - net, 0.0):.1f})",
            flush=True,
        )


async def _fetch_day_stats_for_work_types_impl(
    client:        httpx.AsyncClient,
    sem:           asyncio.Semaphore,
    day_start:     datetime,
    cutoff:        datetime,
    work_type_ids: list[int],
    group_name:    str,
) -> dict[str, dict]:

    is_harvest = group_name == HARVEST_GRAIN_GROUP_NAME
    if is_harvest:
        # Площадь 5.0 — как 10_main: зерновые wt + скашивание на кукурузе на силос.
        work_type_ids = sorted(AREA_TASK_WT_IDS)

    url              = f"{BASE_URL}/machine_tasks"
    # Запрос с запасом с 01.01; нижняя граница смен — group_period_start.
    season_start_str = datetime(REPORT_DATE.year, 1, 1, tzinfo=TZ_MSK).strftime("%Y-%m-%dT%H:%M:%S")
    # Уборка: +buffer к cutoff для end_time (как 10_main_harvest, по умолчанию 12 ч).
    _harvest_buf = _harvest_cutoff_buffer_hours() if is_harvest else 0
    fetch_cutoff = cutoff + timedelta(hours=_harvest_buf) if is_harvest else cutoff
    cutoff_str       = fetch_cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    DAY_START_HOUR   = 6
    group_period_start = _group_period_start(group_name)
    group_period_end = _group_period_end_if_set(group_name)
    area_min_ha = _detail_area_min_ha(group_name)
    is_plow = group_name == PLOW_GROUP_NAME
    _fact_seasons = _allowed_task_seasons_for_group(group_name)

    def _shift_window(end_time_raw):
        end_dt = parse_dt(end_time_raw)
        if end_dt is None:
            return None
        cal_date = end_dt.date()
        anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
        if end_dt < anchor:
            cal_date = cal_date - timedelta(days=1)
            anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                                DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
        return cal_date, anchor, anchor + timedelta(days=1)

    def _task_wt(task: dict) -> int | None:
        try:
            return int(task.get("work_type_id"))
        except (TypeError, ValueError):
            return None

    def _counts_as_grain_machine(task: dict) -> bool:
        """Счётчик комбайнов — как 11_main GRAIN_WT_IDS (без 6/7)."""
        if not is_harvest:
            return True
        wt = _task_wt(task)
        return wt is not None and wt in GRAIN_HARVEST_WT_IDS

    print(f"    [{group_name}] Загружаем machine_tasks за сезон (batch)...")
    from machine_tasks_batch_fetch import fetch_machine_tasks_by_wt_async

    _tg = time.monotonic()
    tasks_season = await fetch_machine_tasks_by_wt_async(
        fetch_pages, client, BASE_URL, work_type_ids,
        {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_str},
        sem, log_prefix=f"[{group_name}] ",
    )
    _gmark(group_name, "tasks", _tg)

    tasks_with_area = tasks_season  # не фильтруем по covered_area: площадь из covered_area_hourly
    print(f"    [{group_name}] Заданий за сезон: {len(tasks_with_area)}")

    if not tasks_with_area:
        return {firm: _empty_stats() for firm in AGROFIRMS}

    use_task_area = group_name in SPRAY_TASK_GROUPS
    tasks_by_id = {t["id"]: t for t in tasks_with_area}

    # Задания за текущую смену.
    # Уборка: end_time в [day_start, cutoff+buffer); hourly режется по cutoff (как 10_main).
    # Остальные: окно смены [D 06:00, D+1 06:00).
    today_task_ids: set[int] = set()
    if is_harvest:
        cutoff_ext = cutoff + timedelta(hours=_harvest_buf)
        for t in tasks_with_area:
            end_dt = parse_dt(t.get("end_time"))
            if end_dt is None:
                continue
            if day_start <= end_dt < cutoff_ext:
                today_task_ids.add(t["id"])
    else:
        for t in tasks_with_area:
            if use_task_area and (t.get("covered_area") or 0) <= 0:
                continue
            sw = _shift_window(t.get("end_time"))
            if sw is None:
                continue
            _, shift_ds, shift_co = sw
            if shift_ds >= day_start and shift_co <= cutoff:
                today_task_ids.add(t["id"])
    print(f"    [{group_name}] Заданий за текущую смену: {len(today_task_ids)}")

    print(f"    [{group_name}] Загружаем mtfmi...")
    from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks

    _tg = time.monotonic()
    mtfmi_map = await fetch_mtfmi_map_for_tasks(
        client, sem, BASE_URL, [t["id"] for t in tasks_with_area],
        log_prefix=f"    [{group_name}] ",
    )
    _gmark(group_name, "mtfmi", _tg)

    from report_resources_fetch import (
        collect_report_resource_ids,
        load_fields_list_async,
        load_implements_dict_async,
        load_machines_map_async,
    )

    global machines_map, implements_map
    field_ids, machine_ids, implement_ids = collect_report_resource_ids(
        tasks_with_area, mtfmi_map,
    )
    if field_ids or machine_ids or implement_ids:
        print(
            f"    [{group_name}] Ресурсы: полей={len(field_ids)}, "
            f"машин={len(machine_ids)}, орудий={len(implement_ids)}"
        )
        _tg = time.monotonic()
        fields_all, new_machines, new_implements = await asyncio.gather(
            load_fields_list_async(client, BASE_URL, field_ids, sem),
            load_machines_map_async(client, BASE_URL, machine_ids, sem),
            load_implements_dict_async(client, BASE_URL, implement_ids, sem),
        )
        _gmark(group_name, "res", _tg)
        machines_map.update(new_machines)
        implements_map.update(new_implements)
        field_to_fg.update({
            int(f["id"]): f.get("field_group_id")
            for f in fields_all if f.get("id") is not None
        })
        for f in fields_all:
            fid = f.get("id")
            if fid is None:
                continue
            fid_i = int(fid)
            field_area_map[fid_i] = f.get("tillable_area") or f.get("calculated_area") or 0.0
            field_name_map[fid_i] = f.get("name") or f.get("title") or f"field_{fid_i}"
        try:
            from svodki_sanity import register_field_areas as _reg_fa
            _reg_fa(field_area_map)
        except ImportError:
            pass
        _rebuild_field_firm_resolution(
            fields_all, get_all_page("field_groups"), merge=True,
        )

    await _wait_reference_data(group_name)

    from task_area_shared import task_cov_by_firm, task_field_cov_for_firm

    def _task_row_allowed(item: dict) -> bool:
        return _row_matches_group_crop(group_name, item)

    def _task_field_ok(fid_i: int) -> bool:
        return _spray_field_ok(fid_i, group_name)

    def _field_allowed_for_item(fid_i: int) -> bool:
        # Вспашка — все поля с фирмой (без available). Культура строки не отсекает поле.
        if is_plow or group_name in TILLAGE_GROUPS:
            return bool(get_firm_for_field(fid_i))
        if group_name in FACT_BY_ALL_FIELDS_GROUPS:
            return bool(get_firm_for_field(fid_i))
        if group_name in FIRM_ONLY_NO_CROP_GROUPS:
            return bool(get_firm_for_field(fid_i))
        if fid_i not in available_field_ids:
            return False
        return True

    # --- Сезонная статистика и машины ---
    # Сырые площади по машине-дню; финальная площадь — после порога (как _flush_area_from_machines).
    season_machine_day_cov: dict[str, dict[str, dict[int, float]]] = {
        f: defaultdict(lambda: defaultdict(float)) for f in AGROFIRMS
    }
    season_machine_day_fields: dict[str, dict[str, dict[int, list[tuple[int, float]]]]] = {
        f: defaultdict(lambda: defaultdict(list)) for f in AGROFIRMS
    }
    # Вспашка: площадь по полям без порога на машину (как field_day_area в 2_main).
    plow_field_day_area: dict[str, dict[str, dict[int, float]]] = {
        f: defaultdict(lambda: defaultdict(float)) for f in AGROFIRMS
    }
    season_daily_area:     dict[str, dict[str, float]]    = {f: defaultdict(float) for f in AGROFIRMS}
    season_daily_machines: dict[str, dict[str, set[int]]] = {f: defaultdict(set)   for f in AGROFIRMS}
    season_field_cov:      dict[str, dict[int, float]]    = {f: defaultdict(float) for f in AGROFIRMS}
    per_firm_machine:      dict[str, dict[int, dict]]     = {f: {} for f in AGROFIRMS}
    _cap = None
    if not is_harvest:
        try:
            from proverka.task_facts import bind_capture

            _cap = bind_capture()
        except Exception:
            _cap = None

    if is_plow:
        from collections import Counter
        _plow_season_counts = Counter(
            str(t.get("season") or t.get("year") or "").strip() or "<пусто>"
            for t in tasks_with_area
        )
        print(
            f"    [вспашка] сезон заданий (нужен {PLOW_TASK_SEASON}): "
            f"{dict(_plow_season_counts)}"
        )

    for t in tasks_with_area:
        tid = t["id"]
        tseason = str(t.get("season") or t.get("year") or "").strip()
        # Сутки и сезон — сезоны этой операции из редактора планов.
        if _fact_seasons is not None and tseason not in _fact_seasons:
            continue
        mid = t.get("machine_id")
        items = mtfmi_map.get(tid, [])
        sw = _shift_window(t.get("end_time"))
        if sw is None:
            continue
        shift_date, shift_ds, shift_co = sw
        if shift_date < group_period_start:
            continue
        if group_period_end is not None and shift_date > group_period_end:
            continue
        grain_mach = _counts_as_grain_machine(t)

        if use_task_area:
            if shift_date < REPORT_DATE:
                firm_cov_season = task_cov_by_firm(
                    t,
                    items,
                    cutoff=shift_co,
                    window_start=shift_ds,
                    field_ok=_task_field_ok,
                    firm_for_field=get_firm_for_field,
                    parse_dt=parse_dt,
                    row_allowed=_task_row_allowed,
                )
                for firm, cov_firm in firm_cov_season.items():
                    if cov_firm <= 0 or mid is None:
                        continue
                    day_key = shift_date.isoformat()
                    season_machine_day_cov[firm][day_key][mid] += cov_firm
                    field_cov = task_field_cov_for_firm(
                        t,
                        items,
                        firm,
                        cov_firm,
                        cutoff=shift_co,
                        window_start=shift_ds,
                        field_ok=_task_field_ok,
                        firm_for_field=get_firm_for_field,
                        parse_dt=parse_dt,
                        row_allowed=_task_row_allowed,
                    )
                    for fid, fcov in field_cov.items():
                        if fcov > 0:
                            season_machine_day_fields[firm][day_key][mid].append(
                                (int(fid), float(fcov))
                            )

            if tid in today_task_ids:
                firm_cov_today = task_cov_by_firm(
                    t,
                    items,
                    cutoff=cutoff,
                    window_start=day_start,
                    field_ok=_task_field_ok,
                    firm_for_field=get_firm_for_field,
                    parse_dt=parse_dt,
                    row_allowed=_task_row_allowed,
                )
                if firm_cov_today:
                    work_dur = t.get("work_duration") or 0
                    road_dur = t.get("movements_on_road_duration") or 0
                    work_net = max(work_dur - road_dur, 0)
                    task_raw_total = sum(firm_cov_today.values()) or 1.0
                    for firm, cov_firm_today in firm_cov_today.items():
                        if cov_firm_today <= 0:
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
                                "field_cov":        defaultdict(float),
                                "count_machine":    True,
                            }
                        pm[mid]["cov_total"] += cov_firm_today
                        pm[mid]["work_dur"] += work_net * (cov_firm_today / task_raw_total)
                        field_cov = task_field_cov_for_firm(
                            t,
                            items,
                            firm,
                            cov_firm_today,
                            cutoff=cutoff,
                            window_start=day_start,
                            field_ok=_task_field_ok,
                            firm_for_field=get_firm_for_field,
                            parse_dt=parse_dt,
                            row_allowed=_task_row_allowed,
                        )
                        for fid, fcov in field_cov.items():
                            pm[mid]["field_cov"][fid] += fcov
                            pm[mid].setdefault("crop_cov", defaultdict(float))
                            pm[mid]["crop_cov"][_history_crop_for_field_items(items, int(fid))] += fcov
                            if _cap is not None and fcov > 0:
                                mrec = machines_map.get(mid) or {}
                                _cap[1](
                                    (group_name, firm, mid),
                                    group=group_name,
                                    firm=firm,
                                    crop=_history_crop_for_field_items(items, int(fid)),
                                    field_id=int(fid),
                                    task_id=int(t["id"]),
                                    machine_id=int(mid) if mid is not None else None,
                                    machine=str(mrec.get("name") or ""),
                                    ha=fcov,
                                    season=str(t.get("season") or ""),
                                )
                        if not pm[mid]["implement_width"]:
                            imp = implements_map.get(t.get("implement_id"))
                            w   = (imp.get("width") if imp else None) or 0
                            pm[mid]["implement_width"] = float(w)
                        sp = t.get("average_speed_on_field")
                        if sp and sp > 0 and sp not in pm[mid]["speeds"]:
                            pm[mid]["speeds"].append(sp)
            continue

        # Площадь по смене через hourly < shift_co, без нижней границы
        # (fallback на covered_area если нет hourly — для боронования и посева)
        firm_items_map: dict[str, list[tuple]] = defaultdict(list)
        for item in items:
            fid = item.get("field_id")
            if not fid:
                continue
            fid_i = int(fid)
            if not _field_allowed_for_item(fid_i):
                continue
            if not _row_matches_group_crop(group_name, item):
                continue
            firm = get_firm_for_field(fid_i)
            if not firm:
                continue
            hourly = item.get("covered_area_hourly") or []
            cov = 0.0
            for entry in hourly:
                h = parse_dt(entry[0])
                if h and h < shift_co:
                    cov += entry[1] or 0
            if not hourly:
                cov = item.get("covered_area") or 0
            if cov > 0:
                firm_items_map[firm].append((item, float(cov)))

        for firm, fitems_cov in firm_items_map.items():
            # Сезонный факт — только дни строго до даты отчёта, как в эталоне.
            if shift_date < REPORT_DATE:
                cov_firm = sum(cov for _, cov in fitems_cov)
                day_key = shift_date.isoformat()
                if is_plow:
                    for item, cov_item in fitems_cov:
                        fid = item.get("field_id")
                        if fid and cov_item > 0:
                            plow_field_day_area[firm][day_key][int(fid)] += cov_item
                            season_field_cov[firm][int(fid)] += cov_item
                    if cov_firm > 0 and mid is not None:
                        season_machine_day_cov[firm][day_key][mid] += cov_firm
                elif cov_firm > 0 and mid is not None:
                    season_machine_day_cov[firm][day_key][mid] += cov_firm
                    for item, cov_item in fitems_cov:
                        fid = item.get("field_id")
                        if fid and cov_item > 0:
                            season_machine_day_fields[firm][day_key][mid].append(
                                (int(fid), float(cov_item))
                            )

        if tid not in today_task_ids:
            continue

        # Суточная детализация по машинам — площадь через h < cutoff
        for firm, fitems_cov in firm_items_map.items():
            cov_firm_today = 0.0
            field_cov_today: dict[int, float] = defaultdict(float)
            for item, _ in fitems_cov:
                fid    = item.get("field_id")
                hourly = item.get("covered_area_hourly") or []
                cov_h  = 0.0
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and h < cutoff:
                        cov_h += entry[1] or 0
                if not hourly:
                    cov_h = item.get("covered_area") or 0
                if cov_h > 0:
                    cov_firm_today += cov_h
                    if fid:
                        field_cov_today[fid] += cov_h

            if cov_firm_today <= 0:
                continue

            total_cov_in_window = 0.0
            for item in items:
                hourly = item.get("covered_area_hourly") or []
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and h < cutoff:
                        total_cov_in_window += entry[1] or 0
                if not hourly:
                    total_cov_in_window += item.get("covered_area") or 0

            work_dur = t.get("work_duration") or 0
            road_dur = t.get("movements_on_road_duration") or 0
            work_net = max(work_dur - road_dur, 0)

            pm = per_firm_machine[firm]
            if mid not in pm:
                pm[mid] = {
                    "cov_total":        0.0,
                    "grain_area":       0.0,
                    "work_dur":         0.0,
                    "track_area":       0.0,
                    "work_distance_km": 0.0,
                    "implement_width":  0.0,
                    "speeds":           [],
                    "field_cov":        defaultdict(float),
                    "count_machine":    grain_mach,
                }
            else:
                # Уборка: машина считается, если хотя бы одно задание — зерновой wt (без 6/7).
                pm[mid]["count_machine"] = pm[mid].get("count_machine", True) or grain_mach

            pm[mid]["cov_total"] += cov_firm_today
            if grain_mach:
                pm[mid]["grain_area"] += cov_firm_today
            if total_cov_in_window > 0:
                pm[mid]["work_dur"] += work_net * (cov_firm_today / total_cov_in_window)
            else:
                pm[mid]["work_dur"] += work_net

            for item, _ in fitems_cov:
                hourly = item.get("covered_area_hourly") or []
                cov_item_total = sum(e[1] or 0 for e in hourly)
                cov_item_in    = 0.0
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and h < cutoff:
                        cov_item_in += entry[1] or 0
                if not hourly:
                    cov_item_total = cov_item_in = item.get("covered_area") or 0
                ratio = cov_item_in / cov_item_total if cov_item_total > 0 else 0.0
                pm[mid]["track_area"]       += (item.get("covered_area_by_track") or 0) * ratio
                pm[mid]["work_distance_km"] += ((item.get("work_distance") or 0) / 1000) * ratio

            if not pm[mid]["implement_width"]:
                imp = implements_map.get(t.get("implement_id"))
                w   = (imp.get("width") if imp else None) or 0
                pm[mid]["implement_width"] = float(w)

            sp = t.get("average_speed_on_field")
            if sp and sp > 0 and sp not in pm[mid]["speeds"]:
                pm[mid]["speeds"].append(sp)

            for fid, fid_cov in field_cov_today.items():
                if fid_cov > 0:
                    pm[mid]["field_cov"][fid] += fid_cov
                    pm[mid].setdefault("crop_cov", defaultdict(float))
                    pm[mid]["crop_cov"][
                        _history_crop_for_field_items([it for it, _c in fitems_cov], int(fid))
                    ] += fid_cov
                    if _cap is not None:
                        mrec = machines_map.get(mid) or {}
                        _cap[1](
                            (group_name, firm, mid),
                            group=group_name,
                            firm=firm,
                            crop=_history_crop_for_field_items(items, int(fid)),
                            field_id=int(fid),
                            task_id=int(t["id"]),
                            machine_id=int(mid) if mid is not None else None,
                            machine=str(mrec.get("name") or ""),
                            ha=fid_cov,
                            season=str(t.get("season") or ""),
                        )

    # Сезонная площадь grain-only для уборки (avg/machines как 11_main).
    season_daily_area_grain: dict[str, dict[str, float]] = {
        f: defaultdict(float) for f in AGROFIRMS
    }
    season_daily_machines_grain: dict[str, dict[str, set[int]]] = {
        f: defaultdict(set) for f in AGROFIRMS
    }
    if is_harvest:
        for t in tasks_with_area:
            if not _counts_as_grain_machine(t):
                continue
            mid = t.get("machine_id")
            items = mtfmi_map.get(t["id"], [])
            sw = _shift_window(t.get("end_time"))
            if sw is None:
                continue
            shift_date, _, shift_co = sw
            if shift_date < group_period_start or shift_date >= REPORT_DATE:
                continue
            if group_period_end is not None and shift_date > group_period_end:
                continue
            day_key = shift_date.isoformat()
            for item in items:
                fid = item.get("field_id")
                if not fid:
                    continue
                fid_i = int(fid)
                if not _field_allowed_for_item(fid_i):
                    continue
                if not _row_matches_group_crop(group_name, item):
                    continue
                firm = get_firm_for_field(fid_i)
                if not firm:
                    continue
                hourly = item.get("covered_area_hourly") or []
                cov = 0.0
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and h < shift_co:
                        cov += float(entry[1] or 0)
                if not hourly:
                    cov = float(item.get("covered_area") or 0)
                if cov <= 0:
                    continue
                season_daily_area_grain[firm][day_key] += cov
                if mid is not None:
                    season_daily_machines_grain[firm][day_key].add(int(mid))

    # Финальная площадь сезона: порог на машину-день (вспашка — по полям, без порога).
    for firm in AGROFIRMS:
        if is_plow:
            for day_key, by_fid in plow_field_day_area[firm].items():
                day_area = sum(float(v) for v in by_fid.values() if float(v) > 0)
                if day_area > 0:
                    season_daily_area[firm][day_key] = day_area
            for day_key, by_mid in season_machine_day_cov[firm].items():
                for mid, total in by_mid.items():
                    if _machine_qualifies_for_area(total, 0.0):
                        season_daily_machines[firm][day_key].add(mid)
            continue
        if is_harvest:
            season_daily_area[firm] = season_daily_area_grain[firm]
            season_daily_machines[firm] = season_daily_machines_grain[firm]
            continue
        for day_key, by_mid in season_machine_day_cov[firm].items():
            day_area = 0.0
            for mid, total in by_mid.items():
                if not _machine_qualifies_for_area(total, area_min_ha):
                    continue
                day_area += float(total)
                season_daily_machines[firm][day_key].add(mid)
                for fid, cov_item in season_machine_day_fields[firm][day_key].get(mid, []):
                    season_field_cov[firm][fid] += cov_item
            if day_area > 0:
                season_daily_area[firm][day_key] = day_area

    # Суточная площадь по полям — из машин, прошедших порог площади.
    today_area_by_field: dict[int, float] = defaultdict(float)
    for firm in AGROFIRMS:
        for mid, d in per_firm_machine[firm].items():
            if is_plow:
                for fid, fid_cov in d["field_cov"].items():
                    if fid_cov > 0:
                        today_area_by_field[int(fid)] += float(fid_cov)
            elif is_harvest:
                # Выраб. 5.0 — grain_area (11_main), без порога.
                if float(d.get("grain_area") or 0) <= 0:
                    continue
                for fid, fid_cov in d["field_cov"].items():
                    if fid_cov > 0:
                        today_area_by_field[int(fid)] += float(fid_cov)
            elif _machine_qualifies_for_area(d["cov_total"], area_min_ha):
                for fid, fid_cov in d["field_cov"].items():
                    if fid_cov > 0:
                        today_area_by_field[int(fid)] += float(fid_cov)
    if _cap is not None:
        if is_plow:
            _cap[2](group_name, None)
        else:
            _cap[2](
                group_name,
                {
                    (firm, mid)
                    for firm, pm in per_firm_machine.items()
                    for mid, d in pm.items()
                    if _machine_qualifies_for_area(d["cov_total"], area_min_ha)
                },
            )

    harvest_perf_10: tuple[
        dict[str, float],
        dict[str, set[int]],
        dict[str, dict[int, float]],
    ] | None = None
    if is_harvest:
        harvest_maps = _load_harvest_maps_from_main10()
        if harvest_maps:
            harvest_ftc, harvest_f2f = harvest_maps
            print(
                f"    [{group_name}] field_to_crop/field_to_firm (10_main): "
                f"{len(harvest_ftc)} / {len(harvest_f2f)} полей"
            )
        else:
            harvest_ftc, harvest_f2f = field_to_crop, None
        harvest_perf_10 = _harvest_perf_10_style(
            tasks_with_area,
            mtfmi_map,
            today_task_ids,
            cutoff,
            machines_map,
            day_start=day_start,
            field_to_crop_map=harvest_ftc,
            field_to_firm_map=harvest_f2f,
        )

    # --- Собираем результат ---
    result: dict[str, dict] = {}

    for firm in AGROFIRMS:
        pm = per_firm_machine[firm]
        da = season_daily_area[firm]
        dm = season_daily_machines[firm]
        smdc = season_machine_day_cov[firm]
        season_sum   = sum(da.values())
        _count_thr = _count_threshold_for_group(group_name)
        if is_harvest:
            # Как 11_main: все машины с grain_area > 0 в день.
            machine_days = sum(
                len(dm.get(d, set()))
                for d, area in da.items()
                if area > 0
            )
        else:
            machine_days = sum(
                sum(
                    1 for mid in dm.get(d, set())
                    if _machine_qualifies_for_area(smdc.get(d, {}).get(mid, 0.0), _count_thr)
                )
                for d, area in da.items()
                if area > 0
            )
        avg_season = round(season_sum / machine_days, 1) if machine_days else 0.0

        area_by_crop:   dict[str, float] = defaultdict(float)
        today_by_field: dict[int, float] = defaultdict(float)
        for fid, fid_cov in today_area_by_field.items():
            if fid_cov <= 0:
                continue
            if get_firm_for_field(fid) != firm:
                continue
            today_by_field[fid] += fid_cov
        for mid, d in pm.items():
            qualifies = (
                float(d.get("grain_area") or 0) > 0
                if is_harvest
                else _machine_qualifies_for_area(d["cov_total"], 0.0 if is_plow else area_min_ha)
            )
            if not qualifies:
                continue
            for crop, ha in (d.get("crop_cov") or {}).items():
                if ha:
                    area_by_crop[crop] += ha

        harvest_mids: set[int] = set()
        if is_harvest and harvest_perf_10 is not None:
            harvest_mids = harvest_perf_10[1].get(firm, set())

        if is_harvest and harvest_perf_10 is not None:
            h_area, _, _ = harvest_perf_10
            raw_area = float(h_area.get(firm, 0.0))
            area_today = round(raw_area, 2)
            # Итого «Обработано за сутки» суммирует today_by_field, не area_today.
            # Кукуруза на силос вне инвентаря 1.1 иначе даёт ~1103 вместо ~1171.
            today_by_field = {0: area_today} if area_today else {}
        elif is_harvest:
            area_today = round(
                sum(float(d.get("grain_area") or 0) for d in pm.values() if float(d.get("grain_area") or 0) > 0),
                2,
            )
        elif use_task_area:
            area_today = round(
                sum(
                    d["cov_total"] for d in pm.values()
                    if _machine_qualifies_for_area(d["cov_total"], area_min_ha)
                ),
                2,
            )
        else:
            area_today = sum(today_by_field.values())

        if is_harvest and harvest_perf_10 is not None:
            hours_today = sum(
                pm[mid]["work_dur"] for mid in harvest_mids if mid in pm
            ) / 3600
        elif is_harvest:
            hours_today = sum(
                d["work_dur"] for d in pm.values()
                if float(d.get("grain_area") or 0) > 0
            ) / 3600
        else:
            hours_today = sum(
                d["work_dur"] for mid, d in pm.items()
                if _machine_qualifies_for_area(d["cov_total"], _count_thr)
            ) / 3600

        detail_own:   list[dict] = []
        detail_hired: list[dict] = []
        for mid, d in pm.items():
            if is_harvest and harvest_perf_10 is not None:
                if int(mid) not in harvest_mids:
                    continue
                cov = round(float(harvest_perf_10[2][firm][int(mid)]), 2)
            elif is_harvest:
                if float(d.get("grain_area") or 0) <= 0:
                    continue
                cov = round(float(d["grain_area"]), 2)
            else:
                if not _machine_qualifies_for_area(d["cov_total"], _count_thr):
                    continue
                cov = round(d["cov_total"], 2)
            m     = machines_map.get(mid, {})
            group = m.get("machine_group_id")
            secs  = d["work_dur"]
            rate  = round(cov / (secs / 3600), 2) if secs else 0.0
            speed = round(sum(d["speeds"]) / len(d["speeds"]) * 3.6) if d["speeds"] else 0
            w_real  = d["implement_width"]
            l_pole  = d["work_distance_km"]
            s_chist = d["track_area"]
            coef    = round(s_chist / l_pole / w_real * 10 * 100) if w_real and l_pole else 0
            row_data = {
                "machine_id":   mid,
                "machine_name": m.get("name", f"machine_{mid}"),
                "group":        group,
                "area":         cov,
                "track_area":   round(s_chist, 2),
                "speed":        speed,
                "work_secs":    secs,
                "rate":         rate,
                "coef":         coef,
            }
            if _is_hired_machine_group(group):
                detail_hired.append(row_data)
            else:
                detail_own.append(row_data)

        if is_harvest and harvest_perf_10 is not None:
            _, _, h_mid_area = harvest_perf_10
            seen_mids = {r["machine_id"] for r in detail_own + detail_hired}
            for mid in harvest_mids:
                if mid in seen_mids:
                    continue
                cov = round(float(h_mid_area[firm][mid]), 2)
                m = machines_map.get(mid, {})
                group = m.get("machine_group_id")
                row_data = {
                    "machine_id":   mid,
                    "machine_name": m.get("name", f"machine_{mid}"),
                    "group":        group,
                    "area":         cov,
                    "track_area":   0.0,
                    "speed":        0,
                    "work_secs":    0,
                    "rate":         0.0,
                    "coef":         0,
                }
                if _is_hired_machine_group(group):
                    detail_hired.append(row_data)
                else:
                    detail_own.append(row_data)

        n_mach = (
            len(harvest_mids)
            if is_harvest and harvest_perf_10 is not None
            else len(detail_own) + len(detail_hired)
        )

        result[firm] = {
            "n_mach":         n_mach,
            "n_hired":        len(detail_hired),
            "area_today":     area_today,
            "hours_today":    round(hours_today, 1),
            "avg_season":     avg_season,
            "season_sum":     season_sum,
            "machine_days":   machine_days,
            "area_by_crop":   dict(area_by_crop),
            "today_by_field": dict(today_by_field),
            "detail_own":     sorted(detail_own,   key=lambda x: -x["area"]),
            "detail_hired":   sorted(detail_hired, key=lambda x: -x["area"]),
        }

    from svodki_daily_facts_area import apply_report1_area_today_facts

    result = apply_report1_area_today_facts(
        result,
        group_name=group_name,
        work_type_ids=work_type_ids,
        shift_date=REPORT_DATE,
        day_start=day_start,
        cutoff=cutoff,
        area_min_ha=area_min_ha,
        field_ok=_field_allowed_for_item,
        firm_for_field=get_firm_for_field,
        base_url=BASE_URL,
        task_seasons=_fact_seasons,
        firm_keys=list(AGROFIRMS),
    )
    if is_harvest and harvest_perf_10 is not None:
        # Факты по инвентарю 1.1 затирают area_today и снова теряют силос.
        # Итого и строки фирм остаются на площади 5.0 (как сводка 2).
        for firm, st in result.items():
            by_field = st.get("today_by_field") or {}
            st["area_today"] = round(sum(float(v) for v in by_field.values()), 2)

    return result
 


def _empty_stats() -> dict:
    return {
        "n_mach": 0, "n_hired": 0,
        "area_today": 0.0, "hours_today": 0.0, "avg_season": 0.0,
        "season_sum": 0.0, "machine_days": 0,
        "area_by_crop": {}, "today_by_field": {},
        "detail_own": [], "detail_hired": [],
    }


def _report1_group_fetch_concurrency() -> int:
    raw = os.environ.get("REPORT1_GROUP_FETCH_CONCURRENCY", "4")
    try:
        return max(1, int(str(raw).strip()))
    except ValueError:
        return 2


async def _fetch_other_group_stats(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    day_start: datetime,
    cutoff: datetime,
    other_groups: list[tuple[str, list[int]]],
    skipped_perf_groups: set[str],
) -> dict[str, dict[str, dict]]:
    """
    Загрузка выработки по группам работ.
    Группы из REPORT1_SKIP_PERF_GROUPS не тянут season+mtfmi — скелет заполняется из доноров.
    Остальные — с ограниченным параллелизмом (меньше пик RAM при batch).
    """
    skipped = set(skipped_perf_groups)
    to_fetch = [(gn, wt) for gn, wt in other_groups if gn not in skipped]
    if skipped:
        print(
            "Сводка 1 fast-path: без API-расчёта (данные из доноров): "
            + ", ".join(sorted(skipped))
        )

    group_sem = asyncio.Semaphore(_report1_group_fetch_concurrency())
    fetched: dict[str, dict[str, dict]] = {}

    async def _one(group_name: str, work_type_ids: list[int]) -> None:
        async with group_sem:
            fetched[group_name] = await fetch_day_stats_for_work_types(
                client, sem, day_start, cutoff, work_type_ids, group_name,
            )

    if to_fetch:
        _t_groups = time.monotonic()
        await asyncio.gather(*[_one(gn, wt) for gn, wt in to_fetch])
        _tprint(
            f"остальные группы ({len(to_fetch)} шт., параллелизм "
            f"{_report1_group_fetch_concurrency()}) wall", _t_groups,
        )

    result: dict[str, dict[str, dict]] = {}
    for group_name, _ in other_groups:
        if group_name in skipped:
            result[group_name] = {firm: _empty_stats() for firm in AGROFIRMS}
        else:
            result[group_name] = fetched[group_name]
    return result


def _firm_day_area_ha(stats: dict | None) -> float:
    """Суточные га строки выработки — та же база, что и «Итого».

    SVODKI_DAILY_FACTS_DAILY подменяет area_today цифрой из TaskStore. Она не
    сходится со сводкой 2 и с Cropwise, а «Итого» уже суммирует today_by_field.
    """
    by_field = (stats or {}).get("today_by_field") or {}
    if by_field:
        return round(sum(float(v) for v in by_field.values()), 2)
    return round(float((stats or {}).get("area_today") or 0.0), 2)


def _today_area_total_ha(
    day_stats: dict[str, dict],
    firms: list[str] | None = None,
) -> float:
    """Единый итог «факт за сутки» — сумма mtfmi по полям без промежуточного округления."""
    total = 0.0
    for firm in firms or AGROFIRMS:
        by_field = day_stats.get(firm, {}).get("today_by_field") or {}
        total += sum(float(v) for v in by_field.values())
    return total


def perf_stats_is_effectively_empty(day_stats: dict[str, dict]) -> bool:
    """
    True — по всем агрофирмам нет выработки/техники за сутки и за сезон в таблице;
    такой лист «Выработка техники» не создаём (только строки плана и нулей).
    """
    for firm in AGROFIRMS:
        s = day_stats.get(firm, _empty_stats())
        if (s.get("n_mach") or 0) > 0:
            return False
        if (s.get("n_hired") or 0) > 0:
            return False
        if (s.get("area_today") or 0) > 0:
            return False
        if (s.get("hours_today") or 0) > 0:
            return False
        if (s.get("avg_season") or 0) > 0:
            return False
        for v in (s.get("area_by_crop") or {}).values():
            if (v or 0) > 0:
                return False
        if s.get("detail_own") or s.get("detail_hired"):
            return False
    return True

# =============================================================================
# СТИЛИ
# =============================================================================

_COLOR_HDR      = "7B8EB1"
_COLOR_TOTAL    = "D9E1F2"
_COLOR_TITLE_TX = "111827"
_COLOR_DATA     = "FFFFFF"
# Пастельные заливки по культурам (шапка групп и колонка «Культура»)
_CROP_HEADER_FILL: dict[str, str] = {
    "Рапс озимый": "FDE8D4",
    "Пшеница озимая": "FFF9C4",
    "Многолетние травы": "D9EAD3",
}
_COLOR_ALERT_PROD = "FCE4EC"      # выработка на единицу меньше плановой
_COLOR_ALERT_FORECAST = "F8BBD0"  # прогнозная дата слишком далеко


def _border() -> Border:
    t = Side(style="thin", color="FFB4B4B4")
    return Border(left=t, right=t, top=t, bottom=t)


def _cell(
    ws, row: int, col: int, value=None,
    bold: bool = False, color: str = "000000", bg=None,
    halign: str = "center", wrap: bool = True, border: bool = True,
    number_format: str | None = None,
    font_name: str = "Calibri", font_size: int = 11,
) -> openpyxl.cell.Cell:
    c = ws.cell(row=row, column=col, value=value)
    # Для меток "Итого/Итог..." перенос не нужен: в PDF/Excel они могут
    # распадаться на две строки в узких колонках.
    if isinstance(value, str) and value.strip().lower().startswith("итог"):
        wrap = False
    c.font      = Font(name=font_name, size=font_size, bold=bold, color=color)
    c.alignment = Alignment(horizontal=halign, vertical="center", wrap_text=wrap)
    if bg:
        c.fill = PatternFill("solid", fgColor=bg)
    if border:
        c.border = _border()
    if number_format:
        c.number_format = number_format
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        if abs(v - round(v)) < 1e-9:
            c.number_format = NF_INT
        elif abs(v - round(v, 1)) < 1e-9:
            c.number_format = NF_DEC1
        else:
            c.number_format = NF_DEC2
    return c


def _estimate_wrapped_lines(value: object, col_width: float | None) -> int:
    text = str(value or "").strip()
    if not text:
        return 1
    width = float(col_width or 10)
    chars_per_line = max(1, int(width * 0.9))
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
    min_height: float = 16.0,
    line_height: float = 13.0,
    max_height: float = 120.0,
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
        target = min(max_height, max(min_height, max_lines * line_height + 2))
        current = ws.row_dimensions[r].height
        ws.row_dimensions[r].height = max(current or 0.0, target)


# =============================================================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ: факт общий
# =============================================================================

def _is_multi_year_grass_crop(crop: str) -> bool:
    """Многолетние травы — для листов «Свод по агрофирмам» и внесения удобрений."""
    lc = (crop or "").lower().replace("ё", "е")
    if "многолетние" not in lc:
        return False
    if "введение в оборот" in lc:
        return False
    return True


def _report1_other_kept_apart(crop: str) -> bool:
    """Культура режима «Иные», которую в этой группе показываем своей строкой."""
    name = str(crop or "").strip()
    if not name or name in SKIP_CROPS or name == "Иные культуры":
        return False
    if not other_crops_as_rows(WORK_TYPE_NAME):
        return False
    return crop_report_mode(WORK_TYPE_NAME, name) == CROP_MODE_OTHER


def _sum_multi_year_grass(crops: dict[str, float]) -> float:
    return sum(
        float(v or 0.0)
        for k, v in crops.items()
        if _is_multi_year_grass_crop(k) and not _report1_other_kept_apart(k)
    )


def _plan_ha_like_report2(firm: str, crop: str) -> float:
    """План культуры как во 2-й сводке (plan_ha_for_report).

    Строка «Многолетние травы» в 1-й сводке складывает все годы этой культуры.
    """
    if _is_multi_year_grass_crop(crop):
        firm_plan = (HARDCODED_PLAN.get(WORK_TYPE_NAME) or {}).get(firm) or {}
        if not isinstance(firm_plan, dict):
            return 0.0
        return sum(
            plan_ha_for_report(WORK_TYPE_NAME, firm, str(name))
            for name in firm_plan
            if _is_multi_year_grass_crop(str(name)) and not _report1_other_kept_apart(str(name))
        )
    return plan_ha_for_report(WORK_TYPE_NAME, firm, crop)


def _fold_report1_crop_inputs(
    season_data: dict[str, dict[str, float]],
    day_stats: dict[str, dict],
    plan_data: dict[str, dict[str, float]],
) -> tuple[dict, dict, dict]:
    """Культуры с флагом редактора: в «Иные культуры» или вне факта сводки 1."""
    def _kept(firm: str, crops: dict) -> dict:
        """Без культур, убранных в редакторе (скрытие по АФ, ранг 0, «Не считать»)."""
        return {
            c: v for c, v in (crops or {}).items()
            if not is_crop_user_excluded(WORK_TYPE_NAME, firm, str(c))
        }

    season = {
        firm: collapse_crop_areas(WORK_TYPE_NAME, _kept(firm, crops))
        for firm, crops in (season_data or {}).items()
    }
    plan = {
        firm: collapse_crop_areas(WORK_TYPE_NAME, _kept(firm, crops))
        for firm, crops in (plan_data or {}).items()
    }
    stats: dict[str, dict] = {}
    for firm, row in (day_stats or {}).items():
        copied = dict(row or {})
        original = {
            str(crop): float(ha or 0.0)
            for crop, ha in (copied.get("area_by_crop") or {}).items()
        }
        folded = collapse_crop_areas(WORK_TYPE_NAME, _kept(firm, original))
        copied["area_by_crop"] = folded
        dropped = sum(original.values()) - sum(folded.values())
        if dropped > 0:
            area_today = max(0.0, float(copied.get("area_today") or 0.0) - dropped)
            copied["area_today"] = round(area_today, 2)
            copied["today_by_field"] = {0: copied["area_today"]} if copied["area_today"] else {}
        stats[firm] = copied
    return season, stats, plan


def get_fact_total(crop: str,
                   crops_season: dict[str, float],
                   _crops_today: dict[str, float],
                   is_multi: bool = False) -> float:
    """Факт общий — только накопленный за сезон (mtfmi), без сложения с сутками."""
    if is_multi:
        return _sum_multi_year_grass(crops_season)
    return crops_season.get(crop, 0.0)


def _summary_crop_key(crop: str) -> str:
    lc = crop.lower()
    if "многолетние" in lc:
        return "Многолетние травы"
    if "рапс" in lc:
        return "Рапс озимый"
    return crop


OTHER_CROPS_FOOTNOTE_FALLBACK = "Введение в оборот, Многолетние травы, Пар"


def _other_crops_footnote_labels(raw_fact_by_crop: dict[str, float]) -> list[str]:
    """Подписи для примечания * к строке «Иные культуры»."""
    labels: set[str] = set()
    for crop, val in raw_fact_by_crop.items():
        if float(val or 0.0) <= 0:
            continue
        lc = crop.lower()
        if "введение в оборот" in lc:
            labels.add("Введение в оборот")
        elif "многолетние" in lc:
            labels.add("Многолетние травы")
        elif lc == "пар":
            labels.add("Пар")
        elif "однолетние" in lc and "трав" in lc:
            labels.add("Однолетние травы")
        elif "лугов" in lc and "трав" in lc:
            labels.add("Луговые травы")
        else:
            labels.add(crop)
    return sorted(labels)


def _other_crops_footnote_text(raw_fact_by_crop: dict[str, float]) -> str:
    labels = _other_crops_footnote_labels(raw_fact_by_crop)
    if not labels:
        return f"* Иные культуры: {OTHER_CROPS_FOOTNOTE_FALLBACK}"
    return "* Иные культуры: " + ", ".join(labels)


_S1_SUBHDRS = (
    "План, га",
    "Факт всего, га",
    "Остаток к выполнению, га",
    "Доля факта от плана, %",
    "Факт за сутки, га",
)
_S1_COLS_PER_CROP = len(_S1_SUBHDRS)

# Для Рапс озимый: 2 фиксированных + 2*N удобрений (сезон+сутки) + 1 (факт за сутки)
# Число колонок вычисляется динамически через _raps_cols()
def _raps_subhdrs() -> tuple[str, ...]:
    """Динамически строит заголовки колонок для Рапс озимый."""
    hdrs = ["План обраб. пл., га", "Факт общий, га"]
    for _, fname in raps_fert_order:
        hdrs.append(f"Удобрение: {fname}, га")
    hdrs.append("Факт за сутки, га")
    for _, fname in raps_fert_order:
        hdrs.append(f"Удобрение: {fname}, га (сутки)")
    return tuple(hdrs)

def _raps_cols() -> int:
    return 2 + len(raps_fert_order) * 2 + 1


def _raps_get_selected_fert_ids() -> list[int]:
    """
    Возвращает список fert_id для отображения в колонках Рапс озимый.
    Приоритет — удобрения с ключевыми словами Селитра/Сульфат в названии.
    Если таких нет — все удобрения рапса.
    """
    pairs = [(fid, (name or "").lower()) for fid, name in raps_fert_order]
    ids = [
        fid for fid, lname in pairs
        if (FERT_NITRATE_KEY in lname) or (FERT_SULFATE_KEY in lname)
    ]
    if not ids:
        ids = [fid for fid, _ in raps_fert_order]
    return ids


def _raps_fact_from_visible_ferts(fd: dict[int, dict[str, float]]) -> float:
    """
    Факт по рапсу для сводной таблицы:
    сумма отображаемых колонок (селитра + сульфат, или все если не найдены).
    """
    selected_ids = _raps_get_selected_fert_ids()
    return round(sum(float(fd.get(fid, {}).get("season", 0.0)) for fid in selected_ids), 2)

# =============================================================================
# ЛИСТ 1: СВОДКА ПО КУЛЬТУРАМ (агрегат по холдингу, только WORK_TYPE_ID=55)
# =============================================================================


def _summary_crops_ordered_list() -> list[str]:
    """CROP_SUMMARY_CROPS с учётом рангов группы 1.1 из редактора планов."""
    return order_crops_by_group_rank(WORK_TYPE_NAME, CROP_SUMMARY_CROPS)


def write_crop_summary_sheet(
    wb:          openpyxl.Workbook,
    season_data: dict[str, dict[str, float]],
    day_stats:   dict[str, dict],
    plan_data:   dict[str, dict[str, float]],
    fert_data:   dict[str, dict[int, dict[str, float]]] | None = None,
) -> None:
    season_data, day_stats, plan_data = _fold_report1_crop_inputs(
        season_data, day_stats, plan_data,
    )
    ws = wb.create_sheet("Сводка по культурам", 0)
    hdr = 1
    headers = [
        "№",
        "Культура",
        "План, га",
        "Факт общий, га",
        "Остаток к выполнению, га",
        "Доля факта от плана, %",
        "Факт за сутки, га",
        "Прогнозная дата завершения по фактической выработке",
    ]
    for ci, h in enumerate(headers, start=1):
        fs = 9 if "Прогнозная дата завершения" in h else 10
        _cell(ws, hdr, ci, h, bold=True, color="FFFFFF", bg=_COLOR_HDR, font_size=fs)
    ws.row_dimensions[hdr].height = 66

    agg_plan: dict[str, float] = defaultdict(float)
    agg_fact: dict[str, float] = defaultdict(float)
    agg_today: dict[str, float] = defaultdict(float)

    # «План, га» — как колонка «План» / «План, га» во 2-й сводке
    # (plan_ha_for_report и firm_group_plan_ha), не автоплощадь полей.
    for firm in AGROFIRMS:
        crops_fact = season_data.get(firm, {})
        crops_today = day_stats.get(firm, {}).get("area_by_crop", {})
        for crop in _summary_crops_ordered_list():
            plan_val = _plan_ha_like_report2(firm, crop)
            fact_val = _mu_season_fact(
                crop, firm, crops_fact, crops_today,
            )
            today_val = crops_today.get(crop, 0.0)
            agg_plan[crop] += plan_val
            agg_fact[crop] += fact_val
            agg_today[crop] += today_val

    data_row = hdr + 1
    total_plan = total_fact = total_today = total_rem = 0.0
    for idx, crop in enumerate(_summary_crops_ordered_list(), start=1):
        plan_val = round(agg_plan.get(crop, 0.0), 2)
        fact_val = round(float(agg_fact.get(crop, 0.0)), 2)
        today_val = round(agg_today.get(crop, 0.0), 2)
        rem = round(plan_val - fact_val, 2)
        share = round(fact_val / plan_val * 100, 2) if plan_val > 0 else 0.0
        fcd = compute_forecast_end_date(plan_val, fact_val, today_val)
        row_bg = _CROP_HEADER_FILL.get(crop, _COLOR_DATA)
        fc_bg = _COLOR_ALERT_FORECAST if forecast_date_is_alert(fcd) else _COLOR_DATA

        _cell(ws, data_row, 1, idx, bg=_COLOR_DATA)
        _cell(ws, data_row, 2, crop, halign="left", bg=row_bg)
        _cell(ws, data_row, 3, plan_val if plan_val else 0, bg=_COLOR_DATA, number_format=NF_INT)
        _cell(ws, data_row, 4, fact_val, bg=_COLOR_DATA, number_format=NF_DEC2)
        _cell(ws, data_row, 5, rem, bg=_COLOR_DATA, number_format=NF_DEC2)
        _cell(ws, data_row, 6, share, bg=_COLOR_DATA, number_format="0.00")
        _cell(ws, data_row, 7, today_val if today_val else 0, bg=_COLOR_DATA, number_format=NF_DEC2)
        _cell(ws, data_row, 8, fcd, bg=fc_bg, number_format=NF_DATE)
        total_plan += plan_val
        total_fact += fact_val
        total_today += today_val
        total_rem += rem
        data_row += 1

    # ── Иные культуры ─────────────────────────────────────────────────────────
    # Собираем культуры из season_data и day_stats, которых нет в CROP_SUMMARY_CROPS.
    # Режим «по культурам»: отмеченные в редакторе культуры — своими строками.
    crop_summary_set = set(CROP_SUMMARY_CROPS)
    other_fact_by_crop: dict[str, float] = {}
    other_raw_fact: dict[str, float] = {}
    other_today_by_crop: dict[str, float] = {}
    split_fact: dict[str, float] = {}
    split_today: dict[str, float] = {}

    def _summary_split_name(crop: str) -> bool:
        name = str(crop or "").strip()
        if not name or name in crop_summary_set:
            return False
        return _report1_other_kept_apart(name)

    for firm in AGROFIRMS:
        crops_fact = season_data.get(firm, {})
        crops_today = day_stats.get(firm, {}).get("area_by_crop", {})
        for crop, val in crops_fact.items():
            v = float(val or 0.0)
            if _summary_split_name(crop):
                split_fact[crop] = split_fact.get(crop, 0.0) + v
                continue
            norm = _summary_crop_key(crop)
            if norm not in crop_summary_set and crop not in SKIP_CROPS:
                other_fact_by_crop[norm] = other_fact_by_crop.get(norm, 0.0) + v
                if v > 0:
                    other_raw_fact[crop] = other_raw_fact.get(crop, 0.0) + v
        for crop, val in crops_today.items():
            if _summary_split_name(crop):
                split_today[crop] = split_today.get(crop, 0.0) + float(val or 0.0)
                continue
            norm = _summary_crop_key(crop)
            if norm not in crop_summary_set and crop not in SKIP_CROPS:
                other_today_by_crop[norm] = other_today_by_crop.get(norm, 0.0) + float(val or 0.0)
        firm_plan = (HARDCODED_PLAN.get(WORK_TYPE_NAME) or {}).get(firm) or {}
        if isinstance(firm_plan, dict):
            for crop in firm_plan:
                if _summary_split_name(str(crop)):
                    split_fact.setdefault(str(crop), split_fact.get(str(crop), 0.0))

    split_names = sorted(
        set(split_fact) | set(split_today),
        key=lambda name: crop_sort_key_for_group(WORK_TYPE_NAME, name),
    )
    row_idx = len(CROP_SUMMARY_CROPS) + 1
    for name in split_names:
        plan_val = round(
            sum(plan_ha_for_report(WORK_TYPE_NAME, firm, name) for firm in AGROFIRMS),
            2,
        )
        fact_val = round(split_fact.get(name, 0.0), 2)
        today_val = round(split_today.get(name, 0.0), 2)
        rem = round(plan_val - fact_val, 2)
        share = round(fact_val / plan_val * 100, 2) if plan_val > 0 else 0.0
        fcd = compute_forecast_end_date(plan_val, fact_val, today_val)
        fc_bg = _COLOR_ALERT_FORECAST if forecast_date_is_alert(fcd) else _COLOR_DATA
        _cell(ws, data_row, 1, row_idx, bg=_COLOR_DATA)
        _cell(ws, data_row, 2, name, halign="left", bg=_COLOR_DATA)
        _cell(ws, data_row, 3, plan_val if plan_val else 0, bg=_COLOR_DATA, number_format=NF_INT)
        _cell(ws, data_row, 4, fact_val, bg=_COLOR_DATA, number_format=NF_DEC2)
        _cell(ws, data_row, 5, rem, bg=_COLOR_DATA, number_format=NF_DEC2)
        _cell(ws, data_row, 6, share, bg=_COLOR_DATA, number_format="0.00")
        _cell(ws, data_row, 7, today_val if today_val else 0, bg=_COLOR_DATA, number_format=NF_DEC2)
        _cell(ws, data_row, 8, fcd, bg=fc_bg, number_format=NF_DATE)
        ws.row_dimensions[data_row].height = 18
        total_plan += plan_val
        total_fact += fact_val
        total_today += today_val
        total_rem += rem
        data_row += 1
        row_idx += 1

    other_fact = round(sum(other_fact_by_crop.values()), 2)
    other_today = round(sum(other_today_by_crop.values()), 2)
    other_plan = sum(
        max(
            0.0,
            firm_group_plan_ha(WORK_TYPE_NAME, firm)
            - sum(_plan_ha_like_report2(firm, crop) for crop in CROP_SUMMARY_CROPS)
            - sum(plan_ha_for_report(WORK_TYPE_NAME, firm, name) for name in split_names),
        )
        for firm in AGROFIRMS
    )
    other_rem = round(other_plan - other_fact, 2)
    other_share = round(other_fact / other_plan * 100, 2) if other_plan > 0 else 0.0

    _cell(ws, data_row, 1, row_idx, bg=_COLOR_DATA)
    _cell(ws, data_row, 2, "Иные культуры", halign="left", bg=_COLOR_DATA)
    _cell(ws, data_row, 3, other_plan if other_plan else 0, bg=_COLOR_DATA, number_format=NF_INT)
    _cell(ws, data_row, 4, other_fact, bg=_COLOR_DATA, number_format=NF_DEC2)
    _cell(ws, data_row, 5, other_rem, bg=_COLOR_DATA, number_format=NF_DEC2)
    _cell(ws, data_row, 6, other_share, bg=_COLOR_DATA, number_format="0.00")
    _cell(ws, data_row, 7, other_today, bg=_COLOR_DATA, number_format=NF_DEC2)
    _cell(ws, data_row, 8, None, bg=_COLOR_DATA)
    ws.row_dimensions[data_row].height = 18
    data_row += 1

    total_fact += other_fact
    total_plan += other_plan
    total_rem += other_rem
    total_today_exact = _today_area_total_ha(day_stats)

    _cell(ws, data_row, 1, "", bold=True, bg=_COLOR_TOTAL)
    _cell(ws, data_row, 2, "Итого", bold=True, bg=_COLOR_TOTAL, halign="left")
    total_plan_display = round(
        sum(firm_group_plan_ha(WORK_TYPE_NAME, firm) for firm in AGROFIRMS),
        1,
    )
    _cell(
        ws, data_row, 3, total_plan_display,
        bold=True, bg=_COLOR_TOTAL,
        number_format=NF_INT if abs(total_plan_display - round(total_plan_display)) < 1e-9 else NF_DEC1,
    )
    _cell(ws, data_row, 4, round(total_fact, 2), bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
    _cell(ws, data_row, 5, round(total_rem, 2), bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
    _cell(
        ws,
        data_row,
        6,
        round(total_fact / total_plan_display * 100, 2) if total_plan_display > 0 else 0.0,
        bold=True,
        bg=_COLOR_TOTAL,
        number_format="0.00",
    )
    _cell(ws, data_row, 7, round(total_today_exact, 2), bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
    _cell(ws, data_row, 8, None, bold=True, bg=_COLOR_TOTAL)
    ws.row_dimensions[data_row].height = 18
    data_row += 1

    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 21
    for ci in range(3, 8):
        ws.column_dimensions[get_column_letter(ci)].width = 24
    ws.column_dimensions["H"].width = 42
    _auto_fit_row_heights(ws, hdr + 1, data_row - 1, 8, min_height=18.0, line_height=13.0)

    # Приписка мелким шрифтом: расшифровка иных культур (всегда под таблицей)
    note_text = _other_crops_footnote_text(other_raw_fact)
    ws.merge_cells(start_row=data_row, start_column=1, end_row=data_row, end_column=len(headers))
    c = ws.cell(row=data_row, column=1, value=note_text)
    c.font = Font(name="Calibri", size=7, italic=True, color="555555")
    c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    c.border = _border()
    ws.row_dimensions[data_row].height = 14


# =============================================================================
# ЛИСТ 2: СВОД ПО АГРОФИРМАМ (агрофирма × культура, только WORK_TYPE_ID=55)
# =============================================================================

def _report1_firm_extra_crops(
    season_data: dict[str, dict[str, float]],
    day_stats: dict[str, dict],
    plan_data: dict[str, dict[str, float]],
) -> list[str]:
    """Культуры режима «Иные», которым на листе фирм нужна своя колонка."""
    if not other_crops_as_rows(WORK_TYPE_NAME):
        return []
    summary = set(SUMMARY_CROPS)
    names: set[str] = set()

    def _take(crop: str) -> None:
        if _report1_other_kept_apart(crop) and str(crop) not in summary:
            names.add(str(crop))

    for firm_crops in (season_data or {}).values():
        for crop in firm_crops or {}:
            _take(str(crop))
    for row in (day_stats or {}).values():
        for crop in ((row or {}).get("area_by_crop") or {}):
            _take(str(crop))
    for firm_crops in (plan_data or {}).values():
        for crop in firm_crops or {}:
            _take(str(crop))
    for firm_crops in (HARDCODED_PLAN.get(WORK_TYPE_NAME) or {}).values():
        if isinstance(firm_crops, dict):
            for crop in firm_crops:
                _take(str(crop))
    return sorted(names, key=lambda name: crop_sort_key_for_group(WORK_TYPE_NAME, name))


def write_sheet1(wb: openpyxl.Workbook,
                 season_data: dict[str, dict[str, float]],
                 day_stats:   dict[str, dict],
                 plan_data:   dict[str, dict[str, float]],
                 fert_data:   dict[str, dict[int, dict[str, float]]] | None = None) -> None:
    season_data, day_stats, plan_data = _fold_report1_crop_inputs(
        season_data, day_stats, plan_data,
    )
    extra_crops = _report1_firm_extra_crops(season_data, day_stats, plan_data)
    n_raps = _raps_cols()
    total_cols = 1 + (_S1_COLS_PER_CROP * (len(SUMMARY_CROPS) - 1)) + n_raps + len(extra_crops) + 2
    ws = wb.create_sheet("Свод по агрофирмам", 1)
    hdr_row = 1
    _hdr = dict(bold=True, color="FFFFFF", bg=_COLOR_HDR, font_size=10)

    _cell(ws, hdr_row, 1, "Агрофирма", halign="center", **_hdr)
    ws.merge_cells(start_row=hdr_row, start_column=1, end_row=hdr_row + 1, end_column=1)

    col = 2
    for crop in SUMMARY_CROPS:
        ncols = n_raps if crop == "Рапс озимый" else _S1_COLS_PER_CROP
        ch = _CROP_HEADER_FILL.get(crop, _COLOR_HDR)
        crop_hdr = dict(bold=True, color="000000", bg=ch, font_size=10)
        ws.merge_cells(start_row=hdr_row, start_column=col,
                       end_row=hdr_row, end_column=col + ncols - 1)
        _cell(ws, hdr_row, col, crop, halign="center", **crop_hdr)
        col += ncols

    for crop in extra_crops:
        ws.merge_cells(start_row=hdr_row, start_column=col, end_row=hdr_row + 1, end_column=col)
        _cell(ws, hdr_row, col, f"{crop}\nФакт общий, га", halign="center", **_hdr)
        col += 1

    ws.merge_cells(start_row=hdr_row, start_column=col, end_row=hdr_row + 1, end_column=col)
    _cell(ws, hdr_row, col, "Общая пл. за сутки,\nпо всем культ., га", halign="center", **_hdr)
    ws.merge_cells(start_row=hdr_row, start_column=col + 1, end_row=hdr_row + 1, end_column=col + 1)
    _cell(ws, hdr_row, col + 1, "Иные культуры\nФакт общий, га", halign="center", **_hdr)

    sub_row = hdr_row + 1
    col = 2
    for _crop in SUMMARY_CROPS:
        subhdrs = _raps_subhdrs() if _crop == "Рапс озимый" else _S1_SUBHDRS
        for sh in subhdrs:
            _cell(ws, sub_row, col, sh, halign="center", **_hdr)
            col += 1

    ws.row_dimensions[hdr_row].height = 42
    ws.row_dimensions[sub_row].height = 60

    row = hdr_row + 2
    totals: dict[str, float] = defaultdict(float)
    summary_crops_lower = {c.lower() for c in SUMMARY_CROPS}

    for firm in AGROFIRMS:
        _cell(ws, row, 1, firm, halign="left", color="000000", bg=_COLOR_DATA)
        col = 2

        crops_season = season_data.get(firm, {})
        crops_today  = day_stats.get(firm, {}).get("area_by_crop", {})
        area_today_total = 0.0
        other_fact_total = 0.0

        for crop in SUMMARY_CROPS:
            is_multi = crop == "Многолетние травы"
            fact_val = _mu_season_fact(
                crop, firm, crops_season, crops_today, is_multi=is_multi,
            )
            if crop == "Рапс озимый":
                today_val = float(crops_today.get("Рапс озимый", 0.0))
            else:
                today_val = float(
                    _sum_multi_year_grass(crops_today)
                    if is_multi else crops_today.get(crop, 0.0),
                )
            plan_val = _plan_ha_like_report2(firm, crop)
            pl = get_column_letter(col)
            fl = get_column_letter(col + 1)

            _cell(ws, row, col,     plan_val if plan_val else 0, number_format=NF_INT)
            _cell(ws, row, col + 1, fact_val if fact_val else 0,      number_format=NF_DEC2)

            if crop == "Рапс озимый":
                fd = (fert_data or {}).get(firm, {})
                c = col + 2
                # Колонки сезон по каждому удобрению
                for fid, _ in raps_fert_order:
                    v = fd.get(fid, {}).get("season", 0.0)
                    _cell(ws, row, c, v if v else 0, number_format=NF_DEC2)
                    totals[f"raps_season_{fid}"] += v
                    c += 1
                # Факт за сутки
                _cell(ws, row, c, today_val if today_val else 0, number_format=NF_DEC2)
                c += 1
                # Колонки сутки по каждому удобрению
                for fid, _ in raps_fert_order:
                    v = fd.get(fid, {}).get("today", 0.0)
                    _cell(ws, row, c, v if v else 0, number_format=NF_DEC2)
                    totals[f"raps_today_{fid}"] += v
                    c += 1
                col += n_raps
            else:
                _cell(ws, row, col + 2, f"={pl}{row}-{fl}{row}", number_format=NF_DEC2)
                if plan_val > 0:
                    _cell(ws, row, col + 3, f"={fl}{row}/{pl}{row}*100", number_format="0.00")
                else:
                    _cell(ws, row, col + 3, 0, number_format="0.00")
                _cell(ws, row, col + 4, today_val if today_val else 0, number_format=NF_DEC2)
                col += _S1_COLS_PER_CROP

            totals[f"{crop}_plan"]  += plan_val
            totals[f"{crop}_fact"]  += fact_val
            totals[f"{crop}_today"] += today_val

            # Вместо area_today_total += today_val для рапса:
            if crop == "Рапс озимый":
                area_today_total += float(crops_today.get("Рапс озимый", 0.0))
            else:
                area_today_total += today_val

        for cn, fv in crops_season.items():
            if _report1_other_kept_apart(cn):
                continue
            if not _is_multi_year_grass_crop(cn) and cn.lower() not in summary_crops_lower \
                    and cn not in SKIP_CROPS:
                other_fact_total += fv
        for cn, tv in crops_today.items():
            if cn.lower() in summary_crops_lower:
                continue
            if _report1_other_kept_apart(cn) or (
                not _is_multi_year_grass_crop(cn) and cn not in SKIP_CROPS
            ):
                area_today_total += tv

        for crop in extra_crops:
            fact = float(crops_season.get(crop, 0.0))
            _cell(ws, row, col, fact if fact else 0, number_format=NF_DEC2)
            totals[f"extra_{crop}"] += fact
            col += 1

        _cell(ws, row, col,     round(area_today_total, 2), number_format=NF_DEC2)
        _cell(ws, row, col + 1, float(other_fact_total), number_format=NF_DEC2)
        totals["today_total"] += area_today_total
        totals["other_total"] += other_fact_total
        row += 1

    tot_row    = row
    data_start = hdr_row + 2
    last_data  = tot_row - 1
    _cell(ws, tot_row, 1, "Итого", bold=True, bg=_COLOR_TOTAL, halign="left", color="000000")
    col = 2
    for crop in SUMMARY_CROPS:
        pl = get_column_letter(col)
        fl = get_column_letter(col + 1)
        _cell(ws, tot_row, col,
              f"=SUM({pl}{data_start}:{pl}{last_data})",
              bold=True, bg=_COLOR_TOTAL, number_format=NF_INT)
        _cell(ws, tot_row, col + 1,
              f"=SUM({fl}{data_start}:{fl}{last_data})",
              bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)

        if crop == "Рапс озимый":
            c = col + 2
            for fid, _ in raps_fert_order:
                _cell(ws, tot_row, c,
                      round(totals.get(f"raps_season_{fid}", 0.0), 1),
                      bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC1)
                c += 1
            tl = get_column_letter(c)
            _cell(ws, tot_row, c,
                  f"=SUM({tl}{data_start}:{tl}{last_data})",
                  bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
            c += 1
            for fid, _ in raps_fert_order:
                _cell(ws, tot_row, c,
                      round(totals.get(f"raps_today_{fid}", 0.0), 2),
                      bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
                c += 1
            col += n_raps
        else:
            _cell(ws, tot_row, col + 2,
                  f"={pl}{tot_row}-{fl}{tot_row}",
                  bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
            _cell(ws, tot_row, col + 3,
                  f"={fl}{tot_row}/{pl}{tot_row}*100",
                  bold=True, bg=_COLOR_TOTAL, number_format="0.00")
            tl = get_column_letter(col + 4)
            _cell(ws, tot_row, col + 4,
                  f"=SUM({tl}{data_start}:{tl}{last_data})",
                  bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
            col += _S1_COLS_PER_CROP

    for _crop in extra_crops:
        letter = get_column_letter(col)
        _cell(
            ws, tot_row, col,
            f"=SUM({letter}{data_start}:{letter}{last_data})",
            bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2,
        )
        col += 1

    _cell(ws, tot_row, col,
          round(_today_area_total_ha(day_stats), 2), bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
    _cell(ws, tot_row, col + 1,
          round(totals["other_total"], 1), bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC1)

    ws.column_dimensions["A"].width = 24
    for ci in range(2, total_cols + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 11.5
    raps_start_col = 2
    for c_name in SUMMARY_CROPS:
        if c_name == "Рапс озимый":
            break
        raps_start_col += _raps_cols() if c_name == "Рапс озимый" else _S1_COLS_PER_CROP
    raps_end_col = raps_start_col + n_raps - 1
    for ci in range(raps_start_col, raps_end_col + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 14.5
    _auto_fit_row_heights(ws, hdr_row + 2, tot_row, total_cols, min_height=16.0, line_height=12.5)


# =============================================================================
# ЛИСТ «ВЫРАБОТКА ТЕХНИКИ» — общая функция для любой группы работ
# =============================================================================

PERF_COLS = [
    "Агрофирмы",
    "Плановая выработка, га/сутки",
    "Количество задействованной техники в т.ч. Прицепных",
    "Обработано за сутки, га",
    "Выработка за сутки, га/1 ед агрегата",
    "Средняя выработка по сезону, га/сутки",
    "Общее время работы на полях, час",
    "Среднее время эффективной работы на поле на 1 агрегат., час.",
    "Часовая выработка в расчете на 1 агрегат, га",
    "Доступно к работе самоходных агрегатов, в т.ч. прицепных",
    "Всего самоходных наемных агрегатов",
]


def write_perf_sheet(
    wb:          openpyxl.Workbook,
    day_stats:   dict[str, dict],
    sheet_title: str,
    sheet_index: int,
    own_count:   dict[str, int],
) -> None:
    """
    Универсальный лист «Выработка техники» для любой группы работ.
    sheet_title — название листа (обрезается до 31 символа).
    own_count — количество собственных агрегатов в наличии из конфига (колонка 10).
    Колонка 11 («Всего самоходных наёмных агрегатов») берётся из day_stats[firm]["n_hired"].
    """
    n  = len(PERF_COLS)
    group_name = (
        WORK_TYPE_NAME
        if sheet_title.strip() == "Выработка техники"
        else (
            sheet_title.split("—", 1)[1].strip()
            if "—" in sheet_title
            else sheet_title.strip()
        )
    )
    _chist = "АФ Чистопольская"
    if group_name in REPORT1_PERF_CHISTOPOL_ONLY_GROUPS and _chist in AGROFIRMS:
        target_firms = [_chist]
    else:
        target_firms = list(AGROFIRMS.keys())
    # Excel ограничивает имя листа 31 символом
    ws = wb.create_sheet(sheet_title[:31], sheet_index)

    for ci, col_name in enumerate(PERF_COLS, start=1):
        _cell(ws, 1, ci, col_name, bold=True, color="FFFFFF", bg=_COLOR_HDR)
    ws.row_dimensions[1].height = 60

    # В 3-й таблице 1-й сводки ("Выработка техники") фиксируем вывод
    # как текст с группировкой тысяч, чтобы формат в PDF совпадал с остальными.
    force_grouped_text = sheet_title.strip() == "Выработка техники"

    def _display_val(ci: int, raw):
        if not force_grouped_text:
            return raw, perf_num_fmt_by_col.get(ci)
        if ci in (2, 3, 10, 11):
            return _fmt_ru_grouped(raw, 0), None
        if ci in (4, 5):
            return _fmt_ru_grouped(raw, 2), None
        if ci == 6:
            return _fmt_ru_grouped(raw, 2), None
        if ci == 9:
            return _fmt_ru_grouped(raw, 1), None
        return raw, None

    # Явно задаём форматы чисел, чтобы в Excel/PDF разряды тысяч были с пробелом.
    perf_num_fmt_by_col = {
        2: NF_INT,   # Плановая выработка, га/сутки
        3: NF_INT,   # Кол-во техники
        4: NF_DEC2,  # Обработано за сутки, га
        5: NF_DEC2,  # Выработка за сутки, га/1 ед
        6: NF_DEC2,  # Средняя выработка по сезону, га/сутки
        9: NF_DEC1,  # Часовая выработка, га
        10: NF_INT,  # Доступно к работе агрегатов
        11: NF_INT,  # Всего самоходных наёмных агрегатов
    }

    row    = 2
    t_mach = t_area = t_hours = 0.0
    t_season_sum   = 0.0
    t_machine_days = 0

    for firm in target_firms:
        s      = day_stats.get(firm, _empty_stats())
        n_mach = s["n_mach"]
        area   = _firm_day_area_ha(s)
        hours  = s["hours_today"]
        avg_s  = s["avg_season"]
        plan   = get_plan_rate_for_group(group_name, firm)
        avail  = own_count.get(firm, 0)            # собственных в наличии (из конфига)
        hired  = s["n_hired"]                        # наёмных задействованных (из day_stats)

        rate = round(area / n_mach, 2) if n_mach else 0.0
        eff_h = round(hours / n_mach, 4) if n_mach else 0.0
        vals = [
            firm,
            plan,
            n_mach,
            round(area, 2),
            rate,
            avg_s,
            decimal_hours_to_hhmm(hours),
            decimal_hours_to_hhmm(eff_h),
            round(area / hours, 1) if hours else 0.0,
            avail,
            hired,
        ]
        for ci, v in enumerate(vals, start=1):
            bg = _COLOR_DATA
            if ci == 5 and n_mach and rate < get_plan_rate_for_group(group_name, firm):
                bg = _COLOR_ALERT_PROD
            display_v, nf = _display_val(ci, v)
            _cell(
                ws,
                row,
                ci,
                display_v,
                halign="left" if ci == 1 else "center",
                bg=bg,
                number_format=nf,
            )
        t_mach  += n_mach
        t_area  += area
        t_hours += hours
        t_season_sum   += s.get("season_sum", 0.0)
        t_machine_days += s.get("machine_days", 0)
        row += 1

    avg_total = round(t_season_sum / t_machine_days, 1) if t_machine_days else 0.0
    t_area_total = _today_area_total_ha(day_stats, target_firms)
    tot_rate = round(t_area_total / t_mach, 2) if t_mach else 0.0
    tot_eff_h = round(t_hours / t_mach, 4) if t_mach else 0.0
    totals = [
        "Итого",
        get_plan_rate_for_group(group_name, target_firms[0] if target_firms else list(AGROFIRMS.keys())[0]),
        int(t_mach),
        round(t_area_total, 2),
        tot_rate,
        avg_total,
        decimal_hours_to_hhmm(t_hours),
        decimal_hours_to_hhmm(tot_eff_h),
        round(t_area_total / t_hours, 1) if t_hours else 0.0,
        sum(own_count.get(f, 0) for f in target_firms),
        sum(day_stats.get(f, _empty_stats())["n_hired"] for f in target_firms),
    ]
    for ci, v in enumerate(totals, start=1):
        bg = _COLOR_TOTAL
        if ci == 5 and t_mach and tot_rate < get_plan_rate_for_group(group_name, target_firms[0] if target_firms else list(AGROFIRMS.keys())[0]):
            bg = _COLOR_ALERT_PROD
        display_v, nf = _display_val(ci, v)
        _cell(
            ws,
            row,
            ci,
            display_v,
            bold=True,
            halign="left" if ci == 1 else "center",
            bg=bg,
            number_format=nf,
        )

    ws.column_dimensions["A"].width = 30
    for ci in range(2, n + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 18
    _auto_fit_row_heights(ws, 2, row, n, min_height=16.0, line_height=12.5)


def write_holding_fertilizer_summary_sheet(
    wb: openpyxl.Workbook,
    fert_data: dict[str, dict[int, dict[str, float]]],
) -> None:
    """
    Свод по удобрениям по холдингу (сумма по агрофирмам).
    Вставляется после листов по фирмам и перед блоком 2.1 и следующими группами.
    """
    if not raps_fert_order:
        return
    ws = wb.create_sheet("Свод по удобрениям (ХК)")
    headers = ["№", "Удобрение", "За сезон, га", "За сутки, га"]
    for ci, h in enumerate(headers, start=1):
        _cell(ws, 1, ci, h, bold=True, color="FFFFFF", bg=_COLOR_HDR)
    ws.row_dimensions[1].height = 36
    row = 2
    tot_s = tot_t = 0.0
    for idx, (fid, fname) in enumerate(raps_fert_order, start=1):
        s_sum = sum(
            (fert_data.get(firm, {}).get(fid) or {}).get("season", 0.0)
            for firm in AGROFIRMS
        )
        t_sum = sum(
            (fert_data.get(firm, {}).get(fid) or {}).get("today", 0.0)
            for firm in AGROFIRMS
        )
        s_sum = round(s_sum, 1)
        t_sum = round(t_sum, 2)
        tot_s += s_sum
        tot_t += t_sum
        _cell(ws, row, 1, idx, bg=_COLOR_DATA)
        _cell(ws, row, 2, fname, halign="left", bg=_COLOR_DATA)
        _cell(ws, row, 3, s_sum, bg=_COLOR_DATA, number_format=NF_DEC1)
        _cell(ws, row, 4, t_sum, bg=_COLOR_DATA, number_format=NF_DEC2)
        row += 1
    _cell(ws, row, 1, "", bold=True, bg=_COLOR_TOTAL)
    _cell(ws, row, 2, "Итого", bold=True, bg=_COLOR_TOTAL, halign="left")
    _cell(ws, row, 3, round(tot_s, 1), bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC1)
    _cell(ws, row, 4, round(tot_t, 2), bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 42
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 18


# =============================================================================
# ЛИСТЫ ПО АГРОФИРМАМ (только WORK_TYPE_ID=55)
# =============================================================================

def write_firm_sheet(wb:          openpyxl.Workbook,
                     firm_name:   str,
                     season_data: dict[str, dict[str, float]],
                     day_stats:   dict[str, dict],
                     plan_data:   dict[str, dict[str, float]],
                     fert_data:   dict[str, dict[int, dict[str, float]]] | None = None) -> None:
    crops_season = season_data.get(firm_name, {})
    crops_today  = day_stats.get(firm_name, {}).get("area_by_crop", {})
    stats        = day_stats.get(firm_name, _empty_stats())

    has_data = any(
        get_fact_total(crop, crops_season, crops_today, crop == "Многолетние травы") or
        _plan_ha_like_report2(firm_name, crop)
        for crop in SUMMARY_CROPS
    ) or bool(stats.get("detail_own") or stats.get("detail_hired"))

    if REPORT1_HIDE_EMPTY_TABLES and not has_data:
        return

    ws  = wb.create_sheet(firm_name[:28])
    row = 1

    # Динамическая шапка: № + фикс. колонки + по удобрению + … (колонка A — как у блоков СОБСТВЕННЫЕ/НАЁМНЫЕ)
    firm_hdrs = (
        ["№", "Культура", "План обраб. пл., га", "Факт общий, га"]
        + [f"Удобрение: {fname}, га" for _, fname in raps_fert_order]
        + ["Остаток к выполнению, га", "Доля факта от плана, %", "Факт за сутки, га"]
        + [f"Удобрение: {fname}, га (сутки)" for _, fname in raps_fert_order]
        + ["Прогнозная дата завершения по фактической выработке"]
    )
    for ci, h in enumerate(firm_hdrs, start=1):
        fs = 10 if "Прогнозная дата завершения" in h else 11
        _cell(ws, row, ci, h, bold=True, color="FFFFFF", bg=_COLOR_HDR, font_size=fs)
    ws.row_dimensions[row].height = 72
    row += 1

    # Вычислим номера ключевых колонок (кол. 1 — №, кол. 2 — Культура, …)
    n_fert      = len(raps_fert_order)
    col_crop    = 2
    col_plan    = 3
    col_fact    = 4
    col_fert0   = 5                      # первое удобрение сезон
    col_rem     = 5 + n_fert             # остаток
    col_share   = col_rem + 1
    col_today   = col_rem + 2
    col_fert0t  = col_today + 1          # первое удобрение сутки
    col_fcd     = col_fert0t + n_fert    # прогноз

    total_plan = total_fact = total_today = total_rem = 0.0

    for idx, crop in enumerate(SUMMARY_CROPS, start=1):
        is_multi  = crop == "Многолетние травы"
        fd = (fert_data or {}).get(firm_name, {})
        fact_val  = round(_mu_season_fact(
            crop, firm_name, crops_season, crops_today, is_multi=is_multi,
        ), 2)
        if crop == "Рапс озимый":
            today_val = round(sum(
                fd.get(fid, {}).get("today", 0.0)
                for fid in _raps_get_selected_fert_ids()
            ), 2)
        else:
            today_val = round(
                _sum_multi_year_grass(crops_today)
                if is_multi else crops_today.get(crop, 0.0), 2,
            )
        plan_val = _plan_ha_like_report2(firm_name, crop)
        rem   = round(plan_val - fact_val, 2)
        share = round(fact_val / plan_val * 100, 2) if plan_val > 0 else 0.0
        fcd   = compute_forecast_end_date(plan_val, fact_val, today_val)
        row_bg = _CROP_HEADER_FILL.get(crop, _COLOR_DATA)
        fc_bg  = _COLOR_ALERT_FORECAST if forecast_date_is_alert(fcd) else _COLOR_DATA

        _cell(ws, row, 1, idx, bg=_COLOR_DATA)
        _cell(ws, row, col_crop,  crop,       halign="left", bg=row_bg)
        _cell(ws, row, col_plan,  plan_val if plan_val else 0, bg=_COLOR_DATA, number_format=NF_DEC1)
        _cell(ws, row, col_fact,  fact_val,   bg=_COLOR_DATA, number_format=NF_DEC2)

        for i, (fid, _) in enumerate(raps_fert_order):
            v = fd.get(fid, {}).get("season", 0.0) if crop == "Рапс озимый" else 0.0
            _cell(ws, row, col_fert0 + i, v,
                  bg=_COLOR_DATA, number_format=NF_DEC1)

        _cell(ws, row, col_rem,   rem,        bg=_COLOR_DATA, number_format=NF_DEC2)
        _cell(ws, row, col_share, share,      bg=_COLOR_DATA, number_format="0.00")
        _cell(ws, row, col_today, today_val,  bg=_COLOR_DATA, number_format=NF_DEC2)
        for i, (fid, _) in enumerate(raps_fert_order):
            v = fd.get(fid, {}).get("today", 0.0) if crop == "Рапс озимый" else 0.0
            _cell(ws, row, col_fert0t + i, v,
                  bg=_COLOR_DATA, number_format=NF_DEC2)
        _cell(ws, row, col_fcd, fcd, bg=fc_bg, number_format=NF_DATE)

        total_plan  += plan_val
        total_fact  += fact_val
        total_today += today_val
        total_rem   += rem
        row += 1

    # Итоговая строка
    fd_total = (fert_data or {}).get(firm_name, {})
    _cell(ws, row, 1, "", bold=True, bg=_COLOR_TOTAL)
    _cell(ws, row, col_crop,  "Итого", bold=True, bg=_COLOR_TOTAL)
    _cell(ws, row, col_plan,  round(total_plan, 1),  bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC1)
    _cell(ws, row, col_fact,  round(total_fact, 2),  bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
    for i, (fid, _) in enumerate(raps_fert_order):
        _cell(ws, row, col_fert0 + i,
              round(fd_total.get(fid, {}).get("season", 0.0), 1),
              bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC1)
    _cell(ws, row, col_rem,   round(total_rem, 2), bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
    _cell(ws, row, col_share,
          round(total_fact / total_plan * 100, 2) if total_plan > 0 else 0.0,
          bold=True, bg=_COLOR_TOTAL, number_format="0.00")
    _cell(ws, row, col_today, round(total_today, 2), bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
    for i, (fid, _) in enumerate(raps_fert_order):
        _cell(ws, row, col_fert0t + i,
              round(fd_total.get(fid, {}).get("today", 0.0), 2),
              bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
    _cell(ws, row, col_fcd, None, bold=True, bg=_COLOR_TOTAL)
    row += 2

    sec_merge_end = max(7, col_fcd)
    for section_name, rows_data in [
        ("СОБСТВЕННЫЕ", stats["detail_own"]),
        ("НАЁМНЫЕ", stats["detail_hired"]),
    ]:
        if not rows_data:
            continue
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=sec_merge_end)
        c = ws.cell(row=row, column=1, value=section_name)
        c.font = Font(bold=True, size=12, color=_COLOR_TITLE_TX)
        c.fill = PatternFill("solid", fgColor=_COLOR_DATA)
        c.border = _border()
        c.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[row].height = 24
        row += 1

        for ci, h in enumerate(
            ["№", "Машина", "Площадь, га", "Время на полях",
             "Выработка га/час", "Ср. скорость км/ч", "Коэф. шир. захвата, %"],
            start=1
        ):
            fs = 10 if ci >= 5 else 11
            _cell(ws, row, ci, h, bold=True, color="FFFFFF", bg=_COLOR_HDR, font_size=fs)
        ws.row_dimensions[row].height = 54
        row += 1

        for i, rd in enumerate(rows_data, start=1):
            for ci, v in enumerate(
                [i, rd["machine_name"], rd["area"],
                 seconds_to_hhmm(rd["work_secs"]),
                 rd["rate"], rd["speed"], rd["coef"]], start=1
            ):
                _cell(ws, row, ci, v,
                      halign="left" if ci == 2 else "center",
                      bg=_COLOR_DATA, font_size=11)
            ws.row_dimensions[row].height = 44
            row += 1

        tc  = round(sum(r["area"]  for r in rows_data), 2)
        ts  = sum(r["work_secs"]   for r in rows_data)
        tr  = round(tc / (ts / 3600), 2) if ts else 0.0
        asp = round(sum(r["speed"] for r in rows_data) / len(rows_data))
        acf = round(sum(r["coef"]  for r in rows_data) / len(rows_data))
        for ci, v in enumerate(
            ["Итого", "", tc, seconds_to_hhmm(ts), tr, asp, acf], start=1
        ):
            _cell(ws, row, ci, v, bold=True, bg=_COLOR_TOTAL, font_size=12)
        ws.row_dimensions[row].height = 30
        row += 2

    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 40
    last_data_col = col_fcd
    for ci in range(3, min(8, last_data_col + 1)):
        ws.column_dimensions[get_column_letter(ci)].width = 19
    for ci in range(8, last_data_col + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 17
    for ci in range(col_fert0, min(col_rem, last_data_col + 1)):
        ws.column_dimensions[get_column_letter(ci)].width = 19
    for ci in range(col_fert0t, min(col_fcd, last_data_col + 1)):
        ws.column_dimensions[get_column_letter(ci)].width = 19
    ws.column_dimensions[get_column_letter(col_fcd)].width = 40


def write_comparison_sheet(
    wb:        openpyxl.Workbook,
    cmp_mtfmi: dict[int, float],        # field_id → га из mtfmi
    cmp_agro:  dict[int, dict],         # field_id → {area, ferts, firm, crop}
) -> None:
    """
    Лист «Сравнение источников»: по каждому полю показывает площадь
    из machine_tasks/mtfmi и из agro_operations, их разницу,
    а также список удобрений из agro_operations.
    Строки с расхождением > 1 га подсвечиваются жёлтым.
    """
    ws = wb.create_sheet("Сравнение источников")

    # Заголовок
    title = (
        f"Сравнение источников площадей внесения удобрений  |  "
        f"machine_tasks/mtfmi  vs  agro_operations  |  {REPORT_DATE}"
    )
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=9)
    c = ws.cell(row=1, column=1, value=title)
    c.font      = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
    c.fill      = PatternFill("solid", fgColor=_COLOR_HDR)
    c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    c.border    = _border()
    ws.row_dimensions[1].height = 30

    # Шапка (10 колонок — добавлена детализация операций)
    NCOLS = 10
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=NCOLS)
    c_title = ws.cell(row=1, column=1)
    c_title.value     = ws.cell(row=1, column=1).value  # уже заполнен выше
    headers = [
        "Агрофирма",
        "Поле (id)",
        "Название поля",
        "Культура",
        "га mtfmi\n(machine_tasks)",
        "га agro_ops\n(completed_area)",
        "Разница, га\n(agro − mtfmi)",
        "Кол-во\nопераций",
        "Удобрения (суммарно по полю)",
        "Площадь пашни, га",
    ]
    for ci, h in enumerate(headers, start=1):
        _cell(ws, 2, ci, h, bold=True, color="FFFFFF", bg=_COLOR_HDR, font_size=11)
    ws.row_dimensions[2].height = 40

    # Собираем все field_id из обоих источников
    all_field_ids = sorted(set(cmp_mtfmi) | set(cmp_agro))

    _COLOR_DIFF       = "FFF3CD"  # жёлтый — расхождение > 1 га
    _COLOR_ONLY_MTFMI = "E3F2FD"  # голубой — только в mtfmi
    _COLOR_ONLY_AGRO  = "E8F5E9"  # зелёный — только в agro
    _COLOR_OP_ROW     = "F5F5F5"  # серый — строка отдельной операции

    row = 3
    for fid in all_field_ids:
        mtfmi_area = round(cmp_mtfmi.get(fid, 0.0), 2)
        agro_entry = cmp_agro.get(fid)
        agro_area  = round(agro_entry["area"], 2) if agro_entry else 0.0
        diff       = round(agro_area - mtfmi_area, 2)

        firm  = agro_entry["firm"] if agro_entry else get_firm_for_field(fid)
        from mtfmi_batch_fetch import UNDEFINED_CROP

        crop  = agro_entry["crop"] if agro_entry else UNDEFINED_CROP
        fname = field_name_map.get(fid, f"field_{fid}")
        farea = round(field_area_map.get(fid, 0.0), 2)
        ops_list = agro_entry.get("ops", []) if agro_entry else []
        n_ops = len(ops_list)

        # Удобрения суммарно по полю
        if agro_entry and agro_entry["ferts"]:
            fert_strs = [
                f"{fertilizer_name_map.get(fid_f, f'fert_{fid_f}')}: {round(v, 2)} га"
                for fid_f, v in sorted(agro_entry["ferts"].items(), key=lambda x: -x[1])
            ]
            ferts_text = "; ".join(fert_strs)
        else:
            ferts_text = "—"

        # Цвет строки поля
        if mtfmi_area == 0:
            bg = _COLOR_ONLY_AGRO
        elif agro_area == 0:
            bg = _COLOR_ONLY_MTFMI
        elif abs(diff) > 1.0:
            bg = _COLOR_DIFF
        else:
            bg = _COLOR_DATA

        vals = [firm, fid, fname, crop,
                mtfmi_area if mtfmi_area else 0,
                agro_area  if agro_area  else 0,
                diff, n_ops, ferts_text, farea]
        for ci, v in enumerate(vals, start=1):
            fmt = NF_DEC2 if ci in (5, 6, 7, 10) else None
            _cell(ws, row, ci, v, bg=bg, bold=(n_ops > 1),
                  halign="left" if ci in (1, 3, 4, 9) else "center",
                  number_format=fmt, font_size=10)
        ws.row_dimensions[row].height = 18
        row += 1

        # Детализация по операциям — выводим если их больше одной (именно здесь риск задвоения)
        if n_ops > 1:
            for op_info in ops_list:
                op_ferts = "; ".join(
                    fertilizer_name_map.get(f, str(f)) for f in op_info["ferts"]
                )
                today_mark = " ✓сутки" if op_info["is_today"] else ""
                op_vals = [
                    "", f"  └ op#{op_info['op_id']}", "", "",
                    "", round(op_info["area"], 2), "", "",
                    f"{op_ferts}{today_mark}", "",
                ]
                for ci, v in enumerate(op_vals, start=1):
                    fmt = NF_DEC2 if ci == 6 else None
                    _cell(ws, row, ci, v, bg=_COLOR_OP_ROW,
                          halign="left" if ci in (2, 9) else "center",
                          number_format=fmt, font_size=9)
                ws.row_dimensions[row].height = 15
                row += 1

    # Итоговая строка
    total_mtfmi = round(sum(cmp_mtfmi.values()), 2)
    total_agro  = round(sum(e["area"] for e in cmp_agro.values()), 2)
    total_diff  = round(total_agro - total_mtfmi, 2)
    for ci, v in enumerate(
        ["Итого", "", "", "", total_mtfmi, total_agro, total_diff, "", "", ""],
        start=1,
    ):
        fmt = NF_DEC2 if ci in (5, 6, 7) else None
        _cell(ws, row, ci, v, bold=True, bg=_COLOR_TOTAL,
              halign="center", number_format=fmt, font_size=10)

    # Легенда
    row += 2
    legend = [
        (_COLOR_ONLY_MTFMI, "Поле есть только в machine_tasks/mtfmi (нет в agro_operations)"),
        (_COLOR_ONLY_AGRO,  "Поле есть только в agro_operations (нет в machine_tasks/mtfmi)"),
        (_COLOR_DIFF,       "Расхождение площадей > 1 га  |  жирный шрифт = несколько операций на поле"),
        (_COLOR_DATA,       "Данные совпадают (расхождение ≤ 1 га)"),
        (_COLOR_OP_ROW,     "Детализация по отдельным операциям (строки с └)"),
    ]
    for color, text in legend:
        ws.cell(row=row, column=1).fill   = PatternFill("solid", fgColor=color)
        ws.cell(row=row, column=1).border = _border()
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=NCOLS)
        c2 = ws.cell(row=row, column=2, value=text)
        c2.font      = Font(name="Calibri", size=10, italic=True)
        c2.alignment = Alignment(horizontal="left", vertical="center")
        row += 1

    # Ширины колонок
    col_widths = [22, 14, 28, 20, 16, 16, 14, 10, 60, 16]
    for ci, w in enumerate(col_widths, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    ws.freeze_panes = "A3"
    print(f"    Лист сравнения: {len(all_field_ids)} полей")



def write_today_fert_detail_sheet(wb: openpyxl.Workbook, today_ops_detail: list[dict]) -> None:
    """
    Лист детализации: какие операции засчитаны в 'Удобрения за сутки'.
    Строка = одна agro_operation с is_today=True.
    """
    ws = wb.create_sheet("Сутки — детализация удобрений")

    headers = [
        "Агрофирма", "Поле (id)", "Название поля", "Культура",
        "op_id", "Статус", "Пл. операции, га", "Пл. за сутки (mtfmi), га",
        "Дата операции", "Удобрения",
    ]
    for ci, h in enumerate(headers, start=1):
        _cell(ws, 1, ci, h, bold=True, color="FFFFFF", bg=_COLOR_HDR, font_size=10)
    ws.row_dimensions[1].height = 36

    # Группируем по агрофирме для итогов
    firm_totals: dict[str, float] = defaultdict(float)

    row = 2
    for rec in sorted(today_ops_detail, key=lambda x: (x["firm"], x["field_id"])):
        fert_names = ", ".join(
            fertilizer_name_map.get(f, str(f)) for f in rec["ferts"]
        )
        op_dt_str = rec["op_dt"].strftime("%d.%m %H:%M") if rec["op_dt"] else "—"
        vals = [
            rec["firm"],
            rec["field_id"],
            rec["field_name"],
            rec["crop"],
            rec["op_id"],
            rec["status"],
            round(rec["area_op"], 2),
            round(rec["area_today"], 2),
            op_dt_str,
            fert_names,
        ]
        for ci, v in enumerate(vals, start=1):
            fmt = NF_DEC2 if ci in (7, 8) else None
            _cell(ws, row, ci, v,
                  halign="left" if ci in (1, 3, 4, 10) else "center",
                  bg=_COLOR_DATA, number_format=fmt, font_size=10)
        firm_totals[rec["firm"]] += rec["area_today"]
        ws.row_dimensions[row].height = 16
        row += 1

    # Итоги по агрофирмам
    row += 1
    _cell(ws, row, 1, "ИТОГИ ПО АГРОФИРМАМ", bold=True, bg=_COLOR_HDR,
          color="FFFFFF", font_size=10)
    row += 1
    for firm in AGROFIRMS:
        total = round(firm_totals.get(firm, 0.0), 2)
        _cell(ws, row, 1, firm,  bold=True, bg=_COLOR_TOTAL, halign="left", font_size=10)
        _cell(ws, row, 8, total, bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2, font_size=10)
        row += 1

    col_widths = [24, 10, 28, 20, 10, 12, 14, 18, 14, 50]
    for ci, w in enumerate(col_widths, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.freeze_panes = "A2"
    print(f"    Лист детализации суток: {len(today_ops_detail)} операций")



# =============================================================================
# ЛИСТ «ВНЕСЕНИЕ УДОБРЕНИЙ — ПО КУЛЬТУРЕ» (фиксированные колонки Селитра/Сульфат)
# Формат: Агрофирма | План, га | Факт (Селитра | Сульфат) | Остаток | % | Факт сутки | Прогноз
# =============================================================================

def _fert_fact_for_crop(crop: str, firm: str) -> float:
    """
    Факт общий для таблицы удобрений.
    Должен быть равен сумме двух отображаемых колонок: Селитра + Сульфат.
    """
    nitrate_val, _ = _fert_by_fid(crop, firm, FERT_NITRATE_ID)
    sulfate_val, _ = _fert_by_fid(crop, firm, FERT_SULFATE_ID)
    by_visible_cols = round(float(nitrate_val) + float(sulfate_val), 2)
    # Fallback на старое поведение, если фиксированные удобрения не определились.
    if by_visible_cols > 0 or (FERT_NITRATE_ID is not None or FERT_SULFATE_ID is not None):
        return by_visible_cols
    return round(all_crops_fact_agro.get(crop, {}).get(firm, 0.0), 2)


def _mu_season_fact(
    crop: str,
    firm: str,
    crops_season: dict[str, float],
    crops_today: dict[str, float],
    *,
    is_multi: bool = False,
) -> float:
    """
    Факт общий для блока МУ.
    Источник — season_data (machine_tasks/mtfmi), как и во всех остальных сводках:
    это единственный источник, который фильтрует поля по available_field_ids
    и обрезает сезон по REPORT_DATE (shift_date < REPORT_DATE).
    agro_operations (_fert_fact_for_crop) здесь больше НЕ используется для
    культур из FERT_TABLE_CROPS — там нет фильтра available_field_ids и нет
    верхней границы по дате отчёта, из-за чего факт был завышен относительно
    эталона (см. diag_wheat_fert.py).
    """
    return float(get_fact_total(crop, crops_season, crops_today, is_multi))

def _fert_today_for_crop(crop: str, firm: str) -> float:
    """Факт за сутки из agro_operations для культуры."""
    return round(all_crops_fact_today.get(crop, {}).get(firm, 0.0), 2)

def _fert_by_fid(crop: str, firm: str, fert_id: int | None) -> tuple[float, float]:
    """
    Возвращает (season, today) для конкретного удобрения, культуры и фирмы.
    Если точный fert_id не найден — суммирует все удобрения с совпадающим
    ключевым словом в названии (селитра / сульфат), чтобы не терять данные
    когда в agro_operations у культуры используется другой id того же типа.
    """
    if fert_id is None:
        return 0.0, 0.0
    firm_data = all_crops_fert_data.get(crop, {}).get(firm, {})
    d = firm_data.get(fert_id)
    if d:
        return d.get("season", 0.0), d.get("today", 0.0)
    # Fallback: ищем все fert_id с таким же ключевым словом в названии
    key = ""
    if fert_id == FERT_NITRATE_ID:
        key = FERT_NITRATE_KEY
    elif fert_id == FERT_SULFATE_ID:
        key = FERT_SULFATE_KEY
    if not key:
        return 0.0, 0.0
    season_sum = today_sum = 0.0
    for fid, vals in firm_data.items():
        fname = fertilizer_name_map.get(fid, "").lower()
        if key in fname:
            season_sum += vals.get("season", 0.0)
            today_sum  += vals.get("today",  0.0)
    return round(season_sum, 4), round(today_sum, 4)


def write_fert_crop_table(
    ws,
    crop:        str,
    plan_data:   dict[str, dict[str, float]],
    start_row:   int,
    col_offset:  int = 1,
    day_stats:   dict[str, dict] | None = None,
    season_data: dict[str, dict[str, float]] | None = None,
) -> int:
    """
    Записывает таблицу внесения удобрений для одной культуры на лист ws.
    Возвращает следующую свободную строку.
    Колонки: Агрофирма | План, га | Селитра, га | Сульфат, га | Остаток, га | % | Факт сутки, га | Прогнозная дата
    """
    HDR_COLS = ["Агрофирма", "План, га", "Селитра, га", "Сульфат, га",
                "Остаток, га", "%", "Факт сутки, га", "Прогнозная дата"]
    N = len(HDR_COLS)
    c0 = col_offset

    # Для этих культур показываем только фирмы, где есть данные.
    active_firms_only_crops = {
        "Тритикале озимая",
        "Однолетние травы",
        "Пар",
        "Картофель",
        "Морковь",
        "Свекла столовая",
    }

    row_payloads: list[dict] = []
    for firm in AGROFIRMS:
        plan_val = _plan_ha_like_report2(firm, crop)

        # Факт (сезон) берём из season_data (machine_tasks/mtfmi) — эталонный
        # источник, согласован с diag_wheat_fert.py: фильтр available_field_ids
        # + обрезка сезона по REPORT_DATE. agro_operations (_fert_fact_for_crop)
        # для суммарного факта больше не используем — там эти фильтры отсутствуют,
        # из-за чего итог был завышен (напр. Пшеница озимая: 88177.28 вместо 85624.69 га).
        is_multi = _is_multi_year_grass_crop(crop)
        if season_data is not None:
            crops_season_firm = season_data.get(firm, {})
            fact_val = round(get_fact_total(crop, crops_season_firm, {}, is_multi), 2)
        else:
            # Фолбэк на старое поведение, если season_data почему-то не передан.
            fact_val = _fert_fact_for_crop(crop, firm)
        # Факт за сутки берём из machine_tasks/mtfmi (day_stats),
        # как в write_sheet1 — это эталонный источник (телематика).
        # agro_operations (_fert_today_for_crop) не используем: там неполные данные.
        if day_stats is not None:
            crops_today = day_stats.get(firm, {}).get("area_by_crop", {})
            today_val = round(
                _sum_multi_year_grass(crops_today)
                if is_multi else crops_today.get(crop, 0.0), 2
            )
        else:
            today_val = _fert_today_for_crop(crop, firm)
        nitrate_val, _ = _fert_by_fid(crop, firm, FERT_NITRATE_ID)
        sulfate_val, _ = _fert_by_fid(crop, firm, FERT_SULFATE_ID)
        rem_val = round(plan_val - fact_val, 2)
        share_val = round(fact_val / plan_val * 100, 2) if plan_val > 0 else 0.0
        fcd = compute_forecast_end_date(plan_val, fact_val, today_val)
        fc_bg = _COLOR_ALERT_FORECAST if forecast_date_is_alert(fcd) else _COLOR_DATA

        has_data = any(
            abs(float(v)) > 1e-9
            for v in (plan_val, fact_val, today_val, nitrate_val, sulfate_val)
        )
        row_payloads.append(
            {
                "firm": firm,
                "plan": float(plan_val or 0.0),
                "fact": float(fact_val or 0.0),
                "today": float(today_val or 0.0),
                "nitrate": float(nitrate_val or 0.0),
                "sulfate": float(sulfate_val or 0.0),
                "rem": rem_val,
                "share": share_val,
                "fcd": fcd,
                "fc_bg": fc_bg,
                "has_data": has_data,
            }
        )

    if crop in active_firms_only_crops:
        row_payloads = [r for r in row_payloads if r["has_data"]]

    # Заголовок культуры (объединённая строка)
    # Для слайдов удобрений используем нейтральную шапку без разноцветных подложек.
    crop_fill = "FFFFFF"
    ws.merge_cells(start_row=start_row, start_column=c0,
                   end_row=start_row, end_column=c0 + N - 1)
    _cell(ws, start_row, c0, crop, bold=True, color="000000",
          bg=crop_fill, halign="center", font_size=11)
    start_row += 1

    # Подзаголовок «Факт, га» над Селитрой/Сульфатом
    _cell(ws, start_row, c0,     "Агрофирма",      bold=True, color="FFFFFF", bg=_COLOR_HDR, halign="center")
    _cell(ws, start_row, c0 + 1, "План, га",        bold=True, color="FFFFFF", bg=_COLOR_HDR, halign="center")
    ws.merge_cells(start_row=start_row, start_column=c0 + 2,
                   end_row=start_row, end_column=c0 + 3)
    _cell(ws, start_row, c0 + 2, "Факт, га",        bold=True, color="FFFFFF", bg=_COLOR_HDR, halign="center")
    _cell(ws, start_row, c0 + 4, "Остаток, га",     bold=True, color="FFFFFF", bg=_COLOR_HDR, halign="center")
    _cell(ws, start_row, c0 + 5, "%",               bold=True, color="FFFFFF", bg=_COLOR_HDR, halign="center")
    _cell(ws, start_row, c0 + 6, "Факт сутки, га",  bold=True, color="FFFFFF", bg=_COLOR_HDR, halign="center")
    _cell(ws, start_row, c0 + 7, "Прогнозная дата", bold=True, color="FFFFFF", bg=_COLOR_HDR,
          halign="center", font_size=9)
    ws.row_dimensions[start_row].height = 30
    start_row += 1

    # Строка с именами подколонок Селитра / Сульфат
    _cell(ws, start_row, c0,     "",         bold=True, color="FFFFFF", bg=_COLOR_HDR)
    _cell(ws, start_row, c0 + 1, "",         bold=True, color="FFFFFF", bg=_COLOR_HDR)
    _cell(ws, start_row, c0 + 2, "Селитра",  bold=True, color="FFFFFF", bg=_COLOR_HDR, halign="center")
    _cell(ws, start_row, c0 + 3, "Сульфат",  bold=True, color="FFFFFF", bg=_COLOR_HDR, halign="center")
    _cell(ws, start_row, c0 + 4, "",         bold=True, color="FFFFFF", bg=_COLOR_HDR)
    _cell(ws, start_row, c0 + 5, "",         bold=True, color="FFFFFF", bg=_COLOR_HDR)
    _cell(ws, start_row, c0 + 6, "",         bold=True, color="FFFFFF", bg=_COLOR_HDR)
    _cell(ws, start_row, c0 + 7, "",         bold=True, color="FFFFFF", bg=_COLOR_HDR)
    ws.row_dimensions[start_row].height = 24
    start_row += 1

    # Объединяем фиксированные колонки только по двум строкам заголовка:
    # (строка с названиями колонок + строка подколонок Селитра/Сульфат).
    hdr1 = start_row - 2  # строка с "Агрофирма", "План, га", ...
    hdr2 = start_row - 1  # строка с "Селитра"/"Сульфат"
    for col_idx in [c0, c0 + 1, c0 + 4, c0 + 5, c0 + 6, c0 + 7]:
        ws.merge_cells(start_row=hdr1, start_column=col_idx,
                       end_row=hdr2, end_column=col_idx)

    # Данные по фирмам
    data_start = start_row
    tot_plan = tot_nitrate = tot_sulfate = tot_fact = tot_today = 0.0

    for r in row_payloads:
        firm = r["firm"]
        plan_val = r["plan"]
        fact_val = r["fact"]
        today_val = r["today"]
        nitrate_val = r["nitrate"]
        sulfate_val = r["sulfate"]
        rem_val = r["rem"]
        share_val = r["share"]
        fcd = r["fcd"]
        fc_bg = r["fc_bg"]

        _cell(ws, start_row, c0,     firm,        halign="left",   bg=_COLOR_DATA)
        _cell(ws, start_row, c0 + 1, plan_val if plan_val else 0,
              bg=_COLOR_DATA, number_format=NF_DEC1)
        _cell(ws, start_row, c0 + 2, nitrate_val if nitrate_val else 0,
              bg=_COLOR_DATA, number_format=NF_DEC2)
        _cell(ws, start_row, c0 + 3, sulfate_val if sulfate_val else 0,
              bg=_COLOR_DATA, number_format=NF_DEC2)
        _cell(ws, start_row, c0 + 4, rem_val,     bg=_COLOR_DATA, number_format=NF_DEC2)
        _cell(ws, start_row, c0 + 5, share_val,   bg=_COLOR_DATA, number_format="0.00")
        _cell(ws, start_row, c0 + 6, today_val if today_val else 0,
              bg=_COLOR_DATA, number_format=NF_DEC2)
        _cell(ws, start_row, c0 + 7, fcd,         bg=fc_bg,        number_format=NF_DATE)

        tot_plan    += plan_val
        tot_nitrate += nitrate_val
        tot_sulfate += sulfate_val
        tot_fact    += fact_val
        tot_today   += today_val
        start_row   += 1

    # Итого
    tot_rem   = round(sum(float(r["rem"] or 0.0) for r in row_payloads), 2)
    tot_share = round(tot_fact / tot_plan * 100, 2) if tot_plan > 0 else 0.0
    _cell(ws, start_row, c0,     "Итого",             bold=True, bg=_COLOR_TOTAL, halign="left", color="000000")
    _cell(ws, start_row, c0 + 1, round(tot_plan, 1),  bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC1)
    _cell(ws, start_row, c0 + 2, round(tot_nitrate, 2), bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
    _cell(ws, start_row, c0 + 3, round(tot_sulfate, 2), bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
    _cell(ws, start_row, c0 + 4, tot_rem,             bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
    _cell(ws, start_row, c0 + 5, tot_share,           bold=True, bg=_COLOR_TOTAL, number_format="0.00")
    _cell(ws, start_row, c0 + 6, round(tot_today, 2), bold=True, bg=_COLOR_TOTAL, number_format=NF_DEC2)
    _cell(ws, start_row, c0 + 7, None,                bold=True, bg=_COLOR_TOTAL)

    return start_row + 2  # +1 строка отступа между таблицами


def write_fert_slide_sheet(
    wb:          openpyxl.Workbook,
    crops:       list[str],
    plan_data:   dict[str, dict[str, float]],
    title:       str,
    index:       int,
    day_stats:   dict[str, dict] | None = None,
    season_data: dict[str, dict[str, float]] | None = None,
) -> None:
    """
    Создаёт лист с двумя таблицами внесения удобрений (по одной на каждую культуру из crops).
    """
    ws = wb.create_sheet(title[:31], index)
    row = 1
    for crop in crops:
        row = write_fert_crop_table(ws, crop, plan_data, row, day_stats=day_stats, season_data=season_data)

    # Ширины колонок
    ws.column_dimensions["A"].width = 28
    for ci in range(2, 9):
        ws.column_dimensions[get_column_letter(ci)].width = 14
    ws.column_dimensions[get_column_letter(8)].width = 20


# =============================================================================
# ЛИСТ «ВСПАШКА + ГЛУБОКОРЫХЛЕНИЕ» — две таблицы на одном листе
# =============================================================================

def write_tillage_combined_sheet(
    wb:         openpyxl.Workbook,
    stats_dict: dict[str, dict[str, dict]],  # group_name → firm → stats
    index:      int,
) -> None:
    """
    Два блока выработки (Вспашка и Глубокорыхление) на одном листе.
    """
    TILLAGE_GROUPS = ["2.4 Вспашка", "2.8 Глубокорыхление"]
    ws = wb.create_sheet("Вспашка + Глубокорыхление"[:31], index)
    n  = len(PERF_COLS)

    row = 1
    for group_name in TILLAGE_GROUPS:
        gstats = stats_dict.get(group_name, {firm: _empty_stats() for firm in AGROFIRMS})
        own_count = _fleet_for_group(group_name)

        # Заголовок группы
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n)
        c = ws.cell(row=row, column=1, value=group_name)
        c.font      = Font(name="Calibri", size=12, bold=True, color="FFFFFF")
        c.fill      = PatternFill("solid", fgColor=_COLOR_HDR)
        c.alignment = Alignment(horizontal="left", vertical="center")
        c.border    = _border()
        ws.row_dimensions[row].height = 22
        row += 1

        # Шапка колонок
        for ci, col_name in enumerate(PERF_COLS, start=1):
            _cell(ws, row, ci, col_name, bold=True, color="FFFFFF", bg=_COLOR_HDR)
        ws.row_dimensions[row].height = 60
        row += 1

        perf_num_fmt_by_col = {
            2: NF_INT, 3: NF_INT, 4: NF_DEC2, 5: NF_DEC2,
            6: NF_DEC2, 9: NF_DEC1, 10: NF_INT, 11: NF_INT,
        }
        t_mach = t_area = t_hours = 0.0
        t_season_sum   = 0.0
        t_machine_days = 0

        for firm in AGROFIRMS:
            s      = gstats.get(firm, _empty_stats())
            n_mach = s["n_mach"]
            area   = _firm_day_area_ha(s)
            hours  = s["hours_today"]
            avg_s  = s["avg_season"]
            plan   = get_plan_rate_for_group(group_name, firm)
            avail  = own_count.get(firm, 0)
            hired  = s["n_hired"]
            rate   = round(area / n_mach, 2) if n_mach else 0.0
            eff_h  = round(hours / n_mach, 4) if n_mach else 0.0

            vals = [firm, plan, n_mach, round(area, 2), rate, avg_s,
                    decimal_hours_to_hhmm(hours), decimal_hours_to_hhmm(eff_h),
                    round(area / hours, 1) if hours else 0.0, avail, hired]
            for ci, v in enumerate(vals, start=1):
                bg  = _COLOR_DATA
                nf  = perf_num_fmt_by_col.get(ci)
                if ci == 5 and n_mach and rate < get_plan_rate_for_group(group_name, firm):
                    bg = _COLOR_ALERT_PROD
                _cell(ws, row, ci, v,
                      halign="left" if ci == 1 else "center",
                      bg=bg, number_format=nf)
            t_mach  += n_mach
            t_area  += area
            t_hours += hours
            t_season_sum   += s.get("season_sum", 0.0)
            t_machine_days += s.get("machine_days", 0)
            row += 1

        avg_total = round(t_season_sum / t_machine_days, 1) if t_machine_days else 0.0
        t_area_total = _today_area_total_ha(gstats)
        tot_rate  = round(t_area_total / t_mach, 2) if t_mach else 0.0
        tot_eff_h = round(t_hours / t_mach, 4) if t_mach else 0.0
        totals = ["Итого", get_plan_rate_for_group(group_name, list(AGROFIRMS.keys())[0]),
                  int(t_mach), round(t_area_total, 2), tot_rate, avg_total,
                  decimal_hours_to_hhmm(t_hours), decimal_hours_to_hhmm(tot_eff_h),
                  round(t_area_total / t_hours, 1) if t_hours else 0.0,
                  sum(own_count.values()),
                  sum(gstats.get(f, _empty_stats())["n_hired"] for f in AGROFIRMS)]
        for ci, v in enumerate(totals, start=1):
            bg = _COLOR_TOTAL
            nf = perf_num_fmt_by_col.get(ci)
            if ci == 5 and t_mach and tot_rate < get_plan_rate_for_group(group_name, list(AGROFIRMS.keys())[0]):
                bg = _COLOR_ALERT_PROD
            _cell(ws, row, ci, v, bold=True,
                  halign="left" if ci == 1 else "center", bg=bg, number_format=nf)
        row += 2  # отступ между блоками

    ws.column_dimensions["A"].width = 30
    for ci in range(2, n + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 18


def _resolve_main10_path() -> Path | None:
    root = Path(__file__).resolve().parent
    for name in ("10_main_tasks.py", "10_main_harvest.py"):
        path = root / name
        if path.is_file():
            return path
    return None


def _resolve_main11_path() -> Path | None:
    root = Path(__file__).resolve().parent
    for name in ("11_main_harvest.py",):
        path = root / name
        if path.is_file():
            return path
    matches = sorted(root.glob("11_main*.py"))
    return matches[0] if matches else None


def _load_py_module(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return mod


def _write_group_perf_sheet(
    wb: openpyxl.Workbook,
    all_group_stats: dict[str, dict[str, dict]],
    group_name: str,
    *,
    skip_tillage: frozenset[str] | set[str] | None = None,
) -> None:
    if skip_tillage and group_name in skip_tillage:
        return
    if group_name not in all_group_stats:
        return
    gstats = all_group_stats[group_name]
    if REPORT1_HIDE_EMPTY_TABLES and perf_stats_is_effectively_empty(gstats):
        return
    write_perf_sheet(
        wb,
        gstats,
        f"Выраб. — {group_name}",
        len(wb.sheetnames),
        _fleet_for_group(group_name),
    )


def _sync_harvest_day_area_from_cache(all_group_stats: dict, cache) -> None:
    """«Обработано за сутки» на стр. 1 = фирмы «Факт за сутки» листа «Общий намолот».

    Оба числа — сумма area_by_firm_total кэша сводки 10, уже округлённая по фирмам.
    """
    gstats = all_group_stats.get(HARVEST_GRAIN_GROUP_NAME) or {}
    by_firm = getattr(cache, "area_by_firm_total", None) or {}
    before = 0.0
    after = 0.0
    for firm, st in gstats.items():
        before += float(st.get("area_today") or 0.0)
        pair = by_firm.get(firm)
        today = round(float(pair[1]), 2) if pair else 0.0
        st["area_today"] = today
        st["today_by_field"] = {0: today} if today else {}
        after += today
    print(
        f"  5.0 сутки: выработка {before:.2f} га → общий намолот {after:.2f} га",
        flush=True,
    )


async def _append_harvest_block_async(
    wb: openpyxl.Workbook,
    all_group_stats: dict[str, dict[str, dict]],
) -> None:
    """Блок уборки (стр. 1–4 PDF): 5.0 + сводки 11/10."""
    import traceback

    main10_path = _resolve_main10_path()
    mod10 = None
    harvest_cache = None
    if main10_path is not None:
        try:
            mod10 = _load_py_module("main10_for_r1", main10_path)
            _configure_harvest_module(mod10, REPORT_DATE)
        except Exception as e:
            print(f"  сводка 10: ошибка загрузки — {e}")
            mod10 = None
        if mod10 is not None:
            try:
                harvest_cache = await mod10.prepare_harvest_cache_async(REPORT_DATE)
                _sync_harvest_day_area_from_cache(all_group_stats, harvest_cache)
            except Exception:
                print("Листы намолота (сводка 10): ошибка при подготовке данных — листы пропущены:")
                traceback.print_exc()
                harvest_cache = None

    _write_group_perf_sheet(wb, all_group_stats, REPORT1_HARVEST_PERF_GROUP)

    main11_path = _resolve_main11_path()
    if main11_path is None:
        print("Уборка (овощи): модуль сводки 11 не найден, лист пропущен.")
    else:
        try:
            mod11 = _load_py_module("main11_for_r1", main11_path)
            _configure_harvest_module(mod11, REPORT_DATE)
            if mod10 is not None:
                mod11._load_main10_harvest = lambda _m=mod10: _m
            veg_ctx = await mod11.prepare_veg_summary_context_for_report1(REPORT_DATE)
            mod11.write_veg_summary_sheet_for_report1(wb, veg_ctx)
            print("Лист «Сводка oвощi» добавлен (данные сводки 11).")
        except Exception as e:
            print(f"Лист «Сводка oвощi»: ошибка — {e}")
            traceback.print_exc()

    if main10_path is None or mod10 is None or harvest_cache is None:
        print("Намолот: данные сводки 10 не подготовлены, листы пропущены.")
        return

    try:
        general = mod10.build_general_harvest(REPORT_DATE, harvest_cache)
        if not general.empty:
            mod10._write_formatted_harvest_sheet(
                wb, "Общий намолот", general, mod10._WIDTHS_GENERAL,
            )
            print("Лист «Общий намолот» добавлен (данные сводки 10).")
        else:
            print("Намолот: нет данных зерновых, лист пропущен.")
    except Exception:
        print("Лист «Общий намолот»: ошибка построения, лист пропущен:")
        traceback.print_exc()

    try:
        import asyncio as _asyncio
        vegetables = await _asyncio.to_thread(mod10.build_vegetables_report, REPORT_DATE)
        if not vegetables.empty:
            mod10._write_formatted_harvest_sheet(
                wb, "Овощи", vegetables, mod10._WIDTHS_VEG,
            )
            print("Лист «Овощи» добавлен (данные сводки 10, 5.4).")
        else:
            print("Овощи (5.4): нет данных, лист пропущен.")
    except Exception:
        print("Лист «Овощи»: ошибка построения, лист пропущен:")
        traceback.print_exc()


def _skip_harvest_api_fetch() -> bool:
    """Листы уборки заполняются из доноров 10/11 (XLSX/PDF graft), без повторного API."""
    raw = (os.environ.get("REPORT1_SKIP_HARVEST_FETCH") or "").strip().lower()
    return raw in {"1", "true", "yes", "on", "y"}


def _append_veg_harvest_sheet_stub(wb: openpyxl.Workbook) -> None:
    """Стр. 2 — заглушка «Сводка овощи»; PDF/XLSX из сводки 11 после донора."""
    sheet_title = "Сводка овощи"
    if sheet_title in wb.sheetnames:
        return
    ws = wb.create_sheet(sheet_title[:31], index=len(wb.sheetnames))
    ws.cell(row=1, column=1, value="Уборка овощей — данные из сводки 11")
    print("Лист «Сводка овощи» (заглушка; заполнение из сводки 11).")


def _append_namolot_sheets_stub(wb: openpyxl.Workbook) -> None:
    """Стр. 3–4 — заглушки «Общий намолот» / «Овощи»; XLSX из сводки 10 после донора."""
    for title in ("Общий намолот", "Овощи"):
        if title in wb.sheetnames:
            continue
        ws = wb.create_sheet(title[:31], index=len(wb.sheetnames))
        ws.cell(row=1, column=1, value=f"{title} — данные из сводки 10")
    print("Листы «Общий намолот» / «Овощи» (заглушки; заполнение из сводки 10).")


def _configure_harvest_module(mod, report_date: date) -> None:
    """Синхронизировать REPORT_DATE/SEASON_START после dynamic import 10/11."""
    if hasattr(mod, "configure_for_report_date"):
        mod.configure_for_report_date(report_date)
        return
    mod.REPORT_DATE = report_date
    if hasattr(mod, "SEASON_YEAR"):
        mod.SEASON_YEAR = report_date.year
    if hasattr(mod, "resolve_combine_season_start"):
        mod.SEASON_START = mod.resolve_combine_season_start(report_date)
    if hasattr(mod, "_field_to_crop_cache"):
        mod._field_to_crop_cache = None
    if hasattr(mod, "_silage_corn_field_ids_cache"):
        mod._silage_corn_field_ids_cache = None
    if hasattr(mod, "_harvest_cache"):
        mod._harvest_cache = None
    if hasattr(mod, "_harvest_cache_date"):
        mod._harvest_cache_date = None


async def _append_harvest_tail_sheets(wb: openpyxl.Workbook) -> None:
    if _skip_harvest_api_fetch():
        _append_veg_harvest_sheet_stub(wb)
        _append_namolot_sheets_stub(wb)
        return
    import traceback

    main10_path = _resolve_main10_path()
    main11_path = _resolve_main11_path()
    mod10 = None

    if main10_path is not None:
        try:
            mod10 = _load_py_module("main10_for_r1", main10_path)
            _configure_harvest_module(mod10, REPORT_DATE)
        except Exception as e:
            print(f"  сводка 10: ошибка загрузки — {e}")

    if main11_path is None:
        print("Сводка овощи: 11_main_harvest.py не найден, пропуск.")
    else:
        try:
            mod11 = _load_py_module("main11_for_r1", main11_path)
            _configure_harvest_module(mod11, REPORT_DATE)
            if mod10 is not None:
                mod11._load_main10_harvest = lambda _m=mod10: _m
            ctx = await mod11.prepare_veg_summary_context_for_report1(REPORT_DATE)
            mod11.write_veg_summary_sheet_for_report1(wb, ctx)
            print("Лист «Сводка овощи» добавлен (данные сводки 11).")
        except Exception as e:
            print(f"Лист «Сводка oвощi» (сводка 11): ошибка — {e}")
            traceback.print_exc()

    if main10_path is None or mod10 is None:
        print("Намolot: 10_main_harvest.py не найден, листы пропущены.")
        return

    try:
        cache = await mod10.prepare_harvest_cache_async(REPORT_DATE)
        general = mod10.build_general_harvest(REPORT_DATE, cache)
        if not general.empty:
            mod10._write_formatted_harvest_sheet(
                wb, "Общий намолот", general, mod10._WIDTHS_GENERAL,
            )
            print("Лист «Общий намолот» добавлен (данные сводки 10).")
        else:
            print("Намolot: нет данных зерновых, лист пропущен.")

        vegetables = await asyncio.to_thread(mod10.build_vegetables_report, REPORT_DATE)
        if not vegetables.empty:
            mod10._write_formatted_harvest_sheet(
                wb, "Овощи", vegetables, mod10._WIDTHS_VEG,
            )
            print("Лист «Овощи» добавлен (данные сводки 10, 5.4).")
        else:
            print("Овощи (уборка): нет данных, лист пропущен.")
    except Exception as e:
        print(f"Листы уборки (сводка 10): ошибка — {e}")


async def _append_veg_harvest_sheet(wb: openpyxl.Workbook) -> None:
    """
    Стр. 2 сводки 1 — «Сводка овощи» из расчёта сводки 11 (как намолот из 10).
    PDF-graft из сводки 11 остаётся запасным путём, если лист уже с данными — не критично.
    """
    harvest_path = Path(__file__).resolve().parent / "11_main_harvest.py"
    if not harvest_path.exists():
        print("Сводка овощи: 11_main_harvest.py не найден, пропуск.")
        return
    try:
        spec = importlib.util.spec_from_file_location("main11_for_r1", str(harvest_path))
        if spec is None or spec.loader is None:
            print("Сводка овощи: не удалось загрузить 11_main_harvest.py.")
            return
        mod = importlib.util.module_from_spec(spec)
        # dataclass / глобали 11_main требуют запись в sys.modules
        sys.modules["main11_for_r1"] = mod
        spec.loader.exec_module(mod)
        _configure_harvest_module(mod, REPORT_DATE)
        ctx = await mod.prepare_veg_summary_context_for_report1(REPORT_DATE)
        mod.write_veg_summary_sheet_for_report1(wb, ctx)
        print("Лист «Сводка овощи» добавлен (данные сводки 11).")
    except Exception as e:
        print(f"Лист «Сводка овощи» (сводка 11): ошибка — {e}")


async def _append_namolot_sheet(wb: openpyxl.Workbook) -> None:
    """Стр. 3–4 сводки 1 — «Общий намолот» и «Овощи» из сводки 10."""
    harvest_path = Path(__file__).resolve().parent / "10_main_harvest.py"
    if not harvest_path.exists():
        print("Намолот: 10_main_harvest.py не найден, пропуск.")
        return
    try:
        spec = importlib.util.spec_from_file_location("main10_for_r1", str(harvest_path))
        if spec is None or spec.loader is None:
            print("Намолот: не удалось загрузить 10_main_harvest.py.")
            return
        mod = importlib.util.module_from_spec(spec)
        # dataclass / глобали 10_main требуют запись в sys.modules
        sys.modules["main10_for_r1"] = mod
        spec.loader.exec_module(mod)
        _configure_harvest_module(mod, REPORT_DATE)
        cache = await mod.prepare_harvest_cache_async(REPORT_DATE)

        general = mod.build_general_harvest(REPORT_DATE, cache)
        if not general.empty:
            mod._write_formatted_harvest_sheet(
                wb, "Общий намолот", general, mod._WIDTHS_GENERAL,
            )
            print("Лист «Общий намолот» добавлен (данные сводки 10).")
        else:
            print("Намолот: нет данных зерновых, лист пропущен.")

        vegetables = await asyncio.to_thread(mod.build_vegetables_report, REPORT_DATE)
        if not vegetables.empty:
            mod._write_formatted_harvest_sheet(
                wb, "Овощи", vegetables, mod._WIDTHS_VEG,
            )
            print("Лист «Овощи» добавлен (данные сводки 10).")
        else:
            print("Овощи (уборка): нет данных, лист пропущен.")
    except Exception as e:
        print(f"Листы уборки (сводка 10): ошибка — {e}")


async def _append_dop_report1_tables(
    wb: openpyxl.Workbook,
    seeding_day_stats: dict[str, dict] | None = None,
) -> None:
    dop_path = Path(__file__).resolve().parent / "1_main_dop.py"
    if not dop_path.exists():
        print("Доп. таблицы: 1_main_dop.py не найден, пропуск.")
        return
    try:
        spec = importlib.util.spec_from_file_location("report1_dop_module", str(dop_path))
        if spec is None or spec.loader is None:
            print("Доп. таблицы: не удалось загрузить модуль 1_main_dop.py.")
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Переиспользуем HTTP-кэш основного скрипта — dop не тянет справочники повторно.
        mod.get_all_from_id = get_all_from_id
        append_fn = getattr(mod, "append_tables_to_workbook_async", None)
        if not callable(append_fn):
            print("Доп. таблицы: append_tables_to_workbook_async не найден.")
            return
        # Суточный факт «Итого (все АФ)» = площадь из «Выраб. — 4.0 Посев».
        await append_fn(wb, REPORT_DATE, seeding_day_stats=seeding_day_stats)
        print("Доп. таблицы по агрофирмам добавлены.")
    except Exception as e:
        print(f"Доп. таблицы: ошибка подключения 1_main_dop.py: {e}")

async def build_report_async() -> None:
    timing_start()
    apply_report1_thresholds()
    refresh_season_config()
    day_start, cutoff = compute_time_range(REPORT_DATE)

    print(f"Отчёт: {WORK_TYPE_NAME}  |  {REPORT_DATE}")
    print(f"Смена: {day_start.strftime('%d.%m %H:%M')} — {cutoff.strftime('%d.%m %H:%M')}\n")

    other_groups  = [wg for wg in WORK_GROUPS if wg[0] != WORK_TYPE_NAME]
    if _env_flag("REPORT1_STANDALONE", False):
        skipped_perf_groups = set()
        print(
            "Сводка 1 standalone: все группы работ из API "
            "(без доноров 3–8 и без graft)"
        )
    else:
        skipped_perf_groups = set(REPORT1_SKIP_PERF_GROUPS)
    if skipped_perf_groups:
        print(
            "Сводка 1 fast-path: метрики 1–10 подставятся из доноров "
            f"({len(skipped_perf_groups)} групп без API)"
        )

    async def _fetch_report1_api():
        headers = {"X-User-Api-Token": API_TOKEN}
        sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
        sequential = _env_flag("REPORT1_SEQUENTIAL_FETCH", False)
        mode = "последовательно (REPORT1_SEQUENTIAL_FETCH=1)" if sequential else "одновременно"
        print(f"\nЗагружаем данные: группа 1.1 и остальные группы — {mode}...")
        t_api = time.monotonic()
        async with httpx.AsyncClient(headers=headers, timeout=120) as client:
            if sequential:
                # Прежнее поведение (для A/B-замера): 1.1, потом остальные группы.
                t = time.monotonic()
                main = await fetch_fertilizer_11_mtfmi(client, sem, day_start, cutoff)
                _tprint("загрузка 1.1 МУ (wall)", t)
                t = time.monotonic()
                other_group_stats = await _fetch_other_group_stats(
                    client, sem, day_start, cutoff, other_groups, skipped_perf_groups,
                )
                _tprint("загрузка остальных групп (wall)", t)
            else:
                reset_field_firm_resolution()

                async def _timed_main():
                    t = time.monotonic()
                    res = await fetch_fertilizer_11_mtfmi(
                        client, sem, day_start, cutoff, merge_firm_map=True,
                    )
                    _tprint("загрузка 1.1 МУ (wall, параллельно)", t)
                    return res

                async def _timed_other():
                    t = time.monotonic()
                    res = await _fetch_other_group_stats(
                        client, sem, day_start, cutoff, other_groups, skipped_perf_groups,
                    )
                    _tprint("загрузка остальных групп (wall, параллельно)", t)
                    return res

                main, other_group_stats = await asyncio.gather(
                    _timed_main(), _timed_other(),
                )
        stage_add("API-загрузка 1.1 + группы (wall)", time.monotonic() - t_api)
        return main, other_group_stats

    from report_pipeline import run_parallel_refs_and_fetch

    def _load_reference_timed() -> None:
        global _skip_field_map_reset
        t = time.monotonic()
        try:
            load_reference_data()
        finally:
            _skip_field_map_reset = False
            _reference_ready.set()
        stage_add("справочники (в потоке, wall)", time.monotonic() - t)

    global field_to_fg, field_area_map, field_name_map, _skip_field_map_reset
    _reference_ready.clear()
    _skip_field_map_reset = True
    field_to_fg = {}
    field_area_map = {}
    field_name_map = {}

    print("Загружаем справочники...")
    with stage("справочники ∥ API (общий wall)"):
        _, (main_fetch, other_group_stats) = await run_parallel_refs_and_fetch(
            _load_reference_timed, _fetch_report1_api, label="r1-api",
        )
    season_data, day_stats_main, fert_data, cmp_mtfmi, cmp_agro, today_ops_detail = main_fetch

    print("Строим план...")
    with stage("план"):
        plan_data = build_plan_ha()

    season_data, day_stats_main, plan_data = _fold_report1_crop_inputs(
        season_data, day_stats_main, plan_data,
    )

    all_group_stats: dict[str, dict[str, dict]] = {WORK_TYPE_NAME: day_stats_main}
    all_group_stats.update(other_group_stats)

    print("\nФормируем Excel...")
    _t_excel = time.monotonic()
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    # Листы 1-3: культуры + свод + выработка (только для WORK_TYPE_ID=55).
    # Верхняя шапка «СВОДКА… / ВИД РАБОТ / Сводка за дату» — только в PDF (см. svodki_app._stamp_pdf_header).
    if REPORT1_INCLUDE_CROP_SUMMARY:
        write_crop_summary_sheet(wb, season_data, day_stats_main, plan_data, fert_data)
    write_sheet1(wb, season_data, day_stats_main, plan_data, fert_data)

    for title, crops in FERT_TABLE_SLIDES:
        write_fert_slide_sheet(
            wb,
            crops=crops,
            plan_data=plan_data,
            title=title,
            index=len(wb.sheetnames),
            day_stats=day_stats_main,
            season_data=season_data,
        )

    if (not REPORT1_HIDE_EMPTY_TABLES) or (not perf_stats_is_effectively_empty(day_stats_main)):
        write_perf_sheet(
            wb, day_stats_main, "Выработка техники", len(wb.sheetnames),
            _fleet_for_group(WORK_TYPE_NAME),
        )

    # Листы по агрофирмам (только WORK_TYPE_ID=55)
    for firm in AGROFIRMS:
        write_firm_sheet(wb, firm, season_data, day_stats_main, plan_data, fert_data)

    if REPORT1_ENABLE_DEBUG_SHEETS:
        # Диагностические листы включаются только по явному флагу:
        # их построение заметно замедляет обычную генерацию отчёта.
        write_today_fert_detail_sheet(wb, today_ops_detail)

    # Объединённый лист Вспашка + Глубокорыхление
    _TILLAGE_PERF_SKIP = {"2.4 Вспашка", "2.8 Глубокорыхление"}
    write_tillage_combined_sheet(
        wb,
        stats_dict=all_group_stats,
        index=len(wb.sheetnames),
    )

    standalone = _env_flag("REPORT1_STANDALONE", False)
    if standalone:
        for group_name in REPORT1_PRE_HARVEST_PERF_GROUPS:
            _write_group_perf_sheet(
                wb, all_group_stats, group_name, skip_tillage=_TILLAGE_PERF_SKIP,
            )
        await _append_harvest_block_async(wb, all_group_stats)
        for group_name in REPORT1_POST_HARVEST_PERF_GROUPS:
            _write_group_perf_sheet(
                wb, all_group_stats, group_name, skip_tillage=_TILLAGE_PERF_SKIP,
            )
    else:
        for group_name in REPORT1_PERF_GROUP_ORDER:
            if group_name in _TILLAGE_PERF_SKIP:
                continue
            if group_name not in all_group_stats:
                continue
            gstats = all_group_stats[group_name]
            if REPORT1_HIDE_EMPTY_TABLES and perf_stats_is_effectively_empty(gstats):
                continue
            sheet_title  = f"Выраб. — {group_name}"
            own_count    = _fleet_for_group(group_name)
            write_perf_sheet(
                wb,
                gstats,
                sheet_title,
                len(wb.sheetnames),
                own_count,
            )

    stage_add("Excel: листы сводки 1 (+блок уборки в standalone)", time.monotonic() - _t_excel)
    with stage("Excel: доп. таблицы (1_main_dop)"):
        await _append_dop_report1_tables(
            wb,
            seeding_day_stats=all_group_stats.get(SEEDING_GROUP_NAME),
        )
    if not standalone:
        with stage("Excel: хвост уборки (10/11)"):
            await _append_harvest_tail_sheets(wb)

    # Переставляем листы в порядок эталона до PDF-конвертации,
    # чтобы шапки страниц совпадали с таблицами.
    if _reorder_r1_sheets is not None:
        try:
            changed = _reorder_r1_sheets(wb)
            if changed:
                print("  Порядок листов переставлен по эталону сводки 1.")
        except Exception as _e:
            print(f"  Предупреждение: не удалось переставить листы — {_e}")

    output_file = f"1. fertilizer_report_{REPORT_DATE}.xlsx"
    with stage("сохранение xlsx"):
        wb.save(output_file)
    print(f"\nСохранено: {output_file}")
    timing_finish()


def build_report() -> None:
    from report_fetch_orchestration import run_report_async

    run_report_async(build_report_async())


if __name__ == "__main__":
    build_report()