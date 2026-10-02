# -*- coding: utf-8 -*-
"""Параллельная live-загрузка machine_tasks: окна времени × пачки work_type_id (сводка 2)."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta

import httpx

_ISO_FMT = "%Y-%m-%dT%H:%M:%S"


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "1" if default else "0").strip().lower()
    if raw in ("1", "true", "yes", "on", "да"):
        return True
    if raw in ("0", "false", "no", "off", "нет"):
        return False
    return default


def async_tasks_enabled() -> bool:
    if "SVODKI_REPORT2_ASYNC_TASKS" in os.environ:
        return _parse_bool_env("SVODKI_REPORT2_ASYNC_TASKS", True)
    return _parse_bool_env("SVODKI_ASYNC_TASKS", True)


def tasks_window_days(default: int = 14) -> int:
    for key in ("SVODKI_REPORT2_TASKS_WINDOW_DAYS", "SVODKI_TASKS_WINDOW_DAYS"):
        raw = os.environ.get(key, "").strip()
        if raw:
            try:
                return max(1, min(366, int(raw)))
            except ValueError:
                pass
    return max(1, min(366, default))


def split_time_windows(start_str: str, end_str: str, days: int) -> list[tuple[str, str]]:
    """Полуоткрытые окна [a, b) для end_time_gt_eq / end_time_lt."""
    a = datetime.strptime(start_str, _ISO_FMT)
    end = datetime.strptime(end_str, _ISO_FMT)
    out: list[tuple[str, str]] = []
    while a < end:
        b = min(a + timedelta(days=days), end)
        out.append((a.strftime(_ISO_FMT), b.strftime(_ISO_FMT)))
        a = b
    return out


def window_span_days(time_params: dict[str, str]) -> int | None:
    gt = time_params.get("end_time_gt_eq") or time_params.get("start_time_gt_eq")
    lt = time_params.get("end_time_lt") or time_params.get("start_time_lt_eq")
    if not gt or not lt:
        return None
    try:
        a = datetime.strptime(str(gt).replace(" ", "T")[:19], _ISO_FMT)
        b = datetime.strptime(str(lt).replace(" ", "T")[:19], _ISO_FMT)
    except ValueError:
        return None
    return max(0, (b - a).days)


async def fetch_machine_tasks_windowed_async(
    client: httpx.AsyncClient,
    base_url: str,
    work_type_ids: set[int] | list[int],
    time_params: dict[str, str],
    semaphore: asyncio.Semaphore,
    *,
    window_days: int | None = None,
    log_prefix: str = "",
) -> list[dict]:
    """Параллельно: split по времени × batch work_type_id.

    Если включено инкрементальное хранилище (SVODKI_TASK_STORE_DIR) и запрос — окно
    end_time (+season), ответ берётся из него: сезон качается один раз, дальше — дельта.
    """
    try:
        from svodki_task_store import eligible, get_store

        store = get_store(base_url) if eligible(time_params) else None
    except ImportError:
        store = None
    if store is not None:
        return await store.query_tasks(client, semaphore, work_type_ids, time_params, log_prefix)
    return await windowed_live_impl(
        client, base_url, work_type_ids, time_params, semaphore,
        window_days=window_days, log_prefix=log_prefix,
    )


async def windowed_live_impl(
    client: httpx.AsyncClient,
    base_url: str,
    work_type_ids: set[int] | list[int],
    time_params: dict[str, str],
    semaphore: asyncio.Semaphore,
    *,
    window_days: int | None = None,
    log_prefix: str = "",
) -> list[dict]:
    """Живая загрузка окнами × пачками work_type (без хранилища)."""
    from async_http import fetch_pages, should_fallback_single
    from machine_tasks_batch_fetch import WT_BATCH_SIZE, _chunked, dedupe_rows_by_id

    gt = time_params.get("end_time_gt_eq")
    lt = time_params.get("end_time_lt")
    if not gt or not lt:
        raise ValueError("windowed fetch requires end_time_gt_eq and end_time_lt")

    days = tasks_window_days() if window_days is None else window_days
    url = f"{str(base_url).rstrip('/')}/machine_tasks"
    wt_batches = _chunked(sorted({int(w) for w in work_type_ids}), WT_BATCH_SIZE)
    windows = split_time_windows(str(gt), str(lt), days)

    async def one(a: str, b: str, batch: list[int]) -> list[dict]:
        params = {
            "end_time_gt_eq": a,
            "end_time_lt": b,
            "work_type_id": ",".join(str(w) for w in batch),
        }
        if "season" in time_params:
            params["season"] = time_params["season"]
        try:
            return await fetch_pages(client, url, params, semaphore)
        except Exception as exc:
            if len(batch) == 1 or not should_fallback_single(exc):
                raise
            parts = await asyncio.gather(*[one(a, b, [w]) for w in batch])
            return [r for part in parts for r in part]

    if log_prefix and len(windows) > 1:
        print(
            f"  {log_prefix}machine_tasks windowed: {len(windows)}×{len(wt_batches)} запросов",
            flush=True,
        )
    res = await asyncio.gather(*[
        one(a, b, batch) for a, b in windows for batch in wt_batches
    ])
    return dedupe_rows_by_id([r for part in res for r in part])


def should_use_windowed_live_fetch(time_params: dict[str, str]) -> bool:
    if not async_tasks_enabled():
        return False
    if not time_params.get("end_time_gt_eq") or not time_params.get("end_time_lt"):
        return False
    span = window_span_days(time_params)
    if span is None:
        return False
    return span > tasks_window_days()


async def fetch_machine_tasks_date_windows_async(
    client: httpx.AsyncClient,
    base_url: str,
    time_params: dict[str, str],
    semaphore: asyncio.Semaphore,
    *,
    window_days: int | None = None,
    log_prefix: str = "",
) -> list[dict]:
    """Параллельная выгрузка machine_tasks по окнам времени БЕЗ фильтра work_type_id.

    Для сводок, которым нужны задания всех видов работ за длинный период
    (например, 26 — комбайны любых work_type). Окна полуоткрытые [a, b), как в
    ``fetch_machine_tasks_windowed_async``; дубли по id на стыках убираются.
    """
    from async_http import fetch_pages
    from machine_tasks_batch_fetch import dedupe_rows_by_id

    gt = time_params.get("end_time_gt_eq")
    lt = time_params.get("end_time_lt")
    if not gt or not lt:
        raise ValueError("date-window fetch requires end_time_gt_eq and end_time_lt")

    days = tasks_window_days() if window_days is None else window_days
    url = f"{str(base_url).rstrip('/')}/machine_tasks"
    windows = split_time_windows(str(gt), str(lt), days)
    extra = {k: v for k, v in time_params.items() if k not in ("end_time_gt_eq", "end_time_lt")}

    if log_prefix and len(windows) > 1:
        print(f"  {log_prefix}machine_tasks windowed (без work_type): {len(windows)} окон", flush=True)

    res = await asyncio.gather(*[
        fetch_pages(client, url, {**extra, "end_time_gt_eq": a, "end_time_lt": b}, semaphore)
        for a, b in windows
    ])
    return dedupe_rows_by_id([r for part in res for r in part])
