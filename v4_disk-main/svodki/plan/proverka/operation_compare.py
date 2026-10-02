"""Сверка факта за смену по операциям между сводками 1, 2, 3…"""
from __future__ import annotations

from typing import Any

from proverka.operation_registry import (
    OPERATION_PEERS,
    display_group_name,
    peer_reports_for_crop_cross_check,
    peer_reports_for_group,
)
from proverka.ha_thresholds import compare_ha_status, diff_is_significant
from proverka.reports_config import REPORTS


def _area_from_operation(op: dict[str, Any]) -> dict[str, float]:
    raw = op.get("area_today_by_firm") or {}
    return {str(k): round(float(v or 0), 2) for k, v in raw.items()}


def _crop_area_from_operation(op: dict[str, Any]) -> dict[str, dict[str, float]]:
    raw = op.get("area_today_by_firm_crop") or {}
    return {
        str(crop): {str(firm): round(float(v or 0), 2) for firm, v in by_firm.items()}
        for crop, by_firm in raw.items()
    }


def _crop_totals(area_by_firm: dict[str, float]) -> float:
    return round(sum(area_by_firm.values()), 2)


def _norm_crop(name: str) -> str:
    return " ".join(str(name or "").casefold().replace("ё", "е").split())


def _slice_from_crop_rows(
    crops: list[dict[str, Any]],
    total: float | None,
) -> dict[str, Any]:
    crop_ha: dict[str, float] = {}
    firm_ha: dict[str, dict[str, float]] = {}
    norm: dict[str, str] = {}
    for cc in crops:
        crop = str(cc.get("crop") or "").strip()
        if not crop:
            continue
        crop_ha[crop] = round(float(cc.get("cropwise_ha") or 0), 2)
        norm[_norm_crop(crop)] = crop
        by_firm: dict[str, float] = {}
        for fr in cc.get("firms") or []:
            firm = str(fr.get("firm") or "").strip()
            if firm:
                by_firm[firm] = round(float(fr.get("cropwise_ha") or 0), 2)
        firm_ha[crop] = by_firm
    return {
        "total": None if total is None else round(float(total), 2),
        "crops": crop_ha,
        "firms": firm_ha,
        "norm": norm,
        "has_crops": bool(crop_ha),
    }


def _cropwise_from_operation_comparisons(
    reports: dict[str, Any],
    group_name: str,
) -> dict[str, Any] | None:
    """Факт Cropwise UI по операции из сводок 1/2 (одна цифра на культуру)."""
    found: dict[str, Any] | None = None
    for rid in peer_reports_for_group(group_name):
        oc = ((reports.get(rid) or {}).get("operation_comparisons") or {}).get(group_name) or {}
        if not isinstance(oc, dict) or not oc:
            continue
        crops = oc.get("crop_comparisons") or []
        cmp_ = oc.get("comparison") or {}
        total = cmp_.get("cropwise_fact_total_ha")
        if total is None:
            total = cmp_.get("cropwise_ui_ha")
        if not crops and total is None:
            continue
        sl = _slice_from_crop_rows(crops, total)
        by_firm = {
            str(firm): round(float(ha or 0), 2)
            for firm, ha in (cmp_.get("cropwise_by_firm") or {}).items()
            if float(ha or 0) >= 0.01
        }
        if by_firm:
            sl["by_firm"] = by_firm
        if found is None or (sl["has_crops"] and not found["has_crops"]):
            found = sl
        if found["has_crops"]:
            break
    return found


