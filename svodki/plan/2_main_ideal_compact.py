"""
1 Компактная сводка ХК (альтернатива «2. Сводка ХК Детальная», отчёт 20):
- Загрузка данных — как в полной сводке; в Excel выводится не больше N последних
  колонок с датами (по умолчанию 5), настраивается через SVODKI_HK_COMPACT_MAX_DAYS.
- PDF: для каждой группы — полоса с видом работ, строка «Сезон» + название справа, большая таблица;
  затем разрыв; на новой странице пустая строка и три сводные таблицы. Между группами **нет**
  ручного разрыва — иначе новая страница начиналась с одной строки «2.1 …», а сезон и таблица
  уезжали вниз (как у боронования). Разрыв только после большой таблицы перед сводками.
- Режим «только дни с данными» (SVODKI_HK_COMPACT_ONLY_NONEMPTY=1): колонки —
  последние N дней, в которые есть активность; иначе — последние N календарных дней
  (не раньше START_DATE). Расчёты «средняя по сезону» в табл. 4 — по всем дням
  группы, не только по видимым колонкам.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from plans import *

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from excel_number_fmt import NF_DEC1, NF_DEC2, NF_INT
from task_area_shared import HARVESTER_GROUP_IDS

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

# Без REPORT_DATE — сегодня по Москве (раньше была зашита дата).
TODAY        = datetime.now(timezone(timedelta(hours=3))).date()

if os.environ.get("REPORT_DATE"):
    TODAY = date.fromisoformat(os.environ["REPORT_DATE"].strip())
START_DATE   = date(TODAY.year, 3, 14)
_season_start_raw = (
    os.environ.get("SVODKI_SEASON_START_DATE")
    or os.environ.get("SEASON_START_DATE")
    or ""
).strip()
if _season_start_raw:
    try:
        START_DATE = date.fromisoformat(_season_start_raw)
    except ValueError:
        pass

OUTPUT_FILE  = f"2. ХК Детальная за {(TODAY - timedelta(days=1)).strftime('%d.%m.%Y')}.xlsx"
REPORT_WINDOW_START_RAW = os.environ.get("REPORT_WINDOW_START", "").strip()
REPORT_WINDOW_END_RAW = os.environ.get("REPORT_WINDOW_END", "").strip()
CUSTOM_WINDOW_START: datetime | None = None
CUSTOM_WINDOW_END: datetime | None = None
CUSTOM_SHIFT_DATE_MIN: date | None = None
CUSTOM_SHIFT_DATE_MAX: date | None = None
BORON_GROUP_NAME = "2.1 Боронование"
ROLLING_GROUP_NAME = "2.12 Прикатывание"
SEEDING_GROUP_NAME = "4.0 Посев"
VEG_SEEDING_GROUP_NAME = "4.1 Посев (овощи)"
CULTIVATION_GROUP_NAME = "2.11 Культивация"
DISK_GROUP_NAME = "2.9 Дискование"
PLOW_GROUP_NAME = "2.4 Вспашка"
DEEP_GROUP_NAME = "2.8 Глубокорыхление"
# Вспашка / диск / боронование: окно смен как в plan/ (START_DATE .. TODAY),
# без обрезки period_end из season_unified (календарь Cropwise до 01.07).
_TILLAGE_SHIFT_GROUPS = frozenset({
    PLOW_GROUP_NAME,
    DEEP_GROUP_NAME,
    DISK_GROUP_NAME,
    BORON_GROUP_NAME,
    CULTIVATION_GROUP_NAME,
})
# Группы с «основным» сезоном season_year_for_date(TODAY) (культура поля берётся по нему).
# Факт по умолчанию считается по обоим сезонам history_years / сезонам редактора планов.
SEASON_YEAR_PLUS_ONE_GROUPS = frozenset({
    PLOW_GROUP_NAME,
    SEEDING_GROUP_NAME,
    VEG_SEEDING_GROUP_NAME,
})


def _fact_seasons_for_group(group_name: str) -> set[str] | None:
    """Годы machine_task.season для суток и сезонного факта группы."""
    try:
        from season_unified import fact_season_years_for_group

        years = fact_season_years_for_group(group_name, TODAY)
        if years:
            return {str(y) for y in years}
    except Exception:
        pass
    both = {
        "1.1 Внесение минеральных удобрений",
        "2.4 Вспашка",
        "2.8 Глубокорыхление",
        "2.9 Дискование",
        "2.11 Культивация",
        "3.1 Опрыскивание",
        "3.2 Опрыскивание (овощи)",
        "4.0 Посев",
        "4.1 Посев (овощи)",
    }
    if group_name in both:
        _cur = season_year_for_date(TODAY)
        return {str(_cur - 1), str(_cur)}
    return {str(TODAY.year)}
SPRAY_GROUP_NAME = "3.1 Опрыскивание"
VEG_SPRAY_GROUP_NAME = "3.2 Опрыскивание (овощи)"
# Для группы "Опрыскивание": через связку AgroOperation засчитываем эти
# культуры только если HistoryItem.year == текущему сезону (season_year_for_date(TODAY)) — так же,
# как field_to_crop_general уже исключает их запись за прошший календарный год.
SPRAY_WINTER_CROPS_SEASON_ONLY = frozenset({"Рапс озимый", "Пшеница озимая"})
# Вспашка: план/отображение по культурам; факт «Выполнено» — все поля,
# если machine_task.season входит в сезоны группы (эталон: Чистопольская ≈1810, Раздолье ≈2828.7).
PLOW_SEASON_CROPS: frozenset[str] = frozenset({
    "Рапс озимый",
    "Рапс яровой",
    "Пшеница озимая",
    "Подсолнечник",
    "Соя",
    "Кукуруза на зерно",
    "Кукуруза на силос",
    "Картофель",
    "Морковь",
    "Свекла столовая",
})
SVODKA2_PLOW_SEASON_FIX = "2026-09-07a"
# Дискование: культуры/учёт — history 2026+2027 (field_to_crop_general), не sowing-only.
SVODKA2_DISK_SEASON_FIX = "2026-08-07a"
# Факт: культура строки только из mtfmi.history_item_id.
# Учётная площадь — history_items сезонов этой операции, одно поле один раз.
SVODKA2_HISTORY_FIELD_FIX = "2026-10-06"
_HISTORY_FIELD_ONLY = True
FERT_GROUP_NAME = "1.1 Внесение минеральных удобрений"
HARVEST_GROUP_NAME = "5.0 Уборка"
HARVEST_VEG_PLAN_KEY = "5.4 Уборка картофеля, лука"
HARVEST_VEG_GROUP_NAME = "5.4 Уборка овощей"

from season_unified import (
    GROUP_SEASON_RULE,
    clear_season_cache,
    load_season_store,
    period_range_for_group,
    resolve_group_season_config,
    season_label_for_group,
    task_season_str_for_group,
    task_season_years_set_for_group,
    winter_spray_task_seasons,
)
from season_calendar import fact_window_end, season_year_for_date


def _merged_period_for_group(group_name: str, default_start: date) -> tuple[date, date]:
    """Единый резолвер season_unified (редактор планов + правила GROUP_SEASON_RULE)."""
    try:
        return period_range_for_group(group_name, TODAY)
    except Exception:
        return default_start, TODAY


def _editor_period_for_group(group_name: str, default_start: date) -> tuple[date, date]:
    """Период группы из редактора, без общего SVODKI_SEASON_START_DATE.

    При генерации общий старт пишется из «5.0 Уборка». Для вспашки он
    подменяет дату группы; культивация без своего ключа читает его же.
    """
    keys = ("SVODKI_SEASON_START_DATE", "SEASON_START_DATE")
    saved = {key: os.environ.pop(key, None) for key in keys}
    try:
        return _merged_period_for_group(group_name, default_start)
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def _override_period_start(group_name: str, default: date) -> date:
    return _merged_period_for_group(group_name, default)[0]


def _override_period_end(group_name: str, default: date | None = None) -> date:
    return _merged_period_for_group(group_name, default or date(TODAY.year, 3, 1))[1]


def _override_season_years(group_name: str, default: list[int] | None = None) -> list[int]:
    cfg = resolve_group_season_config(group_name, TODAY)
    years = list(cfg.get("history_years") or [])
    return years or list(default or [])


_fert_cfg = resolve_group_season_config(FERT_GROUP_NAME, TODAY)
FERT_SEASON_YEARS: list[int] = list(_fert_cfg.get("history_years") or [])
FERT_PERIOD_START = date.fromisoformat(_fert_cfg["period_start"])
FERT_PERIOD_END = date.fromisoformat(_fert_cfg["period_end"])
_harvest_cfg = resolve_group_season_config(HARVEST_GROUP_NAME, TODAY)
HARVEST_SEASON_START = date.fromisoformat(_harvest_cfg["period_start"])
SEASON_RANGE_OVERRIDES: dict[str, dict] = dict(load_season_store(TODAY).get("by_group") or {})


def refresh_season_config() -> None:
    """Перечитать season_ranges.json и env (редактор планов / генерация)."""
    global FERT_SEASON_YEARS, FERT_PERIOD_START, FERT_PERIOD_END, HARVEST_SEASON_START
    global SEASON_RANGE_OVERRIDES
    clear_season_cache()
    SEASON_RANGE_OVERRIDES = dict(load_season_store(TODAY, reload=True).get("by_group") or {})
    fert = resolve_group_season_config(FERT_GROUP_NAME, TODAY, reload=True)
    FERT_SEASON_YEARS = list(fert.get("history_years") or [])
    FERT_PERIOD_START = date.fromisoformat(fert["period_start"])
    FERT_PERIOD_END = date.fromisoformat(fert["period_end"])
    harvest = resolve_group_season_config(HARVEST_GROUP_NAME, TODAY, reload=True)
    HARVEST_SEASON_START = date.fromisoformat(harvest["period_start"])


if SEASON_RANGE_OVERRIDES:
    print(
        f"  [season_unified] групп в season_ranges.json: {len(SEASON_RANGE_OVERRIDES)}; "
        f"1.1 период={FERT_PERIOD_START}–{FERT_PERIOD_END}, сезоны={FERT_SEASON_YEARS or '—'}"
    )
# Буфер end_time при отборе заданий уборки (как 10_main CUTOFF_BUFFER_HOURS).
try:
    HARVEST_CUTOFF_BUFFER_HOURS: int = int(
        (os.environ.get("SVODKI_REPORT10_CUTOFF_BUFFER_HOURS") or "12").strip()
    )
except ValueError:
    HARVEST_CUTOFF_BUFFER_HOURS = 12
GENERAL_CROP_GROUPS = frozenset({
    FERT_GROUP_NAME,
    SPRAY_GROUP_NAME,
    VEG_SPRAY_GROUP_NAME,
    HARVEST_GROUP_NAME,
})
VEG_SPRAY_CROPS = frozenset({
    "Картофель", "Морковь", "Свекла столовая",
})
HARVEST_VEG_CROPS = frozenset({"Картофель", "Морковь", "Свекла столовая"})
HARVEST_VEG_CROP_ORDER = ("Картофель", "Морковь", "Свекла столовая")
# Суточный факт — по field_to_firm_all, без обязательной культуры в sowing-карте
# (поле может выпасть из field_to_crop, напр. озимая пшеница 2027 без sowing_date).
FACT_BY_ALL_FIELDS_GROUPS = frozenset({
    BORON_GROUP_NAME,
    CULTIVATION_GROUP_NAME,
    ROLLING_GROUP_NAME,
    SEEDING_GROUP_NAME,
})
VEG_HARVEST_WT_IDS = frozenset({75, 76, 77})
# Маркер версии правки листа 5.4 (для проверки на сервере: grep SVODKA2_VEG54_FIX).
SVODKA2_VEG54_FIX = "2026-07-09c"
# Маркер правки 5.0 Уборка → как 10_main_harvest (grep SVODKA2_HARVEST10_FIX).
SVODKA2_HARVEST10_FIX = "2026-10-16a"
CORN_GRAIN_CROP = "Кукуруза на зерно"
CORN_SILAGE_CROP = "Кукуруза на силос"
CORN_SUMMARY_LABEL = "Кукуруза, из них:"
# work_type_id «Скашивание» — кукуруза на силос (как 10_main_harvest).
SILAGE_CORN_MOWING_WT_IDS: frozenset[int] = frozenset({74, 83, 84, 147, 149})
# Овощи и травы — не выводить на листе 5.0 (эталон 08.07.2026).
# Строка «Иные культуры» из редактора планов на листе остаётся.
HARVEST_CROP_EXCLUDE_BY_FIRM: dict[str, frozenset[str]] = {
    "АФ Чистопольская": frozenset({
        "Многолетние травы 2021 года",
        "Многолетние травы 2022 года",
        "Многолетние травы 2024 года",
        "Многолетние травы 2025 года",
        "Картофель",
        "Морковь",
        "Свекла столовая",
        "Пар",
        "Луговые травы",
    }),
    "АгроРаздолье": frozenset({
        "Многолетние травы 2021 года",
        "Многолетние травы 2024 года",
        "Многолетние травы 2025 года",
        "Пар",
        "Люцерна",
        "Многолетние травы 2023 года",
        "Обмен",
    }),
    "Аксу-Агро": frozenset({
        "Многолетние травы 2021 года",
        "Многолетние травы 2022 года",
        "Многолетние травы 2024 года",
        "Многолетние травы 2025 года",
        "Зябь",
        "Многолетние травы 2023 года",
        "Многолетние травы 2026 года",
    }),
    "НурлатАгро": frozenset({
        "Многолетние травы 2022 года",
        "Многолетние травы 2024 года",
        "Многолетние травы 2025 года",
        "Зябь",
        "Многолетние травы",
        "Многолетние травы 2023 года",
    }),
    "Дрожжаное Рассвет Агро": frozenset({
        "Однолетние травы",
        "Многолетние травы 2022 года",
        "Многолетние травы 2024 года",
        "Многолетние травы 2025 года",
    }),
    "Актай": frozenset({
        "Многолетние травы 2021 года",
        "Многолетние травы 2024 года",
        "Многолетние травы 2025 года",
    }),
}
TILLAGE_GROUPS = frozenset({PLOW_GROUP_NAME, DEEP_GROUP_NAME})
MIN_DETAIL_AREA_HA = 0.5
# Счётчик агрегатов и суточная выработка на 1 агрегат — как в сводках 5, 7, 8.
MIN_COUNT_AREA_HA = 10.0
# Для этих групп порог 10 га на счётчик техники не применяется (3.1 — не в списке).
MIN_COUNT_AREA_EXEMPT_GROUPS: frozenset[str] = frozenset({
    "2.8 Глубокорыхление",
    "4.1 Посев (овощи)",
    "3.2 Опрыскивание (овощи)",
    "5.4 Уборка картофеля, лука",
    "2.6 Фрезерование",
    "2.7 Гребнеобразование",
})
# Уточнение: правило "площадь >= 0.5 га" (детали/агрегаты/счётчик техники) не должно
# распространяться на эти группы вовсе (порог = 0, без минимума). Уборка овощей
# определяется отдельно через _is_harvest_veg_group (ключи HARVEST_VEG_PLAN_KEY /
# HARVEST_VEG_GROUP_NAME / "5.4 Уборка...").
DETAIL_AREA_NO_THRESHOLD_GROUPS: frozenset[str] = frozenset({
    "2.6 Фрезерование",
    "2.7 Гребнеобразование",
    "2.8 Глубокорыхление",
    PLOW_GROUP_NAME,     # "2.4 Вспашка"
    HARVEST_GROUP_NAME,  # "5.0 Уборка"
})



def _resolve_output_xlsx_path() -> Path:
    name = f"2. ХК Детальная за {(TODAY - timedelta(days=1)).strftime('%d.%m.%Y')}.xlsx"
    custom = (os.environ.get("SVODKI_OUTPUT_DIR") or "").strip()
    base = Path(custom).expanduser().resolve() if custom else Path.cwd()
    return base / name

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


def _shift_date_from_dt(dt: datetime, day_start_hour: int = 6) -> date:
    cal = dt.date()
    anchor = datetime(cal.year, cal.month, cal.day, day_start_hour, 0, 0, tzinfo=TZ_MSK)
    if dt < anchor:
        return cal - timedelta(days=1)
    return cal


def _last_shift_date_before(window_end: datetime, day_start_hour: int = 6) -> date:
    """Последняя смена, попадающая в окно [*, window_end) (смена D = [D 06:00, D+1 06:00))."""
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=TZ_MSK)
    else:
        window_end = window_end.astimezone(TZ_MSK)
    return _shift_date_from_dt(window_end - timedelta(seconds=1), day_start_hour)


def _shift_overlaps_window(day_start: datetime, cutoff: datetime) -> bool:
    if CUSTOM_WINDOW_START is None or CUSTOM_WINDOW_END is None:
        return True
    return day_start < CUSTOM_WINDOW_END and cutoff > CUSTOM_WINDOW_START


def _init_custom_window() -> None:
    global CUSTOM_WINDOW_START, CUSTOM_WINDOW_END, CUSTOM_SHIFT_DATE_MIN, CUSTOM_SHIFT_DATE_MAX
    global TODAY, OUTPUT_FILE
    ws = _parse_report_window(REPORT_WINDOW_START_RAW)
    we = _parse_report_window(REPORT_WINDOW_END_RAW)
    if ws is None or we is None:
        return
    if ws >= we:
        raise ValueError(
            f"Неверное окно сводки 2: {REPORT_WINDOW_START_RAW} — {REPORT_WINDOW_END_RAW}"
        )
    CUSTOM_WINDOW_START = ws
    CUSTOM_WINDOW_END = we
    CUSTOM_SHIFT_DATE_MIN = _shift_date_from_dt(ws)
    CUSTOM_SHIFT_DATE_MAX = _last_shift_date_before(we)
    if CUSTOM_SHIFT_DATE_MAX < CUSTOM_SHIFT_DATE_MIN:
        raise ValueError(
            f"Неверное окно сводки 2: нет ни одной смены между "
            f"{REPORT_WINDOW_START_RAW} и {REPORT_WINDOW_END_RAW}"
        )
    # cal_date < TODAY: включаем смены до CUSTOM_SHIFT_DATE_MAX включительно
    TODAY = CUSTOM_SHIFT_DATE_MAX + timedelta(days=1)
    OUTPUT_FILE = (
        f"2. ХК Детальная за {CUSTOM_SHIFT_DATE_MIN.strftime('%d.%m.%Y')}"
        f"—{CUSTOM_SHIFT_DATE_MAX.strftime('%d.%m.%Y')}.xlsx"
    )


_init_custom_window()


def _excel_period_export() -> bool:
    """Сводка 2 за произвольный период — только Excel на сайте (без PDF)."""
    return CUSTOM_SHIFT_DATE_MIN is not None and CUSTOM_SHIFT_DATE_MAX is not None


def _hourly_window_bounds(day_start: datetime, cutoff: datetime) -> tuple[datetime, datetime] | None:
    """Границы учёта hourly: смена, обрезанная произвольным окном REPORT_WINDOW_*."""
    lo = day_start
    hi = cutoff
    if CUSTOM_WINDOW_START is not None and CUSTOM_WINDOW_END is not None:
        lo = max(day_start, CUSTOM_WINDOW_START)
        hi = min(cutoff, CUSTOM_WINDOW_END)
        if lo >= hi:
            return None
    return lo, hi


def _cov_from_hourly(item: dict, day_start: datetime, cutoff: datetime) -> float:
    bounds = _hourly_window_bounds(day_start, cutoff)
    if bounds is None:
        return 0.0
    lo, hi = bounds
    hourly = item.get("covered_area_hourly") or []
    cov = 0.0
    for entry in hourly:
        h = parse_dt(entry[0])
        if h and lo <= h < hi:
            cov += float(entry[1] or 0.0)
    return cov


def _item_area_in_window(
    item: dict,
    window_end: datetime | None,
    window_start: datetime | None = None,
) -> float:
    """Площадь MTFMI в окне смены — единая логика сводок 1/2/6 (task_area_shared)."""
    from task_area_shared import item_area_in_window as _shared_item_area

    return _shared_item_area(
        item, window_end, window_start, parse_dt=parse_dt,
    )


def _machine_count_min_ha(group_name: str) -> float:
    """Минимальная площадь агрегата за смену для счётчика техники — как в 6_main_3.py."""
    if group_name in MIN_COUNT_AREA_EXEMPT_GROUPS or _detail_area_exempt(group_name):
        return 0.0
    if group_name == PLOW_GROUP_NAME:
        return MIN_DETAIL_AREA_HA
    return max(MIN_DETAIL_AREA_HA, MIN_COUNT_AREA_HA)


def _detail_area_exempt(group_name: str) -> bool:
    """Группы, где правило 0.5 га не применяется вовсе: гребнеобразование,
    глубокорыхление, фрезерование, вспашка, уборка, уборка овощей."""
    return group_name in DETAIL_AREA_NO_THRESHOLD_GROUPS or _is_harvest_veg_group(group_name)


def _detail_area_min_ha(group_name: str) -> float:
    """Порог 0.5 га для отдельной детали/дня — 0 (нет порога) для исключённых групп."""
    if _detail_area_exempt(group_name):
        return 0.0
    return MIN_DETAIL_AREA_HA


def _group_count_threshold(group_name: str) -> float:
    if _detail_area_exempt(group_name):
        return 0.0
    machine_min = _machine_count_min_ha(group_name)
    if machine_min > 0:
        return machine_min
    return MIN_DETAIL_AREA_HA


def _day_rate_area(by_mid: dict[int, float], area_threshold: float = MIN_DETAIL_AREA_HA) -> float:
    """Площадь за день: сумма по машинам >= порога (как area_today в 3_main)."""
    if area_threshold <= 0:
        return sum(float(v) for v in (by_mid or {}).values() if float(v) > 0)
    return sum(
        float(v) for v in (by_mid or {}).values()
        if round(float(v), 2) >= area_threshold
    )


def _day_rate_machines(by_mid: dict[int, float], count_threshold: float) -> int:
    return sum(1 for v in (by_mid or {}).values() if round(float(v), 2) >= count_threshold)


def _season_stats_for_firm(
    mda_firm: dict[date, dict[int, float]],
    stats_dates: list[date],
    area_threshold: float = MIN_DETAIL_AREA_HA,
    count_threshold: float = MIN_DETAIL_AREA_HA,
    area_by_date: dict[date, float] | None = None,
) -> tuple[float, int]:
    """Средняя по сезону: площадь (по полям или машинам) и machine_days по порогу техники."""
    season_sum = 0.0
    machine_days = 0
    for dt in stats_dates:
        if area_by_date is not None:
            day_area = float(area_by_date.get(dt, 0.0) or 0.0)
        else:
            by_mid = mda_firm.get(dt, {})
            day_area = _day_rate_area(by_mid, area_threshold)
        if day_area <= 0:
            continue
        season_sum += day_area
        by_mid = mda_firm.get(dt, {})
        machine_days += _day_rate_machines(by_mid, count_threshold)
    return season_sum, machine_days


MANUAL_OVERRIDES_ENV = "SVODKI_MANUAL_OVERRIDES_PATH"

SEMAPHORE_LIMIT = max(
    1,
    int(
        os.environ.get("SVODKI_REPORT2_HTTP_CONCURRENCY")
        or os.environ.get("SVODKI_REPORT22_HTTP_CONCURRENCY")
        or "25"
    ),
)

def _parse_int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, str(default)).strip())
        return max(lo, min(hi, v))
    except (ValueError, TypeError):
        return default


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "1" if default else "0").strip().lower()
    if raw in ("1", "true", "yes", "on", "да"):
        return True
    if raw in ("0", "false", "no", "off", "нет"):
        return False
    return default


from svodki_pipeline import (
    BackgroundJob,
    pipeline_enabled,
    reference_workers,
    run_reference_jobs,
)
from report_fetch_orchestration import report_async_client, run_report_async

# Конвейер: справочники и «цепочка заданий» (задачи → смены → mtfmi → mtaomi)
# грузятся одновременно. SVODKI_PIPELINE=0 / SVODKI_REPORT2_PIPELINE=0 — последовательно.
PIPELINE = pipeline_enabled()
REFERENCE_WORKERS = reference_workers(9)

# Замеры этапов вынесены в utils/shared/svodki_timing.py (общие для всех сводок).
from svodki_timing import stage as _stage, stage_add, timing_finish, timing_start


def _fetch_machine_tasks_parallel(
    wt_ids: list[int],
    season_start_str: str,
    cutoff_str: str,
    *,
    tasks_index: dict[int, list[dict]] | None = None,
) -> list[dict]:
    if not wt_ids:
        return []

    if tasks_index is not None:
        return _tasks_from_index(tasks_index, wt_ids, season_start_str, cutoff_str)

    try:
        from report2_prefetch_io import prefetch_tasks_by_wt

        pref = prefetch_tasks_by_wt()
        if pref:
            return _tasks_from_index(pref, wt_ids, season_start_str, cutoff_str)
    except Exception as exc:
        print(f"  [r2 prefetch] fallback live machine_tasks: {exc}", flush=True)

    from machine_tasks_batch_fetch import fetch_by_work_type_ids_sync

    return fetch_by_work_type_ids_sync(
        get_all_from_id,
        "machine_tasks",
        wt_ids,
        {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_str},
        log_prefix="[r2] ",
    )


def _build_global_mtfmi_map(task_ids: list[int], headers: dict) -> dict[int, list[dict]]:
    """Один проход mtfmi: prefetch JSON, недостающее — live batch."""
    unique = sorted({int(t) for t in task_ids if t is not None})
    if not unique:
        return {}
    out: dict[int, list[dict]] = {}
    live_ids = list(unique)
    try:
        from svodki_prefetch_io import mtfmi_map_hybrid

        pref_map, missing = mtfmi_map_hybrid(unique)
        if pref_map is not None and not missing:
            return pref_map
        if pref_map is not None and missing:
            live_ids = missing
            out = dict(pref_map)
    except Exception:
        live_ids = list(unique)

    if live_ids:
        print(f"  mtfmi live batch: {len(live_ids)} задач", flush=True)
        from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks

        async def _fetch() -> dict[int, list[dict]]:
            async with report_async_client(
                API_TOKEN, semaphore_limit=SEMAPHORE_LIMIT, timeout=120,
            ) as (client, sem):
                return await fetch_mtfmi_map_for_tasks(
                    client, sem, BASE_URL, live_ids, log_prefix="[r2] ",
                )

        fetched = run_report_async(_fetch())
        out.update(fetched)
    return out


def _widest_machine_tasks_window() -> tuple[str, str]:
    """Максимальное окно для всех групп сводки 2 (фильтр — локально по группе)."""
    day_h = 6
    start_d = date(TODAY.year, 1, 1)
    # Учитываем ручной старт 1.1 / других групп (напр. 2025-03-01).
    for cand in (FERT_PERIOD_START, START_DATE, HARVEST_SEASON_START):
        if isinstance(cand, date) and cand < start_d:
            start_d = cand
    for group_name in (
        PLOW_GROUP_NAME,
        DEEP_GROUP_NAME,
        DISK_GROUP_NAME,
        CULTIVATION_GROUP_NAME,
        BORON_GROUP_NAME,
    ):
        group_start = _editor_period_for_group(group_name, START_DATE)[0]
        if group_start < start_d:
            start_d = group_start
    for ov in SEASON_RANGE_OVERRIDES.values():
        raw = str((ov or {}).get("period_start") or "").strip()
        if not raw:
            continue
        try:
            d = date.fromisoformat(raw.split("T")[0])
        except ValueError:
            continue
        if d < start_d:
            start_d = d
    start = f"{start_d.strftime('%Y-%m-%d')}T00:00:00"
    end_dt = datetime(TODAY.year, TODAY.month, TODAY.day, day_h, 0, 0, tzinfo=TZ_MSK) + timedelta(days=1)
    return start, end_dt.strftime("%Y-%m-%dT%H:%M:%S")


_LIVE_TASKS_BY_WT: dict[int, list[dict]] | None = None

# Живая выгрузка machine_tasks: параллельно по окнам времени и пачкам work_type_id.
# SVODKI_REPORT2_ASYNC_TASKS=0 — вернуть прежнюю последовательную выгрузку.
ASYNC_TASKS = _parse_bool_env("SVODKI_REPORT2_ASYNC_TASKS", True)
TASKS_WINDOW_DAYS = _parse_int_env("SVODKI_REPORT2_TASKS_WINDOW_DAYS", 14, 1, 366)
_ISO_FMT = "%Y-%m-%dT%H:%M:%S"


def _split_windows(start_str: str, end_str: str, days: int) -> list[tuple[str, str]]:
    from live_machine_tasks_fetch import split_time_windows

    return split_time_windows(start_str, end_str, days)


async def _fetch_tasks_live_async(
    wt_ids: list[int] | set[int],
    start_str: str,
    end_str: str,
    headers: dict,
) -> list[dict]:
    from live_machine_tasks_fetch import fetch_machine_tasks_windowed_async

    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
    limits = httpx.Limits(
        max_connections=SEMAPHORE_LIMIT,
        max_keepalive_connections=SEMAPHORE_LIMIT,
    )
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=limits) as client:
        return await fetch_machine_tasks_windowed_async(
            client,
            BASE_URL,
            wt_ids,
            {"end_time_gt_eq": start_str, "end_time_lt": end_str},
            sem,
            window_days=TASKS_WINDOW_DAYS,
            log_prefix="[r2 live] ",
        )



def _ensure_live_tasks_preloaded(
    window: tuple[str, str] | None = None,
) -> dict[int, list[dict]]:
    """Один параллельный live-fetch всех work_type_id (fallback без prefetch)."""
    global _LIVE_TASKS_BY_WT
    if _LIVE_TASKS_BY_WT is not None:
        return _LIVE_TASKS_BY_WT
    all_wt = sorted({int(wt) for _, ids in WORK_GROUPS for wt in ids})
    season_start_str, cutoff_str = window or _widest_machine_tasks_window()
    print(
        f"  [r2 live] предзагрузка machine_tasks: {len(all_wt)} work types…",
        flush=True,
    )
    started = time.monotonic()
    rows: list[dict] | None = None
    from live_machine_tasks_fetch import async_tasks_enabled

    if ASYNC_TASKS and async_tasks_enabled():
        try:
            _split_windows(season_start_str, cutoff_str, 1)  # проверка формата дат
            rows = run_report_async(_fetch_tasks_live_async(
                all_wt, season_start_str, cutoff_str, {"X-User-Api-Token": API_TOKEN},
            ))
        except ValueError:
            rows = None  # нестандартный формат окна → прежний путь
    if rows is None:
        from machine_tasks_batch_fetch import fetch_by_work_type_ids_sync

        rows = fetch_by_work_type_ids_sync(
            get_all_from_id,
            "machine_tasks",
            all_wt,
            {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_str},
            log_prefix="[r2 live] ",
        )
    by_wt: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        wt = row.get("work_type_id")
        if wt is not None:
            by_wt[int(wt)].append(row)
    _LIVE_TASKS_BY_WT = dict(by_wt)
    total = sum(len(v) for v in by_wt.values())
    print(
        f"  [r2 live] machine_tasks: {total} за {time.monotonic() - started:.1f} с",
        flush=True,
    )
    return _LIVE_TASKS_BY_WT


def _get_tasks_index(window: tuple[str, str] | None = None) -> dict[int, list[dict]]:
    """prefetch JSON, а при его отсутствии — live-выгрузка (как раньше в collect_work_data)."""
    tasks_index: dict[int, list[dict]] | None = None
    try:
        from report2_prefetch_io import prefetch_tasks_by_wt

        tasks_index = prefetch_tasks_by_wt()
    except Exception:
        tasks_index = None
    if tasks_index is None:
        tasks_index = _ensure_live_tasks_preloaded(window)
    return tasks_index


def _tasks_from_index(
    source: dict[int, list[dict]],
    wt_ids: list[int],
    season_start_str: str,
    cutoff_str: str,
) -> list[dict]:
    season_start = parse_dt(season_start_str)
    cutoff = parse_dt(cutoff_str)
    if season_start is None or cutoff is None:
        return []
    out: list[dict] = []
    seen: set[int] = set()
    for wt_id in wt_ids:
        for row in source.get(wt_id, []):
            end_dt = parse_dt(
                row.get("end_time") or row.get("finished_at") or row.get("updated_at")
            )
            if end_dt is None:
                continue
            if not (season_start <= end_dt < cutoff):
                continue
            rid = row.get("id")
            if rid is not None:
                try:
                    rid_i = int(rid)
                except (TypeError, ValueError):
                    rid_i = None
                if rid_i is not None:
                    if rid_i in seen:
                        continue
                    seen.add(rid_i)
            out.append(row)
    return out

def is_skip_crop(crop_name: str) -> bool:
    if not crop_name:
        return True
    if crop_name in SKIP_CROPS:
        return True
    if crop_name.lower().startswith("введение в оборот"):
        return True
    return False


_FIELD_CROP_BY_YEAR: dict[int, dict[int, str]] = {}


def _remember_field_crops_by_year(history_by_year: dict, crops_map: dict) -> None:
    """field_id → год history → культура. Для дискования берётся сезон задания."""
    global _FIELD_CROP_BY_YEAR
    out: dict[int, dict[int, str]] = {}
    for year, items in (history_by_year or {}).items():
        try:
            year_i = int(year)
        except (TypeError, ValueError):
            continue
        for hi in items or []:
            if not hi.get("active"):
                continue
            fid, cid = hi.get("field_id"), hi.get("crop_id")
            if not fid or not cid:
                continue
            try:
                fid_i = int(fid)
                cid_i = int(cid)
            except (TypeError, ValueError):
                continue
            name = str(crops_map.get(cid_i) or crops_map.get(cid) or "").strip()
            if not name or name in SKIP_CROPS:
                continue
            out.setdefault(fid_i, {})[year_i] = name
    _FIELD_CROP_BY_YEAR = out


def _harvest_crop_excluded(firm: str, crop: str) -> bool:
    if crop in HARVEST_VEG_CROPS:
        return True
    # «Иные» и пустая культура остаются в итоге фирмы, без своей строки.
    # «Иные культуры» — строка редактора планов, её показываем.
    if crop in {"Не определена", "Иные", "__other__"}:
        return True
    excluded = HARVEST_CROP_EXCLUDE_BY_FIRM.get(firm)
    return excluded is not None and crop in excluded


def _harvest_grain_field_allowed(group_name: str, crop: str) -> bool:
    if group_name == HARVEST_GROUP_NAME and crop in HARVEST_VEG_CROPS:
        return False
    return True


# Площадь «Выполнено, га»: комбайны и ещё трактора, которые ведут уборку.
HARVEST_AREA_GROUP_IDS: frozenset[int] = HARVESTER_GROUP_IDS | frozenset({
    4,   # Трактора
    17,  # Трактора энергонасыщенные
})


def _is_harvest_area_machine(
    machine_id: int | None,
    machine_group: dict[int, int],
) -> bool:
    """Чья площадь входит в «Выполнено, га» уборки."""
    if machine_id is None:
        return False
    grp = int(machine_group.get(int(machine_id), 0) or 0)
    return grp in HARVEST_AREA_GROUP_IDS or grp == HIRED_GROUP_ID


def _as_silage_if_corn_mowing(wt: int | None, crop: str) -> str:
    """Скашивание кукурузы — силос, даже если в истории написано «на зерно»."""
    if wt is None or int(wt) not in SILAGE_CORN_MOWING_WT_IDS:
        return crop
    if crop and "кукуруза" in crop.casefold():
        return CORN_SILAGE_CROP
    return crop


def _harvest_crop_for_field(
    wt: int | None,
    fid_i: int,
    active_crop_map: dict[int, str],
) -> str | None:
    """Скашивание кукурузы → «Кукуруза на силос», в том числе при подписи «на зерно»."""
    field_crop = active_crop_map.get(fid_i, "")
    if wt is not None and int(wt) in SILAGE_CORN_MOWING_WT_IDS:
        named = _as_silage_if_corn_mowing(wt, field_crop)
        if named != CORN_SILAGE_CROP:
            return None
        return CORN_SILAGE_CROP
    return field_crop or "Не определена"


def _merge_silage_harvest_tasks(
    tasks: list[dict],
    season_start_str: str,
    cutoff_str: str,
) -> list[dict]:
    """Догрузка machine_tasks скашивания — вне «5.0 Уборка» в prefetch."""
    existing = {t["id"] for t in tasks}
    from machine_tasks_batch_fetch import fetch_by_work_type_ids_sync

    sup = fetch_by_work_type_ids_sync(
        get_all_from_id,
        "machine_tasks",
        sorted(SILAGE_CORN_MOWING_WT_IDS),
        {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_str},
        log_prefix="[r2] silage+ ",
    )
    added = 0
    out = list(tasks)
    for t in sup:
        tid = t.get("id")
        if tid is None or tid in existing:
            continue
        existing.add(tid)
        out.append(t)
        added += 1
    if added:
        print(f"    machine_tasks скашивание (API): +{added}", flush=True)
    return out


def _fetch_silage_corn_field_ids() -> set[int]:
    """Поля с операцией «Скашивание» в сезоне — кукуруза на силос (как 10_main)."""
    from machine_tasks_batch_fetch import fetch_by_work_type_ids_sync

    field_ids: set[int] = set()
    for op in fetch_by_work_type_ids_sync(
        get_all_from_id,
        "agro_operations",
        SILAGE_CORN_MOWING_WT_IDS,
        {"season": TODAY.year},
    ):
        fid = op.get("field_id")
        if fid is not None:
            field_ids.add(int(fid))
    print(f"    кукуруза на силос (Скашивание): {len(field_ids)} полей")
    return field_ids


_SILAGE_IDS_CACHE: set[int] | None = None  # заполняется при параллельной выгрузке справочников
_CROPS_RAW: list[dict] | None = None           # сырой список crops (чтобы не грузить повторно)
_MACHINES_ALL: list[dict] | None = None        # сырой список machines (то же)


def _load_silage_corn_field_ids() -> set[int]:
    """Поля «Скашивание»: из кэша справочников, иначе live-выгрузка."""
    if _SILAGE_IDS_CACHE is not None:
        return set(_SILAGE_IDS_CACHE)
    return _fetch_silage_corn_field_ids()


def _resolve_corn_subtype_harvest(crop_name: str, field_id: int | None, silage_ids: set[int]) -> str:
    if not crop_name or "кукуруза" not in crop_name.casefold():
        return crop_name
    if field_id is None:
        return crop_name
    return CORN_SILAGE_CROP if int(field_id) in silage_ids else CORN_GRAIN_CROP


def _map_plow_crop_name(raw: str) -> str | None:
    """Нормализация культуры для вспашки → ключ из PLOW_SEASON_CROPS или None."""
    name = str(raw or "").strip()
    if not name:
        return None
    if name in PLOW_SEASON_CROPS:
        return name
    n = name.casefold().replace("ё", "е")
    if n == "свекла" or n.startswith("свекла "):
        return "Свекла столовая"
    if "кукуруза" in n:
        if "силос" in n:
            return "Кукуруза на силос"
        return "Кукуруза на зерно"
    return None


def _build_field_to_crop_plow(
    crops_map: dict[int, str],
    history_year_items: list[dict],
    *,
    season_year: int,
) -> dict[int, str]:
    """
    Культура поля для 2.4 Вспашка — только текущий сезон (season_year_for_date; как посев с
    build_field_to_crop(..., season_year=season_year)): history_items year=season_year,
    без добора из года отчёта. Дальше — только PLOW_SEASON_CROPS.
    """
    result: dict[int, str] = {}
    kept = 0
    skipped_other = 0
    for hi in history_year_items or []:
        if not hi.get("active"):
            continue
        try:
            yr = int(hi.get("year") or 0)
        except (TypeError, ValueError):
            yr = 0
        if yr != season_year:
            continue
        fid = hi.get("field_id")
        crop_id = hi.get("crop_id")
        if not fid or not crop_id:
            continue
        try:
            fid_i = int(fid)
            cid_i = int(crop_id)
        except (TypeError, ValueError):
            continue
        raw = str(crops_map.get(cid_i) or "").strip()
        if not raw or is_skip_crop(raw):
            continue
        mapped = _map_plow_crop_name(raw)
        if mapped is None:
            skipped_other += 1
            continue
        result[fid_i] = mapped
        kept += 1
    print(
        f"    field_to_crop_plow (сезон {season_year}, "
        f"{len(PLOW_SEASON_CROPS)} культур): {len(result)} полей "
        f"(записей ок={kept}, прочих культур пропущено={skipped_other})"
    )
    return result


def _build_field_to_crop_harvest(
    crops_map: dict[int, str],
    history_year_items: list[dict],
) -> dict[int, str]:
    """
    Культура поля для 5.0 Уборка — как 10_main_harvest.get_field_to_crop:
    history_items year=TODAY.year + разбивка кукурузы по «Скашиванию».
    Не использует field_to_crop_general (там озимые year_now отфильтрованы).
    """
    silage_ids = _load_silage_corn_field_ids()
    result: dict[int, str] = {}
    for hi in history_year_items or []:
        if not hi.get("active"):
            continue
        fid = hi.get("field_id")
        crop_id = hi.get("crop_id")
        if not fid or not crop_id:
            continue
        try:
            fid_i = int(fid)
            cid_i = int(crop_id)
        except (TypeError, ValueError):
            continue
        name = str(crops_map.get(cid_i) or "").strip()
        if not name or is_skip_crop(name):
            continue
        result[fid_i] = _resolve_corn_subtype_harvest(name, fid_i, silage_ids)
    print(f"    field_to_crop_harvest (year={TODAY.year}): {len(result)} полей")
    return result


def _iter_harvest_crop_rows(all_crops: list[str]) -> list[tuple[str, str]]:
    """Разворачивает кукурузу в блок: итог + силос + зерно."""
    has_corn = CORN_GRAIN_CROP in all_crops or CORN_SILAGE_CROP in all_crops
    if not has_corn:
        return [(crop, "normal") for crop in all_crops]

    rows: list[tuple[str, str]] = []
    corn_block_emitted = False
    for crop in all_crops:
        if crop == CORN_SILAGE_CROP:
            if CORN_GRAIN_CROP not in all_crops and not corn_block_emitted:
                rows.extend([
                    (CORN_SUMMARY_LABEL, "corn_summary"),
                    (CORN_SILAGE_CROP, "corn_silage"),
                    (CORN_GRAIN_CROP, "corn_grain"),
                ])
                corn_block_emitted = True
            continue
        if crop == CORN_GRAIN_CROP:
            rows.extend([
                (CORN_SUMMARY_LABEL, "corn_summary"),
                (CORN_SILAGE_CROP, "corn_silage"),
                (CORN_GRAIN_CROP, "corn_grain"),
            ])
            corn_block_emitted = True
            continue
        rows.append((crop, "normal"))
    return rows


def _crop_data_block(crop_data: dict, crop: str) -> dict:
    if crop in crop_data:
        return crop_data[crop]
    if crop == "Свекла столовая" and "Свекла" in crop_data:
        return crop_data["Свекла"]
    return {"total_ha": 0.0, "by_date": {}, "area_ha": 0.0}


def _plan_ha_for_crop(group_name: str, firm: str, crop: str) -> float:
    return plan_ha_for_report(group_name, firm, crop)


def _map_harvest_veg_crop(raw_name: str) -> str | None:
    """Приводит название культуры к ключам HARVEST_VEG_CROPS."""
    n = str(raw_name or "").strip().lower().replace("-", " ").replace("_", " ")
    if not n:
        return None
    if "карто" in n:
        return "Картофель"
    if "морков" in n:
        return "Морковь"
    if "свек" in n:
        return "Свекла столовая"
    if "лук" in n:
        return None
    name = str(raw_name or "").strip()
    if name in HARVEST_VEG_CROPS:
        return name
    return None


def _harvest_veg_crop_for_field(
    fid_i: int,
    primary_crop_map: dict[int, str],
    fallback_crop_map: dict[int, str],
) -> str | None:
    """Устаревший fallback; для 5.4 используйте field_to_crop_veg."""
    for crop_map in (primary_crop_map, fallback_crop_map):
        mapped = _map_harvest_veg_crop(crop_map.get(fid_i, ""))
        if mapped:
            return mapped
    return None


def _build_field_to_crop_veg_harvest(
    crop_map: dict[int, str],
    history_by_year: dict[int, list[dict]],
    priority_year: int,
) -> dict[int, str]:
    """
    Мапа только овощных полей из history_items (2027+2026).
    Зерновые/пропашные планы year=2027 не попадают — не перебивают овощь 2026.
    """
    field_to_crop: dict[int, str] = {}
    other_years = sorted(y for y in history_by_year if y != priority_year)
    for crop_year in [priority_year, *other_years]:
        for hi in history_by_year.get(crop_year, []):
            if not hi.get("active"):
                continue
            fid = hi.get("field_id")
            crop_id = hi.get("crop_id")
            if not fid or not crop_id:
                continue
            cname = crop_map.get(int(crop_id), "").strip()
            if not cname or cname in SKIP_CROPS:
                continue
            mapped = _map_harvest_veg_crop(cname)
            if not mapped:
                continue
            fid_i = int(fid)
            if fid_i in field_to_crop and crop_year != priority_year:
                continue
            field_to_crop[fid_i] = mapped
    return field_to_crop


def field_crops_for_operation_years(
    history_by_year: dict[int, list[dict]],
    years: list[int],
    crop_map: dict,
    *,
    newest_year_wins: bool = False,
) -> dict[int, str]:
    """Одна культура на поле среди сезонов операции.

    Площадь по годам не складывается: у поля остаётся одна запись.
    Без newest_year_wins сначала год отчёта, затем следующий — как общая карта,
    чтобы план следующего года не затёр уже идущий сезон.
    Озимый рапс и озимая пшеница берутся только из более нового года.
    """
    from plans import _is_winter_rape_or_wheat

    selected = []
    for year in years or []:
        try:
            selected.append(int(year))
        except (TypeError, ValueError):
            continue
    if not selected:
        return {}
    priority = max(selected)
    if newest_year_wins:
        order = sorted(set(selected))
    elif priority in selected and (priority - 1) in selected:
        order = [priority - 1, priority, *[y for y in sorted(set(selected)) if y not in (priority - 1, priority)]]
    else:
        order = sorted(set(selected))
    chosen: dict[int, str] = {}
    for crop_year in order:
        for hi in history_by_year.get(crop_year, []) or []:
            if not hi.get("active"):
                continue
            fid = hi.get("field_id")
            crop_id = hi.get("crop_id")
            if not fid or not crop_id:
                continue
            try:
                fid_i = int(fid)
                name = str(crop_map.get(int(crop_id)) or crop_map.get(crop_id) or "").strip()
            except (TypeError, ValueError):
                continue
            if not name or is_skip_crop(name):
                continue
            if not newest_year_wins and _is_winter_rape_or_wheat(name) and crop_year != priority:
                continue
            if fid_i in chosen and not newest_year_wins:
                continue
            chosen[fid_i] = name
    return chosen


def field_crops_per_season(
    history_by_year: dict[int, list[dict]],
    years: list[int],
    crop_map: dict,
) -> dict[int, list[str]]:
    """Культуры поля по каждому сезону операции (без выбора одной).

    Учётная площадь должна стоять под той культурой, по которой идёт работа. Факт строки
    привязан к записи истории своего сезона, поэтому поле с соей 2026 и озимым рапсом 2027
    входит и в «Соя», и в «Рапс озимый». Одна и та же культура в двух годах поле не удваивает.
    Озимый рапс и озимая пшеница берутся только из более нового года (как в
    field_crops_for_operation_years).
    """
    from plans import _is_winter_rape_or_wheat

    selected: list[int] = []
    for year in years or []:
        try:
            selected.append(int(year))
        except (TypeError, ValueError):
            continue
    if not selected:
        return {}
    priority = max(selected)
    out: dict[int, list[str]] = {}
    for crop_year in sorted(set(selected)):
        for hi in history_by_year.get(crop_year, []) or []:
            if not hi.get("active"):
                continue
            fid = hi.get("field_id")
            crop_id = hi.get("crop_id")
            if not fid or not crop_id:
                continue
            try:
                fid_i = int(fid)
                name = str(crop_map.get(int(crop_id)) or crop_map.get(crop_id) or "").strip()
            except (TypeError, ValueError):
                continue
            if not name or is_skip_crop(name):
                continue
            if _is_winter_rape_or_wheat(name) and crop_year != priority:
                continue
            names = out.setdefault(fid_i, [])
            if name not in names:
                names.append(name)
    return out


# Боронование, культивация, глубокорыхление: учётная площадь — сумма сезонов.
# Поле сезона 2026 и то же поле сезона 2027 входят оба раза.
_SUM_SEASON_AREA_GROUPS = frozenset({
    BORON_GROUP_NAME,
    CULTIVATION_GROUP_NAME,
    DEEP_GROUP_NAME,
})
# Культура вне плана остаётся в итоге (строка «Иные культуры»).
# «Не учитывать» в редакторе по-прежнему выбрасывает гектары.
_KEEP_UNLISTED_ACCOUNTED = frozenset({
    PLOW_GROUP_NAME,
    DISK_GROUP_NAME,
    BORON_GROUP_NAME,
    CULTIVATION_GROUP_NAME,
    DEEP_GROUP_NAME,
})


def fact_crop_code(group_name: str, firm: str, crop: str) -> str | None:
    """Культура в колонке «Выполнено». None — гектары этой строки не входят.

    Боронование, культивация и глубокорыхление сходятся с полной площадью
    операции за выбранные сезоны: культура вне плана остаётся в итоге.
    Явное «Не учитывать» в редакторе по-прежнему выбрасывает гектары.
    """
    coded = crop_code_for_report(group_name, crop)
    if coded is not None:
        return coded
    if group_name not in _KEEP_UNLISTED_ACCOUNTED:
        return None
    if is_crop_user_excluded(group_name, firm, crop):
        return None
    if other_crops_as_rows(group_name):
        return crop
    return OTHER_CROP_CODE


def _accounted_crop_label(group_name: str, raw: str, firm: str = "") -> str | None:
    """Учётная площадь под тем же именем, что и строка сводки.

    Режим «Иные культуры» из редактора планов складывается в одну строку
    «Иные культуры». На вспашке, дисковании, бороновании, культивации и
    глубокорыхлении культура вне плана тоже остаётся в этой строке, если её
    явно не убрали в «Не учитывать».
    """
    raw_name = str(raw or "").strip()
    if not raw_name or is_skip_crop(raw_name):
        return None
    if group_name == PLOW_GROUP_NAME:
        label = _map_plow_crop_name(raw_name)
    elif _is_harvest_veg_group(group_name):
        label = _map_harvest_veg_crop(raw_name)
    else:
        label = raw_name
    keep_unlisted = group_name in _KEEP_UNLISTED_ACCOUNTED
    if not label:
        if keep_unlisted and not is_crop_user_excluded(group_name, firm, raw_name):
            return OTHER_CROP_CODE
        return None
    if is_crop_user_excluded(group_name, firm, label):
        return None
    coded = crop_code_for_report(group_name, label)
    if coded is None:
        if keep_unlisted and not is_crop_user_excluded(group_name, firm, label):
            # Одна строка складывает культуру в «Иные». По культурам та же
            # площадь выходит своей строкой и в общую строку не входит.
            if other_crops_as_rows(group_name):
                return label or raw_name
            return OTHER_CROP_CODE
        return None
    return coded


def accounted_field_passes(
    group_name: str,
    history_by_year: dict[int, list[dict]],
    years: list[int],
    crop_map: dict,
) -> list[dict[int, list[str]]]:
    """Проходы учётной площади. Каждый проход прибавляет гектары полей.

    Боронование, культивация и глубокорыхление: отдельный проход на каждый
    сезон, чтобы 2026 и 2027 сложились. Озимые считаются в своём сезоне, а не
    только в более новом. Остальные операции — один проход на все сезоны.
    Внесение удобрений — одна, более новая культура.
    """
    selected: list[int] = []
    for year in years or []:
        try:
            selected.append(int(year))
        except (TypeError, ValueError):
            continue
    selected = sorted(set(selected))
    if group_name in _SUM_SEASON_AREA_GROUPS:
        return [
            field_crops_per_season(history_by_year, [year], crop_map)
            for year in selected
        ]
    if group_name == FERT_GROUP_NAME:
        chosen = field_crops_for_operation_years(
            history_by_year, selected, crop_map, newest_year_wins=True,
        )
        return [{fid: [raw] for fid, raw in chosen.items()}]
    return [field_crops_per_season(history_by_year, selected, crop_map)]


def build_accounted_area_by_group(
    history_by_year: dict[int, list[dict]],
    crop_map: dict,
    field_area: dict[int, float],
    field_to_firm: dict[int, str],
    available_ids: set[int],
    report_date: date,
) -> dict[str, dict[str, dict[str, float]]]:
    """Учётная площадь: фирма → культура → га, отдельно для каждой операции.

    Годы — сезоны операции в редакторе. У боронования, культивации и
    глубокорыхления площадь сезонов складывается. У остальных одна и та же
    культура в двух годах поле не удваивает. Внесение удобрений — одна,
    более новая культура.
    """
    from season_unified import fact_season_years_for_group

    out: dict[str, dict[str, dict[str, float]]] = {}
    for group_name, _wt in WORK_GROUPS:
        try:
            years = list(fact_season_years_for_group(group_name, report_date) or [])
        except Exception:
            years = [report_date.year]
        # Эти три операции считают поля сезона, даже если на дату отчёта
        # поле уже не available: иначе старший сезон теряет гектары.
        require_available = group_name not in _SUM_SEASON_AREA_GROUPS
        by_firm: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for per_field in accounted_field_passes(
            group_name, history_by_year, years, crop_map,
        ):
            for fid, raws in per_field.items():
                if require_available and fid not in available_ids:
                    continue
                firm = field_to_firm.get(fid)
                if not firm:
                    continue
                labels: list[str] = []
                for raw in raws:
                    label = _accounted_crop_label(group_name, raw, firm)
                    if label and label not in labels:
                        labels.append(label)
                area = float(field_area.get(fid, 0.0) or 0.0)
                for label in labels:
                    by_firm[firm][label] += area
        out[group_name] = {firm: dict(crops_ha) for firm, crops_ha in by_firm.items()}
    return out


def _is_harvest_veg_group(group_name: str) -> bool:
    g = str(group_name or "").strip()
    return g in {HARVEST_VEG_PLAN_KEY, HARVEST_VEG_GROUP_NAME} or g.startswith("5.4 Уборка")


def _group_display_name(group_name: str) -> str:
    g = str(group_name or "").strip()
    if g == HARVEST_VEG_PLAN_KEY:
        return HARVEST_VEG_GROUP_NAME
    return g


def _planned_crops_for_group_firm(group_name: str, firm: str) -> list[str]:
    return [
        crop for (g, f, crop) in HARDCODED_PLAN_FLAT
        if g == group_name and f == firm
    ]


def _crop_plan_multiplier(crop_name: str) -> float:
    if (crop_name or "").strip().lower() in OZ_RAPE_CROP_NAMES:
        return 2.0
    return 1.0

_PREFERRED_CROP_INDEX = {name.lower(): i for i, name in enumerate(PREFERRED_CROP_ORDER)}

def _crop_sort_key(name: str) -> tuple[int, int, str]:
    raw = str(name or "").strip()
    low = raw.lower()
    if low in _PREFERRED_CROP_INDEX:
        return (0, _PREFERRED_CROP_INDEX[low], low)
    # Культуры вне списка — после приоритетных, в алфавите.
    return (1, 10_000, low)


def _crop_sort_key_for_group(group_name: str, name: str) -> tuple[int, int, str]:
    """Сортировка с учётом ранга группы из редактора планов."""
    try:
        return crop_sort_key_for_group(group_name, name)
    except Exception:
        return _crop_sort_key(name)


def _apply_manual_plan_rates() -> None:
    path = os.environ.get(MANUAL_OVERRIDES_ENV, "").strip()
    if not path:
        return
    p = Path(path)
    if not p.is_file():
        return
    try:
        store = json.loads(p.read_text(encoding="utf-8"))
        block = store.get("2") or {}
        raw_map = block.get("plan_rate_by_group")
        if not isinstance(raw_map, dict):
            return
        for group_name, raw in raw_map.items():
            try:
                PLAN_RATE_PER_GROUP[str(group_name)] = float(raw)
            except (TypeError, ValueError):
                continue
    except Exception:
        return

# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ИЕРАРХИИ ПАПОК
# =============================================================================

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
        pid = (gf.get("parent_id")
               or gf.get("parent_group_folder_id")
               or gf.get("group_folder_parent_id"))
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


def load_field_to_firm(fields_all: list[dict],
                       fgs_all: list[dict],
                       gfs_all: list[dict]) -> dict[int, str]:
    """
    Строит словарь field_id → firm_name через иерархию папок.
    Также применяет принудительные правила по имени поля.
    """
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
        # Сначала ищем по подсказкам имён папок в цепочке
        for cid in _folder_id_chain(rint, parent_map):
            if cid in gf_hints:
                firm_key = gf_hints[cid]
                break
        # Затем по корневым папкам холдинга
        if firm_key is None:
            root = _resolve_to_holding_root(rint, parent_map, root_ids)
            if root is not None:
                firm_key = FOLDER_TO_FIRM.get(root)
        if firm_key:
            field_to_firm[int(fid)] = firm_key

    # Принудительные правила по имени поля (перезаписывают)
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
        f"(принудительно по имени: {forced})"
    )
    return field_to_firm


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


# =============================================================================
# HTTP
# =============================================================================

from cropwise_paging import CROPWISE_PAGE_LIMIT, from_id_should_continue

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})

_TLS = threading.local()


def _thread_session() -> requests.Session:
    """requests.Session на поток: справочники и задачи грузятся параллельно.

    Retry/backoff на 429 и 5xx (раньше один 502 валил весь отчёт). После
    исчерпания попыток возвращается последний ответ, а raise_for_status()
    в get_all_from_id бросает HTTPError, как и раньше.
    """
    sess = getattr(_TLS, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({"X-User-Api-Token": API_TOKEN})
        kwargs = dict(
            total=6, connect=6, read=6, status=6, backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            respect_retry_after_header=True, raise_on_status=False,
        )
        try:
            retry = Retry(allowed_methods=frozenset({"GET"}), **kwargs)
        except TypeError:  # urllib3 < 1.26
            retry = Retry(method_whitelist=frozenset({"GET"}), **kwargs)
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _TLS.session = sess
    return sess


def get_all_from_id(endpoint: str,
                    extra_params: dict | None = None) -> list[dict]:
    """Курсорная выгрузка endpoint (from_id). Потокобезопасна: сессия на поток."""
    params  = dict(extra_params or {})
    results = []
    from_id = 0
    sess = _thread_session()
    for _ in range(500):
        r = sess.get(f"{BASE_URL}/{endpoint}",
                     params={**params, "from_id": from_id, "limit": CROPWISE_PAGE_LIMIT},
                     timeout=120)
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
    else:
        print(f"  ВНИМАНИЕ: {endpoint}: достигнут лимит 500 страниц, выгрузка может быть неполной", flush=True)
    return results

# =============================================================================
# ЗАГРУЗКА СПРАВОЧНИКОВ
# =============================================================================

class _IhiIntervals(list):
    """Разобранные интервалы (field_id_raw, ev_start, ev_end) только для available=True."""


def _ihi_intervals(ihi_all: list[dict]) -> "_IhiIntervals":
    """Один разбор дат inventory_history_items вместо трёх (plan / period / general)."""
    out = _IhiIntervals()
    for r in ihi_all:
        if not r.get("available"):
            continue
        fid = r.get("historyable_id")
        if fid is None:
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
        out.append((fid, ev_start, ev_end))
    return out


def _ids_from_intervals(intervals, period_start: date, period_end: date) -> set[int]:
    """Пересечение: ev_start <= period_end AND (ev_end IS NULL OR ev_end >= period_start)."""
    ids: set[int] = set()
    for fid, ev_start, ev_end in intervals:
        if ev_start <= period_end and (ev_end is None or ev_end >= period_start):
            ids.add(int(fid))
    return ids


def _load_available_field_ids_in_period(period_start: date, period_end: date,
                                         ihi_all: list[dict]) -> set[int]:
    """
    Возвращает множество field_id, у которых available=True хотя бы в один день
    периода [period_start, period_end]. Использует уже загруженный список ihi_all.
    Пересечение: ev_start <= period_end AND (ev_end IS NULL OR ev_end >= period_start).
    """
    intervals = ihi_all if isinstance(ihi_all, _IhiIntervals) else _ihi_intervals(ihi_all)
    available_ids = _ids_from_intervals(intervals, period_start, period_end)

    print(f"    Полей available=True в периоде {period_start}–{period_end}: {len(available_ids)}")
    return available_ids


def _fetch_reference_raw(years_to_load: list[int]) -> dict:
    """Независимые справочники грузятся ПАРАЛЛЕЛЬНО (между эндпоинтами курсоры независимы).

    Результат не зависит от порядка выгрузки: история по годам собирается
    потом в порядке years_to_load, как и раньше.
    """
    global _SILAGE_IDS_CACHE
    jobs: dict[str, object] = {
        "history:%d" % y: (lambda yy=y: get_all_from_id("history_items", {"year": yy}))
        for y in years_to_load
    }
    jobs["inventory_history_items"] = lambda: get_all_from_id(
        "inventory_history_items", {"historyable_type": "Field"})
    jobs["crops"] = lambda: get_all_from_id("crops")
    jobs["fields"] = lambda: get_all_from_id("fields")
    jobs["field_groups"] = lambda: get_all_from_id("field_groups")
    jobs["group_folders"] = lambda: get_all_from_id("group_folders")
    jobs["machines"] = lambda: get_all_from_id("machines")
    jobs["silage"] = _fetch_silage_corn_field_ids

    results = run_reference_jobs(
        jobs,
        parallel=PIPELINE,
        workers=REFERENCE_WORKERS,
        thread_prefix="r2-ref",
    )

    _SILAGE_IDS_CACHE = results["silage"]
    return {
        "crops": results["crops"],
        "history": {y: results["history:%d" % y] for y in years_to_load},
        "fields": results["fields"],
        "field_groups": results["field_groups"],
        "group_folders": results["group_folders"],
        "ihi": results["inventory_history_items"],
        "machines": results["machines"],
    }


def load_reference_data(*, refresh: bool = True) -> tuple[  # noqa: C901
    dict[int, str],
    dict[int, str],
    dict[int, str],
    dict[int, str],
    dict[int, str],
    dict[int, str],
    dict[int, str],
    set[int],
    set[int],
    set[int],
    dict[int, float],
    dict[int, int],
    dict[str, float],
    dict[str, dict[str, float]],
    dict[str, dict[str, float]],
    dict[int, str],
    dict[int, int],
]:
    global _CROPS_RAW, _MACHINES_ALL, _SILAGE_IDS_CACHE
    if refresh:
        refresh_season_config()
    _CROPS_RAW = _MACHINES_ALL = _SILAGE_IDS_CACHE = None

    season_year = season_year_for_date(TODAY)
    years_to_load = [season_year, season_year - 1]
    # Сезоны из редактора (напр. 1.1 → 2025+2026) — догружаем history_items.
    for y in FERT_SEASON_YEARS:
        if y not in years_to_load:
            years_to_load.append(y)
    for _g in set(SEASON_RANGE_OVERRIDES) | set(GROUP_SEASON_RULE):
        for y in resolve_group_season_config(_g, TODAY).get("history_years") or []:
            if y not in years_to_load:
                years_to_load.append(y)
    years_to_load = sorted(set(years_to_load), reverse=True)

    print(
        f"  Загружаем справочники ({'параллельно' if PIPELINE else 'последовательно'}): "
        f"crops, history_items {years_to_load}, fields, field_groups, group_folders, "
        f"inventory_history_items, machines, силос..."
    )
    raw_ref = _fetch_reference_raw(years_to_load)
    _CROPS_RAW = raw_ref["crops"]
    _MACHINES_ALL = raw_ref["machines"]
    crops_map = {c["id"]: c.get("name") or c.get("title", "")
                 for c in raw_ref["crops"]}

    history_by_year: dict[int, list[dict]] = {}
    all_history_items: list[dict] = []
    for crop_year in years_to_load:
        chunk = raw_ref["history"][crop_year]
        history_by_year[crop_year] = chunk
        all_history_items.extend(chunk)
        print(f"    history_items year={crop_year}: {len(chunk)} записей")

    # 4.0 Посев — культура по текущему сезону (как 5_main_fixed: CROP_SEASON_YEAR).
    field_to_crop = build_field_to_crop(
        crop_map=crops_map,
        history_items=all_history_items,
        report_date=TODAY,
        period_start=START_DATE,
        skip_crops=SKIP_CROPS,
        season_year=season_year,
    )

    # HistoryItem.id -> crop_name — для связки MachineTask ->
    # MachineTaskAgroOperationMappingItem -> AgroOperation -> HistoryItem.
    history_item_to_crop: dict[int, str] = {}
    history_item_to_year: dict[int, int] = {}
    for hi in all_history_items:
        hi_id   = hi.get("id")
        crop_id = hi.get("crop_id")
        if hi_id is None or crop_id is None:
            continue
        cname = (crops_map.get(int(crop_id)) or "").strip()
        if cname:
            history_item_to_crop[int(hi_id)] = cname
            try:
                history_item_to_year[int(hi_id)] = int(hi.get("year") or 0)
            except (TypeError, ValueError):
                pass
    print(f"    {len(history_item_to_crop)} history_item -> культура (для связки через AgroOperation)")
    crop_field_ids = set(field_to_crop.keys())
    print(
        f"    {len(crop_field_ids)} полей с культурой посева "
        f"(сезон {season_year}, sowing_date + оверлей рапса)"
    )

    field_to_crop_general = build_field_to_crop_general(
        crop_map=crops_map,
        history_by_year=history_by_year,
        priority_year=season_year,
        skip_crops=SKIP_CROPS,
    )
    _remember_field_crops_by_year(history_by_year, crops_map)
    # 5.0 Уборка — культура как в 10_main (history year=TODAY.year + силос по Скашиванию).
    print(f"  Строим field_to_crop_harvest (уборка, year={TODAY.year})...")
    field_to_crop_harvest = _build_field_to_crop_harvest(
        crops_map={int(k): str(v) for k, v in crops_map.items() if k is not None},
        history_year_items=history_by_year.get(TODAY.year, []),
    )
    # 2.4 Вспашка — только сезон year+1 (как посев season_year), разрешённый перечень культур.
    print(f"  Строим field_to_crop_plow (вспашка, сезон {season_year})...")
    field_to_crop_plow = _build_field_to_crop_plow(
        crops_map={int(k): str(v) for k, v in crops_map.items() if k is not None},
        history_year_items=history_by_year.get(season_year, []),
        season_year=season_year,
    )
    report3_history_years = [int(y) for y in (FERT_SEASON_YEARS or [TODAY.year])]
    # Как сводка 1 / 3_main: build_field_to_crop_general + newest_year_wins.
    fert_history = {
        y: history_by_year.get(y, [])
        for y in report3_history_years
        if y in history_by_year
    }
    field_to_crop_report3 = build_field_to_crop_general(
        crop_map=crops_map,
        history_by_year=fert_history,
        priority_year=max(report3_history_years) if report3_history_years else season_year,
        skip_crops=SKIP_CROPS,
        newest_year_wins=True,
    )
    crop_field_ids_report3 = set(field_to_crop_report3.keys())
    print(
        f"    {len(crop_field_ids_report3)} полей для 1.1 (history_items "
        f"years={report3_history_years}, active, без SKIP_CROPS)"
    )
    print(
        f"    {len(field_to_crop_general)} полей с культурой "
        f"(общая мапа {years_to_load}, приоритет {season_year})"
    )

    field_to_crop_veg = _build_field_to_crop_veg_harvest(
        crops_map, history_by_year, season_year,
    )
    print(
        f"    {len(field_to_crop_veg)} овощных полей "
        f"(history_items {years_to_load}, приоритет {season_year})"
    )

    fields_all = raw_ref["fields"]
    fgs_all    = raw_ref["field_groups"]
    gfs_all    = raw_ref["group_folders"]

    # Площадь полей (все поля, не только активные — на случай нужды)
    field_area: dict[int, float] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        field_area[fid] = (f.get("tillable_area") or
                           f.get("calculated_area") or 0.0)
    try:
        from svodki_sanity import register_field_areas as _reg_fa
        _reg_fa(field_area)
    except ImportError:
        pass

    for _diag_crop in ("Рапс озимый", "Пшеница озимая"):
        _diag_fids = [fid for fid, c in field_to_crop_general.items() if c == _diag_crop]
        _diag_area = sum(field_area.get(fid, 0.0) or 0.0 for fid in _diag_fids)
        print(
            f"    [диагностика] '{_diag_crop}' в field_to_crop_general: "
            f"{len(_diag_fids)} полей, {_diag_area:,.0f} га (физическая площадь)"
        )

    # Определяем агрофирму для каждого поля через полную иерархию папок
    field_to_firm_all = load_field_to_firm(fields_all, fgs_all, gfs_all)

    # inventory_history_items уже выгружены (параллельно со всеми справочниками);
    # даты разбираем ОДИН раз и используем для плана, факта и general-периода.
    ihi_all = raw_ref["ihi"]
    print(f"    Всего записей inventory_history_items: {len(ihi_all)}")
    ihi_intervals = _ihi_intervals(ihi_all)

    # --- Для ПЛАНА: только поля, активные (available=True) на сегодня ---
    plan_available_ids = _ids_from_intervals(ihi_intervals, TODAY, TODAY)
    print(f"    Полей available=True на сегодня ({TODAY}): {len(plan_available_ids)}")

    # field_to_firm для плана: поле есть в сезоне И активно сегодня
    field_to_firm_plan = {
        fid: firm for fid, firm in field_to_firm_all.items()
        if fid in crop_field_ids and fid in plan_available_ids
    }
    print(f"    {len(field_to_firm_plan)} полей для плана (активны сегодня, с культурой)")

    # --- Для ФАКТА: поля активные в периоде и с культурой (как сводка 1) ---
    period_available_ids = _load_available_field_ids_in_period(
        START_DATE, TODAY, ihi_intervals
    )
    period_available_ids_general = _load_available_field_ids_in_period(
        FERT_PERIOD_START, FERT_PERIOD_END, ihi_intervals
    )
    field_to_firm = {
        fid: firm for fid, firm in field_to_firm_all.items()
        if fid in crop_field_ids and fid in period_available_ids
    }
    field_to_firm_general = {
        fid: firm for fid, firm in field_to_firm_all.items()
        if fid in field_to_crop_general and fid in period_available_ids_general
    }
    field_to_firm_veg = {
        fid: firm for fid, firm in field_to_firm_all.items()
        if fid in field_to_crop_veg and fid in period_available_ids_general
    }
    print(
        f"    {len(field_to_firm)} полей для факта "
        f"(активны в периоде {START_DATE}–{TODAY}, с культурой)"
    )
    print(
        f"    {len(field_to_firm_general)} полей для 1.1/опрыскивания/уборки "
        f"(активны в периоде {FERT_PERIOD_START}–{FERT_PERIOD_END}, с культурой)"
    )
    print(
        f"    {len(field_to_firm_veg)} полей для 5.4 уборки овощей "
        f"(активны в периоде {FERT_PERIOD_START}–{FERT_PERIOD_END})"
    )

    # Плановые площади считаем только по полям, активным сегодня
    firm_plan_ha:      dict[str, float]            = defaultdict(float)
    firm_crop_area_ha: dict[str, dict[str, float]] = defaultdict(
        lambda: defaultdict(float))
    # Учётная площадь для дискования — оба сезона (как field_to_crop_general).
    firm_crop_area_ha_general: dict[str, dict[str, float]] = defaultdict(
        lambda: defaultdict(float))

    for fid, firm in field_to_firm_plan.items():
        crop = field_to_crop.get(fid, "")
        if is_skip_crop(crop):
            continue
        area = field_area.get(fid, 0.0)
        firm_plan_ha[firm] += area
        firm_crop_area_ha[firm][crop] += area

    for fid, crop in field_to_crop_general.items():
        firm = field_to_firm_all.get(fid)
        if not firm or is_skip_crop(crop):
            continue
        if fid not in plan_available_ids:
            continue
        firm_crop_area_ha_general[firm][crop] += field_area.get(fid, 0.0)

    for fid, crop in field_to_crop_veg.items():
        firm = field_to_firm_all.get(fid)
        if not firm:
            continue
        firm_crop_area_ha[firm][crop] += field_area.get(fid, 0.0)

    accounted_by_group = build_accounted_area_by_group(
        history_by_year,
        crops_map,
        field_area,
        field_to_firm_all,
        plan_available_ids,
        TODAY,
    )

    machines_all = raw_ref["machines"]
    machine_group: dict[int, int] = {
        m["id"]: (m.get("machine_group_id") or 0)
        for m in machines_all
    }

    return (
        field_to_crop,
        field_to_crop_general,
        field_to_crop_harvest,
        field_to_crop_plow,
        field_to_crop_veg,
        field_to_crop_report3,
        field_to_firm,
        field_to_firm_general,
        field_to_firm_veg,
        field_to_firm_all,
        crop_field_ids_report3,
        period_available_ids,
        period_available_ids_general,
        field_area,
        machine_group,
        dict(firm_plan_ha),
        {f: dict(crops) for f, crops in firm_crop_area_ha.items()},
        {f: dict(crops) for f, crops in firm_crop_area_ha_general.items()},
        history_item_to_crop,
        history_item_to_year,
        accounted_by_group,
    )

# MTFMI / mtaomi / mass_request — utils/api/mtfmi_batch_fetch.py, mtaomi_batch_fetch.py


async def _fetch_task_agro_ops_raw(
    task_ids: list[int],
    headers: dict,
) -> tuple[dict[int, set[int]], dict[int, int], dict[int, int]]:
    """
    СЫРЫЕ данные для культуры через AgroOperation (без history_item_to_crop):
      task_id → {agro_operation_id}, agro_operation_id → history_item_id / field_id.

    Не зависит от справочников, поэтому выполняется в фоне вместе с mtfmi и ОДИН
    раз на все группы (а не по asyncio.run на каждую группу).
    """
    task_to_agro_ops: dict[int, set[int]] = defaultdict(set)
    agro_op_to_history: dict[int, int] = {}
    agro_op_to_field: dict[int, int] = {}
    if not task_ids:
        return task_to_agro_ops, agro_op_to_history, agro_op_to_field

    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    from mtaomi_batch_fetch import fetch_mtaomi_by_machine_task_ids, mass_request_async

    # Связи «задание → агрооперация» лежат в sqlite-хранилище и обновляются только изменённым
    # (svodki_task_store_links). Раньше они качались заново целиком на каждый запуск.
    try:
        from svodki_task_store import get_store
        from svodki_task_store_links import links_enabled

        _store = get_store(BASE_URL) if links_enabled() else None
    except ImportError:
        _store = None
    if _store is not None and hasattr(_store, "get_task_agro_ops_raw"):
        try:
            async with httpx.AsyncClient(headers=headers, timeout=120) as client:
                return await _store.get_task_agro_ops_raw(client, semaphore, task_ids, log="[r2] ")
        except Exception as exc:  # хранилище — оптимизация: при сбое качаем как раньше
            print(f"  [r2] связи агроопераций из хранилища недоступны ({exc!r}) — выгрузка из API", flush=True)

    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        mtaomi_map = await fetch_mtaomi_by_machine_task_ids(
            client, BASE_URL, task_ids, semaphore, log_prefix="[r2] ",
        )
        for tid, items in mtaomi_map.items():
            for item in items:
                aoid = item.get("agro_operation_id")
                if aoid is not None:
                    task_to_agro_ops[int(tid)].add(int(aoid))

        all_agro_op_ids = sorted({aoid for ids in task_to_agro_ops.values() for aoid in ids})
        agro_ops = await mass_request_async(
            client, BASE_URL, "agro_operations", all_agro_op_ids, semaphore,
        )

    for ao in agro_ops:
        if ao.get("id") is None:
            continue
        aoid = int(ao["id"])
        if ao.get("history_item_id") is not None:
            agro_op_to_history[aoid] = int(ao["history_item_id"])
        if ao.get("field_id") is not None:
            agro_op_to_field[aoid] = int(ao["field_id"])
    return task_to_agro_ops, agro_op_to_history, agro_op_to_field


def _resolve_task_crops(
    raw: tuple[dict[int, set[int]], dict[int, int], dict[int, int]] | None,
    history_item_to_crop: dict[int, str],
    only_task_ids=None,
) -> tuple[dict[int, str], dict[tuple[int, int], str]]:
    """
    Культура MachineTask через AgroOperation -> HistoryItem (точнее, чем поле+дата).
    Второй словарь — (task_id, field_id) → культура: на одном задании бывают
    пшеница, соя и картофель, и одна культура на всё задание их смешивает.
    Сезон озимых фильтруется отдельно в `_crop_for_task_field` по MachineTask.season.

    only_task_ids — разбор только для задач одной группы (срез общей выгрузки).
    Вызывается ПОСЛЕ _load_mtfmi_row_crops, который может дополнить
    history_item_to_crop (порядок важен для идентичности результата).
    """
    task_crop: dict[int, str] = {}
    task_field_crop: dict[tuple[int, int], str] = {}
    if raw is None:
        return task_crop, task_field_crop
    task_to_agro_ops, agro_op_to_history, agro_op_to_field = raw
    wanted = None if only_task_ids is None else set(only_task_ids)
    for tid, aoids in task_to_agro_ops.items():
        if wanted is not None and tid not in wanted:
            continue
        crops_found: set[str] = set()
        for aoid in aoids:
            hi_id = agro_op_to_history.get(aoid)
            if hi_id is None:
                continue
            crop = history_item_to_crop.get(hi_id)
            if not crop:
                continue
            crops_found.add(crop)
            fid = agro_op_to_field.get(aoid)
            if fid is not None:
                task_field_crop[(int(tid), fid)] = crop
        if len(crops_found) == 1:
            task_crop[tid] = next(iter(crops_found))
    return task_crop, task_field_crop


async def fetch_task_crop_via_agro_operation(
    task_ids: list[int],
    headers: dict,
    history_item_to_crop: dict[int, str],
) -> tuple[dict[int, str], dict[tuple[int, int], str]]:
    """Совместимость: выгрузка + разбор для одного набора задач (в конвейере не используется)."""
    raw = await _fetch_task_agro_ops_raw(task_ids, headers)
    return _resolve_task_crops(raw, history_item_to_crop)


def _load_mtfmi_row_crops(
    mtfmi_map: dict[int, list[dict]],
    history_item_to_crop: dict[int, str],
    headers: dict,
    history_item_to_year: dict[int, int] | None = None,
) -> dict[int, str]:
    """Культура строки площади: MachineTaskFieldMappingItem.history_item_id."""
    pairs: list[tuple[int, int]] = []
    missing: set[int] = set()
    rows = no_hid = 0
    for items in mtfmi_map.values():
        for item in items:
            rows += 1
            mid = item.get("id")
            hid = item.get("history_item_id")
            if hid is None:
                no_hid += 1
            if mid is None or hid is None:
                continue
            mid_i, hid_i = int(mid), int(hid)
            pairs.append((mid_i, hid_i))
            if hid_i not in history_item_to_crop:
                missing.add(hid_i)
    extra: dict[int, str] = {}
    if missing:
        from mtaomi_batch_fetch import mass_request_async

        crops_map = {
            int(c["id"]): str(c.get("name") or c.get("title") or "").strip()
            for c in (_CROPS_RAW if _CROPS_RAW is not None else get_all_from_id("crops"))
            if c.get("id") is not None
        }

        async def _fetch() -> list[dict]:
            async with report_async_client(
                API_TOKEN, semaphore_limit=SEMAPHORE_LIMIT, timeout=120,
            ) as (client, sem):
                return await mass_request_async(
                    client, BASE_URL, "history_items", sorted(missing), sem,
                )

        for hi in run_report_async(_fetch()):
            if hi.get("id") is None or hi.get("crop_id") is None:
                continue
            name = crops_map.get(int(hi["crop_id"]), "")
            if not name:
                continue
            extra[int(hi["id"])] = name
            history_item_to_crop.setdefault(int(hi["id"]), name)
            try:
                year = int(hi.get("year") or 0)
            except (TypeError, ValueError):
                year = 0
            if year and history_item_to_year is not None:
                history_item_to_year.setdefault(int(hi["id"]), year)
    out: dict[int, str] = {}
    for mid_i, hid_i in pairs:
        name = extra.get(hid_i) or history_item_to_crop.get(hid_i) or ""
        if name:
            out[mid_i] = name
    print(
        f"  культура строки площади: строк {rows}, без history_item_id {no_hid}, "
        f"с культурой {len(out)} из {len(pairs)}, "
        f"history_item вне годовой карты: {len(missing)}",
        flush=True,
    )
    return out

# =============================================================================
# СБОР ДАННЫХ
# =============================================================================



def _resolve_firm_for_fact(
    fid_i: int,
    group_name: str,
    field_to_crop: dict[int, str],
    field_to_firm: dict[int, str],
    field_to_firm_all: dict[int, str],
    period_available_ids: set[int],
    *,
    use_report3_field_filter: bool = False,
) -> str | None:
    """Фильтр полей для факта.

    При _HISTORY_FIELD_ONLY культура строки уже известна из history_item,
    поэтому членство поля в карте культур факт не отсекает. Фирма берётся
    из полной карты, доступность периода — как раньше у группы.
    """
    if _HISTORY_FIELD_ONLY:
        if use_report3_field_filter or group_name in (
            {DISK_GROUP_NAME, HARVEST_GROUP_NAME, SPRAY_GROUP_NAME, VEG_SPRAY_GROUP_NAME}
            | TILLAGE_GROUPS
            | FACT_BY_ALL_FIELDS_GROUPS
        ):
            return field_to_firm_all.get(fid_i)
        if fid_i not in period_available_ids:
            return None
        return field_to_firm_all.get(fid_i)

    if use_report3_field_filter:
        if fid_i not in field_to_crop:
            return None
        return field_to_firm_all.get(fid_i)
    use_all_firm_map = (
        group_name == DISK_GROUP_NAME
        or group_name in TILLAGE_GROUPS
    )
    if use_all_firm_map:
        return field_to_firm_all.get(fid_i)
    if group_name in FACT_BY_ALL_FIELDS_GROUPS:
        return field_to_firm_all.get(fid_i)
    if group_name == HARVEST_GROUP_NAME:
        # Как 10_main: cache.field_to_firm без фильтра available/crop.
        return field_to_firm_all.get(fid_i)
    if group_name == CULTIVATION_GROUP_NAME:
        # Как 8_main_fixed: history_items year=TODAY.year (field_to_crop_report3) + field_to_firm_all.
        if fid_i not in period_available_ids or fid_i not in field_to_crop:
            return None
        return field_to_firm_all.get(fid_i)
    if _is_harvest_veg_group(group_name):
        if fid_i not in period_available_ids or fid_i not in field_to_crop:
            return None
        return field_to_firm_all.get(fid_i)
    if fid_i not in period_available_ids:
        return None
    firm = field_to_firm.get(fid_i)
    if not firm or fid_i not in field_to_crop:
        return None
    return firm

def _empty_group_block() -> dict:
    return {
        "crops":              {},
        "machines":           {f: defaultdict(lambda: {"own": set(), "hired": set()})
                               for f in FIRM_ORDER},
        "area_by_firm_date":  {f: defaultdict(float) for f in FIRM_ORDER},
        "area_by_firm_total": {f: 0.0 for f in FIRM_ORDER},
        "machine_day_area":   {f: {} for f in FIRM_ORDER},
    }


def _machine_qualifies_for_area(total: float, min_ha: float) -> bool:
    tc = round(float(total), 2)
    if min_ha <= 0:
        return tc > 0
    return tc >= min_ha


def _flush_area_from_machines(
    data: dict,
    group_name: str,
    machine_day_area: dict[str, dict[date, dict[int | None, float]]],
    machine_day_items: dict[str, dict[date, dict[int | None, list[tuple[int, str, float]]]]],
    firm_crop_fields: dict[str, dict[str, set]],
    agg: dict[str, dict[str, dict[date, float]]],
) -> bool:
    """Площадь за смену: порог min_ha на машину (как 3_main), не на mtfmi-строку."""
    min_ha = _detail_area_min_ha(group_name)
    gd = data[group_name]
    group_has = False

    for firm in FIRM_ORDER:
        gd["area_by_firm_total"][firm] = 0.0
        gd["area_by_firm_date"][firm] = defaultdict(float)

    for firm in FIRM_ORDER:
        for t_date, by_mid in machine_day_area.get(firm, {}).items():
            day_area = sum(
                float(v) for v in by_mid.values()
                if _machine_qualifies_for_area(v, min_ha)
            )
            if day_area <= 0:
                continue
            group_has = True
            gd["area_by_firm_date"][firm][t_date] += day_area
            gd["area_by_firm_total"][firm] += day_area

            for mid, total in by_mid.items():
                if not _machine_qualifies_for_area(total, min_ha):
                    continue
                for fid_i, crop, cov in machine_day_items.get(firm, {}).get(t_date, {}).get(mid, []):
                    agg[firm][crop][t_date] += cov
                    firm_crop_fields[firm][crop].add(fid_i)

    return group_has


def _shift_window_report1(end_time_raw, day_start_hour: int = 6) -> tuple[date, datetime, datetime] | None:
    """Окно смены — как в 1_main_ammonium__1_.py."""
    end_dt = parse_dt(end_time_raw)
    if end_dt is None:
        return None
    cal_date = end_dt.date()
    anchor = datetime(
        cal_date.year, cal_date.month, cal_date.day,
        day_start_hour, 0, 0, tzinfo=TZ_MSK,
    )
    if end_dt < anchor:
        cal_date = cal_date - timedelta(days=1)
        anchor = datetime(
            cal_date.year, cal_date.month, cal_date.day,
            day_start_hour, 0, 0, tzinfo=TZ_MSK,
        )
    return cal_date, anchor, anchor + timedelta(days=1)


def _task_in_report_shift_window(
    end_time_raw,
    day_start: datetime,
    cutoff: datetime,
) -> bool:
    sw = _shift_window_report1(end_time_raw)
    if sw is None:
        return False
    _, shift_ds, shift_co = sw
    return shift_ds >= day_start and shift_co <= cutoff


def _prepare_group_selection(
    headers: dict,
    *,
    refresh: bool = True,
    tasks_window: tuple[str, str] | None = None,
) -> dict:
    """
    Фаза A сводки: НЕ зависит от справочников, поэтому в конвейере идёт в фоне
    параллельно с load_reference_data.

      machine_tasks (prefetch/live) → отбор по группам и сменам → mtfmi
      и сырые связи mtaomi/agro_operations (культура через агрооперацию).

    Карты полей/фирм здесь не нужны: в pending_groups хранятся только ИМЕНА
    карт, фаза B (collect_work_data) подставляет сами карты.
    """
    _season_cache_snapshot()
    if refresh:
        refresh_season_config()
    data: dict[str, dict] = {}
    pending_groups: list[dict] = []
    use_crop_via_agro_op = _parse_bool_env("SVODKI_CROP_VIA_AGRO_OPERATION", True)

    DAY_START_HOUR = 6  # смена начинается в 06:00 МСК
    # Сутки отчёта при смене 06:00–06:00 (REPORT_DATE = TODAY из env): [TODAY-1 06:00, TODAY 06:00)
    REPORT_DAY_CUTOFF = datetime(
        TODAY.year, TODAY.month, TODAY.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    REPORT_DAY_START = REPORT_DAY_CUTOFF - timedelta(days=1)
    REPORT_SHIFT_DATE = TODAY - timedelta(days=1)

    def _shift_date_and_window(
        end_time_raw,
        period_start: date,
        period_end: date | None = None,
        *,
        use_plan_today_bound: bool = False,
    ) -> tuple[date, datetime, datetime] | None:
        """
        Определяет дату смены и временное окно [day_start, cutoff) по end_time задания.
        Логика как в файле 3: смена дня D = [D 06:00, D+1 06:00).
        Если end_time < D 06:00 — задание относится к смене D-1.
        Возвращает (shift_date, day_start, cutoff) или None если вне периода.
        """
        end_dt = parse_dt(end_time_raw)
        if end_dt is None:
            return None
        cal_date = end_dt.date()
        cutoff   = datetime(cal_date.year, cal_date.month, cal_date.day,
                            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
        if end_dt < cutoff:
            cal_date = cal_date - timedelta(days=1)
            cutoff   = datetime(cal_date.year, cal_date.month, cal_date.day,
                                DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK) + timedelta(days=1)
        else:
            cutoff = cutoff + timedelta(days=1)
        day_start = cutoff - timedelta(days=1)
        if use_plan_today_bound:
            if not (period_start <= cal_date < TODAY):
                return None
        else:
            pe = period_end if period_end is not None else TODAY
            if not (period_start <= cal_date <= pe):
                return None
        if not _shift_overlaps_window(day_start, cutoff):
            return None
        return cal_date, day_start, cutoff

    with _stage("задания: индекс machine_tasks (prefetch/live)"):
        tasks_index = _get_tasks_index(tasks_window)

    for group_name, wt_ids in WORK_GROUPS:
        if group_name == HARVEST_GROUP_NAME:
            wt_ids = [wt for wt in wt_ids if wt not in VEG_HARVEST_WT_IDS]
        if group_name == PLOW_GROUP_NAME:
            # 49 в справочнике — «2.5 Вспашка по многолетним травам и залежи».
            # Колонка Cropwise «2.4 Вспашка» эту операцию не включает.
            wt_ids = [wt for wt in wt_ids if int(wt) != 49]
        print(f"  Загружаем {group_name}...")
        use_report3_coverage_logic = (len(wt_ids) == 1 and wt_ids[0] == 55)
        use_report6_spray_logic = group_name in (SPRAY_GROUP_NAME, VEG_SPRAY_GROUP_NAME)
        is_harvest_veg = _is_harvest_veg_group(group_name)
        is_harvest_grain = group_name == HARVEST_GROUP_NAME
        use_general_crop_map = group_name in GENERAL_CROP_GROUPS and not is_harvest_grain
        if use_report3_coverage_logic:
            active_crop_map = "field_to_crop_report3"
            active_firm_map = "field_to_firm_all"
            active_available_ids = "crop_field_ids_report3"
            group_period_start, group_period_end = _merged_period_for_group(
                group_name, date(TODAY.year, 3, 1)
            )
        elif is_harvest_veg:
            active_crop_map = "field_to_crop_veg"
            active_firm_map = "field_to_firm_veg"
            active_available_ids = "period_available_ids_general"
            group_period_start, group_period_end = _merged_period_for_group(
                group_name, date(TODAY.year, 3, 1)
            )
        elif is_harvest_grain:
            # Как 10_main_harvest: культура year=TODAY, фирма — полная карта.
            active_crop_map = "field_to_crop_harvest"
            active_firm_map = "field_to_firm_all"
            active_available_ids = "period_available_ids_general"
            group_period_start, group_period_end = _merged_period_for_group(
                group_name, HARVEST_SEASON_START
            )
        elif group_name == PLOW_GROUP_NAME:
            # Факт: сезоны из редактора. Культура — как у остальных: HistoryItem
            # строки, иначе field_to_crop_plow (year+1, не общая карта 2026+2027).
            active_crop_map = "field_to_crop_plow"
            active_firm_map = "field_to_firm_all"
            active_available_ids = "period_available_ids"
            group_period_start, group_period_end = _editor_period_for_group(
                group_name, START_DATE
            )
        elif group_name == DEEP_GROUP_NAME:
            # Как 9_main_fixed. Конец окна ниже поднимает fact_window_end до даты отчёта.
            active_crop_map = "field_to_crop_general"
            active_firm_map = "field_to_firm_all"
            active_available_ids = "period_available_ids_general"
            group_period_start, group_period_end = _editor_period_for_group(
                group_name, START_DATE
            )
        elif group_name == DISK_GROUP_NAME:
            # Дискование: оба сезона history (год отчёта + year+1), не sowing-мапа посева.
            active_crop_map = "field_to_crop_general"
            active_firm_map = "field_to_firm_all"
            active_available_ids = "period_available_ids_general"
            group_period_start, group_period_end = _editor_period_for_group(
                group_name, START_DATE
            )
        elif use_general_crop_map:
            active_crop_map = "field_to_crop_general"
            active_firm_map = "field_to_firm_general"
            active_available_ids = "period_available_ids_general"
            group_period_start, group_period_end = _merged_period_for_group(
                group_name, date(TODAY.year, 3, 1)
            )
        elif group_name == CULTIVATION_GROUP_NAME:
            # Как 8_main_fixed.load_field_filters: active history_items текущего года.
            active_crop_map = "field_to_crop_report3"
            active_firm_map = "field_to_firm_all"
            active_available_ids = "period_available_ids"
            group_period_start, group_period_end = _editor_period_for_group(
                group_name, START_DATE
            )
        else:
            active_crop_map = "field_to_crop"
            active_firm_map = "field_to_firm"
            active_available_ids = "period_available_ids"
            group_period_start, group_period_end = _merged_period_for_group(group_name, START_DATE)
        # Боронование считает covered_area, как отчёт 4. Старт не позже 01.01
        # года отчёта, но выбранные сезоны (например 2026+2027 с 01.07.2025)
        # окно не обрезают: иначе осенние гектары сезона выпадают из «Выполнено».
        use_report4_boron_logic = group_name == BORON_GROUP_NAME
        own_ranges = bool(resolve_group_season_config(group_name, TODAY).get("has_own_ranges"))
        if use_report4_boron_logic and not own_ranges:
            group_period_start = min(group_period_start, date(TODAY.year, 1, 1))
            group_period_end = TODAY
        # Один сезон в редакторе (календарь до 01.07) не должен обнулять факт
        # после этой даты. Сводки 1 и 6 режут machine_task.season и считают
        # смены до даты отчёта. Иначе «только 2026» в сентябре даёт нули.
        if not own_ranges:
            group_period_end = fact_window_end(group_period_end, TODAY)
        # Дискование — площадь как в сводках 1 и 7: без is_skip_crop, только поля с культурой в сезоне.
        use_report17_area_logic = group_name == DISK_GROUP_NAME
        # Запрашиваем с запасом: от START_DATE 06:00 до (TODAY+1) 06:00
        # Для боронования — от 01.01 года 00:00, как в отчёте 4.
        report_day_cutoff = datetime(
            TODAY.year, TODAY.month, TODAY.day,
            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
        )
        if CUSTOM_WINDOW_START is not None and CUSTOM_WINDOW_END is not None:
            season_start_str = CUSTOM_WINDOW_START.strftime("%Y-%m-%dT%H:%M:%S")
            cutoff_dt = CUSTOM_WINDOW_END
        elif use_report4_boron_logic:
            # Боронование: с 01.01 года отчёта, либо period_start из редактора.
            if resolve_group_season_config(group_name, TODAY).get("history_years"):
                season_start_str = f"{group_period_start.strftime('%Y-%m-%d')}T00:00:00"
            else:
                season_start_str = f"{TODAY.year}-01-01T00:00:00"
            cutoff_dt = report_day_cutoff + timedelta(days=1)
        elif use_report3_coverage_logic or use_general_crop_map:
            # 1.1 и прочие «general»: старт из редактора / FERT_PERIOD_START.
            season_start_str = f"{group_period_start.strftime('%Y-%m-%d')}T00:00:00"
            cutoff_dt = report_day_cutoff + timedelta(days=1)
        elif is_harvest_grain:
            # Как 10_main: сезон с 10.06, верхняя граница cutoff + буфер на поздний end_time.
            season_start_str = f"{group_period_start.strftime('%Y-%m-%d')}T00:00:00"
            cutoff_dt = report_day_cutoff + timedelta(hours=HARVEST_CUTOFF_BUFFER_HOURS)
        elif use_report6_spray_logic or is_harvest_veg:
            if resolve_group_season_config(group_name, TODAY).get("history_years"):
                season_start_str = f"{group_period_start.strftime('%Y-%m-%d')}T00:00:00"
            else:
                season_start_str = f"{TODAY.year}-01-01T00:00:00"
            cutoff_dt = report_day_cutoff + timedelta(days=1)
        else:
            season_start_dt = datetime(
                group_period_start.year,
                group_period_start.month,
                group_period_start.day,
                DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
            )
            season_start_str = season_start_dt.strftime("%Y-%m-%dT%H:%M:%S")
            cutoff_dt = report_day_cutoff + timedelta(days=1)
        print(
            f"    период группы: {group_period_start}–{group_period_end} "
            f"(tasks from {season_start_str[:10]})"
        )
        cutoff_str = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")
        all_tasks = _fetch_machine_tasks_parallel(
            wt_ids, season_start_str, cutoff_str, tasks_index=tasks_index,
        )
        if is_harvest_grain:
            all_tasks = _merge_silage_harvest_tasks(
                all_tasks, season_start_str, cutoff_str,
            )

        if use_report4_boron_logic:
            all_tasks = [t for t in all_tasks if (t.get("covered_area") or 0) > 0]
            print(f"    заданий с covered_area>0 (как в отчёте 4): {len(all_tasks)}")

        if not all_tasks:
            print(f"    Нет заданий, шаблонный лист")
            data[group_name] = _empty_group_block()
            continue

        # Вычисляем дату смены для каждого задания и фильтруем вне периода
        task_shift: dict[int, tuple[date, datetime, datetime]] = {}
        filtered_tasks: list[dict] = []
        no_shift_tasks: list[dict] = []   # задания без end_time — fallback на covered_area
        harvest_late = 0
        for t in all_tasks:
            result = _shift_date_and_window(
                t.get("end_time"),
                group_period_start,
                group_period_end,
                use_plan_today_bound=group_name in _TILLAGE_SHIFT_GROUPS,
            )
            if result is not None:
                task_shift[t["id"]] = result
                filtered_tasks.append(t)
                continue
            # Уборка: задания с end_time чуть после cutoff смены (закрытие в API
            # с задержкой) — как 10_main CUTOFF_BUFFER: относим к REPORT_SHIFT_DATE,
            # площадь режем hourly < REPORT_DAY_CUTOFF.
            if is_harvest_grain:
                end_dt = parse_dt(t.get("end_time"))
                if (
                    end_dt is not None
                    and REPORT_DAY_START <= end_dt < (
                        REPORT_DAY_CUTOFF + timedelta(hours=HARVEST_CUTOFF_BUFFER_HOURS)
                    )
                ):
                    task_shift[t["id"]] = (
                        REPORT_SHIFT_DATE, REPORT_DAY_START, REPORT_DAY_CUTOFF,
                    )
                    filtered_tasks.append(t)
                    harvest_late += 1
                    continue
            no_shift_tasks.append(t)   # не выбрасываем, берём covered_area напрямую
        all_tasks = filtered_tasks
        if is_harvest_grain and harvest_late:
            print(
                f"    заданий уборки с end_time в буфере +{HARVEST_CUTOFF_BUFFER_HOURS}ч: "
                f"{harvest_late}"
            )
        if is_harvest_grain:
            # Как 10_main: только задания с end_time (без fallback covered_area).
            no_shift_tasks = []

        if not all_tasks and not no_shift_tasks:
            print(f"    Нет заданий в периоде после фильтра по смене, шаблонный лист")
            data[group_name] = _empty_group_block()
            continue
        if not all_tasks:
            print(f"    Нет заданий с известной сменой, только {len(no_shift_tasks)} без end_time")

        print(f"    {len(all_tasks)} заданий за период")

        group_has_data = False  # Реальные непустые значения, пришедшие из API
        data[group_name] = {
            "crops":              {},
            "machines":           {f: defaultdict(lambda: {"own": set(), "hired": set()})
                                   for f in FIRM_ORDER},
            "area_by_firm_date":  {f: defaultdict(float) for f in FIRM_ORDER},
            "area_by_firm_total": {f: 0.0 for f in FIRM_ORDER},
        }

        all_tasks_for_mtfmi = all_tasks + no_shift_tasks
        task_machine: dict[int, int] = {t["id"]: t.get("machine_id") for t in all_tasks_for_mtfmi}
        task_ids  = [t["id"] for t in all_tasks_for_mtfmi]
        pending_groups.append({
            "group_name": group_name,
            "use_report3_coverage_logic": use_report3_coverage_logic,
            "use_report6_spray_logic": use_report6_spray_logic,
            "use_report17_area_logic": use_report17_area_logic,
            "is_harvest_veg": is_harvest_veg,
            "active_crop_map": active_crop_map,
            "active_firm_map": active_firm_map,
            "active_available_ids": active_available_ids,
            "all_tasks": all_tasks,
            "no_shift_tasks": no_shift_tasks,
            "task_shift": task_shift,
            "task_ids": task_ids,
            "task_machine": task_machine,
            "all_tasks_for_mtfmi": all_tasks_for_mtfmi,
            "period_start": group_period_start,
        })
        continue

    all_task_ids = [tid for g in pending_groups for tid in g["task_ids"]]

    # Культура через AgroOperation нужна не всем группам; выгружаем ОДИН раз
    # на объединение задач (раньше — отдельный asyncio.run на каждую группу).
    agro_task_ids: list[int] = []
    if use_crop_via_agro_op:
        agro_task_ids = sorted({
            int(tid)
            for g in pending_groups
            if g["group_name"] not in _CROP_VIA_OP_EXCLUDED_GROUPS
            for tid in g["task_ids"]
        })

    def _mtfmi() -> dict:
        with _stage("mtfmi: выгрузка"):
            return _build_global_mtfmi_map(all_task_ids, headers)

    def _agro_raw():
        if not agro_task_ids:
            return None
        with _stage("agro_operations: выгрузка связей"):
            return run_report_async(_fetch_task_agro_ops_raw(agro_task_ids, headers))

    from report_pipeline import run_parallel_sync_pair

    if PIPELINE:
        global_mtfmi, agro_raw = run_parallel_sync_pair(
            _mtfmi, _agro_raw, thread_prefix="r2-prep",
        )
    else:
        global_mtfmi = _mtfmi()
        agro_raw = _agro_raw()

    return {
        "data": data,
        "pending_groups": pending_groups,
        "global_mtfmi": global_mtfmi,
        "agro_raw": agro_raw,
        # ВНИМАНИЕ (унаследованное поведение): цикл обработки групп читает
        # is_harvest_grain / use_report4_boron_logic, которые в оригинале
        # «утекали» из ПОСЛЕДНЕЙ итерации цикла отбора (последняя группа
        # WORK_GROUPS), а не относились к обрабатываемой группе. Значения
        # сохранены как есть, чтобы цифры отчёта не изменились.
        "leaked_flags": (is_harvest_grain, use_report4_boron_logic),
    }


# Группы, для которых культура НЕ берётся через AgroOperation (только с поля).
_CROP_VIA_OP_EXCLUDED_GROUPS = frozenset({
    HARVEST_GROUP_NAME,
    PLOW_GROUP_NAME,
    DEEP_GROUP_NAME,
    DISK_GROUP_NAME,
    SEEDING_GROUP_NAME,
    VEG_SEEDING_GROUP_NAME,
})


# =============================================================================
# КЭШ ВКЛАДОВ ЗАДАНИЙ В СЕЗОННЫЕ СУММЫ (utils/shared/season_contrib_cache.py)
# =============================================================================
# SVODKI_SEASON_CACHE=off|shadow|on. Вклад задания хранится в sqlite-хранилище и пересчитывается только если
# изменилась версия задания (оно само, строки площади, связи/привязка агроопераций) или то, на что оно смотрит.
_SEASON_CACHE_CTX: dict = {}


def _season_cache_snapshot() -> None:
    """Версии заданий ДО чтения заданий и строк площади (см. svodki_calc_cache). Вызывается в начале фазы A."""
    _SEASON_CACHE_CTX.clear()
    try:
        from season_contrib_cache import cache_mode

        mode = cache_mode()
        if mode == "off":
            return
        from svodki_task_store import get_store

        store = get_store(BASE_URL)
        if store is None or not hasattr(store, "calc_snapshot"):
            print("  [season-cache] нет sqlite-хранилища (SVODKI_TASK_STORE_DIR) — полный пересчёт", flush=True)
            return
        _SEASON_CACHE_CTX.update(mode=mode, store=store, snap=store.calc_snapshot())
    except Exception as exc:  # кэш — оптимизация
        print(f"  [season-cache] отключён: {exc!r}", flush=True)
        _SEASON_CACHE_CTX.clear()


def _make_stamp_parts(
    *, group_name, task_machine, task_shift, mtfmi_map, task_crop_via_operation, task_field_crop,
    mtfmi_row_crop, machine_group, active_crop_map, active_firm_map, active_available_ids, field_to_firm_all,
):
    """Всё, на что вклад задания смотрит СНАРУЖИ самого задания (версию задания хранилище ведёт само):
    смена, машина и её группа, культура строк/задания/агрооперации, карты полей (культура, фирма, доступность).
    Берётся с запасом (лишнее приводит только к лишнему пересчёту, забытое — к неверной цифре),
    поэтому в режиме shadow эти данные сверяются с полным пересчётом."""
    from mtfmi_batch_fetch import crop_of_item

    by_task_field: dict[int, list] = defaultdict(list)
    for (tid_, fid_), crop_ in task_field_crop.items():
        by_task_field[int(tid_)].append([int(fid_), crop_])
    for v in by_task_field.values():
        v.sort()
    is_fert = group_name == FERT_GROUP_NAME
    is_disk = group_name == DISK_GROUP_NAME

    def parts(task: dict):
        tid = task["id"]
        mid = task_machine.get(tid)
        rows = []
        for item in mtfmi_map.get(tid, []):
            rid = item.get("id")
            fid = item.get("field_id")
            try:
                f = int(fid) if fid else None
            except (TypeError, ValueError):
                f = None
            rows.append([
                rid, f,
                mtfmi_row_crop.get(int(rid), "") if rid is not None else "",
                crop_of_item(item, "") if is_fert else None,
                (f in active_available_ids) if f is not None else None,
                active_crop_map.get(f) if f is not None else None,
                active_firm_map.get(f) if f is not None else None,
                field_to_firm_all.get(f) if f is not None else None,
                _FIELD_CROP_BY_YEAR.get(f) if (is_disk and f is not None) else None,
            ])
        return [
            mid, (machine_group.get(int(mid), 0) if mid is not None else None),
            task_shift.get(tid), task_crop_via_operation.get(tid), by_task_field.get(int(tid), []), rows,
        ]

    return parts


def _accumulate_group_with_cache(
    *, group_name, all_tasks, no_shift_tasks, group_acc, acc_run, capture_on, ckey_parts, stamp_parts,
) -> bool:
    """Заполнить аккумуляторы группы: полным пересчётом (по умолчанию) или через кэш вкладов заданий."""
    ctx = _SEASON_CACHE_CTX
    mode = ctx.get("mode", "off")
    if mode == "off" or CUSTOM_WINDOW_START is not None or not ctx.get("store"):
        return acc_run(all_tasks, no_shift_tasks, group_acc)
    import season_contrib_cache as sc
    from svodki_calc_cache import calc_key

    store, snap = ctx["store"], ctx["snap"]
    plan_dir = Path(__file__).resolve().parent
    ckey = calc_key(
        "r2-contrib", 1,
        sc.code_hash(Path(__file__).resolve(), plan_dir / "utils" / "shared" / "task_area_shared.py",
                     plan_dir / "utils" / "shared" / "season_contrib_cache.py"),
        FIRM_ORDER, ckey_parts,
    )
    hot_cut = datetime(TODAY.year, TODAY.month, TODAY.day, 6, 0, 0, tzinfo=TZ_MSK) - timedelta(days=1 + sc.hot_days())

    def is_hot(task: dict) -> bool:
        dt = parse_dt(task.get("end_time"))
        return dt is None or dt >= hot_cut

    kwargs = dict(
        store=store, snapshot=snap, ckey=ckey, label=group_name, tasks=all_tasks, no_shift=no_shift_tasks,
        new_acc_fn=lambda: sc.new_acc(FIRM_ORDER), accumulate=acc_run, stamp_parts=stamp_parts,
        is_hot=is_hot, audit=sc.audit_fraction(mode), log="    ",
    )
    if mode == "shadow":
        shadow_acc = sc.new_acc(FIRM_ORDER)
        capture_on[0] = False  # контрольный проход не должен дублировать захват фактов для проверки
        try:
            sc.cached_pass(target=shadow_acc, **{**kwargs, "audit": 0.0})
        except Exception as exc:
            print(f"    [season-cache shadow] {group_name}: проход через кэш не удался ({exc!r})", flush=True)
            shadow_acc = None
        finally:
            capture_on[0] = True
        has = acc_run(all_tasks, no_shift_tasks, group_acc)  # результат — полный пересчёт, как раньше
        if shadow_acc is not None:
            n, bad = sc.diff_flat(sc.flatten(group_acc), sc.flatten(shadow_acc))
            if n:
                print(f"    [season-cache shadow] {group_name}: РАСХОЖДЕНИЙ {n}: {bad}", flush=True)
            else:
                print(f"    [season-cache shadow] {group_name}: кэш == полный пересчёт", flush=True)
        return has
    try:
        has, _st = sc.cached_pass(target=group_acc, **kwargs)
        return has
    except Exception as exc:
        print(f"    [season-cache] {group_name}: ошибка кэша ({exc!r}) — полный пересчёт", flush=True)
        for key in group_acc:  # частично залитое обнуляем (в on-режиме заливка происходит в самом конце)
            group_acc[key].clear()
        fresh = sc.new_acc(FIRM_ORDER)
        for key in group_acc:
            group_acc[key].update(fresh[key])
        return acc_run(all_tasks, no_shift_tasks, group_acc)


def collect_work_data(
    field_to_crop: dict[int, str],
    field_to_crop_general: dict[int, str],
    field_to_crop_harvest: dict[int, str],
    field_to_crop_plow: dict[int, str],
    field_to_crop_veg: dict[int, str],
    field_to_crop_report3: dict[int, str],
    field_to_firm: dict[int, str],
    field_to_firm_general: dict[int, str],
    field_to_firm_veg: dict[int, str],
    field_to_firm_all: dict[int, str],
    crop_field_ids_report3: set[int],
    period_available_ids: set[int],
    period_available_ids_general: set[int],
    field_area:    dict[int, float],
    machine_group: dict[int, int],
    headers:       dict,
    history_item_to_crop: dict[int, str] | None = None,
    history_item_to_year: dict[int, int] | None = None,
    prepared: dict | None = None,
) -> dict:
    """Фаза B: всё, что зависит от справочников. prepared — результат
    _prepare_group_selection (из фона); если None, подготовка выполняется здесь."""
    if prepared is None:
        prepared = _prepare_group_selection(headers, refresh=True)
    data: dict[str, dict] = prepared["data"]
    pending_groups: list[dict] = prepared["pending_groups"]
    global_mtfmi = prepared["global_mtfmi"]
    agro_raw = prepared["agro_raw"]
    is_harvest_grain, use_report4_boron_logic = prepared["leaked_flags"]
    history_item_to_crop = history_item_to_crop or {}
    history_item_to_year = history_item_to_year or {}
    use_crop_via_agro_op = _parse_bool_env("SVODKI_CROP_VIA_AGRO_OPERATION", True)

    DAY_START_HOUR = 6  # смена начинается в 06:00 МСК
    REPORT_DAY_CUTOFF = datetime(
        TODAY.year, TODAY.month, TODAY.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    REPORT_DAY_START = REPORT_DAY_CUTOFF - timedelta(days=1)
    REPORT_SHIFT_DATE = TODAY - timedelta(days=1)

    _refs = {
        "field_to_crop": field_to_crop,
        "field_to_crop_general": field_to_crop_general,
        "field_to_crop_harvest": field_to_crop_harvest,
        "field_to_crop_plow": field_to_crop_plow,
        "field_to_crop_veg": field_to_crop_veg,
        "field_to_crop_report3": field_to_crop_report3,
        "field_to_firm": field_to_firm,
        "field_to_firm_general": field_to_firm_general,
        "field_to_firm_veg": field_to_firm_veg,
        "field_to_firm_all": field_to_firm_all,
        "crop_field_ids_report3": crop_field_ids_report3,
        "period_available_ids": period_available_ids,
        "period_available_ids_general": period_available_ids_general,
    }

    with _stage("mtfmi: культура строк площади"):
        mtfmi_row_crop = _load_mtfmi_row_crops(
            global_mtfmi, history_item_to_crop, headers, history_item_to_year,
        )
    if _HISTORY_FIELD_ONLY:
        print("  факт: культура строки только из history_item_id, без карты поля")

    try:
        from season_mismatch_log import log_row_season_mismatches

        logged_tasks = [
            task
            for group in pending_groups
            for task in group["all_tasks_for_mtfmi"]
        ]
        log_row_season_mismatches(
            logged_tasks,
            global_mtfmi,
            history_item_to_year,
            history_item_to_crop,
        )
    except Exception as exc:
        print(f"  сезоны культуры: {exc}", flush=True)

    for g in pending_groups:
        group_name = g["group_name"]
        use_report3_coverage_logic = g["use_report3_coverage_logic"]
        use_report6_spray_logic = g["use_report6_spray_logic"]
        use_report17_area_logic = g["use_report17_area_logic"]
        is_harvest_veg = g["is_harvest_veg"]
        active_crop_map = _refs[g["active_crop_map"]]
        active_firm_map = _refs[g["active_firm_map"]]
        active_available_ids = _refs[g["active_available_ids"]]
        all_tasks = g["all_tasks"]
        no_shift_tasks = g["no_shift_tasks"]
        task_shift = g["task_shift"]
        task_ids = g["task_ids"]
        task_machine = g["task_machine"]
        all_tasks_for_mtfmi = g["all_tasks_for_mtfmi"]
        harvest_period_start = g.get("period_start") or date(TODAY.year, 1, 1)
        mtfmi_map = {tid: global_mtfmi.get(tid, []) for tid in task_ids}

        task_season: dict[int, str] = {
            t["id"]: str(t.get("season") or t.get("year") or "").strip()
            for t in all_tasks_for_mtfmi
        }
        task_crop_via_operation: dict[int, str] = {}
        task_field_crop: dict[tuple[int, int], str] = {}
        # Уборка 5.0 / вспашка / дискование / посев — культура только с поля (без AgroOperation).
        # Диск: мапа general (2026+2027), иначе AgroOperation часто тянет только season year+1.
        if (
            use_crop_via_agro_op
            and history_item_to_crop
            and group_name not in _CROP_VIA_OP_EXCLUDED_GROUPS
        ):
            task_crop_via_operation, task_field_crop = _resolve_task_crops(
                agro_raw, history_item_to_crop, only_task_ids=task_ids,
            )
            print(
                f"    культура через AgroOperation определена для "
                f"{len(task_crop_via_operation)} из {len(task_ids)} заданий"
            )

        _winter_season_only = (
            SPRAY_WINTER_CROPS_SEASON_ONLY if group_name == SPRAY_GROUP_NAME else frozenset()
        )
        _season_task_seasons = _fact_seasons_for_group(group_name)
        _winter_keep = (
            winter_spray_task_seasons(_season_task_seasons, TODAY)
            if _winter_season_only
            else set()
        )

        if _season_task_seasons is not None:
            from collections import Counter
            _season_task_counts = Counter(
                task_season.get(t["id"], "<пусто>") or "<пусто>" for t in all_tasks
            )
            _need = "+".join(sorted(_season_task_seasons))
            print(
                f"    [{group_name}] сезон заданий (нужен {_need}): "
                f"{dict(_season_task_counts)}"
            )

        if _winter_season_only and os.environ.get("SVODKI_DEBUG_WINTER_SEASON"):
            from collections import Counter

            task_by_id_dbg = {t["id"]: t for t in all_tasks_for_mtfmi}
            _winter_task_ids = [
                tid_ for tid_, crop_ in task_crop_via_operation.items()
                if crop_ in _winter_season_only
            ]
            print(
                f"    [диагностика озимых/{group_name}] заданий с культурой рапс/пшеница "
                f"через AgroOperation: {len(_winter_task_ids)}"
            )
            season_counter = Counter(task_season.get(tid_, "<пусто>") for tid_ in _winter_task_ids)
            print(f"    [диагностика озимых] распределение task_season: {dict(season_counter)}")

        def _apply_winter_season_filter(crop_: str, tid_: int, default: str) -> str:
            if (
                crop_
                and crop_ in _winter_season_only
                and _winter_keep
                and task_season.get(tid_) not in _winter_keep
            ):
                return default
            return crop_

        def _history_row_crop(item: dict | None) -> str:
            """Культура строки только из history_item_id. Пустая ссылка — пустая культура."""
            if item is None:
                return ""
            raw_id = item.get("id")
            if raw_id is not None:
                named = mtfmi_row_crop.get(int(raw_id), "")
                if named:
                    return named
            if item.get("history_item_id") is None:
                return ""
            from mtfmi_batch_fetch import crop_of_item

            return str(crop_of_item(item, "") or "")

        def _crop_for_task_field(
            tid_: int,
            fid_i_: int,
            default: str = "",
            item: dict | None = None,
        ) -> str:
            if _HISTORY_FIELD_ONLY:
                named = _history_row_crop(item)
                if group_name == DISK_GROUP_NAME and (not named or named == "Зябь"):
                    return ""
                if is_harvest_veg:
                    return _map_harvest_veg_crop(named) or ""
                return named
            # Сравнение со старым путём: карта поля, агрооперация, сезон задания.
            if group_name == FERT_GROUP_NAME:
                from mtfmi_batch_fetch import crop_of_item

                fallback = active_crop_map.get(fid_i_, default) or default
                if item is not None:
                    named = crop_of_item(item, fallback)
                    if named:
                        return _apply_winter_season_filter(named, tid_, default)
                if fallback:
                    return _apply_winter_season_filter(fallback, tid_, default)
                return default
            if item is not None:
                raw_id = item.get("id")
                if raw_id is not None:
                    named = mtfmi_row_crop.get(int(raw_id), "")
                    if named and not is_skip_crop(named):
                        return named
            if group_name == DISK_GROUP_NAME:
                try:
                    season_year = int(task_season.get(tid_) or 0) or None
                except (TypeError, ValueError):
                    season_year = None
                named = crop_for_disk_field(
                    _FIELD_CROP_BY_YEAR.get(int(fid_i_), {}), season_year,
                )
                if named:
                    return named
                fallback = active_crop_map.get(fid_i_, default)
                return "" if fallback == "Зябь" else (fallback or default)
            # Строка без культуры: культура поля своей карты группы.
            # Вспашка — field_to_crop_plow (только history year+1).
            if group_name in {
                HARVEST_GROUP_NAME,
                PLOW_GROUP_NAME,
                DEEP_GROUP_NAME,
                DISK_GROUP_NAME,
                SEEDING_GROUP_NAME,
                VEG_SEEDING_GROUP_NAME,
            }:
                return active_crop_map.get(fid_i_, default)
            # Сначала культура агрооперации этого поля: на одном задании
            # пшеница, соя и картофель не должны схлопываться в одну культуру.
            field_crop = task_field_crop.get((int(tid_), int(fid_i_)))
            if field_crop:
                return _apply_winter_season_filter(field_crop, tid_, default)
            crop = task_crop_via_operation.get(tid_)
            if crop:
                return _apply_winter_season_filter(crop, tid_, default)
            map_crop = active_crop_map.get(fid_i_, default)
            return _apply_winter_season_filter(map_crop, tid_, default)

        agg: dict[str, dict[str, dict[date, float]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(float)))
        firm_crop_fields: dict[str, dict[str, set]] = defaultdict(
            lambda: defaultdict(set))
        machine_day_area: dict[str, dict[date, dict[int, float]]] = {
            f: defaultdict(lambda: defaultdict(float)) for f in FIRM_ORDER
        }
        machine_day_group: dict[str, dict[date, dict[int, int]]] = {
            f: defaultdict(dict) for f in FIRM_ORDER
        }
        field_day_area: dict[str, dict[date, dict[int, float]]] = {
            f: defaultdict(lambda: defaultdict(float)) for f in FIRM_ORDER
        }
        machine_day_items: dict[str, dict[date, dict[int | None, list[tuple[int, str, float]]]]] = {
            f: defaultdict(lambda: defaultdict(list)) for f in FIRM_ORDER
        }
        count_threshold = _group_count_threshold(group_name)

        group_has_data = False
        _capture_on = [True]  # кэш вкладов на время контрольного прохода (shadow) отключает захват фактов
        _cap = None
        if group_name != HARVEST_GROUP_NAME:
            try:
                from proverka.task_facts import bind_capture

                _cap = bind_capture()
            except Exception:
                _cap = None

        def _note_shift(firm_s, mid_s, tid_s, fid_s, crop_s, ha_s, task_s, t_date_s) -> None:
            if _cap is None or not _capture_on[0] or t_date_s != REPORT_SHIFT_DATE or ha_s <= 0:
                return
            _cap[1](
                (group_name, firm_s, mid_s),
                group=group_name,
                firm=firm_s,
                crop=str(crop_s or ""),
                field_id=int(fid_s) if fid_s else None,
                task_id=int(tid_s) if tid_s is not None else None,
                machine_id=int(mid_s) if mid_s is not None else None,
                ha=float(ha_s),
                season=str(task_s.get("season") or ""),
            )

        def _accumulate(
            tasks_a, tasks_b, machine_day_area, machine_day_group, field_day_area,
            machine_day_items, agg, firm_crop_fields,
        ) -> bool:
            """Исходные циклы сводки: вклады заданий tasks_a (со сменой) и tasks_b (без смены) в аккумуляторы.

            Вынесено в функцию без изменения расчёта: полный пересчёт зовёт её для всех заданий группы, кэш вкладов
            (season_contrib_cache) — для одного задания. Возвращает «в группе есть данные».
            """
            group_has_data = False
            def _note_harvest_portion(
                firm_s, mid_s, tid_s, fid_s, crop_s, crop_agg_s, ha_s, task_s, shift_date,
            ) -> None:
                """Площадь уборки — в смену часа работы, не в день закрытия задания."""
                machine_day_area[firm_s][shift_date][mid_s] += float(ha_s)
                _note_shift(firm_s, mid_s, tid_s, fid_s, crop_agg_s or crop_s, ha_s, task_s, shift_date)
                if mid_s is not None:
                    machine_day_group[firm_s][shift_date][mid_s] = machine_group.get(mid_s, 0)
                if crop_agg_s:
                    machine_day_items[firm_s][shift_date][mid_s].append(
                        (fid_s, crop_agg_s, float(ha_s))
                    )
                elif is_harvest_grain and crop_s in HARVEST_VEG_CROPS:
                    agg[firm_s][crop_s][shift_date] += float(ha_s)
                    firm_crop_fields[firm_s][crop_s].add(fid_s)

            for task in tasks_a:
                tid  = task["id"]
                mid  = task_machine.get(tid)
                # Как 10_main_harvest: задания без machine_id не учитываем.
                if group_name == HARVEST_GROUP_NAME and mid is None:
                    continue
                if group_name == HARVEST_GROUP_NAME and not _is_harvest_area_machine(mid, machine_group):
                    continue
                # Сезоны группы: редактор планов, иначе history_years по умолчанию (оба сезона).
                # Только текущий сезон (season_year_for_date) используется для культуры поля, не для факта.
                if _season_task_seasons is not None and task_season.get(tid) not in _season_task_seasons:
                    continue
                shift_info = task_shift.get(tid)
                if not shift_info:
                    continue
                t_date, day_start, cutoff = shift_info

                if use_report6_spray_logic:
                    from task_area_shared import task_cov_by_firm, task_crop_cov_for_firm

                    items = mtfmi_map.get(tid, [])

                    def _spray_row_crop(item: dict) -> str:
                        fid = item.get("field_id")
                        try:
                            fid_i = int(fid) if fid is not None else 0
                        except (TypeError, ValueError):
                            fid_i = 0
                        return _crop_for_task_field(tid, fid_i, "Не определена", item)

                    def _spray_row_ok(item: dict) -> bool:
                        crop_s = _spray_row_crop(item)
                        if group_name == SPRAY_GROUP_NAME:
                            # Как сводка 1: пустая культура строки в сутки не входит.
                            return bool(crop_s) and crop_s not in VEG_SPRAY_CROPS
                        if group_name == VEG_SPRAY_GROUP_NAME:
                            return bool(crop_s) and crop_s in VEG_SPRAY_CROPS
                        return True

                    def _spray_field_ok(fid_i: int) -> bool:
                        # Как сводка 1: любое поле, фирма режется отдельно.
                        # Список «доступных» полей сезона отбрасывал часть гектаров.
                        del fid_i
                        return True

                    def _spray_firm_for_field(fid_i: int) -> str:
                        return _resolve_firm_for_fact(
                            fid_i, group_name, active_crop_map,
                            active_firm_map, field_to_firm_all, active_available_ids,
                        ) or ""

                    firm_cov = task_cov_by_firm(
                        task,
                        items,
                        cutoff=cutoff,
                        window_start=day_start,
                        field_ok=_spray_field_ok,
                        firm_for_field=_spray_firm_for_field,
                        parse_dt=parse_dt,
                        row_allowed=_spray_row_ok,
                    )
                    if not firm_cov:
                        continue
                    group_has_data = True
                    for firm_s, cov_for_firm in firm_cov.items():
                        crop_cov = task_crop_cov_for_firm(
                            task,
                            items,
                            firm_s,
                            cov_for_firm,
                            cutoff=cutoff,
                            window_start=day_start,
                            field_ok=_spray_field_ok,
                            firm_for_field=_spray_firm_for_field,
                            crop_for_field=lambda fid: _crop_for_task_field(tid, fid, "Не определена"),
                            parse_dt=parse_dt,
                            row_allowed=_spray_row_ok,
                            crop_for_item=_spray_row_crop,
                        )
                        for crop_s, crop_area in crop_cov.items():
                            if crop_area <= 0:
                                continue
                            rep_fid = 0
                            for item in items:
                                fid = item.get("field_id")
                                if not fid:
                                    continue
                                fid_i = int(fid)
                                if (
                                    _spray_field_ok(fid_i)
                                    and _spray_row_ok(item)
                                    and _spray_firm_for_field(fid_i) == firm_s
                                    and _spray_row_crop(item) == crop_s
                                ):
                                    rep_fid = fid_i
                                    break
                            machine_day_items[firm_s][t_date][mid].append(
                                (rep_fid, crop_s, float(crop_area))
                            )
                        machine_day_area[firm_s][t_date][mid] += cov_for_firm
                        machine_day_group[firm_s][t_date][mid] = machine_group.get(mid, 0)
                        if len(crop_cov) == 1:
                            only_crop = next(iter(crop_cov))
                            _note_shift(firm_s, mid, tid, 0, only_crop, cov_for_firm, task, t_date)
                        else:
                            noted = 0.0
                            for crop_s, crop_area in crop_cov.items():
                                _note_shift(firm_s, mid, tid, 0, crop_s, crop_area, task, t_date)
                                noted += float(crop_area or 0)
                            _note_shift(firm_s, mid, tid, 0, "", cov_for_firm - noted, task, t_date)
                    continue

                hourly_cutoff = cutoff

                for item in mtfmi_map.get(tid, []):
                    fid = item.get("field_id")
                    if not fid:
                        continue

                    fid_i = int(fid)
                    firm = _resolve_firm_for_fact(
                        fid_i, group_name, active_crop_map,
                        active_firm_map, field_to_firm_all, active_available_ids,
                        use_report3_field_filter=use_report3_coverage_logic,
                    )
                    if not firm:
                        continue
                    crop_raw = _crop_for_task_field(tid, fid_i, item=item)
                    from mtfmi_batch_fetch import UNDEFINED_CROP

                    if is_harvest_veg:
                        crop = crop_raw or UNDEFINED_CROP
                    else:
                        crop = crop_raw or UNDEFINED_CROP
                        # is_harvest_grain в фазе B утек из последней группы и равен False,
                        # поэтому скашивание кукурузы оставалось «на зерно».
                        # Скашивание не-кукурузы в 5.0 не входит — как в 10_main_harvest.
                        if group_name == HARVEST_GROUP_NAME:
                            _wt_raw = task.get("work_type_id")
                            crop = _as_silage_if_corn_mowing(_wt_raw, crop)
                            try:
                                _wt_i = int(_wt_raw) if _wt_raw is not None else None
                            except (TypeError, ValueError):
                                _wt_i = None
                            if _wt_i in SILAGE_CORN_MOWING_WT_IDS and crop != CORN_SILAGE_CROP:
                                continue
                        if group_name == DISK_GROUP_NAME and not crop_raw:
                            crop = ""
                        if group_name == SPRAY_GROUP_NAME and crop in VEG_SPRAY_CROPS:
                            continue
                        if group_name == VEG_SPRAY_GROUP_NAME and crop not in VEG_SPRAY_CROPS:
                            continue

                    # Редактор планов: культура скрыта / «не считать» — гектары не входят в итоги.
                    # Дискование — как сводка 1: такие гектары остаются в суточной площади
                    # (строка «Иные культуры»), иначе сутки занижаются.
                    if (
                        crop
                        and group_name != DISK_GROUP_NAME
                        and is_crop_user_excluded(group_name, firm, crop)
                    ):
                        continue

                    if group_name in (PLOW_GROUP_NAME, DISK_GROUP_NAME):
                        # Cropwise считает все гектары операции, в том числе культуры
                        # вне плана. Одной строкой они в «Иные культуры» и отдельно
                        # не считаются. По культурам — своя строка, не внутри «Иных».
                        if crop:
                            coded = crop_code_for_report(group_name, crop)
                            if (
                                coded is None
                                and other_crops_as_rows(group_name)
                                and not is_crop_user_excluded(group_name, firm, crop)
                            ):
                                pass
                            else:
                                crop = coded or OTHER_CROP_CODE
                        else:
                            crop = OTHER_CROP_CODE
                    elif crop:
                        coded = fact_crop_code(group_name, firm, crop)
                        if coded is None:
                            continue
                        crop = coded

                    crop_for_agg = (
                        crop if _harvest_grain_field_allowed(group_name, crop) else None
                    )

                    if is_harvest_veg:
                        from task_area_shared import harvest_area_booked_on_close

                        booked = harvest_area_booked_on_close(
                            item,
                            day_start=day_start,
                            cutoff=cutoff,
                            task_end=parse_dt(task.get("end_time")),
                            parse_dt=parse_dt,
                        )
                        if (
                            booked > 0
                            and harvest_period_start <= t_date < TODAY
                        ):
                            _note_harvest_portion(
                                firm, mid, tid, fid_i, crop, crop_for_agg,
                                float(booked), task, t_date,
                            )
                        continue

                    if use_report4_boron_logic:
                        # Как 4_main_fixed (сезонный проход): covered_area, не hourly.
                        cov = float(item.get("covered_area") or 0)
                    elif CUSTOM_WINDOW_START is not None and CUSTOM_WINDOW_END is not None:
                        cov = _cov_from_hourly(item, day_start, cutoff)
                    else:
                        hourly = item.get("covered_area_hourly") or []
                        cov = 0.0
                        for entry in hourly:
                            h = parse_dt(entry[0])
                            if h and h < hourly_cutoff:
                                cov += entry[1] or 0
                        if not hourly and not use_report3_coverage_logic:
                            cov = item.get("covered_area") or 0

                    if cov <= 0:
                        continue

                    machine_day_area[firm][t_date][mid] += float(cov)
                    _note_shift(firm, mid, tid, fid_i, crop_for_agg or crop, cov, task, t_date)
                    if mid is not None:
                        machine_day_group[firm][t_date][mid] = machine_group.get(mid, 0)
                    if crop_for_agg:
                        machine_day_items[firm][t_date][mid].append(
                            (fid_i, crop_for_agg, float(cov))
                        )
                    elif (
                        is_harvest_grain
                        and crop in HARVEST_VEG_CROPS
                    ):
                        # Cropwise UI: картофель в колонке 5.0, не только 5.4.
                        agg[firm][crop][t_date] += float(cov)
                        firm_crop_fields[firm][crop].add(fid_i)
                    if use_report17_area_logic or group_name == PLOW_GROUP_NAME:
                        # Вспашка: как сводка 1, часы до конца смены закрытия задания.
                        # Сводка по фирмам берётся из полей, а строки культур — из agg.
                        # Для вспашки flush не вызывается, поэтому культуру пишем здесь.
                        field_day_area[firm][t_date][int(fid)] += float(cov)
                        if group_name == PLOW_GROUP_NAME and crop_for_agg:
                            agg[firm][crop_for_agg][t_date] += float(cov)
                            firm_crop_fields[firm][crop_for_agg].add(fid_i)

            for task in tasks_b:
                tid = task["id"]
                if use_report3_coverage_logic or use_report6_spray_logic:
                    continue  # для 1.1 без смены не считаем; опрыскивание — только по смене (как в сводке 6)
                if group_name == HARVEST_GROUP_NAME and task.get("machine_id") is None:
                    continue
                if _season_task_seasons is not None and task_season.get(tid) not in _season_task_seasons:
                    continue
                for item in mtfmi_map.get(tid, []):
                    fid = item.get("field_id")
                    if not fid:
                        continue
                    fid_i = int(fid)
                    firm = _resolve_firm_for_fact(
                        fid_i, group_name, active_crop_map,
                        active_firm_map, field_to_firm_all, active_available_ids,
                        use_report3_field_filter=use_report3_coverage_logic,
                    )
                    if not firm:
                        continue
                    crop_raw = _crop_for_task_field(tid, fid_i, item=item)
                    from mtfmi_batch_fetch import UNDEFINED_CROP

                    if is_harvest_veg:
                        crop = crop_raw or UNDEFINED_CROP
                    else:
                        crop = crop_raw or UNDEFINED_CROP
                        # is_harvest_grain в фазе B утек из последней группы и равен False,
                        # поэтому скашивание кукурузы оставалось «на зерно».
                        # Скашивание не-кукурузы в 5.0 не входит — как в 10_main_harvest.
                        if group_name == HARVEST_GROUP_NAME:
                            _wt_raw = task.get("work_type_id")
                            crop = _as_silage_if_corn_mowing(_wt_raw, crop)
                            try:
                                _wt_i = int(_wt_raw) if _wt_raw is not None else None
                            except (TypeError, ValueError):
                                _wt_i = None
                            if _wt_i in SILAGE_CORN_MOWING_WT_IDS and crop != CORN_SILAGE_CROP:
                                continue
                        if group_name == DISK_GROUP_NAME and not crop_raw:
                            crop = ""
                        if group_name == SPRAY_GROUP_NAME and crop in VEG_SPRAY_CROPS:
                            continue
                        if group_name == VEG_SPRAY_GROUP_NAME and crop not in VEG_SPRAY_CROPS:
                            continue
                    if (
                        crop
                        and group_name != DISK_GROUP_NAME
                        and is_crop_user_excluded(group_name, firm, crop)
                    ):
                        continue
                    if group_name in (PLOW_GROUP_NAME, DISK_GROUP_NAME):
                        if crop:
                            coded = crop_code_for_report(group_name, crop)
                            if (
                                coded is None
                                and other_crops_as_rows(group_name)
                                and not is_crop_user_excluded(group_name, firm, crop)
                            ):
                                pass
                            else:
                                crop = coded or OTHER_CROP_CODE
                        else:
                            crop = OTHER_CROP_CODE
                    elif crop:
                        coded = fact_crop_code(group_name, firm, crop)
                        if coded is None:
                            continue
                        crop = coded
                    cov = float(item.get("covered_area") or 0)
                    mid = task_machine.get(tid)
                    if cov <= 0:
                        continue
                    crop_for_agg = (
                        crop if _harvest_grain_field_allowed(group_name, crop) else None
                    )
                    t_date = TODAY
                    if group_name == PLOW_GROUP_NAME:
                        field_day_area[firm][t_date][fid_i] += float(cov)
                        agg[firm][crop][t_date] += float(cov)
                        firm_crop_fields[firm][crop].add(fid_i)
                        group_has_data = True
                        if mid is not None:
                            machine_day_area[firm][t_date][mid] += float(cov)
                            machine_day_group[firm][t_date][mid] = machine_group.get(mid, 0)
                        continue
                    machine_day_area[firm][t_date][mid] += float(cov)
                    if mid is not None:
                        machine_day_group[firm][t_date][mid] = machine_group.get(mid, 0)
                    if crop_for_agg:
                        machine_day_items[firm][t_date][mid].append(
                            (fid_i, crop_for_agg, float(cov))
                        )
                    if use_report17_area_logic:
                        field_day_area[firm][t_date][int(fid)] += float(cov)
            return group_has_data

        _group_acc = {
            "mda": machine_day_area, "mdg": machine_day_group, "fda": field_day_area,
            "mdi": machine_day_items, "agg": agg, "fcf": firm_crop_fields,
        }

        def _acc_run(tasks_a, tasks_b, acc) -> bool:
            return _accumulate(
                tasks_a, tasks_b, acc["mda"], acc["mdg"], acc["fda"], acc["mdi"], acc["agg"], acc["fcf"],
            )

        group_has_data = _accumulate_group_with_cache(
            group_name=group_name,
            all_tasks=all_tasks,
            no_shift_tasks=no_shift_tasks,
            group_acc=_group_acc,
            acc_run=_acc_run,
            capture_on=_capture_on,
            ckey_parts=[
                group_name,
                [use_report3_coverage_logic, use_report6_spray_logic, use_report17_area_logic,
                 is_harvest_veg, is_harvest_grain, use_report4_boron_logic],
                harvest_period_start.isoformat(),
                sorted(_season_task_seasons or []),
                sorted(_winter_keep),
                sorted(_winter_season_only),
                crop_filter_fingerprint(),  # правки «Редактора планов» (скрытие, «не считать», ранг)
                bool(use_crop_via_agro_op and history_item_to_crop
                     and group_name not in _CROP_VIA_OP_EXCLUDED_GROUPS),
                bool(_HISTORY_FIELD_ONLY),
                "plow-close-shift",
                "harvest-area-tractors",
            ],
            stamp_parts=_make_stamp_parts(
                group_name=group_name, task_machine=task_machine, task_shift=task_shift,
                mtfmi_map=mtfmi_map, task_crop_via_operation=task_crop_via_operation,
                task_field_crop=task_field_crop, mtfmi_row_crop=mtfmi_row_crop,
                machine_group=machine_group, active_crop_map=active_crop_map,
                active_firm_map=active_firm_map, active_available_ids=active_available_ids,
                field_to_firm_all=field_to_firm_all,
            ),
        )

        if group_name != PLOW_GROUP_NAME:
            group_has_data = _flush_area_from_machines(
                data,
                group_name,
                machine_day_area,
                machine_day_items,
                firm_crop_fields,
                agg,
            ) or group_has_data

        _machine_min_ha = _machine_count_min_ha(group_name)
        for firm in FIRM_ORDER:
            for t_date, by_mid in machine_day_area[firm].items():
                for mid, total_cov in by_mid.items():
                    tc = float(total_cov)
                    if _machine_min_ha > 0 and round(tc, 2) < _machine_min_ha:
                        continue
                    grp = machine_day_group[firm][t_date].get(mid, machine_group.get(mid, 0))
                    key = "hired" if grp == HIRED_GROUP_ID else "own"
                    data[group_name]["machines"][firm][t_date][key].add(mid)

        if group_name == PLOW_GROUP_NAME:
            # Суточная площадь — по полям (как сводки 1 и 9); техника >= 10 га — отдельно.
            data[group_name]["area_by_firm_date"] = {
                firm: defaultdict(
                    float,
                    {
                        t_date: sum(float(v) for v in by_fid.values())
                        for t_date, by_fid in field_day_area[firm].items()
                        if sum(by_fid.values()) > 0
                    },
                )
                for firm in FIRM_ORDER
            }
            data[group_name]["area_by_firm_total"] = {
                firm: sum(
                    area
                    for t_date, area in data[group_name]["area_by_firm_date"][firm].items()
                    if t_date < TODAY
                )
                for firm in FIRM_ORDER
            }
            if sum(data[group_name]["area_by_firm_total"].values()) > 0:
                group_has_data = True

        if group_name != PLOW_GROUP_NAME:
            for firm in FIRM_ORDER:
                data[group_name]["area_by_firm_total"][firm] = sum(
                    area
                    for t_date, area in data[group_name]["area_by_firm_date"][firm].items()
                    if t_date < TODAY
                )

        data[group_name]["machine_day_area"] = {
            firm: {dt: dict(by_mid) for dt, by_mid in machine_day_area[firm].items()}
            for firm in FIRM_ORDER
        }

        if _cap is not None:
            if group_name == PLOW_GROUP_NAME:
                _cap[2](group_name, None)
            else:
                min_ha = _detail_area_min_ha(group_name)
                ok_pairs = set()
                for firm in FIRM_ORDER:
                    by_mid = machine_day_area[firm].get(REPORT_SHIFT_DATE) or {}
                    for mid, total in by_mid.items():
                        if _machine_qualifies_for_area(total, min_ha):
                            ok_pairs.add((firm, mid))
                _cap[2](group_name, ok_pairs)

        if not group_has_data:
            print(f"    Пусто по API (шаблонные значения)")

        for firm in FIRM_ORDER:
            data[group_name]["crops"][firm] = {}
            for crop, by_date in agg.get(firm, {}).items():
                fields_set = firm_crop_fields[firm][crop]
                area_ha    = sum(field_area.get(fid, 0) for fid in fields_set)
                if CUSTOM_SHIFT_DATE_MIN is not None and CUSTOM_SHIFT_DATE_MAX is not None:
                    total_ha = sum(
                        v
                        for d, v in by_date.items()
                        if CUSTOM_SHIFT_DATE_MIN <= d <= CUSTOM_SHIFT_DATE_MAX
                    )
                else:
                    total_ha = sum(v for d, v in by_date.items() if d < TODAY)
                crop_digits = 2 if _is_harvest_veg_group(group_name) else 1
                data[group_name]["crops"][firm][crop] = {
                    "area_ha":  round(area_ha, 1),
                    "total_ha": round(total_ha, crop_digits),
                    "by_date":  {d: round(v, crop_digits) for d, v in by_date.items()},
                }

    return data


def dates_with_any_activity(work_data: dict) -> list[date]:
    """
    Дни в диапазоне [START_DATE, TODAY], где по любой группе работ есть данные:
    площадь по фирме за день, площадь по культуре за день или задействованная техника.
    """
    active: set[date] = set()
    for gd in work_data.values():
        area_by_firm  = gd.get("area_by_firm_date", {})
        crops_by_firm = gd.get("crops", {})
        machines_firm = gd.get("machines", {})
        for firm in FIRM_ORDER:
            for dt, v in area_by_firm.get(firm, {}).items():
                if isinstance(dt, date) and (v or 0) > 0:
                    active.add(dt)
            for _crop, cd in crops_by_firm.get(firm, {}).items():
                for dt, v in cd.get("by_date", {}).items():
                    if isinstance(dt, date) and (v or 0) > 0:
                        active.add(dt)
            for dt, bag in machines_firm.get(firm, {}).items():
                if isinstance(dt, date) and (bag.get("own") or bag.get("hired")):
                    active.add(dt)
    upper = TODAY - timedelta(days=1)
    return sorted(d for d in active if START_DATE <= d <= upper)


def dates_with_activity_for_firm(gd: dict, firm: str) -> list[date]:
    """Дни, в которых у данной фирмы в этой группе работ есть данные (для расчёта суточной выработки)."""
    active: set[date] = set()
    for dt, v in gd.get("area_by_firm_date", {}).get(firm, {}).items():
        if isinstance(dt, date) and (v or 0) > 0:
            active.add(dt)
    for _crop, cd in gd.get("crops", {}).get(firm, {}).items():
        for dt, v in cd.get("by_date", {}).items():
            if isinstance(dt, date) and (v or 0) > 0:
                active.add(dt)
    for dt, bag in gd.get("machines", {}).get(firm, {}).items():
        if not isinstance(dt, date):
            continue
        if bag.get("own") or bag.get("hired"):
            active.add(dt)
    upper = TODAY - timedelta(days=1)
    return sorted(d for d in active if START_DATE <= d <= upper)


def compute_display_dates(work_data: dict) -> list[date]:
    """
    Список дат для колонок Excel: не больше max_n последних дней.
    Если only_nonempty: последние max_n дней среди дней с данными.
    Иначе: последние max_n календарных дней до даты отчёта (без неё), не раньше START_DATE.
    Excel за период: все сменные дни от даты начала до даты конца включительно.
    """
    if CUSTOM_SHIFT_DATE_MIN is not None and CUSTOM_SHIFT_DATE_MAX is not None:
        lower = max(CUSTOM_SHIFT_DATE_MIN, START_DATE)
        upper = CUSTOM_SHIFT_DATE_MAX
        out: list[date] = []
        d = lower
        while d <= upper:
            out.append(d)
            d += timedelta(days=1)
        period_cap = _parse_int_env("SVODKI_REPORT2_EXCEL_PERIOD_MAX_DAYS", 31, 1, 62)
        if len(out) > period_cap:
            out = out[-period_cap:]
        return out
    max_n = _parse_int_env("SVODKI_HK_COMPACT_MAX_DAYS", 5, 1, 31)
    only_nonempty = _parse_bool_env("SVODKI_HK_COMPACT_ONLY_NONEMPTY", True)
    if only_nonempty:
        cand = dates_with_any_activity(work_data)
        if len(cand) <= max_n:
            return cand
        return cand[-max_n:]
    out: list[date] = []
    d = TODAY - timedelta(days=1)
    while len(out) < max_n and d >= START_DATE:
        out.append(d)
        d -= timedelta(days=1)
    out.reverse()
    return out


# =============================================================================
# СТИЛИ (как в PDF «Сводка ХК Детальная — Мастер Свод»)
# =============================================================================

_RU_MONTHS_GEN = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _date_header_ru(d: date) -> str:
    return f"{d.day} {_RU_MONTHS_GEN[d.month]}"


def _solid_fill(hex6: str) -> PatternFill:
    """ARGB для Excel/LibreOffice (необходим префикс FF)."""
    h = hex6.upper().lstrip("#")
    if len(h) == 6:
        h = "FF" + h
    return PatternFill(fill_type="solid", start_color=h, end_color=h)


def make_styles(*, for_excel_period: bool = False) -> dict:
    """Палитра как в отчётах 3, 5–8: шапки 8093B3, итоги BDD7EE, данные белые."""
    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_size = 10 if for_excel_period else 8
    return {
        "border":       border,
        "align_c":      Alignment(horizontal="center", vertical="center", wrap_text=False),
        "align_hdr":    Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=True,
            shrink_to_fit=not for_excel_period,
        ),
        "align_l":      Alignment(vertical="center", wrap_text=False),
        "fill_title":   _solid_fill("FFFFFF"),
        "font_title":   Font(bold=True, color="FF111827", size=14, name="Arial"),
        "fill_grp":     _solid_fill("FFFFFF"),
        "font_grp":     Font(bold=True, size=10, name="Arial", color="FF111827"),
        "fill_season":  _solid_fill("F2F2F2"),
        "font_season":  Font(bold=True, size=10, name="Arial", color="FF000000"),
        "fill_hdr":     _solid_fill("8093B3"),
        "font_hdr":     Font(bold=True, color="FFFFFFFF", size=hdr_size, name="Arial"),
        "fill_firm":    _solid_fill("FFFFFF"),
        "font_firm":    Font(bold=True, size=9, name="Arial", color="FF000000"),
        "fill_data":    _solid_fill("FFFFFF"),
        "font_data":    Font(name="Arial", size=8, color="FF000000"),
        "font_data_bold": Font(bold=True, name="Arial", size=8, color="FF000000"),
        "fill_total":   _solid_fill("BDD7EE"),
        "font_total":   Font(bold=True, name="Arial", size=8, color="FF000000"),
        "fill_subhdr":  _solid_fill("F2F2F2"),
        "font_subhdr":  Font(bold=True, size=9, name="Arial", color="FF000000"),
    }


def _merge_row_bar(
    ws,
    row: int,
    c1: int,
    c2: int,
    text: str,
    s: dict,
    *,
    fill,
    font,
    align: Alignment,
) -> None:
    """
    LibreOffice корректно показывает объединённую шапку, если заливка и границы
    заданы для каждой ячейки диапазона до merge.
    """
    br = s["border"]
    for col in range(c1, c2 + 1):
        c = ws.cell(row=row, column=col)
        c.fill = fill
        c.border = br
    top = ws.cell(row=row, column=c1, value=text)
    top.font = font
    top.alignment = align
    if c2 > c1:
        ws.merge_cells(start_row=row, start_column=c1, end_row=row, end_column=c2)


def sc(cell, s, fill=None, font=None, align=None) -> None:
    cell.border = s["border"]
    if fill:  cell.fill = fill
    if font:  cell.font = font
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


def _append_firm_title_row(ws, row: int, firm: str, s: dict) -> int:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    c = ws.cell(row=row, column=1, value=firm)
    sc(c, s, fill=s["fill_firm"], font=s["font_firm"], align=s["align_l"])
    return row + 1


def _veg54_accounting_area_ha(
    crop_area_ref: dict,
    crop_data: dict,
    crop: str,
) -> float:
    """Учётная площадь: сезонный справочник, иначе площадь полей с фактом по группе."""
    ref = float(crop_area_ref.get(crop, 0.0) or 0.0)
    if ref > 0:
        return round(ref, 1)
    cd = _crop_data_block(crop_data, crop)
    fallback = float(cd.get("area_ha", 0.0) or 0.0)
    return round(fallback, 1) if fallback > 0 else 0.0


def _append_simple_crop_row(
    ws,
    row: int,
    firm: str,
    crop: str,
    crop_data: dict,
    crop_area_ref: dict,
    group_name: str,
    display_dates: list,
    headers_crops: list[str],
    s: dict,
) -> int:
    area_ha = _veg54_accounting_area_ha(crop_area_ref, crop_data, crop)
    cd = _crop_data_block(crop_data, crop)
    plan_ha = _plan_ha_for_crop(group_name, firm, crop)
    total_ha = cd["total_ha"]
    pct = round(total_ha / plan_ha * 100, 1) if plan_ha else 0.0
    vals = [firm, crop, area_ha, plan_ha, total_ha, pct]
    for ci, val in enumerate(vals, start=1):
        c = ws.cell(row=row, column=ci, value=val)
        sc(
            c,
            s,
            fill=s["fill_data"],
            font=s["font_data"],
            align=s["align_l"] if ci in (1, 2) else s["align_c"],
        )
    for di, d in enumerate(display_dates, start=len(headers_crops) + 1):
        val = cd["by_date"].get(d, 0.0)
        c = ws.cell(row=row, column=di, value=val)
        sc(c, s, fill=s["fill_data"], font=s["font_data"], align=s["align_c"])
    return row + 1


def _write_harvest_veg54_firm_rows(
    ws,
    row: int,
    firm: str,
    crop_data: dict,
    crop_area_ref: dict,
    group_name: str,
    display_dates: list,
    headers_crops: list[str],
    s: dict,
) -> int:
    """Лист 5.4: фиксированный набор культур для Чистополя."""
    row = _append_firm_title_row(ws, row, firm, s)
    for crop in HARVEST_VEG_CROP_ORDER:
        if is_crop_omitted(group_name, firm, crop) or is_crop_omitted(
            HARVEST_VEG_PLAN_KEY, firm, crop
        ):
            continue
        row = _append_simple_crop_row(
            ws, row, firm, crop, crop_data, crop_area_ref,
            group_name, display_dates, headers_crops, s,
        )
    return row


def _audit_veg54_sheet(wb) -> None:
    """Контроль листа 5.4 после записи — видно в логах генерации."""
    title = _group_display_name(HARVEST_VEG_PLAN_KEY)[:31]
    if title not in wb.sheetnames:
        print(f"  [5.4] WARN: лист «{title}» не найден")
        return
    ws = wb[title]
    found: list[str] = []
    for r in range(1, ws.max_row + 1):
        crop = ws.cell(r, 2).value
        if crop in HARVEST_VEG_CROP_ORDER:
            found.append(str(crop))
    print(f"  [5.4] культуры на листе: {found}")
    missing = [c for c in HARVEST_VEG_CROP_ORDER if c not in found]
    if missing:
        print(f"  [5.4] WARN: отсутствуют строки: {missing}")

# =============================================================================
# ЗАПИСЬ ЛИСТА (только непустые группы)
# =============================================================================

def _apply_manual_page_breaks(ws, row_indices: list[int]) -> None:
    """Ручные горизонтальные разрывы: новая страница начинается с указанной строки."""
    from openpyxl.worksheet.pagebreak import Break

    seen: set[int] = set()
    for r in sorted(row_indices):
        r = int(r)
        if r <= 1 or r in seen:
            continue
        seen.add(r)
        ws.row_breaks.append(Break(id=r, min=0, max=16383, man=True))


def _make_unique_sheet_title(base: str, used_titles: set[str]) -> str:
    title = str(base or "").strip() or "Отчёт"
    title = title[:31]
    if title not in used_titles:
        return title
    idx = 2
    while True:
        suffix = f" ({idx})"
        candidate = f"{title[: max(1, 31 - len(suffix))]}{suffix}"
        if candidate not in used_titles:
            return candidate
        idx += 1


def _lock_row_heights(ws, max_row: int) -> None:
    """Уменьшаем высоту всех строк примерно на треть (для компактного PDF)."""
    for r in range(1, max_row + 1):
        rd = ws.row_dimensions[r]
        base_height = float(rd.height or 18)
        rd.height = round(base_height * 2 / 3, 1)


def _table_hdr_row_height(kind: str) -> float:
    """Высота строки шапки таблицы: multi — многострочные подписи, tall — блок 4."""
    if _excel_period_export():
        return {"multi": 44.0, "medium": 38.0, "tall": 50.0}[kind]
    return {"multi": 22.0, "medium": 20.0, "tall": 30.0}[kind]


def write_report_sheet(
    wb,
    work_data,          # теперь содержит только группы с данными
    display_dates,      # колонки по датам (последние N дней; не шире этих колонок)
    firm_plan_ha,
    firm_crop_area_ha,
    firm_crop_area_ha_general: dict | None = None,
    accounted_by_group: dict | None = None,
):
    excel_period = _excel_period_export()
    s = make_styles(for_excel_period=excel_period)
    n_dates = len(display_dates)
    n_cols  = 6 + n_dates

    # Овощные группы идут после зерновых
    VEG_GROUPS = {
        "4.1 Посев (овощи)",
        VEG_SPRAY_GROUP_NAME,
        HARVEST_VEG_PLAN_KEY,
        "2.7 Гребнеобразование",
        "2.6 Фрезерование",
    }
    group_names = [
        g for g, _ in WORK_GROUPS
        if g not in VEG_GROUPS
    ] + [
        g for g, _ in WORK_GROUPS
        if g in VEG_GROUPS
    ]

    # Группы, в которых показывается только одна фирма
    GROUP_FIRM_ONLY: dict[str, set[str]] = {
        "2.7 Гребнеобразование": {"АФ Чистопольская"},
        "2.6 Фрезерование":      {"АФ Чистопольская"},
        "4.1 Посев (овощи)":     {"АФ Чистопольская"},
        VEG_SPRAY_GROUP_NAME:    {"АФ Чистопольская"},
        HARVEST_VEG_PLAN_KEY:  {"АФ Чистопольская"},
    }
    # Группы, в которых показываются только определённые культуры
    _OVOSHI = set(VEG_SPRAY_CROPS) | {"Соя"}
    GROUP_CROP_ONLY: dict[str, set[str]] = {
        "2.7 Гребнеобразование": _OVOSHI,
        "2.6 Фрезерование":      _OVOSHI,
        "4.1 Посев (овощи)":     set(VEG_SPRAY_CROPS),
        VEG_SPRAY_GROUP_NAME:    set(VEG_SPRAY_CROPS),
        HARVEST_VEG_PLAN_KEY:  set(HARVEST_VEG_CROPS),
        # 2.4 Вспашка: только культуры сезона year+1 (без МТ/пара/«Не определено»).
        PLOW_GROUP_NAME:         set(PLOW_SEASON_CROPS),
    }
    GROUP_CROP_EXCLUDE: dict[str, frozenset[str]] = {
        SPRAY_GROUP_NAME: VEG_SPRAY_CROPS,
    }

    used_titles: set[str] = set()
    for group_name in group_names:
        gd = work_data.get(group_name) or _empty_group_block()
        display_name = _group_display_name(group_name)
        sheet_title = _make_unique_sheet_title(display_name, used_titles)
        used_titles.add(sheet_title)
        ws = wb.create_sheet(sheet_title)
        row = 1
        manual_break_rows: list[int] = []

        _merge_row_bar(
            ws, row, 1, n_cols, display_name, s,
            fill=s["fill_grp"],
            font=s["font_grp"],
            align=s["align_c"],
        )
        ws.row_dimensions[row].height = 18
        row += 1

        season_y = season_label_for_group(group_name, TODAY)
        _merge_row_bar(
            ws, row, 1, 3, f"Сезон: {season_y}", s,
            fill=s["fill_season"],
            font=s["font_season"],
            align=s["align_l"],
        )
        _merge_row_bar(
            ws, row, 4, n_cols, "", s,
            fill=s["fill_season"],
            font=s["font_season"],
            align=s["align_l"],
        )
        ws.row_dimensions[row].height = 18
        row += 1

        # -------------------------------------------------------------
        # 1. ТАБЛИЦА ПО КУЛЬТУРАМ
        # -------------------------------------------------------------
        headers_crops = [
            "Агрофирма",
            "Культура",
            "Площадь культуры\nучётная, га",
            "План",
            "Выполнено работ\nв Га",
            "в %",
        ]
        for ci, col_name in enumerate(headers_crops, start=1):
            c = ws.cell(row=row, column=ci, value=col_name)
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        for di, d in enumerate(display_dates, start=len(headers_crops) + 1):
            c = ws.cell(row=row, column=di, value=_date_header_ru(d))
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        ws.row_dimensions[row].height = _table_hdr_row_height("multi")
        row += 1
        crop_table_data_start = row

        for firm in FIRM_ORDER:
            # Фильтр: только разрешённые фирмы
            if group_name in GROUP_FIRM_ONLY and firm not in GROUP_FIRM_ONLY[group_name]:
                continue
            crop_data = gd.get("crops", {}).get(firm, {})
            if accounted_by_group is not None:
                crop_area_ref = (accounted_by_group.get(group_name) or {}).get(firm, {})
            elif group_name == DISK_GROUP_NAME and firm_crop_area_ha_general is not None:
                crop_area_ref = firm_crop_area_ha_general.get(firm, {})
            else:
                crop_area_ref = firm_crop_area_ha.get(firm, {})

            if _is_harvest_veg_group(group_name):
                row = _write_harvest_veg54_firm_rows(
                    ws, row, firm, crop_data, crop_area_ref,
                    group_name, display_dates, headers_crops, s,
                )
                continue

            all_crops = sorted(
                set(list(crop_data.keys()) + list(crop_area_ref.keys())),
                key=lambda c: _crop_sort_key_for_group(group_name, c),
            )
            # Фильтр: только разрешённые культуры (+ из плана, если в API нет строки)
            if group_name in GROUP_CROP_ONLY:
                allowed = GROUP_CROP_ONLY[group_name]
                plan_crops = [
                    c for c in _planned_crops_for_group_firm(group_name, firm)
                    if c in allowed or crop_code_for_report(group_name, c) == c
                ]
                all_crops = sorted(
                    set(all_crops) | set(plan_crops),
                    key=lambda c: _crop_sort_key_for_group(group_name, c),
                )
                all_crops = [
                    c for c in all_crops
                    if c in allowed
                    or c == OTHER_CROP_CODE
                    or crop_code_for_report(group_name, c) == c
                    or (
                        other_crops_as_rows(group_name)
                        and not is_crop_user_excluded(group_name, firm, c)
                        and crop_code_for_report(group_name, c) is None
                    )
                ]
            if group_name == HARVEST_GROUP_NAME:
                # Только лист 5.0: овощи и «Иные» без строки не выводим.
                # «Иные культуры» из редактора планов остаётся.
                all_crops = [c for c in all_crops if not _harvest_crop_excluded(firm, c)]
            excluded = GROUP_CROP_EXCLUDE.get(group_name)
            if excluded:
                all_crops = [c for c in all_crops if c not in excluded]
            # Редактор планов: ранг 0 / глаз по АФ / SKIP_CROPS (залежь и т.п.).
            # «Иные культуры» — одна строка на всех листах, не только вспашка и дискование.
            all_crops = [
                c for c in all_crops
                if c == OTHER_CROP_CODE
                or (
                    not is_crop_omitted(group_name, firm, c)
                    and crop_code_for_report(group_name, c) == c
                )
                or (
                    other_crops_as_rows(group_name)
                    and group_name in (PLOW_GROUP_NAME, DISK_GROUP_NAME)
                    and not is_crop_user_excluded(group_name, firm, c)
                    and crop_code_for_report(group_name, c) is None
                )
            ]
            if (
                OTHER_CROP_CODE not in all_crops
                and (
                    OTHER_CROP_CODE in crop_data
                    or OTHER_CROP_CODE in crop_area_ref
                    or _plan_ha_for_crop(group_name, firm, OTHER_CROP_CODE) > 0
                )
            ):
                all_crops.append(OTHER_CROP_CODE)
                all_crops.sort(key=lambda c: _crop_sort_key_for_group(group_name, c))
            if not all_crops:
                continue

            row = _append_firm_title_row(ws, row, firm, s)

            for crop, row_kind in (
                _iter_harvest_crop_rows(all_crops)
                if group_name == HARVEST_GROUP_NAME
                else [(c, "normal") for c in all_crops]
            ):
                grain_cd = _crop_data_block(crop_data, CORN_GRAIN_CROP)
                silage_cd = _crop_data_block(crop_data, CORN_SILAGE_CROP)
                grain_area = round(crop_area_ref.get(CORN_GRAIN_CROP, 0.0), 1)
                silage_area = round(crop_area_ref.get(CORN_SILAGE_CROP, 0.0), 1)
                grain_plan = _plan_ha_for_crop(group_name, firm, CORN_GRAIN_CROP)
                silage_plan = _plan_ha_for_crop(group_name, firm, CORN_SILAGE_CROP)

                if row_kind == "corn_summary":
                    area_ha = grain_area
                    plan_ha = grain_plan + silage_plan
                    cd = {
                        "total_ha": grain_cd["total_ha"] + silage_cd["total_ha"],
                        "by_date": {
                            d: grain_cd["by_date"].get(d, 0.0) + silage_cd["by_date"].get(d, 0.0)
                            for d in set(grain_cd["by_date"]) | set(silage_cd["by_date"])
                        },
                    }
                    crop_label = CORN_SUMMARY_LABEL
                elif row_kind == "corn_silage":
                    area_ha = silage_area
                    plan_ha = silage_plan
                    cd = silage_cd
                    crop_label = CORN_SILAGE_CROP
                elif row_kind == "corn_grain":
                    area_ha = grain_area
                    plan_ha = grain_plan
                    cd = grain_cd
                    crop_label = CORN_GRAIN_CROP
                else:
                    area_ha = round(crop_area_ref.get(crop, 0.0), 1)
                    cd = _crop_data_block(crop_data, crop)
                    plan_ha = _plan_ha_for_crop(group_name, firm, crop)
                    crop_label = crop

                total_ha = cd["total_ha"]
                plan_for_pct = (
                    grain_plan + silage_plan if row_kind == "corn_summary"
                    else float(plan_ha) if isinstance(plan_ha, (int, float)) else 0.0
                )
                pct = round(total_ha / plan_for_pct * 100, 1) if plan_for_pct else 0.0

                vals = [firm, crop_label, area_ha, plan_ha, total_ha, pct]
                bold_cols = (
                    row_kind in {"corn_summary", "corn_grain"}
                    and row_kind != "corn_silage"
                )
                for ci, val in enumerate(vals, start=1):
                    c = ws.cell(row=row, column=ci, value=val)
                    cell_font = s["font_data_bold"] if bold_cols and ci in (2, 3, 4) else s["font_data"]
                    sc(
                        c,
                        s,
                        fill=s["fill_data"],
                        font=cell_font,
                        align=s["align_l"] if ci in (1, 2) else s["align_c"],
                    )
                for di, d in enumerate(display_dates, start=len(headers_crops) + 1):
                    val = cd["by_date"].get(d, 0.0)
                    c = ws.cell(row=row, column=di, value=val)
                    sc(c, s, fill=s["fill_data"], font=s["font_data"], align=s["align_c"])
                row += 1

        # Разрыв перед сводными — только если таблица культур длинная.
        # Иначе на коротких листах (овощи/фрезерование) PDF получает
        # полупустую страницу с 2–3 строками и пустым низом.
        crop_data_rows = max(0, row - crop_table_data_start)
        _MIN_CROP_ROWS_FOR_PAGE_BREAK = 16
        if crop_data_rows >= _MIN_CROP_ROWS_FOR_PAGE_BREAK:
            manual_break_rows.append(row)
        row += 1

        # -------------------------------------------------------------
        # 2. СОВОКУПНАЯ ПЛОЩАДЬ ПО АГРОФИРМАМ
        # -------------------------------------------------------------
        _merge_row_bar(
            ws, row, 1, n_cols, "СОВОКУПНАЯ ПЛОЩАДЬ ПО АГРОФИРМАМ", s,
            fill=s["fill_subhdr"],
            font=s["font_subhdr"],
            align=s["align_c"],
        )
        ws.row_dimensions[row].height = 18
        row += 1

        headers_svod = ["Агрофирма", "План, га", "Выполнено, га", "%"]
        for ci, col_name in enumerate(headers_svod, start=1):
            c = ws.cell(row=row, column=ci, value=col_name)
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        for di, d in enumerate(display_dates, start=len(headers_svod) + 1):
            c = ws.cell(row=row, column=di, value=_date_header_ru(d))
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        ws.row_dimensions[row].height = _table_hdr_row_height("medium")
        row += 1

        def _firm_plan_from_crops(gn: str, firm: str) -> float | None:
            """План фирмы с учётом редактора: «Не считать» вне суммы, «Иные» — один раз."""
            total = firm_group_plan_ha(gn, firm)
            return float(total) if total else None

        area_digits = 2 if group_name == PLOW_GROUP_NAME or _is_harvest_veg_group(group_name) else 1
        for firm in FIRM_ORDER:
            if group_name in GROUP_FIRM_ONLY and firm not in GROUP_FIRM_ONLY[group_name]:
                continue
            plan_ha = _firm_plan_from_crops(group_name, firm) or 0.0
            area_by_firm_date = gd.get("area_by_firm_date", {}).get(firm, {})
            total_ha = round(gd.get("area_by_firm_total", {}).get(firm, 0.0), area_digits)
            pct = round(total_ha / plan_ha * 100, 1) if plan_ha else 0.0

            vals = [firm, plan_ha, total_ha, pct]
            for ci, val in enumerate(vals, start=1):
                c = ws.cell(row=row, column=ci, value=val)
                al = s["align_l"] if ci == 1 else s["align_c"]
                sc(c, s, fill=s["fill_data"], font=s["font_data"], align=al)
            for di, d in enumerate(display_dates, start=len(headers_svod) + 1):
                val = round(area_by_firm_date.get(d, 0.0), area_digits)
                c = ws.cell(row=row, column=di, value=val)
                sc(c, s, fill=s["fill_data"], font=s["font_data"], align=s["align_c"])
            row += 1
        # Итого
        _firms_svod = [f for f in FIRM_ORDER
                       if group_name not in GROUP_FIRM_ONLY or f in GROUP_FIRM_ONLY[group_name]]
        _plans = [(_firm_plan_from_crops(group_name, f) or 0.0) for f in _firms_svod]
        total_plan = round(sum(_plans), 1)
        total_done   = round(sum(gd.get("area_by_firm_total", {}).get(f, 0.0) for f in _firms_svod), area_digits)
        total_pct  = round(total_done / total_plan * 100, 1) if total_plan else 0.0
        itogo_svod   = ["Итого", total_plan, total_done, total_pct]
        for ci, val in enumerate(itogo_svod, start=1):
            c = ws.cell(row=row, column=ci, value=val)
            al = s["align_l"] if ci == 1 else s["align_c"]
            sc(c, s, fill=s["fill_total"], font=s["font_total"], align=al)
        for di, d in enumerate(display_dates, start=len(headers_svod) + 1):
            val = round(sum(gd.get("area_by_firm_date", {}).get(f, {}).get(d, 0.0) for f in _firms_svod), area_digits)
            c = ws.cell(row=row, column=di, value=val)
            sc(c, s, fill=s["fill_total"], font=s["font_total"], align=s["align_c"])
        row += 1
        row += 1

        # Малые сводные таблицы держим вместе, чтобы они не растягивались
        # отдельными почти пустыми страницами в PDF.
        row += 1

        # -------------------------------------------------------------
        # 3. КОЛИЧЕСТВО ЗАДЕЙСТВОВАННЫХ АГРЕГАТОВ
        # -------------------------------------------------------------
        _merge_row_bar(
            ws, row, 1, n_cols, "КОЛИЧЕСТВО ЗАДЕЙСТВОВАННЫХ АГРЕГАТОВ", s,
            fill=s["fill_subhdr"],
            font=s["font_subhdr"],
            align=s["align_c"],
        )
        ws.row_dimensions[row].height = 18
        row += 1

        headers_agg = ["Агрофирма", "Собственных\nагрегатов", "Наёмных\nза тек. день"]
        for ci, col_name in enumerate(headers_agg, start=1):
            c = ws.cell(row=row, column=ci, value=col_name)
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        for di, d in enumerate(display_dates, start=len(headers_agg) + 1):
            c = ws.cell(row=row, column=di, value=_date_header_ru(d))
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        ws.row_dimensions[row].height = _table_hdr_row_height("medium")
        row += 1

        today_date = display_dates[-1] if display_dates else None
        itogo_own   = 0
        itogo_hired = 0
        itogo_daily: dict[date, int] = defaultdict(int)

        for firm in FIRM_ORDER:
            if group_name in GROUP_FIRM_ONLY and firm not in GROUP_FIRM_ONLY[group_name]:
                continue
            machines = gd.get("machines", {}).get(
                firm, defaultdict(lambda: {"own": set(), "hired": set()})
            )
            own_count   = AVAILABLE_OWN.get(group_name, OWN_AGGREGATES).get(firm, 0)
            # Наёмных за текущий день (последняя дата в display_dates)
            hired_today = len(machines[today_date]["hired"]) if today_date else 0
            daily_counts: dict = defaultdict(lambda: {"own": 0, "hired": 0})
            for dt, v in machines.items():
                daily_counts[dt]["own"]   = len(v["own"])
                daily_counts[dt]["hired"] = len(v["hired"])
            itogo_own   += own_count
            itogo_hired += hired_today
            for d in display_dates:
                dc = daily_counts.get(d, {"own": 0, "hired": 0})
                itogo_daily[d] += dc["own"] + dc["hired"]
            vals = [firm, own_count, hired_today]
            for ci, val in enumerate(vals, start=1):
                c = ws.cell(row=row, column=ci, value=val)
                al = s["align_l"] if ci == 1 else s["align_c"]
                sc(c, s, fill=s["fill_data"], font=s["font_data"], align=al)
            for di, d in enumerate(display_dates, start=len(headers_agg) + 1):
                dc = daily_counts.get(d, {"own": 0, "hired": 0})
                total_agg = dc["own"] + dc["hired"]
                c = ws.cell(row=row, column=di, value=total_agg)
                sc(c, s, fill=s["fill_data"], font=s["font_data"], align=s["align_c"])
            row += 1
        # Итого
        for ci, val in enumerate(["Итого", itogo_own, itogo_hired], start=1):
            c = ws.cell(row=row, column=ci, value=val)
            al = s["align_l"] if ci == 1 else s["align_c"]
            sc(c, s, fill=s["fill_total"], font=s["font_total"], align=al)
        for di, d in enumerate(display_dates, start=len(headers_agg) + 1):
            c = ws.cell(row=row, column=di, value=itogo_daily.get(d, 0))
            sc(c, s, fill=s["fill_total"], font=s["font_total"], align=s["align_c"])
        row += 1
        row += 1

        # Следующий малый блок продолжается ниже на той же странице.
        row += 1

        # -------------------------------------------------------------
        # 4. СУТОЧНАЯ ВЫРАБОТКА НА 1 АГРЕГАТ (те же заливки/границы, что у блоков 2–3)
        # -------------------------------------------------------------
        _merge_row_bar(
            ws, row, 1, n_cols, "СУТОЧНАЯ ВЫРАБОТКА НА 1 АГРЕГАТ", s,
            fill=s["fill_subhdr"],
            font=s["font_subhdr"],
            align=s["align_c"],
        )
        ws.row_dimensions[row].height = 18
        row += 1

        headers_rate = [
            "Агрофирма",
            "Плановая\nвыработка, га/сутки",
            "Средняя выработка\nпо сезону, га/сутки",
        ]
        hdr_row = row
        for ci, col_name in enumerate(headers_rate, start=1):
            c = ws.cell(row=row, column=ci, value=col_name)
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        for di, d in enumerate(display_dates, start=len(headers_rate) + 1):
            c = ws.cell(row=row, column=di, value=_date_header_ru(d))
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        ws.row_dimensions[hdr_row].height = _table_hdr_row_height("tall")
        row += 1

        plan_rate = PLAN_RATE_PER_GROUP.get(group_name, 0.0)
        count_threshold = _group_count_threshold(group_name)
        area_threshold = _detail_area_min_ha(group_name)
        season_count_threshold = count_threshold
        mda_all = gd.get("machine_day_area", {})
        _firms_rate = [f for f in FIRM_ORDER
                       if group_name not in GROUP_FIRM_ONLY or f in GROUP_FIRM_ONLY[group_name]]
        for firm in _firms_rate:
            mda_firm = mda_all.get(firm, {})
            stats_dates = dates_with_activity_for_firm(gd, firm)
            area_by_date = (
                gd.get("area_by_firm_date", {}).get(firm, {})
                if group_name == PLOW_GROUP_NAME else None
            )
            season_sum, machine_days = _season_stats_for_firm(
                mda_firm,
                stats_dates,
                area_threshold,
                season_count_threshold,
                area_by_date=area_by_date,
            )
            avg_season = round(season_sum / machine_days, 1) if machine_days else 0.0

            vals = [firm, plan_rate, avg_season]
            for ci, val in enumerate(vals, start=1):
                c = ws.cell(row=row, column=ci, value=val)
                al = s["align_l"] if ci == 1 else s["align_c"]
                sc(c, s, fill=s["fill_data"], font=s["font_data"], align=al)
            firm_area_by_date = gd.get("area_by_firm_date", {}).get(firm, {})
            for di, dt in enumerate(display_dates, start=len(headers_rate) + 1):
                by_mid = mda_firm.get(dt, {})
                if group_name == PLOW_GROUP_NAME:
                    area = float(firm_area_by_date.get(dt, 0.0) or 0.0)
                else:
                    area = _day_rate_area(by_mid, area_threshold)
                agg_count = _day_rate_machines(by_mid, count_threshold)
                prod = round(area / agg_count, 2) if agg_count > 0 else 0
                c = ws.cell(row=row, column=di, value=prod)
                sc(c, s, fill=s["fill_data"], font=s["font_data"], align=s["align_c"])
            row += 1
        # Итого: season_sum всех фирм / machine_days всех фирм (как в сводке 5)
        total_season_sum = 0.0
        total_machine_days = 0
        for f in _firms_rate:
            abf = gd.get("area_by_firm_date", {}).get(f, {}) if group_name == PLOW_GROUP_NAME else None
            ss, md = _season_stats_for_firm(
                mda_all.get(f, {}),
                dates_with_activity_for_firm(gd, f),
                area_threshold,
                season_count_threshold,
                area_by_date=abf,
            )
            total_season_sum += ss
            total_machine_days += md
        avg_season_total = round(total_season_sum / total_machine_days, 1) if total_machine_days else 0.0
        for ci, val in enumerate(["Итого", plan_rate, avg_season_total], start=1):
            c = ws.cell(row=row, column=ci, value=val)
            al = s["align_l"] if ci == 1 else s["align_c"]
            sc(c, s, fill=s["fill_total"], font=s["font_total"], align=al)
        for di, dt in enumerate(display_dates, start=len(headers_rate) + 1):
            if group_name == PLOW_GROUP_NAME:
                total_area = sum(
                    float(gd.get("area_by_firm_date", {}).get(f, {}).get(dt, 0.0) or 0.0)
                    for f in _firms_rate
                )
            else:
                total_area = sum(
                    _day_rate_area(mda_all.get(f, {}).get(dt, {}), area_threshold)
                    for f in _firms_rate
                )
            total_agg = sum(
                _day_rate_machines(mda_all.get(f, {}).get(dt, {}), count_threshold)
                for f in _firms_rate
            )
            prod = round(total_area / total_agg, 2) if total_agg > 0 else 0
            c = ws.cell(row=row, column=di, value=prod)
            sc(c, s, fill=s["fill_total"], font=s["font_total"], align=s["align_c"])
        row += 1
        # Ширина колонок
        # Поджимаем "Агрофирма" для более компактной пропорции шапки.
        # Колонки: Агрофирма, Культура, Площадь культуры, План, Выполнено, %
        widths = [20, 26, 26, 11, 18, 10] if excel_period else [18, 24, 20, 9, 15, 9]
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        date_col_w = 14 if excel_period else 12
        for i in range(7, 7 + n_dates):
            ws.column_dimensions[get_column_letter(i)].width = date_col_w

        if not excel_period:
            _lock_row_heights(ws, row)
        _apply_manual_page_breaks(ws, manual_break_rows)


def build_report() -> None:
    _apply_manual_plan_rates()
    print(f"Отчёт: выполнение работ по культурам")
    if CUSTOM_WINDOW_START is not None and CUSTOM_WINDOW_END is not None:
        print(
            f"Произвольное окно: {CUSTOM_WINDOW_START.strftime('%d.%m.%Y %H:%M')} — "
            f"{CUSTOM_WINDOW_END.strftime('%d.%m.%Y %H:%M')}"
        )
        print(
            f"Сменные дни в отчёте: {CUSTOM_SHIFT_DATE_MIN.strftime('%d.%m.%Y')} — "
            f"{CUSTOM_SHIFT_DATE_MAX.strftime('%d.%m.%Y')}"
        )
    print(f"Сводка 2, правка 5.4: {SVODKA2_VEG54_FIX}")
    print(f"Сводка 2, уборка как 10_main: {SVODKA2_HARVEST10_FIX}")
    print(f"Сводка 2, дискование 2026+2027: {SVODKA2_DISK_SEASON_FIX}")
    print(f"Период: {START_DATE} — {TODAY}")
    print(f"Уборка: сезон с {HARVEST_SEASON_START}, буфер end_time +{HARVEST_CUTOFF_BUFFER_HOURS}ч\n")

    headers = {"X-User-Api-Token": API_TOKEN}

    timing_start()

    # Конвейер: цепочка заданий (machine_tasks → смены → mtfmi → агрооперации)
    # не зависит от справочников, поэтому стартует в фоне и идёт параллельно
    # с load_reference_data. Конфиг сезонов обновляем один раз ДО запуска потоков.
    prepared_job: BackgroundJob | None = None
    if PIPELINE:
        refresh_season_config()
        tasks_window = _widest_machine_tasks_window()
        print("Фоном запускаем подготовку заданий (machine_tasks → mtfmi → агрооперации)...")
        prepared_job = BackgroundJob(
            lambda: _prepare_group_selection(
                headers, refresh=False, tasks_window=tasks_window,
            ),
            "r2-prepare",
        )

    print("Загружаем справочники...")
    t_ref = time.monotonic()
    (field_to_crop, field_to_crop_general, field_to_crop_harvest,
     field_to_crop_plow,
     field_to_crop_veg,
     field_to_crop_report3,
     field_to_firm, field_to_firm_general, field_to_firm_veg,
     field_to_firm_all, crop_field_ids_report3,
     period_available_ids, period_available_ids_general,
     field_area, machine_group,
     firm_plan_ha, firm_crop_area_ha, firm_crop_area_ha_general,
     history_item_to_crop, history_item_to_year, accounted_by_group,
     ) = load_reference_data(refresh=not PIPELINE)
    stage_add("справочники (wall)", time.monotonic() - t_ref)

    print(f"\nПлан по агрофирмам (tillable_area активных полей):")
    for firm, ha in firm_plan_ha.items():
        print(f"  {firm}: {round(ha):,} га")

    print("\nСобираем данные по работам...")
    prepared = None
    if prepared_job is not None:
        # Если фон уже закончился раньше справочников — ожидание ≈ 0.
        with _stage("ожидание фоновой подготовки заданий"):
            prepared = prepared_job.result()
    t_collect = time.monotonic()
    work_data = collect_work_data(
        field_to_crop, field_to_crop_general, field_to_crop_harvest,
        field_to_crop_plow,
        field_to_crop_veg,
        field_to_crop_report3,
        field_to_firm, field_to_firm_general, field_to_firm_veg, field_to_firm_all,
        crop_field_ids_report3,
        period_available_ids, period_available_ids_general,
        field_area, machine_group, headers,
        history_item_to_crop=history_item_to_crop,
        history_item_to_year=history_item_to_year,
        prepared=prepared,
    )
    stage_add("сбор данных (без фона)", time.monotonic() - t_collect)

    if not _excel_period_export():
        try:
            from r23_from_report2 import export_r23_bundle_after_report2

            if os.environ.get("SVODKI_R23_WORK_DATA_BUNDLE_OUT"):
                machines_all = (
                    _MACHINES_ALL if _MACHINES_ALL is not None
                    else get_all_from_id("machines")
                )
                subtype_by_id = {
                    int(m["id"]): (m.get("machine_subtype") or "—")
                    for m in machines_all
                    if m.get("id") is not None
                }
                export_r23_bundle_after_report2(
                    work_data,
                    report_stamp=TODAY,
                    firm_order=FIRM_ORDER,
                    subtype_by_id=subtype_by_id,
                )
        except Exception as exc:
            print(f"  [r23←2] Не удалось записать снимок для сводки 23: {exc}")

    n_full = (TODAY - START_DATE).days + 1
    max_n = _parse_int_env("SVODKI_HK_COMPACT_MAX_DAYS", 5, 1, 31)
    only_n = _parse_bool_env("SVODKI_HK_COMPACT_ONLY_NONEMPTY", True)
    with_data_days = len(dates_with_any_activity(work_data))
    display_dates = compute_display_dates(work_data)
    print(
        f"\nДней в календарном периоде: {n_full}; дней с данными (всего): {with_data_days}; "
        f"макс. колонок с датами: {max_n}; только непустые дни: {only_n}; "
        f"выведено колонок: {len(display_dates)}"
    )

    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    print("Пишем листы по операциям...")
    with _stage("Excel: листы"):
        write_report_sheet(
            wb, work_data, display_dates, firm_plan_ha, firm_crop_area_ha,
            firm_crop_area_ha_general=firm_crop_area_ha_general,
            accounted_by_group=accounted_by_group,
        )
        _audit_veg54_sheet(wb)

    output_path = _resolve_output_xlsx_path()
    with _stage("Excel: сохранение"):
        wb.save(output_path)
    try:
        from datetime import timedelta

        from svodki_sanity import cross_op_daily_from_report2, publish_cross_op_snapshot

        shift_day = TODAY - timedelta(days=1)
        publish_cross_op_snapshot(cross_op_daily_from_report2(work_data, shift_day))
    except Exception:
        pass
    print(f"\nСохранено: {output_path.resolve()}")
    timing_finish()

def _work_day_ha(work_data: dict, day: date) -> tuple[float, dict[tuple[str, str, str], float]]:
    """Сумма «выполнено» за смену и гектары по группе/фирме/культуре."""
    total = 0.0
    by_crop: dict[tuple[str, str, str], float] = defaultdict(float)
    for group, gd in work_data.items():
        for firm, by_date in (gd.get("area_by_firm_date") or {}).items():
            total += float(by_date.get(day, 0.0) or 0.0)
        for firm, crops in (gd.get("crops") or {}).items():
            for crop, cd in crops.items():
                ha = float((cd.get("by_date") or {}).get(day, 0.0) or 0.0)
                if ha:
                    by_crop[(group, firm, crop)] += ha
    return total, by_crop


def _print_history_field_delta(old: dict, new: dict, days: list[date]) -> None:
    print("\n=== Факт: карта поля → history_item_id ===")
    for day in days:
        old_total, old_crops = _work_day_ha(old, day)
        new_total, new_crops = _work_day_ha(new, day)
        print(
            f"\n{day.isoformat()}  выполнено {old_total:.1f} → {new_total:.1f} га "
            f"({new_total - old_total:+.1f})"
        )
        keys = sorted(set(old_crops) | set(new_crops))
        shown = 0
        for key in keys:
            old_ha = round(old_crops.get(key, 0.0), 1)
            new_ha = round(new_crops.get(key, 0.0), 1)
            if abs(old_ha - new_ha) < 0.1:
                continue
            group, firm, crop = key
            print(f"  {group} | {firm} | {crop}: {old_ha:.1f} → {new_ha:.1f} ({new_ha - old_ha:+.1f})")
            shown += 1
        if not shown:
            print("  культуры без изменений ≥ 0.1 га")


def compare_history_field(days: list[date]) -> None:
    """Один проход заданий, затем старая карта поля и history_item_id."""
    global _HISTORY_FIELD_ONLY
    os.environ["SVODKI_SEASON_CACHE"] = "off"
    headers = {"X-User-Api-Token": API_TOKEN}
    refresh_season_config()
    print(f"Сравнение культуры факта сводки 2  |  дни: {', '.join(d.isoformat() for d in days)}")
    print("Загружаем справочники...")
    (
        field_to_crop, field_to_crop_general, field_to_crop_harvest,
        field_to_crop_plow, field_to_crop_veg, field_to_crop_report3,
        field_to_firm, field_to_firm_general, field_to_firm_veg,
        field_to_firm_all, crop_field_ids_report3,
        period_available_ids, period_available_ids_general,
        field_area, machine_group,
        _firm_plan_ha, _firm_crop_area_ha, _firm_crop_area_ha_general,
        history_item_to_crop, history_item_to_year, _accounted_by_group,
    ) = load_reference_data(refresh=True)
    print("Готовим задания...")
    prepared = _prepare_group_selection(
        headers, refresh=False, tasks_window=_widest_machine_tasks_window(),
    )
    collect_args = (
        field_to_crop, field_to_crop_general, field_to_crop_harvest,
        field_to_crop_plow, field_to_crop_veg, field_to_crop_report3,
        field_to_firm, field_to_firm_general, field_to_firm_veg, field_to_firm_all,
        crop_field_ids_report3,
        period_available_ids, period_available_ids_general,
        field_area, machine_group, headers,
    )
    print("\nСтарый расчёт: карта поля")
    _HISTORY_FIELD_ONLY = False
    old = collect_work_data(
        *collect_args,
        history_item_to_crop=history_item_to_crop,
        history_item_to_year=history_item_to_year,
        prepared=prepared,
    )
    import copy
    old = copy.deepcopy(old)
    print("\nНовый расчёт: history_item_id")
    _HISTORY_FIELD_ONLY = True
    new = collect_work_data(
        *collect_args,
        history_item_to_crop=history_item_to_crop,
        history_item_to_year=history_item_to_year,
        prepared=prepared,
    )
    _print_history_field_delta(old, new, days)


if __name__ == "__main__":
    if "--history-check" in sys.argv:
        raw_days = os.environ.get("SVODKI_HISTORY_CHECK_DAYS", "").strip()
        if raw_days:
            check_days = [date.fromisoformat(part) for part in raw_days.split(",") if part.strip()]
        else:
            check_days = [TODAY - timedelta(days=i) for i in (3, 2, 1)]
        compare_history_field(check_days)
    else:
        build_report()