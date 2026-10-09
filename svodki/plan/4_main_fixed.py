# 11.06.2026
"""
26 Cropwise — отчёт по боронованию
=================================
Виды боронования: wt=103 (2.1 Зябь), 81 (2.2 Многолетние), 97 (2.3 По всходам), 12 (Боронование)

Структура Excel:
  Лист 1 "Сводка"        — общая таблица: агрофирма × показатели выработки
  Листы 2-8              — по одному листу на агрофирму (детализация по технике)

Логика определения агрофирмы:
  Агрофирма определяется НЕ по привязке машины к региону,
  а по полю из mtfmi: field → field_group → group_folder → агрофирма.

Логика площадей:
  Факт за сутки  ← mtfmi (covered_area, задания за текущую смену)
  Источник данных — исключительно mtfmi (machine_task_field_mapping_items).

Средняя выработка по сезону:
  Все гектары с начала сезона / суммарное количество техники
  в дни, где была выработка (sum(area_by_day) / sum(machine_count_by_day)).
"""

from __future__ import annotations


import asyncio
import json
import os
import sys
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
from cropwise_paging import CROPWISE_PAGE_LIMIT, from_id_should_continue
from report_fetch_orchestration import (
    fetch_all_pages as fetch_pages,
    fetch_machine_tasks_window,
    fetch_mtfmi_map_hybrid_async,
    report_async_client,
    run_report_async,
)
from report_pipeline import run_parallel_refs_and_fetch
from svodki_timing import stage, timing_finish, timing_start
import httpx
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
import requests
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
    FOLDER_TO_FIRM,
    HIRED_GROUP_ID,
    AVAILABLE_OWN,
    PLAN_RATE_PER_GROUP,
    SKIP_CROPS,
    FIELD_NAME_FORCE_RULES,
    is_crop_user_excluded as _plan_crop_excluded,
    is_crop_excluded_from_totals as _plan_crop_excluded_totals,
    crop_code_for_report as _plan_crop_code,
    work_group_ids,
)

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================
def _default_report_date() -> date:
    """Сегодня по Москве (без зашитой даты; явная дата — переменная REPORT_DATE)."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    return _dt.now(_tz(_td(hours=3))).date()


REPORT_DATE = _default_report_date()
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

SEASON_START = date(REPORT_DATE.year, 3, 1)  # средняя выработка по сезону считается с этой даты
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
try:
    from season_unified import season_start_from_editor

    SEASON_START = season_start_from_editor("2.1 Боронование", REPORT_DATE, SEASON_START)
except Exception:
    pass

WORK_TYPES: dict[int, str] = {
    wt_id: "2.1 Боронование" for wt_id in work_group_ids("2.1 Боронование")
}
ALL_WT_IDS    = set(WORK_TYPES.keys())
WORK_TYPE_TITLE = "Боронование"

DAY_START_HOUR = 6
DAY_END_HOUR   = 6
SEMAPHORE_LIMIT = 20
MIN_DISPLAY_AREA_HA = 0.5
# Машины с площадью ниже этого порога не учитываются в счётчиках техники,
# времени работы и детальных таблицах, но их площадь суммируется в общий объём.
MIN_COUNT_AREA_HA = 10.0
SUMMARY_MACHINES_NOTE = (
    "В сводных данных отображается техника, отработавшая больше 10 га"
)

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
_WORK_TYPE_KEY = "2.1 Боронование"  # ключ в PLAN_RATE_PER_GROUP и AVAILABLE_OWN
_BASE_AGROFIRMS_4: dict[str, tuple[int, int, float, int]] = {
    firm: (
        FIRM_TO_FOLDER[firm],
        _REGION_IDS[firm],
        PLAN_RATE_PER_GROUP.get(_WORK_TYPE_KEY, 200.0),
        AVAILABLE_OWN.get(_WORK_TYPE_KEY, {}).get(firm, 0),
    )
    for firm in FIRM_TO_FOLDER
}


def _merge_manual_report4(
    base: dict[str, tuple[int, int, float, int]],
) -> dict[str, tuple[int, int, float, int]]:
    """Планы и «собств. агрегаты» из manual_overrides.json (задаёт svodki_app)."""
    path = os.environ.get(MANUAL_OVERRIDES_ENV)
    if not path:
        return base
    p = Path(path)
    if not p.is_file():
        return base
    try:
        store = json.loads(p.read_text(encoding="utf-8"))
        block = store.get("4") or {}
    except Exception:
        return base
    out = {k: list(v) for k, v in base.items()}
    if isinstance(block.get("plan_rate_by_firm"), dict):
        for k, v in block["plan_rate_by_firm"].items():
            if k in out:
                try:
                    out[k][2] = float(v)
                except (TypeError, ValueError):
                    pass
    if isinstance(block.get("own_count_by_firm"), dict):
        for k, v in block["own_count_by_firm"].items():
            if k in out:
                try:
                    out[k][3] = int(v)
                except (TypeError, ValueError):
                    pass
    return {k: tuple(t) for k, t in out.items()}


AGROFIRMS = _merge_manual_report4(_BASE_AGROFIRMS_4)

FIRM_LABELS_TABLE: dict[str, str] = {
    "НурлатАгро": "Нурлат Агро",
}


def _firm_label(firm: str) -> str:
    return FIRM_LABELS_TABLE.get(firm, firm)


# =============================================================================
# HTTP-КЛИЕНТ (синхронный для справочников)
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


def get_all_from_id(endpoint: str, extra_params: dict | None = None) -> list[dict]:
    params  = dict(extra_params or {})
    results = []
    from_id = 0
    while True:
        r = session.get(f"{BASE_URL}/{endpoint}",
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
    return results


def get_all_page(endpoint: str, params: dict | None = None) -> list[dict]:
    params = dict(params or {})
    params.setdefault("per_page", 1000)
    results, page = [], 1
    while True:
        r = session.get(f"{BASE_URL}/{endpoint}", params={**params, "page": page},
                        timeout=120)
        r.raise_for_status()
        body  = r.json()
        chunk = body if isinstance(body, list) else body.get("data", [])
        results.extend(chunk)
        if len(chunk) < params["per_page"]:
            break
        page += 1
    return results


# =============================================================================
# ВРЕМЯ
# =============================================================================

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


def seconds_to_hms(seconds: float) -> str:
    total = int(seconds)
    h     = total // 3600
    m     = (total % 3600) // 60
    s     = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def hours_to_hms(hours: float) -> str:
    return seconds_to_hms(round(float(hours or 0.0) * 3600))


# =============================================================================
# ГЛОБАЛЬНЫЕ КЭШИ
# =============================================================================

machines_map:          dict[int, dict] = {}
implements_map:        dict[int, dict] = {}
crop_map:              dict[int, str]  = {}
field_to_crop:         dict[int, str]  = {}
field_id_resolved_firm: dict[int, str] = {}


def _norm_field_label(name: str) -> str:
    return (
        (name or "")
        .strip()
        .casefold()
        .replace(" ", "")
        .replace("\u00a0", "")
    )


def _firm_from_field_name(field_rec: dict) -> str | None:
    """Принудительные правила по имени поля (совпадение с логикой 2_main_ideal_compact)."""
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


def _rebuild_field_firm_resolution(fields: list[dict], fgs: list[dict]) -> None:
    """Полная иерархия папок — как в 2_main_ideal_compact.load_field_to_firm."""
    global field_id_resolved_firm
    fgs_map    = {fg["id"]: fg for fg in fgs}
    gfs_all    = get_all_from_id("group_folders")
    parent_map = _folder_parent_map(gfs_all)
    root_ids   = set(FIRM_TO_FOLDER.values())
    gf_hints   = _group_folder_name_firm_hints(gfs_all)
    field_id_resolved_firm = {}
    for f in fields:
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
            field_id_resolved_firm[int(fid)] = firm_key
    # Принудительные правила по имени поля (перезаписывают)
    forced = 0
    for f in fields:
        fid = f.get("id")
        if fid is None:
            continue
        fk = _firm_from_field_name(f)
        if fk:
            field_id_resolved_firm[int(fid)] = fk
            forced += 1
    print(
        f"    Полей с привязкой к агрофирме: {len(field_id_resolved_firm)} "
        f"(принудительно по имени: {forced})"
    )


def load_reference_data() -> None:
    global crop_map, field_to_crop, field_id_resolved_firm

    sync_season_start_for_report(REPORT_DATE)

    from svodki_pipeline import run_reference_jobs

    _cur_season = season_year_for_date(REPORT_DATE)
    crop_years = sorted({_cur_season - 1, _cur_season}, reverse=True)
    jobs: dict[str, object] = {"crops": lambda: get_all_page("crops")}
    for crop_year in crop_years:
        jobs[f"history:{crop_year}"] = (
            lambda yy=crop_year: get_all_from_id("history_items", {"year": yy})
        )
    print(f"  Загружаем crops и history_items (годы {crop_years})...")
    raw = run_reference_jobs(jobs)
    crops = raw["crops"]
    crop_map = {c["id"]: c.get("name") or c.get("title", "") for c in crops}
    print(f"    {len(crop_map)} культур")

    for crop_year in crop_years:
        his = raw[f"history:{crop_year}"]
        for hi in his:
            fid     = hi.get("field_id")
            crop_id = hi.get("crop_id")
            active  = hi.get("active")
            if not (fid and crop_id and crop_id in crop_map and active):
                continue
            crop_name = crop_map[crop_id]
            if crop_name in SKIP_CROPS:
                continue
            field_to_crop[int(fid)] = crop_name
    print(f"    {len(field_to_crop)} полей с культурами")


def field_to_firm_fn(fid: int) -> str:
    return field_id_resolved_firm.get(int(fid), "")


def _boron_item_excluded(firm: str, item: dict) -> bool:
    """Культура убрана в «Редакторе планов» (скрыта / ранг 0 / «не считать») — гектары в итоги не идут."""
    from mtfmi_batch_fetch import crop_label

    return _plan_crop_excluded_totals("2.1 Боронование", firm, crop_label(item))


# =============================================================================
# АСИНХРОННЫЕ ЗАПРОСЫ
# =============================================================================

async def fetch_mtfmi(client: httpx.AsyncClient, task_id: int,
                      sem: asyncio.Semaphore) -> tuple[int, list[dict]]:
    items = await fetch_pages(client, f"{BASE_URL}/machine_task_field_mapping_items",
                              {"machine_task_id": task_id}, sem)
    return task_id, items


# =============================================================================
# ОСНОВНАЯ АСИНХРОННАЯ ФУНКЦИЯ
# =============================================================================

async def fetch_all_data(
    client:    httpx.AsyncClient,
    sem:       asyncio.Semaphore,
    day_start: datetime,
    cutoff:    datetime,
) -> tuple[dict[str, dict], dict[str, float], float, int]:
    """
    Загружает ВСЕ machine_tasks с начала сезона до cutoff.
    Возвращает:
      - day_stats:    детальная статистика за текущую смену по каждой агрофирме
      - season_stats: средняя выработка по сезону по каждой агрофирме
    Источник площадей — исключительно mtfmi.
    """
    url = f"{BASE_URL}/machine_tasks"
    season_start_str = datetime(
        SEASON_START.year, SEASON_START.month, SEASON_START.day, tzinfo=TZ_MSK
    ).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str       = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    day_start_str    = day_start.strftime("%Y-%m-%dT%H:%M:%S")

    print("    Загружаем machine_tasks за сезон (prefetch / окна по датам)...")
    all_tasks = await fetch_machine_tasks_window(
        client, sem, BASE_URL, ALL_WT_IDS,
        {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_str},
        log_prefix="[r4] ",
    )

    tasks_with_area = [t for t in all_tasks if (t.get("covered_area") or 0) > 0]
    try:
        _sdir = Path(__file__).resolve().parent / "utils" / "season"
        if not _sdir.is_dir():
            _sdir = Path(__file__).resolve().parents[1] / "svodki" / "plan" / "utils" / "season"
        if _sdir.is_dir() and str(_sdir) not in sys.path:
            sys.path.insert(0, str(_sdir))
        from season_unified import filter_tasks_by_group_seasons as _filter_seasons

        tasks_with_area = _filter_seasons(tasks_with_area, _WORK_TYPE_KEY, REPORT_DATE)
    except Exception:
        pass
    print(f"    Заданий боронования с covered_area > 0 за сезон: {len(tasks_with_area)}")

    if not tasks_with_area:
        empty = _empty_stats()
        return (
            {firm: empty for firm in AGROFIRMS},
            {firm: 0.0   for firm in AGROFIRMS},
        )

    print("    Загружаем mtfmi для всех заданий...")
    mtfmi_map = await fetch_mtfmi_map_hybrid_async(
        client, sem, BASE_URL, [t["id"] for t in tasks_with_area], log_prefix="[r4] ",
    )

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
    print(
        f"    Ресурсы отчёта: полей={len(field_ids)}, "
        f"машин={len(machine_ids)}, орудий={len(implement_ids)}"
    )
    fields_all, machines_map, implements_map = await asyncio.gather(
        load_fields_list_async(client, BASE_URL, field_ids, sem),
        load_machines_map_async(client, BASE_URL, machine_ids, sem),
        load_implements_dict_async(client, BASE_URL, implement_ids, sem),
    )
    fgs = get_all_page("field_groups")
    _rebuild_field_firm_resolution(fields_all, fgs)

    # Задания за текущую смену
    today_task_ids: set[int] = set()
    for t in tasks_with_area:
        end_str = str(t.get("end_time", ""))[:19]
        if day_start_str <= end_str < cutoff_str:
            today_task_ids.add(t["id"])
    print(f"    Заданий за текущую смену: {len(today_task_ids)}")

    # firm → machine_id → статистика за сутки
    per_firm_machine: dict[str, dict[int, dict]] = {
        firm: {} for firm in AGROFIRMS
    }

    # Для средней выработки по сезону:
    # firm → date_str → {area, machine_ids}
    season_daily: dict[str, dict[str, dict]] = {
        firm: defaultdict(lambda: {"area": 0.0, "mids": set()})
        for firm in AGROFIRMS
    }

    def _parse_dt(s) -> datetime | None:
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

    # -------------------------------------------------------------------------
    # Проход 1: сезонная статистика (как в 2_main: только задания с известной датой смены,
    # в периоде [SEASON_START, REPORT_DATE), площадь из covered_area, порог per-item 0.5 га)
    # -------------------------------------------------------------------------
    for t in tasks_with_area:
        tid = t["id"]
        mid = t["machine_id"]

        end_dt = _parse_dt(t.get("end_time"))
        if not end_dt:
            continue  # без end_time — пропускаем (как no_shift_tasks в 2_main)
        anchor = datetime(
            end_dt.year, end_dt.month, end_dt.day,
            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK
        )
        if end_dt < anchor:
            shift_date = (end_dt - timedelta(days=1)).date()
        else:
            shift_date = end_dt.date()

        if not (SEASON_START <= shift_date < REPORT_DATE):
            continue  # текущая смена и будущее не входят в сезон

        items = mtfmi_map.get(tid, [])
        for item in items:
            fid = item.get("field_id")
            if not fid:
                continue
            firm = field_to_firm_fn(int(fid))
            if not firm:
                continue
            if _boron_item_excluded(firm, item):
                continue
            cov_item = float(item.get("covered_area") or 0.0)
            if cov_item <= 0.5:
                continue
            season_daily[firm][shift_date]["area"] += cov_item
            if mid is not None:
                season_daily[firm][shift_date]["mids"].add(mid)

    # -------------------------------------------------------------------------
    # Проход 2: суточная статистика (только задания текущей смены)
    # -------------------------------------------------------------------------
    _cap = None
    try:
        from proverka.task_facts import bind_capture

        _cap = bind_capture()
    except Exception:
        _cap = None
    today_crop_by_firm: dict[str, dict[str, float]] = {
        firm: defaultdict(float) for firm in AGROFIRMS
    }
    for t in tasks_with_area:
        tid   = t["id"]
        mid   = t["machine_id"]
        items = mtfmi_map.get(tid, [])

        if tid not in today_task_ids:
            continue

        # Группируем items по агрофирме
        firm_items_map: dict[str, list[tuple[dict, float]]] = defaultdict(list)
        for item in items:
            fid = item.get("field_id")
            if not fid:
                continue
            firm = field_to_firm_fn(int(fid))
            if not firm:
                continue
            if _boron_item_excluded(firm, item):
                continue
            # Площадь через covered_area_hourly < cutoff (без нижней границы)
            hourly = item.get("covered_area_hourly") or []
            cov_item = 0.0
            for entry in hourly:
                h = _parse_dt(entry[0])
                if h and h < cutoff:
                    cov_item += float(entry[1] or 0.0)
            # Порог per-item 0.5 га — как в 2_main_ideal_compact
            if cov_item <= 0.5:
                continue
            firm_items_map[firm].append((item, cov_item))
            from mtfmi_batch_fetch import crop_label

            crop_name = crop_label(item)
            # «Иные культуры» в редакторе: строка сводки одна, как во 2-й сводке.
            crop_name = _plan_crop_code("2.1 Боронование", crop_name) or crop_name
            today_crop_by_firm[firm][crop_name] += cov_item
            if _cap is not None:
                from mtfmi_batch_fetch import crop_of_item

                fid_i = int(item.get("field_id") or 0)
                mrec = machines_map.get(mid) or {}
                _cap[1](
                    ("2.1 Боронование", firm, mid),
                    group="2.1 Боронование",
                    firm=firm,
                    crop=crop_of_item(item, ""),
                    field_id=fid_i,
                    task_id=int(tid),
                    machine_id=int(mid) if mid is not None else None,
                    machine=str(mrec.get("name") or ""),
                    ha=cov_item,
                    season=str(t.get("season") or ""),
                )

        for firm, fitems_cov in firm_items_map.items():

            # --- Площадь за сутки: сумма уже отфильтрованных item-значений
            cov_firm = sum(cov for _, cov in fitems_cov)
            if cov_firm <= 0:
                continue

            # Общая площадь задания в окне (для масштабирования времени)
            total_cov_in_window = 0.0
            for item in items:
                for entry in (item.get("covered_area_hourly") or []):
                    h = _parse_dt(entry[0])
                    if h and h < cutoff:
                        total_cov_in_window += entry[1] or 0

            work_dur  = t.get("work_duration") or 0
            road_move = t.get("movements_on_road_duration") or 0
            work_net  = max(work_dur - road_move, 0)

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

            pm[mid]["cov_total"] += cov_firm

            # track_area и work_distance масштабируются по доле часов в окне (как в эталоне)
            for item, _ in fitems_cov:
                hourly         = item.get("covered_area_hourly") or []
                cov_item_total = sum(e[1] or 0 for e in hourly)
                cov_item_in    = 0.0
                for entry in hourly:
                    h = _parse_dt(entry[0])
                    if h and h < cutoff:
                        cov_item_in += entry[1] or 0
                ratio = cov_item_in / cov_item_total if cov_item_total > 0 else 0.0
                pm[mid]["track_area"]       += (item.get("covered_area_by_track") or 0) * ratio
                pm[mid]["work_distance_km"] += ((item.get("work_distance") or 0) / 1000) * ratio

            if total_cov_in_window > 0:
                pm[mid]["work_dur"] += work_net * (cov_firm / total_cov_in_window)
            else:
                pm[mid]["work_dur"] += work_net

            if not pm[mid]["implement_width"]:
                imp = implements_map.get(t.get("implement_id"))
                w   = (imp.get("width") if imp else None) or 0
                pm[mid]["implement_width"] = float(w)

            sp = t.get("average_speed_on_field")
            if sp and sp > 0 and sp not in pm[mid]["speeds"]:
                pm[mid]["speeds"].append(sp)

    # -------------------------------------------------------------------------
    # Средняя выработка по сезону (как в 3-й сводке):
    # sum(площадь за сезон) / sum(машин в дни с выработкой)
    # -------------------------------------------------------------------------
    avg_season: dict[str, float] = {}
    season_components: dict[str, tuple[float, int]] = {}
    for firm in AGROFIRMS:
        daily = season_daily[firm]
        if not daily:
            avg_season[firm] = 0.0
            season_components[firm] = (0.0, 0)
            continue
        season_sum = sum(d["area"] for d in daily.values())
        machine_days = sum(
            len(d["mids"])
            for d in daily.values()
            if d["area"] > 0 and d["mids"]
        )
        avg_season[firm] = round(season_sum / machine_days, 1) if machine_days else 0.0
        season_components[firm] = (season_sum, machine_days)

    # Итоговые компоненты считаем как в 2_main: объединяем все даты всех фирм,
    # суммируем площадь и машины по каждой фирме в каждый день без фильтра area > 0
    all_dates: set = set()
    for firm in AGROFIRMS:
        all_dates.update(season_daily[firm].keys())
    total_season_sum_global = sum(
        season_daily[firm][dt]["area"]
        for firm in AGROFIRMS
        for dt in all_dates
        if dt in season_daily[firm]
    )
    total_machine_days_global = sum(
        len(season_daily[firm][dt]["mids"])
        for firm in AGROFIRMS
        for dt in all_dates
        if dt in season_daily[firm]
    )

    # -------------------------------------------------------------------------
    # Собираем day_stats
    # -------------------------------------------------------------------------
    day_stats: dict[str, dict] = {}

    for firm in AGROFIRMS:
        pm = per_firm_machine[firm]
        if not pm:
            day_stats[firm] = _empty_stats()
            continue

        detail_own:   list[dict] = []
        detail_hired: list[dict] = []


        pm_filtered = {mid: d for mid, d in pm.items()
                       if round(d["cov_total"], 2) >= MIN_DISPLAY_AREA_HA}

        # Машины с площадью >= MIN_COUNT_AREA_HA учитываются в счётчиках,
        # времени и деталях. Машины ниже порога — только в суммарной площади.
        pm_counted = {mid: d for mid, d in pm_filtered.items()
                      if round(d["cov_total"], 2) >= MIN_COUNT_AREA_HA}

        for mid, d in pm_counted.items():
            m     = machines_map.get(mid, {})
            group = m.get("machine_group_id")
            cov   = round(d["cov_total"], 2)
            secs  = d["work_dur"]
            rate  = round(cov / (secs / 3600), 1) if secs else 0.0
            speed = round(sum(d["speeds"]) / len(d["speeds"]) * 3.6) \
                    if d["speeds"] else 0

            # Коэффициент ширины захвата — как в удобрениях
            w_real  = d["implement_width"]
            l_pole  = d["work_distance_km"]
            s_chist = d["track_area"]
            coef    = round(s_chist / l_pole / w_real * 10 * 100) \
                      if w_real and l_pole else 0

            row_data = {
                "machine_id":    mid,
                "machine_name":  m.get("name", f"machine_{mid}"),
                "group":         group,
                "area":          cov,
                "track_area":    round(s_chist, 2),
                "speed":         speed,
                "work_secs":     secs,
                "rate":          rate,
                "coef":          coef,
            }
            if group == HIRED_GROUP_ID:
                detail_hired.append(row_data)
            else:
                detail_own.append(row_data)

        # Площадь суммируется по ВСЕМ машинам >= MIN_DISPLAY_AREA_HA (включая < MIN_COUNT_AREA_HA).
        # Счётчики техники и время — только по машинам >= MIN_COUNT_AREA_HA.
        area_today  = sum(d["cov_total"] for d in pm_filtered.values())
        hours_today = sum(d["work_dur"]  for d in pm_counted.values()) / 3600

        day_stats[firm] = {
            "n_mach":       len(pm_counted),
            "n_hired":      len(detail_hired),
            "area_today":   round(area_today, 2),
            "hours_today":  hours_today,
            "detail_own":   sorted(detail_own,   key=lambda x: -x["area"]),
            "detail_hired": sorted(detail_hired, key=lambda x: -x["area"]),
            "area_by_crop": {
                k: round(v, 2)
                for k, v in today_crop_by_firm[firm].items()
                if round(v, 2) > 0
            },
        }

    if _cap is not None:
        ok_pairs = {
            (firm, mid)
            for firm, pm in per_firm_machine.items()
            for mid, d in pm.items()
            if round(d["cov_total"], 2) >= MIN_DISPLAY_AREA_HA
        }
        _cap[2]("2.1 Боронование", ok_pairs)

    return day_stats, avg_season, total_season_sum_global, total_machine_days_global


def _empty_stats() -> dict:
    return {
        "n_mach": 0, "n_hired": 0,
        "area_today": 0.0, "hours_today": 0.0,
        "detail_own": [], "detail_hired": [],
    }


# =============================================================================
# СТИЛИ EXCEL (палитра как в отчётах 3, 5–8)
# =============================================================================

_COLOR_HDR = "8093B3"      # шапки колонок
_COLOR_TOTAL = "BDD7EE"   # строка «Итого»
_COLOR_TITLE_TX = "111827"  # заголовок листа без цветной полосы
_COLOR_DATA = "FFFFFF"
_COLOR_ALERT_PROD = "FCE4EC"  # выработка на 1 агрегат < плана (как сводка 1)
_FONT_SIZE_TABLE = 15


def _border() -> Border:
    t = Side(style="thin")
    return Border(left=t, right=t, top=t, bottom=t)


def _cell(ws, row: int, col: int, value=None,
          bold=False, color="000000", bg=None,
          halign="center", wrap=True, border=True) -> openpyxl.cell.Cell:
    c = ws.cell(row=row, column=col, value=value)
    c.font      = Font(bold=bold, color=color, size=_FONT_SIZE_TABLE)
    c.alignment = Alignment(horizontal=halign, vertical="center", wrap_text=wrap)
    if bg:
        c.fill = PatternFill("solid", fgColor=bg)
    if border:
        c.border = _border()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not c.number_format or c.number_format in {"General", "0"}:
            v = float(value)
            if abs(v - round(v)) < 1e-9:
                c.number_format = NF_INT
            elif abs(v - round(v, 1)) < 1e-9:
                c.number_format = NF_DEC1
            else:
                c.number_format = NF_DEC2
    return c


def _fmt_grouped_2(raw) -> str:
    try:
        return f"{float(raw):,.2f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(raw)


# =============================================================================
# ЛИСТ 1: СВОДКА
# =============================================================================

SUMMARY_COLS = [
    "№ п/п",
    "Агрофирмы",
    "Плановая выработка, га/сутки",
    "Количество задействованной техники",
    "Обработано за сутки, га",
    "Выработка за сутки, га/1 ед агрегата",
    "Средняя выработка по сезону, га/сутки",
    "Общее время работы на полях, час",
    "Среднее время эффективной работы на поле на 1 агрегат., час.",
    "Часовая выработка в расчете на 1 агрегат, га",
    "Всего собств. агрегатов в наличии",
]


def write_sheet1(wb: openpyxl.Workbook,
                 day_stats:              dict[str, dict],
                 avg_season:             dict[str, float],
                 total_season_sum:       float,
                 total_machine_days_all: int,
                 report_date:            date) -> None:
    ws = wb.create_sheet("Сводка", 0)
    n  = len(SUMMARY_COLS)

    for ci, col in enumerate(SUMMARY_COLS, start=1):
        _cell(ws, 1, ci, col, bold=True, color="FFFFFF", bg=_COLOR_HDR)
    # Длинные подписи колонок (время, часовая выработка) — запас по высоте при wrap_text.
    ws.row_dimensions[1].height = 76

    row    = 2
    t_mach = t_area = t_hours = 0.0

    for i, firm in enumerate(AGROFIRMS, start=1):
        s      = day_stats.get(firm, _empty_stats())
        n_mach = s["n_mach"]
        area   = s["area_today"]
        hours  = s["hours_today"]
        avg_s  = avg_season.get(firm, 0.0)
        _, _, plan, own_count = AGROFIRMS[firm]

        rate_for_row = round(area / n_mach, 2) if n_mach else 0.0
        vals = [
            i,
            _firm_label(firm),
            plan,
            n_mach,
            area,
            rate_for_row,
            avg_s,
            hours_to_hms(hours),
            hours_to_hms(hours / n_mach) if n_mach else "00:00:00",
            round(area  / hours,  1) if hours  else 0.0,
            own_count,
        ]
        for ci, v in enumerate(vals, start=1):
            hdr = SUMMARY_COLS[ci - 1]
            val, nf = summary_cell_value_and_format(hdr, v)
            bg = _COLOR_ALERT_PROD if (ci == 6 and n_mach and rate_for_row < plan) else _COLOR_DATA
            c = _cell(
                ws, row, ci, val,
                halign="left" if ci == 2 else "center",
                bg=bg,
            )
            if nf:
                c.number_format = nf
        label = str(vals[1] if len(vals) > 1 else "")
        ws.row_dimensions[row].height = 32 if len(label) > 36 else 28
        t_mach  += n_mach
        t_area  += area
        t_hours += hours
        row += 1

    # Итоговая средняя выработка по сезону — как в 2_main:
    # объединённые даты всех фирм, машины без фильтра area > 0
    avg_total = round(total_season_sum / total_machine_days_all, 1) if total_machine_days_all else 0.0
    tot_rate = round(t_area / t_mach, 2) if t_mach else 0.0
    first_plan = AGROFIRMS[list(AGROFIRMS.keys())[0]][2]
    totals = [
        "",
        "Итого",
        first_plan,
        int(t_mach),
        round(t_area, 2),
        round(t_area  / t_mach,  2) if t_mach  else 0.0,
        avg_total,
        hours_to_hms(t_hours),
        hours_to_hms(t_hours / t_mach) if t_mach else "00:00:00",
        round(t_area  / t_hours, 1) if t_hours else 0.0,
        sum(AGROFIRMS[f][3] for f in AGROFIRMS),
    ]
    for ci, v in enumerate(totals, start=1):
        wrap_cell = False if (ci == 2 and v == "Итого") else True
        hdr = SUMMARY_COLS[ci - 1]
        val, nf = summary_cell_value_and_format(hdr, v)
        bg = _COLOR_TOTAL
        if ci == 6 and t_mach and tot_rate < first_plan:
            bg = _COLOR_ALERT_PROD
        c = _cell(
            ws,
            row,
            ci,
            val,
            bold=True,
            halign="left" if ci == 2 else "center",
            bg=bg,
            wrap=wrap_cell,
        )
        if nf:
            c.number_format = nf
    ws.row_dimensions[row].height = 36

    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 42
    for ci in range(3, n + 1):
        letter = get_column_letter(ci)
        # Колонки H–K: длинные заголовки про время и выработку.
        if ci >= 8:
            ws.column_dimensions[letter].width = 25
        else:
            ws.column_dimensions[letter].width = 22



DETAIL_COLS = [
    "№", "Машина", "Площадь, га", "Время на полях",
    "Выр-ка га/час", "Ср. скорость км/ч",
    "Коэф. шир. захвата, %",
]

DETAIL_COL_WIDTHS_4 = [9, 42, 20, 20, 20, 20, 26]
DETAIL_PAGINATION_CFG_4 = DetailPaginationConfig(
    page_body_height_pt=505.0,
    page_safe_reserve_pt=36.0,
    section_title_height_pt=26.0,
    header_height_pt=44.0,
    data_row_height_pt=24.0,
    data_row_max_height_pt=36.0,
    total_row_height_pt=28.0,
    section_gap_height_pt=12.0,
    note_height_pt=38.0,
    wrap_chars_per_unit=0.85,
)


def write_firm_sheet(wb:          openpyxl.Workbook,
                     firm_name:   str,
                     day_stats:   dict[str, dict],
                     report_date: date,
                     day_start:   datetime,
                     cutoff:      datetime) -> None:
    stats = day_stats.get(firm_name, _empty_stats())
    detail_own = [
        r for r in (stats.get("detail_own") or [])
        if float(r.get("area") or 0.0) > 0
    ]
    detail_hired = [
        r for r in (stats.get("detail_hired") or [])
        if float(r.get("area") or 0.0) > 0
    ]
    if not detail_own and not detail_hired:
        return

    from firm_sheet_names import firm_sheet_tab_name

    ws = wb.create_sheet(firm_sheet_tab_name(firm_name))
    n = len(DETAIL_COLS)
    cfg = DETAIL_PAGINATION_CFG_4

    for ci, w in enumerate(DETAIL_COL_WIDTHS_4, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    ctx = DetailPageContext(
        ws, n, DETAIL_COL_WIDTHS_4, cfg,
        on_new_page=lambda: _write_note_4(ctx),
    )

    def _write_note_4(page_ctx: DetailPageContext) -> None:
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

    _write_note_4(ctx)

    def _detail_vals(idx: int, rd: dict) -> list[object]:
        return [
            idx, rd["machine_name"], rd["area"],
            seconds_to_hms(rd["work_secs"]),
            rd["rate"], rd["speed"], rd["coef"],
        ]

    firm_single_page = compute_firm_single_page(
        ctx, detail_own, detail_hired, _detail_vals, include_note=True,
    )
    same_firm_both_sections = bool(detail_own and detail_hired)

    def _make_section_writer(rows_data: list[dict]):
        def _caption(title: str, relaxed: bool) -> None:
            ctx.ensure_space(
                cfg.section_title_height_pt + cfg.header_height_pt,
                relaxed=relaxed,
            )
            ws.merge_cells(start_row=ctx.row, start_column=1, end_row=ctx.row, end_column=n)
            _cell(ws, ctx.row, 1, title, bold=True, color=_COLOR_TITLE_TX, bg=_COLOR_DATA)
            ws.cell(row=ctx.row, column=1).font = Font(bold=True, size=16, color=_COLOR_TITLE_TX)
            ws.row_dimensions[ctx.row].height = cfg.section_title_height_pt
            ctx.row += 1
            header_row = ctx.row
            for ci, h in enumerate(DETAIL_COLS, start=1):
                _cell(ws, ctx.row, ci, h, bold=True, color="FFFFFF", bg=_COLOR_HDR)
            ws.row_dimensions[header_row].height = cfg.header_height_pt
            ctx.row += 1
            ctx.page_used_pt += cfg.section_title_height_pt + cfg.header_height_pt

        def _data_row(r: int, vals: list[object]) -> float:
            for ci, v in enumerate(vals, start=1):
                if ci == 3:
                    v = _fmt_grouped_2(v)
                _cell(
                    ws, r, ci, v,
                    halign="left" if ci == 2 else "center",
                    bg=_COLOR_DATA,
                )
                ws.cell(row=r, column=ci).font = Font(size=11)
            h = ctx.row_height(vals)
            ws.row_dimensions[r].height = h
            return h

        def _total_row() -> float:
            tc  = round(sum(r["area"] for r in rows_data), 2)
            ts  = sum(r["work_secs"] for r in rows_data)
            tr  = round(tc / (ts / 3600), 1) if ts else 0.0
            asp = round(sum(r["speed"] for r in rows_data) / len(rows_data))
            acf = round(sum(r["coef"] for r in rows_data) / len(rows_data))
            r = ctx.row
            for ci, v in enumerate(
                ["Итого", "", tc, seconds_to_hms(ts), tr, asp, acf], start=1
            ):
                if ci == 3:
                    v = _fmt_grouped_2(v)
                wrap_cell = False if (ci == 1 and v == "Итого") else True
                _cell(ws, r, ci, v, bold=True, bg=_COLOR_TOTAL, wrap=wrap_cell)
                ws.cell(row=r, column=ci).font = Font(size=11, bold=True)
            ws.row_dimensions[r].height = cfg.total_row_height_pt
            return cfg.total_row_height_pt

        return _caption, _data_row, _total_row

    if detail_own:
        cap, data, tot = _make_section_writer(detail_own)
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
        cap, data, tot = _make_section_writer(detail_hired)
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
# ГЛАВНАЯ ФУНКЦИЯ
# =============================================================================

def sync_season_start_for_report(report_date: date | None = None) -> None:
    """
    SEASON_START как при генерации Excel: 1 марта (или env), а не season_unified 1 янв.
    Proverka вызывает перед расчётом — иначе history_items/сезон расходятся с Excel.
    """
    global SEASON_START, REPORT_DATE
    rd = report_date or REPORT_DATE
    REPORT_DATE = rd
    raw = (
        os.environ.get("SVODKI_SEASON_START_DATE")
        or os.environ.get("SEASON_START_DATE")
        or ""
    ).strip()
    if raw:
        try:
            SEASON_START = date.fromisoformat(raw)
        except ValueError:
            SEASON_START = date(rd.year, 3, 1)
    else:
        SEASON_START = date(rd.year, 3, 1)
    try:
        from season_unified import season_start_from_editor

        SEASON_START = season_start_from_editor("2.1 Боронование", rd, SEASON_START)
    except Exception:
        pass


async def compute_harrowing_facts(
    report_date: date | None = None,
) -> tuple[dict[str, dict], dict[str, float], float, int, datetime, datetime]:
    """Факт за смену + сезон — тот же путь, что build_report_async / proverka."""
    rd = report_date or REPORT_DATE
    sync_season_start_for_report(rd)
    day_start, cutoff = compute_time_range(rd)

    async def _fetch_facts():
        async with report_async_client(API_TOKEN, semaphore_limit=SEMAPHORE_LIMIT, timeout=120) as (client, sem):
            return await fetch_all_data(client, sem, day_start, cutoff)

    _, fetched = await run_parallel_refs_and_fetch(
        load_reference_data, _fetch_facts, label="r4-tasks+mtfmi",
    )
    day_stats, avg_season, total_season_sum, total_machine_days = fetched
    return day_stats, avg_season, total_season_sum, total_machine_days, day_start, cutoff


async def build_report_async() -> None:
    day_start, cutoff = compute_time_range(REPORT_DATE)

    print(f"Отчёт: {WORK_TYPE_TITLE}  |  {REPORT_DATE}")
    print(f"Смена: {day_start.strftime('%d.%m %H:%M')} — {cutoff.strftime('%d.%m %H:%M')}\n")

    print("Загружаем справочники и данные за сезон и за сутки...")
    timing_start()
    with stage("справочники ∥ задания+mtfmi+ресурсы"):
        day_stats, avg_season, total_season_sum, total_machine_days, day_start, cutoff = (
            await compute_harrowing_facts(REPORT_DATE)
        )

    print("\nФормируем Excel...")
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    write_sheet1(wb, day_stats, avg_season, total_season_sum, total_machine_days, REPORT_DATE)
    for firm in AGROFIRMS:
        write_firm_sheet(wb, firm, day_stats, REPORT_DATE, day_start, cutoff)

    output_file = f"report_harrowing_{REPORT_DATE}.xlsx"
    daily_total_ha = round(
        sum(float((day_stats.get(f) or {}).get("area_today") or 0) for f in AGROFIRMS),
        2,
    )
    with stage("Excel: сохранение"):
        wb.save(output_file)
    try:
        from svodki_sanity import emit_calc_total_ha, publish_cross_op_snapshot

        emit_calc_total_ha(daily_total_ha)
        publish_cross_op_snapshot({"2.1 Боронование": daily_total_ha})
    except Exception:
        pass
    print(f"\nСохранено: {output_file}")
    timing_finish()


def build_report() -> None:
    run_report_async(build_report_async())


if __name__ == "__main__":
    build_report()
