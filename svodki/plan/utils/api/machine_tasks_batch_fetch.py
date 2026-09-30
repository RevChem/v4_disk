# -*- coding: utf-8 -*-
"""Batch fetch machine_tasks / agro_operations by work_type_id (comma-separated).

Поддерживает оба call-style (на V3 часть скриптов уже на Downloads-API,
prefetch / 11 / 12 — на plan-API с url=):

  Downloads:
    fetch_machine_tasks_by_wt_async(
        fetch_pages_fn, client, base_url, work_type_ids, params, sem,
        *, log_prefix="",
    )

  Plan / prefetch:
    fetch_machine_tasks_by_wt_async(
        client, sem, wt_ids, base_params, fetch_pages,
        *, url=..., log_prefix="",
    )
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

WT_BATCH_SIZE = max(
    1,
    int(
        os.environ.get("SVODKI_MACHINE_TASKS_WT_BATCH_SIZE")
        or os.environ.get("CROPWISE_MACHINE_TASKS_WT_BATCH_SIZE", "25")
        or 25
    ),
)
# Alias for older plan-style imports.
MT_WT_ID_BATCH_SIZE = WT_BATCH_SIZE

FetchPagesAsync = Callable[
    [Any, str, dict[str, Any], asyncio.Semaphore],
    Awaitable[list[dict]],
]
GetAllSync = Callable[[str, dict[str, Any] | None], list[dict]]


def _chunked(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def dedupe_rows_by_id(rows: list[dict]) -> list[dict]:
    by_id: dict[int, dict] = {}
    for row in rows:
        rid = row.get("id")
        if rid is not None:
            by_id[int(rid)] = row
    return list(by_id.values())


# Alias used by plan-style helper.
_dedupe_tasks = dedupe_rows_by_id


async def fetch_by_work_type_ids_async(
    fetch_pages_fn: Callable[..., Any],
    client: Any,
    url: str,
    work_type_ids: list[int] | set[int],
    base_params: dict | None,
    sem: asyncio.Semaphore,
    *,
    log_prefix: str = "",
) -> list[dict]:
    """
    GET с work_type_id=1,2,3,... пачками по WT_BATCH_SIZE.
    Пустой batch или ошибка → fallback по одному work_type_id.
    """
    wt_list = sorted({int(w) for w in work_type_ids})
    if not wt_list:
        return []

    base = dict(base_params or {})
    batches = _chunked(wt_list, WT_BATCH_SIZE)
    all_rows: list[dict] = []
    fallback_wts: list[int] = []

    async def _one_batch(batch: list[int]) -> list[dict]:
        params = {**base, "work_type_id": ",".join(str(w) for w in batch)}
        return await fetch_pages_fn(client, url, params, sem)

    batch_results = await asyncio.gather(
        *[_one_batch(batch) for batch in batches],
        return_exceptions=True,
    )
    for batch, res in zip(batches, batch_results):
        if isinstance(res, BaseException):
            fallback_wts.extend(batch)
            continue
        if not res:
            fallback_wts.extend(batch)
            continue
        all_rows.extend(res)

    fallback_wts = list(dict.fromkeys(fallback_wts))
    if fallback_wts:
        if log_prefix:
            print(
                f"  {log_prefix}work_type_id batch fallback: "
                f"{len(fallback_wts)} поштучно",
                flush=True,
            )
        retry = await asyncio.gather(
            *[_one_batch([wt]) for wt in fallback_wts],
            return_exceptions=True,
        )
        for res in retry:
            if isinstance(res, BaseException):
                continue
            all_rows.extend(res)

    rows = dedupe_rows_by_id(all_rows)
    if "machine_tasks" in str(url):
        try:
            from season_mismatch_log import remember_tasks

            remember_tasks(rows)
        except Exception:
            pass
    return rows


async def fetch_machine_tasks_by_wt_async(
    *args: Any,
    url: str | None = None,
    log_prefix: str = "",
    **_kwargs: Any,
) -> list[dict]:
    """
    Dual API — см. docstring модуля.

    Plan/prefetch: url= обязателен (keyword), args =
      (client, sem, wt_ids, base_params, fetch_pages)

    Downloads: без url=, args =
      (fetch_pages_fn, client, base_url, work_type_ids, params, sem)
    """
    if url is not None:
        if len(args) < 5:
            raise TypeError(
                "fetch_machine_tasks_by_wt_async(plan-style) expects "
                "(client, sem, wt_ids, base_params, fetch_pages, *, url=...)"
            )
        client, sem, wt_ids, base_params, fetch_pages = args[:5]
        return await fetch_by_work_type_ids_async(
            fetch_pages, client, url, wt_ids, base_params, sem,
            log_prefix=log_prefix,
        )

    if len(args) < 6:
        raise TypeError(
            "fetch_machine_tasks_by_wt_async() missing arguments "
            "(Downloads: fetch_pages, client, base_url, wt_ids, params, sem; "
            "plan: client, sem, wt_ids, params, fetch_pages, *, url=...)"
        )
    fetch_pages_fn, client, base_url, work_type_ids, params, sem = args[:6]
    mt_url = f"{str(base_url).rstrip('/')}/machine_tasks"
    return await fetch_by_work_type_ids_async(
        fetch_pages_fn, client, mt_url, work_type_ids, params, sem,
        log_prefix=log_prefix,
    )


async def fetch_agro_operations_by_wt_async(
    fetch_pages_fn: Callable[..., Any],
    client: Any,
    base_url: str,
    work_type_ids: list[int] | set[int],
    params: dict | None,
    sem: asyncio.Semaphore,
    *,
    log_prefix: str = "",
) -> list[dict]:
    url = f"{str(base_url).rstrip('/')}/agro_operations"
    return await fetch_by_work_type_ids_async(
        fetch_pages_fn, client, url, work_type_ids, params, sem,
        log_prefix=log_prefix,
    )


def fetch_by_work_type_ids_sync(
    get_all_from_id_fn: Callable[[str, dict | None], list[dict]],
    endpoint: str,
    work_type_ids: list[int] | set[int],
    base_params: dict | None = None,
    *,
    log_prefix: str = "",
) -> list[dict]:
    """Sync: get_all_from_id с batch work_type_id и fallback поштучно."""
    wt_list = sorted({int(w) for w in work_type_ids})
    if not wt_list:
        return []

    base = dict(base_params or {})
    all_rows: list[dict] = []
    fallback_wts: list[int] = []

    for batch in _chunked(wt_list, WT_BATCH_SIZE):
        params = {**base, "work_type_id": ",".join(str(w) for w in batch)}
        try:
            rows = get_all_from_id_fn(endpoint, params)
        except Exception:
            fallback_wts.extend(batch)
            continue
        if not rows:
            fallback_wts.extend(batch)
            continue
        all_rows.extend(rows)

    fallback_wts = list(dict.fromkeys(fallback_wts))
    if fallback_wts:
        if log_prefix:
            print(
                f"  {log_prefix}work_type_id batch fallback: "
                f"{len(fallback_wts)} поштучно",
                flush=True,
            )
        for wt in fallback_wts:
            try:
                rows = get_all_from_id_fn(endpoint, {**base, "work_type_id": wt})
            except Exception:
                continue
            all_rows.extend(rows)

    rows = dedupe_rows_by_id(all_rows)
    if str(endpoint) == "machine_tasks":
        try:
            from season_mismatch_log import remember_tasks

            remember_tasks(rows)
        except Exception:
            pass
    return rows


def fetch_machine_tasks_by_wt_sync(
    get_all_fn: GetAllSync,
    wt_ids: list[int] | set[int],
    base_params: dict[str, Any],
    *,
    endpoint: str = "machine_tasks",
    log_prefix: str = "",
) -> list[dict]:
    """Plan-style sync wrapper."""
    return fetch_by_work_type_ids_sync(
        get_all_fn, endpoint, wt_ids, base_params, log_prefix=log_prefix,
    )
