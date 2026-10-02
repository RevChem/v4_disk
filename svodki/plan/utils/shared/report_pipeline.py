"""Конвейер сводки 2 (_1): справочники и API-fetch параллельно.

По умолчанию SVODKI_PIPELINE=1. SVODKI_PIPELINE=0 / SVODKI_REPORT2_PIPELINE=0 — последовательно.

Два режима:
  * run_parallel_refs_and_fetch — внутри async build_report (asyncio.gather + to_thread);
  * overlap_sync_with_async_fetch — sync build_report (BackgroundJob, как _prepare_group_selection).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Awaitable, Callable, TypeVar

from report_fetch_orchestration import (
    TasksMtfmiFetchOptions,
    background_async,
    fetch_all_tasks_and_mtfmi,
    report_async_client,
    run_report_async,
)
from svodki_pipeline import BackgroundJob, pipeline_enabled

T = TypeVar("T")
F = TypeVar("F")


def run_parallel_sync_pair(
    first: Callable[[], T],
    second: Callable[[], F],
    *,
    parallel: bool | None = None,
    thread_prefix: str = "svodki-pair",
) -> tuple[T, F]:
    """Два независимых sync-вызова параллельно (mtfmi ∥ agro в сводке 2)."""
    from concurrent.futures import ThreadPoolExecutor

    use = pipeline_enabled() if parallel is None else parallel
    if not use:
        return first(), second()
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix=thread_prefix) as ex:
        f1 = ex.submit(first)
        f2 = ex.submit(second)
        return f1.result(), f2.result()


async def run_parallel_refs_and_fetch(
    sync_refs: Callable[[], T],
    fetch: Callable[[], Awaitable[F]],
    *,
    parallel: bool | None = None,
    label: str = "API-fetch",
) -> tuple[T, F]:
    """Справочники (sync, в потоке) параллельно с async-загрузкой заданий/mtfmi."""
    use = pipeline_enabled() if parallel is None else parallel
    if use:
        t0 = time.monotonic()
        print(f"  [pipeline] параллельно: справочники + {label}...", flush=True)
        refs, data = await asyncio.gather(
            asyncio.to_thread(sync_refs),
            fetch(),
        )
        print(
            f"  [timing] pipeline ({label}): {time.monotonic() - t0:.1f} с",
            flush=True,
        )
        return refs, data
    return sync_refs(), await fetch()


def overlap_sync_with_async_fetch(
    sync_refs: Callable[[], T],
    async_fetch: Callable[[], Awaitable[F]],
    *,
    job_name: str = "report-prefetch",
    parallel: bool | None = None,
) -> tuple[T, F]:
    """Sync entry: фоновый asyncio.run(fetch) пока грузятся справочники."""
    use = pipeline_enabled() if parallel is None else parallel
    job: BackgroundJob | None = None
    if use:
        print(f"  [pipeline] фон: {job_name}...", flush=True)
        job = background_async(job_name, async_fetch)
    t0 = time.monotonic()
    refs = sync_refs()
    print(f"  [timing] справочники (wall): {time.monotonic() - t0:.1f} с", flush=True)
    fetch_out = job.result() if job else run_report_async(async_fetch())
    return refs, fetch_out


@dataclass
class TasksMtfmiResourcesBundle:
    tasks_today: list[dict]
    tasks_season: list[dict]
    mtfmi_map: dict[int, list[dict]]
    field_to_firm: dict[int, str]
    machines_map: dict[int, dict]
    implements_dict: dict[int, dict]


async def fetch_tasks_mtfmi_resources(
    api_token: str,
    base_url: str,
    day_start: datetime,
    cutoff: datetime,
    season_start: datetime,
    work_type_ids: set[int],
    build_field_to_firm_fn: Callable[[list[dict]], dict[int, str]],
    get_all_from_id_fn: Callable[..., list[dict]],
    *,
    semaphore_limit: int = 30,
    timeout: float = 60.0,
    mtfmi_options: TasksMtfmiFetchOptions | None = None,
    log_label: str = "",
) -> TasksMtfmiResourcesBundle:
    """Задания + mtfmi + fields/machines/implements — один AsyncClient."""
    from report_resources_fetch import load_report_resources

    async with report_async_client(
        api_token, semaphore_limit=semaphore_limit, timeout=timeout,
    ) as (client, semaphore):
        tasks_today, tasks_season, mtfmi_map = await fetch_all_tasks_and_mtfmi(
            client, semaphore, base_url,
            day_start, cutoff, season_start, work_type_ids,
            mtfmi_options,
        )
        all_tasks = tasks_today + tasks_season
        field_to_firm, machines_map, implements_dict = await load_report_resources(
            client, base_url, semaphore, all_tasks, mtfmi_map,
            get_all_from_id_fn, build_field_to_firm_fn,
        )
    if log_label:
        print(
            f"  [pipeline] {log_label}: сутки={len(tasks_today)}, "
            f"сезон={len(tasks_season)}, mtfmi={len(mtfmi_map)}",
            flush=True,
        )
    return TasksMtfmiResourcesBundle(
        tasks_today=tasks_today,
        tasks_season=tasks_season,
        mtfmi_map=mtfmi_map,
        field_to_firm=field_to_firm,
        machines_map=machines_map,
        implements_dict=implements_dict,
    )
