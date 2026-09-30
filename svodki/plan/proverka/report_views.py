"""Детализация proverka «от лица одной сводки».

Таблицы по культурам и по агрофирмам: расчёт модуля, файл (uploads), Cropwise, peer-сводки.
"""
from __future__ import annotations

from typing import Any, Iterator

from proverka.ha_thresholds import compare_ha_status


def _operation_total_ha(op: dict[str, Any] | None) -> float:
    if not op:
        return 0.0
    if op.get("total_ha") is not None:
        return float(op.get("total_ha") or 0)
    area = op.get("area_today_by_firm") or {}
    return round(sum(float(v or 0) for v in area.values()), 2)


def _work_groups_for_focus(
    focus_rid: str,
    batch: dict[str, Any],
    spec: Any,
) -> tuple[str, ...]:
    """Операции карточки: из каталога или из фактических operations сводки."""
    if spec.work_groups:
        return tuple(spec.work_groups)
    from proverka.operation_registry import peer_reports_for_group

    reports = batch.get("reports") or {}
    result = reports.get(focus_rid) or {}
    ops = (result.get("svodka") or {}).get("operations") or {}
    out: list[str] = []
    for group in sorted(ops.keys()):
        if focus_rid not in peer_reports_for_group(group):
            continue
        if _operation_total_ha(ops[group]) < 0.01:
            continue
        out.append(group)
    return tuple(out)


def _crop_comparisons_for_group(result: dict[str, Any], group: str) -> Iterator[dict[str, Any]]:
    for cc in result.get("crop_comparisons") or []:
        g = cc.get("group")
        if g and g != group:
            continue
        yield cc
    oc = (result.get("operation_comparisons") or {}).get(group) or {}
    for cc in oc.get("crop_comparisons") or []:
        if isinstance(cc, dict):
            yield cc


def _norm_crop(name: str) -> str:
    return " ".join(str(name or "").casefold().replace("ё", "е").split())


def _module_op(reports: dict[str, Any], rid: str, group: str) -> dict[str, Any]:
    from proverka.operation_compare import operation_for_cross_check

    return operation_for_cross_check(reports.get(rid) or {}, rid, group) or {}


def _crop_ha_from_op(op: dict[str, Any], crop: str) -> float | None:
    by_fc = op.get("area_today_by_firm_crop") or {}
    wanted = _norm_crop(crop)
    for name, firms in by_fc.items():
        if _norm_crop(name) == wanted:
            return round(sum(float(v or 0) for v in firms.values()), 2)
    return None


def _firm_ha_from_op(op: dict[str, Any], firm: str) -> float | None:
    area = op.get("area_today_by_firm") or {}
    if firm in area:
        return round(float(area[firm] or 0), 2)
    from proverka.file_values import _norm_firm_name

    want = _norm_firm_name(firm)
    for name, ha in area.items():
        if _norm_firm_name(name) == want:
            return round(float(ha or 0), 2)
    return None


def _file_crop(reports: dict[str, Any], rid: str, group: str, crop: str) -> float | None:
    crops = ((reports.get(rid) or {}).get("file_crops") or {}).get(group) or {}
    wanted = _norm_crop(crop)
    for name, ha in crops.items():
        if _norm_crop(name) == wanted:
            return round(float(ha or 0), 2)
    return None


def _file_firm(reports: dict[str, Any], rid: str, group: str, firm: str) -> float | None:
    firms = ((reports.get(rid) or {}).get("file_firms") or {}).get(group) or {}
    if firm in firms:
        return round(float(firms[firm] or 0), 2)
    from proverka.file_values import _norm_firm_name

    want = _norm_firm_name(firm)
    for name, ha in firms.items():
        if _norm_firm_name(name) == want:
            return round(float(ha or 0), 2)
    return None


def _peers_for_view(
    focus_rid: str,
    group: str,
    present: set[str],
) -> list[str]:
    from proverka.operation_registry import peer_reports_for_group

    out: list[str] = []
    for rid in peer_reports_for_group(group):
        if rid not in present:
            continue
        out.append(str(rid))
    if focus_rid not in out and focus_rid in present:
        out.append(focus_rid)
    return sorted(set(out), key=lambda x: (x != focus_rid, int(x) if x.isdigit() else x))


def _report_order(focus_rid: str, peer_ids: list[str]) -> list[str]:
    return [focus_rid] + [r for r in peer_ids if r != focus_rid]


def _spread(values: dict[str, float | None]) -> tuple[float, str]:
    nums = [float(v) for v in values.values() if v is not None]
    if len(nums) < 2:
        return 0.0, "OK"
    gap = round(max(nums) - min(nums), 2)
    return gap, compare_ha_status(gap)


