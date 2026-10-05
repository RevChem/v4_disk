"""Единая async-оркестрация загрузки заданий / mtfmi для сводок plan.

* fetch_all_pages — async_http.fetch_pages (retry, корректная пагинация);
* fetch_all_tasks_and_mtfmi — сутки + сезон batch machine_tasks → один проход mtfmi;
* report_async_client — один AsyncClient + семафор на прогон отчёта;
* run_report_async — точка входа build_report;
* background_async — фоновый asyncio.run (конвейер со справочниками, как сводка 2 _1).
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

import httpx

from async_http import fetch_pages as _fetch_pages
from async_http import retry_summary
from svodki_pipeline import BackgroundJob

T = TypeVar("T")


async def fetch_all_pages(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    """Совместимость с machine_tasks_batch_fetch / mtfmi_batch_fetch (page_fn)."""
    return await _fetch_pages(client, url, params, semaphore)


fetch_pages = fetch_all_pages


def dedupe_tasks_by_id(tasks: list[dict]) -> list[dict]:
    seen: set[int] = set()
    out: list[dict] = []
    for t in tasks:
        tid = t.get("id")
        if tid is None or tid in seen:
            continue
        seen.add(int(tid))
        out.append(t)
    return out


def filter_tasks_by_group_seasons_safe(
    tasks_today: list[dict],
    tasks_season: list[dict],
    group_key: str,
    report_date: date,
) -> tuple[list[dict], list[dict]]:
    try:
        from season_unified import filter_tasks_by_group_seasons as _filter_seasons

        return (
            _filter_seasons(tasks_today, group_key, report_date),
            _filter_seasons(tasks_season, group_key, report_date),
        )
    except Exception:
        return tasks_today, tasks_season


async def _fetch_machine_tasks_for_window(
    page_fn: Callable[..., Any],
    client: httpx.AsyncClient,
    base_url: str,
    work_type_ids: set[int],
    time_params: dict[str, str],
    semaphore: asyncio.Semaphore,
    *,
    season_years: list[str] | None,
    log_prefix: str,
    use_prefetch: bool = True,
) -> list[dict]:
    from machine_tasks_batch_fetch import dedupe_rows_by_id, fetch_machine_tasks_by_wt_async
    from svodki_prefetch_io import machine_tasks_window_from_prefetch

    if use_prefetch:
        pref_rows = await asyncio.to_thread(
            machine_tasks_window_from_prefetch,
            work_type_ids,
            time_params,
            season_years=season_years,
            log_prefix=log_prefix,
        )
        if pref_rows is not None:
            return pref_rows

    async def _live(params: dict[str, str]) -> list[dict]:
        from live_machine_tasks_fetch import (
            fetch_machine_tasks_windowed_async,
            should_use_windowed_live_fetch,
        )

        if should_use_windowed_live_fetch(params):
            return await fetch_machine_tasks_windowed_async(
                client, base_url, work_type_ids, params, semaphore,
                log_prefix=log_prefix,
            )
        return await fetch_machine_tasks_by_wt_async(
            page_fn, client, base_url, work_type_ids, params, semaphore,
            log_prefix=log_prefix,
        )

    if not season_years:
        return await _live(time_params)
    parts = await asyncio.gather(*[
        _live({**time_params, "season": year})
        for year in season_years
    ])
    rows: list[dict] = []
    for part in parts:
        rows.extend(part)
    return dedupe_rows_by_id(rows)


async def fetch_machine_tasks_window(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    base_url: str,
    work_type_ids: set[int],
    time_params: dict[str, str],
    *,
    season_years: list[str] | None = None,
    log_prefix: str = "",
    use_prefetch: bool = True,
) -> list[dict]:
    """machine_tasks за одно окно времени: prefetch → окна×пачки live → batch по work_type.

    ``use_prefetch=False`` — сразу live (если prefetch не покрывает нужные work_type и
    пустой список из него был бы ошибкой, как у докачки скашивания в сводке 10).

    Публичная обёртка для сводок, которым нужен один диапазон (например, весь сезон),
    без разбиения на «сутки + сезон» и без mtfmi.
    """
    return await _fetch_machine_tasks_for_window(
        fetch_all_pages, client, base_url, work_type_ids, time_params, semaphore,
        season_years=season_years, log_prefix=log_prefix, use_prefetch=use_prefetch,
    )


async def fetch_mtfmi_map_hybrid_async(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    base_url: str,
    task_ids: list[int],
    *,
    log_prefix: str = "",
    fetch_log_prefix: str | None = None,
    remember_crops: bool = False,
) -> dict[int, list[dict]]:
    """mtfmi по task_id: из prefetch-JSON, недостающее — live batch (как в сводке 2)."""
    from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks
    from svodki_prefetch_io import mtfmi_map_hybrid

    flp = fetch_log_prefix if fetch_log_prefix is not None else log_prefix
    ids = list({int(i) for i in task_ids})
    if not ids:
        return {}
    pref_map, missing = await asyncio.to_thread(mtfmi_map_hybrid, ids)
    if pref_map is None:
        return await fetch_mtfmi_map_for_tasks(
            client, semaphore, base_url, ids, log_prefix=flp,
        )
    if missing:
        print(
            f"  {log_prefix}mtfmi live batch: {len(missing)} задач (остальное из prefetch)",
            flush=True,
        )
        live = await fetch_mtfmi_map_for_tasks(
            client, semaphore, base_url, missing, log_prefix=flp,
        )
        result = {**pref_map, **live}
    else:
        result = pref_map
    if remember_crops:
        # live-путь сам кэширует культуры history_item; для строк из prefetch — догружаем
        from mtfmi_batch_fetch import remember_history_crops

        try:
            await remember_history_crops(client, base_url, result, semaphore)
        except Exception as exc:  # некритично: crop_of_item вернёт fallback
            print(f"  {log_prefix}культура history_item: {exc}", flush=True)
    try:
        from mtfmi_batch_fetch import _check_loaded

        _check_loaded(result, ids, crops_known=remember_crops)  # prefetch-строки не проходили проверки в этом процессе
    except Exception as exc:  # noqa: BLE001
        print(f"  {log_prefix}проверки данных: {exc}", flush=True)
    return result


@dataclass(frozen=True)
class TasksMtfmiFetchOptions:
    dedupe: bool = False
    machine_task_season_years: list[str] | None = None
    filter_group_key: str | None = None
    filter_report_date: date | None = None
    log_prefix: str = ""
    season_start_label: str | None = None
    mtfmi_log_prefix: str | None = None
    print_mtfmi_banner: bool = True


async def fetch_all_tasks_and_mtfmi(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    base_url: str,
    day_start: datetime,
    cutoff: datetime,
    season_start: datetime,
    work_type_ids: set[int],
    options: TasksMtfmiFetchOptions | None = None,
) -> tuple[list[dict], list[dict], dict[int, list[dict]]]:
    """Задания за сутки и за сезон (batch по work_type_id), затем mtfmi одним проходом."""

    opt = options or TasksMtfmiFetchOptions()
    lp = opt.log_prefix
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    season_start_str = season_start.strftime("%Y-%m-%dT%H:%M:%S")

    if opt.machine_task_season_years:
        print(
            f"  {lp}Фильтр machine_task.season в запросе: "
            f"{', '.join(opt.machine_task_season_years)} (по году)",
            flush=True,
        )

    dual_fetch = os.environ.get("SVODKI_DUAL_TASK_FETCH", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    season_covers_day = day_start >= season_start
    if season_covers_day and not dual_fetch:
        from svodki_prefetch_io import filter_tasks_end_time_window

        tasks_season = await _fetch_machine_tasks_for_window(
            fetch_all_pages,
            client,
            base_url,
            work_type_ids,
            {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_str},
            semaphore,
            season_years=opt.machine_task_season_years,
            log_prefix=lp,
        )
        tasks_today = filter_tasks_end_time_window(
            tasks_season, day_start_str, cutoff_str,
        )
        print(
            f"  {lp}[pipeline] machine_tasks: один HTTP-проход (сутки={len(tasks_today)} "
            f"из сезона={len(tasks_season)})",
            flush=True,
        )
    else:
        tasks_today, tasks_season = await asyncio.gather(
            _fetch_machine_tasks_for_window(
                fetch_all_pages, client, base_url, work_type_ids,
                {"end_time_gt_eq": day_start_str, "end_time_lt": cutoff_str},
                semaphore,
                season_years=opt.machine_task_season_years,
                log_prefix=lp,
            ),
            _fetch_machine_tasks_for_window(
                fetch_all_pages, client, base_url, work_type_ids,
                {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_str},
                semaphore,
                season_years=opt.machine_task_season_years,
                log_prefix=lp,
            ),
        )

    if opt.filter_group_key and opt.filter_report_date:
        tasks_today, tasks_season = filter_tasks_by_group_seasons_safe(
            tasks_today, tasks_season, opt.filter_group_key, opt.filter_report_date,
        )

    if opt.dedupe:
        tasks_today = dedupe_tasks_by_id(tasks_today)
        tasks_season = dedupe_tasks_by_id(tasks_season)

    season_lbl = opt.season_start_label or season_start.strftime("%d.%m.%Y")
    print(
        f"  {lp}Заданий за сутки: {len(tasks_today)}, "
        f"за сезон (с {season_lbl}): {len(tasks_season)}",
        flush=True,
    )

    all_task_ids = list({int(t["id"]) for t in tasks_today + tasks_season if t.get("id") is not None})
    mtfmi_lp = opt.mtfmi_log_prefix if opt.mtfmi_log_prefix is not None else lp
    if all_task_ids:
        if opt.print_mtfmi_banner:
            print(f"  {lp}Загружаем mtfmi для {len(all_task_ids)} заданий...", flush=True)
        mtfmi_map = await fetch_mtfmi_map_hybrid_async(
            client, semaphore, base_url, all_task_ids,
            log_prefix=lp, fetch_log_prefix=mtfmi_lp,
        )
    else:
        mtfmi_map = {}

    return tasks_today, tasks_season, mtfmi_map


def _default_semaphore_limit(fallback: int) -> int:
    raw = os.environ.get("SVODKI_HTTP_CONCURRENCY", "").strip()
    if raw:
        try:
            return max(1, min(128, int(raw)))
        except ValueError:
            pass
    return fallback


@asynccontextmanager
async def report_async_client(
    api_token: str,
    *,
    semaphore_limit: int | None = None,
    timeout: float = 60.0,
    max_connections: int = 60,
    max_keepalive: int = 30,
) -> AsyncIterator[tuple[httpx.AsyncClient, asyncio.Semaphore]]:
    limit = _default_semaphore_limit(semaphore_limit or 30)
    headers = {"X-User-Api-Token": api_token}
    sem = asyncio.Semaphore(limit)
    pool = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_keepalive)
    async with httpx.AsyncClient(headers=headers, timeout=timeout, limits=pool) as client:
        yield client, sem


def run_report_async(coro: Awaitable[T]) -> T:
    try:
        return asyncio.run(coro)
    finally:
        summary = retry_summary()
        if summary:
            print(f"  [http] {summary}", flush=True)


def background_async(name: str, fn: Callable[[], Awaitable[T]]) -> BackgroundJob:
    """Фоновый прогон корутины (отдельный event loop в потоке)."""

    def _runner() -> T:
        return run_report_async(fn())

    return BackgroundJob(_runner, name)


async def fetch_tasks_day_and_season(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    base_url: str,
    work_type_ids: set[int],
    day_start: datetime,
    cutoff: datetime,
    season_start: datetime,
    *,
    log_prefix: str = "",
) -> tuple[list[dict], list[dict]]:
    """Только machine_tasks (сутки + сезон), без mtfmi — для fetch_all_data в 4/6."""
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    season_start_str = season_start.strftime("%Y-%m-%dT%H:%M:%S")

    dual_fetch = os.environ.get("SVODKI_DUAL_TASK_FETCH", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if day_start >= season_start and not dual_fetch:
        from svodki_prefetch_io import filter_tasks_end_time_window

        tasks_season = await _fetch_machine_tasks_for_window(
            fetch_all_pages,
            client,
            base_url,
            work_type_ids,
            {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_str},
            semaphore,
            season_years=None,
            log_prefix=log_prefix,
        )
        tasks_today = filter_tasks_end_time_window(
            tasks_season, day_start_str, cutoff_str,
        )
        return tasks_today, tasks_season

    return await asyncio.gather(
        _fetch_machine_tasks_for_window(
            fetch_all_pages, client, base_url, work_type_ids,
            {"end_time_gt_eq": day_start_str, "end_time_lt": cutoff_str},
            semaphore, season_years=None, log_prefix=log_prefix,
        ),
        _fetch_machine_tasks_for_window(
            fetch_all_pages, client, base_url, work_type_ids,
            {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_str},
            semaphore, season_years=None, log_prefix=log_prefix,
        ),
    )
