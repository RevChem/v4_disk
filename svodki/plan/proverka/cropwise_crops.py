"""Разбивка Cropwise по культурам: вкладка cropRows + сезоны, которых нет в сессии UI."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from proverka.crop_compare import cropwise_area_by_firm_crop


def _add_crop_areas(
    dst: dict[str, dict[str, float]],
    src: dict[str, dict[str, float]],
) -> None:
    for crop, by_firm in src.items():
        bucket = dst.setdefault(crop, {})
        for firm, ha in by_firm.items():
            bucket[firm] = round(float(bucket.get(firm, 0.0)) + float(ha or 0), 2)


def seasons_missing_from_ui(
    task_years: list[int] | None,
    ui_session: int | None,
    ui_has_crops: bool,
) -> list[str]:
    """
    work.json cropRows — только сезон открытой сессии.
    Остальные сезоны плана (и сам сезон сессии, если вкладка пустая) берём из API v3.
    """
    years = [str(int(y)) for y in (task_years or [])]
    if not years:
        return []
    if not ui_has_crops:
        return years
    session = str(int(ui_session)) if ui_session is not None else None
    return [y for y in years if y != session]


def api_area_by_firm_crop(
    tasks: list[dict],
    mtfmi_map: dict[int, list[dict]],
    field_to_firm: dict[int, str],
    seasons: set[str],
    cutoff,
) -> dict[str, dict[str, float]]:
    """
    Культура задания — HistoryItem агрооперации Cropwise (как вкладка «Культура» в UI).
    Площадь — covered_area_hourly до конца смены.
    """
    from datetime import datetime

    from plans import API_TOKEN, BASE_URL
    from proverka.season_cropwise import _area_by_field_in_shift, _get_all

    if not isinstance(cutoff, datetime) or not seasons:
        return {}
    selected = [
        t for t in tasks
        if str(t.get("season") or "") in seasons and t.get("id") is not None
    ]
    if not selected:
        return {}

    task_ids = [int(t["id"]) for t in selected]
    import httpx
    from mtaomi_batch_fetch import fetch_mtaomi_by_machine_task_ids, mass_request_async

    async def _load() -> tuple[dict[int, list[dict]], list[dict], list[dict]]:
        sem = asyncio.Semaphore(20)
        headers = {"X-User-Api-Token": API_TOKEN}
        async with httpx.AsyncClient(headers=headers, timeout=120) as client:
            mtaomi = await fetch_mtaomi_by_machine_task_ids(
                client, BASE_URL, task_ids, sem,
            )
            agro_ids: set[int] = set()
            for items in mtaomi.values():
                for item in items:
                    aoid = item.get("agro_operation_id")
                    if aoid is not None:
                        agro_ids.add(int(aoid))
            agro_ops = await mass_request_async(
                client, BASE_URL, "agro_operations", sorted(agro_ids), sem,
            )
            hist_ids = {
                int(ao["history_item_id"])
                for ao in agro_ops
                if ao.get("history_item_id") is not None
            }
            history = await mass_request_async(
                client, BASE_URL, "history_items", sorted(hist_ids), sem,
            )
        return mtaomi, agro_ops, history

    mtaomi, agro_ops, history = asyncio.run(_load())
    crops = {
        int(c["id"]): str(c.get("name") or c.get("title") or "").strip()
        for c in _get_all("crops")
        if c.get("id") is not None
    }
    hist_crop: dict[int, str] = {}
    for hi in history:
        if hi.get("id") is None or hi.get("crop_id") is None:
            continue
        name = crops.get(int(hi["crop_id"]), "").strip()
        if name:
            hist_crop[int(hi["id"])] = name

    task_field_crop: dict[tuple[int, int], str] = {}
    task_to_ops: dict[int, set[int]] = defaultdict(set)
    for tid, items in mtaomi.items():
        for item in items:
            aoid = item.get("agro_operation_id")
            if aoid is not None:
                task_to_ops[int(tid)].add(int(aoid))
    op_hist: dict[int, int] = {}
    op_field: dict[int, int] = {}
    for ao in agro_ops:
        if ao.get("id") is None:
            continue
        aoid = int(ao["id"])
        if ao.get("history_item_id") is not None:
            op_hist[aoid] = int(ao["history_item_id"])
        if ao.get("field_id") is not None:
            op_field[aoid] = int(ao["field_id"])
    for tid, aoids in task_to_ops.items():
        for aoid in aoids:
            crop = hist_crop.get(op_hist.get(aoid, -1), "")
            fid = op_field.get(aoid)
            if crop and fid is not None:
                task_field_crop[(tid, fid)] = crop

    from proverka.crop_compare import _crop_label

    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in selected:
        tid = int(t["id"])
        items = mtfmi_map.get(tid) or mtfmi_map.get(t["id"]) or []
        by_field = _area_by_field_in_shift(items, cutoff, cutoff)
        for fid, ha in by_field.items():
            if ha <= 0:
                continue
            crop = task_field_crop.get((tid, int(fid)), "")
            firm = field_to_firm.get(int(fid), "")
            if not crop or not firm:
                continue
            out[_crop_label(crop)][firm] += ha
    return {
        crop: {firm: round(ha, 2) for firm, ha in by_firm.items() if ha >= 0.01}
        for crop, by_firm in out.items()
    }


def cropwise_crops_all_seasons(
    body: Any,
    firm_names: list[str],
    fact_hints: list[str],
    *,
    shift_cache: Any | None,
    task_years: list[int] | None,
    ui_session: int | None,
    wt_ids: list[int],
    cutoff,
) -> dict[str, dict[str, float]]:
    """cropRows сессии плюс культуры остальных сезонов плана из API v3."""
    ui = cropwise_area_by_firm_crop(body, firm_names, fact_hints)
    ui_has = bool(ui) and not (isinstance(body, dict) and body.get("reportBlank"))
    missing = seasons_missing_from_ui(task_years, ui_session, ui_has)
    merged: dict[str, dict[str, float]] = {}
    if ui_has:
        _add_crop_areas(merged, ui)
    if not missing or shift_cache is None:
        return {c: dict(f) for c, f in merged.items()}

    wt_set = {int(x) for x in wt_ids}
    tasks = [
        t for t in (shift_cache.all_tasks or [])
        if int(t.get("work_type_id") or 0) in wt_set
    ]
    api = api_area_by_firm_crop(
        tasks,
        shift_cache.mtfmi_map,
        shift_cache.field_to_firm,
        set(missing),
        cutoff,
    )
    _add_crop_areas(merged, api)
    return {c: dict(f) for c, f in merged.items() if f}
