"""Batched mtfmi для сводок 3–9 (пачки task_id вместо N отдельных HTTP)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


async def fetch_mtfmi_map_batched(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    task_ids: list[int],
    *,
    base_url: str,
    log_prefix: str = "",
) -> dict[int, list[dict[str, Any]]]:
    if not task_ids:
        return {}
    try:
        from report1_prefetch_io import fetch_mtfmi_for_tasks

        return await fetch_mtfmi_for_tasks(
            sorted({int(t) for t in task_ids}),
            semaphore,
            client,
            base_url=base_url,
            log_prefix=log_prefix,
        )
    except Exception as exc:
        print(f"{log_prefix}batched mtfmi fallback: {exc}", flush=True)
        return {}


async def fetch_mtfmi_pairs_batched(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    task_ids: list[int],
    *,
    base_url: str,
    fetch_one,
    log_prefix: str = "",
) -> dict[int, list[dict[str, Any]]]:
    """Batched HTTP; при ошибке — по одному task_id через fetch_one(client, tid, sem)."""
    out = await fetch_mtfmi_map_batched(
        client, semaphore, task_ids, base_url=base_url, log_prefix=log_prefix,
    )
    if out and all(out.get(int(tid)) is not None for tid in task_ids):
        if all(out.get(int(tid)) for tid in task_ids) or len(out) >= len(set(task_ids)) * 0.9:
            return out
    if not task_ids:
        return out
    print(f"{log_prefix}mtfmi per-task fallback: {len(task_ids)} tasks", flush=True)
    results = await asyncio.gather(
        *[fetch_one(client, int(tid), semaphore) for tid in task_ids],
        return_exceptions=True,
    )
    for res in results:
        if isinstance(res, BaseException):
            continue
        tid, items = res
        out[int(tid)] = items
    return out
