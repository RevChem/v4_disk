# -*- coding: utf-8 -*-
"""
Единый расчёт «обработано за сутки» по 2.4 Вспашка — логика 1_main_ammonium__1_.py.
Используется сводками 1, 2 и 9.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Callable


def report_shift_date(report_cutoff: datetime, day_start_hour: int = 6):
    """Календарная дата смены при окне [cutoff-24h, cutoff) с границей в 06:00."""
    return (report_cutoff - timedelta(days=1)).date()


def _plow_per_firm_machine_from_tasks(
    tasks_season: list[dict],
    mtfmi_map: dict[int, list[dict]],
    day_start: datetime,
    cutoff: datetime,
    *,
    resolve_firm: Callable[[int], str],
    field_to_crop: dict[int, str],
    available_field_ids: set[int],
    work_type_ids: set[int],
    parse_dt_fn: Callable,
    tz,
    day_start_hour: int = 6,
    item_min_ha: float = 0.5,
) -> dict[str, dict[int, float]]:
    """Площадь за смену отчёта по машинам: firm → machine_id → га."""
    today_ids: set[int] = set()
    for t in tasks_season:
        if t.get("work_type_id") not in work_type_ids:
            continue
        end_dt = parse_dt_fn(t.get("end_time"))
        if end_dt is None:
            continue
        cal_date = end_dt.date()
        anchor = datetime(
            cal_date.year, cal_date.month, cal_date.day,
            day_start_hour, 0, 0, tzinfo=tz,
        )
        if end_dt < anchor:
            cal_date = cal_date - timedelta(days=1)
            anchor = datetime(
                cal_date.year, cal_date.month, cal_date.day,
                day_start_hour, 0, 0, tzinfo=tz,
            )
        shift_ds = anchor
        shift_co = anchor + timedelta(days=1)
        if shift_ds >= day_start and shift_co <= cutoff:
            today_ids.add(t["id"])

    per_firm_machine: dict[str, dict[int, dict]] = defaultdict(dict)

    for t in tasks_season:
        tid = t.get("id")
        if tid not in today_ids:
            continue
        if t.get("work_type_id") not in work_type_ids:
            continue
        mid = t.get("machine_id")
        if mid is None:
            continue
        items = mtfmi_map.get(tid, [])

        end_dt = parse_dt_fn(t.get("end_time"))
        if end_dt is None:
            continue
        cal_date = end_dt.date()
        anchor = datetime(
            cal_date.year, cal_date.month, cal_date.day,
            day_start_hour, 0, 0, tzinfo=tz,
        )
        if end_dt < anchor:
            cal_date = cal_date - timedelta(days=1)
            anchor = datetime(
                cal_date.year, cal_date.month, cal_date.day,
                day_start_hour, 0, 0, tzinfo=tz,
            )
        shift_co = anchor + timedelta(days=1)

        firm_items_map: dict[str, list[tuple]] = defaultdict(list)
        for item in items:
            fid = item.get("field_id")
            if not fid:
                continue
            fid_i = int(fid)
            if fid_i not in available_field_ids or fid_i not in field_to_crop:
                continue
            firm = resolve_firm(fid_i)
            if not firm:
                continue
            hourly = item.get("covered_area_hourly") or []
            cov = 0.0
            for entry in hourly:
                h = parse_dt_fn(entry[0])
                if h and h < shift_co:
                    cov += entry[1] or 0
            if not hourly:
                cov = float(item.get("covered_area") or 0.0)
            if cov > item_min_ha:
                firm_items_map[firm].append((item, cov))

        for firm, fitems_cov in firm_items_map.items():
            cov_firm_today = 0.0
            for item, _ in fitems_cov:
                hourly = item.get("covered_area_hourly") or []
                cov_h = 0.0
                for entry in hourly:
                    h = parse_dt_fn(entry[0])
                    if h and h < cutoff:
                        cov_h += entry[1] or 0
                if not hourly:
                    cov_h = float(item.get("covered_area") or 0.0)
                if cov_h > 0:
                    cov_firm_today += cov_h
            if cov_firm_today <= 0:
                continue
            pm = per_firm_machine[firm]
            if mid not in pm:
                pm[mid] = {"cov_total": 0.0}
            pm[mid]["cov_total"] += cov_firm_today

    return {
        firm: {int(mid): float(d["cov_total"]) for mid, d in pm.items()}
        for firm, pm in per_firm_machine.items()
    }


def plow_area_by_firm_from_tasks(
    tasks_season: list[dict],
    mtfmi_map: dict[int, list[dict]],
    day_start: datetime,
    cutoff: datetime,
    *,
    resolve_firm: Callable[[int], str],
    field_to_crop: dict[int, str],
    available_field_ids: set[int],
    work_type_ids: set[int],
    parse_dt_fn: Callable,
    tz,
    day_start_hour: int = 6,
    min_machine_ha: float = 10.0,
    item_min_ha: float = 0.5,
) -> dict[str, float]:
    """Площадь за смену отчёта: сумма по машинам >= min_machine_ha (только вспашка)."""
    per_firm = _plow_per_firm_machine_from_tasks(
        tasks_season,
        mtfmi_map,
        day_start,
        cutoff,
        resolve_firm=resolve_firm,
        field_to_crop=field_to_crop,
        available_field_ids=available_field_ids,
        work_type_ids=work_type_ids,
        parse_dt_fn=parse_dt_fn,
        tz=tz,
        day_start_hour=day_start_hour,
        item_min_ha=item_min_ha,
    )
    return {
        firm: sum(cov for cov in pm.values() if cov >= min_machine_ha)
        for firm, pm in per_firm.items()
    }


def plow_machine_cov_by_firm_from_tasks(
    tasks_season: list[dict],
    mtfmi_map: dict[int, list[dict]],
    day_start: datetime,
    cutoff: datetime,
    *,
    resolve_firm: Callable[[int], str],
    field_to_crop: dict[int, str],
    available_field_ids: set[int],
    work_type_ids: set[int],
    parse_dt_fn: Callable,
    tz,
    day_start_hour: int = 6,
    item_min_ha: float = 0.5,
) -> dict[str, dict[int, float]]:
    """Площадь за смену отчёта по каждой машине (для счётчика техники в сводке 2)."""
    return _plow_per_firm_machine_from_tasks(
        tasks_season,
        mtfmi_map,
        day_start,
        cutoff,
        resolve_firm=resolve_firm,
        field_to_crop=field_to_crop,
        available_field_ids=available_field_ids,
        work_type_ids=work_type_ids,
        parse_dt_fn=parse_dt_fn,
        tz=tz,
        day_start_hour=day_start_hour,
        item_min_ha=item_min_ha,
    )
