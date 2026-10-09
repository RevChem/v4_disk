"""Сверка уже созданных сводок с work.json. Модули сводок заново не считаются."""
from __future__ import annotations

import time
from datetime import date
from typing import Any

# Красная точка у сводки: расхождение со Cropwise меньше 1 га ошибкой не считается.
CROPWISE_DIFF_MIN_HA = 1.0


def ha_matches(file_ha: float, cropwise_ha: float) -> bool:
    return abs(float(file_ha) - float(cropwise_ha)) < CROPWISE_DIFF_MIN_HA


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
    notes: list[str] = []
    if problems:
        notes.append(f"не все сезоны: {'; '.join(problems)}")
    if seasons is not None:
        loaded_years = {str(year) for year in by_year}
        missing = [str(year) for year in sorted(seasons, key=str) if str(year) not in loaded_years]
        if missing:
            notes.append(
                "сезоны редактора не вошли в сумму, их нет в проверке UI: " + ", ".join(missing)
            )
    note = f" ({'; '.join(notes)})" if notes else ""
    return round(total, 2), note


# Есть в файле, но не в единственной группе каталога: иначе один work.json смешает виды работ.
FILE_CHECK_EXTRA_GROUPS: dict[str, tuple[str, ...]] = {
    "5": ("4.1 Посев (овощи)",),
    "10": ("5.0 Уборка",),
}


def groups_for_file_check(report_id: str, work_groups: tuple[str, ...]) -> set[str]:
    """Операции этой сводки, а не общий набор сводок 1, 2, 6 и 9."""
    extra = set(FILE_CHECK_EXTRA_GROUPS.get(report_id) or ())
    if work_groups:
        return set(work_groups) | extra
    if report_id in {"1", "2"}:
        from proverka.operation_registry import OPERATION_PEERS

        return {name for name, peers in OPERATION_PEERS.items() if report_id in peers} | extra
    return extra


def _slots_for_report(report_id: str, packed: Any, work_groups: tuple[str, ...]) -> list[tuple[str, dict]]:
    if not isinstance(packed, dict):
        return []
    allowed = groups_for_file_check(report_id, work_groups)
    if isinstance(packed.get("by_year"), dict):
        # Один work.json на несколько групп смешал бы виды работ. Такие слоты
        # приходят отдельным словарём {имя группы: pack}, его разбирает цикл ниже.
        if len(work_groups) != 1:
            return []
        name = work_groups[0]
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


def _report_path(item: Any):
    xlsx = getattr(item, "xlsx_path", None)
    pdf = getattr(item, "pdf_path", None)
    if xlsx is not None and xlsx.is_file():
        return xlsx
    if pdf is not None and pdf.is_file():
        return pdf
    return None


def _loaded_bodies(slot: dict, seasons: set[str] | None) -> list[dict]:
    by_year = slot.get("by_year") if isinstance(slot, dict) else None
    if not isinstance(by_year, dict):
        return []
    wanted = None if seasons is None else {str(year) for year in seasons}
    bodies: list[dict] = []
    for year, year_slot in by_year.items():
        if wanted is not None and str(year) not in wanted:
            continue
        if not isinstance(year_slot, dict) or year_slot.get("load_status") != "loaded":
            continue
        body = year_slot.get("body")
        if isinstance(body, dict):
            bodies.append(body)
    return bodies


def cropwise_ruled_matrix(
    slot: dict,
    hints: tuple[str, ...] | list[str],
    seasons: set[str] | None,
    group: str,
    report_id: str,
) -> tuple[dict[str, dict[str, float]], bool, str | None]:
    """Матрица после правил, признак строк культур и группа, к которой правила применились.

    Пустая матрица при True значит, что правила выкинули все гектары.
    Группа None — сводка правила редактора не использует, итог остаётся из «Всего».
    """
    from plans import FIRM_ORDER
    from proverka.crop_compare import apply_editor_crop_rules, cropwise_area_by_firm_crop, editor_rules_for

    names = list(FIRM_ORDER)
    acc: dict[str, dict[str, float]] = {}
    for body in _loaded_bodies(slot, seasons):
        for crop, by_firm in cropwise_area_by_firm_crop(body, names, list(hints)).items():
            bucket = acc.setdefault(str(crop), {})
            for firm, ha in (by_firm or {}).items():
                bucket[str(firm)] = bucket.get(str(firm), 0.0) + float(ha or 0)
    rule_group, remap = editor_rules_for(report_id, group)
    return apply_editor_crop_rules(rule_group, acc, remap=remap), bool(acc), rule_group


