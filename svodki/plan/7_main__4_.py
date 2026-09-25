# 11.06.2026
"""
26 Cropwise Operations — отчёт "Дискование"
=========================================
wt=51,54,59,60,61

Логика:
- Агрофирма определяется по ПОЛЮ из mtfmi (field → field_group → group_folder → фирма),
  а НЕ по привязке машины к региону.
- Площадь: covered_area_hourly из machine_task_field_mapping_items.
  Метка H означает работу в [H, H+1), поэтому фильтр: h < cutoff.
- DAY_START = report_date - 1 день + DAY_START_HOUR (по умолчанию 06:00)
- CUTOFF    = report_date + DAY_END_HOUR             (по умолчанию 06:00)
- Время:   work_duration - movements_on_road_duration, масштабируется по доле площади
- Коэффициент ширины: covered_area_by_track / work_distance_km / implement_width * 10 * 100
  track и distance масштабируются пропорционально доле часов в диапазоне
- implement_width берётся из implements через implement_id задачи
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

import httpx
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import pandas as pd
import requests
from excel_number_fmt import NF_DEC1, NF_DEC2, NF_INT, summary_cell_value_and_format
from excel_style import pattern_fill_low_output_vs_plan, summary_rate_below_plan_fill
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
    FIRM_TO_REGION,
    FOLDER_TO_FIRM,
    FIELD_NAME_FORCE_RULES,
    HIRED_GROUP_ID,
    AVAILABLE_OWN,
    PLAN_RATE_PER_GROUP,
    WORK_GROUPS,
    SKIP_CROPS,
)

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================
REPORT_DATE = date(2026, 9, 4)
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

SEASON_START = date(2026, 3, 15)
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

WORK_TYPE_TITLE = "2.9 Дискование"
_WORK_TYPE_KEY  = "2.9 Дискование"

# ALL_WT_IDS выводится из WORK_GROUPS (plans), локальный WORK_TYPES не нужен
ALL_WT_IDS: set[int] = {
    wt_id
    for group_name, wt_ids in WORK_GROUPS
    if group_name == _WORK_TYPE_KEY
    for wt_id in wt_ids
}

# название → (region_id, план га/сутки, собств. агрегатов в наличии)
# FIRM_TO_REGION из plans заменяет локальный _REGION_IDS
AGROFIRMS: dict[str, tuple[int, float, int]] = {
    firm: (
        FIRM_TO_REGION[firm],
        PLAN_RATE_PER_GROUP.get(_WORK_TYPE_KEY, 200.0),
        AVAILABLE_OWN.get(_WORK_TYPE_KEY, {}).get(firm, 0),
    )
    for firm in FIRM_TO_FOLDER
}

SEMAPHORE_LIMIT = 40
MIN_DETAIL_AREA_HA = 0.5
# Машины с площадью ниже этого порога не учитываются в счётчиках техники,
# времени работы и детальных таблицах, но их площадь суммируется в общий объём.
MIN_COUNT_AREA_HA = 10.0
SUMMARY_MACHINES_NOTE = (
    "В сводных данных отображается техника, отработавшая больше 10 га"
)

# Глобальный кэш машин
machines_map: dict[int, dict] = {}


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


def load_field_to_firm() -> dict[int, str]:
    print("  Загружаем fields / field_groups / group_folders...")
    fields_all = get_all_from_id("fields")
    fgs_all    = get_all_from_id("field_groups")
    gfs_all    = get_all_from_id("group_folders")
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


def _operational_day_key(dt: datetime) -> str:
    """
    Дата "операционного" дня для режима 06:00–06:00.
    00:00..05:59 относятся к предыдущему дню.
    """
    return (dt - timedelta(hours=DAY_START_HOUR)).date().isoformat()


def _as_int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


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
    """
    Загружает задания за сутки и за сезон batch-запросами по work_type_id.
    Агрофирма будет определена позже по полю из mtfmi.
    """
    from machine_tasks_batch_fetch import fetch_machine_tasks_by_wt_async
    from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks

    day_start_str    = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str       = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    season_start_str = season_start.strftime("%Y-%m-%dT%H:%M:%S")

    tasks_today, tasks_season = await asyncio.gather(
        fetch_machine_tasks_by_wt_async(
            fetch_all_pages, client, BASE_URL, work_type_ids,
            {"end_time_gt_eq": day_start_str, "end_time_lt": cutoff_str},
            semaphore,
        ),
        fetch_machine_tasks_by_wt_async(
            fetch_all_pages, client, BASE_URL, work_type_ids,
            {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_str},
            semaphore,
        ),
    )

    try:
        _sdir = Path(__file__).resolve().parent / "utils" / "season"
        if not _sdir.is_dir():
            _sdir = Path(__file__).resolve().parents[1] / "svodki" / "plan" / "utils" / "season"
        if _sdir.is_dir() and str(_sdir) not in sys.path:
            sys.path.insert(0, str(_sdir))
        from season_unified import filter_tasks_by_group_seasons as _filter_seasons

        tasks_today = _filter_seasons(tasks_today, _WORK_TYPE_KEY, REPORT_DATE)
        tasks_season = _filter_seasons(tasks_season, _WORK_TYPE_KEY, REPORT_DATE)
    except Exception:
        pass

    print(f"  Заданий за сутки: {len(tasks_today)}, за сезон (с {SEASON_START.strftime('%d.%m.%Y')}): {len(tasks_season)}")

    all_task_ids = list({t["id"] for t in tasks_today + tasks_season})
    if all_task_ids:
        print(f"  Загружаем mtfmi для {len(all_task_ids)} заданий сезона...")
        mtfmi_map = await fetch_mtfmi_map_for_tasks(
            client, semaphore, BASE_URL, all_task_ids, log_prefix="[r7] ",
        )
    else:
        mtfmi_map = {}

    return tasks_today, tasks_season, mtfmi_map


# =============================================================================
# ПОДСЧЁТ — агрофирма определяется по полю
# =============================================================================

def _empty_stats() -> dict:
    return {
        "machines_today":      0,
        "area_today":          0.0,
        "engine_today":        0.0,
        "avg_season":          0.0,
        "season_sum":          0.0,
        "season_machine_days": 0,
        "detail_own":          [],
        "detail_hired":        [],
    }


def build_all_firm_stats(
    tasks_today:      list[dict],
    tasks_season:     list[dict],
    mtfmi_map:        dict[int, list[dict]],
    season_start:     datetime,
    day_start:        datetime,
    cutoff:           datetime,
    field_to_firm:    dict[int, str],
    implements_dict:  dict[int, dict],
) -> dict[str, dict]:
    """
    Распределяет задания по агрофирмам на основе поля из mtfmi.
    Возвращает: firm_name → stats
    """
    # per_firm_machine[firm][machine_id] → агрегированные данные
    per_firm_machine: dict[str, dict[int, dict]] = {
        firm: {} for firm in AGROFIRMS
    }

    for t in tasks_today:
        mid = _as_int(t.get("machine_id"))
        items = mtfmi_map.get(t["id"], [])

        # Группируем items по агрофирме через поле
        firm_items: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            fid_i = _as_int(item.get("field_id"))
            if fid_i is None:
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
                cov_item_total = sum(e[1] or 0 for e in hourly)
                cov_item_in    = 0.0
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and h < cutoff:
                        cov_item_in += entry[1] or 0
                cov += cov_item_in
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
    # средняя выработка по сезону = sum(площадь за дни) / sum(машин, работавших в эти дни)
    # Площадь распределяем по фирмам только по фактическим mtfmi-item конкретной задачи.
    season_daily_area: dict[str, dict[str, float]] = {f: defaultdict(float) for f in AGROFIRMS}
    season_daily_machines: dict[str, dict[str, set[int]]] = {
        f: defaultdict(set) for f in AGROFIRMS
    }

    for t in tasks_season:
        mid = _as_int(t.get("machine_id"))
        end_dt = parse_dt(t.get("end_time"))
        if not end_dt:
            continue
        # Логика смены: смена дня D = [D 06:00, D+1 06:00)
        anchor = datetime(
            end_dt.year, end_dt.month, end_dt.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK
        )
        if end_dt < anchor:
            shift_date   = (end_dt - timedelta(days=1)).date().isoformat()
            shift_cutoff = anchor
        else:
            shift_date   = end_dt.date().isoformat()
            shift_cutoff = anchor + timedelta(days=1)

        # Исключаем текущий день — как в эталоне
        upper = (cutoff.date() - timedelta(days=1)).isoformat()
        if shift_date > upper:
            continue

        items = mtfmi_map.get(t["id"], [])
        if not items:
            continue

        for item in items:
            fid_i = _as_int(item.get("field_id"))
            if fid_i is None:
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
            if item_cov <= 0.5:
                continue

            season_daily_area[firm][shift_date]         += item_cov
            if mid is not None:
                season_daily_machines[firm][shift_date].add(mid)

    # Собираем результат
    result: dict[str, dict] = {}

    # upper уже вычислен выше, пересчитаем для использования ниже
    upper = (cutoff.date() - timedelta(days=1)).isoformat()

    for firm in AGROFIRMS:
        pm = per_firm_machine[firm]
        da = season_daily_area[firm]

        season_sum   = sum(v for d, v in da.items() if d <= upper)
        machine_days = sum(
            len(season_daily_machines[firm].get(day, set()))
            for day, area in da.items()
            if area > 0 and day <= upper
        )
        avg_season  = round(season_sum / machine_days, 1) if machine_days else 0.0

        pm_filtered = {
            mid: d for mid, d in pm.items()
            if round(d["cov_total"], 2) >= MIN_DETAIL_AREA_HA
        }

        # Машины с площадью >= MIN_COUNT_AREA_HA учитываются в счётчиках,
        # времени и деталях. Машины ниже порога — только в суммарной площади.
        pm_counted = {
            mid: d for mid, d in pm_filtered.items()
            if round(d["cov_total"], 2) >= MIN_COUNT_AREA_HA
        }

        if not pm_filtered:
            stats = _empty_stats()
            stats["avg_season"]          = avg_season
            stats["season_sum"]          = season_sum
            stats["season_machine_days"] = machine_days
            result[firm] = stats
            continue

        detail_own:   list[dict] = []
        detail_hired: list[dict] = []

        for mid, d in pm_counted.items():
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

            machine_name = m.get("name") or m.get("title") or m.get("state_number") or f"machine_{mid}"
            row = {
                "Машина":        machine_name,
                "Площадь, га":   cov,
                "Время (сек)":   secs,
                "Время на полях": seconds_to_hms(secs),
                "Ср. выр-ка га/час": rate,
                "Средняя скорость на полях км/ч": speed,
                "Коэффициент использования конструкционной ширины захвата, %": coef,
            }
            if group == HIRED_GROUP_ID:
                detail_hired.append(row)
            else:
                detail_own.append(row)

        detail_own.sort(key=lambda x:   -x["Площадь, га"])
        detail_hired.sort(key=lambda x: -x["Площадь, га"])

        # Площадь суммируется по ВСЕМ машинам >= MIN_DETAIL_AREA_HA (включая < MIN_COUNT_AREA_HA).
        # Счётчики техники и время — только по машинам >= MIN_COUNT_AREA_HA.
        area_today   = sum(d["cov_total"] for d in pm_filtered.values())
        engine_today = sum(d["work_dur"]  for d in pm_counted.values())

        result[firm] = {
            "machines_today":      len(pm_counted),
            "area_today":          round(area_today, 2),
            "engine_today":        round(engine_today / 3600, 1),
            "avg_season":          avg_season,
            "season_sum":          season_sum,
            "season_machine_days": machine_days,
            "detail_own":          detail_own,
            "detail_hired":        detail_hired,
        }

    return result


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
        "align_c":       Alignment(horizontal="center", vertical="center",
                                    wrap_text=True),
        "align_l":       Alignment(vertical="center", wrap_text=True),
        "align_hdr":     Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=True,
        ),
        # Как в сводках 4–6: сиреневый заголовок, белые строки, без «залитых» секций.
        "fill_hdr":      PatternFill("solid", fgColor="8093B3"),
        "fill_own":      PatternFill("solid", fgColor="FFFFFF"),
        "fill_hired":    PatternFill("solid", fgColor="FFFFFF"),
        "fill_total":    PatternFill("solid", fgColor="BDD7EE"),
        "fill_sec_own":  PatternFill("solid", fgColor="FFFFFF"),
        "fill_sec_hire": PatternFill("solid", fgColor="FFFFFF"),
        "font_hdr":      Font(bold=True, color="FFFFFF"),
        "font_total":    Font(bold=True),
        "font_title":    Font(bold=True, size=13),
        "font_sec":      Font(bold=True, size=12, color="111827"),
        "fill_alert_prod": pattern_fill_low_output_vs_plan(),
    }


def _align_summary_data(ci: int, col_key: str, raw_val: object, s: dict) -> Alignment:
    if str(raw_val).strip() == "Итого" and col_key == "Агрофирма":
        return Alignment(vertical="center", horizontal="left", wrap_text=False)
    if ci == 2:
        return s["align_l"]
    return s["align_c"]


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


def _summary_display_value(col_key: str, raw):
    if col_key in {
        "Общее время работы на полях, машино-часы за сутки",
        "Среднее время эффективной работы на поле на 1 агрегат., час.",
    }:
        try:
            return seconds_to_hms(float(raw) * 3600.0), None
        except (TypeError, ValueError):
            return raw, None
    return raw, None


def _estimate_wrapped_lines(value: object, col_width: float | None) -> int:
    text = str(value or "").strip()
    if not text:
        return 1
    width = float(col_width or 10)
    # Консервативная оценка под LibreOffice PDF, чтобы не занижать высоту шапок.
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
    start_row: int,
    end_row: int,
    max_col: int,
    min_height: float = 18.0,
    line_height: float = 14.0,
    max_height: float = 220.0,
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


def _set_col_widths(ws, n_cols: int, first_wide: bool = True) -> None:
    if first_wide:
        ws.column_dimensions["A"].width = 9
        ws.column_dimensions["B"].width = 42
        ws.column_dimensions["C"].width = 14
        ws.column_dimensions["D"].width = 16
        ws.column_dimensions["E"].width = 18
        ws.column_dimensions["F"].width = 20
        ws.column_dimensions["G"].width = 44
    else:
        ws.column_dimensions["A"].width = 9
        ws.column_dimensions["B"].width = 35
        for ci in range(3, n_cols + 1):
            ws.column_dimensions[get_column_letter(ci)].width = 22


# =============================================================================
# ЗАПИСЬ ЛИСТА ДЕТАЛИЗАЦИИ
# =============================================================================

DETAIL_COL_WIDTHS_7 = [9, 42, 14, 16, 18, 20, 44]
DETAIL_PAGINATION_CFG_7 = DetailPaginationConfig(
    page_body_height_pt=505.0,
    page_safe_reserve_pt=36.0,
    section_title_height_pt=26.0,
    header_height_pt=64.0,
    data_row_height_pt=24.0,
    data_row_max_height_pt=36.0,
    total_row_height_pt=28.0,
    section_gap_height_pt=12.0,
    note_height_pt=38.0,
    wrap_chars_per_unit=0.85,
)


def _detail_dget(rd: dict, key: str, default=None):
    if key in rd:
        return rd.get(key, default)
    aliases = {
        "Машина": ("machine", "machine_name", "name"),
        "Площадь, га": ("area", "covered_area"),
        "Время на полях": ("Время", "time_on_field", "time"),
        "Ср. выр-ка га/час": ("rate", "avg_rate"),
        "Средняя скорость на полях км/ч": ("speed", "avg_speed"),
        "Коэффициент использования конструкционной ширины захвата, %": (
            "coef", "coverage_coef",
        ),
        "Время (сек)": ("time_sec", "seconds"),
    }
    for alt in aliases.get(key, ()):
        if alt in rd:
            return rd.get(alt, default)
    return default


def _normalize_detail_row(rd: object) -> dict:
    if isinstance(rd, dict):
        area = _detail_dget(rd, "Площадь, га", 0.0)
        secs = _detail_dget(rd, "Время (сек)", 0.0)
        time_on_fields = _detail_dget(rd, "Время на полях", None)
        if (not time_on_fields or str(time_on_fields).strip() == "") and secs:
            time_on_fields = seconds_to_hms(float(secs))
        machine = _detail_dget(rd, "Машина", "—")
        if machine is None or str(machine).strip() == "":
            machine = "—"
        return {
            "machine": machine,
            "area": float(area or 0.0),
            "time": str(time_on_fields or "0:00:00"),
            "secs": float(secs or 0.0),
            "rate": float(_detail_dget(rd, "Ср. выр-ка га/час", 0.0) or 0.0),
            "speed": float(_detail_dget(rd, "Средняя скорость на полях км/ч", 0.0) or 0.0),
            "coef": float(
                _detail_dget(rd, "Коэффициент использования конструкционной ширины захвата, %", 0.0)
                or 0.0
            ),
        }
    if isinstance(rd, (list, tuple)):
        vals = list(rd) + [None] * 7
        machine = vals[0] if vals[0] not in (None, "") else "—"
        area = float(vals[1] or 0.0)
        secs = float(vals[2] or 0.0)
        time_on_fields = vals[3] if vals[3] not in (None, "") else seconds_to_hms(secs)
        return {
            "machine": str(machine),
            "area": area,
            "time": str(time_on_fields or "0:00:00"),
            "secs": secs,
            "rate": float(vals[4] or 0.0),
            "speed": float(vals[5] or 0.0),
            "coef": float(vals[6] or 0.0),
        }
    return {
        "machine": "—", "area": 0.0, "time": "0:00:00",
        "secs": 0.0, "rate": 0.0, "speed": 0.0, "coef": 0.0,
    }


def write_detail_sheet(
    wb:          openpyxl.Workbook,
    firm_name:   str,
    report_date: date,
    day_start:   datetime,
    cutoff:      datetime,
    all_details: dict,
) -> None:
    detail_own = [_normalize_detail_row(rd) for rd in (all_details.get("detail_own") or [])]
    detail_hired = [_normalize_detail_row(rd) for rd in (all_details.get("detail_hired") or [])]
    if not detail_own and not detail_hired:
        return

    ws  = wb.create_sheet(title=firm_name[:28])
    s   = _make_styles()
    n   = len(DETAIL_COLS)
    cfg = DETAIL_PAGINATION_CFG_7

    for ci, w in enumerate(DETAIL_COL_WIDTHS_7, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    ctx = DetailPageContext(
        ws, n, DETAIL_COL_WIDTHS_7, cfg,
        on_new_page=lambda: _write_note_7(ctx),
    )

    def _write_note_7(page_ctx: DetailPageContext) -> None:
        page_ctx.ensure_space(cfg.note_height_pt)
        ws.merge_cells(
            start_row=page_ctx.row, start_column=1,
            end_row=page_ctx.row, end_column=n,
        )
        c_note = ws.cell(row=page_ctx.row, column=1, value=SUMMARY_MACHINES_NOTE)
        c_note.font = Font(bold=False, size=11, color="000000")
        c_note.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        ws.row_dimensions[page_ctx.row].height = cfg.note_height_pt
        page_ctx.row += 1
        page_ctx.page_used_pt += cfg.note_height_pt

    _write_note_7(ctx)

    def _detail_vals(idx: int, nd: dict) -> list[object]:
        return [
            idx, nd["machine"], nd["area"], nd["time"],
            nd["rate"], round(nd["speed"]), round(nd["coef"]),
        ]

    firm_single_page = compute_firm_single_page(
        ctx, detail_own, detail_hired, _detail_vals, include_note=True,
    )
    same_firm_both_sections = bool(detail_own and detail_hired)
    row_font = Font(color="000000")

    def _make_section_writer(sec_fill, row_fill, rows_data: list[dict]):
        def _caption(title: str, relaxed: bool) -> None:
            ctx.ensure_space(
                cfg.section_title_height_pt + cfg.header_height_pt,
                relaxed=relaxed,
            )
            ws.merge_cells(start_row=ctx.row, start_column=1, end_row=ctx.row, end_column=n)
            c = ws.cell(row=ctx.row, column=1, value=title)
            _sc(c, s, fill=sec_fill, font=s["font_sec"], align=s["align_c"])
            ws.row_dimensions[ctx.row].height = cfg.section_title_height_pt
            ctx.row += 1
            hdr_row = ctx.row
            for ci, col in enumerate(DETAIL_COLS, start=1):
                c = ws.cell(row=ctx.row, column=ci, value=col)
                _sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
            ws.row_dimensions[hdr_row].height = cfg.header_height_pt
            ctx.row += 1
            ctx.page_used_pt += cfg.section_title_height_pt + cfg.header_height_pt

        def _data_row(r: int, vals: list[object]) -> float:
            for ci, val in enumerate(vals, start=1):
                if ci == 3:
                    val = _fmt_grouped_2(val)
                c = ws.cell(row=r, column=ci, value=val)
                _sc(
                    c, s, fill=row_fill, font=row_font,
                    align=s["align_l"] if ci == 2 else s["align_c"],
                )
            h = ctx.row_height(vals)
            ws.row_dimensions[r].height = h
            return h

        def _total_row() -> float:
            tc  = round(sum(nd["area"] for nd in rows_data), 2)
            ts  = sum(nd["secs"] for nd in rows_data)
            tr  = round(tc / (ts / 3600), 1) if ts else 0.0
            asp = round(sum(nd["speed"] for nd in rows_data) / len(rows_data))
            acf = round(sum(nd["coef"] for nd in rows_data) / len(rows_data))
            r = ctx.row
            for ci, val in enumerate(
                ["Итого", "", tc, seconds_to_hms(ts), tr, asp, acf], start=1
            ):
                if ci == 3:
                    val = _fmt_grouped_2(val)
                c = ws.cell(row=r, column=ci, value=val)
                itogo_nowrap = ci == 1 and val == "Итого"
                align = (
                    Alignment(horizontal="center", vertical="center", wrap_text=False)
                    if itogo_nowrap else s["align_c"]
                )
                _sc(c, s, fill=s["fill_total"], font=s["font_total"], align=align)
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
        st         = all_details[firm_name]
        n_mach     = st["machines_today"]
        area       = st["area_today"]
        eng        = st["engine_today"]
        avg_s      = st["avg_season"]
        _, plan_rate, own_count = AGROFIRMS[firm_name]
        rows.append({
            "№ п/п":                                                       i,
            "Агрофирма":                                                   firm_name,
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

    # Средняя по сезону — как в эталоне: суммарная площадь / суммарные машино-дни
    total_season_sum   = sum(all_details[n].get("season_sum", 0.0)          for n in firm_names)
    total_machine_days = sum(all_details[n].get("season_machine_days", 0)   for n in firm_names)
    avg_season_total   = round(total_season_sum / total_machine_days, 1) if total_machine_days else 0.0

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

    rows    = _build_summary_rows(firm_names, all_details)
    hdr_row = 1

    for ci, col in enumerate(SUMMARY_COLS, start=1):
        c = ws.cell(row=hdr_row, column=ci, value=col)
        _sc(c, s, fill=s["fill_hdr"],
            font=s["font_hdr"], align=s["align_hdr"])
    ws.row_dimensions[hdr_row].height = max(ws.row_dimensions[hdr_row].height or 0.0, 92.0)

    _sk_plan = "Плановая выработка, га/сутки"
    _sk_mach = "Количество задействованной техники"
    _sk_rate = "Выработка за сутки/1 ед агрегата, га"
    for ri, row_data in enumerate(rows, start=hdr_row + 1):
        is_total = row_data.get("Агрофирма") == "Итого"
        fill     = s["fill_total"] if is_total else s["fill_own"]
        font     = s["font_total"] if is_total else None
        for ci, col in enumerate(SUMMARY_COLS, start=1):
            raw = row_data.get(col, "")
            val, nf = _summary_display_value(col, raw)
            c = ws.cell(row=ri, column=ci, value=val)
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
            _sc(c, s, fill=cell_fill, font=font,
                align=_align_summary_data(ci, col, val, s))
        ws.row_dimensions[ri].height = 28

    _set_col_widths(ws, n, first_wide=False)
    _auto_fit_row_heights(
        ws, start_row=1, end_row=hdr_row + len(rows), max_col=n, min_height=22.0, line_height=16.0
    )


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
    headers    = {"X-User-Api-Token": API_TOKEN}
    semaphore  = asyncio.Semaphore(SEMAPHORE_LIMIT)

    print(f"\n  Загружаем задания, mtfmi и ресурсы отчёта (все агрофирмы, один проход)...")
    from report_resources_fetch import load_report_resources

    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    async with httpx.AsyncClient(headers=headers, timeout=60, limits=pool) as client:
        tasks_today, tasks_season, mtfmi_map = await fetch_all_tasks_and_mtfmi(
            client, semaphore, day_start, cutoff, season_start, ALL_WT_IDS,
        )

        all_tasks = tasks_today + tasks_season
        field_to_firm, machines_map, implements_dict = await load_report_resources(
            client, BASE_URL, semaphore, all_tasks, mtfmi_map,
            get_all_from_id, _build_field_to_firm,
        )

    print("  Распределяем задания по агрофирмам через поля...")
    all_details = build_all_firm_stats(
        tasks_today, tasks_season, mtfmi_map, season_start, day_start, cutoff,
        field_to_firm, implements_dict,
    )

    # Консольный вывод
    summary_rows = _build_summary_rows(firm_names, all_details)
    df = pd.DataFrame(summary_rows)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 300)
    pd.set_option("display.float_format", "{:.2f}".format)
    print("\nСВОДКА:")
    print(df.to_string(index=False))

    output_file = f"7. report_disking_{report_date}.xlsx"
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    write_summary_sheet(wb, report_date, day_start, cutoff,
                        firm_names, all_details)

    for firm_name in firm_names:
        write_detail_sheet(
            wb, firm_name, report_date, day_start, cutoff,
            all_details[firm_name],
        )

    wb.save(output_file)
    print(f"\nСохранено: {output_file}")


def build_report(report_date: date = REPORT_DATE) -> None:
    asyncio.run(build_report_async(report_date))


if __name__ == "__main__":
    build_report()
