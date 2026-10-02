"""Единовременная загрузка machine_tasks и mtfmi для всех групп сводки 1."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from report1_prefetch_io import load_prefetch_endpoints
from report_fetch_orchestration import fetch_machine_tasks_window

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
        t_mt = time.monotonic()
        CACHE.mtfmi_by_task = await _fetch_mtfmi_batched(
            legacy, sorted(task_ids), mtfmi_sem, client,
        )
        print(
            f"  [timing] v2 mtfmi ({len(task_ids)} задач): {time.monotonic() - t_mt:.1f} с",
            flush=True,
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
    """Live-загрузка machine_tasks для wt_ids одним объединённым запросом.

    Раньше: по одному запросу на work_type за весь период — длинные последовательные
    цепочки страниц. Теперь: ``fetch_machine_tasks_window`` (как в сводке 2) —
    окна по датам × пачки work_type, все запросы параллельно; затем строки
    раскладываются по work_type_id для патченного fetch_pages.

    Верхняя граница = cutoff + буфер уборки (как fetch_day_stats для группы 5.0):
    кэш сам режет по end_time_lt конкретного запроса, так что лишнее не мешает,
    а строки уборки между cutoff и cutoff+буфер без этого терялись.
    """
    wt_set = {int(w) for w in wt_ids}
    if not wt_set:
        return

    hi = cutoff
    buf_fn = getattr(legacy, "_harvest_cutoff_buffer_hours", None)
    if callable(buf_fn):
        try:
            hi = cutoff + timedelta(hours=int(buf_fn()))
        except Exception:
            hi = cutoff

    time_params = {
        "end_time_gt_eq": season_start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end_time_lt": hi.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    t0 = time.monotonic()
    # use_prefetch=False: prefetch уже разобран в preload_all (endpoints); сюда попадают
    # только wt, которых там не нашлось, — их нужно взять именно из API.
    rows = await fetch_machine_tasks_window(
        client, sem, legacy.BASE_URL, wt_set, time_params,
        log_prefix="[v2] ", use_prefetch=False,
    )
    print(
        f"  [timing] v2 machine_tasks HTTP ({len(wt_set)} wt, окна×пачки): "
        f"{time.monotonic() - t0:.1f} с, строк {len(rows)}",
        flush=True,
    )

    by_wt: dict[int, list[dict]] = {wt: [] for wt in wt_set}
    for t in rows:
        try:
            wt = int(t.get("work_type_id"))
        except (TypeError, ValueError):
            continue
        if wt in by_wt:
            by_wt[wt].append(t)

    for wt, wt_rows in by_wt.items():
        merged = CACHE.tasks_by_wt.get(wt, []) + wt_rows
        seen: set[int] = set()
        deduped: list[dict] = []
        for t in merged:
            tid = t.get("id")
            if tid is None or int(tid) in seen:
                continue
            seen.add(int(tid))
            deduped.append(t)
        # Пустой список — тоже ответ (запрос выполнен): иначе patched_fetch_pages
        # для этого wt ушёл бы в отдельный годовой HTTP-запрос.
        CACHE.tasks_by_wt[wt] = deduped


async def _fetch_mtfmi_batched(
    legacy: Any,
    task_ids: list[int],
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
) -> dict[int, list[dict]]:
    """mtfmi для всех заданий: prefetch JSON + live-пачки (до 300 task_id за запрос).

    Раньше недостающее качалось поштучно (1 задание = 1 запрос, пачками по 120 с
    барьером между ними). Теперь недостающее идёт через общий
    ``fetch_mtfmi_map_for_tasks`` (пачки параллельно, поштучно — только если пачку
    отвергли 4xx; культуры history_item запоминаются).
    """
    from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks, remember_history_crops
    from report1_prefetch_io import full_mtfmi_map_from_prefetch

    # Оригинал (до патча v2), чтобы не зациклиться на собственном кэше.
    fetch_batched = getattr(fetch_mtfmi_map_for_tasks, "_v2_original", fetch_mtfmi_map_for_tasks)

    full = full_mtfmi_map_from_prefetch()
    out: dict[int, list[dict]] = {}
    for tid in task_ids:
        rows = full.get(tid)
        if rows:
            out[tid] = list(rows)
    missing = [tid for tid in task_ids if tid not in out]
    if full:
        print(
            f"  [v2] mtfmi prefetch: покрыто {len(out)}/{len(task_ids)} задач",
            flush=True,
        )
    if missing:
        print(f"  [v2] mtfmi live batch: {len(missing)} задач", flush=True)
        live = await fetch_batched(
            client, sem, legacy.BASE_URL, missing, log_prefix="  [v2] ",
        )
        out.update(live)
        for tid in missing:
            out.setdefault(tid, [])  # запрошено, строк нет — это тоже ответ
    if full and out:
        # live-путь сам запоминает культуры; для строк из prefetch — докачиваем.
        try:
            await remember_history_crops(client, legacy.BASE_URL, out, sem)
        except Exception as exc:
            print(f"  [v2] культура history_item: {exc}", flush=True)
    if sum(len(v) for v in out.values()) == 0:
        print(
            f"  ВНИМАНИЕ: [v2] mtfmi пустой при {len(task_ids)} заданиях — "
            "«Факт общий» будет нулевым.",
            flush=True,
        )
    return out
