"""
11.06.2026
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
    FIELD_NAME_FORCE_RULES,
    HIRED_GROUP_ID,
    AVAILABLE_OWN,
    PLAN_RATE_PER_GROUP,
    SKIP_CROPS,
    is_crop_omitted,
)

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

REPORT_DATE = date(2026, 9, 4)
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

SEASON_START = date(2026, 3, 1)  # средняя выработка по сезону считается с этой даты
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

WORK_TYPE_IDS   = {55}
WORK_TYPE_TITLE = "1.1 Внесение минеральных удобрений"

# название → (region_id, план га/сутки)
# region_id оставляем для совместимости, но для определения фирмы НЕ используем
AGROFIRMS: dict[str, tuple[int, float]] = {
    "АФ Чистопольская":           (7,  250),
    "АгроРаздолье":               (9,  250),
    "Аксу-Агро":                  (8,  250),
    "НурлатАгро":                 (39, 250),
    "Дрожжаное Рассвет Агро":     (11, 250),
    "Актай":                      (10, 250),
    "Лениногорская Птицефабрика": (38, 250),
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
# Счётчик техники, детализация и время — как во 2-й сводке (1.1).
MIN_COUNT_AREA_HA = 10.0
# Оценка доступной высоты печатной области (A4 landscape, с учётом полей в backend-постобработке).
# Используем для условного повторения "название секции + шапка" только при реальном переносе.
DETAIL_PAGE_BODY_HEIGHT_PT = 505.0
DETAIL_SECTION_TITLE_HEIGHT_PT = 26.0
DETAIL_HEADER_HEIGHT_PT = 48.0
DETAIL_DATA_ROW_HEIGHT_PT = 24.0
DETAIL_DATA_ROW_MAX_HEIGHT_PT = 36.0
DETAIL_TOTAL_ROW_HEIGHT_PT = 24.0
DETAIL_SECTION_GAP_HEIGHT_PT = 12.0
DETAIL_ATOMIC_SECTION_EXTRA_BUFFER_PT = 80.0
# Запас по высоте для предотвращения "сиротских" строк Итого на новой странице
# из-за отличий расчёта печатной высоты между openpyxl и PDF-конвертером.
DETAIL_PAGE_SAFE_RESERVE_PT = 36.0
DETAIL_PAGE_TITLE_ROW_HEIGHT_PT = 22.0  # высота каждой из двух строк заголовка страницы
DETAIL_WRAP_LINE_HEIGHT_PT = 14.0
# Коэффициент «символов на единицу ширины» для модели переносов при расчёте разрывов страниц.
# 0.65 слибо мало → лишние строки в оценке ячейки «Машина» → секция кажется выше page_limit
# и включается постраничное дробление (1 строка + пачка + 1 строка).
DETAIL_WRAP_UNIT_FOR_BREAKS = 0.82
# Жёстко «по сумме» блок иногда висит прямо на границе из‑за погрешности; не включаем
# постраничное дробление, если блок едва переваливает модельную высоту.
DETAIL_SINGLE_PAGE_SLACK_PT = 28.0

MANUAL_OVERRIDES_ENV = "SVODKI_MANUAL_OVERRIDES_PATH"

# Глобальный кэш машин
machines_map: dict[int, dict] = {}


def _merge_manual_report3() -> dict:
    base: dict = {
        "plan_rate_by_firm": {
            k: float(PLAN_RATE_PER_GROUP.get(WORK_TYPE_TITLE, v[1]))
            for k, v in AGROFIRMS.items()
        },
        "available_self_propelled_by_firm": {
            k: AVAILABLE_OWN.get(WORK_TYPE_TITLE, {}).get(k, 0)
            for k in AGROFIRMS
        },
    }
    path = os.environ.get(MANUAL_OVERRIDES_ENV)
    if not path:
        return base
    p = Path(path)
    if not p.is_file():
        return base
    try:
        store = json.loads(p.read_text(encoding="utf-8"))
        block = store.get("3") or {}
        if isinstance(block.get("plan_rate_by_firm"), dict):
            for k, v in block["plan_rate_by_firm"].items():
                if k in AGROFIRMS:
                    base["plan_rate_by_firm"][k] = float(v)
        if isinstance(block.get("available_self_propelled_by_firm"), dict):
            for k, v in block["available_self_propelled_by_firm"].items():
                if k in AGROFIRMS:
                    base["available_self_propelled_by_firm"][k] = int(v)
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


def load_field_to_firm() -> dict[int, str]:
    """Строит словарь field_id → firm_name (полный справочник fields)."""
    print("  Загружаем fields / field_groups / group_folders...")
    fields_all = get_all_from_id("fields")
    fgs_all    = get_all_from_id("field_groups")
    gfs_all    = get_all_from_id("group_folders")
    return _build_field_to_firm(fields_all, fgs_all, gfs_all)


def load_crop_field_ids(
    report_year: int,
    field_to_firm: dict[int, str] | None = None,
) -> set[int]:
    """
    Возвращает множество field_id с активной культурой в указанном году,
    исключая культуры из SKIP_CROPS. Как в 4_main_fixed.load_reference_data.
    """
    print(f"  Загружаем crops и history_items year={report_year} (фильтр SKIP_CROPS)...")
    crops_map = {c["id"]: c.get("name") or c.get("title", "")
                 for c in get_all_from_id("crops")}
    his_all = get_all_from_id("history_items", {"year": report_year})

    valid_field_ids: set[int] = set()
    for hi in his_all:
        fid     = hi.get("field_id")
        crop_id = hi.get("crop_id")
        active  = hi.get("active")
        if not (fid and crop_id and crop_id in crops_map and active):
            continue
        crop_name = crops_map[crop_id]
        if crop_name in SKIP_CROPS:
            continue
        if field_to_firm is not None:
            firm = field_to_firm.get(int(fid))
            if firm and is_crop_omitted(WORK_TYPE_TITLE, firm, crop_name):
                continue
        valid_field_ids.add(int(fid))

    print(f"    Полей с активной культурой (не SKIP_CROPS): {len(valid_field_ids)}")
    return valid_field_ids


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

    print(f"  Заданий за сутки: {len(tasks_today)}, за сезон (с {SEASON_START.strftime('%d.%m.%Y')}): {len(tasks_season)}")

    all_task_ids = list({t["id"] for t in tasks_today + tasks_season})
    if all_task_ids:
        print(f"  Загружаем mtfmi для {len(all_task_ids)} заданий сезона...")
        mtfmi_map = await fetch_mtfmi_map_for_tasks(
            client, semaphore, BASE_URL, all_task_ids, log_prefix="[r3] ",
        )
    else:
        mtfmi_map = {}

    return tasks_today, tasks_season, mtfmi_map


# =============================================================================
# ПОДСЧЁТ — агрофирма определяется по полю
# =============================================================================

def build_all_firm_stats(
    tasks_today:      list[dict],
    tasks_season:     list[dict],
    mtfmi_map:        dict[int, list[dict]],
    season_start:     datetime,
    day_start:        datetime,
    cutoff:           datetime,
    field_to_firm:    dict[int, str],
    implements_dict:  dict[int, dict],
    crop_field_ids:   set[int] | None = None,
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
        mid   = t.get("machine_id")
        items = mtfmi_map.get(t["id"], [])

        # Группируем items по агрофирме через поле
        firm_items: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            fid  = item.get("field_id")
            if fid is None:
                continue
            fid_int = int(fid)
            if crop_field_ids is not None and fid_int not in crop_field_ids:
                continue
            firm = field_to_firm.get(fid_int)
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
        mid = t.get("machine_id")
        end_dt = parse_dt(t.get("end_time"))
        if not end_dt:
            continue
        anchor = datetime(
            end_dt.year, end_dt.month, end_dt.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK
        )
        if end_dt < anchor:
            # end_time до 06:00 относится к предыдущей смене
            shift_date = (end_dt - timedelta(days=1)).date().isoformat()
            shift_cutoff = anchor
        else:
            shift_date = end_dt.date().isoformat()
            shift_cutoff = anchor + timedelta(days=1)

        items = mtfmi_map.get(t["id"], [])
        if not items:
            continue

        for item in items:
            fid = item.get("field_id")
            if fid is None:
                continue
            fid_int = int(fid)
            if crop_field_ids is not None and fid_int not in crop_field_ids:
                continue
            firm = field_to_firm.get(fid_int)
            if not firm:
                continue

            # Для 1.1 синхронизируемся с расчётом 2-й сводки:
            # cov = сумма covered_area_hourly только по условию h < cutoff смены.
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

    # Собираем результат
    result: dict[str, dict] = {}

    for firm in AGROFIRMS:
        pm = per_firm_machine[firm]
        da = season_daily_area[firm]

        season_sum = sum(da.values())
        machine_days = sum(
            len(season_daily_machines[firm].get(day, set()))
            for day, area in da.items()
            if area > 0
        )
        # Средняя выработка по сезону:
        # все гектары с начала сезона / суммарное количество техники в эти дни.
        avg_season = round(season_sum / machine_days, 1) if machine_days else 0.0

        if not pm:
            stats = _empty_stats()
            stats["avg_season"] = avg_season
            stats["season_sum"] = season_sum
            stats["season_machine_days"] = machine_days
            stats["season_by_day"] = dict(da)
            result[firm] = stats
            continue

        detail_own:   list[dict] = []
        detail_hired: list[dict] = []

        # Счётчик/детали/время — >= 10 га на машину; площадь за сутки — >= 0.5 га (как сводки 1 и 2).
        pm_count = {
            mid: d for mid, d in pm.items()
            if round(d["cov_total"], 2) >= MIN_COUNT_AREA_HA
        }

        for mid, d in pm_count.items():
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
            if group == HIRED_GROUP_ID:
                detail_hired.append(row)
            else:
                detail_own.append(row)

        detail_own.sort(key=lambda x:   -x["Площадь, га"])
        detail_hired.sort(key=lambda x: -x["Площадь, га"])

        area_today = sum(
            d["cov_total"] for d in pm.values()
            if round(d["cov_total"], 2) >= MIN_DETAIL_AREA_HA
        )
        engine_today = sum(d["work_dur"] for d in pm_count.values())

        result[firm] = {
            "machines_today": len(pm_count),
            "hired_today":    len(detail_hired),
            "area_today":     round(area_today, 2),
            "engine_today":   round(engine_today / 3600, 1),
            "avg_season":     avg_season,
            "season_sum":     season_sum,
            "season_machine_days": machine_days,
            "season_by_day":  dict(da),
            "detail_own":     detail_own,
            "detail_hired":   detail_hired,
        }

    return result


def _empty_stats() -> dict:
    return {
        "machines_today": 0,
        "hired_today":    0,
        "area_today":     0.0,
        "engine_today":   0.0,
        "avg_season":     0.0,
        "season_sum":     0.0,
        "season_machine_days": 0,
        "season_by_day":  {},
        "detail_own":     [],
        "detail_hired":   [],
    }


# =============================================================================
# СТИЛИ
# =============================================================================

def _make_styles() -> dict:
    thin   = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    return {
        "border":        border,
        "align_c":       Alignment(horizontal="center", vertical="center", wrap_text=True),
        "align_l":       Alignment(vertical="center", wrap_text=True),
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


DETAIL_COLS = [
    "№", "Машина", "Площадь, га", "Время на полях",
    "Ср. выр-ка га/час", "Средняя скорость на полях км/ч",
    "Коэффициент использования конструкционной ширины захвата, %",
]
DETAIL_COL_WIDTHS = [7, 46, 14, 16, 18, 20, 44]

SUMMARY_COLS = [
    "№ п/п", "Агрофирма", "Плановая выработка, га/сутки",
    "Кол-во задействованной техники в т.ч. прицепных",
    "Обработано за сутки, га",
    "Выработка за сутки/1 ед агрегата, га",
    "Средняя выработка по сезону, га/сутки",
    "Общее время работы на полях, ч",
    "Среднее время эффективной работы на поле 1 агр, ч",
    "Часовая выработка в расчёте на 1 агрегат, га",
    "Доступно к работе самоходных агрегатов, прицепных",
    "Всего наёмных агрегатов",
]


def _style_cell(cell, s, fill=None, font=None, align=None) -> None:
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


def _estimate_wrapped_lines(value: object, col_width: float | None, chars_per_unit: float = 0.65) -> int:
    """
    Оценка числа строк после переноса по ширине колонки.
    openpyxl не умеет автоподбор высоты строк как Excel, поэтому используем
    предсказуемую эвристику.
    """
    text = str(value or "").strip()
    if not text:
        return 1
    width = float(col_width or 10)
    # Единицы ширины openpyxl примерно равны количеству символов среднего шрифта.
    # chars_per_unit < 1 — меньше символов в строке (больше переносов). Для визуальной
    # подгонки ячеек в отчёте оставляем 0.65; для модели разрывов см. DETAIL_WRAP_UNIT_FOR_BREAKS.
    chars_per_line = max(1, int(width * chars_per_unit))
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


def _estimate_row_height_by_values(
    values: list[object],
    col_widths: list[float],
    min_height: float,
    line_height: float = 16.0,
    max_height: float = 140.0,
    chars_per_unit: float = 0.65,
) -> float:
    max_lines = 1
    for idx, value in enumerate(values):
        if value in (None, ""):
            continue
        width = col_widths[idx] if idx < len(col_widths) else 10.0
        max_lines = max(max_lines, _estimate_wrapped_lines(value, width, chars_per_unit))
    return min(max_height, max(min_height, max_lines * line_height + 4))


# =============================================================================
# ЗАПИСЬ ЛИСТОВ
# =============================================================================

def write_detail_sheet(
    wb:          openpyxl.Workbook,
    firm_name:   str,
    report_date: date,
    day_start:   datetime,
    cutoff:      datetime,
    stats:       dict,
) -> None:
    ws = wb.create_sheet(title=_firm_sheet_title_line(firm_name)[:31])
    s   = _make_styles()
    row = 1
    n   = len(DETAIL_COLS)
    # Фиксируем ширины сразу, чтобы корректно оценивать переносы текста
    # и увеличивать высоты строк до постановки page break.
    for ci, w in enumerate(DETAIL_COL_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    page_used_pt = 0.0

    def _write_page_titles() -> None:
        """Записывает два заголовка страницы: название отчёта и вид работ."""
        nonlocal row, page_used_pt
        for title_text in (
            "ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ",
            f"ВИД РАБОТ: {WORK_TYPE_TITLE}",
        ):
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n)
            c = ws.cell(row=row, column=1, value=title_text)
            c.font      = s["font_title"]
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=False)
            ws.row_dimensions[row].height = DETAIL_PAGE_TITLE_ROW_HEIGHT_PT
            row += 1
            page_used_pt += DETAIL_PAGE_TITLE_ROW_HEIGHT_PT

    # Первый заголовок страницы
    _write_page_titles()

    def _ensure_page_space(required_height_pt: float) -> bool:
        nonlocal row, page_used_pt
        if required_height_pt <= 0:
            return False
        page_limit = max(1.0, DETAIL_PAGE_BODY_HEIGHT_PT - DETAIL_PAGE_SAFE_RESERVE_PT)
        if page_used_pt > 0 and page_used_pt + required_height_pt > page_limit:
            try:
                ws.row_breaks.append(Break(id=max(1, row - 1)))
            except Exception:
                pass
            page_used_pt = 0.0
            _write_page_titles()
            return True
        return False

    def _write_section_caption_and_header(sec_title, sec_fill) -> None:
        nonlocal row, page_used_pt
        _ensure_page_space(DETAIL_SECTION_TITLE_HEIGHT_PT + DETAIL_HEADER_HEIGHT_PT)
        sec_row = row
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n)
        c = ws.cell(row=row, column=1, value=sec_title)
        c.font = s["font_sec"]
        c.fill = sec_fill
        c.alignment = s["align_c"]
        ws.row_dimensions[sec_row].height = DETAIL_SECTION_TITLE_HEIGHT_PT
        row += 1

        hdr_row = row
        for ci, col in enumerate(DETAIL_COLS, start=1):
            c = ws.cell(row=row, column=ci, value=col)
            _style_cell(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_c"])
        ws.row_dimensions[hdr_row].height = DETAIL_HEADER_HEIGHT_PT
        row += 1
        page_used_pt += DETAIL_SECTION_TITLE_HEIGHT_PT + DETAIL_HEADER_HEIGHT_PT

    def _add_section_gap() -> None:
        """
        Межсекционный отступ учитываем в page_used_pt, иначе расчёт разрывов
        может недооценить фактическую высоту страницы и "Итого" уедет сиротой.
        """
        nonlocal row, page_used_pt
        if _ensure_page_space(DETAIL_SECTION_GAP_HEIGHT_PT):
            return
        ws.row_dimensions[row].height = DETAIL_SECTION_GAP_HEIGHT_PT
        row += 1
        page_used_pt += DETAIL_SECTION_GAP_HEIGHT_PT

    def _write_machines_min_area_note() -> None:
        nonlocal row, page_used_pt
        _ensure_page_space(DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT)
        row = write_detail_machines_min_area_note(ws, row, n, border=s["border"])
        page_used_pt += DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT

    firm_block_h = 0.0
    if stats.get("detail_own") or stats.get("detail_hired"):
        firm_block_h = DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT
        if stats.get("detail_own"):
            for i, rd in enumerate(stats["detail_own"], start=1):
                firm_block_h += _estimate_row_height_by_values(
                    [i, rd["Машина"], rd["Площадь, га"], rd["Время на полях"],
                     rd["Ср. выр-ка га/час"], rd["Средняя скорость на полях км/ч"],
                     rd["Коэффициент использования конструкционной ширины захвата, %"]],
                    DETAIL_COL_WIDTHS,
                    min_height=DETAIL_DATA_ROW_HEIGHT_PT,
                    chars_per_unit=DETAIL_WRAP_UNIT_FOR_BREAKS,
                )
            firm_block_h += DETAIL_SECTION_TITLE_HEIGHT_PT + DETAIL_HEADER_HEIGHT_PT + DETAIL_TOTAL_ROW_HEIGHT_PT
        if stats.get("detail_own") and stats.get("detail_hired"):
            firm_block_h += DETAIL_SECTION_GAP_HEIGHT_PT
        if stats.get("detail_hired"):
            firm_block_h += DETAIL_SECTION_TITLE_HEIGHT_PT + DETAIL_HEADER_HEIGHT_PT + DETAIL_TOTAL_ROW_HEIGHT_PT
            for i, rd in enumerate(stats["detail_hired"], start=1):
                firm_block_h += _estimate_row_height_by_values(
                    [i, rd["Машина"], rd["Площадь, га"], rd["Время на полях"],
                     rd["Ср. выр-ка га/час"], rd["Средняя скорость на полях км/ч"],
                     rd["Коэффициент использования конструкционной ширины захвата, %"]],
                    DETAIL_COL_WIDTHS,
                    min_height=DETAIL_DATA_ROW_HEIGHT_PT,
                    chars_per_unit=DETAIL_WRAP_UNIT_FOR_BREAKS,
                )
    page_limit_pre = max(1.0, DETAIL_PAGE_BODY_HEIGHT_PT - DETAIL_PAGE_SAFE_RESERVE_PT)
    firm_single_page = firm_block_h <= (page_limit_pre + DETAIL_SINGLE_PAGE_SLACK_PT)
    same_firm_both_sections = bool(stats.get("detail_own") and stats.get("detail_hired"))

    def write_section(sec_title, rows, row_fill, sec_fill, *, continue_on_page: bool = False) -> None:
        nonlocal row, page_used_pt
        atomic_section = len(rows) <= 1
        continuing_same_page = page_used_pt > 0
        def _detail_vals(idx: int, rd: dict) -> list[object]:
            return [
                idx, rd["Машина"], rd["Площадь, га"],
                rd["Время на полях"], rd["Ср. выр-ка га/час"],
                rd["Средняя скорость на полях км/ч"],
                rd["Коэффициент использования конструкционной ширины захвата, %"],
            ]

        page_limit = max(1.0, DETAIL_PAGE_BODY_HEIGHT_PT - DETAIL_PAGE_SAFE_RESERVE_PT)
        page_limit_relaxed = page_limit + DETAIL_SINGLE_PAGE_SLACK_PT
        section_block_h = 0.0
        if rows:
            section_block_h = DETAIL_SECTION_TITLE_HEIGHT_PT + DETAIL_HEADER_HEIGHT_PT
            for i, rd in enumerate(rows, start=1):
                section_block_h += _estimate_row_height_by_values(
                    _detail_vals(i, rd),
                    DETAIL_COL_WIDTHS,
                    min_height=DETAIL_DATA_ROW_HEIGHT_PT,
                    chars_per_unit=DETAIL_WRAP_UNIT_FOR_BREAKS,
                )
            section_block_h += DETAIL_TOTAL_ROW_HEIGHT_PT
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
            need += min(
                DETAIL_DATA_ROW_MAX_HEIGHT_PT,
                _estimate_row_height_by_values(
                    _detail_vals(1, rows[0]),
                    DETAIL_COL_WIDTHS,
                    min_height=DETAIL_DATA_ROW_HEIGHT_PT,
                    chars_per_unit=DETAIL_WRAP_UNIT_FOR_BREAKS,
                ),
            )
            if len(rows) == 1:
                need += DETAIL_TOTAL_ROW_HEIGHT_PT
            return need

        if atomic_section and len(rows) == 1:
            one_vals = _detail_vals(1, rows[0])
            one_h = min(
                DETAIL_DATA_ROW_MAX_HEIGHT_PT,
                _estimate_row_height_by_values(
                    one_vals,
                    DETAIL_COL_WIDTHS,
                    min_height=DETAIL_DATA_ROW_HEIGHT_PT,
                    chars_per_unit=DETAIL_WRAP_UNIT_FOR_BREAKS,
                ),
            )
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
                _ensure_page_space(full_section_need)
        elif len(rows) > 1:
            if single_page_section:
                if not (firm_single_page or continue_on_page):
                    _ensure_page_space(section_block_h)
            elif not continue_on_page:
                first_vals = _detail_vals(1, rows[0])
                first_h = _estimate_row_height_by_values(
                    first_vals,
                    DETAIL_COL_WIDTHS,
                    min_height=DETAIL_DATA_ROW_HEIGHT_PT,
                    chars_per_unit=DETAIL_WRAP_UNIT_FOR_BREAKS,
                )
                _ensure_page_space(
                    DETAIL_SECTION_TITLE_HEIGHT_PT + DETAIL_HEADER_HEIGHT_PT + first_h
                )

        if rows and page_used_pt > 0:
            _ensure_page_space(_min_section_start_height())

        _write_section_caption_and_header(sec_title, sec_fill)

        if not rows:
            nodata_row = row
            _ensure_page_space(DETAIL_TOTAL_ROW_HEIGHT_PT)
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n)
            ws.cell(row=row, column=1, value="Нет данных").alignment = s["align_c"]
            ws.row_dimensions[nodata_row].height = DETAIL_TOTAL_ROW_HEIGHT_PT
            row += 1
            page_used_pt += DETAIL_TOTAL_ROW_HEIGHT_PT
            return

        use_row_pagination = len(rows) > 1 and not single_page_section

        for idx, rd in enumerate(rows, start=1):
            vals = _detail_vals(idx, rd)
            data_row_height = min(
                DETAIL_DATA_ROW_MAX_HEIGHT_PT,
                _estimate_row_height_by_values(
                    vals,
                    DETAIL_COL_WIDTHS,
                    min_height=DETAIL_DATA_ROW_HEIGHT_PT,
                    chars_per_unit=DETAIL_WRAP_UNIT_FOR_BREAKS,
                ),
            )
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
                    _write_section_caption_and_header(sec_title, sec_fill)

            for ci, val in enumerate(vals, start=1):
                if ci == 3:
                    val = _fmt_grouped_2(val)
                c = ws.cell(row=row, column=ci, value=val)
                _style_cell(c, s, fill=row_fill,
                            align=s["align_l"] if ci == 2 else s["align_c"])
            ws.row_dimensions[row].height = data_row_height
            row += 1
            page_used_pt += data_row_height

        tc  = round(sum(rd["Площадь, га"]  for rd in rows), 2)
        ts  = sum(rd["Время (сек)"]         for rd in rows)
        tr  = round(tc / (ts / 3600), 1)    if ts else 0.0
        asp = round(sum(rd["Средняя скорость на полях км/ч"] for rd in rows) / len(rows))
        acf = round(sum(rd["Коэффициент использования конструкционной ширины захвата, %"]
                        for rd in rows) / len(rows))
        total_row = row
        # Для атомарной секции не допускаем принудительный разрыв перед "Итого":
        # место уже резервируется выше целиком под весь блок.
        if not atomic_section and not (firm_single_page or continue_on_page):
            _ensure_page_space(DETAIL_TOTAL_ROW_HEIGHT_PT)
        for ci, val in enumerate(
            ["Итого", "", tc, seconds_to_hms(ts), tr, asp, acf], start=1
        ):
            if ci == 3:
                val = _fmt_grouped_2(val)
            c = ws.cell(row=row, column=ci, value=val)
            _style_cell(c, s, fill=s["fill_total"], font=s["font_total"], align=s["align_c"])
        ws.row_dimensions[total_row].height = DETAIL_TOTAL_ROW_HEIGHT_PT
        row += 1
        page_used_pt += DETAIL_TOTAL_ROW_HEIGHT_PT

    _write_machines_min_area_note()
    write_section("СОБСТВЕННЫЕ", stats["detail_own"],  s["fill_own"],  s["fill_sec_own"])
    if stats.get("detail_hired"):
        if stats.get("detail_own"):
            if firm_single_page:
                ws.row_dimensions[row].height = DETAIL_SECTION_GAP_HEIGHT_PT
                row += 1
                page_used_pt += DETAIL_SECTION_GAP_HEIGHT_PT
            else:
                _add_section_gap()
        write_section(
            "НАЁМНЫЕ",
            stats["detail_hired"],
            s["fill_hired"],
            s["fill_sec_hire"],
            continue_on_page=True,
        )

    # Для листов детализации не включаем авто-подгонку высот:
    # она меняет фактическую высоту строк после расчёта page break
    # и приводит к «разъезду» продолжений таблиц по страницам в PDF.


def _build_summary_rows(
    firm_names:  list[str],
    all_details: dict[str, dict],
    manual:      dict | None = None,
) -> list[dict]:
    merged    = manual if manual is not None else _merge_manual_report3()
    plan_map  = merged.get("plan_rate_by_firm") or {}
    avail_map = merged.get("available_self_propelled_by_firm") or {}

    rows = []
    for i, firm_name in enumerate(firm_names, start=1):
        s         = all_details[firm_name]
        n_mach    = s["machines_today"]
        n_hire    = s["hired_today"]
        area      = s["area_today"]
        eng       = s["engine_today"]
        avg_s     = float(s.get("avg_season", 0.0) or 0.0)
        _, plan_d = AGROFIRMS[firm_name]
        plan_rate = float(plan_map.get(firm_name, plan_d))
        n_avail   = int(avail_map.get(firm_name, 0))

        rows.append({
            "№ п/п":                                                 i,
            "Агрофирма":                                             _firm_table_label(firm_name),
            "Плановая выработка, га/сутки":                          plan_rate,
            "Кол-во задействованной техники в т.ч. прицепных":       n_mach,
            "Обработано за сутки, га":                               area,
            "Выработка за сутки/1 ед агрегата, га":
                round(area / n_mach, 2) if n_mach else 0.0,
            "Средняя выработка по сезону, га/сутки":                 avg_s,
            "Общее время работы на полях, ч":                        eng,
            "Среднее время эффективной работы на поле 1 агр, ч":
                round(eng / n_mach, 2) if n_mach else 0.0,
            "Часовая выработка в расчёте на 1 агрегат, га":
                round(area / eng, 1) if eng else 0.0,
            "Доступно к работе самоходных агрегатов, прицепных":     n_avail,
            "Всего наёмных агрегатов":                               n_hire,
        })

    total_mach  = sum(r["Кол-во задействованной техники в т.ч. прицепных"] for r in rows)
    total_area  = sum(r["Обработано за сутки, га"]                          for r in rows)
    total_eng   = sum(r["Общее время работы на полях, ч"]                   for r in rows)
    total_hire  = sum(r["Всего наёмных агрегатов"]                          for r in rows)
    total_avail = sum(r["Доступно к работе самоходных агрегатов, прицепных"] for r in rows)
    plan_rate   = rows[0]["Плановая выработка, га/сутки"] if rows else 0

    # "Итого" для колонки считается как среднее арифметическое по строкам фирм над ним:
    # sum(значений фирм) / количество фирм
    firm_avg_vals: list[float] = [
        float(r.get("Средняя выработка по сезону, га/сутки") or 0.0) for r in rows
    ]
    avg_season_total = (
        round(sum(firm_avg_vals) / len(firm_avg_vals), 1) if firm_avg_vals else 0.0
    )

    rows.append({
        "№ п/п":                                                 "",
        "Агрофирма":                                             "Итого",
        "Плановая выработка, га/сутки":                          plan_rate,
        "Кол-во задействованной техники в т.ч. прицепных":       total_mach,
        "Обработано за сутки, га":                               round(total_area, 2),
        "Выработка за сутки/1 ед агрегата, га":
            round(total_area / total_mach, 2) if total_mach else 0.0,
        "Средняя выработка по сезону, га/сутки":
            avg_season_total,
        "Общее время работы на полях, ч":                        round(total_eng, 1),
        "Среднее время эффективной работы на поле 1 агр, ч":
            round(total_eng / total_mach, 2) if total_mach else 0.0,
        "Часовая выработка в расчёте на 1 агрегат, га":
            round(total_area / total_eng, 1) if total_eng else 0.0,
        "Доступно к работе самоходных агрегатов, прицепных":     total_avail,
        "Всего наёмных агрегатов":                               total_hire,
    })
    return rows


def write_main_summary_sheet(
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
    _sk_mach = "Кол-во задействованной техники в т.ч. прицепных"
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

    field_to_firm_pre = load_field_to_firm()
    crop_field_ids = load_crop_field_ids(report_date.year, field_to_firm_pre)

    firm_names = list(AGROFIRMS.keys())
    headers    = {"X-User-Api-Token": API_TOKEN}
    semaphore  = asyncio.Semaphore(SEMAPHORE_LIMIT)

    print(f"\n  Загружаем задания, mtfmi и ресурсы отчёта (все агрофирмы, один проход)...")
    from report_resources_fetch import load_report_resources

    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    async with httpx.AsyncClient(headers=headers, timeout=60, limits=pool) as client:
        tasks_today, tasks_season, mtfmi_map = await fetch_all_tasks_and_mtfmi(
            client, semaphore, day_start, cutoff, season_start, WORK_TYPE_IDS,
        )

        all_tasks = tasks_today + tasks_season
        field_to_firm, machines_map, implements_dict = await load_report_resources(
            client, BASE_URL, semaphore, all_tasks, mtfmi_map,
            get_all_from_id, _build_field_to_firm,
        )

    print("  Распределяем задания по агрофирмам через поля...")
    all_details = build_all_firm_stats(
        tasks_today, tasks_season, mtfmi_map, season_start, day_start, cutoff,
        field_to_firm, implements_dict, crop_field_ids=crop_field_ids,
    )

    manual_r3 = _merge_manual_report3()

    # Консольный вывод
    summary_rows = _build_summary_rows(firm_names, all_details, manual_r3)
    df = pd.DataFrame(summary_rows)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 300)
    pd.set_option("display.float_format", "{:.2f}".format)
    print("\nСВОДКА:")
    print(df.to_string(index=False))

    output_file = f"3. report_fertilizer_{report_date}.xlsx"
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    write_main_summary_sheet(
        wb, report_date, day_start, cutoff,
        firm_names, all_details, manual_r3,
    )
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
