"""Monkey-patch fetch_pages / fetch_mtfmi / load_reference в legacy-модуле."""

from __future__ import annotations

from typing import Any

from .cache import CACHE
from .reference import load_reference_data as load_reference_v2


def apply_patches(legacy: Any) -> tuple[Any, Any]:
    """Патчит legacy; возвращает (orig_fetch_pages, orig_fetch_mtfmi) для HTTP-fallback."""
    orig_fetch_pages = legacy.fetch_pages
    orig_fetch_mtfmi = legacy.fetch_mtfmi
    orig_load_reference_data = legacy.load_reference_data
    legacy._orig_load_reference_data = orig_load_reference_data

    async def patched_fetch_pages(client, url, params, sem):
        if "machine_tasks" in url:
            cached = CACHE.get_machine_tasks(params or {})
            if cached is not None:
                return cached
        if "agro_operations" in url:
            cached = CACHE.get_agro_operations(params or {})
            if cached is not None:
                return cached
        if "machine_task_field_mapping_items" in url:
            tid = (params or {}).get("machine_task_id")
            if tid is not None:
                rows = CACHE.get_mtfmi(int(tid))
                if rows is not None:
                    return rows
        return await orig_fetch_pages(client, url, params, sem)

    async def patched_fetch_mtfmi(client, task_id, sem):
        rows = CACHE.get_mtfmi(int(task_id))
        if rows is not None:
            return int(task_id), rows
        return await orig_fetch_mtfmi(client, task_id, sem)

    legacy.fetch_pages = patched_fetch_pages
    legacy.fetch_mtfmi = patched_fetch_mtfmi
    legacy.load_reference_data = lambda: load_reference_v2(legacy)
    return orig_fetch_pages, orig_fetch_mtfmi
