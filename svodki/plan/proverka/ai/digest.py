"""Минимальный агрегированный digest для LLM (без полного Excel/batch)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from proverka.summary import CROPWISE_VIOLATION_STATUSES, build_rows, compute_violations
from proverka.task_errors import collect_task_errors_from_batch

_MAX_TASK_ERRORS = int(os.environ.get("PROVERKA_AI_MAX_TASK_ERRORS", "20"))


def _is_cropwise_issue(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "")
    if status in CROPWISE_VIOLATION_STATUSES or status.startswith("UI season "):
        return True
    return False


def _is_meaningful_cropwise_issue(row: dict[str, Any]) -> bool:
    """Исключаем blank/UI error при 0/0 — нет операций за смену."""
    if not _is_cropwise_issue(row):
        return False
    status = str(row.get("status") or "")
    diff = abs(float(row.get("diff_ha") or 0))
    sv = abs(float(row.get("svodka_ha") or 0))
    cw = abs(float(row.get("cropwise_ha") or 0))
    if diff >= 0.01:
        return True
    if status in ("UI degraded", "DIFF") or status.startswith("UI season "):
        return sv >= 0.01 or cw >= 0.01
    if status in ("blank", "UI error", "season?"):
        return False
    return True


def _cross_check_status_by_op(batch: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    cross_rows = (batch.get("cross_check") or {}).get("operations") or []
    for row in cross_rows:
        gn = str(row.get("group_name") or "")
        if gn:
            out[gn] = str(row.get("status") or "")
    return out


def _local_vs_canonical(batch: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cross_ok = _cross_check_status_by_op(batch)
    reports = batch.get("reports") or {}
    for rid in ("1", "2"):
        res = reports.get(rid) or {}
        for gn, oc in sorted((res.get("operation_comparisons") or {}).items()):
            if cross_ok.get(gn) == "OK":
                continue
            diff = oc.get("svodka_local_diff_ha")
            if diff is None:
                diff = oc.get("svodka_r2_diff_ha")
            if diff is None or abs(float(diff)) < 0.01:
                continue
            local_op = oc.get("svodka_op_local") or oc.get("svodka_op_r2") or {}
            canon_op = oc.get("svodka_op") or {}
            out.append({
                "report_id": rid,
                "operation": gn,
                "local_ha": float(local_op.get("total_ha") or 0),
                "canon_ha": float(canon_op.get("total_ha") or 0),
                "diff_ha": float(diff),
                "canon_report": oc.get("canonical_report") or oc.get("svodka_source_report"),
            })
    return out


def _task_errors_top(batch: dict[str, Any]) -> list[dict[str, Any]]:
    raw = collect_task_errors_from_batch(batch)
    raw.sort(key=lambda x: -abs(float(x.get("diff_ha") or 0)))
    out: list[dict[str, Any]] = []
    for err in raw[:_MAX_TASK_ERRORS]:
        out.append({
            "work": err.get("work"),
            "report_id": err.get("report_id"),
            "gosnumber": err.get("gosnumber"),
            "task_id": err.get("task_id"),
            "machine_id": err.get("machine_id"),
            "svodka_ha": err.get("svodka_ha"),
            "cropwise_ha": err.get("cropwise_ha"),
            "diff_ha": err.get("diff_ha"),
            "season": err.get("season"),
            "error_kind": err.get("error_kind"),
            "task_url": err.get("task_url"),
        })
    return out


def build_digest(batch: dict[str, Any]) -> dict[str, Any]:
    """Сжатый JSON для DeepSeek из reconcile batch."""
    report_date = batch.get("report_date") or ""
    summary_rows = build_rows(batch)
    cross_check = batch.get("cross_check") or {}
    cross_rows = cross_check.get("operations") or batch.get("cross_check_rows") or []

    cross_diffs: list[dict[str, Any]] = []
    for row in cross_rows:
        if row.get("status") != "DIFF":
            continue
        cross_diffs.append({
            "operation": row.get("group_name"),
            "title": row.get("title"),
            "peer_reports": row.get("active_reports") or row.get("peer_reports"),
            "totals": row.get("totals") or {},
            "max_diff_ha": row.get("max_diff_ha"),
            "firms": [
                {"firm": f.get("firm"), "values": f.get("values"), "max_diff_ha": f.get("max_diff_ha")}
                for f in (row.get("firms") or [])
                if (f.get("max_diff_ha") or 0) >= 0.01
            ][:5],
        })

    cropwise_issues: list[dict[str, Any]] = []
    for row in summary_rows:
        if not _is_meaningful_cropwise_issue(row):
            continue
        cropwise_issues.append({
            "report_id": row.get("id"),
            "title": row.get("title"),
            "svodka_ha": row.get("svodka_ha"),
            "cropwise_ha": row.get("cropwise_ha"),
            "diff_ha": row.get("diff_ha"),
            "status": row.get("status"),
            "season": row.get("season"),
            "operation": row.get("operation"),
        })

    violations = compute_violations(summary_rows, cross_rows)

    return {
        "report_date": report_date,
        "shift": "06:00–06:00 MSK",
        "compare_mode": batch.get("compare_mode"),
        "cross_check_diffs": cross_diffs,
        "cropwise_issues": cropwise_issues,
        "local_vs_canonical": _local_vs_canonical(batch),
        "task_errors_top": _task_errors_top(batch),
        "rule_findings": [],
        "violation_summary": {
            "violation_count": violations.get("violation_count", 0),
            "cropwise_violation_count": violations.get("cropwise_violation_count", 0),
            "cross_check_violation_count": violations.get("cross_check_violation_count", 0),
        },
    }


def load_batch_from_job_dir(job_dir: Path) -> dict[str, Any] | None:
    """Читает reconcile_all_{date}.json из каталога job."""
    if not job_dir.is_dir():
        return None
    for path in sorted(job_dir.glob("reconcile_all_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("reports"):
            return data
    return None
