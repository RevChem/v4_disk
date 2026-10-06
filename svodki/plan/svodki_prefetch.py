from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

try:
    from plans import ALL_WT_IDS as PLAN_ALL_WT_IDS
except Exception:
    PLAN_ALL_WT_IDS = set()

try:
    from plans import API_TOKEN as PLANS_API_TOKEN
    from plans import BASE_URL, WORK_GROUPS
except Exception:
    PLANS_API_TOKEN = ""
    BASE_URL = os.environ.get("CROPWISE_BASE_URL", "https://operations.cropwise.com/api/v3")
    WORK_GROUPS = []

from cropwise_paging import from_id_should_continue

try:
    import plans  # noqa: F401
except Exception:
    from plan_paths import ensure_plan_sys_path

    ensure_plan_sys_path()

from report_fetch_orchestration import run_report_async

TZ_MSK = timezone(timedelta(hours=3))
PAGE_LIMIT = int(os.environ.get("SVODKI_PREFETCH_PAGE_LIMIT", "1000"))
REQUEST_CONCURRENCY = int(os.environ.get("SVODKI_PREFETCH_CONCURRENCY", "20"))
MTFMI_CONCURRENCY = int(os.environ.get("SVODKI_PREFETCH_MTFMI_CONCURRENCY", "30"))

_REPORT1_WORK_GROUP_NAMES: tuple[str, ...] = tuple(
    name for name, _ in (WORK_GROUPS or [])
)

_REPORT_WORK_GROUPS: dict[str, tuple[str, ...]] = {
    "1": _REPORT1_WORK_GROUP_NAMES,
    "3": ("1.1 Внесение минеральных удобрений",),
    "4": ("2.1 Боронование",),
    "5": ("4.0 Посев", "4.1 Посев (овощи)"),
    "6": ("3.1 Опрыскивание", "3.2 Опрыскивание (овощи)"),
    "7": ("2.9 Дискование",),
    "8": ("2.11 Культивация",),
    "9": ("2.4 Вспашка", "2.8 Глубокорыхление"),
    "11": ("5.0 Уборка",),
    "12": ("5.0 Уборка",),
    "10": ("5.0 Уборка",),
    "13": ("5.0 Уборка",),
    # 14: зерновая уборка + овощи (иначе «только 14» → полный season_superset).
    "14": ("5.0 Уборка", "5.4 Уборка картофеля, лука"),
}

# Сводка 2 — все группы из WORK_GROUPS; 1 — report-aware (см. _REPORT_WORK_GROUPS).
_REPORT_ALL_WORK_GROUP_IDS: frozenset[str] = frozenset({"2"})

# Сводка 22 (text_svodka_2.py): смена 06:00–06:00, только WORK_GROUPS из plans.
REPORT_22_DAY_START_HOUR = 6
_REFERENCE_ENDPOINTS_REPORT_22: tuple[tuple[str, dict[str, Any]], ...] = (
    ("fields", {}),
    ("field_groups", {}),
    ("group_folders", {}),
    ("machines", {}),
)
_REFERENCE_ENDPOINTS_FULL: tuple[tuple[str, dict[str, Any]], ...] = (
    ("fields", {}),
    ("field_groups", {}),
    ("group_folders", {}),
    ("machines", {}),
    ("crops", {}),
    ("history_items", {}),  # year подставляется в _fetch_reference
    ("inventory_history_items", {"historyable_type": "Field"}),
    ("implements", {}),
    ("users", {}),
    ("fertilizers", {}),
    ("machine_region_mapping_items", {}),
)


_REPORT_12_DAY_START_HOUR = 6
_REPORT_12_OBJECT_INTERSECTION_LOOKBACK_HOURS = 6


