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
    _patch_mtfmi_batch_fetch()
    legacy.load_reference_data = lambda: load_reference_v2(legacy)
    return orig_fetch_pages, orig_fetch_mtfmi


def _patch_mtfmi_batch_fetch() -> None:
    """Перехват ``mtfmi_batch_fetch.fetch_mtfmi_map_for_tasks`` — реальной точки входа.

    legacy больше не зовёт ``fetch_mtfmi``: и группа 1.1, и остальные группы берут mtfmi
    через ``fetch_mtfmi_map_for_tasks`` (импорт внутри функции → подмена атрибута модуля
    работает). Раньше этот вызов шёл мимо кэша v2: preload качал mtfmi впустую, а затем
    те же данные качались второй раз. Теперь кэш отдаётся, живой запрос — только для
    task_id, которых в кэше нет.
    """
    import mtfmi_batch_fetch as mb

    orig = mb.fetch_mtfmi_map_for_tasks
    if getattr(orig, "_v2_patched", False):
        return

    async def cached_fetch_mtfmi_map_for_tasks(client, sem, base_url, task_ids, *, log_prefix=""):
        ids = sorted({int(t) for t in task_ids if t is not None})
        result: dict[int, list[dict]] = {}
        missing: list[int] = []
        for tid in ids:
            rows = CACHE.get_mtfmi(tid)
            if rows is None:
                missing.append(tid)
            elif rows:
                result[tid] = rows  # как оригинал: задания без строк в словаре отсутствуют
        if missing:
            live = await orig(client, sem, base_url, missing, log_prefix=log_prefix)
            for tid in missing:
                rows = live.get(tid) or []
                CACHE.mtfmi_by_task[tid] = list(rows)
                if rows:
                    result[tid] = list(rows)
        return result

    cached_fetch_mtfmi_map_for_tasks._v2_patched = True  # type: ignore[attr-defined]
    cached_fetch_mtfmi_map_for_tasks._v2_original = orig  # type: ignore[attr-defined]
    mb.fetch_mtfmi_map_for_tasks = cached_fetch_mtfmi_map_for_tasks