def _cropwise_from_thematic_report(
    reports: dict[str, Any],
    group_name: str,
) -> dict[str, Any] | None:
    """Если операцию считает одна тематическая сводка — берём её work.json."""
    from proverka.operation_registry import canonical_cropwise_report

    canon = canonical_cropwise_report(group_name)
    if not canon or canon not in reports:
        return None
    spec = REPORTS.get(canon)
    if not spec or tuple(spec.work_groups or ()) != (group_name,):
        return None
    result = reports[canon]
    cmp_ = result.get("comparison") or {}
    total = cmp_.get("cropwise_fact_total_ha")
    if total is None:
        total = cmp_.get("cropwise_ui_ha")
    stored = result.get("crop_comparisons") or []
    if stored:
        sl = _slice_from_crop_rows(stored, total)
        by_firm = {
            str(firm): round(float(ha or 0), 2)
            for firm, ha in (cmp_.get("cropwise_by_firm") or {}).items()
            if float(ha or 0) >= 0.01
        }
        if by_firm:
            sl["by_firm"] = by_firm
        return sl
    work = result.get("cropwise_work_report") or {}
    body = work.get("body") if work.get("ok") else None
    if not isinstance(body, dict) or not body.get("cropRows"):
        if total is None:
            return None
        sl = _slice_from_crop_rows([], total)
        by_firm = {
            str(firm): round(float(ha or 0), 2)
            for firm, ha in (cmp_.get("cropwise_by_firm") or {}).items()
            if float(ha or 0) >= 0.01
        }
        if by_firm:
            sl["by_firm"] = by_firm
        return sl
    firms = list((result.get("cropwise_parsed") or {}).get("fact_by_firm") or {})
    sv_firms = list(((result.get("svodka") or {}).get("area_today_by_firm") or {}))
    firm_names = list(dict.fromkeys([*firms, *sv_firms]))
    from proverka.crop_compare import cropwise_area_by_firm_crop

    by = cropwise_area_by_firm_crop(body, firm_names, list(spec.fact_column_hints))
    fake = [
        {
            "crop": crop,
            "cropwise_ha": round(sum(by_firm.values()), 2),
            "firms": [{"firm": firm, "cropwise_ha": ha} for firm, ha in by_firm.items()],
        }
        for crop, by_firm in by.items()
    ]
    if total is None and fake:
        total = round(sum(float(c["cropwise_ha"]) for c in fake), 2)
    if not fake and total is None:
        return None
    sl = _slice_from_crop_rows(fake, total)
    by_firm = {
        str(firm): round(float(ha or 0), 2)
        for firm, ha in (cmp_.get("cropwise_by_firm") or {}).items()
        if float(ha or 0) >= 0.01
    }
    if by_firm:
        sl["by_firm"] = by_firm
    return sl


def cropwise_for_cross_check(
    reports: dict[str, Any],
    group_name: str,
) -> dict[str, Any] | None:
    """Один срез Cropwise на операцию. Культуры сводок 1/2 важнее тематического отчёта."""
    primary = _cropwise_from_operation_comparisons(reports, group_name)
    if primary and primary["has_crops"]:
        if primary["total"] is None:
            primary["total"] = round(sum(primary["crops"].values()), 2)
        return primary
    thematic = _cropwise_from_thematic_report(reports, group_name)
    if thematic and thematic["has_crops"]:
        if thematic["total"] is None:
            thematic["total"] = round(sum(thematic["crops"].values()), 2)
        return thematic
    if primary and primary["total"] is not None:
        return primary
    return thematic


def _cropwise_crop_ha(sl: dict[str, Any] | None, crop: str) -> float | None:
    if not sl or not sl.get("has_crops"):
        return None
    if crop in sl["crops"]:
        return sl["crops"][crop]
    key = sl["norm"].get(_norm_crop(crop))
    if key:
        return sl["crops"][key]
    return 0.0


def _cropwise_firm_ha(sl: dict[str, Any] | None, crop: str, firm: str) -> float | None:
    if not sl or not sl.get("has_crops"):
        return None
    key = crop if crop in sl["firms"] else sl["norm"].get(_norm_crop(crop))
    if not key:
        return 0.0
    return float((sl["firms"].get(key) or {}).get(firm, 0.0))


def _gap_vs_cropwise(values: list[float], cropwise: float | None) -> float | None:
    """Наибольший |сводка − Cropwise| по имеющимся цифрам."""
    if cropwise is None:
        return None
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return None
    base = float(cropwise)
    return round(max(abs(v - base) for v in nums), 2)


def _apply_cropwise_max_diff(row: dict[str, Any]) -> None:
    """Max diff — разница сводки с Cropwise, не разброс сводок между собой."""
    gap = _gap_vs_cropwise(list((row.get("totals") or {}).values()), row.get("cropwise_ha"))
    if gap is not None:
        row["max_diff_ha"] = gap
        if row.get("status") != "partial":
            row["status"] = compare_ha_status(gap)
    for fr in row.get("firms") or []:
        fg = _gap_vs_cropwise(list((fr.get("values") or {}).values()), fr.get("cropwise_ha"))
        if fg is not None:
            fr["max_diff_ha"] = fg
    for cr in row.get("crops") or []:
        cg = _gap_vs_cropwise(list((cr.get("totals") or {}).values()), cr.get("cropwise_ha"))
        if cg is not None:
            cr["max_diff_ha"] = cg
            cr["status"] = compare_ha_status(cg)
        for fr in cr.get("firms") or []:
            fg = _gap_vs_cropwise(list((fr.get("values") or {}).values()), fr.get("cropwise_ha"))
            if fg is not None:
                fr["max_diff_ha"] = fg


