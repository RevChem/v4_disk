"""Сверка количества задействованных машин (комбайнов и т.п.) по операциям
между сводками 1, 2, 10, 11, …

Полностью независим от operation_compare.py (сверка по гектарам): свои
функции, своя форма результата, свой статус OK/DIFF. Единственное, что
переиспользуется, — operation_registry.OPERATION_PEERS (+ override для
5.0 Уборка → peer_reports_for_machine_group): справочник «какая сводка кому
пара по какой операции», без логики сверки по гектарам.

ИСТОЧНИК: числа машин берутся из готовых Excel сводок (file_machines.py),
запускает сверку machine_runner.run_machine_proverka_batch.
"""
from __future__ import annotations

from typing import Any

from proverka.operation_registry import (
    OPERATION_PEERS,
    display_group_name,
    peer_reports_for_machine_group,
)

# Начиная с какой разницы в штуках считаем операцию расходящейся.
# В отличие от гектаров (порог 0.01, т.е. "почти любая разница"), здесь это
# всегда целые единицы техники, поэтому порог = 1.
DIFF_THRESHOLD_MACHINES = 1


def _machines_from_operation(op: dict[str, Any]) -> dict[str, int]:
    """Кол-во машин по агрофирмам для одной операции одной сводки."""
    raw = op.get("machines_today_by_firm") or {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v or 0)
        except (TypeError, ValueError):
            out[str(k)] = 0
    return out


def operations_from_svodka(svodka: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Операции сводки в форме {group_name: {...}}.

    Дублирует одноимённую функцию из operation_compare.py намеренно —
    модули должны оставаться независимыми и не импортировать друг друга.
    """
    ops = svodka.get("operations")
    if isinstance(ops, dict) and ops:
        return ops
    groups = svodka.get("work_groups") or []
    if not groups:
        return {}
    gn = groups[0]
    return {
        gn: {
            "machines_today_by_firm": svodka.get("machines_today_by_firm") or {},
        }
    }


def _operation_total_machines(op: dict[str, Any] | None) -> int:
    if not op:
        return 0
    if op.get("total_machines") is not None:
        try:
            return int(op.get("total_machines") or 0)
        except (TypeError, ValueError):
            return 0
    machines = op.get("machines_today_by_firm") or {}
    return sum(int(v or 0) for v in machines.values())


def machines_operation_for_cross_check(
    report_result: dict[str, Any],
    rid: str,
    group_name: str,
) -> dict[str, Any] | None:
    """Факт по машинам для операции для сверки 1↔2↔11/14 (5.0 Уборка).

    Упрощённая версия operation_for_cross_check из operation_compare.py:
    здесь нет темы canon/svodka_op_local (это относилось к сверке с
    Cropwise UI, которой у машинной проверки нет вообще — она только
    "сводки между собой"), поэтому просто достаём операцию свода.
    """
    sv = report_result.get("svodka") or {}
    return operations_from_svodka(sv).get(group_name)


def build_machine_cross_check(batch: dict[str, Any]) -> dict[str, Any]:
    """Аналог build_cross_check(), но по количеству машин, а не по га.

    Не переиспользует build_cross_check и ничего из его внутренних структур
    не разделяет — рассчитан на отдельный вызов, отдельный job, отдельный
    JSON на выходе.
    """
    reports = batch.get("reports") or {}
    present = set(reports.keys())
    rows: list[dict[str, Any]] = []
    ok = diff = partial = 0

    for group_name in OPERATION_PEERS:
        peer_ids = [rid for rid in peer_reports_for_machine_group(group_name) if rid in present]
        if len(peer_ids) < 2:
            continue

        totals: dict[str, int] = {}
        by_firm: dict[str, dict[str, int]] = {}
        grafted_from: dict[str, str] = {}
        for rid in peer_ids:
            op = machines_operation_for_cross_check(reports[rid], rid, group_name)
            if not op:
                continue
            machines = _machines_from_operation(op)
            if not machines and _operation_total_machines(op) == 0:
                # нет факта по машинам в этой сводке для этой операции —
                # не считаем её "активной" по этой операции
                continue
            totals[rid] = sum(machines.values())
            for firm, cnt in machines.items():
                by_firm.setdefault(firm, {})[rid] = cnt
            donor_rid = op.get("_grafted_from_report_id")
            if donor_rid:
                grafted_from[rid] = str(donor_rid)

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
                "max_diff_machines": None,
                "note": "данные по машинам не во всех сводках",
            })
            continue

        total_vals = [totals[r] for r in active_peers]
        max_diff = max(total_vals) - min(total_vals)
        firm_rows: list[dict[str, Any]] = []
        for firm, vals in sorted(by_firm.items()):
            if len(vals) < 2:
                continue
            vlist = list(vals.values())
            fd = max(vlist) - min(vlist)
            if fd >= DIFF_THRESHOLD_MACHINES or any(vlist):
                firm_rows.append({"firm": firm, "values": vals, "max_diff_machines": fd})

        status = "OK" if max_diff < DIFF_THRESHOLD_MACHINES else "DIFF"
        if status == "OK":
            ok += 1
        else:
            diff += 1

        canon = next((r for r in active_peers if r not in ("1", "2")), active_peers[-1])
        rows.append({
            "group_name": group_name,
            "display_name": display_group_name(group_name),
            "canonical_report": canon,
            "peer_reports": peer_ids,
            "active_reports": active_peers,
            "totals": totals,
            "max_diff_machines": max_diff,
            "status": status,
            "firms": firm_rows,
            # {report_id: report_id донора}; при чтении из Excel не заполняется.
            "grafted_from": grafted_from,
        })

    return {
        "operations": rows,
        "summary": {"ok": ok, "diff": diff, "partial": partial, "total": len(rows)},
    }
