"""Гектары операции за сезоны редактора планов: живой work.json, без кэша.

Окно то же, что у сводки: с period_start выбранных сезонов в 06:00 до конца
смены даты отчёта. Каждый год Cropwise запрашивается отдельно.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def fetch_season_hectares(cookie: str, group_name: str, report_date: date) -> dict[str, Any]:
    name = str(group_name or "").strip()
    if not name:
        raise ValueError("Не выбрана группа работ")
    if not str(cookie or "").strip():
        raise ValueError("Нет CROPWISE_SESSION_COOKIE")

    from plans import FIRM_ORDER, work_group_ids
    from proverka.cropwise_ui import WorkLoadJob, load_works_for_seasons
    from proverka.task_names import tasks_for_group
    from season_unified import resolve_group_season_config

    cfg = resolve_group_season_config(name, report_date)
    years = [int(y) for y in (cfg.get("task_season_years") or cfg.get("history_years") or [])]
    if not years:
        raise ValueError(f"У «{name}» в редакторе планов не заданы сезоны")
    wt_ids = work_group_ids(name)
    if not wt_ids:
        raise ValueError(f"У «{name}» нет видов работ")
    hints = list(tasks_for_group(name))
    if not hints:
        raise ValueError(f"У «{name}» нет колонки факта")

    start = f"{str(cfg['period_start']).split('T')[0]}T06:00:00"
    end = f"{(report_date + timedelta(days=1)).isoformat()}T06:00:00"
    loaded = load_works_for_seasons(
        cookie,
        [WorkLoadJob(name, start, end, list(wt_ids), tuple(hints))],
        years=years,
    )
    by_year = loaded.get(name) or {}
    seasons = [
        {"year": year, **_year_summary(by_year.get(year) or {}, hints, list(FIRM_ORDER))}
        for year in years
    ]
    return {
        "group_name": name,
        "years": years,
        "period_start": str(cfg["period_start"]).split("T")[0],
        "period_end": str(cfg["period_end"]).split("T")[0],
        "window_from": start,
        "window_to": end,
        "seasons": seasons,
    }


def _year_summary(slot: dict[str, Any], hints: list[str], firms: list[str]) -> dict[str, Any]:
    status = str(slot.get("load_status") or "")
    if status == "empty":
        return {
            "status": "empty",
            "total_ha": None,
            "by_firm": [],
            "column": None,
            "note": "в сезоне нет данных",
        }
    if status != "loaded":
        return {
            "status": status or "unread",
            "total_ha": None,
            "by_firm": [],
            "column": None,
            "note": str(slot.get("error") or "work.json не загружен"),
        }

    from proverka.cropwise_ui import parse_work_report_body

    parsed = parse_work_report_body(slot.get("body"), firms, hints)
    raw = parsed.get("fact_by_firm") or {}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for firm in firms:
        ha = float(raw.get(firm) or 0)
        if ha > 0:
            rows.append({"firm": firm, "ha": round(ha, 2)})
            seen.add(firm)
    for firm, ha in raw.items():
        if str(firm) in seen or float(ha or 0) <= 0:
            continue
        rows.append({"firm": str(firm), "ha": round(float(ha), 2)})
    unmatched = float(parsed.get("unmatched_firm_ha") or 0)
    if unmatched > 0:
        rows.append({"firm": "Без агрофирмы", "ha": round(unmatched, 2)})
    return {
        "status": "loaded",
        "total_ha": round(float(parsed.get("fact_total_ha") or 0), 2),
        "by_firm": rows,
        "column": parsed.get("fact_column"),
        "note": "",
    }