def _column_defs(focus_rid: str, peer_ids: list[str]) -> list[dict[str, str]]:
    cols: list[dict[str, str]] = []
    for rid in _report_order(focus_rid, peer_ids):
        cols.append({"key": f"m_{rid}", "label": f"Сводка {rid} (расчёт)", "kind": "module"})
        cols.append({"key": f"f_{rid}", "label": f"Сводка {rid} (файл)", "kind": "file"})
    cols.append({"key": "cropwise", "label": "Cropwise", "kind": "cropwise"})
    return cols


def _values_for_crop(
    crop: str,
    focus_rid: str,
    peer_ids: list[str],
    reports: dict[str, Any],
    group: str,
    cw_ha: float | None,
) -> dict[str, float | None]:
    vals: dict[str, float | None] = {}
    for rid in _report_order(focus_rid, peer_ids):
        op = _module_op(reports, rid, group)
        vals[f"m_{rid}"] = _crop_ha_from_op(op, crop)
        vals[f"f_{rid}"] = _file_crop(reports, rid, group, crop)
    vals["cropwise"] = cw_ha
    return vals


def _values_for_firm(
    firm: str,
    focus_rid: str,
    peer_ids: list[str],
    reports: dict[str, Any],
    group: str,
    cw_ha: float | None,
) -> dict[str, float | None]:
    vals: dict[str, float | None] = {}
    for rid in _report_order(focus_rid, peer_ids):
        op = _module_op(reports, rid, group)
        vals[f"m_{rid}"] = _firm_ha_from_op(op, firm)
        vals[f"f_{rid}"] = _file_firm(reports, rid, group, firm)
    vals["cropwise"] = cw_ha
    return vals


def _build_operation_section(
    focus_rid: str,
    batch: dict[str, Any],
    group: str,
) -> dict[str, Any] | None:
    from proverka.operation_registry import display_group_name

    reports = batch.get("reports") or {}
    if focus_rid not in reports:
        return None
    present = set(reports.keys())
    peer_ids = _peers_for_view(focus_rid, group, present)
    result = reports[focus_rid]

    cropwise_by_crop: dict[str, float] = {}
    cropwise_firms: dict[str, dict[str, float]] = {}
    for cc in _crop_comparisons_for_group(result, group):
        crop = str(cc.get("crop") or "")
        if not crop:
            continue
        cropwise_by_crop[crop] = round(float(cc.get("cropwise_ha") or 0), 2)
        for fr in cc.get("firms") or []:
            firm = str(fr.get("firm") or "")
            if firm:
                cropwise_firms.setdefault(crop, {})[firm] = round(float(fr.get("cropwise_ha") or 0), 2)
    if not cropwise_by_crop:
        from proverka.operation_compare import cropwise_for_cross_check

        sl = cropwise_for_cross_check(reports, group)
        if sl and sl.get("has_crops"):
            for crop, ha in (sl.get("crops") or {}).items():
                cropwise_by_crop[str(crop)] = round(float(ha), 2)
            for crop, by_f in (sl.get("firms") or {}).items():
                for firm, ha in by_f.items():
                    cropwise_firms.setdefault(str(crop), {})[str(firm)] = round(float(ha), 2)

    crop_names: set[str] = set(cropwise_by_crop.keys())
    focus_op = _module_op(reports, focus_rid, group)
    for c in (focus_op.get("area_today_by_firm_crop") or {}).keys():
        crop_names.add(str(c))
    for rid in peer_ids:
        op = _module_op(reports, rid, group)
        for c in (op.get("area_today_by_firm_crop") or {}).keys():
            crop_names.add(str(c))
    file_crops = ((result.get("file_crops") or {}).get(group)) or {}
    crop_names.update(str(c) for c in file_crops.keys())
    for rid in peer_ids:
        peer_crops = ((reports.get(rid) or {}).get("file_crops") or {}).get(group) or {}
        crop_names.update(str(c) for c in peer_crops.keys())

    columns = _column_defs(focus_rid, peer_ids)
    crop_rows: list[dict[str, Any]] = []
    for crop in sorted(crop_names, key=lambda c: c.casefold()):
        cw = cropwise_by_crop.get(crop)
        if cw is None:
            for k, v in cropwise_by_crop.items():
                if _norm_crop(k) == _norm_crop(crop):
                    cw = v
                    break
        values = _values_for_crop(crop, focus_rid, peer_ids, reports, group, cw)
        compare_vals = {k: v for k, v in values.items() if v is not None}
        max_diff, status = _spread(compare_vals)
        if max(abs(v or 0) for v in values.values() if v is not None) < 0.01 and not compare_vals:
            continue
        crop_rows.append({
            "crop": crop,
            "values": values,
            "max_diff_ha": max_diff,
            "status": status,
        })

    firm_names: set[str] = set()
    for rid in peer_ids:
        op = _module_op(reports, rid, group)
        firm_names.update(str(f) for f in (op.get("area_today_by_firm") or {}).keys())
    file_firms = ((result.get("file_firms") or {}).get(group)) or {}
    firm_names.update(str(f) for f in file_firms.keys())
    for rid in peer_ids:
        peer_firms = ((reports.get(rid) or {}).get("file_firms") or {}).get(group) or {}
        firm_names.update(str(f) for f in peer_firms.keys())
    for cc in _crop_comparisons_for_group(result, group):
        for fr in cc.get("firms") or []:
            if fr.get("firm"):
                firm_names.add(str(fr["firm"]))

    cropwise_by_firm_total: dict[str, float] = {}
    for _crop, by_f in cropwise_firms.items():
        for firm, ha in by_f.items():
            cropwise_by_firm_total[firm] = round(cropwise_by_firm_total.get(firm, 0.0) + ha, 2)
    # Итог по агрофирмам — из сверки этой операции. Общий comparison сводки
    # 6/9 — сумма вспашки с глубокорыхлением или всего опрыскивания: его нельзя
    # подставлять в каждую секцию.
    oc = (result.get("operation_comparisons") or {}).get(group) or {}
    op_bf = (oc.get("comparison") or {}).get("cropwise_by_firm")
    authoritative_cw = isinstance(op_bf, dict) and bool(op_bf)
    if authoritative_cw:
        cropwise_by_firm_total = {
            str(firm): round(float(ha), 2)
            for firm, ha in op_bf.items()
            if ha is not None
        }
    else:
        from proverka.reports_config import REPORTS

        spec = REPORTS.get(focus_rid)
        multi = bool(spec and len(spec.work_groups or ()) > 1)
        cw_bf = {} if multi else (result.get("comparison") or {}).get("cropwise_by_firm") or {}
        if isinstance(cw_bf, dict) and cw_bf:
            authoritative_cw = True
            cropwise_by_firm_total = {
                str(firm): round(float(ha), 2)
                for firm, ha in cw_bf.items()
                if ha is not None
            }

    firm_rows: list[dict[str, Any]] = []
    for firm in sorted(firm_names, key=lambda f: f.casefold()):
        if firm in cropwise_by_firm_total:
            cw_f: float | None = cropwise_by_firm_total[firm]
        elif authoritative_cw:
            cw_f = 0.0
        else:
            cw_f = None
        values = _values_for_firm(firm, focus_rid, peer_ids, reports, group, cw_f)
        compare_vals = {k: v for k, v in values.items() if v is not None}
        max_diff, status = _spread(compare_vals)
        if max(abs(v or 0) for v in values.values() if v is not None) < 0.01:
            continue
        firm_rows.append({
            "firm": firm,
            "values": values,
            "max_diff_ha": max_diff,
            "status": status,
        })

    if not crop_rows and not firm_rows:
        return None

    op_total = round(float(focus_op.get("total_ha") or 0), 2)
    return {
        "group_name": group,
        "display_name": display_group_name(group),
        "peer_ids": peer_ids,
        "columns": columns,
        "operation_total_ha": op_total,
        "crops": crop_rows,
        "firms": firm_rows,
    }


