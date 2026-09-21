# -*- coding: utf-8 -*-
"""Batch fetch machine_task_agro_operation_mapping_items (mtaomi) + mass_request."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from typing import Any

import httpx

MTAOMI_BATCH_SIZE = max(
    1,
    int(
        os.environ.get("MTAOMI_OP_BATCH_SIZE")
        or os.environ.get("AGRO_OP_BATCH_SIZE", "300")
        or 300
    ),
)
MASS_REQUEST_BATCH_SIZE = max(
    1,
    int(os.environ.get("MASS_REQUEST_BATCH_SIZE", "500") or 500),
)


def _chunked(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def _fetch_pages(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    sem: asyncio.Semaphore,
) -> list[dict]:
    items: list[dict] = []
    from_id = 0
    while True:
        async with sem:
            r = await client.get(url, params={**params, "from_id": from_id, "limit": 1000})
        r.raise_for_status()
        body = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        items.extend(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = int(last_id) + 1
    return items


async def _fetch_mtaomi_batch(
    client: httpx.AsyncClient,
    base_url: str,
    filter_key: str,
    ids: list[int],
    sem: asyncio.Semaphore,
) -> tuple[dict[int, list[dict]], list[int]]:
    """filter_key: agro_operation_id или machine_task_id."""
    url = f"{base_url.rstrip('/')}/machine_task_agro_operation_mapping_items"
    out: dict[int, list[dict]] = defaultdict(list)
    failed: list[int] = []
    empty_fallback: list[int] = []

    async def _one_batch(batch: list[int]) -> list[dict]:
        params = {filter_key: ",".join(str(i) for i in batch)}
        return await _fetch_pages(client, url, params, sem)

    batches = _chunked(ids, MTAOMI_BATCH_SIZE)
    batch_results = await asyncio.gather(
        *[_one_batch(batch) for batch in batches],
        return_exceptions=True,
    )
    for batch, res in zip(batches, batch_results):
        if isinstance(res, BaseException):
            failed.extend(batch)
            continue
        if not res:
            empty_fallback.extend(batch)
            continue
        bucket: dict[int, list[dict]] = defaultdict(list)
        id_field = filter_key
        for item in res:
            key = item.get(id_field)
            if key is not None:
                bucket[int(key)].append(item)
        for i in batch:
            out[i] = bucket.get(i, [])

    fallback_ids = list(dict.fromkeys(empty_fallback + failed))
    if fallback_ids:
        retry = await asyncio.gather(
            *[_one_batch([i]) for i in fallback_ids],
            return_exceptions=True,
        )
        for i, res in zip(fallback_ids, retry):
            if isinstance(res, BaseException):
                out.setdefault(i, [])
                continue
            out[i] = list(res)

    return dict(out), fallback_ids


async def fetch_mtaomi_by_agro_operation_ids(
    client: httpx.AsyncClient,
    base_url: str,
    agro_operation_ids: list[int] | set[int],
    sem: asyncio.Semaphore,
    *,
    log_prefix: str = "",
) -> dict[int, list[dict]]:
    ids = list(dict.fromkeys(int(i) for i in agro_operation_ids if i is not None))
    if not ids:
        return {}
    if log_prefix and len(ids) > 20:
        print(
            f"  {log_prefix}mtaomi by agro_operation_id: {len(ids)} ops, "
            f"батчи по {MTAOMI_BATCH_SIZE}",
            flush=True,
        )
    out, _ = await _fetch_mtaomi_batch(
        client, base_url, "agro_operation_id", ids, sem,
    )
    return out


async def fetch_mtaomi_by_machine_task_ids(
    client: httpx.AsyncClient,
    base_url: str,
    machine_task_ids: list[int] | set[int],
    sem: asyncio.Semaphore,
    *,
    log_prefix: str = "",
) -> dict[int, list[dict]]:
    ids = list(dict.fromkeys(int(i) for i in machine_task_ids if i is not None))
    if not ids:
        return {}
    if log_prefix and len(ids) > 20:
        print(
            f"  {log_prefix}mtaomi by machine_task_id: {len(ids)} tasks, "
            f"батчи по {MTAOMI_BATCH_SIZE}",
            flush=True,
        )
    out, _ = await _fetch_mtaomi_batch(
        client, base_url, "machine_task_id", ids, sem,
    )
    return out


async def mass_request_async(
    client: httpx.AsyncClient,
    base_url: str,
    endpoint: str,
    ids: list[int] | set[int],
    sem: asyncio.Semaphore,
    batch_size: int = MASS_REQUEST_BATCH_SIZE,
) -> list[dict]:
    """POST /{endpoint}/mass_request батчами."""
    id_list = list(dict.fromkeys(int(i) for i in ids if i is not None))
    if not id_list:
        return []

    url = f"{base_url.rstrip('/')}/{endpoint}/mass_request"
    out: list[dict] = []

    async def _one(batch: list[int]) -> list[dict]:
        async with sem:
            r = await client.post(url, json={"data": batch}, timeout=120)
        r.raise_for_status()
        return r.json().get("data", [])

    batches = _chunked(id_list, batch_size)
    results = await asyncio.gather(*[_one(b) for b in batches], return_exceptions=True)
    for res in results:
        if isinstance(res, BaseException):
            continue
        out.extend(res)
    return out
