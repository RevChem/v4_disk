"""Сверка факта за смену по операциям между сводками 1, 2, 3…"""
from __future__ import annotations

from typing import Any

from proverka.operation_registry import (
    OPERATION_PEERS,
    display_group_name,
    peer_reports_for_group,
)
from proverka.reports_config import REPORTS


def _area_from_operation(op: dict[str, Any]) -> dict[str, float]:
    raw = op.get("area_today_by_firm") or {}
    return {str(k): round(float(v or 0), 2) for k, v in raw.items()}


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
    ok = diff = partial = 0

    for group_name in OPERATION_PEERS:
        peer_ids = [rid for rid in peer_reports_for_group(group_name) if rid in present]
        if len(peer_ids) < 2:
            continue

        totals: dict[str, float] = {}
        by_firm: dict[str, dict[str, float]] = {}
        for rid in peer_ids:
            op = operation_for_cross_check(reports[rid], rid, group_name)
            if not op:
                continue
            area = _area_from_operation(op)
            totals[rid] = _operation_total_ha(op)
            for firm, ha in area.items():
                by_firm.setdefault(firm, {})[rid] = ha

        active_peers = [rid for rid in peer_ids if rid in totals]
        if len(active_peers) < 2:
            partial += 1
            rows.append({
                "group_name": group_name,
                "display_name": display_group_name(group_name),
                "peer_reports": peer_ids,
                "active_reports": active_peers,
                "totals": totals,
                "status": "partial",
                "max_diff_ha": None,
                "note": "данные не во всех сводках (сводка 1 без standalone?)",
            })
            continue

        total_vals = [totals[r] for r in active_peers]
        max_diff = round(max(total_vals) - min(total_vals), 2)
        firm_rows: list[dict[str, Any]] = []
        for firm, vals in sorted(by_firm.items()):
            if len(vals) < 2:
                continue
            vlist = list(vals.values())
            fd = round(max(vlist) - min(vlist), 2)
            if fd >= 0.01 or any(vlist):
                firm_rows.append({"firm": firm, "values": vals, "max_diff_ha": fd})

        status = "OK" if max_diff < 0.01 else "DIFF"
        if status == "OK":
            ok += 1
        else:
            diff += 1

        canon = next((r for r in active_peers if r not in ("1", "2")), active_peers[-1])
        spec = REPORTS.get(canon)
        rows.append({
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
        })

    return {
        "operations": rows,
        "summary": {"ok": ok, "diff": diff, "partial": partial, "total": len(rows)},
    }
