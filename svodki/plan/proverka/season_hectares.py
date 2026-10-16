"""Гектары операции за сезоны редактора планов: живой work.json, без кэша.

Окно то же, что у сводки: с period_start выбранных сезонов в 06:00 до конца
смены даты отчёта. Каждый год Cropwise запрашивается отдельно.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any


def fetch_season_hectares(cookie: str, group_name: str, report_date: date) -> dict[str, Any]:
    name = str(group_name or "").strip()
    if not name:
        raise ValueError("Не выбрана группа работ")
    rows = fetch_all_season_hectares(cookie, report_date, groups=[name])
    found = rows["groups"]
    if not found:
        raise ValueError(f"У «{name}» в редакторе планов не заданы сезоны")
    return found[0]


def prepare_season_groups(report_date: date, groups: list[str] | None = None) -> list[dict[str, Any]]:
    """Операции с сезонами редактора: окно, виды работ, имена задач. Без сети."""
    from plans import WORK_GROUPS

    names = (
        [str(name) for name, _ids in WORK_GROUPS]
        if groups is None
        else [str(g).strip() for g in groups if str(g).strip()]
    )
    return [item for name in names if (item := _prepare_group(name, report_date))]


def load_season_slots(
    cookie: str,
    prepared: list[dict[str, Any]],
) -> dict[str, dict[int, dict[str, Any]]]:
    """work.json по каждому сезону группы: {группа: {год: слот загрузки}}. Без кэша."""
    if not str(cookie or "").strip():
        raise ValueError("Нет CROPWISE_SESSION_COOKIE")
    from proverka.cropwise_ui import WorkLoadJob, load_works_for_seasons

    jobs_by_year: dict[int, list] = defaultdict(list)
    for item in prepared:
        job = WorkLoadJob(item["group_name"], item["window_from"], item["window_to"], item["wt_ids"], tuple(item["hints"]))
        for year in item["years"]:
            jobs_by_year[year].append(job)

    loaded_by_group: dict[str, dict[int, dict[str, Any]]] = {item["group_name"]: {} for item in prepared}
    for year in sorted(jobs_by_year):
        loaded = load_works_for_seasons(cookie, jobs_by_year[year], years=[year])
        for name, by_year in loaded.items():
            loaded_by_group.setdefault(name, {}).update(by_year or {})
    return loaded_by_group


def fetch_all_season_hectares(
    cookie: str,
    report_date: date,
    groups: list[str] | None = None,
) -> dict[str, Any]:
    """Все операции (или переданный список) за свои сезоны редактора планов."""
    if not str(cookie or "").strip():
        raise ValueError("Нет CROPWISE_SESSION_COOKIE")

    from plans import FIRM_ORDER

    prepared = prepare_season_groups(report_date, groups)
    if groups is not None and groups and not prepared:
        missing = str(groups[0]).strip()
        raise ValueError(f"У «{missing}» в редакторе планов не заданы сезоны")

    loaded_by_group = load_season_slots(cookie, prepared)

    firms = list(FIRM_ORDER)
    out_groups = []
    for item in prepared:
        by_year = loaded_by_group.get(item["group_name"]) or {}
        seasons = [
            {"year": year, **_year_summary(by_year.get(year) or {}, item["hints"], firms)}
            for year in item["years"]
        ]
        out_groups.append({
            "group_name": item["group_name"],
            "years": item["years"],
            "period_start": item["period_start"],
            "period_end": item["period_end"],
            "window_from": item["window_from"],
            "window_to": item["window_to"],
            "seasons": seasons,
        })
    return {"report_date": report_date.isoformat(), "groups": out_groups}


def _prepare_group(name: str, report_date: date) -> dict[str, Any] | None:
    from proverka.task_names import tasks_for_group
    from proverka.work_groups import work_type_ids_for_group
    from season_unified import resolve_group_season_config

    cfg = resolve_group_season_config(name, report_date)
    years = [int(y) for y in (cfg.get("task_season_years") or cfg.get("history_years") or [])]
    # Те же виды работ, что в проверке за смену: к «5.0 Уборка» добавлено 84
    # («6.3 Скашивание с измельчением кукурузы»), иначе силос в сезоне не загрузится.
    wt_ids = [int(x) for x in work_type_ids_for_group(name)]
    hints = list(tasks_for_group(name))
    if not years or not wt_ids or not hints:
        return None
    start = f"{str(cfg['period_start']).split('T')[0]}T06:00:00"
    end = f"{(report_date + timedelta(days=1)).isoformat()}T06:00:00"
    return {
        "group_name": name,
        "years": years,
        "wt_ids": wt_ids,
        "hints": hints,
        "period_start": str(cfg["period_start"]).split("T")[0],
        "period_end": str(cfg["period_end"]).split("T")[0],
        "window_from": start,
        "window_to": end,
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
