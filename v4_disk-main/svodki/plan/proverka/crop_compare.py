"""Сверка факта за смену по культурам: сводка ↔ Cropwise UI (вкладка cropRows)."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from proverka.cropwise_ui import (
    _normalize_name,
    parse_work_report_detail_rows,
    subgroup_name_to_firm,
)
from proverka.ha_thresholds import compare_ha_status


def _crop_label(raw: str) -> str:
    try:
        from proverka.extractors import _crop_label as label_fn

        return label_fn(raw)
    except Exception:
        return str(raw or "").strip()


def cropwise_area_by_firm_crop(
    body: Any,
    firm_names: list[str],
    fact_hints: list[str],
) -> dict[str, dict[str, float]]:
    """Культура → агрофирма → га из дерева cropRows work.json."""
    if not isinstance(body, dict):
        return {}
    rows = body.get("cropRows") or []
    cols = body.get("cropColumns") or []
    if not rows or not cols:
        return {}
    detail = parse_work_report_detail_rows(rows, cols, firm_names, list(fact_hints))
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    firm_set = set(firm_names)
    for d in detail:
        if d.get("aggregate"):
            continue
        path = str(d.get("path") or "")
        parts = [p.strip() for p in path.split("→") if p.strip()]
        if not parts:
            continue
        crop = _crop_label(parts[0])
        firm = str(d.get("firm") or "").strip()
        if not firm and len(parts) > 1:
            firm = parts[1].strip()
        if firm not in firm_set:
            firm = subgroup_name_to_firm().get(_normalize_name(firm), "")
        if firm not in firm_set:
            continue
        ha = float(d.get("area_ha") or 0)
        if ha <= 0:
            continue
        out[crop][firm] = round(out[crop].get(firm, 0.0) + ha, 2)
    return {c: dict(by_firm) for c, by_firm in out.items()}


def compare_svodka_cropwise_crops(
    svodka_by_firm_crop: dict[str, dict[str, float]] | None,
    cropwise_by_firm_crop: dict[str, dict[str, float]] | None,
    *,
    tolerance_ha: float = 5.0,
) -> list[dict[str, Any]]:
    """
    Список сравнений по культурам (сумма по всем АФ + разбивка по фирмам).
    Сводка без area_today_by_firm_crop не участвует — только Cropwise не с чем.
    """
    sv = svodka_by_firm_crop or {}
    cw = cropwise_by_firm_crop or {}
    if not sv:
        return []
    crops = sorted(set(sv.keys()) | set(cw.keys()))
    rows: list[dict[str, Any]] = []
    for crop in crops:
        sf = sv.get(crop) or {}
        cf = cw.get(crop) or {}
        sv_total = round(sum(float(v or 0) for v in sf.values()), 2)
        cw_total = round(sum(float(v or 0) for v in cf.values()), 2)
        diff = round(sv_total - cw_total, 2)
        if sv_total < 0.01 and cw_total < 0.01:
            continue
        status = compare_ha_status(diff)
        if status == "DIFF" and abs(diff) <= tolerance_ha:
            status = "OK"
        firm_rows: list[dict[str, Any]] = []
        for firm in sorted(set(sf.keys()) | set(cf.keys())):
            sv_ha = round(float(sf.get(firm) or 0), 2)
            cw_ha = round(float(cf.get(firm) or 0), 2)
            fd = round(sv_ha - cw_ha, 2)
            if sv_ha < 0.01 and cw_ha < 0.01:
                continue
            fstatus = compare_ha_status(fd)
            if fstatus == "DIFF" and abs(fd) <= tolerance_ha:
                fstatus = "OK"
            firm_rows.append({
                "firm": firm,
                "svodka_ha": sv_ha,
                "cropwise_ha": cw_ha,
                "diff_ha": fd,
                "status": fstatus,
            })
        rows.append({
            "crop": crop,
            "svodka_ha": sv_total,
            "cropwise_ha": cw_total,
            "diff_ha": diff,
            "status": status,
            "firms": firm_rows,
        })
    return rows
