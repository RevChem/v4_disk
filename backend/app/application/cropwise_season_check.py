"""Сверка сезонных гектаров готовых сводок с work.json Cropwise за сезон.

Включается кнопкой «Cropwise за сезон». Работает после создания сводок, как проверка
за смену (cropwise_file_check): модули сводок заново не считаются.

Что сравнивается:
* сводка 2 — «Выполнено, га» по каждой операции: итог, агрофирмы, культуры;
* сводки 1 и 10 — «Факт общий, га» на листах «Общий намолот» (5.0 Уборка) и овощей.

Cropwise за сезон — work.json каждого сезона из редактора планов (окно с начала сезона
до конца смены даты отчёта). Для «5.0 Уборка» запрашивается и вид работ 84
«6.3 Скашивание с измельчением кукурузы»: скашивание идёт в «Кукуруза на силос»,
уборка кукурузы остаётся «на зерно» — так же, как считает сводка 1.

Красная точка у сводки — расхождение больше 10 га (порог суток — 1 га).
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any, Callable

from app.application.cropwise_file_check import (
    _crops_for_file_check,
    _matrix_crops,
    _matrix_firms,
    _matrix_total,
    cropwise_firms_from_slot,
    cropwise_ruled_matrix,
    work_json_total,
)

# Расхождение сезона больше этого значения — ошибка (красная точка).
CROPWISE_SEASON_DIFF_MIN_HA = 10.0

CORN_PLAIN = "Кукуруза"
CORN_GRAIN = "Кукуруза на зерно"
CORN_SILAGE = "Кукуруза на силос"

# Статусы слота work.json, при которых сезон считается прочитанным.
_READ_OK = ("loaded", "empty")


def season_over_limit(file_ha: float, cropwise_ha: float) -> bool:
    """Ошибка — только когда разница строго больше порога."""
    return abs(float(file_ha) - float(cropwise_ha)) > CROPWISE_SEASON_DIFF_MIN_HA


def align_corn(
    file_map: dict[str, float], cropwise_map: dict[str, float],
) -> tuple[dict[str, float], dict[str, float]]:
    """Кукуруза с обеих сторон в одной детализации.

    Файл делит кукурузу на силос и зерно — «Кукуруза» из Cropwise идёт в зерно,
    как в сводке 1 (скашивание — силос, всё остальное — зерно). Файл держит
    кукурузу одной строкой — силос и зерно Cropwise складываются в неё.
    """
    left = dict(file_map)
    right = dict(cropwise_map)
    if CORN_SILAGE in left or CORN_GRAIN in left:
        left.pop(CORN_PLAIN, None)
        plain = right.pop(CORN_PLAIN, 0.0)
        if plain:
            right[CORN_GRAIN] = round(right.get(CORN_GRAIN, 0.0) + plain, 2)
        return left, right
    parts = sum(right.pop(name, 0.0) for name in (CORN_SILAGE, CORN_GRAIN))
    if parts:
        right[CORN_PLAIN] = round(right.get(CORN_PLAIN, 0.0) + parts, 2)
    return left, right


def _xlsx_path(item: Any):
    xlsx = getattr(item, "xlsx_path", None)
    if xlsx is not None and xlsx.is_file():
        return xlsx
    return None


def _default_loader(cookie: str, prepared: list[dict[str, Any]]):
    from proverka.season_hectares import load_season_slots

    return load_season_slots(cookie, prepared)


def compare_generated_files_to_season(
    report_date: date,
    created: list,
    *,
    cookie: str | None = None,
    loader: Callable[[str, list[dict[str, Any]]], dict[str, dict[int, dict[str, Any]]]] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Один проход work.json по сезонам. Второй список — строки с расхождением или без проверки."""
    from app.adapters.outbound.proverka.service import _ensure_plan_path, session_cookie_from_env

    _ensure_plan_path()
    from report_platform import reload_editor_modules

    reload_editor_modules()
    cookie = session_cookie_from_env() if cookie is None else cookie
    if not cookie:
        return ["Cropwise сезон: нет CROPWISE_SESSION_COOKIE, проверка пропущена."], []

    from proverka.cropwise_ui import pack_loaded_work
    from proverka.season_file_values import SEASON_REPORT_IDS, season_areas_from_path
    from proverka.season_hectares import prepare_season_groups

    started = time.monotonic()
    lines: list[str] = []
    rows: list[dict[str, Any]] = []
    checked = 0
    diffs = 0

    def add_row(
        rid: str, title: str, group: str, status: str,
        file_ha: float | None, cropwise_ha: float | None, note: str,
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
            "source": "cropwise_season",
        })

    # Сезонные значения из файлов.
    file_data: dict[str, dict[str, dict[str, Any]]] = {}
    titles: dict[str, str] = {}
    for item in created:
        rid = str(getattr(item, "report_id", "") or "")
        if rid not in SEASON_REPORT_IDS:
            continue
        title = str(getattr(item, "title", "") or "")
        titles[rid] = title
        path = _xlsx_path(item)
        if path is None:
            lines.append(f"[{rid}] Cropwise сезон: нет Excel сводки, сезон не сверен.")
            continue
        try:
            data = season_areas_from_path(path, rid)
        except Exception as exc:
            lines.append(f"[{rid}] Cropwise сезон: файл не прочитан ({exc}).")
            add_row(rid, title, "", "unread", None, None, f"файл не прочитан ({exc})")
            continue
        if not data:
            lines.append(f"[{rid}] Cropwise сезон: в Excel нет сезонных колонок, сезон не сверен.")
            continue
        file_data[rid] = data
    if not file_data:
        lines.append("Cropwise сезон: среди созданных сводок нет сезонных данных для сверки.")
        return lines, rows

    groups = sorted({group for data in file_data.values() for group in data})
    try:
        prepared = prepare_season_groups(report_date, groups)
    except Exception as exc:
        lines.append(f"Cropwise сезон: сезоны редактора не определены ({exc}).")
        return lines, rows
    by_name = {item["group_name"]: item for item in prepared}
    try:
        slots = (loader or _default_loader)(cookie, prepared) if prepared else {}
    except Exception as exc:
        lines.append(f"Cropwise сезон: work.json не загружен ({exc}).")
        for rid, data in file_data.items():
            for group in data:
                if group in by_name:
                    add_row(rid, titles.get(rid, ""), group, "unread", None, None,
                            f"work.json сезона не загружен ({exc})")
        return lines, rows

    for rid, data in file_data.items():
        title = titles.get(rid, "")
        for group, season in data.items():
            item = by_name.get(group)
            if item is None:
                lines.append(f"[{rid}] {group}: в редакторе планов нет сезонов, сезон не сверен.")
                continue
            by_year = slots.get(group) or {}
            bad = [
                f"{year}: {(by_year.get(year) or {}).get('error') or (by_year.get(year) or {}).get('load_status') or 'не загружен'}"
                for year in item["years"]
                if (by_year.get(year) or {}).get("load_status") not in _READ_OK
            ]
            if bad:
                note = "сезон Cropwise не загружен (" + "; ".join(bad) + ")"
                lines.append(f"[{rid}] {group}: {note}.")
                add_row(rid, title, group, "unread", None, None, note)
                continue
            pack = pack_loaded_work(by_year, item["years"])
            hints = tuple(item["hints"])
            cropwise_ha, note = work_json_total(pack, hints, None)
            matrix, has_crops, rule_group = cropwise_ruled_matrix(pack, hints, None, group, rid)
            if rule_group is not None and has_crops:
                cropwise_ha = _matrix_total(matrix)
            if cropwise_ha is None:
                lines.append(f"[{rid}] {group}: {note}.")
                add_row(rid, title, group, "unread", None, None, note)
                continue
            seasons_note = f"сезоны {', '.join(str(y) for y in item['years'])}"
            file_total = season.get("total")
            if file_total is not None:
                checked += 1
                if season_over_limit(file_total, cropwise_ha):
                    diffs += 1
                    delta = abs(float(file_total) - cropwise_ha)
                    lines.append(
                        f"[{rid}] {group}: сезон, файл {float(file_total):.2f} га, "
                        f"Cropwise {cropwise_ha:.2f} га, Δ {delta:.2f} ({seasons_note}){note}."
                    )
                    add_row(rid, title, group, "diff", float(file_total), cropwise_ha,
                            f"{seasons_note}{note}".strip())
            firm_cw = (
                _matrix_firms(matrix) if rule_group is not None and has_crops
                else cropwise_firms_from_slot(pack, hints, None)
            )
            diffs += _season_part_diffs(
                add_row, lines, rid, title, group, seasons_note,
                season.get("firms") or {}, firm_cw, "агрофирма",
            )
            if has_crops:
                crop_cw = _matrix_crops(matrix)
                file_crops, crop_cw = _crops_for_file_check(
                    rid, group, season.get("crops") or {}, crop_cw,
                )
                file_crops, crop_cw = align_corn(file_crops, crop_cw)
                diffs += _season_part_diffs(
                    add_row, lines, rid, title, group, seasons_note, file_crops, crop_cw, "культура",
                )
    elapsed = time.monotonic() - started
    lines.append(
        f"Cropwise сезон: операций {checked}, расхождений больше "
        f"{CROPWISE_SEASON_DIFF_MIN_HA:.0f} га: {diffs}, {elapsed:.0f} с."
    )
    return lines, rows


def _season_part_diffs(
    add_row,
    lines: list[str],
    rid: str,
    title: str,
    group: str,
    note: str,
    file_map: dict[str, float],
    cropwise_map: dict[str, float],
    kind: str,
) -> int:
    """Расхождения по агрофирмам или культурам. Пустой файл не с чем сравнивать."""
    if not file_map:
        return 0
    found = 0
    for name in sorted(set(file_map) | set(cropwise_map)):
        file_ha = float(file_map.get(name) or 0)
        cropwise_ha = float(cropwise_map.get(name) or 0)
        if not season_over_limit(file_ha, cropwise_ha):
            continue
        label = f"{group} / {name}"
        lines.append(
            f"[{rid}] {label}: сезон, файл {file_ha:.2f} га, "
            f"Cropwise {cropwise_ha:.2f} га ({kind}, {note})."
        )
        add_row(rid, title, label, "diff", file_ha, cropwise_ha, f"{kind}. {note}".strip())
        found += 1
    return found
