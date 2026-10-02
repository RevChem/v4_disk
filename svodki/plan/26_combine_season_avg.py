"""
Отчёт «Выработка комбайнов по агрофирмам»
==========================================
Двухколоночная раскладка: собственные (A–J) | наёмные (L–U).

Колонки (×2 группы):
  №  |  Комбайн  |  Кол-во дней работы  |  Суммарное время на полях, ч  |
  Макс. время за смену (HH:MM)  |  Средняя продолжит. за смену, ч  |
  Обработанная площадь, га  |
  Средняя выработка с учётом погоды в ДЕНЬ, га  |
  Средняя выработка с учётом погоды в ЧАС, га  |
  Средняя выработка без учёта погоды, га

Период: DATE_FROM … DATE_TO.
«Кол-во дней работы» — дни, когда обработанная площадь > 0.5 га.
«Без учёта погоды» = area / calendar_days, где calendar_days = DATE_TO - HARVEST_START[firm] + 1.
Зелёная подсветка (E2EEDA) — «Средняя выработка с учётом погоды в ДЕНЬ» выше
среднего арифметического по этому столбцу в блоке (собственные / наёмные отдельно).
"""
from __future__ import annotations


import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import plans  # noqa: F401  # bootstrap utils/* (plan_paths via plans.py)
from cropwise_paging import CROPWISE_PAGE_LIMIT, from_id_should_continue
from report_fetch_orchestration import report_async_client, run_report_async
from report_pipeline import run_parallel_refs_and_fetch
from svodki_timing import stage, timing_finish, timing_start

def _bootstrap_api_hooks() -> None:
    """Prefetch/API cache (sitecustomize) до requests/httpx."""
    here = Path(__file__).resolve().parent
    for sc in (here / "sitecustomize.py", here.parent / "new" / "sitecustomize.py"):
        if not sc.is_file():
            continue
        import importlib.util

        mod_name = "_svodki_sitecustomize_hooks_r26"
        mod = sys.modules.get(mod_name)
        if mod is None:
            spec = importlib.util.spec_from_file_location(mod_name, sc)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
        install = getattr(mod, "install_svodki_api_hooks", None)
        if callable(install):
            install()
        return


_bootstrap_api_hooks()

import httpx
import openpyxl
import requests
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

_plans_loaded = False
try:
    from plans import (
        API_TOKEN, BASE_URL, TZ_MSK,
        FIRM_ORDER, FIRM_TO_FOLDER, FOLDER_TO_FIRM,
        HIRED_GROUP_ID, FIELD_NAME_FORCE_RULES, SKIP_CROPS,
    )
    _plans_loaded = True
except ImportError:
    try:
        from plans import (
            API_TOKEN, BASE_URL, TZ_MSK,
            FIRM_ORDER, FIRM_TO_FOLDER, FOLDER_TO_FIRM,
            HIRED_GROUP_ID, FIELD_NAME_FORCE_RULES, SKIP_CROPS,
        )
        _plans_loaded = True
    except ImportError:
        pass

if not _plans_loaded:
    API_TOKEN      = "6DayNcBDr9BWEZgst1Dy"
    BASE_URL       = "https://operations.cropwise.com/api/v3"
    TZ_MSK         = timezone(timedelta(hours=3))
    HIRED_GROUP_ID = 7
    FIRM_ORDER     = [
        "АФ Чистопольская", "АгроРаздолье", "Аксу-Агро",
        "НурлатАгро", "Дрожжаное Рассвет Агро", "Актай",
        "Лениногорская Птицефабрика",
    ]
    FIRM_TO_FOLDER = {
        "АФ Чистопольская": 3, "АгроРаздолье": 4, "Аксу-Агро": 5,
        "НурлатАгро": 11, "Дрожжаное Рассвет Агро": 6,
        "Актай": 7, "Лениногорская Птицефабрика": 9,
    }
    FOLDER_TO_FIRM = {v: k for k, v in FIRM_TO_FOLDER.items()}
    SKIP_CROPS     = {
        "Залежь, лес", "Передано в аренду",
        "Дороги/лесополосы", "Овраги/неудобия",
    }
    FIELD_NAME_FORCE_RULES = []

# Период отчёта: DATE_FROM из настроек платформы (сводка 26), DATE_TO = REPORT_DATE
_env_from = os.environ.get("DATE_FROM")
_env_to = os.environ.get("DATE_TO")
_env_report = os.environ.get("REPORT_DATE")
_env_combine = os.environ.get("SVODKI_COMBINE_SEASON_START")


def _read_combine_start_from_manual() -> date | None:
    raw_path = (os.environ.get("SVODKI_MANUAL_OVERRIDES_PATH") or "").strip()
    if not raw_path:
        return None
    try:
        import json as _json
        from pathlib import Path as _Path

        data = _json.loads(_Path(raw_path).read_text(encoding="utf-8"))
        val = str((data.get("26") or {}).get("combine_season_start_date") or "").strip()
        if val:
            return date.fromisoformat(val.split("T")[0])
    except Exception:
        pass
    return None


if _env_to:
    DATE_TO = date.fromisoformat(_env_to.split("T")[0])
elif _env_report:
    DATE_TO = date.fromisoformat(_env_report.split("T")[0])
else:
    DATE_TO = date(2025, 9, 13)
if _env_from:
    DATE_FROM = date.fromisoformat(_env_from.split("T")[0])
