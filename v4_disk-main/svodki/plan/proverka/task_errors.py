"""
Task-level расхождения сводка vs Cropwise для листа «Ошибки» в Excel.

Использует уже загруженные machine_tasks + mtfmi (ShiftWindowCache) и строки #task_id
из work.json — без дополнительных запросов work.json.
"""
from __future__ import annotations

import os
import re
from typing import Any

from proverka.cropwise_ui import aggregate_ui_task_rows
from proverka.season_cropwise import ShiftWindowCache, _area_in_shift, _parse_dt

TASK_URL = "https://operations.cropwise.com/machines/{machine_id}/tasks/{task_id}"
_GOS_FROM_LABEL_RE = re.compile(
    r"#\d+\s+.*?\b(\d{3,4}[A-ZА-Я]{1,4}(?:\d{1,4})?[A-ZА-Я]{0,3})\b",
)
_MIN_TASK_DIFF_HA = 0.01

_machines_by_id: dict[int, dict] | None = None


def task_errors_enabled() -> bool:
    return os.environ.get("PROVERKA_TASK_ERRORS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def clear_machines_cache() -> None:
    global _machines_by_id
    _machines_by_id = None


def get_machines_by_id(*, refresh: bool = False) -> dict[int, dict]:
    global _machines_by_id
    if refresh or _machines_by_id is None:
        from proverka.season_cropwise import _get_all

        _machines_by_id = {
            int(m["id"]): m for m in _get_all("machines") if m.get("id") is not None
        }
    return _machines_by_id


def gosnumber_for(machine_id: int | None, label: str = "") -> str:
    if machine_id is not None:
        m = get_machines_by_id().get(int(machine_id)) or {}
        reg = (m.get("registration_number") or m.get("reg_number") or "").strip()
        if reg:
            return reg
        name = (m.get("name") or "").strip()
        if name:
            return name
    if label:
        m = _GOS_FROM_LABEL_RE.search(label)
        if m:
            return m.group(1)
    return ""


def task_url(machine_id: int | None, task_id: int | None) -> str:
    if machine_id is None or task_id is None:
        return ""
    return TASK_URL.format(machine_id=int(machine_id), task_id=int(task_id))


def comparison_is_violation(comparison: dict[str, Any], *, work_ok: bool) -> bool:
    """Совпадает с логикой «Ошибки» в summary (не OK)."""
    if not work_ok:
        return True
    if bool(comparison.get("ui_degraded")):
        return True
    diff = abs(float(comparison.get("total_diff_ha") or 0))
    if diff < _MIN_TASK_DIFF_HA:
        return False
    if diff <= 5.0:
        return False
    return True


def _cropwise_ha_for_task(
    *,
    season: str,
    ui_session: int | None,
    api_ha: float,
    ui_ha: float,
    is_orphan: bool,
) -> float:
    if is_orphan:
        return round(ui_ha, 2)
    if ui_session is not None and season == str(ui_session):
        return round(ui_ha, 2)
    return round(api_ha, 2)


def _orphans_map(parsed: dict[str, Any], ui_verified: dict[str, Any]) -> dict[int, float]:
    raw = parsed.get("ui_orphan_tasks")
    if raw is None:
        raw = ui_verified.get("orphan_task_ids")
    if isinstance(raw, dict):
        return {int(k): float(v) for k, v in raw.items()}
    return {}


def _task_meta(
    task_by_id: dict[int, dict],
    ui_row: dict[str, Any] | None,
    tid: int,
) -> tuple[int | None, str, str]:
    t = task_by_id.get(tid)
    machine_id = int(t["machine_id"]) if t and t.get("machine_id") is not None else None
    label = str((ui_row or {}).get("label") or "")
    season = str(t.get("season") or "none") if t else "?"
    return machine_id, label, season


def compute_task_errors(
    *,
    season_cropwise: dict[str, Any] | None,
    parsed: dict[str, Any],
    wt_ids: list[int],
    t0: str,
    t1: str,
    task_season_years: list[int],
    shift_cache: ShiftWindowCache | None,
    ui_session: int | None,
    report_id: str,
    work_title: str,
) -> list[dict[str, Any]]:
    """Строки с diff ≥ 0.01 га на уровне machine_task."""
    day_start = _parse_dt(t0)
    cutoff = _parse_dt(t1)
    if day_start is None or cutoff is None:
        return []

    if shift_cache is None:
        shift_cache = ShiftWindowCache.build(t0, t1, wt_ids)

    wt_set = {int(x) for x in wt_ids}
    year_filter = {str(y) for y in task_season_years} if task_season_years else None

    valid_task_ids: set[int] = set()
    if season_cropwise:
        valid_task_ids = set(season_cropwise.get("valid_task_ids") or ())
    if ui_session is None and season_cropwise:
        ui_session = season_cropwise.get("ui_session_seasonValue")

    detail_rows = parsed.get("detail_rows") or []
    ui_verified = aggregate_ui_task_rows(
        detail_rows, valid_task_ids if valid_task_ids else None,
    )
    ui_by_task: dict[int, dict[str, Any]] = {}
    for row in ui_verified.get("task_rows") or []:
        tid = row.get("task_id")
        if tid is not None:
            ui_by_task[int(tid)] = row

    orphans = _orphans_map(parsed, ui_verified)

    task_by_id: dict[int, dict] = {}
    for t in shift_cache.all_tasks:
        tid = t.get("id")
        if tid is not None:
            task_by_id[int(tid)] = t

    candidate_ids: set[int] = set()
    api_ha_by_task: dict[int, float] = {}

    for t in shift_cache.all_tasks:
        if int(t.get("work_type_id") or 0) not in wt_set:
            continue
        tid = t.get("id")
        if tid is None:
            continue
        tid_i = int(tid)
        season = str(t.get("season") or "none")
        if year_filter and season not in year_filter:
            continue
        items = shift_cache.mtfmi_map.get(tid_i, [])
        api_ha = round(_area_in_shift(items, day_start, cutoff), 2)
        api_ha_by_task[tid_i] = api_ha
        if api_ha >= _MIN_TASK_DIFF_HA:
            candidate_ids.add(tid_i)
        elif ui_session and season == str(ui_session) and tid_i in ui_by_task:
            candidate_ids.add(tid_i)

    for tid, ha in orphans.items():
        if ha >= _MIN_TASK_DIFF_HA:
            candidate_ids.add(int(tid))

    for tid, ui_row in ui_by_task.items():
        if float(ui_row.get("area_ha") or 0) >= _MIN_TASK_DIFF_HA:
            candidate_ids.add(int(tid))

    errors: list[dict[str, Any]] = []
    for tid in sorted(candidate_ids):
        is_orphan = tid in orphans and tid not in valid_task_ids
        ui_row = ui_by_task.get(tid)
        ui_ha = float(ui_row.get("area_ha") or 0) if ui_row else 0.0
        if is_orphan:
            ui_ha = max(ui_ha, orphans.get(tid, 0.0))

        machine_id, label, season = _task_meta(task_by_id, ui_row, tid)
        if year_filter and season != "?" and season not in year_filter and not is_orphan:
            continue

        api_ha = api_ha_by_task.get(tid, 0.0)
        svodka_ha = api_ha
        cropwise_ha = _cropwise_ha_for_task(
            season=season,
            ui_session=ui_session,
            api_ha=api_ha,
            ui_ha=ui_ha,
            is_orphan=is_orphan,
        )
        diff = round(svodka_ha - cropwise_ha, 2)
        if abs(diff) < _MIN_TASK_DIFF_HA:
            continue

        if diff > _MIN_TASK_DIFF_HA:
            kind = "missing_in_cropwise"
        elif diff < -_MIN_TASK_DIFF_HA:
            kind = "extra_in_cropwise"
        elif is_orphan:
            kind = "ui_orphan"
        else:
            kind = "area_mismatch"

        errors.append({
            "work": work_title,
            "report_id": report_id,
            "svodka_ha": svodka_ha,
            "cropwise_ha": cropwise_ha,
            "diff_ha": diff,
            "season": season,
            "gosnumber": gosnumber_for(machine_id, label),
            "task_id": tid,
            "machine_id": machine_id,
            "task_url": task_url(machine_id, tid),
            "error_kind": kind,
            "label": label or f"#{tid}",
        })

    errors.sort(key=lambda x: (-abs(float(x.get("diff_ha") or 0)), x.get("task_id") or 0))
    return errors


def attach_task_errors(
    result: dict[str, Any],
    *,
    report_id: str,
    work_title: str,
    wt_ids: list[int],
    t0: str,
    t1: str,
    task_season_years: list[int],
    shift_cache: ShiftWindowCache | None,
    season_cropwise: dict[str, Any] | None = None,
) -> None:
    """Добавляет result['task_errors'] при нарушении сверки."""
    if not task_errors_enabled():
        return
    comparison = result.get("comparison") or {}
    work = result.get("cropwise_work_report") or {}
    if not comparison_is_violation(comparison, work_ok=bool(work.get("ok"))):
        return

    parsed = result.get("cropwise_parsed") or {}
    ui_session = comparison.get("ui_session_seasonValue") or work.get("ui_session_seasonValue")
    if season_cropwise is None:
        season_cropwise = {}

    errors = compute_task_errors(
        season_cropwise=season_cropwise,
        parsed=parsed,
        wt_ids=wt_ids,
        t0=t0,
        t1=t1,
        task_season_years=task_season_years,
        shift_cache=shift_cache,
        ui_session=int(ui_session) if ui_session is not None else None,
        report_id=report_id,
        work_title=work_title,
    )
    if errors:
        result["task_errors"] = errors


def collect_task_errors_from_batch(batch: dict[str, Any]) -> list[dict[str, Any]]:
    """Все task_errors из batch JSON (сводки 1–11), без дубликатов и нулевых diff."""
    from proverka.reports_config import REPORTS, list_report_ids
    from proverka.summary import per_op_row_title

    seen: set[tuple[str, str, int]] = set()
    rows: list[dict[str, Any]] = []
    reports = batch.get("reports") or {}
    for rid in list_report_ids():
        res = reports.get(rid)
        if not res:
            continue
        spec = REPORTS.get(rid)
        if spec and spec.cropwise_per_operation:
            for gn, oc in sorted((res.get("operation_comparisons") or {}).items()):
                for err in oc.get("task_errors") or []:
                    if abs(float(err.get("diff_ha") or 0)) < _MIN_TASK_DIFF_HA:
                        continue
                    row = dict(err)
                    if not row.get("work"):
                        row["work"] = per_op_row_title(rid, gn)
                    if not row.get("report_id"):
                        row["report_id"] = rid
                    key = (str(row.get("work")), str(row.get("report_id")), int(row.get("task_id") or 0))
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(row)
            continue
        title = spec.title if spec else rid
        for err in res.get("task_errors") or []:
            if abs(float(err.get("diff_ha") or 0)) < _MIN_TASK_DIFF_HA:
                continue
            row = dict(err)
            if not row.get("work"):
                row["work"] = title
            if not row.get("report_id"):
                row["report_id"] = rid
            key = (str(row.get("work")), str(row.get("report_id")), int(row.get("task_id") or 0))
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    rows.sort(key=lambda x: (-abs(float(x.get("diff_ha") or 0)), str(x.get("report_id")), x.get("task_id") or 0))
    return rows