def _attach_cropwise(row: dict[str, Any], sl: dict[str, Any] | None) -> None:
    row["cropwise_ha"] = sl["total"] if sl else None
    for cr in row.get("crops") or []:
        cr["cropwise_ha"] = _cropwise_crop_ha(sl, cr["crop"])
        for fr in cr.get("firms") or []:
            fr["cropwise_ha"] = _cropwise_firm_ha(sl, cr["crop"], fr["firm"])
    by_firm = dict((sl or {}).get("by_firm") or {})
    if not by_firm and sl and sl.get("firms"):
        summed: dict[str, float] = {}
        for crop_firms in sl["firms"].values():
            for firm, ha in crop_firms.items():
                summed[firm] = round(summed.get(firm, 0.0) + float(ha or 0), 2)
        by_firm = summed
    seen = {str(fr.get("firm") or "") for fr in row.get("firms") or []}
    for fr in row.get("firms") or []:
        firm = str(fr.get("firm") or "")
        fr["cropwise_ha"] = by_firm.get(firm) if firm in by_firm else None
    for firm, ha in sorted(by_firm.items()):
        if firm in seen or float(ha or 0) < 0.01:
            continue
        row.setdefault("firms", []).append({
            "firm": firm,
            "values": {},
            "max_diff_ha": None,
            "cropwise_ha": round(float(ha), 2),
        })


def operations_from_svodka(svodka: dict[str, Any]) -> dict[str, dict[str, Any]]:
    ops = svodka.get("operations")
    if isinstance(ops, dict) and ops:
        return ops
    groups = svodka.get("work_groups") or []
    if not groups:
        return {}
    gn = groups[0]
    return {
        gn: {
            "area_today_by_firm": svodka.get("area_today_by_firm") or {},
            "total_ha": float(svodka.get("total_ha") or 0),
            "work_type_ids": svodka.get("work_type_ids") or [],
        }
    }


def _operation_total_ha(op: dict[str, Any] | None) -> float:
    if not op:
        return 0.0
    if op.get("total_ha") is not None:
        return round(float(op.get("total_ha") or 0), 2)
    area = op.get("area_today_by_firm") or {}
    return round(sum(float(v or 0) for v in area.values()), 2)


