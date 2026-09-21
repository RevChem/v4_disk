"""Единовременная загрузка machine_tasks и mtfmi для всех групп сводки 1."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any

import httpx

from report1_prefetch_io import fetch_mtfmi_for_tasks, load_prefetch_endpoints

from .cache import CACHE, Report1DataCache


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(1, int(str(raw).strip()))
    except ValueError:
        return default


async def preload_all(
    legacy: Any,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    day_start: datetime,
    cutoff: datetime,
    skipped_groups: set[str],
) -> Report1DataCache:
    """
    Один проход по machine_tasks (prefetch или API) + один batched mtfmi.
    Результат кладётся в CACHE для патченных fetch_pages/fetch_mtfmi.
    """
    CACHE.tasks_by_wt.clear()
    CACHE.mtfmi_by_task.clear()
    CACHE.agro_ops_wt55 = None
    CACHE.set_parse_dt(legacy.parse_dt)

    wt_needed: set[int] = set()
    for group_name, wt_ids in legacy.WORK_GROUPS:
        if group_name in skipped_groups:
            continue
        for wt in wt_ids:
            wt_needed.add(int(wt))

    if not wt_needed:
        print("  [v2] preload: нет групп для API (все из доноров)")
        return CACHE

    endpoints = load_prefetch_endpoints()
    season_lo = datetime(legacy.REPORT_DATE.year, 1, 1, tzinfo=legacy.TZ_MSK)
    mu_lo = datetime(
        legacy.PERIOD_START_DATE.year,
        legacy.PERIOD_START_DATE.month,
        legacy.PERIOD_START_DATE.day,
        tzinfo=legacy.TZ_MSK,
    )
    season_start = min(season_lo, mu_lo)

    if endpoints:
        print(f"  [v2] preload: machine_tasks из prefetch ({len(wt_needed)} work types)...")
        all_rows = endpoints.get("machine_tasks") or []
        by_wt: dict[int, list[dict]] = {wt: [] for wt in wt_needed}
        seen: dict[int, set[int]] = {wt: set() for wt in wt_needed}
        for t in all_rows:
            try:
                wt = int(t.get("work_type_id"))
            except (TypeError, ValueError):
                continue
            if wt not in wt_needed:
                continue
            tid = t.get("id")
            if tid is None:
                continue
            tid_i = int(tid)
            if tid_i in seen[wt]:
                continue
            end_dt = legacy.parse_dt(t.get("end_time"))
            if end_dt is None or end_dt < season_start:
                continue
            seen[wt].add(tid_i)
            by_wt[wt].append(t)
        for wt, rows in by_wt.items():
            if rows:
                CACHE.tasks_by_wt[wt] = rows
        prefetched = sum(len(v) for v in CACHE.tasks_by_wt.values())
        print(
            f"  [v2] prefetch machine_tasks: {prefetched} заданий "
            f"по {len(CACHE.tasks_by_wt)} wt (один проход JSON)"
        )
        missing_wt = [wt for wt in wt_needed if wt not in CACHE.tasks_by_wt]
        if missing_wt:
            await _fetch_tasks_http(legacy, client, sem, missing_wt, season_start, cutoff)
    else:
        print(f"  [v2] preload: machine_tasks HTTP ({len(wt_needed)} work types)...")
        await _fetch_tasks_http(legacy, client, sem, sorted(wt_needed), season_start, cutoff)

    task_ids: set[int] = set()
    for rows in CACHE.tasks_by_wt.values():
        for t in rows:
            tid = t.get("id")
            if tid is not None:
                task_ids.add(int(tid))
    print(f"  [v2] preload: уникальных machine_tasks: {len(task_ids)}")

    if task_ids:
        mtfmi_conc = _env_int("REPORT1_V2_MTFMI_CONCURRENCY", 40)
        mtfmi_sem = asyncio.Semaphore(mtfmi_conc)
        CACHE.mtfmi_by_task = await fetch_mtfmi_for_tasks(
            sorted(task_ids),
            mtfmi_sem,
            client,
            base_url=legacy.BASE_URL,
            log_prefix="[v2] ",
        )

    agro = (endpoints or {}).get("agro_operations") if endpoints else None
    if agro:
        CACHE.agro_ops_wt55 = [
            op
            for op in agro
            if int(op.get("work_type_id") or 0) == int(legacy.WORK_TYPE_ID)
        ]
        print(f"  [v2] agro_operations wt=55 из prefetch: {len(CACHE.agro_ops_wt55)}")
    else:
        try:
            ops = await legacy.fetch_pages(
                client,
                f"{legacy.BASE_URL}/agro_operations",
                {"work_type_id": legacy.WORK_TYPE_ID, "season": legacy.SEASON},
                sem,
            )
            CACHE.agro_ops_wt55 = ops
            print(f"  [v2] agro_operations wt=55 HTTP: {len(ops)}")
        except Exception as exc:
            print(f"  [v2] agro_operations: {exc}")

    return CACHE


async def _fetch_tasks_http(
    legacy: Any,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    wt_ids: list[int],
    season_start: datetime,
    cutoff: datetime,
) -> None:
    url = f"{legacy.BASE_URL}/machine_tasks"
    season_start_str = season_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    async def _one(wt_id: int) -> tuple[int, list[dict]]:
        rows = await legacy.fetch_pages(
            client,
            url,
            {
                "work_type_id": wt_id,
                "end_time_gt_eq": season_start_str,
                "end_time_lt": cutoff_str,
            },
            sem,
        )
        return wt_id, rows

    results = await asyncio.gather(*[_one(wt) for wt in wt_ids])
    for wt_id, rows in results:
        if rows:
            CACHE.tasks_by_wt.setdefault(wt_id, []).extend(rows)
            # dedupe by id
            seen: set[int] = set()
            deduped: list[dict] = []
            for t in CACHE.tasks_by_wt[wt_id]:
                tid = t.get("id")
                if tid is None or int(tid) in seen:
                    continue
                seen.add(int(tid))
                deduped.append(t)
            CACHE.tasks_by_wt[wt_id] = deduped
