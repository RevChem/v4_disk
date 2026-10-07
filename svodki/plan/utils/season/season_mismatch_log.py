# -*- coding: utf-8 -*-
"""Лог несовпадения сезона задания и года культуры. Расчёт сводок не меняет."""

from __future__ import annotations

import os
from collections import defaultdict

MARK = "SEASON_MISMATCH"
_MAX_GROUPS = 40

# task_id → season (machine_task.season, иначе year)
_TASK_SEASON: dict[int, str] = {}
# history_item_id → year
_HISTORY_YEAR: dict[int, int] = {}
# history_item_id → название культуры
_HISTORY_CROP: dict[int, str] = {}
_SEEN: set[tuple] = set()
_LAST_LINES: list[str] = []


def mismatch_lines() -> list[str]:
    """Группы, где год культуры не совпал с сезоном задания. Культуру расчёт не подменяет."""
    return list(_LAST_LINES)


def report_label() -> str:
    return (os.environ.get("SVODKI_REPORT_ID") or "").strip() or "?"


def _sanity(name: str, *args) -> None:
    """Встроенные проверки данных (svodki_sanity). Сбой проверок не должен ронять сводку."""
    try:
        import svodki_sanity

        getattr(svodki_sanity, name)(*args)
    except Exception as exc:  # noqa: BLE001
        print(f"DATA_CHECK пропущен: {exc!r}", flush=True)


def remember_tasks(tasks: list[dict] | None) -> None:
    if tasks:
        _sanity("on_tasks", tasks)
    for task in tasks or []:
        tid = task.get("id")
        if tid is None:
            continue
        season = str(task.get("season") or task.get("year") or "").strip()
        if season:
            _TASK_SEASON[int(tid)] = season


def remember_history_item(item_id: int, year: int | None, crop: str = "") -> None:
    if year:
        _HISTORY_YEAR[int(item_id)] = int(year)
    name = str(crop or "").strip()
    if name:
        _HISTORY_CROP[int(item_id)] = name


def log_loaded_mtfmi(mtfmi_map: dict) -> None:
    """После загрузки строк площади: сравнить с уже запомненными заданиями."""
    log_row_season_mismatches(None, mtfmi_map, _HISTORY_YEAR, _HISTORY_CROP)


def log_row_season_mismatches(
    tasks: list[dict] | None,
    mtfmi_map: dict,
    history_years: dict[int, int],
    history_crops: dict[int, str] | None = None,
    *,
    report: str | None = None,
) -> None:
    """Печатает строки SEASON_MISMATCH, если HistoryItem.year != season задания.

    Культуру в расчёте не подменяет.
    """
    remember_tasks(tasks)
    if mtfmi_map:
        _sanity("on_mtfmi", mtfmi_map)
    label = (report or report_label()).strip() or "?"
    crops = history_crops or {}
    groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)

    for raw_tid, items in (mtfmi_map or {}).items():
        try:
            tid = int(raw_tid)
        except (TypeError, ValueError):
            continue
        task_season = _TASK_SEASON.get(tid, "")
        if not task_season:
            continue
        for item in items or []:
            hid = item.get("history_item_id")
            if hid is None:
                continue
            try:
                hid_i = int(hid)
                hist_year = int(history_years.get(hid_i) or _HISTORY_YEAR.get(hid_i) or 0)
            except (TypeError, ValueError):
                continue
            if not hist_year or str(hist_year) == task_season:
                continue
            fid = item.get("field_id")
            seen_key = (label, tid, fid, hid_i)
            if seen_key in _SEEN:
                continue
            _SEEN.add(seen_key)
            crop = str(crops.get(hid_i) or _HISTORY_CROP.get(hid_i) or "").strip() or "без имени"
            groups[(task_season, str(hist_year), crop)].append(tid)

    if not groups:
        return

    shown = 0
    for (task_season, hist_year, crop), task_ids in groups.items():
        if shown >= _MAX_GROUPS:
            break
        shown += 1
        sample = ",".join(str(x) for x in task_ids[:5])
        line = (
            f"сезон задания {task_season}, год культуры {hist_year}, "
            f"{crop}, заданий {len(task_ids)} ({sample})"
        )
        if line not in _LAST_LINES and len(_LAST_LINES) < _MAX_GROUPS:
            _LAST_LINES.append(line)
        print(
            f"{MARK} сводка={label} сезон_задания={task_season} "
            f"год_культуры={hist_year} культура={crop} "
            f"строк={len(task_ids)} задания={sample}",
            flush=True,
        )
    hidden = len(groups) - shown
    if hidden > 0:
        print(f"{MARK} сводка={label} ещё_групп={hidden}", flush=True)