elif _env_combine:
    DATE_FROM = date.fromisoformat(_env_combine.split("T")[0])
else:
    _manual_start = _read_combine_start_from_manual()
    if _manual_start:
        DATE_FROM = _manual_start
    elif _env_report:
        _rd = date.fromisoformat(_env_report.split("T")[0])
        _harvest = date(_rd.year, 6, 10)
        DATE_FROM = date(_rd.year - 1, 6, 10) if _rd < _harvest else _harvest
    else:
        DATE_FROM = date(2025, 6, 10)

# Дата начала уборки по каждой агрофирме (для расчёта «без учёта погоды»)
_HARVEST_FIRMS = [
    "АФ Чистопольская",
    "АгроРаздолье",
    "Аксу-Агро",
    "НурлатАгро",
    "Дрожжаное Рассвет Агро",
    "Актай",
    "Лениногорская Птицефабрика",
]
HARVEST_START: dict[str, date] = {name: DATE_FROM for name in _HARVEST_FIRMS}

OUTPUT_FILE = f"combine_report_{DATE_FROM}_{DATE_TO}.xlsx"

SEMAPHORE_LIMIT  = int(os.environ.get("SVODKI_COMBINE_MTFMI_CONCURRENCY", "80"))
SHIFT_START_HOUR = 6        # граница смены — 06:00 МСК
MIN_WORK_AREA    = 0.5      # га — порог «рабочего дня»

_PREFETCH_ENDPOINTS: dict | None = None
_PREFETCH_LOAD_DONE = False

# =============================================================================
# HTTP
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


def _prefetch_endpoints() -> dict | None:
    from svodki_prefetch_io import load_prefetch_endpoints

    global _PREFETCH_ENDPOINTS, _PREFETCH_LOAD_DONE
    if _PREFETCH_LOAD_DONE:
        return _PREFETCH_ENDPOINTS
    _PREFETCH_LOAD_DONE = True
    _PREFETCH_ENDPOINTS = load_prefetch_endpoints()
    return _PREFETCH_ENDPOINTS


def _mtfmi_map_from_prefetch(task_ids: set[int]) -> dict[int, list[dict]]:
    from svodki_prefetch_io import mtfmi_subset_from_prefetch

    return mtfmi_subset_from_prefetch(task_ids)


