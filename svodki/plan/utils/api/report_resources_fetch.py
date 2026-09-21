# -*- coding: utf-8 -*-
"""Точечная загрузка fields/machines/implements по id из tasks+mtfmi (mass_request)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx

from mtaomi_batch_fetch import mass_request_async


def collect_report_resource_ids(
    tasks: list[dict],
    mtfmi_map: dict[int, list[dict]],
) -> tuple[set[int], set[int], set[int]]:
    """field / machine / implement id из заданий и mtfmi."""
    field_ids: set[int] = set()
    machine_ids: set[int] = set()
    implement_ids: set[int] = set()
    for items in mtfmi_map.values():
        for item in items:
            fid = item.get("field_id")
            if fid is not None:
                field_ids.add(int(fid))
    for t in tasks:
        mid = t.get("machine_id")
        if mid is not None:
            machine_ids.add(int(mid))
        iid = t.get("implement_id")
        if iid is not None:
            implement_ids.add(int(iid))
    return field_ids, machine_ids, implement_ids


async def load_fields_list_async(
    client: httpx.AsyncClient,
    base_url: str,
    field_ids: set[int] | list[int],
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    ids = sorted({int(i) for i in field_ids if i is not None})
    if not ids:
        return []
    print(f"  Загружаем {len(ids)} полей через mass_request...", flush=True)
    return await mass_request_async(client, base_url, "fields", ids, semaphore)


async def load_machines_map_async(
    client: httpx.AsyncClient,
    base_url: str,
    machine_ids: set[int] | list[int],
    semaphore: asyncio.Semaphore,
) -> dict[int, dict]:
    ids = sorted({int(i) for i in machine_ids if i is not None})
    if not ids:
        return {}
    print(f"  Загружаем {len(ids)} машин через mass_request...", flush=True)
    rows = await mass_request_async(client, base_url, "machines", ids, semaphore)
    return {int(m["id"]): m for m in rows if m.get("id") is not None}


async def load_implements_dict_async(
    client: httpx.AsyncClient,
    base_url: str,
    implement_ids: set[int] | list[int],
    semaphore: asyncio.Semaphore,
) -> dict[int, dict]:
    ids = sorted({int(i) for i in implement_ids if i is not None})
    if not ids:
        return {}
    print(f"  Загружаем {len(ids)} орудий через mass_request...", flush=True)
    rows = await mass_request_async(client, base_url, "implements", ids, semaphore)
    return {int(imp["id"]): imp for imp in rows if imp.get("id") is not None}


async def load_field_to_firm_for_ids(
    client: httpx.AsyncClient,
    base_url: str,
    semaphore: asyncio.Semaphore,
    field_ids: set[int],
    get_all_from_id_fn: Callable[[str, dict | None], list[dict]],
    build_field_to_firm_fn: Callable[[list[dict], list[dict], list[dict]], dict[int, str]],
) -> dict[int, str]:
    """Поля из mtfmi через mass_request; field_groups/group_folders — полный справочник."""
    fields_all = await load_fields_list_async(client, base_url, field_ids, semaphore)
    print("  Загружаем field_groups / group_folders...", flush=True)
    fgs_all = get_all_from_id_fn("field_groups")
    gfs_all = get_all_from_id_fn("group_folders")
    return build_field_to_firm_fn(fields_all, fgs_all, gfs_all)


async def load_report_resources(
    client: httpx.AsyncClient,
    base_url: str,
    semaphore: asyncio.Semaphore,
    tasks: list[dict],
    mtfmi_map: dict[int, list[dict]],
    get_all_from_id_fn: Callable[[str, dict | None], list[dict]],
    build_field_to_firm_fn: Callable[[list[dict], list[dict], list[dict]], dict[int, str]],
) -> tuple[dict[int, str], dict[int, dict], dict[int, dict]]:
    field_ids, machine_ids, implement_ids = collect_report_resource_ids(tasks, mtfmi_map)
    print(
        f"  Ресурсы отчёта: полей={len(field_ids)}, "
        f"машин={len(machine_ids)}, орудий={len(implement_ids)}",
        flush=True,
    )
    return await asyncio.gather(
        load_field_to_firm_for_ids(
            client, base_url, semaphore, field_ids,
            get_all_from_id_fn, build_field_to_firm_fn,
        ),
        load_machines_map_async(client, base_url, machine_ids, semaphore),
        load_implements_dict_async(client, base_url, implement_ids, semaphore),
    )
