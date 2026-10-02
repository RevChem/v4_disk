# -*- coding: utf-8 -*-
"""Общий prefetch JSON (svodki_prefetch.py): tasks, mtfmi, справочники без повторного HTTP."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

_LOAD_LOCK = threading.Lock()
_ENDPOINTS: dict[str, Any] | None = None
_LOAD_DONE = False
_TASKS_BY_WT: dict[int, list[dict[str, Any]]] | None = None
_MTFMI_ROWS: list[dict[str, Any]] | None = None


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "1" if default else "0").strip().lower()
    if raw in ("1", "true", "yes", "on", "да"):
        return True
    if raw in ("0", "false", "no", "off", "нет"):
        return False
    return default


def use_prefetch_tasks() -> bool:
    for key in ("SVODKI_USE_PREFETCH_TASKS", "SVODKI_REPORT2_USE_PREFETCH_TASKS"):
        explicit = os.environ.get(key)
        if explicit is not None and str(explicit).strip():
            return str(explicit).strip().lower() not in {"0", "false", "no"}
    return bool((os.environ.get("SVODKI_PREFETCH_DATA_PATH") or "").strip())


def prefetch_data_path() -> str:
    return (os.environ.get("SVODKI_PREFETCH_DATA_PATH") or "").strip()


def parse_end_time(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_window_bound(s: str) -> datetime | None:
    s = str(s).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def load_prefetch_endpoints() -> dict[str, Any] | None:
    """Справочники и сырые endpoints из JSON (кэш процесса)."""
    global _ENDPOINTS, _LOAD_DONE, _TASKS_BY_WT, _MTFMI_ROWS
    with _LOAD_LOCK:
        if _LOAD_DONE:
            return _ENDPOINTS
        _LOAD_DONE = True
        _ENDPOINTS = None
        _TASKS_BY_WT = None
        _MTFMI_ROWS = None
        path = prefetch_data_path()
        if not path or not os.path.isfile(path):
            return None
        started = time.monotonic()
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            print(f"  [prefetch] не удалось прочитать {path}: {exc}", flush=True)
            return None
        endpoints = data.get("endpoints")
        if not isinstance(endpoints, dict):
            return None
        _ENDPOINTS = endpoints
        if use_prefetch_tasks():
            tasks = endpoints.get("machine_tasks") or []
            mtfmi = endpoints.get("machine_task_field_mapping_items") or []
            by_wt: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for row in tasks:
                wt = row.get("work_type_id")
                if wt is None:
                    continue
                by_wt[int(wt)].append(row)
            _TASKS_BY_WT = dict(by_wt)
            _MTFMI_ROWS = list(mtfmi)
            profile = str(data.get("prefetch_profile") or "").strip()
            print(
                f"  [prefetch] JSON за {time.monotonic() - started:.1f} с"
                + (f" ({profile})" if profile else "")
                + f": tasks={len(tasks)} mtfmi={len(mtfmi)} wt={len(_TASKS_BY_WT)}",
                flush=True,
            )
        else:
            profile = str(data.get("prefetch_profile") or "").strip()
            print(
                f"  [prefetch] JSON (только справочники)"
                + (f" {profile}" if profile else "")
                + f" за {time.monotonic() - started:.1f} с",
                flush=True,
            )
        return _ENDPOINTS


def endpoint_rows(name: str) -> list[dict] | None:
    ep = load_prefetch_endpoints()
    if not ep:
        return None
    rows = ep.get(name)
    return list(rows) if isinstance(rows, list) else None


def prefetch_tasks_by_wt() -> dict[int, list[dict[str, Any]]] | None:
    load_prefetch_endpoints()
    if not use_prefetch_tasks() or not _TASKS_BY_WT:
        return None
    return _TASKS_BY_WT


def tasks_from_index(
    source: dict[int, list[dict]],
    work_type_ids: set[int] | list[int],
    *,
    window_gt_eq: str,
    window_lt: str,
    time_field: str = "end_time",
    season_years: list[str] | None = None,
) -> list[dict]:
    """Фильтр machine_tasks из индекса по wt и полуинтервалу [start, end) по time_field."""
    season_start = _parse_window_bound(window_gt_eq)
    cutoff = _parse_window_bound(window_lt)
    if season_start is None or cutoff is None:
        return []
    wt_set = {int(w) for w in work_type_ids}
    years = {str(y) for y in season_years} if season_years else None
    out: list[dict] = []
    seen: set[int] = set()
    for wt_id in wt_set:
        for row in source.get(wt_id, []):
            tid = row.get("id")
            if tid is None:
                continue
            tid_i = int(tid)
            if tid_i in seen:
                continue
            if years is not None:
                sy = row.get("season")
                if sy is not None and str(sy) not in years:
                    continue
            if time_field == "start_time":
                raw_t = row.get("start_time") or row.get("started_at")
            else:
                raw_t = row.get("end_time") or row.get("finished_at") or row.get("updated_at")
            end_dt = parse_end_time(raw_t)
            if end_dt is None:
                continue
            end_u = end_dt.astimezone(timezone.utc) if end_dt.tzinfo else end_dt.replace(
                tzinfo=timezone.utc,
            )
            s_u = season_start.astimezone(timezone.utc) if season_start.tzinfo else season_start.replace(
                tzinfo=timezone.utc,
            )
            c_u = cutoff.astimezone(timezone.utc) if cutoff.tzinfo else cutoff.replace(
                tzinfo=timezone.utc,
            )
            if s_u <= end_u < c_u:
                seen.add(tid_i)
                out.append(row)
    return out


def filter_tasks_end_time_window(
    tasks: list[dict],
    window_gt_eq: str,
    window_lt: str,
    *,
    time_field: str = "end_time",
) -> list[dict]:
    """Полуинтервал [gt_eq, lt) по end_time — как API machine_tasks (как в сводке 2)."""
    season_start = _parse_window_bound(window_gt_eq)
    cutoff = _parse_window_bound(window_lt)
    if season_start is None or cutoff is None:
        return []
    out: list[dict] = []
    seen: set[int] = set()
    for row in tasks:
        tid = row.get("id")
        if tid is None:
            continue
        tid_i = int(tid)
        if tid_i in seen:
            continue
        if time_field == "start_time":
            raw_t = row.get("start_time") or row.get("started_at")
        else:
            raw_t = row.get("end_time") or row.get("finished_at") or row.get("updated_at")
        end_dt = parse_end_time(raw_t)
        if end_dt is None:
            continue
        end_u = end_dt.astimezone(timezone.utc) if end_dt.tzinfo else end_dt.replace(
            tzinfo=timezone.utc,
        )
        s_u = season_start.astimezone(timezone.utc) if season_start.tzinfo else season_start.replace(
            tzinfo=timezone.utc,
        )
        c_u = cutoff.astimezone(timezone.utc) if cutoff.tzinfo else cutoff.replace(
            tzinfo=timezone.utc,
        )
        if s_u <= end_u < c_u:
            seen.add(tid_i)
            out.append(row)
    return out


def machine_tasks_window_from_prefetch(
    work_type_ids: set[int] | list[int],
    time_params: dict[str, str],
    *,
    season_years: list[str] | None = None,
    time_field: str = "end_time",
    log_prefix: str = "",
) -> list[dict] | None:
    """None — prefetch недоступен, иначе список задач (может быть пустым)."""
    index = prefetch_tasks_by_wt()
    if index is None:
        return None
    if time_field == "start_time":
        gt = time_params.get("start_time_gt_eq")
        lt = time_params.get("start_time_lt") or time_params.get("start_time_lt_eq")
    else:
        gt = time_params.get("end_time_gt_eq")
        lt = time_params.get("end_time_lt")
    if not gt or not lt:
        return None
    rows = tasks_from_index(
        index,
        work_type_ids,
        window_gt_eq=str(gt),
        window_lt=str(lt),
        time_field=time_field,
        season_years=season_years,
    )
    if log_prefix:
        print(
            f"  {log_prefix}[prefetch] machine_tasks: {len(rows)} "
            f"(wt={len(set(work_type_ids))})",
            flush=True,
        )
    return rows


def _task_id_from_mtfmi_row(row: dict) -> int | None:
    for key in ("machine_task_id", "machine_taskId", "task_id"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def build_mtfmi_map(task_ids: list[int] | set[int]) -> dict[int, list[dict[str, Any]]] | None:
    """Индекс mtfmi из prefetch. None — prefetch tasks/mtfmi выключен или JSON нет."""
    load_prefetch_endpoints()
    if not use_prefetch_tasks() or _MTFMI_ROWS is None:
        return None
    need = {int(t) for t in task_ids if t is not None}
    if not need:
        return {}
    started = time.monotonic()
    out: dict[int, list[dict[str, Any]]] = {tid: [] for tid in need}
    for row in _MTFMI_ROWS:
        tid = _task_id_from_mtfmi_row(row)
        if tid is not None and tid in need:
            out[tid].append(row)
    print(
        f"  [prefetch] mtfmi index: {len(need)} задач за {time.monotonic() - started:.1f} с",
        flush=True,
    )
    return out


def mtfmi_map_hybrid(
    task_ids: list[int] | set[int],
) -> tuple[dict[int, list[dict]] | None, list[int]]:
    """
    Prefetch-часть + список id без строк в JSON (нужен live batch).
    (None, all_ids) — prefetch недоступен.
    """
    pref = build_mtfmi_map(task_ids)
    if pref is None:
        need = sorted({int(t) for t in task_ids if t is not None})
        return None, need
    missing = [tid for tid, rows in pref.items() if not rows]
    return pref, missing


def sync_machine_tasks_prefetch_or_batch(
    get_all_fn: Callable[..., list[dict]],
    wt_ids: set[int] | list[int],
    base_params: dict[str, str],
    *,
    time_field: str = "end_time",
    log_prefix: str = "",
) -> list[dict]:
    """Prefetch JSON, windowed async live или batch sync по work_type_id."""
    from machine_tasks_batch_fetch import dedupe_rows_by_id, fetch_machine_tasks_by_wt_sync

    pref = machine_tasks_window_from_prefetch(
        wt_ids, base_params, time_field=time_field, log_prefix=log_prefix,
    )
    if pref is not None:
        return dedupe_rows_by_id(pref)

    if time_field == "end_time":
        from live_machine_tasks_fetch import should_use_windowed_live_fetch

        if should_use_windowed_live_fetch(base_params):
            try:
                from plans import API_TOKEN, BASE_URL
            except Exception:
                API_TOKEN = ""
                BASE_URL = ""
            if API_TOKEN and BASE_URL:
                from live_machine_tasks_fetch import fetch_machine_tasks_windowed_async
                from report_fetch_orchestration import report_async_client, run_report_async

                async def _windowed():
                    async with report_async_client(API_TOKEN, semaphore_limit=30) as (
                        client, sem,
                    ):
                        return await fetch_machine_tasks_windowed_async(
                            client, BASE_URL, wt_ids, base_params, sem,
                            log_prefix=log_prefix,
                        )

                return run_report_async(_windowed())

    return fetch_machine_tasks_by_wt_sync(
        get_all_fn, wt_ids, base_params, log_prefix=log_prefix,
    )


def mtfmi_subset_from_prefetch(task_ids: set[int] | list[int]) -> dict[int, list[dict]]:
    m = build_mtfmi_map(task_ids)
    return m if m is not None else {}


def machine_tasks_from_prefetch_legacy(
    wt_ids: set[int],
    *,
    season_start: datetime,
    cutoff: datetime,
    parse_dt: Callable[[Any], Any] | None = None,
    loose_end_filter: bool = True,
) -> list[dict]:
    """Совместимость с report1_prefetch_io (посев / сводка 1)."""
    ep = load_prefetch_endpoints()
    if not ep:
        return []
    pdt = parse_dt or parse_end_time
    wt_set = set(wt_ids)
    seen_ids: set[int] = set()
    out: list[dict] = []
    for t in ep.get("machine_tasks") or []:
        try:
            wt = int(t.get("work_type_id"))
        except (TypeError, ValueError):
            continue
        if wt not in wt_set:
            continue
        tid = t.get("id")
        if tid is None:
            continue
        tid_i = int(tid)
        if tid_i in seen_ids:
            continue
        end_dt = pdt(t.get("end_time"))
        if end_dt is None:
            continue
        if end_dt < season_start:
            continue
        if not loose_end_filter and end_dt >= cutoff:
            continue
        seen_ids.add(tid_i)
        out.append(t)
    return out
