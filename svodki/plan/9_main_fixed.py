# 11.06.2026
"""
Cropwise Operations — отчёт "Вспашка и глубокорыхление"
=========================================================
Вспашка:         wt=49,171,57
Глубокорыхление: wt=11,150,121,106,58

Логика (по эталону 3_main_real.py / 4_main_fixed.py):
- Агрофирма определяется по ПОЛЮ из mtfmi (field → field_group → group_folder → фирма),
  а НЕ по привязке машины к региону.
- Площадь: covered_area_hourly из machine_task_field_mapping_items.
  Фильтр: h < cutoff (для суток) / h < shift_cutoff (для сезона).
- DAY_START = report_date - 1 день + DAY_START_HOUR (по умолчанию 06:00)
- CUTOFF    = report_date + DAY_END_HOUR             (по умолчанию 06:00)
- Время:   work_duration - movements_on_road_duration, масштабируется по доле площади
- Коэффициент ширины: covered_area_by_track / work_distance_km / implements.width * 10 * 100
  track и distance масштабируются пропорционально доле часов в диапазоне
- Скорость: average_speed_on_field (м/с) × 3.6 → км/ч, округление до целых
- Сред. выработка по сезону: sum(площадь_сезона) / sum(машин_в_день) по всем дням сезона
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

import httpx
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import pandas as pd
import requests


DETAIL_MACHINES_MIN_AREA_NOTE = (
    "По вспашке в таблицах учитывается техника отработавшая больше 0,5 га; "
    "по глубокорыхлению — больше 10 га."
)
DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT = 22.0

from detail_sheet_pagination import (
    DetailPaginationConfig,
    DetailPageContext,
    compute_firm_single_page,
    write_paginated_section,
)


def _write_detail_machines_min_area_note(ws, row: int, n_cols: int) -> int:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n_cols)
    cell = ws.cell(row=row, column=1, value=DETAIL_MACHINES_MIN_AREA_NOTE)
    cell.font = Font(bold=False, size=11, color="000000")
    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT
    return row + 1

from plans import (
    API_TOKEN,
    BASE_URL,
    AVAILABLE_OWN,
    WORK_GROUPS,
    PLAN_RATE_PER_GROUP,
    HARDCODED_PLAN,
    PREFERRED_CROP_ORDER,
    FIRM_TO_FOLDER,
    FOLDER_TO_FIRM,
    FIRM_ORDER,
    FIELD_NAME_FORCE_RULES,
    SKIP_CROPS,
    is_crop_firm_hidden,
    is_crop_omitted,
    crop_sort_key_for_group,
    TZ_MSK as _TZ_MSK,
)

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================
REPORT_DATE = date(2026, 9, 4)
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

# Год сезона в Cropwise (значение "year" в history_items) — для озимых культур
# НЕ совпадает с календарным годом отчёта: посев озимых осенью 2026 относится
# к сезону/году урожая 2027, поэтому историю культур запрашиваем именно за
# этот год, а не за REPORT_DATE.year.
def _plow_task_season_year(report_date: date) -> int:
    """Год machine_task.season для вспашки (год урожая, обычно year+1).

    Сайт кладёт в SEASON_YEAR календарный год отчёта: его пишет «5.0 Уборка»
    раньше «2.4 Вспашка». Задания вспашки сезона year+1 тогда отбрасываются,
    и «Обработано за сутки, га» становится 0. Локальный Excel этот env не ставит.
    Берём годы группы 2.4, иначе year+1. Глобальный SEASON_YEAR не используем.
    """
    raw = (os.environ.get("SVODKI_GRP_2_4_Вспашка_TASK_SEASON_YEARS") or "").strip()
    years: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            year = int(part)
        except ValueError:
            continue
        if 1990 <= year <= 2100:
            years.append(year)
    if years:
        return max(years)
    return report_date.year + 1


# Вспашка (факт): только machine_task.season == SEASON_YEAR (как 2_main).
SEASON_YEAR: int = _plow_task_season_year(REPORT_DATE)
SVODKA9_PLOW_SEASON_FIX = "2026-08-07c"

SEASON_START = date(REPORT_DATE.year, 3, 14)
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

TZ_MSK = _TZ_MSK

# Вспашка — ID берём из WORK_GROUPS plans.py ("2.4 Вспашка")
_plow_ids: set[int] = {wt_id for name, ids in WORK_GROUPS if name == "2.4 Вспашка" for wt_id in ids}
PLOW_WORK_TYPES: dict[int, str] = {wt_id: "2.4 Вспашка" for wt_id in _plow_ids}
PLOW_WT_IDS = _plow_ids

# Глубокорыхление — ID берём из WORK_GROUPS plans.py ("2.8 Глубокорыхление")
_deep_ids: set[int] = {wt_id for name, ids in WORK_GROUPS if name == "2.8 Глубокорыхление" for wt_id in ids}
DEEP_WORK_TYPES: dict[int, str] = {wt_id: "2.8 Глубокорыхление" for wt_id in _deep_ids}
DEEP_WT_IDS = _deep_ids

ALL_WT_IDS = PLOW_WT_IDS | DEEP_WT_IDS

WORK_TYPE_TITLE = "Вспашка и глубокорыхление"

# Доступно собственных агрегатов по вспашке — из AVAILABLE_OWN plans.py
PLOW_OWN_COUNT: dict[str, int] = dict(AVAILABLE_OWN["2.4 Вспашка"])

# Доступно собственных агрегатов по глубокорыхлению — из AVAILABLE_OWN plans.py
DEEP_OWN_COUNT: dict[str, int] = dict(AVAILABLE_OWN["2.8 Глубокорыхление"])

# Плановая выработка за сутки из plans.py
PLAN_DAY_PLOW: float = PLAN_RATE_PER_GROUP.get("2.4 Вспашка", 40.0)
PLAN_DAY_DEEP: float = PLAN_RATE_PER_GROUP.get("2.8 Глубокорыхление", 38.0)

# План га сезон (индивидуален для каждой фирмы, не хранится в plans.py)
PLAN_SEASON_PLOW: dict[str, float] = {
    "АФ Чистопольская":           22047,
    "АгроРаздолье":               20373,
    "Аксу-Агро":                  20152,
    "НурлатАгро":                 33127,
    "Дрожжаное Рассвет Агро":     13046,
    "Актай":                        958,
    "Лениногорская Птицефабрика":     0,
}

# --- Вспашка по культурам (как 2_main_ideal_compact, раздел 2.4) ------------
# Культура поля — только history_items year=SEASON_YEAR (2027), не 2026+2027.
# AgroOperation для вспашки не используется (как во 2_main).
PLOW_SEASON_CROPS: frozenset[str] = frozenset({
    "Рапс озимый",
    "Рапс яровой",
    "Пшеница озимая",
    "Подсолнечник",
    "Соя",
    "Кукуруза на зерно",
    "Картофель",
    "Морковь",
    "Свекла столовая",
})

_PREFERRED_CROP_INDEX = {name.lower(): i for i, name in enumerate(PREFERRED_CROP_ORDER)}


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
            return None
        return "Кукуруза на зерно"
    return None


def _plow_crop_for_field(fid_i: int, field_to_crop_plow: dict[int, str]) -> str | None:
    return field_to_crop_plow.get(fid_i)


def _build_field_to_crop_plow(
    crops_map: dict[int, str],
    *,
    season_year: int | None = None,
) -> dict[int, str]:
    """
    Культура поля для 2.4 Вспашка — только сезон year+1 (SEASON_YEAR),
    без добора из календарного года отчёта (как 2_main_ideal_compact).
    """
    season_year = int(season_year if season_year is not None else SEASON_YEAR)
    result: dict[int, str] = {}
    kept = 0
    skipped_other = 0
    for hi in get_all_from_id("history_items", {"year": str(season_year)}):
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
        if not raw or raw in SKIP_CROPS:
            continue
        mapped = _map_plow_crop_name(raw)
        if mapped is None:
            skipped_other += 1
            continue
        result[fid_i] = mapped
        kept += 1
    print(
        f"  field_to_crop_plow (только сезон {season_year}, "
        f"{len(PLOW_SEASON_CROPS)} культур): {len(result)} полей "
        f"(записей ок={kept}, прочих культур пропущено={skipped_other})"
    )
    return result


def _crop_sort_key(name: str) -> tuple[int, int, str]:
    try:
        return crop_sort_key_for_group("2.4 Вспашка", name)
    except Exception:
        low = (name or "").strip().lower()
        if low in _PREFERRED_CROP_INDEX:
            return (0, _PREFERRED_CROP_INDEX[low], low)
        return (1, 999, low)


def _plow_plan_ha(firm: str, crop: str) -> float:
    return float(
        HARDCODED_PLAN.get("2.4 Вспашка", {}).get(firm, {}).get(crop, 0.0) or 0.0
    )


def _plow_crops_for_firm(firm: str, by_firm_crop: dict[str, dict[str, dict]]) -> list[str]:
    """Культуры фирмы: из плана или с ненулевым фактом."""
    crops: set[str] = set()
    firm_stats = by_firm_crop.get(firm, {})
    for crop in PLOW_SEASON_CROPS:
        if is_crop_omitted("2.4 Вспашка", firm, crop):
            continue
        plan = _plow_plan_ha(firm, crop)
        st = firm_stats.get(crop, {})
        if plan > 0 or (st.get("season_plow") or 0) > 0 or (st.get("plow_today") or 0) > 0:
            crops.add(crop)
    return sorted(crops, key=_crop_sort_key)


def _plow_crops_for_report(by_firm_crop: dict[str, dict[str, dict]]) -> list[str]:
    """Культуры, для которых создаётся отдельный лист (есть план или факт)."""
    crops: set[str] = set()
    for firm in FIRM_ORDER:
        crops.update(_plow_crops_for_firm(firm, by_firm_crop))
    return sorted(crops, key=_crop_sort_key)


def _plow_sheet_title(crop: str, used: set[str]) -> str:
    """Уникальное имя листа Excel (≤31 символ)."""
    base = f"Вспашка {crop}"[:31]
    title = base
    n = 2
    while title in used:
        suffix = f" ({n})"
        title = f"{base[:31 - len(suffix)]}{suffix}"
        n += 1
    used.add(title)
    return title

# FIRM_TO_FOLDER, FOLDER_TO_FIRM, FIELD_NAME_FORCE_RULES импортированы из plans.py

SEMAPHORE_LIMIT = 40
MIN_DISPLAY_AREA_HA = 0.5
MIN_COUNT_AREA_HA = 10.0
# Вспашка: без порога 10 га (как в 1–2-й сводках — от 0,5 га на машину).
PLOW_COUNT_AREA_HA = MIN_DISPLAY_AREA_HA
DEEP_COUNT_AREA_HA = MIN_COUNT_AREA_HA
HIRED_MACHINE_GROUP_ID = 7
PLOW_GROUP_NAME = "2.4 Вспашка"
DEEP_GROUP_NAME = "2.8 Глубокорыхление"
SUMMARY_MACHINES_NOTE = DETAIL_MACHINES_MIN_AREA_NOTE

# Глобальный кэш машин
machines_map: dict[int, dict] = {}


def _is_hired_machine_group(group) -> bool:
    if group is None:
        return False
    try:
        return int(group) == int(HIRED_MACHINE_GROUP_ID)
    except (TypeError, ValueError):
        return False


def _machine_meets_detail_threshold(d: dict) -> bool:
    """Детализация: вспашка >= 0,5 га или глубокорыхление >= 10 га."""
    return (
        round(float(d.get("plow_area") or 0.0), 2) >= PLOW_COUNT_AREA_HA
        or round(float(d.get("deep_area") or 0.0), 2) >= DEEP_COUNT_AREA_HA
    )


def _shift_window(end_time_raw) -> tuple[date, datetime, datetime] | None:
    """Окно смены — как в 1_main_ammonium__1_.py."""
    end_dt = parse_dt(end_time_raw)
    if end_dt is None:
        return None
    cal_date = end_dt.date()
    anchor = datetime(
        cal_date.year, cal_date.month, cal_date.day,
        DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    if end_dt < anchor:
        cal_date = cal_date - timedelta(days=1)
        anchor = datetime(
            cal_date.year, cal_date.month, cal_date.day,
            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
        )
    return cal_date, anchor, anchor + timedelta(days=1)


def _today_task_ids(
    tasks: list[dict],
    day_start: datetime,
    cutoff: datetime,
) -> set[int]:
    """Задания текущей смены отчёта — как в 1-й сводке."""
    out: set[int] = set()
    for t in tasks:
        sw = _shift_window(t.get("end_time"))
        if sw is None:
            continue
        _, shift_ds, shift_co = sw
        if shift_ds >= day_start and shift_co <= cutoff:
            out.add(t["id"])
    return out


def load_field_to_crop_and_available(
    report_date: date,
    period_start: date | None = None,
    season_year: int | None = None,
) -> tuple[dict[int, str], set[int]]:
    """Как в 1-й сводке: активная культура + поля available в периоде.

    season_year — год сезона в Cropwise (значение "year" в history_items).
    Для озимых культур это НЕ календарный год отчёта: посев осенью относится
    к сезону следующего года (год урожая), поэтому по умолчанию берём
    SEASON_YEAR, а не report_date.year.
    """
    season_year = season_year if season_year is not None else SEASON_YEAR
    period_start = period_start or date(report_date.year, 3, 1)
    crops_map = {
        c["id"]: c.get("name") or c.get("title", "")
        for c in get_all_from_id("crops")
    }
    field_to_crop: dict[int, str] = {}
    years_to_load: list[int] = []
    for y in (season_year, report_date.year):
        if y not in years_to_load:
            years_to_load.append(y)
    for crop_year in years_to_load:
        chunk_n = 0
        for hi in get_all_from_id("history_items", {"year": str(crop_year)}):
            fid = hi.get("field_id")
            crop_id = hi.get("crop_id")
            if not (fid and crop_id and crop_id in crops_map and hi.get("active")):
                continue
            crop_name = crops_map[crop_id]
            if crop_name in SKIP_CROPS:
                continue
            fid_i = int(fid)
            if fid_i in field_to_crop and crop_year != season_year:
                continue
            field_to_crop[fid_i] = crop_name
            chunk_n += 1
        print(f"  history_items year={crop_year}: +{chunk_n} полей (всего {len(field_to_crop)})")

    available_ids: set[int] = set()
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
            available_ids.add(int(fid_r))

    print(f"  Полей с культурой (без SKIP_CROPS): {len(field_to_crop)}")
    print(f"  Полей available в периоде {period_start}–{report_date}: {len(available_ids)}")
    return field_to_crop, available_ids


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


def _build_field_to_firm(
    fields_all: list[dict],
    fgs_all:    list[dict],
    gfs_all:    list[dict],
) -> dict[int, str]:
    """field_id → firm_name через иерархию папок + принудительные правила по имени."""
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


async def load_field_to_firm_async(
    client:    httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    field_ids: set[int],
) -> dict[int, str]:
    """Поля из mtfmi через mass_request; field_groups/group_folders — полный справочник."""
    if field_ids:
        print(f"  Загружаем {len(field_ids)} полей через mass_request...")
        fields_all = await mass_request(client, "fields", field_ids, semaphore)
    else:
        fields_all = []
    print("  Загружаем field_groups / group_folders...")
    fgs_all = get_all_from_id("field_groups")
    gfs_all = get_all_from_id("group_folders")
    return _build_field_to_firm(fields_all, fgs_all, gfs_all)



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
                        params={**params, "from_id": from_id, "limit": 1000})
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


# =============================================================================
# АСИНХРОННЫЕ ЗАПРОСЫ
# =============================================================================

async def fetch_all_pages(
    client:    httpx.AsyncClient,
    url:       str,
    params:    dict,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    items   = []
    from_id = 0
    async with semaphore:
        while True:
            r     = await client.get(url, params={**params, "from_id": from_id, "limit": 1000})
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


MASS_REQUEST_BATCH_SIZE = 500


async def mass_request(
    client:     httpx.AsyncClient,
    endpoint:   str,
    ids:        set[int] | list[int],
    semaphore:  asyncio.Semaphore,
    batch_size: int = MASS_REQUEST_BATCH_SIZE,
) -> list[dict]:
    """POST /{endpoint}/mass_request — ресурсы по id, батчами batch_size."""
    ids = list(ids)
    if not ids:
        return []

    async def _one(batch: list[int]) -> list[dict]:
        async with semaphore:
            r = await client.post(
                f"{BASE_URL}/{endpoint}/mass_request",
                json={"data": batch},
                timeout=120,
            )
            r.raise_for_status()
            return r.json().get("data", [])

    batches = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
    results = await asyncio.gather(*[_one(b) for b in batches])
    out: list[dict] = []
    for chunk in results:
        out.extend(chunk)
    return out


def collect_report_resource_ids(
    tasks_season: list[dict],
    mtfmi_map:    dict[int, list[dict]],
) -> tuple[set[int], set[int], set[int]]:
    """field / machine / implement id из заданий сезона и mtfmi."""
    field_ids: set[int] = set()
    machine_ids: set[int] = set()
    implement_ids: set[int] = set()
    for items in mtfmi_map.values():
        for item in items:
            fid = item.get("field_id")
            if fid is not None:
                field_ids.add(int(fid))
    for t in tasks_season:
        mid = t.get("machine_id")
        if mid is not None:
            machine_ids.add(int(mid))
        iid = t.get("implement_id")
        if iid is not None:
            implement_ids.add(int(iid))
    return field_ids, machine_ids, implement_ids


async def load_machines_map_async(
    client:      httpx.AsyncClient,
    semaphore:   asyncio.Semaphore,
    machine_ids: set[int],
) -> dict[int, dict]:
    if not machine_ids:
        return {}
    print(f"  Загружаем {len(machine_ids)} машин через mass_request...")
    rows = await mass_request(client, "machines", machine_ids, semaphore)
    return {m["id"]: m for m in rows if m.get("id") is not None}


async def load_implements_dict_async(
    client:        httpx.AsyncClient,
    semaphore:     asyncio.Semaphore,
    implement_ids: set[int],
) -> dict[int, dict]:
    if not implement_ids:
        return {}
    print(f"  Загружаем {len(implement_ids)} орудий через mass_request...")
    rows = await mass_request(client, "implements", implement_ids, semaphore)
    return {imp["id"]: imp for imp in rows if imp.get("id") is not None}


async def fetch_all_tasks_and_mtfmi(
    client:        httpx.AsyncClient,
    semaphore:     asyncio.Semaphore,
    day_start:     datetime,
    cutoff:        datetime,
    season_start:  datetime,
    work_type_ids: set[int],
) -> tuple[list[dict], list[dict], dict[int, list[dict]]]:
    """
    Загружает задания за сезон с фильтром work_type_id на стороне API (батчами).
    Агрофирма будет определена позже по полю из mtfmi.
    """
    from machine_tasks_batch_fetch import fetch_machine_tasks_by_wt_async

    season_start_str = season_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str       = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    tasks_season_raw = await fetch_machine_tasks_by_wt_async(
        fetch_all_pages, client, BASE_URL, work_type_ids,
        {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_str},
        semaphore, log_prefix="[r9] ",
    )

    seen_season: set[int] = set()
    tasks_season: list[dict] = []
    for t in tasks_season_raw:
        tid = t.get("id")
        if tid is None or tid in seen_season:
            continue
        seen_season.add(tid)
        tasks_season.append(t)

    today_ids = _today_task_ids(tasks_season, day_start, cutoff)
    print(
        f"  Заданий за сезон (с {SEASON_START.strftime('%d.%m.%Y')}): {len(tasks_season)}, "
        f"за смену отчёта (как сводка 1): {len(today_ids)}"
    )

    all_task_ids = list({t["id"] for t in tasks_season})
    if all_task_ids:
        from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks

        mtfmi_map = await fetch_mtfmi_map_for_tasks(
            client, semaphore, BASE_URL, all_task_ids, log_prefix="[r9] ",
        )
    else:
        mtfmi_map = {}

    return today_ids, tasks_season, mtfmi_map


# =============================================================================
# ПОДСЧЁТ — агрофирма определяется по полю
# =============================================================================

def _empty_crop_plow_stats() -> dict:
    return {
        "season_plow": 0.0,
        "plow_today": 0.0,
        "engine_plow": 0.0,
        "plow_machines_today": 0,
    }


def build_plow_stats_by_crop(
    today_task_ids: set[int],
    tasks_season: list[dict],
    mtfmi_map: dict[int, list[dict]],
    cutoff: datetime,
    field_to_firm: dict[int, str],
    field_to_crop_plow: dict[int, str],
    task_year: int | None = None,
) -> dict[str, dict[str, dict]]:
    """
    Факт вспашки по культурам PLOW_SEASON_CROPS — один проход по заданиям.
    Культура — только field_to_crop_plow (history_items year=SEASON_YEAR).
    """
    required_season = int(task_year) if task_year is not None else int(SEASON_YEAR)
    report_day = cutoff.date()

    by_firm_crop: dict[str, dict[str, dict]] = {
        firm: {crop: _empty_crop_plow_stats() for crop in PLOW_SEASON_CROPS}
        for firm in FIRM_ORDER
    }
    today_engine_sec: dict[str, dict[str, float]] = {
        firm: defaultdict(float) for firm in FIRM_ORDER
    }
    today_machines: dict[str, dict[str, set[int]]] = {
        firm: {crop: set() for crop in PLOW_SEASON_CROPS} for firm in FIRM_ORDER
    }

    def _plow_season_ok(task: dict) -> bool:
        for key in ("season", "year", "season_year"):
            raw = task.get(key)
            if raw is None:
                continue
            try:
                return int(raw) == required_season
            except (TypeError, ValueError):
                continue
        return False

    def _item_cov_before(cutoff_dt: datetime, item: dict) -> float:
        hourly = item.get("covered_area_hourly") or []
        cov = 0.0
        for entry in hourly:
            h = parse_dt(entry[0])
            if h and h < cutoff_dt:
                cov += float(entry[1] or 0.0)
        if not hourly:
            cov = float(item.get("covered_area") or 0.0)
        return cov

    # --- за сутки ---
    for t in tasks_season:
        tid = t.get("id")
        if not _plow_season_ok(t):
            continue
        if tid not in today_task_ids:
            continue
        try:
            if int(t.get("work_type_id")) not in PLOW_WT_IDS:
                continue
        except (TypeError, ValueError):
            continue

        mid = t.get("machine_id")
        items = mtfmi_map.get(tid, [])
        sw = _shift_window(t.get("end_time"))
        if sw is None:
            continue

        total_cov = sum(_item_cov_before(cutoff, item) for item in items)

        for item in items:
            fid = item.get("field_id")
            if not fid:
                continue
            fid_i = int(fid)
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue
            crop = _plow_crop_for_field(fid_i, field_to_crop_plow)
            if not crop:
                continue

            cov_h = _item_cov_before(cutoff, item)
            if cov_h <= 0:
                continue

            by_firm_crop[firm][crop]["plow_today"] += cov_h
            if mid is not None:
                today_machines[firm][crop].add(int(mid))

            work_dur = t.get("work_duration") or 0
            road_move = t.get("movements_on_road_duration") or 0
            work_net = max(work_dur - road_move, 0)
            if total_cov > 0:
                today_engine_sec[firm][crop] += work_net * (cov_h / total_cov)

    # --- за сезон (до даты отчёта) ---
    for t in tasks_season:
        if not _plow_season_ok(t):
            continue
        try:
            if int(t.get("work_type_id")) not in PLOW_WT_IDS:
                continue
        except (TypeError, ValueError):
            continue

        end_dt = parse_dt(t.get("end_time"))
        if not end_dt:
            continue
        anchor = datetime(
            end_dt.year, end_dt.month, end_dt.day,
            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
        )
        if end_dt < anchor:
            shift_d = (end_dt - timedelta(days=1)).date()
            shift_cutoff = anchor
        else:
            shift_d = end_dt.date()
            shift_cutoff = anchor + timedelta(days=1)
        if shift_d >= report_day:
            continue

        for item in mtfmi_map.get(t["id"], []):
            fid = item.get("field_id")
            if not fid:
                continue
            fid_i = int(fid)
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue
            crop = _plow_crop_for_field(fid_i, field_to_crop_plow)
            if not crop:
                continue

            cov = 0.0
            for entry in (item.get("covered_area_hourly") or []):
                h = parse_dt(entry[0])
                if h and h < shift_cutoff:
                    cov += float(entry[1] or 0.0)
            if cov <= 0:
                continue

            by_firm_crop[firm][crop]["season_plow"] += cov

    for firm in FIRM_ORDER:
        for crop in PLOW_SEASON_CROPS:
            st = by_firm_crop[firm][crop]
            st["season_plow"] = round(st["season_plow"], 2)
            st["plow_today"] = round(st["plow_today"], 2)
            st["engine_plow"] = round(today_engine_sec[firm][crop] / 3600, 1)
            st["plow_machines_today"] = len(today_machines[firm][crop])

    season_total = round(
        sum(
            by_firm_crop[f][c]["season_plow"]
            for f in FIRM_ORDER for c in PLOW_SEASON_CROPS
        ),
        2,
    )
    print(f"  [вспашка по культурам] season_plow итого={season_total}")
    return by_firm_crop


def build_all_firm_stats(
    today_task_ids: set[int],
    tasks_season:   list[dict],
    mtfmi_map:      dict[int, list[dict]],
    season_start:   datetime,
    day_start:      datetime,
    cutoff:         datetime,
    field_to_firm:  dict[int, str],
    field_to_crop:  dict[int, str],
    implements_dict: dict[int, dict],
    available_field_ids: set[int] | None = None,
    task_year: int | None = None,
) -> dict[str, dict]:
    """
    Распределяет задания по агрофирмам на основе поля из mtfmi.
    Возвращает: firm_name → stats
    """

    # per_firm_machine[firm][machine_id] → агрегированные данные
    per_firm_machine: dict[str, dict[int, dict]] = {
        firm: {} for firm in FIRM_ORDER
    }

    def _field_allowed(fid_i: int) -> bool:
        """Как 2_main (TILLAGE_GROUPS): фирма по полю, без available/crop."""
        return fid_i in field_to_firm

    def _item_cov_before(cutoff_dt: datetime, item: dict) -> float:
        hourly = item.get("covered_area_hourly") or []
        cov = 0.0
        for entry in hourly:
            h = parse_dt(entry[0])
            if h and h < cutoff_dt:
                cov += float(entry[1] or 0.0)
        if not hourly:
            cov = float(item.get("covered_area") or 0.0)
        return cov

    def _task_is_plow(task: dict) -> bool:
        try:
            return int(task.get("work_type_id")) in PLOW_WT_IDS
        except (TypeError, ValueError):
            return False

    def _plow_task_season_ok(task: dict) -> bool:
        """Вспашка: только season == year+1 (как 2_main). Без season — пропуск."""
        required = int(task_year) if task_year is not None else int(SEASON_YEAR)
        for key in ("season", "year", "season_year"):
            raw_value = task.get(key)
            if raw_value is None:
                continue
            try:
                return int(raw_value) == required
            except (TypeError, ValueError):
                continue
        return False

    def _task_matches_required_year(task: dict) -> bool:
        # Глубокорыхление — без фильтра по сезону задания.
        if not _task_is_plow(task):
            return True
        return _plow_task_season_ok(task)

    today_plow_by_field: dict[str, dict[int, float]] = {
        firm: defaultdict(float) for firm in FIRM_ORDER
    }
    today_deep_by_field: dict[str, dict[int, float]] = {
        firm: defaultdict(float) for firm in FIRM_ORDER
    }
    _note = None
    try:
        from proverka.task_facts import bind_capture

        _bound = bind_capture()
        _note = _bound[0] if _bound else None
    except Exception:
        _note = None

    for t in tasks_season:
        tid = t.get("id")
        if not _task_matches_required_year(t):
            continue
        if tid not in today_task_ids:
            continue

        mid   = t.get("machine_id")
        wt    = t.get("work_type_id")
        items = mtfmi_map.get(tid, [])

        sw = _shift_window(t.get("end_time"))
        if sw is None:
            continue

        firm_items_map: dict[str, list[tuple]] = defaultdict(list)
        for item in items:
            fid = item.get("field_id")
            if not fid:
                continue
            fid_i = int(fid)
            if not _field_allowed(fid_i):
                continue
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue
            cov_h = _item_cov_before(cutoff, item)
            if cov_h <= 0:
                continue
            firm_items_map[firm].append((item, cov_h))

        if not firm_items_map:
            continue

        total_cov_in_window = 0.0
        for item in items:
            total_cov_in_window += _item_cov_before(cutoff, item)

        for firm, fitems_cov in firm_items_map.items():
            cov_firm_today = 0.0
            track_area       = 0.0
            work_distance_km = 0.0

            for item, cov_h in fitems_cov:
                fid_i = int(item.get("field_id") or 0)
                cov_firm_today += cov_h
                if wt in PLOW_WT_IDS and fid_i:
                    today_plow_by_field[firm][fid_i] += cov_h
                    if _note is not None:
                        _note(
                            group="2.4 Вспашка", firm=firm, crop=str(field_to_crop.get(fid_i) or ""),
                            field_id=fid_i, task_id=int(tid) if tid is not None else None,
                            machine_id=int(mid) if mid is not None else None, ha=cov_h,
                            season=str(t.get("season") or ""),
                        )
                if wt in DEEP_WT_IDS and fid_i:
                    today_deep_by_field[firm][fid_i] += cov_h
                    if _note is not None:
                        _note(
                            group="2.8 Глубокорыхление", firm=firm,
                            crop=str(field_to_crop.get(fid_i) or ""),
                            field_id=fid_i, task_id=int(tid) if tid is not None else None,
                            machine_id=int(mid) if mid is not None else None, ha=cov_h,
                            season=str(t.get("season") or ""),
                        )

                hourly_all = item.get("covered_area_hourly") or []
                cov_item_total = sum(e[1] or 0 for e in hourly_all)
                cov_item_in    = _item_cov_before(cutoff, item)
                ratio = cov_item_in / cov_item_total if cov_item_total > 0 else 0.0
                track_area       += (item.get("covered_area_by_track") or 0) * ratio
                work_distance_km += ((item.get("work_distance") or 0) / 1000) * ratio

            if cov_firm_today <= 0:
                continue

            pm = per_firm_machine[firm]
            if mid not in pm:
                pm[mid] = {
                    "cov_total":        0.0,
                    "plow_area":        0.0,
                    "deep_area":        0.0,
                    "work_dur":         0.0,
                    "track_area":       0.0,
                    "work_distance_km": 0.0,
                    "implement_width":  0.0,
                    "speeds":           [],
                    "has_plow":         False,
                    "has_deep":         False,
                    "is_hired":         False,
                }

            pm[mid]["cov_total"]        += cov_firm_today
            pm[mid]["track_area"]       += track_area
            pm[mid]["work_distance_km"] += work_distance_km

            if wt in PLOW_WT_IDS:
                pm[mid]["plow_area"] += cov_firm_today
                pm[mid]["has_plow"]   = True
            elif wt in DEEP_WT_IDS:
                pm[mid]["deep_area"] += cov_firm_today
                pm[mid]["has_deep"]   = True

            work_dur  = t.get("work_duration") or 0
            road_move = t.get("movements_on_road_duration") or 0
            work_net  = max(work_dur - road_move, 0)
            if total_cov_in_window > 0:
                pm[mid]["work_dur"] += work_net * (cov_firm_today / total_cov_in_window)
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

    # -------------------------------------------------------------------------
    # Сезонная статистика:
    # sum(площадь за сезон) / sum(машин в дни с выработкой)
    # Раздельно для вспашки и глубокорыхления.
    # -------------------------------------------------------------------------
    season_daily_area:      dict[str, dict[str, float]]    = {f: defaultdict(float) for f in FIRM_ORDER}
    season_daily_machines:  dict[str, dict[str, set[int]]] = {f: defaultdict(set)   for f in FIRM_ORDER}
    season_daily_area_plow:     dict[str, dict[str, float]]    = {f: defaultdict(float) for f in FIRM_ORDER}
    season_daily_machines_plow: dict[str, dict[str, set[int]]] = {f: defaultdict(set)   for f in FIRM_ORDER}
    season_daily_area_deep:     dict[str, dict[str, float]]    = {f: defaultdict(float) for f in FIRM_ORDER}
    season_daily_machines_deep: dict[str, dict[str, set[int]]] = {f: defaultdict(set)   for f in FIRM_ORDER}
    # Суточная площадь по каждой машине за сезон — для корректного machine_days с порогом.
    # Площадь (season_fact_*) учитывается полностью; в machine_days идут только машины >= порога.
    season_mdc_plow: dict[str, dict[str, dict[int, float]]] = {
        f: defaultdict(lambda: defaultdict(float)) for f in FIRM_ORDER
    }
    season_mdc_deep: dict[str, dict[str, dict[int, float]]] = {
        f: defaultdict(lambda: defaultdict(float)) for f in FIRM_ORDER
    }

    # Сезонный факт для сводки (вспашка / глубокорыхление отдельно)
    season_fact_total: dict[str, float] = defaultdict(float)
    season_fact_plow:  dict[str, float] = defaultdict(float)
    season_fact_deep:  dict[str, float] = defaultdict(float)

    # Дата отчёта (конец окна смены 06:00) — как TODAY во 2_main; не day_start.date().
    report_day = cutoff.date()

    for t in tasks_season:
        if not _task_matches_required_year(t):
            continue
        mid = t.get("machine_id")
        wt  = t.get("work_type_id")
        end_dt = parse_dt(t.get("end_time"))
        if not end_dt:
            continue
        anchor = datetime(
            end_dt.year, end_dt.month, end_dt.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK
        )
        if end_dt < anchor:
            shift_d      = (end_dt - timedelta(days=1)).date()
            shift_cutoff = anchor
        else:
            shift_d      = end_dt.date()
            shift_cutoff = anchor + timedelta(days=1)
        # Вспашка: сезон «Выполнено» — только смены строго до даты отчёта (как 2_main).
        if wt in PLOW_WT_IDS and shift_d >= report_day:
            continue
        shift_date = shift_d.isoformat()

        items = mtfmi_map.get(t["id"], [])
        if not items:
            continue

        for item in items:
            fid = item.get("field_id")
            if not fid:
                continue
            fid_i = int(fid)
            if not _field_allowed(fid_i):
                continue
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue

            cov = 0.0
            for entry in (item.get("covered_area_hourly") or []):
                h = parse_dt(entry[0])
                if h and h < shift_cutoff:
                    cov += float(entry[1] or 0.0)
            if cov <= 0:
                continue

            season_daily_area[firm][shift_date] += cov
            if mid is not None:
                season_daily_machines[firm][shift_date].add(int(mid))

            season_fact_total[firm] += cov
            if wt in PLOW_WT_IDS:
                season_fact_plow[firm] += cov
                season_daily_area_plow[firm][shift_date] += cov
                if mid is not None:
                    season_daily_machines_plow[firm][shift_date].add(int(mid))
                    season_mdc_plow[firm][shift_date][int(mid)] += cov
            elif wt in DEEP_WT_IDS:
                season_fact_deep[firm] += cov
                season_daily_area_deep[firm][shift_date] += cov
                if mid is not None:
                    season_daily_machines_deep[firm][shift_date].add(int(mid))
                    season_mdc_deep[firm][shift_date][int(mid)] += cov

    # -------------------------------------------------------------------------
    # Собираем результат
    # -------------------------------------------------------------------------

    def _avg_season_for(
        da:  dict[str, float],
        dm:  dict[str, set[int]],
        mdc: dict[str, dict[int, float]],
        thr: float,
    ) -> tuple[float, int]:
        """season_sum / machine_days, где machine_days считает только машины
        с суточной площадью >= thr (площадь в season_sum учитывается полностью)."""
        s  = sum(da.values())
        md = sum(
            sum(1 for mid in dm.get(day, set()) if round(mdc.get(day, {}).get(mid, 0.0), 2) >= thr)
            for day, area in da.items()
            if area > 0
        )
        return (round(s / md, 1) if md else 0.0), md

    result: dict[str, dict] = {}

    for firm in FIRM_ORDER:
        pm = per_firm_machine[firm]

        _mdc_plow_f = season_mdc_plow[firm]
        _mdc_deep_f = season_mdc_deep[firm]
        _dm_total   = season_daily_machines[firm]
        _da_total   = season_daily_area[firm]

        def _md_total_firm() -> int:
            count = 0
            for day, area in _da_total.items():
                if area <= 0:
                    continue
                for mid in _dm_total.get(day, set()):
                    if (round(_mdc_plow_f.get(day, {}).get(mid, 0.0), 2) >= PLOW_COUNT_AREA_HA
                            or round(_mdc_deep_f.get(day, {}).get(mid, 0.0), 2) >= DEEP_COUNT_AREA_HA):
                        count += 1
            return count

        s_total  = sum(_da_total.values())
        md_total = _md_total_firm()
        avg_season = round(s_total / md_total, 1) if md_total else 0.0

        avg_season_plow, md_plow = _avg_season_for(
            season_daily_area_plow[firm], season_daily_machines_plow[firm],
            _mdc_plow_f, PLOW_COUNT_AREA_HA,
        )
        avg_season_deep, md_deep = _avg_season_for(
            season_daily_area_deep[firm], season_daily_machines_deep[firm],
            _mdc_deep_f, DEEP_COUNT_AREA_HA,
        )

        if not pm:
            result[firm] = _empty_stats()
            result[firm]["avg_season"]      = avg_season
            result[firm]["avg_season_plow"] = avg_season_plow
            result[firm]["avg_season_deep"] = avg_season_deep
            result[firm]["season_sum"]      = round(season_fact_total.get(firm, 0.0), 2)
            result[firm]["season_plow"]     = round(season_fact_plow.get(firm, 0.0), 2)
            result[firm]["season_deep"]     = round(season_fact_deep.get(firm, 0.0), 2)
            result[firm]["machine_days_total"] = md_total
            result[firm]["machine_days_plow"]  = md_plow
            result[firm]["machine_days_deep"]  = md_deep
            continue

        detail_own:   list[dict] = []
        detail_hired: list[dict] = []

        pm_for_detail = {
            mid: d for mid, d in pm.items()
            if _machine_meets_detail_threshold(d)
        }

        for mid, d in pm_for_detail.items():
            m     = machines_map.get(mid, {})
            group = m.get("machine_group_id")
            cov   = round(d["cov_total"], 2)
            secs  = d["work_dur"]
            rate  = round(cov / (secs / 3600), 1) if secs else 0.0
            speed = round(sum(d["speeds"]) / len(d["speeds"]) * 3.6) \
                    if d["speeds"] else 0

            w_real  = d["implement_width"]
            l_pole  = d["work_distance_km"]
            s_chist = d["track_area"]
            coef    = round(s_chist / l_pole / w_real * 10 * 100) \
                      if w_real and l_pole else 0

            row = {
                "Машина":                     m.get("name", f"machine_{mid}"),
                "Площадь, га":                cov,
                "в т.ч. вспашка, га":         round(d["plow_area"], 2),
                "в т.ч. глубокорыхление, га": round(d["deep_area"], 2),
                "Время (сек)":                secs,
                "Время на полях":             seconds_to_hms(secs),
                "Ср. выр-ка га/час":          rate,
                "Средняя скорость на полях км/ч": speed,
                "Коэффициент использования конструкционной ширины захвата, %": coef,
                "_has_plow":  d["has_plow"],
                "_has_deep":  d["has_deep"],
            }
            if _is_hired_machine_group(group):
                d["is_hired"] = True
                detail_hired.append(row)
            else:
                detail_own.append(row)

        detail_own.sort(key=lambda x:   -x["Площадь, га"])
        detail_hired.sort(key=lambda x: -x["Площадь, га"])

        plow_for_count = {
            mid: d for mid, d in pm.items()
            if round(d["plow_area"], 2) >= PLOW_COUNT_AREA_HA
        }
        deep_for_count = {
            mid: d for mid, d in pm.items()
            if round(d["deep_area"], 2) >= DEEP_COUNT_AREA_HA
        }
        plow_for_engine = {
            mid: d for mid, d in pm.items()
            if round(d["plow_area"], 2) >= PLOW_COUNT_AREA_HA
        }
        deep_for_engine = {
            mid: d for mid, d in pm.items()
            if round(d["deep_area"], 2) >= DEEP_COUNT_AREA_HA
        }

        plow_today = round(sum(today_plow_by_field[firm].values()), 2)
        deep_today = round(sum(today_deep_by_field[firm].values()), 2)
        area_today = round(plow_today + deep_today, 2)
        engine_plow  = sum(d["work_dur"] for d in plow_for_engine.values())
        engine_deep  = sum(d["work_dur"] for d in deep_for_engine.values())
        engine_today = sum(
            d["work_dur"]
            for mid, d in pm_for_detail.items()
        )

        plow_mach_ids   = set(plow_for_count)
        deep_mach_ids   = set(deep_for_count)
        active_mach_ids = plow_mach_ids | deep_mach_ids
        plow_hired_cnt = sum(
            1 for mid in plow_mach_ids
            if _is_hired_machine_group(machines_map.get(mid, {}).get("machine_group_id"))
        )
        deep_hired_cnt = sum(
            1 for mid in deep_mach_ids
            if _is_hired_machine_group(machines_map.get(mid, {}).get("machine_group_id"))
        )

        result[firm] = {
            "machines_today":      len(active_mach_ids),
            "hired_today":         len(detail_hired),
            "plow_machines_today": len(plow_mach_ids),
            "deep_machines_today": len(deep_mach_ids),
            "plow_hired_today":    plow_hired_cnt,
            "deep_hired_today":    deep_hired_cnt,
            "area_today":      round(area_today, 2),
            "plow_today":      round(plow_today, 2),
            "deep_today":      round(deep_today, 2),
            "engine_today":    round(engine_today  / 3600, 1),
            "engine_plow":     round(engine_plow   / 3600, 1),
            "engine_deep":     round(engine_deep   / 3600, 1),
            "avg_season":      avg_season,
            "avg_season_plow": avg_season_plow,
            "avg_season_deep": avg_season_deep,
            "season_sum":      round(season_fact_total.get(firm, 0.0), 2),
            "season_plow":     round(season_fact_plow.get(firm, 0.0), 2),
            "season_deep":     round(season_fact_deep.get(firm, 0.0), 2),
            "machine_days_total": md_total,
            "machine_days_plow":  md_plow,
            "machine_days_deep":  md_deep,
            "detail_own":      detail_own,
            "detail_hired":    detail_hired,
        }

    return result


def _empty_stats() -> dict:
    return {
        "machines_today":      0,
        "hired_today":         0,
        "plow_machines_today": 0,
        "deep_machines_today": 0,
        "plow_hired_today":    0,
        "deep_hired_today":    0,
        "area_today":      0.0,
        "plow_today":      0.0,
        "deep_today":      0.0,
        "engine_today":    0.0,
        "engine_plow":     0.0,
        "engine_deep":     0.0,
        "avg_season":      0.0,
        "avg_season_plow": 0.0,
        "avg_season_deep": 0.0,
        "season_sum":      0.0,
        "season_plow":     0.0,
        "season_deep":     0.0,
        "machine_days_total": 0,
        "machine_days_plow":  0,
        "machine_days_deep":  0,
        "detail_own":      [],
        "detail_hired":    [],
    }


# =============================================================================
# СТИЛИ И КОЛОНКИ
# =============================================================================

DETAIL_COLS = [
    "№", "Машина", "Площадь, га",
    "в т.ч. вспашка, га", "в т.ч. глубокорыхление, га",
    "Время на полях",
    "Ср. выр-ка га/час", "Средняя скорость на полях км/ч",
    "Коэффициент использования конструкционной ширины захвата, %",
]

SUMMARY_COLS = [
    "№ п/п",
    "Агрофирма",
    "Плановая выработка, га/сутки",
    "Количество задействованной техники",
    "План, га",
    "Факт всего, га",
    "в т.ч. факт вспашка всего, га",
    "в т.ч. факт глубокорыхление всего, га",
    "Обработано за сутки, га",
    "Доля, %",
    "Средняя выработка за сутки, га/сутки",
    "Средняя выработка по сезону, га/сутки",
    "Общее время работы на полях, ч",
]

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

# Колонки отдельного листа вспашки по одной культуре.
CROP_PLOW_COLS = [
    "Агрофирмы",
    "План, га",
    "Факт общий, га",
    "Остаток, га",
    "Остаток, %",
    "Факт за сутки, га",
    "Общее время работы на полях, час",
    "Среднее время эффективной работы на поле на 1 агрегат., час.",
    "Часовая выработка в расчете на 1 агрегат, га",
]


def _make_styles() -> dict:
    thin   = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    # Как в сводках 4/5/8: шапка 8093B3, строки данных белые, итог BDD7EE, секции на белом фоне.
    return {
        "border":        border,
        "align_c":       Alignment(horizontal="center", vertical="center", wrap_text=True),
        "align_l":       Alignment(horizontal="left", vertical="center", wrap_text=True),
        "align_c_nowrap": Alignment(horizontal="center", vertical="center", wrap_text=False),
        "align_l_nowrap": Alignment(horizontal="left", vertical="center", wrap_text=False),
        "fill_hdr":      PatternFill("solid", fgColor="8093B3"),
        "fill_own":      PatternFill("solid", fgColor="FFFFFF"),
        "fill_hired":    PatternFill("solid", fgColor="FFFFFF"),
        "fill_total":    PatternFill("solid", fgColor="BDD7EE"),
        "fill_sec_own":  PatternFill("solid", fgColor="FFFFFF"),
        "fill_sec_hire": PatternFill("solid", fgColor="FFFFFF"),
        "font_hdr":      Font(bold=True, color="FFFFFF", size=12.5),
        "font_data":     Font(size=12.5),
        "font_total":    Font(bold=True, size=12.5),
        "font_title":    Font(bold=True, size=13),
        "font_sec":      Font(bold=True, size=12, color="111827"),
    }


def _sc(cell, s, fill=None, font=None, align=None) -> None:
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
    # Кириллица в Excel по ширине колонки занимает больше «единиц», чем латиница.
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
    start_row:   int,
    end_row:     int,
    max_col:     int,
    min_height:  float = 20.0,
    line_height: float = 16.0,
    max_height:  float = 200.0,
) -> None:
    for r in range(start_row, end_row + 1):
        if ws.cell(row=r, column=1).value == DETAIL_MACHINES_MIN_AREA_NOTE:
            ws.row_dimensions[r].height = DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT
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
# ЗАПИСЬ ЛИСТА ДЕТАЛИЗАЦИИ
# =============================================================================

DETAIL_COL_WIDTHS_9 = [7, 42, 14, 22, 26, 18, 18, 22, 44]
DETAIL_PAGINATION_CFG_9 = DetailPaginationConfig(
    # Landscape PDF реально вмещает ~17 строк «СОБСТВЕННЫЕ» на одну страницу;
    # прежний лимит ~501pt резал секцию и дублировал заголовок на той же стр.
    page_body_height_pt=560.0,
    page_safe_reserve_pt=28.0,
    section_title_height_pt=28.0,
    header_height_pt=54.0,
    data_row_height_pt=24.0,
    data_row_max_height_pt=36.0,
    total_row_height_pt=28.0,
    section_gap_height_pt=12.0,
    single_page_slack_pt=48.0,
    note_height_pt=DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT,
    wrap_chars_per_unit=0.78,
)


def write_detail_sheet(
    wb:          openpyxl.Workbook,
    firm_name:   str,
    report_date: date,
    day_start:   datetime,
    cutoff:      datetime,
    stats:       dict,
) -> None:
    detail_own = list(stats.get("detail_own") or [])
    detail_hired = list(stats.get("detail_hired") or [])
    if not detail_own and not detail_hired:
        return

    ws = wb.create_sheet(title=firm_name[:28])
    s  = _make_styles()
    n  = len(DETAIL_COLS)
    cfg = DETAIL_PAGINATION_CFG_9

    for ci, w in enumerate(DETAIL_COL_WIDTHS_9, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    ctx = DetailPageContext(
        ws, n, DETAIL_COL_WIDTHS_9, cfg,
        on_new_page=lambda: _write_note_9(ctx),
    )

    def _write_note_9(page_ctx: DetailPageContext) -> None:
        page_ctx.ensure_space(cfg.note_height_pt)
        page_ctx.row = _write_detail_machines_min_area_note(ws, page_ctx.row, n)
        page_ctx.page_used_pt += cfg.note_height_pt

    _write_note_9(ctx)

    def _detail_vals(idx: int, rd: dict) -> list[object]:
        return [
            idx,
            rd["Машина"],
            rd["Площадь, га"],
            rd["в т.ч. вспашка, га"],
            rd["в т.ч. глубокорыхление, га"],
            rd["Время на полях"],
            rd["Ср. выр-ка га/час"],
            rd["Средняя скорость на полях км/ч"],
            rd["Коэффициент использования конструкционной ширины захвата, %"],
        ]

    firm_single_page = compute_firm_single_page(
        ctx, detail_own, detail_hired, _detail_vals, include_note=True,
    )
    same_firm_both_sections = bool(detail_own and detail_hired)

    def _make_section_writer(sec_fill, row_fill, rows_data: list[dict]):
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
                _sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_c"])
            ws.row_dimensions[hdr_row].height = cfg.header_height_pt
            ctx.row += 1
            ctx.page_used_pt += cfg.section_title_height_pt + cfg.header_height_pt

        def _data_row(r: int, vals: list[object]) -> float:
            for ci, val in enumerate(vals, start=1):
                c = ws.cell(row=r, column=ci, value=val)
                _sc(
                    c, s, fill=row_fill,
                    align=s["align_l"] if ci == 2 else s["align_c"],
                )
                if ci in {3, 4, 5}:
                    c.number_format = "# ##0.00"
                elif ci == 7:
                    c.number_format = "# ##0.0"
                elif ci in {8, 9}:
                    c.number_format = "# ##0"
            h = ctx.row_height(vals)
            ws.row_dimensions[r].height = h
            return h

        def _total_row() -> float:
            tc  = round(sum(rd["Площадь, га"] for rd in rows_data), 2)
            tpl = round(sum(rd["в т.ч. вспашка, га"] for rd in rows_data), 2)
            tdp = round(sum(rd["в т.ч. глубокорыхление, га"] for rd in rows_data), 2)
            ts  = sum(rd["Время (сек)"] for rd in rows_data)
            tr  = round(tc / (ts / 3600), 1) if ts else 0.0
            asp = round(sum(rd["Средняя скорость на полях км/ч"] for rd in rows_data) / len(rows_data))
            acf = round(
                sum(rd["Коэффициент использования конструкционной ширины захвата, %"]
                    for rd in rows_data) / len(rows_data)
            )
            r = ctx.row
            for ci, val in enumerate(
                ["Итого", "", tc, tpl, tdp, seconds_to_hms(ts), tr, asp, acf], start=1
            ):
                c = ws.cell(row=r, column=ci, value=val)
                _sc(c, s, fill=s["fill_total"], font=s["font_total"], align=s["align_c"])
                if ci in {3, 4, 5}:
                    c.number_format = "# ##0.00"
                elif ci == 7:
                    c.number_format = "# ##0.0"
                elif ci in {8, 9}:
                    c.number_format = "# ##0"
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


# =============================================================================
# СВОДНЫЙ ЛИСТ
# =============================================================================

def _build_summary_rows(
    firm_names:  list[str],
    all_details: dict[str, dict],
) -> list[dict]:
    rows = []
    for i, firm_name in enumerate(firm_names, start=1):
        st = all_details[firm_name]
        _, plan_day, plan_season, own_count = PLAN_DAY_PLOW, PLAN_DAY_PLOW, PLAN_SEASON_PLOW.get(firm_name, 0), PLOW_OWN_COUNT.get(firm_name, 0)

        n_mach     = st["machines_today"]
        area_today = st["area_today"]
        plow_today = st["plow_today"]
        deep_today = st["deep_today"]
        eng        = st["engine_today"]
        avg_s      = float(st.get("avg_season", 0.0) or 0.0)
        fact_total = st["season_sum"]
        fact_plow  = st["season_plow"]
        fact_deep  = st["season_deep"]

        doля = round(fact_total / plan_season * 100, 0) if plan_season else 0.0

        rows.append({
            "№ п/п":                                        i,
            "Агрофирма":                                    firm_name,
            "Плановая выработка, га/сутки":                 plan_day,
            "Количество задействованной техники":            n_mach,
            "План, га":                                     plan_season,
            "Факт всего, га":                               round(fact_total, 2),
            "в т.ч. факт вспашка всего, га":               round(fact_plow, 2),
            "в т.ч. факт глубокорыхление всего, га":       round(fact_deep, 2),
            "Обработано за сутки, га":                      area_today,
            "Доля, %":                                      int(doля),
            "Средняя выработка за сутки, га/сутки":
                round(area_today / n_mach, 1) if n_mach else 0.0,
            "Средняя выработка по сезону, га/сутки":        avg_s,
            "Общее время работы на полях, ч":               eng,
        })

    tm   = sum(r["Количество задействованной техники"]          for r in rows)
    tps  = sum(r["План, га"]                                    for r in rows)
    tft  = round(sum(r["Факт всего, га"]                        for r in rows), 2)
    tfpl = round(sum(r["в т.ч. факт вспашка всего, га"]        for r in rows), 2)
    tfdp = round(sum(r["в т.ч. факт глубокорыхление всего, га"] for r in rows), 2)
    tad  = round(sum(r["Обработано за сутки, га"]               for r in rows), 2)
    te   = round(sum(r["Общее время работы на полях, ч"]        for r in rows), 1)
    tdol = round(tft / tps * 100, 0) if tps else 0.0

    # Средняя по сезону — как в эталоне: суммарная площадь / суммарные машино-дни
    total_season_area  = sum(all_details[n].get("season_sum", 0.0)          for n in firm_names)
    total_machine_days = sum(all_details[n].get("machine_days_total", 0)    for n in firm_names)
    avg_season_total   = round(total_season_area / total_machine_days, 1) if total_machine_days else 0.0

    rows.append({
        "№ п/п":                                        "",
        "Агрофирма":                                    "Итого",
        "Плановая выработка, га/сутки":
            rows[0]["Плановая выработка, га/сутки"] if rows else 0,
        "Количество задействованной техники":            tm,
        "План, га":                                     tps,
        "Факт всего, га":                               tft,
        "в т.ч. факт вспашка всего, га":               tfpl,
        "в т.ч. факт глубокорыхление всего, га":       tfdp,
        "Обработано за сутки, га":                      tad,
        "Доля, %":                                      int(tdol),
        "Средняя выработка за сутки, га/сутки":
            round(tad / tm, 1) if tm else 0.0,
        "Средняя выработка по сезону, га/сутки":  avg_season_total,
        "Общее время работы на полях, ч":               te,
    })
    return rows


def write_summary_sheet(
    wb:          openpyxl.Workbook,
    report_date: date,
    day_start:   datetime,
    cutoff:      datetime,
    firm_names:  list[str],
    all_details: dict[str, dict],
) -> None:
    ws = wb.create_sheet(title="Сводка", index=0)
    s  = _make_styles()
    n  = len(SUMMARY_COLS)

    hdr_row = 1
    for ci, col in enumerate(SUMMARY_COLS, start=1):
        c = ws.cell(row=hdr_row, column=ci, value=col)
        _sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_c"])

    rows = _build_summary_rows(firm_names, all_details)
    for ri, row_data in enumerate(rows, start=hdr_row + 1):
        is_total = row_data.get("Агрофирма") == "Итого"
        fill     = s["fill_total"] if is_total else s["fill_own"]
        font     = s["font_total"] if is_total else None
        for ci, col in enumerate(SUMMARY_COLS, start=1):
            c = ws.cell(row=ri, column=ci, value=row_data.get(col, ""))
            _sc(c, s, fill=fill, font=font,
                align=s["align_l"] if ci == 2 else s["align_c"])
            if ci in {3, 4, 5, 10}:
                c.number_format = "# ##0"
            elif ci in {6, 7, 8, 9}:
                c.number_format = "# ##0.00"
            elif ci in {11, 12, 13}:
                c.number_format = "# ##0.0"
        lbl = str(row_data.get("Агрофирма") or "")
        ws.row_dimensions[ri].height = 32 if len(lbl) > 34 else 26

    # Ширины под длинные заголовки (в т.ч. «глубокорыхление», средние за сутки/сезон).
    _SUMMARY_WIDTHS = {
        1: 9,
        2: 36,
        3: 22,
        4: 16,
        5: 14,
        6: 16,
        7: 20,
        8: 26,
        9: 20,
        10: 10,
        11: 24,
        12: 26,
        13: 18,
    }
    for ci in range(1, n + 1):
        ws.column_dimensions[get_column_letter(ci)].width = _SUMMARY_WIDTHS.get(ci, 20)

    _auto_fit_row_heights(ws, start_row=1, end_row=hdr_row + len(rows), max_col=n)
    hr = ws.row_dimensions[hdr_row].height or 0
    ws.row_dimensions[hdr_row].height = max(hr, 78.0)


def write_crop_plow_sheet(
    wb: openpyxl.Workbook,
    crop_name: str,
    sheet_title: str,
    by_firm_crop: dict[str, dict[str, dict]],
    *,
    sheet_index: int = 1,
) -> None:
    """Отдельный лист вспашки по одной культуре (строка = агрофирма)."""
    styles = _make_styles()
    n = len(CROP_PLOW_COLS)
    ws = wb.create_sheet(title=sheet_title[:31], index=sheet_index)
    row = 1

    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n)
    c0 = ws.cell(row=row, column=1, value=f"2.4 Вспашка — {crop_name}")
    c0.font = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
    c0.fill = styles["fill_hdr"]
    c0.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    c0.border = styles["border"]
    ws.row_dimensions[row].height = 28
    row += 1

    for ci, col_name in enumerate(CROP_PLOW_COLS, start=1):
        cc = ws.cell(row=row, column=ci, value=col_name)
        _sc(
            cc, styles,
            fill=styles["fill_hdr"],
            font=Font(bold=True, color="FFFFFF", size=12),
            align=Alignment(horizontal="center", vertical="center", wrap_text=True),
        )
    ws.row_dimensions[row].height = 66
    row += 1

    num_fmt_by_col = {
        2: "# ##0",
        3: "# ##0",
        4: "# ##0",
        5: "# ##0.0",
        6: "# ##0.00",
        7: "# ##0.00",
        8: "# ##0.00",
        9: "# ##0.0",
    }

    def _safe_float(v: object) -> float:
        try:
            x = float(v or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return x if math.isfinite(x) else 0.0

    def _safe_hhmm(hours_val: object) -> str:
        h = _safe_float(hours_val)
        return seconds_to_hms(max(0, int(round(h * 3600))))

    t_plan = t_fact_season = t_fact_today = t_hours = 0.0
    t_mach = 0

    for firm in FIRM_ORDER:
        firm_stats = by_firm_crop.get(firm, {}).get(crop_name, _empty_crop_plow_stats())
        plan = _plow_plan_ha(firm, crop_name)
        fact_season = _safe_float(firm_stats.get("season_plow", 0.0))
        fact_today = _safe_float(firm_stats.get("plow_today", 0.0))
        hours_today = _safe_float(firm_stats.get("engine_plow", 0.0))
        n_mach = int(_safe_float(firm_stats.get("plow_machines_today", 0)))

        ostatok_ga = round(plan - fact_season, 2)
        ostatok_pct = round(ostatok_ga / plan * 100, 1) if plan else 0.0
        eff_h = round(hours_today / n_mach, 4) if n_mach else 0.0
        rate_h = round(fact_today / hours_today, 1) if hours_today else 0.0

        vals = [
            firm,
            plan,
            round(fact_season, 2),
            ostatok_ga,
            ostatok_pct,
            round(fact_today, 2),
            _safe_hhmm(hours_today),
            _safe_hhmm(eff_h),
            rate_h,
        ]
        for ci, v in enumerate(vals, start=1):
            cell = ws.cell(row=row, column=ci, value=v)
            cell.number_format = num_fmt_by_col.get(ci, "General")
            _sc(
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
    tot_ostatok_pct = round(tot_ostatok_ga / t_plan * 100, 1) if t_plan else 0.0
    tot_eff_h = round(t_hours / t_mach, 4) if t_mach else 0.0
    tot_rate_h = round(t_fact_today / t_hours, 1) if t_hours else 0.0

    totals = [
        "Итого",
        round(t_plan, 2),
        round(t_fact_season, 2),
        tot_ostatok_ga,
        tot_ostatok_pct,
        round(t_fact_today, 2),
        _safe_hhmm(t_hours),
        _safe_hhmm(tot_eff_h),
        tot_rate_h,
    ]
    for ci, v in enumerate(totals, start=1):
        cell = ws.cell(row=row, column=ci, value=v)
        cell.number_format = num_fmt_by_col.get(ci, "General")
        _sc(
            cell, styles,
            fill=PatternFill("solid", fgColor="BDD7EE"),
            font=Font(bold=True, size=12),
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


def write_all_crop_plow_sheets(
    wb: openpyxl.Workbook,
    by_firm_crop: dict[str, dict[str, dict]],
    *,
    start_index: int = 1,
) -> None:
    """Отдельный лист для каждой культуры из PLOW_SEASON_CROPS (план или факт)."""
    used_titles: set[str] = set(wb.sheetnames)
    for i, crop in enumerate(_plow_crops_for_report(by_firm_crop)):
        write_crop_plow_sheet(
            wb,
            crop,
            _plow_sheet_title(crop, used_titles),
            by_firm_crop,
            sheet_index=start_index + i,
        )


def write_tillage_combined_sheet(
    wb: openpyxl.Workbook,
    all_details: dict[str, dict],
) -> None:
    """
    Первый лист 9-й сводки:
    два отдельных блока как в 1-й сводке — 2.4 Вспашка и 2.8 Глубокорыхление.
    """
    ws = wb.create_sheet(title="Сводка", index=0)
    styles = _make_styles()
    n = len(PERF_COLS)
    row = 1

    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n)
    c0 = ws.cell(
        row=row,
        column=1,
        value=f"{WORK_TYPE_TITLE} — 2.4 Вспашка, 2.8 Глубокорыхление",
    )
    c0.font = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
    c0.fill = styles["fill_hdr"]
    c0.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    c0.border = styles["border"]
    ws.row_dimensions[row].height = 28
    row += 1

    groups = [
        ("2.4 Вспашка",         "plow_today", PLAN_DAY_PLOW, "plow_machines_today", "plow_hired_today", PLOW_OWN_COUNT, "avg_season_plow", "engine_plow"),
        ("2.8 Глубокорыхление", "deep_today",  PLAN_DAY_DEEP, "deep_machines_today", "deep_hired_today", DEEP_OWN_COUNT, "avg_season_deep", "engine_deep"),
    ]

    num_fmt_by_col = {
        2: "# ##0",
        3: "# ##0",
        4: "# ##0.00",
        5: "# ##0.00",
        6: "# ##0.00",
        9: "# ##0.0",
        10: "# ##0",
        11: "# ##0",
    }

    def _safe_float(v: object) -> float:
        try:
            x = float(v or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return x if math.isfinite(x) else 0.0

    def _safe_hhmm(hours_val: object) -> str:
        h = _safe_float(hours_val)
        return seconds_to_hms(max(0, int(round(h * 3600))))

    for group_name, area_key, plan_day, mach_key, hired_key, own_count_dict, avg_key, engine_key in groups:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n)
        c = ws.cell(row=row, column=1, value=group_name)
        c.font = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
        c.fill = styles["fill_hdr"]
        c.alignment = Alignment(horizontal="left", vertical="center")
        c.border = styles["border"]
        ws.row_dimensions[row].height = 22
        row += 1

        for ci, col_name in enumerate(PERF_COLS, start=1):
            cc = ws.cell(row=row, column=ci, value=col_name)
            _sc(cc, styles, fill=styles["fill_hdr"], font=Font(bold=True, color="FFFFFF", size=12), align=Alignment(horizontal="center", vertical="center", wrap_text=True))
        ws.row_dimensions[row].height = 66
        row += 1

        t_mach = t_area = t_hours = 0.0
        t_season_area = 0.0
        t_machine_days = 0
        t_avail = t_hired = 0

        for firm in FIRM_ORDER:
            firm_stats = all_details.get(firm, _empty_stats())
            n_mach = int(_safe_float(firm_stats.get(mach_key, 0)))
            area = _safe_float(firm_stats.get(area_key, 0.0))
            hours = _safe_float(firm_stats.get(engine_key, 0.0))
            avg_s = _safe_float(firm_stats.get(avg_key, 0.0))
            hired = int(_safe_float(firm_stats.get(hired_key, 0)))
            own_count = own_count_dict.get(firm, 0)
            # Для итого по сезону — берём сырые season_sum и machine_days
            if avg_key == "avg_season_plow":
                firm_season_area  = _safe_float(firm_stats.get("season_plow", 0.0))
                firm_machine_days = int(firm_stats.get("machine_days_plow", 0) or 0)
            elif avg_key == "avg_season_deep":
                firm_season_area  = _safe_float(firm_stats.get("season_deep", 0.0))
                firm_machine_days = int(firm_stats.get("machine_days_deep", 0) or 0)
            else:
                firm_season_area  = _safe_float(firm_stats.get("season_sum", 0.0))
                firm_machine_days = int(firm_stats.get("machine_days_total", 0) or 0)
            rate = round(area / n_mach, 2) if n_mach else 0.0
            eff_h = round(hours / n_mach, 4) if n_mach else 0.0
            vals = [
                firm,
                plan_day,
                n_mach,
                area,
                rate,
                avg_s,
                _safe_hhmm(hours),
                _safe_hhmm(eff_h),
                round(area / hours, 1) if hours else 0.0,
                own_count,
                hired,
            ]
            for ci, v in enumerate(vals, start=1):
                bg = "FFFFFF"
                cell = ws.cell(row=row, column=ci, value=v)
                cell.number_format = num_fmt_by_col.get(ci, "General")
                _sc(
                    cell,
                    styles,
                    fill=PatternFill("solid", fgColor=bg),
                    font=Font(size=12),
                    align=Alignment(horizontal="left" if ci == 1 else "center", vertical="center", wrap_text=True),
                )
            t_mach += n_mach
            t_area += area
            t_hours += hours
            t_avail += own_count
            t_hired += hired
            t_season_area  += firm_season_area
            t_machine_days += firm_machine_days
            row += 1

        avg_total = round(t_season_area / t_machine_days, 1) if t_machine_days else 0.0
        tot_rate = round(t_area / t_mach, 2) if t_mach else 0.0
        tot_eff_h = round(t_hours / t_mach, 4) if t_mach else 0.0
        totals = [
            "Итого",
            plan_day,
            int(t_mach),
            round(t_area, 2),
            tot_rate,
            avg_total,
            _safe_hhmm(t_hours),
            _safe_hhmm(tot_eff_h),
            round(t_area / t_hours, 1) if t_hours else 0.0,
            t_avail,
            t_hired,
        ]
        for ci, v in enumerate(totals, start=1):
            bg = "BDD7EE"
            cell = ws.cell(row=row, column=ci, value=v)
            cell.number_format = num_fmt_by_col.get(ci, "General")
            _sc(
                cell,
                styles,
                fill=PatternFill("solid", fgColor=bg),
                font=Font(bold=True, size=12),
                align=Alignment(
                    horizontal="left" if ci == 1 else "center",
                    vertical="center",
                    wrap_text=False,
                ),
            )
        row += 2

    ws.column_dimensions["A"].width = 30
    for ci in range(2, n + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 18


# =============================================================================
# ПОСТРОЕНИЕ ОТЧЁТА
# =============================================================================

async def build_report_async(report_date: date = REPORT_DATE) -> None:
    global machines_map

    print(f"Отчёт: {WORK_TYPE_TITLE}")
    print(f"Дата: {report_date}  |  сезон вспашки (task.season): {SEASON_YEAR}  |  fix={SVODKA9_PLOW_SEASON_FIX}")
    print(f"Период: с {SEASON_START}  |  смена {DAY_START_HOUR}:00–{DAY_END_HOUR}:00")

    day_start, cutoff = compute_time_range(report_date, DAY_START_HOUR, DAY_END_HOUR)
    season_start = datetime(
        SEASON_START.year, SEASON_START.month, SEASON_START.day, tzinfo=TZ_MSK
    )

    print(f"Отчёт '{WORK_TYPE_TITLE}'  |  Дата: {report_date}")
    print(f"  Суточный диапазон: {day_start} — {cutoff}")
    print(f"  Сезон (средняя выработка): с {SEASON_START.strftime('%d.%m.%Y')}\n")

    period_start = date(report_date.year, 3, 1)
    field_to_crop, available_field_ids = load_field_to_crop_and_available(
        report_date, period_start,
    )
    crops_map = {
        c["id"]: c.get("name") or c.get("title", "")
        for c in get_all_from_id("crops")
    }
    field_to_crop_plow = _build_field_to_crop_plow(crops_map)

    firm_names = list(FIRM_ORDER)
    headers    = {"X-User-Api-Token": API_TOKEN}
    semaphore  = asyncio.Semaphore(SEMAPHORE_LIMIT)

    print(f"\n  Загружаем задания и mtfmi (все агрофирмы, один проход)...")
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    async with httpx.AsyncClient(headers=headers, timeout=60, limits=pool) as client:
        today_task_ids, tasks_season, mtfmi_map = await fetch_all_tasks_and_mtfmi(
            client, semaphore, day_start, cutoff, season_start, ALL_WT_IDS,
        )

        field_ids, machine_ids, implement_ids = collect_report_resource_ids(
            tasks_season, mtfmi_map,
        )
        print(
            f"  Ресурсы отчёта: полей={len(field_ids)}, "
            f"машин={len(machine_ids)}, орудий={len(implement_ids)}"
        )

        field_to_firm, machines_map, implements_dict = await asyncio.gather(
            load_field_to_firm_async(client, semaphore, field_ids),
            load_machines_map_async(client, semaphore, machine_ids),
            load_implements_dict_async(client, semaphore, implement_ids),
        )
    print(f"  Машин задействовано: {len(machines_map)}")

    print("  Распределяем задания по агрофирмам через поля...")
    all_details = build_all_firm_stats(
        today_task_ids,
        tasks_season,
        mtfmi_map,
        season_start,
        day_start,
        cutoff,
        field_to_firm,
        field_to_crop,
        implements_dict,
        available_field_ids=None,
    )

    plow_by_crop = build_plow_stats_by_crop(
        today_task_ids,
        tasks_season,
        mtfmi_map,
        cutoff,
        field_to_firm,
        field_to_crop_plow,
    )
    crop_sheets = _plow_crops_for_report(plow_by_crop)
    print(f"  Листов по культурам вспашки: {len(crop_sheets)} — {', '.join(crop_sheets)}")

    # Консольный вывод
    summary_rows = _build_summary_rows(firm_names, all_details)
    df = pd.DataFrame(summary_rows)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 300)
    pd.set_option("display.float_format", "{:.2f}".format)
    print("\nСВОДКА:")
    print(df.to_string(index=False))

    output_file = f"report_plowing_{report_date}.xlsx"
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    write_tillage_combined_sheet(wb, all_details)
    write_all_crop_plow_sheets(wb, plow_by_crop, start_index=1)
    for firm_name in firm_names:
        write_detail_sheet(wb, firm_name, report_date, day_start, cutoff, all_details[firm_name])

    wb.save(output_file)
    print(f"\nСохранено: {output_file}")


def build_report(report_date: date = REPORT_DATE) -> None:
    asyncio.run(build_report_async(report_date))


if __name__ == "__main__":
    build_report()