def cropwise_firms_from_slot(
    slot: dict, hints: tuple[str, ...] | list[str], seasons: set[str] | None,
) -> dict[str, float]:
    """Агрофирмы из колонки «Всего», когда правила редактора к сводке не применяются."""
    from plans import FIRM_ORDER
    from proverka.cropwise_ui import parse_work_report_body

    names = list(FIRM_ORDER)
    totals: dict[str, float] = {}
    for body in _loaded_bodies(slot, seasons):
        parsed = parse_work_report_body(body, names, list(hints))
        for firm, ha in (parsed.get("fact_by_firm") or {}).items():
            totals[str(firm)] = totals.get(str(firm), 0.0) + float(ha or 0)
    return {firm: round(ha, 2) for firm, ha in totals.items() if ha > 0}


def _matrix_total(matrix: dict[str, dict[str, float]]) -> float:
    return round(
        sum(float(ha or 0) for by_firm in matrix.values() for ha in by_firm.values()),
        2,
    )


def _matrix_firms(matrix: dict[str, dict[str, float]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for by_firm in matrix.values():
        for firm, ha in by_firm.items():
            totals[str(firm)] = totals.get(str(firm), 0.0) + float(ha or 0)
    return {firm: round(ha, 2) for firm, ha in totals.items() if ha > 0}


def _matrix_crops(matrix: dict[str, dict[str, float]]) -> dict[str, float]:
    return {
        crop: round(sum(float(ha or 0) for ha in by_firm.values()), 2)
        for crop, by_firm in matrix.items()
        if sum(float(ha or 0) for ha in by_firm.values()) > 0
    }


def _append_part_diffs(
    add_row,
    lines: list[str],
    rid: str,
    title: str,
    group: str,
    note: str,
    file_map: dict,
    cropwise_map: dict,
    kind: str,
) -> int:
    """Расхождения по агрофирмам или культурам. Пустой файл не с чем сравнивать."""
    if not file_map:
        return 0
    found = 0
    for name in sorted(set(file_map) | set(cropwise_map)):
        file_ha = float(file_map.get(name) or 0)
        cropwise_ha = float(cropwise_map.get(name) or 0)
        if ha_matches(file_ha, cropwise_ha):
            continue
        label = f"{group} / {name}"
        lines.append(
            f"[{rid}] {label}: файл {file_ha:.2f} га, "
            f"Cropwise {cropwise_ha:.2f} га ({kind}){note}."
        )
        add_row(rid, title, label, "diff", file_ha, cropwise_ha, f"{kind}. {note}".strip())
        found += 1
    return found


def compare_generated_files_to_work_json(
    report_date: date, created: list,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Один проход work.json. Второй список — только строки с расхождением."""
    from app.adapters.outbound.proverka.service import _ensure_plan_path, session_cookie_from_env

    _ensure_plan_path()
    from report_platform import reload_editor_modules

    reload_editor_modules()
    cookie = session_cookie_from_env()
    if not cookie:
        return ["Cropwise: нет CROPWISE_SESSION_COOKIE, проверка пропущена."], []

    from proverka.file_values import crop_areas_from_path, firm_areas_from_path, operation_areas_from_path
    from proverka.report2_reconcile import _fact_hints_for_group
    from proverka.reports_config import REPORTS
    from proverka.runner import _prefetch_work_json
    from season_unified import task_season_years_set_for_group

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
        return ["Cropwise: среди созданных сводок нет сверки с work.json."], []

    started = time.monotonic()
    progress: list[str] = []
    prefetched, _ui = _prefetch_work_json(
        cookie, report_date, ids, progress.append, also_groups=FILE_CHECK_EXTRA_GROUPS,
    )

    lines: list[str] = list(progress)
    rows: list[dict[str, Any]] = []
    checked = 0
    diffs = 0

    def add_row(
        rid: str,
        title: str,
        group: str,
        status: str,
        file_ha: float | None,
        cropwise_ha: float | None,
        note: str,
        source: str = "cropwise",
    ) -> None:
        delta = None
        if file_ha is not None and cropwise_ha is not None:
            delta = round(abs(float(file_ha) - float(cropwise_ha)), 2)
        rows.append({
            "report_id": rid,
            "title": title,
            "group": group,
            "status": status,
            "file_ha": None if file_ha is None else round(float(file_ha), 2),
            "cropwise_ha": None if cropwise_ha is None else round(float(cropwise_ha), 2),
            "delta": delta,
            "note": note,
            "source": source,
        })

    for rid in ids:
        spec = REPORTS[rid]
        item = files[rid]
        title = str(getattr(item, "title", "") or "")
        path = _report_path(item)
        if path is None:
            lines.append(f"[{rid}] Cropwise: файл сводки не найден.")
            add_row(rid, title, "", "unread", None, None, "файл сводки не найден")
            continue
        try:
            file_areas = operation_areas_from_path(path, rid)
            file_firms = firm_areas_from_path(path, rid)
            file_crops = crop_areas_from_path(path, rid)
        except Exception as exc:
            lines.append(f"[{rid}] Cropwise: файл не прочитан ({exc}).")
            add_row(rid, title, "", "unread", None, None, f"файл не прочитан ({exc})")
            continue
        slots = _slots_for_report(rid, prefetched.get(rid), tuple(spec.work_groups or ()))
        if not slots:
            lines.append(f"[{rid}] Cropwise: work.json для сводки не загружен.")
            continue
        for group, slot in slots:
            hints = _fact_hints_for_group(group) or tuple(spec.fact_column_hints)
            seasons = task_season_years_set_for_group(group, report_date)
            cropwise_ha, note = work_json_total(slot, hints, seasons)
            matrix, has_crops, rule_group = cropwise_ruled_matrix(slot, hints, seasons, group, rid)
            if rule_group is not None and has_crops:
                cropwise_ha = _matrix_total(matrix)
            file_ha = file_areas.get(group)
            if cropwise_ha is None:
                lines.append(f"[{rid}] {group}: {note}.")
                add_row(rid, title, group, "unread", None, None, note)
                continue
            if file_ha is None:
                if cropwise_ha >= CROPWISE_DIFF_MIN_HA:
                    lines.append(
                        f"[{rid}] {group}: в файле нет этой операции, "
                        f"Cropwise {cropwise_ha:.2f} га{note}."
                    )
                    add_row(rid, title, group, "missing", None, cropwise_ha, note)
                continue
            checked += 1
            if not ha_matches(float(file_ha), cropwise_ha):
                diffs += 1
                delta = abs(float(file_ha) - cropwise_ha)
                lines.append(
                    f"[{rid}] {group}: файл {float(file_ha):.2f} га, "
                    f"Cropwise {cropwise_ha:.2f} га, Δ {delta:.2f}{note}."
                )
                add_row(rid, title, group, "diff", float(file_ha), cropwise_ha, note)
            if rule_group is not None and has_crops:
                firm_cw = _matrix_firms(matrix)
            else:
                firm_cw = cropwise_firms_from_slot(slot, hints, seasons)
            crop_cw = _matrix_crops(matrix) if has_crops else {}
            diffs += _append_part_diffs(
                add_row, lines, rid, title, group, note,
                file_firms.get(group) or {},
                firm_cw,
                "агрофирма",
            )
            diffs += _append_part_diffs(
                add_row, lines, rid, title, group, note,
                file_crops.get(group) or {},
                crop_cw,
                "культура",
            )
    from proverka.file_consistency import build_sheet_checks

    for row in build_sheet_checks(report_date, ids):
        if row.get("status") in (None, "OK"):
            continue
        sheet_file = row.get("total_ha")
        sheet_other = row.get("firms_ha") if row.get("firms_ha") is not None else row.get("crops_ha")
        if sheet_file is not None and sheet_other is not None:
            if abs(float(sheet_file) - float(sheet_other)) < CROPWISE_DIFF_MIN_HA:
                continue
        issues = row.get("issues") or []
        note = "; ".join(
            str(item.get("kind") or item) if isinstance(item, dict) else str(item)
            for item in issues
        ) or str(row.get("status"))
        rid = str(row.get("report_id") or "")
        group = str(row.get("display_name") or row.get("group_name") or "")
        lines.append(f"[{rid}] {group}: листы не сходятся ({note}).")
        add_row(
            rid,
            str(REPORTS[rid].title) if rid in REPORTS else "",
            group,
            "diff" if row.get("status") == "DIFF" else "unread",
            row.get("total_ha"),
            row.get("firms_ha") if row.get("firms_ha") is not None else row.get("crops_ha"),
            f"листы одного файла, не Cropwise. {note}".strip(),
            source="sheets",
        )
        diffs += 1
    elapsed = time.monotonic() - started
    lines.append(
        f"Cropwise work.json: операций {checked}, расхождений {diffs}, {elapsed:.0f} с."
    )
    return lines, rows