def build_focus_report_view(
    focus_rid: str,
    batch: dict[str, Any],
) -> dict[str, Any] | None:
    from proverka.reports_config import REPORTS

    spec = REPORTS.get(focus_rid)
    if not spec:
        return None
    groups = _work_groups_for_focus(focus_rid, batch, spec)
    if not groups:
        return None

    sections: list[dict[str, Any]] = []
    for group in groups:
        sec = _build_operation_section(focus_rid, batch, group)
        if sec:
            sections.append(sec)

    if not sections:
        return None

    base = {
        "report_id": focus_rid,
        "title": spec.title,
    }
    if len(sections) == 1:
        sec = sections[0]
        return {
            **base,
            **sec,
            "sections": sections,
        }
    return {
        **base,
        "sections": sections,
        **sections[0],
    }


def _sort_report_id(rid: str) -> tuple:
    try:
        return (0, int(rid), rid)
    except ValueError:
        return (1, 0, rid)


def build_report_views(
    batch: dict[str, Any],
    report_date: Any,
    report_ids: list[str] | None = None,
) -> dict[str, Any]:
    """report_id → карточка сводки для каждой выбранной сводки из proverka REPORTS."""
    _ = report_date
    from proverka.reports_config import REPORTS

    wanted = {str(x) for x in (report_ids or [])}
    out: dict[str, Any] = {}
    for rid in sorted((wanted & set(REPORTS.keys())), key=_sort_report_id):
        try:
            view = build_focus_report_view(rid, batch)
        except Exception:
            continue
        if view and (view.get("crops") or view.get("firms") or view.get("sections")):
            out[rid] = view
    return out
