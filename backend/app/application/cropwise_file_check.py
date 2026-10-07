"""Сверка уже созданных сводок с work.json. Модули сводок заново не считаются."""
from __future__ import annotations

import time
from datetime import date
from typing import Any

_MATCH_ABS_HA = 5.0
_MATCH_REL = 0.02


def ha_matches(file_ha: float, cropwise_ha: float) -> bool:
    diff = abs(float(file_ha) - float(cropwise_ha))
    tol = max(_MATCH_ABS_HA, max(float(file_ha), float(cropwise_ha)) * _MATCH_REL)
    return diff <= tol


def work_json_total(
    packed: dict[str, Any],
    hints: tuple[str, ...] | list[str],
    seasons: set[str] | None = None,
) -> tuple[float | None, str]:
    """Сумма «Всего» по сезонам группы. None — ни один нужный сезон не прочитан.

    seasons — task_season_years_set_for_group. Чужой сезон в сумму не входит:
    иначе посев или вспашка за два года разойдутся со сводкой, которая считает один.
    """
    by_year = packed.get("by_year") if isinstance(packed, dict) else None
    if not isinstance(by_year, dict) or not by_year:
        return None, "work.json не загружен"
    total = 0.0
    seen = False
    problems: list[str] = []
    for year, slot in by_year.items():
        if seasons is not None and str(year) not in seasons:
            continue
        if not isinstance(slot, dict):
            continue
        status = str(slot.get("load_status") or "")
        if status == "loaded":
            from proverka.cropwise_ui import cropwise_total_ha

            seen = True
            total += cropwise_total_ha(slot, hints)
        elif status == "empty":
            seen = True
        else:
            err = str(slot.get("error") or status or "не загружен")
            problems.append(f"{year}: {err}")
    if not seen:
        return None, "; ".join(problems) or "work.json не загружен"
    note = f" (не все сезоны: {'; '.join(problems)})" if problems else ""
    return round(total, 2), note


def groups_for_file_check(report_id: str, work_groups: tuple[str, ...]) -> set[str]:
    """Операции этой сводки, а не общий набор сводок 1, 2, 6 и 9."""
    if work_groups:
        return set(work_groups)
    if report_id in {"1", "2"}:
        from proverka.operation_registry import OPERATION_PEERS

        return {name for name, peers in OPERATION_PEERS.items() if report_id in peers}
    return set()


def _slots_for_report(report_id: str, packed: Any, work_groups: tuple[str, ...]) -> list[tuple[str, dict]]:
    if not isinstance(packed, dict):
        return []
    allowed = groups_for_file_check(report_id, work_groups)
    if isinstance(packed.get("by_year"), dict):
        name = work_groups[0] if len(work_groups) == 1 else report_id
        if allowed and name not in allowed:
            return []
        return [(name, packed)]
    out: list[tuple[str, dict]] = []
    for name, slot in packed.items():
        if allowed and str(name) not in allowed:
            continue
        if isinstance(slot, dict) and isinstance(slot.get("by_year"), dict):
            out.append((str(name), slot))
    return out


def compare_generated_files_to_work_json(report_date: date, created: list) -> list[str]:
    """Один проход work.json по созданным сводкам, которые сверяются с Cropwise."""
    from app.adapters.outbound.proverka.service import _ensure_plan_path, session_cookie_from_env

    _ensure_plan_path()
    cookie = session_cookie_from_env()
    if not cookie:
        return ["Cropwise: нет CROPWISE_SESSION_COOKIE, проверка пропущена."]

    from proverka.file_values import operation_areas_from_path
    from proverka.report2_reconcile import _fact_hints_for_group
    from proverka.reports_config import REPORTS
    from proverka.runner import _prefetch_work_json

    files: dict[str, Any] = {}
    ids: list[str] = []
    for item in created:
        rid = str(getattr(item, "report_id", "") or "")
        spec = REPORTS.get(rid)
        if not spec or not (spec.cropwise_comparable or spec.cropwise_per_operation):
            continue
        ids.append(rid)
        files[rid] = item
    if not ids:
        return ["Cropwise: среди созданных сводок нет сверки с work.json."]

    started = time.monotonic()
    progress: list[str] = []
    prefetched, _ui = _prefetch_work_json(
        cookie, report_date, ids, progress.append,
    )

    lines: list[str] = list(progress)
    checked = 0
    diffs = 0
    for rid in ids:
        spec = REPORTS[rid]
        item = files[rid]
        path = getattr(item, "xlsx_path", None) or getattr(item, "pdf_path", None)
        if path is None or not path.is_file():
            lines.append(f"[{rid}] Cropwise: файл сводки не найден.")
            continue
        try:
            file_areas = operation_areas_from_path(path, rid)
        except Exception as exc:
            lines.append(f"[{rid}] Cropwise: файл не прочитан ({exc}).")
            continue
        slots = _slots_for_report(rid, prefetched.get(rid), tuple(spec.work_groups or ()))
        if not slots:
            lines.append(f"[{rid}] Cropwise: work.json для сводки не загружен.")
            continue
        for group, slot in slots:
            hints = _fact_hints_for_group(group) or tuple(spec.fact_column_hints)
            from season_unified import task_season_years_set_for_group

            seasons = task_season_years_set_for_group(group, report_date)
            cropwise_ha, note = work_json_total(slot, hints, seasons)
            file_ha = file_areas.get(group)
            if cropwise_ha is None:
                lines.append(f"[{rid}] {group}: {note}.")
                continue
            if file_ha is None:
                if cropwise_ha > _MATCH_ABS_HA:
                    lines.append(
                        f"[{rid}] {group}: в файле нет этой операции, "
                        f"Cropwise {cropwise_ha:.2f} га{note}."
                    )
                continue
            checked += 1
            if ha_matches(float(file_ha), cropwise_ha):
                continue
            diffs += 1
            delta = abs(float(file_ha) - cropwise_ha)
            lines.append(
                f"[{rid}] {group}: файл {float(file_ha):.2f} га, "
                f"Cropwise {cropwise_ha:.2f} га, Δ {delta:.2f}{note}."
            )
    elapsed = time.monotonic() - started
    lines.append(
        f"Cropwise work.json: операций {checked}, расхождений {diffs}, {elapsed:.0f} с."
    )
    return lines
