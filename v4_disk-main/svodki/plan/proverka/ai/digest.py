"""Минимальный агрегированный digest для LLM (без полного Excel/batch)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from proverka.ha_thresholds import DIFF_MIN_HA
from proverka.summary import (
    CROPWISE_VIOLATION_STATUSES,
    build_rows,
    compute_violations,
    _violation_detail_cross,
    _violation_detail_cross_crop,
    _violation_detail_sheet,
)

from proverka.task_errors import collect_task_errors_from_batch

_MAX_TASK_ERRORS = int(os.environ.get("PROVERKA_AI_MAX_TASK_ERRORS", "60"))
_DIGEST_CHAR_LIMIT = int(os.environ.get("PROVERKA_AI_DIGEST_CHAR_LIMIT", "360000"))


def digest_char_limit() -> int:
    return _DIGEST_CHAR_LIMIT


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


def _cross_check_crop_diffs(cross_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in cross_rows:
        op_name = row.get("display_name") or row.get("group_name")
        for crop_row in row.get("crops") or []:
            if crop_row.get("status") != "DIFF" and (crop_row.get("max_diff_ha") or 0) < DIFF_MIN_HA:
                continue
            out.append({
                "operation": op_name,
                "group_name": row.get("group_name"),
                "crop": crop_row.get("crop"),
                "totals": crop_row.get("totals") or {},
                "cropwise_ha": crop_row.get("cropwise_ha"),
                "max_diff_ha": crop_row.get("max_diff_ha"),
                "peer_reports": crop_row.get("active_reports") or row.get("active_reports") or [],
                "detail": _violation_detail_cross_crop(row, crop_row),
            })
    return out


def _sheet_check_diffs(batch: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for chk in batch.get("sheet_checks") or []:
        if chk.get("status") not in ("DIFF", "ERROR"):
            continue
        out.append({
            "report_id": chk.get("report_id"),
            "operation": chk.get("display_name") or chk.get("group_name"),
            "max_diff_ha": chk.get("max_diff_ha"),
            "issues": chk.get("issues") or [],
            "detail": _violation_detail_sheet(chk),
        })
    return out


def _peer_module_diffs(focus_rid: str, crop: str, values: dict[str, Any], peer_ids: list[str]) -> str | None:
    """Текст в духе UI «вкладка N сводка»: расхождение культуры с peer-сводками."""
    focus_ha = values.get(f"m_{focus_rid}")
    if focus_ha is None:
        return None
    try:
        focus_f = float(focus_ha)
    except (TypeError, ValueError):
        return None
    parts: list[str] = []
    for rid in sorted(set(peer_ids) | {focus_rid}, key=lambda x: (not str(x).isdigit(), int(x) if str(x).isdigit() else x)):
        if rid == focus_rid:
            continue
        peer_ha = values.get(f"m_{rid}")
        if peer_ha is None:
            continue
        diff = round(abs(focus_f - float(peer_ha)), 2)
        if diff < DIFF_MIN_HA:
            continue
        if not parts:
            parts.append(f"с {rid} сводкой на {diff} га")
        else:
            parts.append(f"на {diff} га со {rid} сводкой")
    if not parts:
        cw = values.get("cropwise")
        if cw is not None and abs(focus_f - float(cw)) >= DIFF_MIN_HA:
            d = round(abs(focus_f - float(cw)), 2)
            return (
                f'Расхождение в культуре «{crop}» со сводкой {focus_rid}: '
                f"расчёт {focus_f} га, Cropwise {float(cw)} га (Δ {d} га)."
            )
        return None
    if len(parts) == 1:
        return f'Расхождение в культуре «{crop}» {parts[0]}.'
    return f'Расхождение в культуре «{crop}» {parts[0]} и {" ".join(parts[1:])}.'


def _report_view_diffs(batch: dict[str, Any]) -> dict[str, Any]:
    """Сжатое содержимое вкладок «N сводка» из report_views (только DIFF)."""
    views = batch.get("report_views") or {}
    by_report: dict[str, Any] = {}
    for rid, view in views.items():
        sections = view.get("sections") or [view]
        lines: list[str] = []
        issues: list[dict[str, Any]] = []
        peer_ids = []
        for sec in sections:
            if not peer_ids:
                peer_ids = list(sec.get("peer_ids") or [])
            op = sec.get("display_name") or sec.get("group_name") or ""
            for crop_row in sec.get("crops") or []:
                if crop_row.get("status") != "DIFF" and (crop_row.get("max_diff_ha") or 0) < DIFF_MIN_HA:
                    continue
                values = crop_row.get("values") or {}
                sentence = _peer_module_diffs(str(rid), str(crop_row.get("crop") or ""), values, peer_ids)
                if sentence:
                    lines.append(sentence)
                issues.append({
                    "kind": "crop",
                    "operation": op,
                    "crop": crop_row.get("crop"),
                    "values": values,
                    "max_diff_ha": crop_row.get("max_diff_ha"),
                    "text": sentence,
                })
            for firm_row in sec.get("firms") or []:
                if firm_row.get("status") != "DIFF" and (firm_row.get("max_diff_ha") or 0) < DIFF_MIN_HA:
                    continue
                issues.append({
                    "kind": "firm",
                    "operation": op,
                    "firm": firm_row.get("firm"),
                    "values": firm_row.get("values"),
                    "max_diff_ha": firm_row.get("max_diff_ha"),
                })
                lines.append(
                    f'По агрофирме «{firm_row.get("firm")}» в операции «{op}»: '
                    f"max Δ {firm_row.get('max_diff_ha')} га между колонками."
                )
        if issues:
            by_report[str(rid)] = {
                "title": view.get("title"),
                "lines": lines,
                "issues": issues,
            }
    return by_report


def build_per_report_markdown(by_report: dict[str, Any]) -> str:
    if not by_report:
        return ""
    blocks: list[str] = []
    for rid in sorted(by_report.keys(), key=lambda x: (not str(x).isdigit(), int(x) if str(x).isdigit() else x)):
        block = by_report[rid]
        lines = block.get("lines") or []
        if not lines:
            continue
        blocks.append(f"### {rid} сводка.")
        blocks.append("")
        for i, line in enumerate(lines, 1):
            blocks.append(f"{i}. {line}")
        blocks.append("")
    return "\n".join(blocks).strip()


def build_digest(batch: dict[str, Any]) -> dict[str, Any]:
    """Сжатый JSON для DeepSeek из reconcile batch."""
    report_date = batch.get("report_date") or ""
    summary_rows = build_rows(batch)
    cross_check = batch.get("cross_check") or {}
    cross_rows = cross_check.get("operations") or batch.get("cross_check_rows") or []
    sheet_checks = batch.get("sheet_checks") or []

    cross_diffs: list[dict[str, Any]] = []
    for row in cross_rows:
        if row.get("status") != "DIFF":
            for crop_row in row.get("crops") or []:
                if crop_row.get("status") == "DIFF":
                    cross_diffs.append({
                        "operation": row.get("display_name") or row.get("group_name"),
                        "title": row.get("title"),
                        "crop": crop_row.get("crop"),
                        "peer_reports": crop_row.get("active_reports") or row.get("active_reports"),
                        "totals": crop_row.get("totals") or {},
                        "max_diff_ha": crop_row.get("max_diff_ha"),
                        "detail": _violation_detail_cross_crop(row, crop_row),
                    })
            continue
        cross_diffs.append({
            "operation": row.get("group_name"),
            "title": row.get("title"),
            "peer_reports": row.get("active_reports") or row.get("peer_reports"),
            "totals": row.get("totals") or {},
            "max_diff_ha": row.get("max_diff_ha"),
            "detail": _violation_detail_cross(row),
            "firms": [
                {"firm": f.get("firm"), "values": f.get("values"), "max_diff_ha": f.get("max_diff_ha")}
                for f in (row.get("firms") or [])
                if (f.get("max_diff_ha") or 0) >= 0.01
            ][:8],
        })

    cross_crop_diffs = _cross_check_crop_diffs(cross_rows)
    sheet_diffs = _sheet_check_diffs(batch)
    report_view_by_report = _report_view_diffs(batch)

    cropwise_issues: list[dict[str, Any]] = []
    for row in summary_rows:
        if not _is_meaningful_cropwise_issue(row):
            continue
        entry: dict[str, Any] = {
            "report_id": row.get("id"),
            "title": row.get("title"),
            "svodka_ha": row.get("svodka_ha"),
            "cropwise_ha": row.get("cropwise_ha"),
            "diff_ha": row.get("diff_ha"),
            "status": row.get("status"),
            "season": row.get("season"),
            "operation": row.get("operation"),
        }
        if row.get("is_crop_row"):
            entry["crop"] = row.get("crop")
        cropwise_issues.append(entry)

    violations = compute_violations(summary_rows, cross_rows, sheet_checks=sheet_checks)
    viol_list = violations.get("violations") or []

    per_report_md = build_per_report_markdown(report_view_by_report)

    return {
        "report_date": report_date,
        "shift": "06:00–06:00 MSK",
        "compare_mode": batch.get("compare_mode"),
        "cross_check_diffs": cross_diffs,
        "cross_check_crop_diffs": cross_crop_diffs,
        "sheet_check_diffs": sheet_diffs,
        "report_view_by_report": report_view_by_report,
        "per_report_markdown_hint": per_report_md,
        "violations": viol_list[:120],
        "cropwise_issues": cropwise_issues,
        "local_vs_canonical": _local_vs_canonical(batch),
        "task_errors_top": _task_errors_top(batch),
        "rule_findings": [],
        "violation_summary": {
            "violation_count": violations.get("violation_count", 0),
            "cropwise_violation_count": violations.get("cropwise_violation_count", 0),
            "cross_check_violation_count": violations.get("cross_check_violation_count", 0),
            "cross_check_crop_violation_count": violations.get("cross_check_crop_violation_count", 0),
            "sheet_consistency_violation_count": violations.get("sheet_consistency_violation_count", 0),
        },
    }


def _merge_status_into_batch(batch: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    """Старые job писали reconcile_all до cross_check/report_views — подмешиваем status.json."""
    status_path = job_dir / "status.json"
    if not status_path.is_file():
        return batch
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return batch
    if not isinstance(status, dict):
        return batch
    merged = dict(batch)
    for key in ("cross_check", "cross_check_rows", "sheet_checks", "report_views"):
        val = status.get(key)
        if val:
            merged[key] = val
    if status.get("report_date") and not merged.get("report_date"):
        merged["report_date"] = status["report_date"]
    return merged


def load_batch_from_job_dir(job_dir: Path) -> dict[str, Any] | None:
    """Читает reconcile_all_{date}.json из каталога job."""
    if not job_dir.is_dir():
        return None
    batch: dict[str, Any] | None = None
    for path in sorted(job_dir.glob("reconcile_all_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("reports"):
            batch = data
            break
    if not batch:
        return None
    return _merge_status_into_batch(batch, job_dir)