async def _fetch_machine_object_intersections_report_12(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    report_date: date,
) -> list[dict[str, Any]]:
    """Окно как в 12_main_harvest_idle.fetch_object_intersections_for_day."""
    cutoff = datetime(
        report_date.year,
        report_date.month,
        report_date.day,
        _REPORT_12_DAY_START_HOUR,
        0,
        0,
        tzinfo=TZ_MSK,
    )
    day_start = cutoff - timedelta(days=1)
    lo = day_start - timedelta(hours=_REPORT_12_OBJECT_INTERSECTION_LOOKBACK_HOURS)
    params = {
        "object_type": "AdditionalObject",
        "hour_start_gt_eq": lo.strftime("%Y-%m-%dT%H:%M:%S"),
        "hour_start_lt": cutoff.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    rows = await _fetch_pages(
        client,
        "machine_object_intersections",
        params=params,
        sem=sem,
    )
    print(f"[prefetch] machine_object_intersections: {len(rows)}", flush=True)
    return rows


def _reference_endpoint_params(
    selected_reports: set[str] | None = None,
) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Сводка 12: AdditionalObjects для таблицы простоя на элеваторах."""
    base = list(_REFERENCE_ENDPOINTS_FULL)
    ids = selected_reports or set()
    if ids & {"12"}:
        base.append(("additional_objects", {}))
    return tuple(base)


def _elapsed(started: float) -> str:
    return f"{time.monotonic() - started:.1f} с"


def _report_date() -> date:
    raw = os.environ.get("REPORT_DATE", "").strip()
    return date.fromisoformat(raw) if raw else date.today()


def _season_start(report_date: date, selected_reports: set[str] | None = None) -> date:
    # Some report add-ons calculate season facts from Jan 1, while others use
    # SVODKI_SEASON_START_DATE/March. Prefetch must be a superset for all of them.
    selected = selected_reports or set()
    if selected & {"11", "12", "26"}:
        raw = (os.environ.get("SVODKI_COMBINE_SEASON_START") or "").strip()
        if raw:
            try:
                return date.fromisoformat(raw.split("T")[0])
            except ValueError:
                pass
        harvest = date(report_date.year, 6, 10)
        if report_date < harvest:
            return date(report_date.year - 1, 6, 10)
        return harvest
    return date(report_date.year, 1, 1)


def _token() -> str:
    return (
        os.environ.get("CROPWISE_API_TOKEN")
        or os.environ.get("API_TOKEN")
        or PLANS_API_TOKEN
        or ""
    ).strip()


def _selected_report_ids() -> set[str]:
    raw = os.environ.get("SVODKI_PREFETCH_REPORT_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _is_report_22_only(report_ids: set[str]) -> bool:
    return report_ids == {"22"}


def _work_type_ids_for_report_22() -> list[int]:
    """Те же work_type_id, что ALL_WT_IDS в text_svodka_2.py (все WORK_GROUPS)."""
    ids: set[int] = set()
    for _, wt_ids in WORK_GROUPS or []:
        for wt_id in wt_ids:
            try:
                ids.add(int(wt_id))
            except Exception:
                pass
    return sorted(ids)


def _all_work_type_ids() -> list[int]:
    ids: set[int] = set(int(x) for x in (PLAN_ALL_WT_IDS or set()))
    for _, wt_ids in WORK_GROUPS or []:
        for wt_id in wt_ids:
            try:
                ids.add(int(wt_id))
            except Exception:
                pass
    # IDs that are used directly in several report files.
    ids.update(
        {
            1, 3, 4, 6, 7, 10, 11, 12, 13, 49, 50, 51, 55, 57, 60, 62, 63,
            64, 65, 66, 68, 69, 70, 71, 72, 73, 75, 76, 77, 81, 96, 97,
            103, 106, 111, 118, 119, 120, 121, 125, 126, 142, 145, 148,
            150, 151, 153, 171, 172,
        }
    )
    return sorted(ids)


def _all_work_group_type_ids() -> list[int]:
    """Все work_type_id из WORK_GROUPS (+ ALL_WT_IDS из plans)."""
    ids: set[int] = set(int(x) for x in (PLAN_ALL_WT_IDS or set()))
    for _, wt_ids in WORK_GROUPS or []:
        for wt_id in wt_ids:
            try:
                ids.add(int(wt_id))
            except Exception:
                pass
    return sorted(ids)


def _work_type_ids_from_group_names(group_names: tuple[str, ...] | list[str]) -> tuple[set[int], list[str]]:
    groups = {name: ids for name, ids in (WORK_GROUPS or [])}
    out: set[int] = set()
    missing: list[str] = []
    for group_name in group_names:
        values = groups.get(group_name)
        if not values:
            missing.append(group_name)
            continue
        for wt_id in values:
            try:
                out.add(int(wt_id))
            except Exception:
                pass
    return out, missing


def _work_type_ids_for_reports(report_ids: set[str]) -> list[int]:
    """Safely narrow work types by selected reports; union known IDs instead of blind superset."""
    if not report_ids:
        return _all_work_type_ids()
    if _is_report_22_only(report_ids):
        wt_ids = _work_type_ids_for_report_22()
        print(
            f"[prefetch] report 22 only: work types from WORK_GROUPS ({len(wt_ids)})",
            flush=True,
        )
        return wt_ids

    if report_ids & _REPORT_ALL_WORK_GROUP_IDS:
        wt_ids = _all_work_group_type_ids()
        print(
            "[prefetch] report-aware mode: WORK_GROUPS superset for reports "
            f"{', '.join(sorted(report_ids))}; work types: {len(wt_ids)}",
            flush=True,
        )
        return wt_ids

    out: set[int] = set()
    missing: list[str] = []
    unknown: list[str] = []
    for rid in sorted(report_ids):
        group_names = _REPORT_WORK_GROUPS.get(rid)
        if group_names is None:
            unknown.append(rid)
            continue
        ids, miss = _work_type_ids_from_group_names(group_names)
        out.update(ids)
        missing.extend(miss)

    if unknown:
        print(
            "[prefetch] report-aware mode: fallback to full work_type superset; "
            f"unknown reports: {', '.join(unknown)}",
            flush=True,
        )
        return _all_work_type_ids()
    if missing or not out:
        print(
            "[prefetch] report-aware mode: fallback to full work_type superset; "
            f"missing groups: {', '.join(missing) or 'none'}",
            flush=True,
        )
        return _all_work_type_ids()

    print(
        "[prefetch] report-aware mode: selected reports "
        f"{', '.join(sorted(report_ids))}; work types: {len(out)}",
        flush=True,
    )
    return sorted(out)


async def _fetch_pages(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict[str, Any] | None = None,
    *,
    sem: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    params = dict(params or {})
    out: list[dict[str, Any]] = []
    from_id = 0
    url = f"{BASE_URL.rstrip('/')}/{endpoint}"
    max_attempts = int(os.environ.get("SVODKI_PREFETCH_HTTP_RETRIES", "5"))
    while True:
        for attempt in range(max_attempts):
            try:
                async with sem:
                    r = await client.get(
                        url,
                        params={**params, "from_id": from_id, "limit": PAGE_LIMIT},
                    )
                r.raise_for_status()
                break
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError) as exc:
                retryable = True
                if isinstance(exc, httpx.HTTPStatusError):
                    retryable = exc.response.status_code in {429, 502, 503, 504}
                if not retryable or attempt >= max_attempts - 1:
                    raise
                delay = min(30.0, 1.5 * (2 ** attempt))
                print(
                    f"[prefetch] {endpoint} retry {attempt + 1}/{max_attempts - 1} "
                    f"from_id={from_id}: {exc}",
                    flush=True,
                )
                await asyncio.sleep(delay)
        body = r.json()
        chunk = body.get("data", []) if isinstance(body, dict) else body
        if not chunk:
            break
        out.extend(chunk)
        meta = body.get("meta", {}).get("response", {}) if isinstance(body, dict) else {}
        last_id = meta.get("last_record_id")
        # Совпадает с поведением fetch_pages во всех plan-скриптах: остановка
        # происходит, когда API вернул меньше 100 записей или не отдал last_record_id.
        if not from_id_should_continue(meta, chunk):
            break
        from_id = int(last_id) + 1
    return out


async def _fetch_reference(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    report_date: date,
    *,
    endpoint_params: tuple[tuple[str, dict[str, Any]], ...] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    # Каждая запись = endpoint + параметры, которыми его дёргает оригинальный скрипт.
    raw_params = endpoint_params or _REFERENCE_ENDPOINTS_FULL
    endpoint_params_list: list[tuple[str, dict[str, Any]]] = []
    for endpoint, params in raw_params:
        p = dict(params)
        if endpoint == "history_items" and "year" not in p:
            # Всегда оба сезона: год отчёта и следующий (посев 2027 при дате 2026).
            years = {str(report_date.year), str(report_date.year + 1)}
            for key in ("SEASON_YEAR", "CROPWISE_SEASON_YEAR"):
                season = os.environ.get(key, "").strip()
                if season.isdigit():
                    years.add(season)
                    years.add(str(int(season) + 1))
            for year in sorted(years):
                endpoint_params_list.append((endpoint, {**p, "year": year}))
            continue
        endpoint_params_list.append((endpoint, p))
    results = await asyncio.gather(
        *[
            _fetch_pages(client, endpoint, params=params, sem=sem)
            for endpoint, params in endpoint_params_list
        ],
        return_exceptions=True,
    )
    data: dict[str, list[dict[str, Any]]] = {}
    failures: list[str] = []
    for (endpoint, params), result in zip(endpoint_params_list, results):
        if isinstance(result, Exception):
            label = endpoint
            if endpoint == "history_items" and params.get("year"):
                label = f"{endpoint}?year={params['year']}"
            print(f"[prefetch] {label}: FAILED ({result})", flush=True)
            failures.append(f"{label}: {result}")
            if endpoint != "history_items":
                data[endpoint] = []
            continue
        if endpoint == "history_items":
            data.setdefault("history_items", []).extend(result)
            label = f"{endpoint}?year={params.get('year', '?')}"
            print(f"[prefetch] {label}: {len(result)} (merged total {len(data['history_items'])})", flush=True)
        else:
            data[endpoint] = result
            print(f"[prefetch] {endpoint}: {len(result)}", flush=True)
    if failures:
        raise RuntimeError("Prefetch reference endpoints failed: " + "; ".join(failures))
    return data


async def _fetch_agro_operations(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    report_date: date,
    wt_ids: list[int],
) -> list[dict[str, Any]]:
    """Параллельно тянем agro_operations по каждому work_type_id, как делают plan-скрипты."""

    season = str(report_date.year)
    results = await asyncio.gather(
        *[
            _fetch_pages(client, "agro_operations", {"season": season, "work_type_id": wt_id}, sem=sem)
            for wt_id in wt_ids
        ],
        return_exceptions=True,
    )
    by_id: dict[int, dict[str, Any]] = {}
    failures: list[str] = []
    for wt_id, result in zip(wt_ids, results):
        if isinstance(result, Exception):
            print(f"[prefetch] agro_operations wt={wt_id}: FAILED ({result})", flush=True)
            failures.append(f"wt={wt_id}: {result}")
            continue
        for row in result:
            row_id = row.get("id")
            try:
                key = int(row_id) if row_id is not None else None
            except Exception:
                key = None
            if key is None:
                continue
            by_id[key] = row
    if failures:
        raise RuntimeError("Prefetch agro_operations failed: " + "; ".join(failures))
    print(f"[prefetch] agro_operations total: {len(by_id)}", flush=True)
    return list(by_id.values())


async def _fetch_mt_pages(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
    sem: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    del url
    return await _fetch_pages(client, "machine_tasks", params, sem=sem)


async def _fetch_machine_tasks(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    report_date: date,
    season_start: date,
    wt_ids: list[int],
    *,
    include_open_tasks: bool,
) -> list[dict[str, Any]]:
    from machine_tasks_batch_fetch import fetch_machine_tasks_by_wt_async

    start = datetime(season_start.year, season_start.month, season_start.day, 0, 0, tzinfo=TZ_MSK)
    end = datetime(report_date.year, report_date.month, report_date.day, 23, 59, 59, tzinfo=TZ_MSK)
    base_params = {
        "end_time_gt_eq": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end_time_lt": end.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    url = f"{BASE_URL.rstrip('/')}/machine_tasks"
    print(f"[prefetch] machine_tasks work types: {len(wt_ids)}", flush=True)
    rows = await fetch_machine_tasks_by_wt_async(
        client, sem, wt_ids, base_params, _fetch_mt_pages,
        url=url, log_prefix="[prefetch] ",
    )
    by_id: dict[int, dict[str, Any]] = {int(r["id"]): r for r in rows if r.get("id") is not None}

    if include_open_tasks:
        open_params = {
            "start_time_gt_eq": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "status": "in_progress",
        }
        print("[prefetch] machine_tasks open/in_progress: enabled", flush=True)
        open_rows = await fetch_machine_tasks_by_wt_async(
            client, sem, wt_ids, open_params, _fetch_mt_pages,
            url=url, log_prefix="[prefetch] open ",
        )
        for row in open_rows:
            try:
                by_id[int(row["id"])] = row
            except Exception:
                pass
        print(f"[prefetch] open machine_tasks merged: {len(open_rows)}", flush=True)

    return list(by_id.values())


async def _fetch_machine_tasks_report_22_shift(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    report_date: date,
    wt_ids: list[int],
) -> list[dict[str, Any]]:
    """Как text_svodka_2.fetch_all_data: одна смена 06:00–06:00, без сезонного хвоста."""
    day_start = datetime(
        report_date.year,
        report_date.month,
        report_date.day,
        REPORT_22_DAY_START_HOUR,
        0,
        0,
        tzinfo=TZ_MSK,
    )
    shift_end = day_start + timedelta(days=1)

    win_start_raw = os.environ.get("REPORT_WINDOW_START", "").strip()
    if win_start_raw:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                day_start = datetime.strptime(win_start_raw, fmt).replace(tzinfo=TZ_MSK)
                break
            except ValueError:
                continue
    shift_end = day_start + timedelta(days=1)
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    shift_end_str = shift_end.strftime("%Y-%m-%dT%H:%M:%S")
    print(
        f"[prefetch] report 22 shift window: {day_start_str} .. {shift_end_str}, "
        f"work types: {len(wt_ids)}",
        flush=True,
    )

    from machine_tasks_batch_fetch import fetch_machine_tasks_by_wt_async

    url = f"{BASE_URL.rstrip('/')}/machine_tasks"
    done_rows = await fetch_machine_tasks_by_wt_async(
        client, sem, wt_ids,
        {"end_time_gt_eq": day_start_str, "end_time_lt": shift_end_str},
        _fetch_mt_pages, url=url, log_prefix="[prefetch] report22 done ",
    )
    open_rows = await fetch_machine_tasks_by_wt_async(
        client, sem, wt_ids,
        {"start_time_gt_eq": day_start_str, "status": "in_progress"},
        _fetch_mt_pages, url=url, log_prefix="[prefetch] report22 open ",
    )
    by_id: dict[int, dict[str, Any]] = {}
    for row in done_rows + open_rows:
        try:
            by_id[int(row["id"])] = row
        except Exception:
            pass
    print(f"[prefetch] report 22 machine_tasks total: {len(by_id)}", flush=True)
    return list(by_id.values())


async def _fetch_mtfmi(
    client: httpx.AsyncClient,
    task_ids: list[int],
) -> list[dict[str, Any]]:
    """Пакетная загрузка mtfmi (до N task_id за HTTP-запрос)."""
    if not task_ids:
        return []
    try:
        from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks
    except ImportError:
        fetch_mtfmi_map_for_tasks = None  # type: ignore[assignment,misc]

    if fetch_mtfmi_map_for_tasks is None:
        sem = asyncio.Semaphore(MTFMI_CONCURRENCY)

        async def one(task_id: int) -> list[dict[str, Any]]:
            return await _fetch_pages(
                client,
                "machine_task_field_mapping_items",
                {"machine_task_id": task_id},
                sem=sem,
            )

        out: list[dict[str, Any]] = []
        done = 0
        wave = max(MTFMI_CONCURRENCY * 4, 100)
        for i in range(0, len(task_ids), wave):
            chunk_ids = task_ids[i : i + wave]
            results = await asyncio.gather(
                *[one(tid) for tid in chunk_ids],
                return_exceptions=True,
            )
            for task_id, result in zip(chunk_ids, results):
                done += 1
                if isinstance(result, Exception):
                    print(f"[prefetch] mtfmi task={task_id}: skipped ({result})", flush=True)
                    continue
                out.extend(result)
            print(f"[prefetch] mtfmi progress: {done}/{len(task_ids)} rows={len(out)}", flush=True)
        return out

    sem = asyncio.Semaphore(min(MTFMI_CONCURRENCY, 20))
    base_url = BASE_URL.rstrip("/")
    mtfmi_map = await fetch_mtfmi_map_for_tasks(
        client,
        sem,
        base_url,
        task_ids,
        log_prefix="[prefetch] ",
    )
    out = [row for tid in sorted(mtfmi_map) for row in mtfmi_map[tid]]
    print(
        f"[prefetch] mtfmi batch done: {len(mtfmi_map)}/{len(task_ids)} tasks, "
        f"{len(out)} rows",
        flush=True,
    )
    return out


async def build_prefetch(output_path: Path) -> None:
    total_started = time.monotonic()
    report_date = _report_date()
    selected_reports = _selected_report_ids()
    report_22_only = _is_report_22_only(selected_reports)
    season_start = (
        date(report_date.year, report_date.month, report_date.day)
        if report_22_only
        else _season_start(report_date, selected_reports)
    )
    wt_ids = _work_type_ids_for_reports(selected_reports)
    include_open_tasks = not selected_reports or "22" in selected_reports
    prefetch_profile = "report_22_shift" if report_22_only else "season_superset"
    token = _token()
    headers = {"X-User-Api-Token": token} if token else {}
    sem = asyncio.Semaphore(REQUEST_CONCURRENCY)

    limits = httpx.Limits(max_connections=max(MTFMI_CONCURRENCY, REQUEST_CONCURRENCY), max_keepalive_connections=40)
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=limits) as client:
        reference_started = time.monotonic()
        if report_22_only:
            print("[prefetch] profile: report 22 only (shift window, no agro_operations)", flush=True)
            reference_task = asyncio.create_task(
                _fetch_reference(
                    client,
                    sem,
                    report_date,
                    endpoint_params=_REFERENCE_ENDPOINTS_REPORT_22,
                )
            )
            agro_task = None
            machine_started = time.monotonic()
            machine_tasks = await _fetch_machine_tasks_report_22_shift(
                client, sem, report_date, wt_ids
            )
        else:
            reference_task = asyncio.create_task(
                _fetch_reference(
                    client,
                    sem,
                    report_date,
                    endpoint_params=_reference_endpoint_params(selected_reports),
                )
            )
            agro_task = asyncio.create_task(_fetch_agro_operations(client, sem, report_date, wt_ids))
            machine_started = time.monotonic()
            machine_tasks = await _fetch_machine_tasks(
                client,
                sem,
                report_date,
                season_start,
                wt_ids,
                include_open_tasks=include_open_tasks,
            )
        print(f"[prefetch] machine_tasks elapsed: {_elapsed(machine_started)}", flush=True)
        print(f"[prefetch] machine_tasks unique total: {len(machine_tasks)}", flush=True)
        reference = await reference_task
        print(f"[prefetch] reference elapsed: {_elapsed(reference_started)}", flush=True)
        if agro_task is not None:
            agro_operations = await agro_task
        else:
            agro_operations = []

        task_ids = sorted({int(t["id"]) for t in machine_tasks if t.get("id") is not None})
        mtfmi_started = time.monotonic()
        mtfmi = await _fetch_mtfmi(client, task_ids)
        print(f"[prefetch] mtfmi elapsed: {_elapsed(mtfmi_started)}", flush=True)

        machine_object_intersections: list[dict[str, Any]] = []
        if selected_reports & {"12"}:
            moi_started = time.monotonic()
            machine_object_intersections = await _fetch_machine_object_intersections_report_12(
                client, sem, report_date
            )
            print(
                f"[prefetch] machine_object_intersections elapsed: {_elapsed(moi_started)}",
                flush=True,
            )

    endpoints = {
        **reference,
        "machine_tasks": machine_tasks,
        "machine_task_field_mapping_items": mtfmi,
        "agro_operations": agro_operations,
    }
    if selected_reports & {"12"}:
        endpoints["machine_object_intersections"] = machine_object_intersections
    payload = {
        "version": 1,
        "kind": "svodki-prefetch",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report_date": report_date.isoformat(),
        "season_start": season_start.isoformat(),
        "selected_report_ids": sorted(selected_reports),
        "prefetch_profile": prefetch_profile,
        "work_type_ids": wt_ids,
        "include_open_tasks": include_open_tasks,
        "base_url": BASE_URL,
        "endpoints": endpoints,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_name(f"{output_path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, output_path)
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError(f"Failed to write prefetch JSON ({output_path}): {exc}") from exc
    print(f"[prefetch] saved: {output_path}", flush=True)
    print(f"[prefetch] total elapsed: {_elapsed(total_started)}", flush=True)


def main() -> None:
    # argv в приоритете, чтобы сборщик не путал свой output с тем prefetch JSON,
    # который sitecustomize мог бы попытаться прочитать через ту же ENV-переменную.
    raw = (sys.argv[1] if len(sys.argv) > 1 else "") or os.environ.get("SVODKI_PREFETCH_DATA_PATH", "")
    if not raw:
        raise SystemExit("Output path argument is required (argv[1] or SVODKI_PREFETCH_DATA_PATH)")
    try:
        run_report_async(build_prefetch(Path(raw)))
    except Exception as exc:
        print(f"[prefetch] FATAL: {exc}", flush=True)
        traceback.print_exc()
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
