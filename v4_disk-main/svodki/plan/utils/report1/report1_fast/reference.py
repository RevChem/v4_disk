"""Справочники сводки 1: prefetch JSON или живой API."""

from __future__ import annotations

from datetime import date
from typing import Any

from report1_prefetch_io import load_prefetch_endpoints
from plans import build_field_to_crop_general


def _crop_name_from_op(op: dict) -> str:
    for key in ("crop_name", "crop_title", "culture_name", "culture_title"):
        v = str(op.get(key) or "").strip()
        if v:
            return v
    return ""


def _field_to_crop_from_agro_ops(
    legacy: Any,
    agro_ops: list[dict],
    *,
    lookup_wt_ids: frozenset[int] | None = None,
) -> dict[int, str]:
    wt_ids = lookup_wt_ids or getattr(legacy, "_CROP_LOOKUP_WT_IDS", frozenset())
    skip = getattr(legacy, "SKIP_CROPS", frozenset())
    season = str(getattr(legacy, "SEASON", ""))
    work_type_id = int(getattr(legacy, "WORK_TYPE_ID", 55))
    out: dict[int, str] = {}
    op_date: dict[int, str] = {}
    by_wt: dict[int, list[dict]] = {}
    for op in agro_ops or []:
        try:
            wt = int(op.get("work_type_id"))
        except (TypeError, ValueError):
            continue
        if wt not in wt_ids:
            continue
        by_wt.setdefault(wt, []).append(op)
    for wt_id in sorted(wt_ids):
        for op in by_wt.get(wt_id, []):
            if str(op.get("season") or season) != season and wt_id != work_type_id:
                continue
            fid = op.get("field_id")
            if fid is None:
                continue
            name = _crop_name_from_op(op)
            if not name or name in skip:
                continue
            fid_i = int(fid)
            d = str(op.get("completed_date") or op.get("completed_datetime") or "")
            if fid_i in out and d <= op_date.get(fid_i, ""):
                continue
            out[fid_i] = name
            op_date[fid_i] = d
    return out


def load_reference_data(legacy: Any) -> None:
    """Заполняет глобальные справочники в legacy-модуле."""
    endpoints = load_prefetch_endpoints()
    if endpoints:
        print("  [v2] Справочники из prefetch JSON...")
        _load_from_endpoints(legacy, endpoints)
    else:
        print("  [v2] Справочники из Cropwise API (prefetch JSON недоступен)...")
        orig = getattr(legacy, "_orig_load_reference_data", None)
        if orig is None:
            raise RuntimeError(
                "report1_fast: не найден _orig_load_reference_data — apply_patches() не вызывался?"
            )
        orig()


def _load_from_endpoints(legacy: Any, endpoints: dict) -> None:
    legacy.machines_map = {m["id"]: m for m in (endpoints.get("machines") or [])}
    print(f"    {len(legacy.machines_map)} машин")

    legacy.implements_map = {i["id"]: i for i in (endpoints.get("implements") or [])}
    print(f"    {len(legacy.implements_map)} орудий")

    crops = endpoints.get("crops") or []
    legacy.crop_map = {c["id"]: c.get("name") or c.get("title", "") for c in crops}
    print(f"    {len(legacy.crop_map)} культур")

    ferts = endpoints.get("fertilizers") or []
    legacy.fertilizer_name_map = {
        f["id"]: (f.get("name") or f.get("title") or f"fert_{f['id']}")
        for f in ferts
    }
    print(f"    {len(legacy.fertilizer_name_map)} удобрений")

    fields = endpoints.get("fields") or []
    fgs = endpoints.get("field_groups") or []
    legacy.field_to_fg = {f["id"]: f.get("field_group_id") for f in fields}
    legacy.fg_to_folder = {fg["id"]: fg.get("group_folder_id") for fg in fgs}
    legacy.folder_to_firm = dict(legacy.FOLDER_TO_FIRM)
    legacy._rebuild_field_firm_resolution(fields, fgs)
    legacy.field_area_map = {
        f["id"]: f.get("tillable_area") or f.get("calculated_area") or 0.0
        for f in fields
    }
    legacy.field_name_map = {
        f["id"]: (f.get("name") or f.get("title") or f"field_{f['id']}")
        for f in fields
    }
    print(f"    {len(fields)} полей, {len(fgs)} групп")

    report_date = getattr(legacy, "REPORT_DATE", date.today())
    season_year = int(report_date.year) + 1
    lookup_wt = getattr(legacy, "_CROP_LOOKUP_WT_IDS", None)
    agro_ops = endpoints.get("agro_operations") or []
    legacy.field_to_crop = _field_to_crop_from_agro_ops(legacy, agro_ops, lookup_wt_ids=lookup_wt)
    print(f"    field_to_crop из prefetch agro_operations: {len(legacy.field_to_crop)} полей")

    period_start = legacy.PERIOD_START_DATE
    period_end = legacy.REPORT_DATE
    _avail: set[int] = set()
    for r in endpoints.get("inventory_history_items") or []:
        if not r.get("available"):
            continue
        fid_r = r.get("historyable_id")
        if fid_r is None:
            continue
        raw_start = r.get("event_start_at")
        raw_end = r.get("event_end_at")
        try:
            ev_start = date.fromisoformat(str(raw_start)[:10]) if raw_start else None
        except ValueError:
            ev_start = None
        try:
            ev_end = date.fromisoformat(str(raw_end)[:10]) if raw_end else None
        except ValueError:
            ev_end = None
        if ev_start is None:
            continue
        if ev_start <= period_end and (ev_end is None or ev_end >= period_start):
            _avail.add(int(fid_r))
    legacy.available_field_ids = _avail
    print(
        f"    Полей available=True в периоде {period_start}–{period_end}: "
        f"{len(legacy.available_field_ids)}"
    )

    missing = legacy.available_field_ids - set(legacy.field_to_crop)
    if missing:
        history_by_year: dict[int, list[dict]] = {}
        for hi in endpoints.get("history_items") or []:
            try:
                crop_year = int(hi.get("year") or report_date.year)
            except (TypeError, ValueError):
                crop_year = int(report_date.year)
            history_by_year.setdefault(crop_year, []).append(hi)
        crop_map_int = {int(k): v for k, v in legacy.crop_map.items()}
        extra = build_field_to_crop_general(
            crop_map=crop_map_int,
            history_by_year=history_by_year,
            priority_year=season_year,
            skip_crops=legacy.SKIP_CROPS,
        )
        added = 0
        for fid_i, crop_name in extra.items():
            if fid_i in missing and fid_i not in legacy.field_to_crop:
                legacy.field_to_crop[fid_i] = crop_name
                added += 1
        print(
            f"    history_items fallback (prefetch): +{added} полей, "
            f"итого {len(legacy.field_to_crop)}"
        )
    else:
        print("    Все active-поля покрыты agro_operations — history_items не нужны.")

    legacy._resolve_fixed_fert_ids()