def operation_for_proverka_display(
    oc: dict[str, Any] | None,
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Колонка «Сводка» в таблице Cropwise для сводок 1/2: локальный PDF-факт,
    не эталон тематической сводки (7, 11, …) из svodka_op.
    """
    oc = oc or {}
    for key in ("svodka_op_local", "svodka_op_r2"):
        cand = oc.get(key)
        if isinstance(cand, dict) and _operation_total_ha(cand) >= 0.01:
            return cand
    if isinstance(fallback, dict) and _operation_total_ha(fallback) >= 0.01:
        return fallback
    canon = oc.get("svodka_op")
    if isinstance(canon, dict):
        return canon
    return fallback or {}


def operation_for_cross_check(
    report_result: dict[str, Any],
    rid: str,
    group_name: str,
) -> dict[str, Any] | None:
    """
    Факт по операции для сверки между сводками.

    Берём локальный расчёт каждой сводки (как в её Excel/PDF), а не
    подставленный эталон из тематической сводки — иначе 1/2 всегда
    повторяют 11 и расхождения с 10/13 не видны.
    """
    spec = REPORTS.get(rid)
    if spec and spec.cropwise_per_operation:
        oc = (report_result.get("operation_comparisons") or {}).get(group_name) or {}
        for key in ("svodka_op_local", "svodka_op_r2"):
            cand = oc.get(key)
            if isinstance(cand, dict) and _operation_total_ha(cand) >= 0.01:
                return cand

    sv = report_result.get("svodka") or {}
    op = operations_from_svodka(sv).get(group_name)
    if op and _operation_total_ha(op) >= 0.01:
        return op

    if spec and spec.cropwise_per_operation:
        oc = (report_result.get("operation_comparisons") or {}).get(group_name) or {}
        canon_op = oc.get("svodka_op")
        if isinstance(canon_op, dict) and _operation_total_ha(canon_op) >= 0.01:
            return canon_op

    return op


def build_cross_check(batch: dict[str, Any]) -> dict[str, Any]:
    reports = batch.get("reports") or {}
    present = set(reports.keys())
    rows: list[dict[str, Any]] = []
    ok = diff = partial = crop_diff = 0

    for group_name in OPERATION_PEERS:
        peer_ids = [rid for rid in peer_reports_for_group(group_name) if rid in present]
        if len(peer_ids) < 2:
            continue

        totals: dict[str, float] = {}
        by_firm: dict[str, dict[str, float]] = {}
        # crop -> rid -> total_ha (сумма по всем АФ), для строки «между собой» по культуре.
        crop_totals: dict[str, dict[str, float]] = {}
        # crop -> firm -> rid -> ha, для детализации расхождений по культуре и АФ.
        crop_by_firm: dict[str, dict[str, dict[str, float]]] = {}
        crop_peer_ids = [
            rid for rid in peer_reports_for_crop_cross_check(group_name) if rid in present
        ]
        for rid in peer_ids:
            op = operation_for_cross_check(reports[rid], rid, group_name)
            if not op:
                continue
            area = _area_from_operation(op)
            totals[rid] = _operation_total_ha(op)
            for firm, ha in area.items():
                by_firm.setdefault(firm, {})[rid] = ha
        for rid in crop_peer_ids:
            op = operation_for_cross_check(reports[rid], rid, group_name)
            if not op:
                continue
            for crop, area_by_firm in _crop_area_from_operation(op).items():
                crop_totals.setdefault(crop, {})[rid] = _crop_totals(area_by_firm)
                for firm, ha in area_by_firm.items():
                    crop_by_firm.setdefault(crop, {}).setdefault(firm, {})[rid] = ha

        cw = cropwise_for_cross_check(reports, group_name)
        active_peers = [rid for rid in peer_ids if rid in totals]
        if len(active_peers) < 2:
            partial += 1
            partial_row = {
                "group_name": group_name,
                "display_name": display_group_name(group_name),
                "peer_reports": peer_ids,
                "active_reports": active_peers,
                "totals": totals,
                "status": "partial",
                "max_diff_ha": None,
                "note": "данные не во всех сводках (сводка 1 без standalone?)",
                "crops": [],
            }
            _attach_cropwise(partial_row, cw)
            _apply_cropwise_max_diff(partial_row)
            rows.append(partial_row)
            continue

        total_vals = [totals[r] for r in active_peers]
        max_diff = round(max(total_vals) - min(total_vals), 2)
        firm_rows: list[dict[str, Any]] = []
        for firm, vals in sorted(by_firm.items()):
            if len(vals) < 2:
                continue
            vlist = list(vals.values())
            fd = round(max(vlist) - min(vlist), 2)
            if diff_is_significant(fd) or any(vlist):
                firm_rows.append({"firm": firm, "values": vals, "max_diff_ha": fd})

        # Та же сверка, но по культуре (напр. «Рапс яровой» внутри «4.0 Посев»).
        # Показываем культуру только там, где она есть минимум в двух сводках
        # из peer_ids — иначе «расхождение» будет просто из-за того, что одна
        # из сводок ещё не отдаёт разбивку по культуре (см. area_today_by_firm_crop).
        crop_rows: list[dict[str, Any]] = []
        for crop, vals in sorted(crop_totals.items()):
            active_crop_peers = [rid for rid in active_peers if rid in vals]
            if len(active_crop_peers) < 2:
                continue
            vlist = [vals[rid] for rid in active_crop_peers]
            cdiff = round(max(vlist) - min(vlist), 2)
            crop_firm_rows: list[dict[str, Any]] = []
            for firm, by_rid in sorted((crop_by_firm.get(crop) or {}).items()):
                firm_peers = {rid: ha for rid, ha in by_rid.items() if rid in active_crop_peers}
                if len(firm_peers) < 2:
                    continue
                fvlist = list(firm_peers.values())
                ffd = round(max(fvlist) - min(fvlist), 2)
                if diff_is_significant(ffd) or any(fvlist):
                    crop_firm_rows.append({"firm": firm, "values": firm_peers, "max_diff_ha": ffd})
            crop_rows.append({
                "crop": crop,
                "active_reports": active_crop_peers,
                "totals": {rid: vals[rid] for rid in active_crop_peers},
                "max_diff_ha": cdiff,
                "status": compare_ha_status(cdiff),
                "firms": crop_firm_rows,
            })

        status = compare_ha_status(max_diff)

        canon = next((r for r in active_peers if r not in ("1", "2")), active_peers[-1])
        spec = REPORTS.get(canon)
        op_row = {
            "group_name": group_name,
            "display_name": display_group_name(group_name),
            "canonical_report": canon,
            "title": spec.title if spec else canon,
            "peer_reports": peer_ids,
            "active_reports": active_peers,
            "totals": totals,
            "max_diff_ha": max_diff,
            "status": status,
            "firms": firm_rows,
            "crops": crop_rows,
        }
        _attach_cropwise(op_row, cw)
        _apply_cropwise_max_diff(op_row)
        status = op_row["status"]
        if status == "OK":
            ok += 1
        else:
            diff += 1
        # Расхождение по культуре считается после подстановки Cropwise:
        # max diff культуры — это |сводка − Cropwise|, а не разброс сводок.
        crop_diff_count = sum(1 for cr in op_row.get("crops") or [] if cr["status"] == "DIFF")
        if crop_diff_count:
            crop_diff += 1
        rows.append(op_row)

    return {
        "operations": rows,
        "summary": {
            "ok": ok, "diff": diff, "partial": partial, "total": len(rows),
            "crop_diff": crop_diff,
        },
    }
