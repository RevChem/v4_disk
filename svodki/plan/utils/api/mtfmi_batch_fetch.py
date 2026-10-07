# -*- coding: utf-8 -*-
"""Пакетная загрузка machine_task_field_mapping_items (до N task_id за HTTP-запрос)."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict

import httpx

from async_http import (
    fetch_pages as _fetch_pages,  # noqa: F401  (имя сохранено для обратной совместимости)
    reraise_if_strict,
    should_fallback_single,
)

MTFMI_BATCH_SIZE = max(1, int(os.environ.get("SVODKI_MTFMI_BATCH_SIZE", "300") or 300))

# Строка без history_item_id или с чужим полем. Гектары остаются в итоге.
UNDEFINED_CROP = "Культура не определена"

# history_item_id → культура строки площади. Заполняется при загрузке mtfmi.
_HISTORY_CROP: dict[int, str] = {}
# history_item_id → field_id записи. Чужое поле не подписываем его культурой.
_HISTORY_FIELD: dict[int, int] = {}
_CROPS_BY_ID: dict[int, str] | None = None
_HISTORY_ERRORS: list[str] = []


def _chunked(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


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
    """mtfmi по task_id. С SVODKI_TASK_STORE_DIR — из инкрементального хранилища
    (докачиваются только отсутствующие и изменённые задания), иначе — живая пачками."""
    try:
        from svodki_task_store import get_store

        store = get_store(base_url)
    except ImportError:
        store = None
    if store is not None:
        result = await store.get_mtfmi(
            client, sem, base_url, task_ids, fetch_mtfmi_map_live,
            log_prefix=log_prefix, crops=_HISTORY_CROP, remember=remember_history_crops,
        )
        _check_loaded(result, task_ids)  # live-путь проверяет сам, хранилище — нет
        return result
    return await fetch_mtfmi_map_live(client, sem, base_url, task_ids, log_prefix=log_prefix)


def _check_loaded(mtfmi_map: dict, requested=None, *, crops_known: bool = True) -> None:
    """Сезоны + встроенные проверки данных для строк, пришедших из хранилища."""
    try:
        from season_mismatch_log import log_loaded_mtfmi
        import svodki_sanity

        svodki_sanity.on_mtfmi(mtfmi_map, requested, crops_known=crops_known)  # в т.ч. задания без строк
        log_loaded_mtfmi(mtfmi_map)
    except Exception as exc:  # noqa: BLE001
        print(f"  проверки данных: {exc}", flush=True)


async def fetch_mtfmi_map_live(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    base_url: str,
    task_ids: list[int],
    *,
    log_prefix: str = "",
) -> dict[int, list[dict]]:
    """mtfmi пачками; при отклонённом пакете (4xx) — поштучный fallback."""
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
            # Retry исчерпаны внутри request_json; поштучно — только если пакет некорректен (4xx).
            if not should_fallback_single(res):
                raise res
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
                reraise_if_strict(res)  # раньше: задача молча выпадала из отчёта
                continue
            mtfmi_map[tid] = list(res)

    result = dict(mtfmi_map)
    try:
        await remember_history_crops(client, base_url, result, sem)
    except Exception as exc:
        # Сводка считается дальше: строки без культуры уйдут в «Культура не определена».
        note_history_load_error(exc, prefix=prefix)
    try:
        from season_mismatch_log import log_loaded_mtfmi
        import svodki_sanity

        svodki_sanity.on_mtfmi(result, unique)  # unique — запрошенные id, чтобы увидеть задания без строк
        log_loaded_mtfmi(result)
    except Exception as exc:
        print(f"  {prefix}сезоны культуры: {exc}", flush=True)
    return result


def _history_settled(hid: int) -> bool:
    """Запись уже есть в кэше. Загрузка факта её не пропускает: crop_id мог смениться."""
    have = hid in _HISTORY_CROP
    try:
        import svodki_sanity

        return svodki_sanity.settled(hid, have)
    except ImportError:
        return have


def _remember_meta(hi: dict) -> None:
    try:
        import svodki_sanity

        svodki_sanity.remember_history(hi)
    except ImportError:
        pass


def history_load_errors() -> list[str]:
    """Ошибки загрузки HistoryItem в этом процессе. Пустой список — загрузка прошла."""
    return list(_HISTORY_ERRORS)


def note_history_load_error(exc: BaseException, *, prefix: str = "") -> None:
    """Ошибка видна в логе и в проверке. Расчёт сводки из-за неё не останавливается."""
    msg = f"{prefix}культура history_item не загружена: {exc}"
    if msg not in _HISTORY_ERRORS and len(_HISTORY_ERRORS) < 20:
        _HISTORY_ERRORS.append(msg)
    print(f"  {msg}", flush=True)


def crop_of_history_id(history_item_id, fallback: str = "") -> str:
    """Культура записи истории. Пустая ссылка или незагруженная запись — fallback."""
    if history_item_id is None:
        return str(fallback or "")
    try:
        name = _HISTORY_CROP.get(int(history_item_id), "")
    except (TypeError, ValueError):
        return str(fallback or "")
    return name or str(fallback or "")


def _history_field_conflicts(item: dict) -> bool:
    """Запись истории принадлежит другому полю — её культурой строку не подписываем."""
    hid = item.get("history_item_id")
    fid = item.get("field_id")
    if hid is None or fid is None:
        return False
    try:
        owned = _HISTORY_FIELD.get(int(hid))
        return bool(owned) and int(fid) != int(owned)
    except (TypeError, ValueError):
        return False


def crop_of_item(item: dict, fallback: str = "") -> str:
    """Культура строки площади. Чужое поле и пустая ссылка не берут культуру с карты поля."""
    if _history_field_conflicts(item):
        return ""
    return crop_of_history_id(item.get("history_item_id"), fallback)


def crop_label(item: dict) -> str:
    """Культура для таблицы. Пустая ссылка остаётся отдельной строкой, гектары не вычитаются."""
    return crop_of_item(item, "") or UNDEFINED_CROP


async def _crops_by_id(
    client: httpx.AsyncClient,
    base_url: str,
    sem: asyncio.Semaphore,
    *,
    refresh: bool = False,
) -> dict[int, str]:
    global _CROPS_BY_ID
    if _CROPS_BY_ID is not None and not refresh:
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
    """Перечитывает HistoryItem строк этого прогона.

    Кэш прошлого запуска не оставляем: агроном мог сменить crop_id.
    Записей на задания смены немного, отдельный срок годности не нужен.
    """
    from mtaomi_batch_fetch import mass_request_async

    missing: list[int] = []
    seen: set[int] = set()
    for items in mtfmi_map.values():
        for item in items:
            hid = item.get("history_item_id")
            if hid is None:
                continue
            hid_i = int(hid)
            if hid_i in seen:
                continue
            seen.add(hid_i)
            missing.append(hid_i)
    if not missing:
        return
    crops = await _crops_by_id(client, base_url, sem, refresh=True)
    history = await mass_request_async(client, base_url, "history_items", missing, sem)
    for hi in history:
        _remember_meta(hi)
        if hi.get("id") is None:
            continue
        hid_i = int(hi["id"])
        if hi.get("field_id") is not None:
            try:
                _HISTORY_FIELD[hid_i] = int(hi["field_id"])
            except (TypeError, ValueError):
                pass
        crop_id = hi.get("crop_id")
        name = crops.get(int(crop_id), "") if crop_id is not None else ""
        _HISTORY_CROP[hid_i] = name
        try:
            year = int(hi.get("year") or 0)
        except (TypeError, ValueError):
            year = 0
        try:
            from season_mismatch_log import remember_history_item

            remember_history_item(hid_i, year or None, name)
        except Exception:
            pass
