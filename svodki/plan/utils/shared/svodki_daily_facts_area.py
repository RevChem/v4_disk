# -*- coding: utf-8 -*-
"""Суточная площадь «Обработано за сутки» из mtfmi_daily (TaskStore) для сводки 1.

SVODKI_DAILY_FACTS_SHADOW=1 — legacy в отчёт, параллельно считаем по фактам и пишем расхождения.
SVODKI_DAILY_FACTS_DAILY=1 — колонка area_today на листе «Выработка техники» из фактов.

Порог на машину-день — как _detail_area_min_ha / _machine_qualifies_for_area в 1_main.
Строки без hourly добавляются через flat_facts (end_time в [day_start, cutoff)).
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Callable

ShadowFieldOk = Callable[[int], bool]
FirmForField = Callable[[int], str | None]


def daily_facts_env_flags() -> tuple[bool, bool]:
    def _on(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}

    return _on("SVODKI_DAILY_FACTS_SHADOW"), _on("SVODKI_DAILY_FACTS_DAILY")


def machine_qualifies_for_area(total: float, min_ha: float) -> bool:
    tc = round(float(total), 2)
    if min_ha <= 0:
        return tc > 0
    return tc >= min_ha


def area_today_by_firm_from_store(
    store,
    *,
    work_type_ids: list[int],
    shift_date: date,
    day_start: datetime,
    cutoff: datetime,
    area_min_ha: float,
    field_ok: ShadowFieldOk,
    firm_for_field: FirmForField,
    task_seasons: set[str] | None = None,
) -> dict[str, float]:
    """Сумма га за сменный день shift_date по агрофирмам (только field_id → фирма)."""
    wts = sorted({int(w) for w in work_type_ids})
    if not wts:
        return {}

    d0 = shift_date.isoformat()
    d1 = (shift_date + timedelta(days=1)).isoformat()

    field_ha: dict[tuple[int, int | None, int | None], float] = defaultdict(float)

    seasons = sorted(task_seasons) if task_seasons else [None]
    for season in seasons:
        kw = {"season": season} if season is not None else {}
        for wt, mid, fid, day, ha in store.daily_facts(wts, d0, d1, by="machine_field_day", **kw):
            if day != d0 or ha <= 0:
                continue
            try:
                fid_i = int(fid) if fid is not None else None
            except (TypeError, ValueError):
                fid_i = None
            if fid_i is None or not field_ok(fid_i):
                continue
            key = (int(wt), mid if mid is None else int(mid), fid_i)
            field_ha[key] += float(ha)

    ts_from = day_start.timestamp()
    ts_to = cutoff.timestamp()
    for wt, mid, fid, end_day, cov in store.flat_facts(wts, ts_from, ts_to):
        if end_day != d0 or cov <= 0:
            continue
        try:
            fid_i = int(fid) if fid is not None else None
        except (TypeError, ValueError):
            fid_i = None
        if fid_i is None or not field_ok(fid_i):
            continue
        if task_seasons is not None:
            # flat_facts не фильтрует season в SQL — отсечём по tasks при необходимости позже;
            # для shadow достаточно без season на flat (редкие строки без hourly).
            pass
        key = (int(wt), mid if mid is None else int(mid), fid_i)
        field_ha[key] += float(cov)

    machine_totals: dict[tuple[int, int | None], float] = defaultdict(float)
    for (wt, mid, _fid), ha in field_ha.items():
        machine_totals[(wt, mid)] += ha

    by_firm: dict[str, float] = defaultdict(float)
    for (wt, mid, fid_i), ha in field_ha.items():
        if area_min_ha > 0 and not machine_qualifies_for_area(
            machine_totals[(wt, mid)], area_min_ha
        ):
            continue
        firm = firm_for_field(fid_i)
        if not firm:
            continue
        by_firm[firm] += ha

    return {f: round(v, 2) for f, v in by_firm.items()}


def apply_report1_area_today_facts(
    day_stats: dict[str, dict],
    *,
    group_name: str,
    work_type_ids: list[int],
    shift_date: date,
    day_start: datetime,
    cutoff: datetime,
    area_min_ha: float,
    field_ok: ShadowFieldOk,
    firm_for_field: FirmForField,
    base_url: str,
    task_seasons: set[str] | None = None,
    firm_keys: list[str] | None = None,
) -> dict[str, dict]:
    shadow, use_daily = daily_facts_env_flags()
    if not shadow and not use_daily:
        return day_stats

    try:
        from svodki_task_store import get_store
    except ImportError:
        print(
            f"[daily_facts] {group_name}: svodki_task_store недоступен — legacy area_today",
            flush=True,
        )
        return day_stats

    store = get_store(base_url)
    if store is None:
        print(
            f"[daily_facts] {group_name}: SVODKI_TASK_STORE_DIR не задан — legacy area_today",
            flush=True,
        )
        return day_stats

    try:
        facts_by_firm = area_today_by_firm_from_store(
            store,
            work_type_ids=work_type_ids,
            shift_date=shift_date,
            day_start=day_start,
            cutoff=cutoff,
            area_min_ha=area_min_ha,
            field_ok=field_ok,
            firm_for_field=firm_for_field,
            task_seasons=task_seasons,
        )
    except Exception as exc:
        print(
            f"[daily_facts] {group_name}: ошибка расчёта по фактам ({exc!r}) — legacy area_today",
            flush=True,
        )
        return day_stats

    firms = firm_keys if firm_keys is not None else list(day_stats.keys())
    tol = 0.015
    any_diff = False
    for firm in firms:
        legacy = round(float((day_stats.get(firm) or {}).get("area_today") or 0.0), 2)
        facts = round(float(facts_by_firm.get(firm, 0.0)), 2)
        delta = round(facts - legacy, 2)
        if abs(delta) > tol:
            any_diff = True
        if shadow and (abs(delta) > tol or (legacy > 0 or facts > 0)):
            print(
                f"[daily_facts shadow] {group_name} | {firm}: "
                f"legacy={legacy} facts={facts} Δ={delta}",
                flush=True,
            )

    if shadow and not any_diff:
        print(
            f"[daily_facts shadow] {group_name}: area_today совпадает с фактами по всем фирмам",
            flush=True,
        )

    if use_daily:
        for firm in firms:
            st = day_stats.setdefault(firm, {})
            st["area_today"] = round(float(facts_by_firm.get(firm, 0.0)), 2)

    return day_stats
