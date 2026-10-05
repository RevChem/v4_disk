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
    *,
    seasons_in_shift: list[str] | None = None,
) -> list[str]:
    """
    work.json cropRows — только сезон открытой сессии.
    Остальные сезоны плана (и сам сезон сессии, если вкладка пустая) берём из API v3.

    Годы, которых нет в плане, не добавляем: один сезон в редакторе
    проверяется отдельно, даже если в смене встретился другой год.
    """
    _ = seasons_in_shift  # сохранён в сигнатуре для совместимости вызовов; в расчёте не участвует
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

    # Задание без агрооперации: культура поля в том же сезоне (history_items).
    missing_fields: dict[int, set[str]] = defaultdict(set)
    for t in selected:
        tid = int(t["id"])
        season = str(t.get("season") or "")
        if not season:
            continue
        items = mtfmi_map.get(tid) or mtfmi_map.get(t["id"]) or []
        for fid, ha in _area_by_field_in_shift(items, cutoff, cutoff).items():
            if ha > 0 and not task_field_crop.get((tid, int(fid))):
                missing_fields[int(fid)].add(season)
    if missing_fields:
        async def _history_by_field() -> list[tuple[int, str, list[dict]]]:
            sem = asyncio.Semaphore(20)
            headers = {"X-User-Api-Token": API_TOKEN}
            async with httpx.AsyncClient(headers=headers, timeout=120) as client:
                async def one(fid: int, year: str) -> tuple[int, str, list[dict]]:
                    async with sem:
                        response = await client.get(
                            f"{BASE_URL}/history_items",
                            params={"field_id": fid, "year": year, "limit": 20},
                        )
                        response.raise_for_status()
                        data = response.json().get("data") or []
                        return fid, year, data

                jobs = [
                    one(fid, year)
                    for fid, years in missing_fields.items()
                    for year in years
                ]
                return list(await asyncio.gather(*jobs))

        for fid, year, items in asyncio.run(_history_by_field()):
            name = ""
            for hi in items:
                if hi.get("crop_id") is None:
                    continue
                name = crops.get(int(hi["crop_id"]), "").strip()
                if name:
                    break
            if not name:
                continue
            for t in selected:
                if str(t.get("season") or "") != year:
                    continue
                tid = int(t["id"])
                items_mt = mtfmi_map.get(tid) or []
                if fid in _area_by_field_in_shift(items_mt, cutoff, cutoff):
                    task_field_crop.setdefault((tid, fid), name)

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


def fill_crop_comparisons(
    target: dict,
    operations: dict,
    *,
    work: dict | None,
    firm_names: list[str],
    hints: list[str],
    shift_cache,
    task_years: list[int],
    ui_session: int | None,
    cutoff_iso: str,
    group_names: list[str] | None = None,
) -> None:
    """Сверка культур, когда work.json недоступен: культуры Cropwise из API v3."""
    from proverka.crop_compare import compare_svodka_cropwise_crops
    from proverka.season_cropwise import _parse_dt

    names = list(group_names or operations.keys())
    rows: list[dict] = []
    body = work.get("body") if isinstance(work, dict) else None
    for gn in names:
        op = operations.get(gn) or {}
        svodka_crop = op.get("area_today_by_firm_crop") or {}
        if not svodka_crop:
            continue
        cw_crop = cropwise_crops_all_seasons(
            body,
            firm_names,
            hints,
            shift_cache=shift_cache,
            task_years=list(task_years),
            ui_session=ui_session,
            wt_ids=list(op.get("work_type_ids") or []),
            cutoff=_parse_dt(cutoff_iso),
        )
        compared = compare_svodka_cropwise_crops(svodka_crop, cw_crop)
        for row in compared:
            row["group"] = gn
        rows.extend(compared)
    if not rows:
        return
    target["crop_comparisons"] = rows
    target.setdefault("comparison", {})
    target["comparison"]["crop_diff_count"] = sum(
        1 for row in rows if row.get("status") == "DIFF"
    )


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
    wt_set = {int(x) for x in wt_ids}
    tasks = [
        t for t in ((shift_cache.all_tasks if shift_cache is not None else None) or [])
        if int(t.get("work_type_id") or 0) in wt_set
    ]
    in_shift = sorted({str(t.get("season")) for t in tasks if t.get("season") not in (None, "")})
    missing = seasons_missing_from_ui(
        task_years, ui_session, ui_has, seasons_in_shift=in_shift,
    )
    merged: dict[str, dict[str, float]] = {}
    if ui_has:
        _add_crop_areas(merged, ui)
    if not missing or shift_cache is None:
        return {c: dict(f) for c, f in merged.items()}
    api = api_area_by_firm_crop(
        tasks,
        shift_cache.mtfmi_map,
        shift_cache.field_to_firm,
        set(missing),
        cutoff,
    )
    _add_crop_areas(merged, api)
    return {c: dict(f) for c, f in merged.items() if f}


def combine_editor_seasons(
    by_year: dict,
    years: list[int],
    firm_names: list[str],
    fact_hints: list[str],
    *,
    shift_cache: Any | None,
    wt_ids: list[int],
    cutoff,
) -> dict[str, Any]:
    """
    Сумма Cropwise по сезонам редактора планов.

    Несколько лет складываются. Один год берётся отдельно.
    Пустая вкладка сезона добирается из API v3 только за этот год.
    """
    from proverka.crop_compare import cropwise_area_by_firm_crop
    from proverka.cropwise_ui import parse_work_report_body

    crops: dict[str, dict[str, float]] = {}
    by_firm: dict[str, float] = defaultdict(float)
    by_year_ha: dict[int, float] = {}
    total = 0.0
    api_by_season: dict | None = None
    wt_set = {int(x) for x in wt_ids}
    for year in years:
        work = by_year.get(year) or by_year.get(str(year)) or {}
        body = work.get("body") if work.get("ok") else None
        usable = isinstance(body, dict) and not body.get("reportBlank")
        year_total = 0.0
        if usable:
            parsed = parse_work_report_body(body, firm_names, list(fact_hints))
            year_total = float(parsed.get("fact_total_ha") or 0)
            for firm, ha in (parsed.get("fact_by_firm") or {}).items():
                by_firm[str(firm)] += float(ha or 0)
            _add_crop_areas(crops, cropwise_area_by_firm_crop(body, firm_names, list(fact_hints)))
        elif shift_cache is not None:
            if api_by_season is None:
                api_by_season, _ids = shift_cache.api_for_wt_ids(list(wt_set))
            block = (api_by_season or {}).get(str(year)) or {}
            year_total = float(block.get("total_ha") or 0)
            for firm, ha in (block.get("by_firm") or {}).items():
                by_firm[str(firm)] += float(ha or 0)
            tasks = [
                t for t in (shift_cache.all_tasks or [])
                if int(t.get("work_type_id") or 0) in wt_set
            ]
            _add_crop_areas(crops, api_area_by_firm_crop(
                tasks, shift_cache.mtfmi_map, shift_cache.field_to_firm, {str(year)}, cutoff,
            ))
        by_year_ha[int(year)] = round(year_total, 2)
        total += year_total
    return {
        "total_ha": round(total, 2),
        "by_firm": {k: round(v, 2) for k, v in by_firm.items() if v >= 0.01},
        "crops": {c: dict(f) for c, f in crops.items() if f},
        "by_year": by_year_ha,
    }
