# -*- coding: utf-8 -*-
"""Пакетная загрузка machine_task_field_mapping_items (до N task_id за HTTP-запрос)."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict

import httpx

MTFMI_BATCH_SIZE = max(1, int(os.environ.get("SVODKI_MTFMI_BATCH_SIZE", "300") or 300))

# history_item_id → культура строки площади. Заполняется при загрузке mtfmi.
_HISTORY_CROP: dict[int, str] = {}
_CROPS_BY_ID: dict[int, str] | None = None


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


async def _fetch_batch(
    client: httpx.AsyncClient,
    base_url: str,
    task_ids: list[int],
    sem: asyncio.Semaphore,
) -> list[dict]:
    if not task_ids:
        return []
    url = f"{base_url.rstrip('/')}/machine_task_field_mapping_items"
    return await _fetch_pages(
        client,
        url,
        {"machine_task_id": ",".join(str(t) for t in task_ids)},
        sem,
    )


async def _fetch_single(
    client: httpx.AsyncClient,
    base_url: str,
    task_id: int,
    sem: asyncio.Semaphore,
) -> list[dict]:
    url = f"{base_url.rstrip('/')}/machine_task_field_mapping_items"
    return await _fetch_pages(client, url, {"machine_task_id": task_id}, sem)


async def fetch_mtfmi_map_for_tasks(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    base_url: str,
    task_ids: list[int],
    *,
    log_prefix: str = "",
) -> dict[int, list[dict]]:
    """mtfmi пачками; при ошибке батча — поштучный fallback."""
    unique = sorted({int(t) for t in task_ids if t is not None})
    if not unique:
        return {}

    mtfmi_map: dict[int, list[dict]] = defaultdict(list)
    batches = _chunked(unique, MTFMI_BATCH_SIZE)
    prefix = log_prefix or ""
    if len(unique) > 20:
        print(
            f"  {prefix}mtfmi batch: {len(unique)} задач, "
            f"{len(batches)} запросов по {MTFMI_BATCH_SIZE}",
            flush=True,
        )

    failed: list[int] = []
    batch_results = await asyncio.gather(
        *[_fetch_batch(client, base_url, batch, sem) for batch in batches],
        return_exceptions=True,
    )
    for batch, res in zip(batches, batch_results):
        if isinstance(res, BaseException):
            failed.extend(batch)
            continue
        for item in res:
            tid = item.get("machine_task_id")
            if tid is not None:
                mtfmi_map[int(tid)].append(item)

    if failed:
        print(
            f"  {prefix}mtfmi batch fallback: {len(failed)} задач поштучно",
            flush=True,
        )
        retry = await asyncio.gather(
            *[_fetch_single(client, base_url, tid, sem) for tid in failed],
            return_exceptions=True,
        )
        for tid, res in zip(failed, retry):
            if isinstance(res, BaseException):
                continue
            mtfmi_map[tid] = list(res)

    result = dict(mtfmi_map)
    try:
        await remember_history_crops(client, base_url, result, sem)
    except Exception as exc:
        print(f"  {prefix}культура history_item: {exc}", flush=True)
    try:
        from season_mismatch_log import log_loaded_mtfmi

        log_loaded_mtfmi(result)
    except Exception as exc:
        print(f"  {prefix}сезоны культуры: {exc}", flush=True)
    return result


def crop_of_item(item: dict, fallback: str = "") -> str:
    """Культура строки площади. Если history_item ещё не загружен — fallback."""
    hid = item.get("history_item_id")
    if hid is not None:
        name = _HISTORY_CROP.get(int(hid), "")
        if name:
            return name
    return str(fallback or "")


async def _crops_by_id(
    client: httpx.AsyncClient,
    base_url: str,
    sem: asyncio.Semaphore,
) -> dict[int, str]:
    global _CROPS_BY_ID
    if _CROPS_BY_ID is not None:
        return _CROPS_BY_ID
    rows = await _fetch_pages(client, f"{base_url.rstrip('/')}/crops", {}, sem)
    _CROPS_BY_ID = {
        int(c["id"]): str(c.get("name") or c.get("title") or "").strip()
        for c in rows
        if c.get("id") is not None
    }
    return _CROPS_BY_ID


async def remember_history_crops(
    client: httpx.AsyncClient,
    base_url: str,
    mtfmi_map: dict,
    sem: asyncio.Semaphore,
) -> None:
    """Догружает HistoryItem.crop_id для строк площади, которых ещё нет в кэше."""
    from mtaomi_batch_fetch import mass_request_async

    missing: list[int] = []
    seen: set[int] = set()
    for items in mtfmi_map.values():
        for item in items:
            hid = item.get("history_item_id")
            if hid is None:
                continue
            hid_i = int(hid)
            if hid_i in _HISTORY_CROP or hid_i in seen:
                continue
            seen.add(hid_i)
            missing.append(hid_i)
    if not missing:
        return
    crops = await _crops_by_id(client, base_url, sem)
    history = await mass_request_async(client, base_url, "history_items", missing, sem)
    for hi in history:
        if hi.get("id") is None or hi.get("crop_id") is None:
            continue
        name = crops.get(int(hi["crop_id"]), "")
        if name:
            _HISTORY_CROP[int(hi["id"])] = name
        try:
            year = int(hi.get("year") or 0)
        except (TypeError, ValueError):
            year = 0
        try:
            from season_mismatch_log import remember_history_item

            remember_history_item(int(hi["id"]), year or None, name)
        except Exception:
            pass