def get_all(endpoint: str, params: dict | None = None) -> list[dict]:
    p = dict(params or {})
    results, from_id = [], 0
    for _ in range(500):
        r = session.get(
            f"{BASE_URL}/{endpoint}",
            params={**p, "from_id": from_id, "limit": CROPWISE_PAGE_LIMIT},
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
        if not from_id_should_continue(meta, chunk):
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


async def fetch_mtfmi_async(task_ids: list[int]) -> dict[int, list[dict]]:
    task_set = set(task_ids)
    out = _mtfmi_map_from_prefetch(task_set)
    missing = [tid for tid in task_ids if tid not in out]
    if not missing:
        print(f"  mtfmi из prefetch JSON: {len(out)} задач (без HTTP)", flush=True)
        return out
    if out:
        print(
            f"  mtfmi из prefetch: {len(out)}, догружаем HTTP: {len(missing)}",
            flush=True,
        )
    else:
        print(f"  Загружаем mtfmi (async batch): {len(missing)} задач...", flush=True)

    from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks

    async with report_async_client(API_TOKEN, semaphore_limit=SEMAPHORE_LIMIT, timeout=120) as (client, sem):
        live = await fetch_mtfmi_map_for_tasks(
            client, sem, BASE_URL, missing, log_prefix="[r26] ",
        )
    out.update(live)
    return out


# =============================================================================
# ВСПОМОГАТЕЛЬНОЕ
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
        except Exception:
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


def shift_day(dt: datetime) -> date:
    """День смены: если время < 06:00, относим к предыдущему дню."""
    if dt.hour < SHIFT_START_HOUR:
        return (dt - timedelta(days=1)).date()
    return dt.date()


def fmt_hhmm(total_seconds: float) -> str:
    if total_seconds <= 0:
        return "00:00:00"
    total = int(total_seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


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


def _resolve_firm(folder_id, parent_map, root_ids):
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
# ЗАГРУЗКА СПРАВОЧНИКОВ
# =============================================================================

def load_references():
    ep = _prefetch_endpoints()
    if ep:
        print("  Справочники из prefetch JSON...", flush=True)
        fields_all = ep.get("fields") or get_all("fields")
        fgs_all = ep.get("field_groups") or get_all("field_groups")
        gfs_all = ep.get("group_folders") or get_all("group_folders")
        machines_all = ep.get("machines") or get_all("machines", {"machine_subtype": "harvester"})
    else:
        print("  Поля...")
        fields_all = get_all("fields")
        fgs_all = get_all("field_groups")
        gfs_all = get_all("group_folders")
        print("  Машины (комбайны)...")
        machines_all = get_all("machines", {"machine_subtype": "harvester"})
    field_to_firm = build_field_to_firm(fields_all, fgs_all, gfs_all)
    machine_name:  dict[int, str] = {}
    machine_group: dict[int, int] = {}
    machine_reg:   dict[int, str] = {}
    machine_vin:   dict[int, str] = {}
    for m in machines_all:
        mid = m.get("id")
        if mid is None:
            continue
        if m.get("machine_subtype") != "harvester":
            continue
        machine_name[mid]  = m.get("name") or m.get("title") or m.get("model") or f"machine_{mid}"
        machine_group[mid] = m.get("machine_group_id") or 0
        machine_reg[mid]   = m.get("registration_number") or m.get("reg_number") or ""
        machine_vin[mid]   = m.get("vin") or m.get("serial_number") or ""

    return field_to_firm, machine_name, machine_group, machine_reg, machine_vin


# =============================================================================
# СБОРКА ДАННЫХ
# =============================================================================

def _fetch_period_tasks_live(mt_params: dict[str, str]) -> list[dict]:
    """Все machine_tasks периода без prefetch.

    Длинный период (> окна в днях) — параллельные окна по датам; иначе — прежний
    одиночный курсор. Любая ошибка оконного пути → откат на прежний get_all.
    """
    try:
        from live_machine_tasks_fetch import (
            fetch_machine_tasks_date_windows_async,
            should_use_windowed_live_fetch,
        )

        if should_use_windowed_live_fetch(mt_params):
            async def _windowed():
                async with report_async_client(
                    API_TOKEN, semaphore_limit=SEMAPHORE_LIMIT, timeout=120,
                ) as (client, sem):
                    return await fetch_machine_tasks_date_windows_async(
                        client, BASE_URL, mt_params, sem, log_prefix="[r26] ",
                    )

            return run_report_async(_windowed())
    except Exception as exc:
        print(f"  [r26] оконная загрузка не удалась ({exc}); один курсор", flush=True)
    return get_all("machine_tasks", mt_params)


def _fetch_period_machine_tasks() -> list[dict]:
    period_start = datetime(
        DATE_FROM.year, DATE_FROM.month, DATE_FROM.day,
        SHIFT_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    period_end = datetime(
        DATE_TO.year, DATE_TO.month, DATE_TO.day,
        SHIFT_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    ) + timedelta(days=1)
    start_str = period_start.strftime("%Y-%m-%dT%H:%M:%S")
    end_str = period_end.strftime("%Y-%m-%dT%H:%M:%S")
    from svodki_prefetch_io import prefetch_tasks_by_wt, sync_machine_tasks_prefetch_or_batch

    print(f"\n  Загружаем machine_tasks ({start_str} — {end_str})...")
    mt_params = {"end_time_gt_eq": start_str, "end_time_lt": end_str}
    index = prefetch_tasks_by_wt()
    wt_scope = index.keys() if index else set()
    _t_fetch = time.monotonic()
    if index:
        all_tasks = sync_machine_tasks_prefetch_or_batch(
            get_all, wt_scope, mt_params, log_prefix="[r26] ",
        )
    else:
        all_tasks = _fetch_period_tasks_live(mt_params)
    print(
        f"  [timing] r26 machine_tasks: {time.monotonic() - _t_fetch:.1f} с "
        f"({'prefetch/batch' if index else 'live'})",
        flush=True,
    )
    seen: set[int] = set()
    unique_tasks: list[dict] = []
    for t in all_tasks:
        tid = t.get("id")
        if tid not in seen:
            seen.add(tid)
            unique_tasks.append(t)
    print(f"  Задач всего: {len(unique_tasks)}")
    return unique_tasks


def collect_data(
    field_to_firm,
    machine_name,
    machine_group,
    machine_reg,
    machine_vin,
    *,
    all_tasks: list[dict] | None = None,
):
    period_start = datetime(DATE_FROM.year, DATE_FROM.month, DATE_FROM.day,
                            SHIFT_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    period_end   = datetime(DATE_TO.year, DATE_TO.month, DATE_TO.day,
                            SHIFT_START_HOUR, 0, 0, tzinfo=TZ_MSK) + timedelta(days=1)

    if all_tasks is None:
        all_tasks = _fetch_period_machine_tasks()
    if not all_tasks:
        return {}

    # Фильтруем до комбайнов ДО async-запросов — не тянем mtfmi для чужих задач
    harvester_tasks = [t for t in all_tasks if t.get("machine_id") in machine_name]
    print(f"  Задач комбайнов: {len(harvester_tasks)}")
    if not harvester_tasks:
        return {}

    task_ids  = [t["id"] for t in harvester_tasks]
    print("  Загружаем field mapping items (async)...")
    mtfmi_map = run_report_async(fetch_mtfmi_async(task_ids))

    # machine_id → firm → day → {secs, area}
    machine_firm_days: dict[int, dict] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: {"secs": 0.0, "area": 0.0}))
    )

    for task in harvester_tasks:
        tid = task["id"]
        mid = task.get("machine_id")
        if not mid:
            continue

        t_start = parse_dt(task.get("start_time") or task.get("started_at"))
        t_end   = parse_dt(task.get("end_time")   or task.get("finished_at"))
        if not t_start or not t_end or t_end <= t_start:
            continue
        if t_end < period_start or t_start > period_end:
            continue

        t_start = max(t_start, period_start)
        t_end   = min(t_end,   period_end)

        if (t_end - t_start).total_seconds() <= 0:
            continue

        items = mtfmi_map.get(tid, [])

        # Время работы на полях = work_duration − переезды по дорогам.
        work_dur = float(task.get("work_duration") or 0)
        road_dur = float(task.get("movements_on_road_duration") or 0)
        work_net_secs = max(work_dur - road_dur, 0)

        # Фолбэк если work_duration отсутствует
        if work_net_secs == 0:
            work_net_secs = (t_end - t_start).total_seconds()

        if work_net_secs <= 0:
            continue

        # Площадь задачи по каждой агрофирме
        firm_task_area: dict[str, float] = defaultdict(float)
        for item in items:
            fid = item.get("field_id")
            cov = float(item.get("covered_area") or 0)
            if cov > 0 and fid and fid in field_to_firm:
                firm_task_area[field_to_firm[fid]] += cov

        if not firm_task_area:
            continue  # нет привязки к агрофирмам — пропускаем задачу

        # Нарезаем по границам смен (06:00 МСК).
        # work_net_secs задачи идёт целиком в каждую фирму, где есть площадь —
        # комбайн работал это время, независимо от того, на сколько фирм поделены поля.
        wall_secs = (t_end - t_start).total_seconds()

        boundaries: list[datetime] = [t_start]
        cur = t_start.replace(hour=SHIFT_START_HOUR, minute=0, second=0, microsecond=0)
        if cur <= t_start:
            cur += timedelta(days=1)
        while cur < t_end:
            boundaries.append(cur)
            cur += timedelta(days=1)
        boundaries.append(t_end)

        for i in range(len(boundaries) - 1):
            seg_start = boundaries[i]
            seg_end   = boundaries[i + 1]
            seg_wall  = (seg_end - seg_start).total_seconds()
            if seg_wall <= 0:
                continue

            ratio = seg_wall / wall_secs if wall_secs > 0 else 1.0
            day   = shift_day(seg_start)

            for firm, f_area in firm_task_area.items():
                machine_firm_days[mid][firm][day]["secs"] += work_net_secs * ratio
                machine_firm_days[mid][firm][day]["area"] += f_area * ratio

    result: dict[str, list[dict]] = defaultdict(list)

    for mid, firms_data in machine_firm_days.items():
        m_name = machine_name.get(mid, f"machine_{mid}")
        m_reg  = machine_reg.get(mid, "")
        m_vin  = machine_vin.get(mid, "")
        parts  = [m_name]
        if m_reg:
            parts.append(m_reg)
        if m_vin:
            parts.append(m_vin)
        machine_str = " ".join(parts)

        is_hired = (machine_group.get(mid, 0) == HIRED_GROUP_ID)

        for firm, firm_days in firms_data.items():
            # Рабочие дни — только те, где площадь этой фирмы > MIN_WORK_AREA
            working_days = {d: v for d, v in firm_days.items() if v["area"] > MIN_WORK_AREA}
            n_days    = len(working_days)
            firm_area = sum(v["area"] for v in firm_days.values())
            total_sec = sum(v["secs"] for v in firm_days.values())
            max_sec   = max(v["secs"] for v in firm_days.values())

            if firm_area <= 0:
                continue

            avg_sec          = total_sec / n_days if n_days else 0.0
            avg_day_weather  = firm_area / n_days if n_days else 0.0
            avg_hour_weather = firm_area / (total_sec / 3600) if total_sec > 0 else 0.0

            cal_start      = HARVEST_START.get(firm, DATE_FROM)
            cal_days       = (DATE_TO - cal_start).days + 1
            avg_no_weather = firm_area / cal_days if cal_days > 0 else 0.0

            result[firm].append({
                "machine":          machine_str,
                "is_hired":         is_hired,
                "n_days":           n_days,
                "total_h":          total_sec / 3600,
                "max_sec":          max_sec,
                "avg_h":            avg_sec / 3600,
                "area":             firm_area,
                "avg_day_weather":  avg_day_weather,
                "avg_hour_weather": avg_hour_weather,
                "avg_no_weather":   avg_no_weather,
                "cal_days":         cal_days,
            })

    for firm in result:
        result[firm].sort(key=lambda x: (x["is_hired"], -x["area"]))

    return result


# =============================================================================
# СТИЛИ
# =============================================================================

# Палитра как в «Средняя выработка комбайнов за сезон на 12_09_2025 исходник(3).xlsx»
_C_HDR      = "8093B3"   # шапка колонок (оба блока)
_C_GREEN    = "E2EEDA"   # выработка в ДЕНЬ выше среднего по столбцу
_C_ROW_O    = "FFFFFF"   # обычная строка
_C_FIRM_A   = "F4F6F8"   # метка агрофирмы в колонке A (между блоками)
_C_TOTAL    = "DFE4EC"   # итоговая строка


def _side() -> Border:
    t = Side(style="thin", color="BBBBBB")
    return Border(left=t, right=t, top=t, bottom=t)


def _cell(ws, row, col, value=None, bold=False, bg=None,
          color="000000", halign="center", wrap=False,
          fmt=None, size=9, italic=False):
    c = ws.cell(row=row, column=col, value=value)
    c.font      = Font(name="Calibri", size=size, bold=bold,
                       color=color, italic=italic)
    c.alignment = Alignment(horizontal=halign, vertical="center",
                            wrap_text=wrap)
    c.border    = _side()
    if bg:
        c.fill = PatternFill("solid", fgColor=bg)
    if fmt:
        c.number_format = fmt
    return c


# Колонки блока собственных: A=1..J=10, разделитель K=11, блок наёмных L=12..U=21
# Структура одного блока (10 колонок):
#  1:№  2:Комбайн  3:Дни  4:Сумм.ч  5:Макс(HH:MM)  6:Ср.ч/смена
#  7:Площадь  8:Ср/день(погода)  9:Ср/час(погода)  10:Без погоды
N_BLOCK = 10   # колонок в одном блоке
SEP_COL = 11   # разделитель между блоками
OWN_START = 1
HRD_START = 12  # SEP_COL + 1

HEADERS_BLOCK = [
    "№",
    "Комбайн",
    "Количество дней работы",
    "Суммарное время работы на полях, часов",
    "Максимальное время работы на полях, ч",
    "Средняя продолжительность за смену, часов",
    "Обработанная площадь, га",
    "Средняя выработка с учетом погоды в ДЕНЬ, га",
    "Средняя выработка с учетом погоды в ЧАС, га",
    "Средняя выработка без учета погоды, га",
]

_FIRM_LABEL = {
    "АФ Чистопольская":           "ЧИСТОПОЛЬСКАЯ",
    "АгроРаздолье":               "АГРОРАЗДОЛЬЕ",
    "Аксу-Агро":                  "АКСУ-АГРО",
    "НурлатАгро":                 "НУРЛАТАГРО",
    "Дрожжаное Рассвет Агро":     "ДРА",
    "Актай":                      "АКТАЙ",
    "Лениногорская Птицефабрика": "ЛПФ",
}

COL_W_OWN = [4, 38, 9, 11, 11, 11, 11, 11, 11, 11]
COL_W_HRD = [4, 38, 9, 11, 11, 11, 11, 11, 11, 11]
COL_W_SEP = 2


# =============================================================================
# ЛИСТ «СВОД»
# =============================================================================

# Короткие названия агрофирм для Свода
_FIRM_SHORT = {
    "АФ Чистопольская":           "Чистопольская",
    "АгроРаздолье":               "АгроРаздолье",
    "Аксу-Агро":                  "Аксу-Агро",
    "НурлатАгро":                 "НурлатАгро",
    "Дрожжаное Рассвет Агро":     "ДРА",
    "Актай":                      "Актай",
    "Лениногорская Птицефабрика": "ЛПФ",
}

# Лист «Свод» — нейтральная палитра как в исходнике
_S_HDR   = "DFE4EC"
_S_TOTAL = "DFE4EC"


def write_svod(
    wb: "openpyxl.Workbook",
    result: dict,
    firm_total_rows: dict[str, int],
) -> None:
    """Добавляет лист «Свод» со сводными показателями по агрофирмам."""
    ws = wb.create_sheet("Свод")

    # Колонки: A=1(№) B=2(Агрофирма)
    # Собственные: C=3(ср.прод/смену) D=4(площадь) E=5(ср.макс) F=6(ср/день) G=7(ср/час)
    # Наёмные:     H=8(ср.прод/смену) I=9(площадь) J=10(ср.макс) K=11(ср/день) L=12(ср/час)

    # Ширины
    col_widths = {
        "A": 4, "B": 24,
        "C": 22, "D": 17, "E": 22, "F": 15, "G": 15,
        "H": 22, "I": 17, "J": 22, "K": 15, "L": 15,
    }
    for col, w in col_widths.items():
        ws.column_dimensions[col].width = w

    def sc(row, col, value=None, bold=False, bg=None, color="000000",
           halign="center", wrap=False, fmt=None, size=11, italic=False):
        c = ws.cell(row=row, column=col, value=value)
        c.font      = Font(name="Calibri", size=size, bold=bold,
                           color=color, italic=italic)
        c.alignment = Alignment(horizontal=halign, vertical="center",
                                wrap_text=wrap)
        c.border    = _side()
        if bg:
            c.fill = PatternFill("solid", fgColor=bg)
        if fmt:
            c.number_format = fmt
        return c

    # ── Строка 1: заголовок ───────────────────────────────────────────────────
    ws.merge_cells("A1:L1")
    c = ws.cell(
        row=1, column=1,
        value=(
            f"Средняя выработка комбайнов за сезон  |  "
            f"{DATE_FROM.strftime('%d.%m.%Y')} — {DATE_TO.strftime('%d.%m.%Y')}"
        ),
    )
    c.font      = Font(name="Calibri", size=13, bold=True, color="000000")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.border    = _side()
    ws.row_dimensions[1].height = 30

    # ── Строки 2–3: шапка (2 уровня) ─────────────────────────────────────────
    ws.merge_cells("A2:A3")
    sc(2, 1, "№", bold=True, bg=_S_HDR, size=11)

    ws.merge_cells("B2:B3")
    sc(2, 2, "Агрофирма", bold=True, bg=_S_HDR, halign="center", size=12)

    ws.merge_cells("C2:G2")
    sc(2, 3, "Собственные", bold=True, bg=_S_HDR, size=12)

    ws.merge_cells("H2:L2")
    sc(2, 8, "Наёмные", bold=True, bg=_S_HDR, size=12)

    sub_headers = [
        "Средняя продолжительность\nработы на полях\nза смену, часов",
        "Обработанная\nплощадь, га",
        "Среднее максимальное\nвремя работы\nна полях, час",
        "Средняя выработка\nв ДЕНЬ, га",
        "Средняя выработка\nв ЧАС, га",
    ]
    for i, h in enumerate(sub_headers):
        sc(3, 3 + i, h, bold=True, bg=_S_HDR, halign="center", wrap=True, size=10)
        sc(3, 8 + i, h, bold=True, bg=_S_HDR, halign="center", wrap=True, size=10)

    ws.row_dimensions[2].height = 16
    ws.row_dimensions[3].height = 52

    # ── Строки данных по агрофирмам ───────────────────────────────────────────
    # Формируем список агрофирм в порядке FIRM_ORDER, только те что есть
    ordered_firms = [f for f in FIRM_ORDER if f in firm_total_rows]
    # Добавляем те, что не в FIRM_ORDER
    for f in firm_total_rows:
        if f not in FIRM_ORDER:
            ordered_firms.append(f)

    # Маппинг колонок итоговой строки «По технике»:
    #   OWN_START=1 → col1=label, col2='', col3=days, col4=tot_h,
    #                  col5=avg_max, col6=avg_h, col7=area, col8=avg_d, col9=avg_hh, col10=avg_no_w
    #   HRD_START=12 → аналогично
    # В Своде нам нужны абсолютные номера колонок листа «По технике»:
    #   ср.прод/смену = OWN_START+5=6 (F) / HRD_START+5=17 (Q)
    #   площадь       = OWN_START+6=7 (G) / HRD_START+6=18 (R)
    #   ср.макс       = OWN_START+4=5 (E) / HRD_START+4=16 (P)
    #   ср/день       = OWN_START+7=8 (H) / HRD_START+7=19 (S)
    #   ср/час        = OWN_START+8=9 (I) / HRD_START+8=20 (T)
    OWN_OFFSET = {"avg_h": 5, "area": 6, "avg_max": 4, "avg_d": 7, "avg_hh": 8}

    def own_col(key):
        return get_column_letter(OWN_START + OWN_OFFSET[key])

    def hrd_col(key):
        return get_column_letter(HRD_START + OWN_OFFSET[key])

    data_row_start = 4
    n = len(ordered_firms)

    for idx, firm in enumerate(ordered_firms):
        row = data_row_start + idx
        total_r = firm_total_rows[firm]
        short   = _FIRM_SHORT.get(firm, firm)
        sc(row, 1, idx + 1, halign="center", size=11)
        sc(row, 2, short,   halign="left",   size=14)

        # Собственные — ссылки на лист «По технике»
        sc(row, 3,  f"='По технике'!{own_col('avg_h')}{total_r}",
           halign="center", size=14, fmt="# ##0.00")
        sc(row, 4,  f"='По технике'!{own_col('area')}{total_r}",
           halign="center", size=14, fmt="# ##0.00")
        sc(row, 5,  f"='По технике'!{own_col('avg_max')}{total_r}",
           halign="center", size=14, fmt="# ##0.00")
        sc(row, 6,  f"='По технике'!{own_col('avg_d')}{total_r}",
           halign="center", size=14, fmt="# ##0.00")
        sc(row, 7,  f"='По технике'!{own_col('avg_hh')}{total_r}",
           halign="center", size=14, fmt="# ##0.00")

        # Наёмные — те же строки, но колонки HRD_START+offset
        sc(row, 8,  f"='По технике'!{hrd_col('avg_h')}{total_r}",
           halign="center", size=14, fmt="# ##0.00")
        sc(row, 9,  f"='По технике'!{hrd_col('area')}{total_r}",
           halign="center", size=14, fmt="# ##0.00")
        sc(row, 10, f"='По технике'!{hrd_col('avg_max')}{total_r}",
           halign="center", size=14, fmt="# ##0.00")
        sc(row, 11, f"='По технике'!{hrd_col('avg_d')}{total_r}",
           halign="center", size=14, fmt="# ##0.00")
        sc(row, 12, f"='По технике'!{hrd_col('avg_hh')}{total_r}",
           halign="center", size=14, fmt="# ##0.00")

        ws.row_dimensions[row].height = 17

    # ── Строка ИТОГО ──────────────────────────────────────────────────────────
    total_row = data_row_start + n
    ws.merge_cells(f"A{total_row}:B{total_row}")
    sc(total_row, 1, "ИТОГО:", bold=True, bg=_S_TOTAL, halign="right", size=14)

    r1, r2 = data_row_start, data_row_start + n - 1
    # Собственные: площадь=SUM, остальное=AVERAGE
    sc(total_row, 3,  f"=AVERAGE(C{r1}:C{r2})", bg=_S_TOTAL, halign="center", fmt="# ##0.00")
    sc(total_row, 4,  f"=SUM(D{r1}:D{r2})",     bg=_S_TOTAL, halign="center", fmt="# ##0.00")
    sc(total_row, 5,  f"=AVERAGE(E{r1}:E{r2})", bg=_S_TOTAL, halign="center", fmt="# ##0.00")
    sc(total_row, 6,  f"=AVERAGE(F{r1}:F{r2})", bg=_S_TOTAL, halign="center", fmt="# ##0.00")
    sc(total_row, 7,  f"=AVERAGE(G{r1}:G{r2})", bg=_S_TOTAL, halign="center", fmt="# ##0.00")
    # Наёмные
    sc(total_row, 8,  f"=AVERAGE(H{r1}:H{r2})", bg=_S_TOTAL, halign="center", fmt="# ##0.00")
    sc(total_row, 9,  f"=SUM(I{r1}:I{r2})",     bg=_S_TOTAL, halign="center", fmt="# ##0.00")
    sc(total_row, 10, f"=AVERAGE(J{r1}:J{r2})", bg=_S_TOTAL, halign="center", fmt="# ##0.00")
    sc(total_row, 11, f"=AVERAGE(K{r1}:K{r2})", bg=_S_TOTAL, halign="center", fmt="# ##0.00")
    sc(total_row, 12, f"=AVERAGE(L{r1}:L{r2})", bg=_S_TOTAL, halign="center", fmt="# ##0.00")
    ws.row_dimensions[total_row].height = 17


# =============================================================================
# ЗАПИСЬ ОТЧЁТА
# =============================================================================

def write_report(result: dict[str, list[dict]]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "По технике"

    # Ширины колонок
    for i, w in enumerate(COL_W_OWN, start=OWN_START):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.column_dimensions[get_column_letter(SEP_COL)].width = COL_W_SEP
    for i, w in enumerate(COL_W_HRD, start=HRD_START):
        ws.column_dimensions[get_column_letter(i)].width = w

    LAST_COL = HRD_START + N_BLOCK - 1  # = 21

    r = 1

    # ── Заголовок отчёта ──────────────────────────────────────────────────────
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=LAST_COL)
    c = ws.cell(
        row=r, column=1,
        value=(
            f"Средняя выработка комбайнов за сезон  |  "
            f"{DATE_FROM.strftime('%d.%m.%Y')} — {DATE_TO.strftime('%d.%m.%Y')}"
        ),
    )
    c.font      = Font(name="Calibri", size=13, bold=True, color="000000")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.border    = _side()
    ws.row_dimensions[r].height = 26
    r += 1

    # firm -> номер строки итогов (одна строка для own и hrd)
    firm_total_rows: dict[str, int] = {}
    firm_block_idx = 0

    def write_firm_block(firm: str, own_rows: list[dict], hrd_rows: list[dict]) -> None:
        nonlocal r, firm_block_idx

        cal_days = (own_rows or hrd_rows)[0]["cal_days"] if (own_rows or hrd_rows) else 0
        firm_label = _FIRM_LABEL.get(firm, firm).upper()

        # ── Строка агрофирмы (как в исходнике: метка в A или C) ───────────────
        if firm_block_idx > 0:
            _cell(ws, r, 1, firm_label, bold=True, bg=_C_FIRM_A, halign="left", size=10)
        else:
            _cell(ws, r, OWN_START + 2, firm_label, bold=True, halign="left", size=10)
        ws.row_dimensions[r].height = 16
        r += 1
        firm_block_idx += 1

        # ── Строка «Количество календарных дней с начала уборки» ─────────────
        _cell(ws, r, 1, "Количество календарных дней с начала уборки:",
              halign="left", size=9, italic=True)
        _cell(ws, r, HRD_START, cal_days, bold=True, halign="center", fmt="0")
        ws.row_dimensions[r].height = 14
        r += 1

        # ── Подзаголовки «Собственные» / «Наёмные» ───────────────────────────
        _cell(ws, r, OWN_START + 2, "Собственные", bold=True, halign="left", size=10)
        _cell(ws, r, HRD_START + 2, "Наёмные", bold=True, halign="left", size=10)
        ws.row_dimensions[r].height = 16
        r += 1

        # ── Шапка колонок ─────────────────────────────────────────────────────
        for i, h in enumerate(HEADERS_BLOCK):
            _cell(ws, r, OWN_START + i, h, bold=True, bg=_C_HDR,
                  color="FFFFFF", halign="center", wrap=True, size=8)
            _cell(ws, r, HRD_START + i, h, bold=True, bg=_C_HDR,
                  color="FFFFFF", halign="center", wrap=True, size=8)
        ws.row_dimensions[r].height = 46
        r += 1

        # ── Строки данных (собственные и наёмные параллельно) ─────────────────
        max_len = max(len(own_rows), len(hrd_rows))

        def row_vals(mrow, num):
            return [
                (num,                                     "center", "0"),
                (mrow["machine"],                         "left",   None),
                (mrow["n_days"],                          "center", "0"),
                (round(mrow["total_h"], 2),               "right",  "# ##0.00"),
                (fmt_hhmm(mrow["max_sec"]),               "center", None),
                (round(mrow["avg_h"], 2),                 "right",  "# ##0.00"),
                (round(mrow["area"], 2),                  "right",  "# ##0.00"),
                (round(mrow["avg_day_weather"], 2),       "right",  "# ##0.00"),
                (round(mrow["avg_hour_weather"], 2),      "right",  "# ##0.00"),
                (round(mrow["avg_no_weather"], 2),        "right",  "# ##0.00"),
            ]

        own_mean = (
            sum(x["avg_day_weather"] for x in own_rows) / len(own_rows) if own_rows else 0.0
        )
        hrd_mean = (
            sum(x["avg_day_weather"] for x in hrd_rows) / len(hrd_rows) if hrd_rows else 0.0
        )

        own_num = 0
        hrd_num = 0
        for i in range(max_len):
            # Собственные
            if i < len(own_rows):
                own_num += 1
                mrow = own_rows[i]
                bg_own = _C_GREEN if mrow["avg_day_weather"] > own_mean else _C_ROW_O
                for ci, (val, align, fmt) in enumerate(row_vals(mrow, own_num)):
                    _cell(ws, r, OWN_START + ci, val, bg=bg_own, halign=align, fmt=fmt)
            else:
                for ci in range(N_BLOCK):
                    _cell(ws, r, OWN_START + ci, None, bg=_C_ROW_O)

            # Наёмные
            if i < len(hrd_rows):
                hrd_num += 1
                mrow = hrd_rows[i]
                bg_hrd = _C_GREEN if mrow["avg_day_weather"] > hrd_mean else _C_ROW_O
                for ci, (val, align, fmt) in enumerate(row_vals(mrow, hrd_num)):
                    _cell(ws, r, HRD_START + ci, val, bg=bg_hrd, halign=align, fmt=fmt)
            else:
                for ci in range(N_BLOCK):
                    _cell(ws, r, HRD_START + ci, None, bg=_C_ROW_O)

            ws.row_dimensions[r].height = 15
            r += 1

        # ── Итоговые строки ───────────────────────────────────────────────────
        def write_total(group_rows, start_col, bg_total, label):
            if not group_rows:
                for ci in range(N_BLOCK):
                    _cell(ws, r, start_col + ci, None, bg=bg_total)
                return
            tot_area  = sum(x["area"]    for x in group_rows)
            tot_h     = sum(x["total_h"] for x in group_rows)
            tot_days  = sum(x["n_days"]  for x in group_rows)
            cal_days  = group_rows[0]["cal_days"]
            avg_d     = tot_area / tot_days if tot_days else 0.0
            avg_hh    = tot_area / tot_h    if tot_h    else 0.0
            avg_no_w  = tot_area / cal_days if cal_days else 0.0
            avg_h_val = tot_h / tot_days if tot_days else 0.0  # средняя продолжительность за смену (ч)
            avg_max   = sum(x["max_sec"] for x in group_rows) / len(group_rows) / 3600  # среднее макс. время, ч
            summary = [
                (label,                    "left",   None),
                ("",                       "left",   None),
                (tot_days,                 "center", "0"),
                (round(tot_h, 2),          "right",  "# ##0.00"),
                (round(avg_max, 2),        "right",  "# ##0.00"),
                (round(avg_h_val, 2),      "right",  "# ##0.00"),
                (round(tot_area, 2),       "right",  "# ##0.00"),
                (round(avg_d, 2),          "right",  "# ##0.00"),
                (round(avg_hh, 2),         "right",  "# ##0.00"),
                (round(avg_no_w, 2),       "right",  "# ##0.00"),
            ]
            for ci, (val, align, fmt) in enumerate(summary):
                _cell(ws, r, start_col + ci, val, bold=True,
                      bg=bg_total, halign=align, fmt=fmt)

        firm_total_rows[firm] = r
        write_total(own_rows, OWN_START, _C_TOTAL, f"ВСЕГО ({firm})")
        write_total(hrd_rows, HRD_START, _C_TOTAL, f"ВСЕГО ({firm})")
        ws.row_dimensions[r].height = 15
        r += 1

        # пустая строка-разделитель
        ws.row_dimensions[r].height = 6
        r += 1

    # ── Агрофирмы из FIRM_ORDER ───────────────────────────────────────────────
    for firm in FIRM_ORDER:
        rows = result.get(firm, [])
        own_rows = [x for x in rows if not x["is_hired"]]
        hrd_rows = [x for x in rows if     x["is_hired"]]
        if not own_rows and not hrd_rows:
            continue
        write_firm_block(firm, own_rows, hrd_rows)

    # ── Агрофирмы не из FIRM_ORDER ────────────────────────────────────────────
    for firm, rows in result.items():
        if firm in FIRM_ORDER:
            continue
        own_rows = [x for x in rows if not x["is_hired"]]
        hrd_rows = [x for x in rows if     x["is_hired"]]
        write_firm_block(firm, own_rows, hrd_rows)

    # ── Лист «Свод» ──────────────────────────────────────────────────────────
    write_svod(wb, result, firm_total_rows)

    wb.save(OUTPUT_FILE)
    print(f"\n✓ Сохранено: {OUTPUT_FILE}")


# =============================================================================
# MAIN
# =============================================================================

async def build_report_async() -> None:
    print(
        f"=== Отчёт по комбайнам | "
        f"{DATE_FROM.strftime('%d.%m.%Y')} — {DATE_TO.strftime('%d.%m.%Y')} ===\n"
    )

    timing_start()

    async def _tasks():
        return await asyncio.to_thread(_fetch_period_machine_tasks)

    with stage("справочники ∥ machine_tasks (wall)"):
        refs, all_tasks = await run_parallel_refs_and_fetch(
            load_references, _tasks, label="r26-tasks",
        )
    field_to_firm, machine_name, machine_group, machine_reg, machine_vin = refs
    with stage("collect_data (mtfmi + расчёт)"):
        result = collect_data(
            field_to_firm, machine_name, machine_group, machine_reg, machine_vin,
            all_tasks=all_tasks,
        )
    print(f"\n  Агрофирм с данными: {len(result)}")
    for firm, rows in result.items():
        print(f"  → {firm}: {len(rows)} комбайнов")
    with stage("Excel + сохранение"):
        write_report(result)
    timing_finish()


def build_report() -> None:
    run_report_async(build_report_async())


if __name__ == "__main__":
    build_report()