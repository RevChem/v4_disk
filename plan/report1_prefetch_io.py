"""Чтение prefetch JSON и загрузка mtfmi с догрузкой по HTTP (посев / сводка 1)."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import httpx

_PREFETCH_ENDPOINTS: dict[str, Any] | None = None
_PREFETCH_LOAD_DONE = False
_FULL_MTFMI_MAP: dict[int, list[dict]] | None = None


def load_prefetch_endpoints() -> dict[str, Any] | None:
    global _PREFETCH_ENDPOINTS, _PREFETCH_LOAD_DONE
    if _PREFETCH_LOAD_DONE:
        return _PREFETCH_ENDPOINTS
    _PREFETCH_LOAD_DONE = True
    raw = (os.environ.get("SVODKI_PREFETCH_DATA_PATH") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(Path(raw).read_text(encoding="utf-8"))
        endpoints = data.get("endpoints")
        if isinstance(endpoints, dict):
            _PREFETCH_ENDPOINTS = endpoints
            profile = str(data.get("prefetch_profile") or "").strip()
            mt = endpoints.get("machine_tasks") or []
            mm = endpoints.get("machine_task_field_mapping_items") or []
            print(
                f"  Prefetch JSON ({profile or 'default'}): "
                f"machine_tasks={len(mt)}, mtfmi rows={len(mm)}",
                flush=True,
            )
    except Exception as exc:
        print(f"  Prefetch JSON не прочитан ({exc}), живой API", flush=True)
    return _PREFETCH_ENDPOINTS


def _task_id_from_mtfmi_row(row: dict) -> int | None:
    for key in ("machine_task_id", "machine_taskId", "task_id"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except Exception:
            continue
    return None


def full_mtfmi_map_from_prefetch() -> dict[int, list[dict]]:
    """Полный индекс mtfmi из prefetch JSON (кэшируется)."""
    global _FULL_MTFMI_MAP
    if _FULL_MTFMI_MAP is not None:
        return _FULL_MTFMI_MAP
    endpoints = load_prefetch_endpoints()
    out: dict[int, list[dict]] = {}
    if not endpoints:
        _FULL_MTFMI_MAP = out
        return out
    for row in endpoints.get("machine_task_field_mapping_items") or []:
        tid = _task_id_from_mtfmi_row(row)
        if tid is None:
            continue
        out.setdefault(tid, []).append(row)
    _FULL_MTFMI_MAP = out
    return out


def machine_tasks_from_prefetch(
    wt_ids: set[int],
    *,
    season_start,
    cutoff,
    parse_dt: Callable[[Any], Any],
    loose_end_filter: bool = True,
) -> list[dict]:
    """
    machine_tasks из prefetch JSON.
    loose_end_filter=True: не отсекаем задания после 23:59 даты отчёта.
    """
    endpoints = load_prefetch_endpoints()
    if not endpoints:
        return []
    wt_set = set(wt_ids)
    seen_ids: set[int] = set()
    out: list[dict] = []
    for t in endpoints.get("machine_tasks") or []:
        try:
            wt = int(t.get("work_type_id"))
        except Exception:
            continue
        if wt not in wt_set:
            continue
        tid = t.get("id")
        if tid is None:
            continue
        tid_i = int(tid)
        if tid_i in seen_ids:
            continue
        end_dt = parse_dt(t.get("end_time"))
        if end_dt is None:
            continue
        if end_dt < season_start:
            continue
        if not loose_end_filter and end_dt >= cutoff:
            continue
        seen_ids.add(tid_i)
        out.append(t)
    return out


def _prefetch_strict_enabled() -> bool:
    return os.environ.get("SVODKI_PREFETCH_STRICT", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


@contextmanager
def _allow_live_api_for_mtfmi():
    """Для догрузки mtfmi снимаем флаги, блокирующие живой Cropwise API."""
    saved: dict[str, str | None] = {}
    for key in (
        "SVODKI_PREFETCH_STRICT",
        "SVODKI_API_CACHE_READ_ONLY",
    ):
        if key in os.environ:
            saved[key] = os.environ.pop(key)
    try:
        yield
    finally:
        for key, val in saved.items():
            if val is not None:
                os.environ[key] = val


async def fetch_mtfmi_for_tasks(
    task_ids: list[int],
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
    *,
    base_url: str,
    log_prefix: str = "",
) -> dict[int, list[dict]]:
    if not task_ids:
        return {}

    full = full_mtfmi_map_from_prefetch()
    out: dict[int, list[dict]] = {}
    for tid in task_ids:
        rows = full.get(tid)
        if rows:
            out[tid] = list(rows)

    missing = [tid for tid in task_ids if not out.get(tid)]
    covered = len(task_ids) - len(missing)
    if full:
        print(
            f"  {log_prefix}mtfmi prefetch: покрыто {covered}/{len(task_ids)} задач "
            f"(всего в JSON: {len(full)})",
            flush=True,
        )

    if not missing:
        return out

    url = f"{base_url}/machine_task_field_mapping_items"

    async def _one(tid: int) -> tuple[int, list[dict]]:
        items: list[dict] = []
        for attempt in range(3):
            items = []
            from_id = 0
            try:
                while True:
                    async with semaphore:
                        r = await client.get(
                            url,
                            params={
                                "machine_task_id": tid,
                                "from_id": from_id,
                                "limit": 1000,
                            },
                        )
                    if r.status_code >= 400:
                        raise httpx.HTTPStatusError(
                            f"mtfmi HTTP {r.status_code}",
                            request=r.request,
                            response=r,
                        )
                    body = r.json()
                    chunk = body.get("data", [])
                    if not chunk:
                        break
                    items.extend(chunk)
                    meta = body.get("meta", {}).get("response", {})
                    last_id = meta.get("last_record_id")
                    if not last_id or meta.get("obtained_records", 0) < 100:
                        break
                    from_id = last_id + 1
                return tid, items
            except Exception as exc:
                if attempt >= 2:
                    print(
                        f"  {log_prefix}mtfmi task {tid}: ошибка после 3 попыток ({exc})",
                        flush=True,
                    )
                    return tid, []
                await asyncio.sleep(0.4 * (attempt + 1))
        return tid, items

    print(
        f"  {log_prefix}догрузка mtfmi по HTTP: {len(missing)} задач "
        f"({'strict off' if _prefetch_strict_enabled() else 'live API'})",
        flush=True,
    )
    chunk_size = int(os.environ.get("SVODKI_MTFMI_HTTP_CHUNK", "120"))
    with _allow_live_api_for_mtfmi():
        for i in range(0, len(missing), chunk_size):
            chunk = missing[i : i + chunk_size]
            results = await asyncio.gather(*[_one(tid) for tid in chunk])
            for tid, items in results:
                if items:
                    out[tid] = items
                elif tid not in out:
                    out[tid] = []

    if sum(len(out.get(tid) or []) for tid in task_ids) == 0:
        print(
            f"  ВНИМАНИЕ: {log_prefix}mtfmi пустой при "
            f"{len(task_ids)} заданиях — «Факт общий» будет нулевым.",
            flush=True,
        )
    return out
