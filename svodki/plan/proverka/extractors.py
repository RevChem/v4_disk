"""Извлечение area_today из модулей сводок (как в PDF/Excel)."""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

from proverka.reports_config import ReportSpec
from proverka.season_policy import attach_season_policy
from proverka.season_runtime import (
    apply_module_season_attrs,
    filter_tasks_by_seasons,
    reload_report_module,
    resolve_group_season,
    season_context,
    sync_module_season_start,
)
from proverka.svodka_details import flatten_firm_details


def _crop_label(raw: str) -> str:
    """
    Нормализует название культуры к единому каноническому виду (как в
    plan/1_main_dop.py::_CROP_MAP / FULL_SEEDING_CROP_ORDER), чтобы одна и
    та же культура из сводок 1, 2 и 5 совпадала по ключу при сверке —
    даже если в сыром field_to_crop/crops остались небольшие расхождения
    в написании ("рапс яр" vs "Рапс яровой" и т.п.).
    """
    raw = str(raw or "").strip()
    if not raw:
        return raw
    try:
        dop_mod = _load_dop_module_for_crops()
    except Exception:
        return raw
    try:
        label = dop_mod._crop_label_for_stats(raw)
    except Exception:
        return raw
    return label or raw


_DOP_MODULE_FOR_CROPS: Any = None


def _load_dop_module_for_crops() -> Any:
    """Ленивая загрузка 1_main_dop.py только ради _crop_label_for_stats/_CROP_MAP —
    без побочных эффектов остальных модулей (использует их собственный
    importlib-кэш, как и _extract_5 → _load_dop_module)."""
    global _DOP_MODULE_FOR_CROPS
    if _DOP_MODULE_FOR_CROPS is None:
        _DOP_MODULE_FOR_CROPS = importlib.import_module("1_main_dop")
    return _DOP_MODULE_FOR_CROPS


def _crop_breakdown_from_day_stats(
    day_stats: dict[str, dict],
    firm_order: list[str],
    *,
    crop_key: str = "area_by_crop",
) -> dict[str, dict[str, float]]:
    """
    {firm: {"area_by_crop": {crop: ha, ...}}} → {crop: {firm: ha}},
    с нормализацией названия культуры через _crop_label.
    """
    out: dict[str, dict[str, float]] = {}
    for firm in firm_order:
        by_crop = (day_stats.get(firm, {}) or {}).get(crop_key) or {}
        for crop, val in by_crop.items():
            v = round(float(val or 0), 2)
            if v <= 0:
                continue
            label = _crop_label(crop)
            out.setdefault(label, {})
            out[label][firm] = round(out[label].get(firm, 0.0) + v, 2)
    return out


def _parse_dt_msk(s: Any) -> datetime | None:
    from plans import TZ_MSK

    if s is None:
        return None
    if isinstance(s, (int, float)):
        ts = float(s)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(s).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_MSK)
    else:
        dt = dt.astimezone(TZ_MSK)
    return dt


SEEDING_WORK_GROUP = "4.0 Посев"


def _seeding_season_cfg(
    report_date: date,
    cfg: dict | None = None,
    run_override: dict | None = None,
) -> dict[str, Any]:
    """Сезоны посева — resolve_group_season_config(4.0 Посев), как сводка 5."""
    cfg = cfg or {}
    gcfg = (cfg.get("by_group") or {}).get(SEEDING_WORK_GROUP)
    if gcfg and (gcfg.get("task_season_years") or gcfg.get("history_years")):
        return gcfg
    return resolve_group_season(SEEDING_WORK_GROUP, report_date, run_override)


def _current_season(report_date: date) -> int:
    from season_calendar import season_year_for_date

    return season_year_for_date(report_date)


def _default_history_years(report_date: date) -> list[int]:
    """Сезоны по умолчанию: предыдущий и текущий (07.10.2026 и 15.03.2027 -> 2026, 2027)."""
    from season_calendar import season_year_for_date

    current = season_year_for_date(report_date)
    return [current - 1, current]


def _seeding_field_crop_by_id(
    m: Any,
    report_date: date,
    cfg: dict | None = None,
    run_override: dict | None = None,
) -> dict[int, str]:
    """Карта поля → культура посева (как 5_main_fixed / 2_main для 4.0 Посев)."""
    m5 = importlib.import_module("5_main_fixed")
    crop_cfg = _seeding_season_cfg(report_date, cfg, run_override)
    history_years = crop_cfg.get("history_years") or _default_history_years(report_date)
    report_season_year = int(min(int(y) for y in history_years))
    task_years = crop_cfg.get("task_season_years") or []
    crop_season_year = int(task_years[0]) if task_years else _current_season(report_date)
    crops = m.get_all_from_id("crops")
    crop_names_by_id = {
        int(c["id"]): str(c.get("name") or c.get("title") or "").strip()
        for c in crops
        if c.get("id") is not None
    }
    all_history: list[dict] = []
    for crop_year in sorted({int(y) for y in history_years}):
        all_history.extend(m.get_all_from_id("history_items", {"year": str(crop_year)}))
    skip_crops = getattr(m, "SKIP_CROPS", set()) or set()
    return m5.build_field_to_crop(
        crop_map=crop_names_by_id,
        history_items=all_history,
        report_date=report_date,
        period_start=date(report_season_year, 1, 1),
        skip_crops=skip_crops,
        season_year=crop_season_year,
    )


async def _seeding_area_today_by_firm_crop_dop(
    m: Any,
    report_date: date,
    cfg: dict | None = None,
    run_override: dict | None = None,
    *,
    day_start: datetime | None = None,
    cutoff: datetime | None = None,
) -> dict[str, dict[str, float]]:
    """Разбивка посева за смену — fetch_all_crop_stats (как сводка 5)."""
    import httpx

    crop_cfg = _seeding_season_cfg(report_date, cfg, run_override)
    m5 = importlib.import_module("5_main_fixed")
    m5.REPORT_DATE = report_date
    day_start, cutoff = m5.resolve_time_range(
        report_date, m5.DAY_START_HOUR, m5.DAY_END_HOUR,
    )
    history_years = crop_cfg.get("history_years") or _default_history_years(report_date)
    task_years = crop_cfg.get("task_season_years") or []
    report_season_year = int(min(int(y) for y in history_years))
    season_start = datetime(report_season_year, 1, 1, tzinfo=m5.TZ_MSK)
    seeding_field_crop = _seeding_field_crop_by_id(
        m5, report_date, cfg, run_override,
    )
    period_start = date(report_season_year, 1, 1)
    available_field_ids: set[int] = set()
    for r in m5.get_all_from_id("inventory_history_items", {"historyable_type": "Field"}):
        if not r.get("available"):
            continue
        fid_r = r.get("historyable_id")
        if fid_r is None:
            continue
        try:
            ev_start = date.fromisoformat(str(r.get("event_start_at"))[:10]) if r.get("event_start_at") else None
        except ValueError:
            ev_start = None
        try:
            ev_end = date.fromisoformat(str(r.get("event_end_at"))[:10]) if r.get("event_end_at") else None
        except ValueError:
            ev_end = None
        if ev_start is None:
            continue
        if ev_start <= report_date and (ev_end is None or ev_end >= period_start):
            available_field_ids.add(int(fid_r))
    from report_resources_fetch import load_report_resources

    headers = {"X-User-Api-Token": m5.API_TOKEN}
    sem = asyncio.Semaphore(m5.SEMAPHORE_LIMIT)
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    area_today_by_firm_crop: dict[str, dict[str, float]] = {}
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        tasks_today, tasks_season, mtfmi = await m5.fetch_all_tasks_and_mtfmi(
            client, sem, day_start, cutoff, season_start, m5.ALL_WT_IDS, label="Посев",
        )
        tasks_today = filter_tasks_by_seasons(m5, tasks_today, task_years)
        tasks_season = filter_tasks_by_seasons(m5, tasks_season, task_years)
        all_tasks = tasks_today + tasks_season
        field_to_firm, _, _ = await load_report_resources(
            client, m5.BASE_URL, sem, all_tasks, mtfmi, m5.get_all_from_id, m5._build_field_to_firm,
        )
        dop_mod = _load_dop_module_for_crops()
        dop_mod.SEASON_YEAR = report_season_year
        crop_firm_stats = await dop_mod.fetch_all_crop_stats(
            client, sem, day_start, cutoff, season_start,
            seeding_field_crop, field_to_firm,
            available_field_ids=available_field_ids,
            report_date=report_date,
            field_to_firm_all=field_to_firm,
        )
    for firm, st in (crop_firm_stats or {}).items():
        for crop, val in (st.get("today_by_crop") or {}).items():
            v = round(float(val or 0), 2)
            if v <= 0:
                continue
            label = _crop_label(crop)
            area_today_by_firm_crop.setdefault(label, {})
            area_today_by_firm_crop[label][firm] = round(
                area_today_by_firm_crop[label].get(firm, 0.0) + v, 2,
            )
    return area_today_by_firm_crop


def _seeding_perf_day_stats_from_tasks(
    tasks_today: list[dict],
    mtfmi_map: dict[int, list[dict]],
    field_to_firm: dict[int, str],
    field_to_crop: dict[int, str],
    cutoff: datetime,
    firm_order: list[str],
    parse_dt: Any,
    *,
    min_ha: float = 0.5,
) -> dict[str, dict]:
    """Статистика «Выраб. — 4.0 Посев»: area_by_crop по history_item строки."""
    from collections import defaultdict

    from mtfmi_batch_fetch import crop_label

    by_firm_crop: dict[str, dict[str, float]] = {
        firm: defaultdict(float) for firm in firm_order
    }
    for t in tasks_today:
        for item in mtfmi_map.get(int(t["id"]), []):
            fid = item.get("field_id")
            if fid is None:
                continue
            cov = 0.0
            for entry in item.get("covered_area_hourly") or []:
                h = parse_dt(entry[0])
                if h and h < cutoff:
                    cov += float(entry[1] or 0)
            if cov <= 0 and not (item.get("covered_area_hourly") or []):
                cov = float(item.get("covered_area") or 0)
            if cov <= min_ha:
                continue
            firm = field_to_firm.get(int(fid))
            if firm not in by_firm_crop:
                continue
            by_firm_crop[firm][crop_label(item)] += cov
    return {
        firm: {"area_by_crop": {k: round(v, 2) for k, v in crops.items()}}
        for firm, crops in by_firm_crop.items()
    }


def _crop_breakdown_seeding_from_today_by_field(
    day_stats: dict[str, dict],
    firm_order: list[str],
    seeding_field_crop: dict[int, str],
) -> dict[str, dict[str, float]]:
    per_firm: dict[str, dict[str, float]] = {}
    for firm in firm_order:
        by_crop: dict[str, float] = defaultdict(float)
        for fid, cov in (day_stats.get(firm, {}) or {}).get("today_by_field", {}).items():
            v = float(cov or 0)
            if v <= 0:
                continue
            crop = seeding_field_crop.get(int(fid))
            if crop:
                by_crop[_crop_label(crop)] += v
        if by_crop:
            per_firm[firm] = {c: round(v, 2) for c, v in by_crop.items() if round(v, 2) > 0}
    return _crop_breakdown_from_firm_crop_dicts(firm_order, per_firm)


def _load_field_to_crop_for_proverka(
    m: Any,
    cfg: dict,
    report_date: date,
    *,
    newest_year_wins: bool = False,
) -> dict[int, str]:
    """field_id → культура (как в сводках 1/2/3 при разбивке area_by_crop).

    newest_year_wins — внесение МУ: культура сезона задания (более новый
    history) заменяет активную культуру прошлого года на том же поле.
    """
    from plans import build_field_to_crop_general

    history_years = cfg.get("history_years") or [int(report_date.year)]
    priority_year = int(history_years[-1])
    if hasattr(m, "get_all_page"):
        crops = m.get_all_page("crops")
    else:
        crops = m.get_all_from_id("crops")
    crop_map_int = {
        int(c["id"]): str(c.get("name") or c.get("title") or "").strip()
        for c in crops
        if c.get("id") is not None
    }
    history_by_year: dict[int, list] = {}
    for crop_year in history_years:
        history_by_year[int(crop_year)] = m.get_all_from_id(
            "history_items", {"year": str(crop_year)},
        )
    skip_crops = getattr(m, "SKIP_CROPS", set()) or set()
    by_year: dict[int, dict[int, str]] = {}
    for crop_year, items in history_by_year.items():
        for hi in items or []:
            if not hi.get("active"):
                continue
            fid, cid = hi.get("field_id"), hi.get("crop_id")
            if not fid or not cid:
                continue
            try:
                fid_i = int(fid)
                cid_i = int(cid)
            except (TypeError, ValueError):
                continue
            name = str(crop_map_int.get(cid_i) or "").strip()
            if not name or name in skip_crops:
                continue
            by_year.setdefault(fid_i, {})[int(crop_year)] = name
    setattr(m, "_proverka_field_crops_by_year", by_year)
    return build_field_to_crop_general(
        crop_map=crop_map_int,
        history_by_year=history_by_year,
        priority_year=priority_year,
        skip_crops=skip_crops,
        newest_year_wins=newest_year_wins,
    )


def _per_firm_area_by_crop_from_mtfmi(
    tasks_today: list[dict],
    mtfmi_map: dict[int, list[dict]],
    field_to_firm: dict[int, str],
    field_to_crop: dict[int, str],
    cutoff: datetime,
    *,
    min_item_ha: float = 0.5,
    crop_field_ids: set[int] | None = None,
    crop_of: Any | None = None,
    row_crop: dict[int, str] | None = None,
) -> dict[str, dict[str, float]]:
    """Площадь за смену по культуре строки (firm → crop → ha). Пустая ссылка не выпадает из суммы."""
    per_firm: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in tasks_today:
        tid = t.get("id")
        if tid is None:
            continue
        items = mtfmi_map.get(tid) or mtfmi_map.get(int(tid)) or []
        for item in items:
            fid = item.get("field_id")
            if fid is None:
                continue
            fid_int = int(fid)
            if row_crop is None and crop_field_ids is not None and fid_int not in crop_field_ids:
                continue
            firm = field_to_firm.get(fid_int)
            if not firm:
                continue
            cov = 0.0
            for entry in (item.get("covered_area_hourly") or []):
                h = _parse_dt_msk(entry[0])
                if h and h < cutoff:
                    cov += float(entry[1] or 0.0)
            if cov <= min_item_ha:
                continue
            from mtfmi_batch_fetch import UNDEFINED_CROP, crop_of_item

            if row_crop is not None:
                raw_id = item.get("id")
                crop = row_crop.get(int(raw_id)) if raw_id is not None else None
            else:
                crop = crop_of_item(item, "")
            per_firm[firm][str(crop or UNDEFINED_CROP)] += cov
    return {
        f: {c: round(v, 2) for c, v in crops.items() if round(v, 2) > 0}
        for f, crops in per_firm.items()
    }


def _crop_breakdown_from_firm_area_by_crop(
    per_firm: dict[str, dict[str, float]],
    firm_order: list[str],
) -> dict[str, dict[str, float]]:
    day_stats = {f: {"area_by_crop": per_firm.get(f, {})} for f in firm_order}
    return _crop_breakdown_from_day_stats(day_stats, firm_order)


def _merge_harvest_veg_from_veg_op(
    grain_op: dict[str, Any],
    veg_op: dict[str, Any] | None,
    *,
    veg_crops: frozenset[str] | None = None,
) -> None:
    """Cropwise UI: картофель в колонке 5.0; у нас он на листе 5.4."""
    if not grain_op or not veg_op:
        return
    allowed = veg_crops or frozenset({"Картофель", "Морковь"})
    crop_bd = dict(grain_op.get("area_today_by_firm_crop") or {})
    veg_crops_bd = veg_op.get("area_today_by_firm_crop") or {}
    for crop, by_firm in veg_crops_bd.items():
        if crop not in allowed and _crop_label(crop) not in allowed:
            continue
        label = _crop_label(crop)
        for firm, ha in (by_firm or {}).items():
            v = round(float(ha or 0), 2)
            if v <= 0:
                continue
            existing = float((crop_bd.get(label) or {}).get(firm) or 0)
            if existing >= v - 0.01:
                continue
            crop_bd.setdefault(label, {})
            crop_bd[label][firm] = v
    if crop_bd:
        grain_op["area_today_by_firm_crop"] = crop_bd


def _merge_harvest_veg_from_compact(
    grain_op: dict[str, Any],
    harvest_group: dict[str, Any] | None,
    shift_date: date,
    *,
    veg_crops: frozenset[str] | None = None,
) -> None:
    """
    Cropwise в колонке 5.0 показывает картофель с зерновыми WT; namolot/10_main
    без кэша дают 0 — подставляем факт из сводки 2 (mtfmi), если есть.
    """
    if not harvest_group or not grain_op:
        return
    allowed = veg_crops or frozenset({"Картофель", "Морковь"})
    crop_bd = grain_op.get("area_today_by_firm_crop") or {}
    crops_by_firm = harvest_group.get("crops") or {}
    for firm, by_crop in crops_by_firm.items():
        for crop, cdata in (by_crop or {}).items():
            if crop not in allowed:
                continue
            ha = round(float((cdata.get("by_date") or {}).get(shift_date, 0) or 0), 2)
            if ha <= 0:
                continue
            label = _crop_label(crop)
            existing = float((crop_bd.get(label) or {}).get(firm) or 0)
            if existing >= ha - 0.01:
                continue
            crop_bd.setdefault(label, {})
            crop_bd[label][firm] = ha
    if crop_bd:
        grain_op["area_today_by_firm_crop"] = crop_bd


def _harvest_crop_breakdown_from_main10(m: Any, cache: Any | None) -> dict[str, dict[str, float]]:
    """
    Факт уборки за смену по культурам — как листы культур сводки 10
    (cache.area_by_firm_crop после prepare_harvest_cache).
    """
    firm_order = list(getattr(m, "AGROFARM_ORDER", []) or [])
    general_order = list(getattr(m, "GENERAL_CROP_ORDER", []) or [])
    other_key = getattr(m, "OTHER_CROP_KEY", "Иные культуры")
    out: dict[str, dict[str, float]] = {}
    if not cache or not getattr(cache, "area_by_firm_crop", None):
        return out
    crop_names = list(general_order)
    if other_key and other_key not in crop_names:
        crop_names.append(other_key)
    for crop_name in crop_names:
        label = _crop_label(crop_name)
        for firm in firm_order:
            _ft, fd = cache.area_by_firm_crop.get((firm, crop_name), (0.0, 0.0))
            v = round(float(fd or 0), 2)
            if v <= 0:
                continue
            out.setdefault(label, {})
            out[label][firm] = round(out[label].get(firm, 0.0) + v, 2)
    return out


def _crop_breakdown_from_firm_crop_dicts(
    firm_order: list[str],
    per_firm_crop: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    return _crop_breakdown_from_firm_area_by_crop(per_firm_crop, firm_order)


def _report9_crop_bd_cropwise_api(
    m: Any,
    today_task_ids: set[int],
    tasks_season: list[dict],
    mtfmi: dict[int, list[dict]],
    cutoff: datetime,
    field_to_firm: dict[int, str],
    wt_ids: set[int],
    season_years: set[int],
    *,
    plow_crop_names: bool = False,
    day_start: datetime | None = None,
) -> dict[str, dict[str, float]]:
    """
    Разбивка за смену по культурам — по history_item_id строки площади (как сводки)
    в proverka.cropwise_crops.api_area_by_firm_crop.
    """
    from proverka.cropwise_crops import api_area_by_firm_crop

    seasons_str = {str(int(y)) for y in season_years if y is not None}
    tasks = [
        t for t in tasks_season
        if t.get("id") in today_task_ids
        and int(t.get("work_type_id") or 0) in wt_ids
    ]
    if not tasks or not seasons_str:
        return {}
    raw = api_area_by_firm_crop(
        tasks, mtfmi, field_to_firm, seasons_str, cutoff, day_start=day_start,
    )
    if not plow_crop_names:
        return {c: dict(by_f) for c, by_f in raw.items() if by_f}
    allowed = getattr(m, "PLOW_SEASON_CROPS", frozenset())
    out: dict[str, dict[str, float]] = {}
    for crop, by_firm in raw.items():
        label = m._map_plow_crop_name(crop)
        if not label or label not in allowed:
            continue
        bucket = out.setdefault(str(label), {})
        for firm, ha in by_firm.items():
            bucket[firm] = round(bucket.get(firm, 0.0) + float(ha or 0), 2)
    return out


def _operations_with_crop_breakdown(
    spec: ReportSpec,
    firm_order: list[str],
    by_firm: dict[str, float],
    crop_breakdown: dict[str, dict[str, float]],
    *,
    work_type_ids: list[int] | set[int] | None = None,
) -> dict[str, dict[str, Any]] | None:
    if not spec.work_groups:
        return None
    wt_ids = sorted(int(x) for x in (work_type_ids or []))
    return {
        gn: _op_from_firm_dict(
            firm_order,
            by_firm,
            work_type_ids=wt_ids or None,
            area_today_by_firm_crop=crop_breakdown or None,
        )
        for gn in spec.work_groups
    }


def _pack(
    spec: ReportSpec,
    report_date: date,
    day_start: datetime,
    cutoff: datetime,
    module: Any,
    by_firm: dict[str, float],
    *,
    season_filter: str | None = None,
    work_type_ids: set[int] | None = None,
    tasks_today_count: int | None = None,
    raw_details: dict[str, dict] | None = None,
    run_override: dict | None = None,
    operations: dict[str, dict[str, Any]] | None = None,
    machines_today_by_firm: dict[str, float] | None = None,
) -> dict[str, Any]:
    firm_order = list(getattr(module, "AGROFIRMS", by_firm).keys())
    area = {f: round(float(by_firm.get(f, 0) or 0), 2) for f in firm_order}
    wt_ids = work_type_ids
    if wt_ids is None:
        wt_ids = set(getattr(module, "WORK_TYPE_IDS", set()) or set())
        wt_ids |= set(getattr(module, "ALL_WT_IDS", set()) or set())
        wt_ids |= set(getattr(module, "SPRAY_WT_IDS", set()) or set())
    result = {
        "report_id": spec.report_id,
        "title": spec.title,
        "module": spec.module,
        "report_date": report_date.isoformat(),
        "window": {"from": day_start.isoformat(), "to": cutoff.isoformat()},
        "work_type_ids": sorted(int(x) for x in wt_ids),
        "work_groups": list(spec.work_groups),
        "firm_order": firm_order,
        "area_today_by_firm": area,
        "total_ha": round(sum(area.values()), 2),
        "tasks_today_count": tasks_today_count,
    }
    if season_filter:
        result["_legacy_season_filter"] = season_filter
    if raw_details is not None:
        result["detail_rows"] = flatten_firm_details(raw_details, firm_order)
    if operations is None and spec.work_groups:
        operations = {
            gn: {
                "area_today_by_firm": dict(area),
                "total_ha": round(sum(area.values()), 2),
                "work_type_ids": sorted(int(x) for x in wt_ids),
            }
            for gn in spec.work_groups
        }
    if machines_today_by_firm is not None and operations:
        # Кол-во уникальных машин за смену — параллельная и полностью
        # независимая от area_today_by_firm метрика (см. proverka/
        # machine_compare.py). Пока прикладываем к каждой work_group этого
        # отчёта: для однотемных сводок (10, 11, …) это однозначно.
        mtbf = {f: round(float(machines_today_by_firm.get(f, 0) or 0), 0) for f in firm_order}
        mtbf_int = {f: int(v) for f, v in mtbf.items()}
        for gn in operations:
            operations[gn]["machines_today_by_firm"] = dict(mtbf_int)
            operations[gn]["total_machines"] = sum(mtbf_int.values())
    result["operations"] = operations or {}
    return attach_season_policy(result, spec, report_date, run_override=run_override)


def _op_from_firm_dict(
    firm_order: list[str],
    by_firm: dict[str, float],
    *,
    work_type_ids: list[int] | None = None,
    area_today_by_firm_crop: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    area = {f: round(float(by_firm.get(f, 0) or 0), 2) for f in firm_order}
    op: dict[str, Any] = {"area_today_by_firm": area, "total_ha": round(sum(area.values()), 2)}
    if work_type_ids:
        op["work_type_ids"] = sorted(int(x) for x in work_type_ids)
    if area_today_by_firm_crop:
        op["area_today_by_firm_crop"] = area_today_by_firm_crop
    return op


def _period_start_from_cfg(cfg: dict | None, report_date: date, fallback_year: int | None = None) -> date:
    if cfg and cfg.get("period_start"):
        return date.fromisoformat(str(cfg["period_start"]).split("T")[0])
    y = fallback_year or report_date.year
    return date(y, 1, 1)


def _load_spray_reference_data(
    module: Any,
    cfg: dict,
    report_date: date,
    run_override: dict | None = None,
) -> None:
    """history_items и available_field_ids с учётом сезонов из редактора планов."""
    history_years = cfg.get("history_years") or [getattr(module, "SEASON_YEAR", report_date.year)]
    module.REPORT_DATE = report_date
    sync_module_season_start(module, report_date, run_override)
    period_start = module.SEASON_START
    module.SEASON_YEAR = int(history_years[-1])

    crops = module.get_all_page("crops")
    module.crop_map = {c["id"]: c.get("name") or c.get("title", "") for c in crops}

    fgs = module.get_all_page("field_groups")
    module.fg_to_folder = {fg["id"]: fg.get("group_folder_id") for fg in fgs}
    module.folder_to_firm = dict(module.FOLDER_TO_FIRM)

    # Как сводка 2 / plans.build_field_to_crop_general: несколько лет history,
    # но озимые (рапс/пшеница) — только из priority_year (2027), как в Cropwise UI.
    from plans import build_field_to_crop_general

    priority_year = int(history_years[-1])
    history_by_year: dict[int, list] = {}
    for crop_year in history_years:
        history_by_year[int(crop_year)] = module.get_all_from_id(
            "history_items", {"year": str(crop_year)}
        )
    crop_map_int = {int(k): str(v) for k, v in module.crop_map.items() if k is not None}
    module.field_to_crop = build_field_to_crop_general(
        crop_map=crop_map_int,
        history_by_year=history_by_year,
        priority_year=priority_year,
        skip_crops=module.SKIP_CROPS,
    )

    period_end = report_date
    module.available_field_ids.clear()
    for r in module.get_all_from_id("inventory_history_items", {"historyable_type": "Field"}):
        if not r.get("available"):
            continue
        fid_r = r.get("historyable_id")
        if fid_r is None:
            continue
        try:
            ev_start = date.fromisoformat(str(r.get("event_start_at"))[:10]) if r.get("event_start_at") else None
        except (TypeError, ValueError):
            ev_start = None
        try:
            ev_end = date.fromisoformat(str(r.get("event_end_at"))[:10]) if r.get("event_end_at") else None
        except (TypeError, ValueError):
            ev_end = None
        if ev_start is None:
            continue
        if ev_start <= period_end and (ev_end is None or ev_end >= period_start):
            module.available_field_ids.add(int(fid_r))


async def _extract_1(
    spec: ReportSpec,
    report_date: date,
    cfg: dict | None = None,
    run_override: dict | None = None,
) -> dict[str, Any]:
    cfg = cfg or {}
    import httpx

    from proverka.work_groups import work_group_wt_map

    wt_by_group = work_group_wt_map()
    os.environ["REPORT1_STANDALONE"] = "1"
    m = importlib.import_module("1_main_ammonium__1_")
    if hasattr(m, "apply_report1_thresholds"):
        m.apply_report1_thresholds()
    m.REPORT_DATE = report_date
    if hasattr(m, "refresh_season_config"):
        m.refresh_season_config()
    day_start, cutoff = m.compute_time_range(report_date)
    m.load_reference_data()
    headers = {"X-User-Api-Token": m.API_TOKEN}
    sem = asyncio.Semaphore(m.SEMAPHORE_LIMIT)
    other_groups = [wg for wg in m.WORK_GROUPS if wg[0] != m.WORK_TYPE_NAME]
    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        _season, day_stats_main, *_rest = await m.fetch_fertilizer_11_mtfmi(
            client, sem, day_start, cutoff,
        )
        other = await m._fetch_other_group_stats(
            client, sem, day_start, cutoff, other_groups, set(),
        )
    all_stats = {m.WORK_TYPE_NAME: day_stats_main}
    all_stats.update(other)
    firm_order = list(m.AGROFIRMS.keys())
    operations = {
        gn: _op_from_firm_dict(
            firm_order,
            {f: float(st.get(f, {}).get("area_today") or 0) for f in firm_order},
            work_type_ids=wt_by_group.get(gn),
        )
        for gn, st in all_stats.items()
    }
    # Кол-во уникальных машин за смену: и fetch_fertilizer_11_mtfmi, и
    # _fetch_other_group_stats уже кладут его в day_stats[firm]["n_mach"]
    # (= len(detail_own)+len(detail_hired), см. 1_main_ammonium__1_.py) —
    # параллельно area_today, по той же группе работ.
    for gn, st in all_stats.items():
        machines_by_firm = {
            f: int(st.get(f, {}).get("n_mach") or 0) for f in firm_order
        }
        operations[gn]["machines_today_by_firm"] = machines_by_firm
        operations[gn]["total_machines"] = sum(machines_by_firm.values())
        # Культуры в PDF сводки 1 — только лист МУ; посев/опрыскивание и др.
        # подставляются из доноров (apply_report1_donor_area_graft).
        if gn == m.WORK_TYPE_NAME:
            crop_breakdown = _crop_breakdown_from_day_stats(st, firm_order)
            if crop_breakdown:
                operations[gn]["area_today_by_firm_crop"] = crop_breakdown
    seeding_gn = getattr(m, "SEEDING_GROUP_NAME", SEEDING_WORK_GROUP)
    if seeding_gn in operations and seeding_gn in all_stats:
        try:
            dop_mod = _load_dop_module_for_crops()
            seed_crop_bd: dict[str, dict[str, float]] = {}
            seed_st = all_stats[seeding_gn]
            for firm in firm_order:
                by_crop_raw = (seed_st.get(firm) or {}).get("area_by_crop") or {}
                if not by_crop_raw:
                    continue
                for crop, val in by_crop_raw.items():
                    v = round(float(val or 0), 2)
                    if v <= 0:
                        continue
                    label = dop_mod._crop_label_for_stats(crop)
                    if label not in dop_mod._DISPLAY_SEEDING_CROP_SET:
                        continue
                    seed_crop_bd.setdefault(label, {})
                    seed_crop_bd[label][firm] = round(
                        seed_crop_bd[label].get(firm, 0.0) + v, 2,
                    )
            if seed_crop_bd:
                operations[seeding_gn]["area_today_by_firm_crop"] = seed_crop_bd
        except Exception:
            pass
    harvest_gn = getattr(m, "HARVEST_GRAIN_GROUP_NAME", HARVEST_GRAIN_GROUP)
    if harvest_gn in operations:
        prev = operations[harvest_gn]
        grain_op = await _grain_harvest_operation_for_proverka(report_date, run_override)
        grain_op["machines_today_by_firm"] = prev.get("machines_today_by_firm") or {}
        grain_op["total_machines"] = prev.get("total_machines") or sum(
            grain_op["machines_today_by_firm"].values()
        )
        operations[harvest_gn] = grain_op
        veg_gn = getattr(
            m, "HARVEST_VEG_PLAN_KEY", "5.4 Уборка картофеля, лука",
        )
        if veg_gn in operations and veg_gn in all_stats:
            veg_crop_bd = _crop_breakdown_from_day_stats(all_stats[veg_gn], firm_order)
            if veg_crop_bd:
                operations[veg_gn]["area_today_by_firm_crop"] = veg_crop_bd
    main_area = operations.get(m.WORK_TYPE_NAME, {}).get("area_today_by_firm") or {}
    by_firm = {f: float(main_area.get(f, 0) or 0) for f in firm_order}
    return _pack(
        spec, report_date, day_start, cutoff, m, by_firm,
        operations=operations, run_override=run_override,
    )


async def _extract_2(
    spec: ReportSpec,
    report_date: date,
    cfg: dict | None = None,
    run_override: dict | None = None,
) -> dict[str, Any]:
    m = importlib.import_module("2_main_ideal_compact")
    from proverka.work_groups import work_group_wt_map

    wt_by_group = work_group_wt_map()
    m.TODAY = report_date
    m.REPORT_DATE = report_date
    if hasattr(m, "refresh_season_config"):
        m.refresh_season_config()
    headers = {"X-User-Api-Token": m.API_TOKEN}
    ref = m.load_reference_data()
    # collect_work_data внутри вызывает asyncio.run — только в отдельном потоке.
    work_data = await asyncio.to_thread(
        m.collect_work_data,
        *ref[0:15],
        headers,
        history_item_to_crop=ref[18],
        history_item_to_year=ref[19],
    )
    shift_date = report_date - timedelta(days=1)
    firm_order = list(m.FIRM_ORDER)
    operations: dict[str, dict[str, Any]] = {}
    for gn, gd in work_data.items():
        abfd = gd.get("area_by_firm_date") or {}
        by_firm = {
            f: round(float((abfd.get(f) or {}).get(shift_date, 0) or 0), 2)
            for f in firm_order
        }
        # Разбивка «факт за сутки» по культуре — уже посчитана в collect_work_data
        # (gd["crops"][firm][crop]["by_date"]), берём срез за сегодняшнюю смену.
        crop_breakdown: dict[str, dict[str, float]] = {}
        crops_by_firm = gd.get("crops") or {}
        for f in firm_order:
            for crop, cdata in (crops_by_firm.get(f) or {}).items():
                v = round(float((cdata.get("by_date") or {}).get(shift_date, 0) or 0), 2)
                if v <= 0:
                    continue
                label = _crop_label(crop)
                crop_breakdown.setdefault(label, {})
                crop_breakdown[label][f] = round(crop_breakdown[label].get(f, 0.0) + v, 2)
        op = _op_from_firm_dict(
            firm_order, by_firm, work_type_ids=wt_by_group.get(gn),
            area_today_by_firm_crop=crop_breakdown,
        )
        # Кол-во машин — как блок «СУТОЧНАЯ ВЫРАБОТКА» в 2_main_ideal_compact
        # (machine_day_area + _day_rate_machines + _group_count_threshold), а не
        # machines[own|hired]: там другой порог и при пустом machines[] факт
        # теряется, хотя площадь за смену уже есть.
        count_threshold = m._group_count_threshold(gn)
        mda_by_firm = gd.get("machine_day_area") or {}
        machines_today_by_firm: dict[str, int] = {}
        for f in firm_order:
            by_mid = (mda_by_firm.get(f) or {}).get(shift_date) or {}
            machines_today_by_firm[f] = m._day_rate_machines(by_mid, count_threshold)
        op["machines_today_by_firm"] = machines_today_by_firm
        op["total_machines"] = sum(machines_today_by_firm.values())
        operations[gn] = op
    if m.HARVEST_GROUP_NAME in operations:
        prev = operations[m.HARVEST_GROUP_NAME]
        grain_op = await _grain_harvest_operation_for_proverka(report_date, run_override)
        grain_op["machines_today_by_firm"] = prev.get("machines_today_by_firm") or {}
        grain_op["total_machines"] = prev.get("total_machines") or sum(
            grain_op["machines_today_by_firm"].values()
        )
        operations[m.HARVEST_GROUP_NAME] = grain_op
    total_by_firm = {
        f: round(sum(operations.get(gn, {}).get("area_today_by_firm", {}).get(f, 0) for gn in operations), 2)
        for f in firm_order
    }
    day_start = datetime(
        shift_date.year, shift_date.month, shift_date.day,
        m.DAY_START_HOUR if hasattr(m, "DAY_START_HOUR") else 6, 0, tzinfo=m.TZ_MSK,
    )
    cutoff = datetime(
        report_date.year, report_date.month, report_date.day,
        m.DAY_START_HOUR if hasattr(m, "DAY_START_HOUR") else 6, 0, tzinfo=m.TZ_MSK,
    )
    return _pack(
        spec, report_date, day_start, cutoff, m, total_by_firm,
        operations=operations, run_override=run_override,
    )


async def _extract_3(
    spec: ReportSpec,
    report_date: date,
    cfg: dict | None = None,
    run_override: dict | None = None,
) -> dict[str, Any]:
    import httpx

    cfg = cfg or {}
    m = importlib.import_module("3_main_real")
    day_start, cutoff = m.compute_time_range(report_date, m.DAY_START_HOUR, m.DAY_END_HOUR)
    season_start_date = sync_module_season_start(m, report_date, run_override)
    season_start = datetime(
        season_start_date.year, season_start_date.month, season_start_date.day,
        tzinfo=m.TZ_MSK,
    )
    history_years = cfg.get("history_years") or [int(report_date.year)]
    task_years = cfg.get("task_season_years") or []
    field_to_firm_pre = m.load_field_to_firm()
    crop_field_ids: set[int] = set()
    for crop_year in history_years:
        crop_field_ids |= m.load_crop_field_ids(int(crop_year), field_to_firm_pre)
    headers = {"X-User-Api-Token": m.API_TOKEN}
    sem = asyncio.Semaphore(m.SEMAPHORE_LIMIT)
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        tasks_today, tasks_season, mtfmi = await m.fetch_all_tasks_and_mtfmi(
            client, sem, day_start, cutoff, season_start, m.WORK_TYPE_IDS,
        )
        tasks_today = filter_tasks_by_seasons(m, tasks_today, task_years)
        tasks_season = filter_tasks_by_seasons(m, tasks_season, task_years)
        from report_resources_fetch import load_report_resources

        all_tasks = tasks_today + tasks_season
        field_to_firm, machines_map, implements_dict = await load_report_resources(
            client, m.BASE_URL, sem, all_tasks, mtfmi, m.get_all_from_id, m._build_field_to_firm,
        )
        mtfmi_crop, _crop_diag = await m.resolve_crops_from_mtfmi(
            client, sem, mtfmi, field_to_firm, int(report_date.year),
        )
    details = m.build_all_firm_stats(
        tasks_today, tasks_season, mtfmi, season_start, day_start, cutoff,
        field_to_firm, implements_dict, mtfmi_crop=mtfmi_crop,
    )
    by_firm = {firm: float(details[firm]["area_today"]) for firm in m.AGROFIRMS}
    # details[firm]["machines_today"] уже посчитан внутри build_all_firm_stats
    # (см. 3_main_real.py: "machines_today": len(pm_count)).
    machines_today_by_firm = {
        firm: int(details[firm].get("machines_today") or 0) for firm in m.AGROFIRMS
    }
    season_filter = "+".join(str(y) for y in task_years) if task_years else None
    firm_order = list(m.AGROFIRMS.keys())
    field_to_crop = _load_field_to_crop_for_proverka(
        m, cfg or {}, report_date, newest_year_wins=True,
    )
    # Порог 0.5 га здесь режет куски поля и вычитает их из культуры,
    # хотя итог операции эти гектары уже включает (соя 0.60 га).
    per_firm_crop = _per_firm_area_by_crop_from_mtfmi(
        tasks_today, mtfmi, field_to_firm, field_to_crop, cutoff,
        min_item_ha=0.0,
        row_crop=mtfmi_crop,
    )
    crop_breakdown = _crop_breakdown_from_firm_area_by_crop(per_firm_crop, firm_order)
    operations = _operations_with_crop_breakdown(
        spec, firm_order, by_firm, crop_breakdown,
        work_type_ids=m.WORK_TYPE_IDS,
    )
    _remember_shift_facts(
        report_id=spec.report_id,
        group=m.WORK_TYPE_TITLE,
        report_date=report_date,
        tasks_today=tasks_today,
        mtfmi_map=mtfmi,
        field_to_firm=field_to_firm,
        field_to_crop=field_to_crop,
        cutoff=cutoff,
        parse_dt=m.parse_dt,
        crop_field_ids=crop_field_ids,
        machines_map=machines_map,
        hired_group_id=getattr(m, "HIRED_GROUP_ID", None),
        min_machine_ha=m.MIN_DETAIL_AREA_HA,
    )
    return _pack(
        spec, report_date, day_start, cutoff, m, by_firm,
        season_filter=season_filter,
        tasks_today_count=len(tasks_today), raw_details=details,
        machines_today_by_firm=machines_today_by_firm,
        operations=operations,
        run_override=run_override,
    )


async def _extract_4(
    spec: ReportSpec,
    report_date: date,
    cfg: dict | None = None,
    run_override: dict | None = None,
) -> dict[str, Any]:
    m = importlib.import_module("4_main_fixed")
    day_stats, _, _, _, day_start, cutoff = await m.compute_harrowing_facts(report_date)
    by_firm = {firm: float(day_stats.get(firm, {}).get("area_today") or 0) for firm in m.AGROFIRMS}
    # day_stats[firm]["n_mach"] — как в 4_main_fixed.py: len(pm_counted).
    machines_today_by_firm = {
        firm: int(day_stats.get(firm, {}).get("n_mach") or 0) for firm in m.AGROFIRMS
    }
    firm_order = list(m.AGROFIRMS.keys())
    crop_breakdown = _crop_breakdown_from_day_stats(day_stats, firm_order)
    operations = _operations_with_crop_breakdown(
        spec, firm_order, by_firm, crop_breakdown,
        work_type_ids=m.ALL_WT_IDS,
    )
    return _pack(
        spec, report_date, day_start, cutoff, m, by_firm,
        raw_details=day_stats,
        machines_today_by_firm=machines_today_by_firm,
        operations=operations,
        run_override=run_override,
    )


async def _extract_5(
    spec: ReportSpec,
    report_date: date,
    cfg: dict | None = None,
    run_override: dict | None = None,
) -> dict[str, Any]:
    import httpx

    cfg = cfg or {}
    m = importlib.import_module("5_main_fixed")
    history_years = cfg.get("history_years") or _default_history_years(report_date)
    task_years = cfg.get("task_season_years") or []
    report_season_year = int(min(int(y) for y in history_years))
    m.SEASON_YEAR = report_season_year
    day_start, cutoff = m.resolve_time_range(report_date, m.DAY_START_HOUR, m.DAY_END_HOUR)
    period_start = date(report_season_year, 1, 1)
    season_start = datetime(report_season_year, 1, 1, tzinfo=m.TZ_MSK)

    crops = m.get_all_from_id("crops")
    crop_names_by_id = {
        int(c["id"]): str(c.get("name") or c.get("title") or "").strip()
        for c in crops if c.get("id") is not None
    }
    all_history: list[dict] = []
    for crop_year in history_years:
        all_history.extend(m.get_all_from_id("history_items", {"year": str(crop_year)}))
    field_crop_by_id = m.build_field_to_crop(
        crop_map=crop_names_by_id,
        history_items=all_history,
        report_date=report_date,
        period_start=period_start,
        skip_crops=m.SKIP_CROPS,
        season_year=int(task_years[0] if task_years else _current_season(report_date)),
    )
    available_field_ids: set[int] = set()
    for r in m.get_all_from_id("inventory_history_items", {"historyable_type": "Field"}):
        if not r.get("available"):
            continue
        fid_r = r.get("historyable_id")
        if fid_r is None:
            continue
        try:
            ev_start = date.fromisoformat(str(r.get("event_start_at"))[:10]) if r.get("event_start_at") else None
        except ValueError:
            ev_start = None
        try:
            ev_end = date.fromisoformat(str(r.get("event_end_at"))[:10]) if r.get("event_end_at") else None
        except ValueError:
            ev_end = None
        if ev_start is None:
            continue
        if ev_start <= report_date and (ev_end is None or ev_end >= period_start):
            available_field_ids.add(int(fid_r))

    headers = {"X-User-Api-Token": m.API_TOKEN}
    sem = asyncio.Semaphore(m.SEMAPHORE_LIMIT)
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        tasks_today, tasks_season, mtfmi = await m.fetch_all_tasks_and_mtfmi(
            client, sem, day_start, cutoff, season_start, m.ALL_WT_IDS, label="Посев",
        )
        tasks_today = filter_tasks_by_seasons(m, tasks_today, task_years)
        tasks_season = filter_tasks_by_seasons(m, tasks_season, task_years)
        from report_resources_fetch import load_report_resources

        all_tasks = tasks_today + tasks_season
        field_to_firm, machines_map, _ = await load_report_resources(
            client, m.BASE_URL, sem, all_tasks, mtfmi, m.get_all_from_id, m._build_field_to_firm,
        )
        # Разбивка «факт за сутки» по культуре (в т.ч. яровой рапс) — тем же
        # способом, каким сводка 5 сама строит лист «Сводка по культурам»
        # (см. 5_main_fixed._fetch_crop_seeding_stats → 1_main_dop.fetch_all_crop_stats).
        # Отдельный проход по machine_tasks того же периода — совпадает с тем,
        # что сама сводка 5 уже делает при генерации Excel.
        area_today_by_firm_crop: dict[str, dict[str, float]] = {}
        try:
            dop_mod = _load_dop_module_for_crops()
            dop_mod.SEASON_YEAR = report_season_year
            crop_firm_stats = await dop_mod.fetch_all_crop_stats(
                client, sem, day_start, cutoff, season_start,
                field_crop_by_id, field_to_firm,
                available_field_ids=available_field_ids,
                report_date=report_date,
                field_to_firm_all=field_to_firm,
            )
            firm_order_seed = list(m.AGROFIRMS.keys())
            seeding_perf = _seeding_perf_day_stats_from_tasks(
                tasks_today,
                mtfmi,
                field_to_firm,
                field_crop_by_id,
                cutoff,
                firm_order_seed,
                m.parse_dt,
                min_ha=float(m.MIN_DETAIL_AREA_HA),
            )
            # Как таблица «Посев — Сводно» в сводке 1: today из «Выраб. — 4.0 Посев»,
            # а не сырой mtfmi fetch_all_crop_stats (иначе пшеница ~×2).
            dop_mod._apply_seeding_today_from_perf(crop_firm_stats, seeding_perf)
            for firm, st in (crop_firm_stats or {}).items():
                for crop, val in (st.get("today_by_crop") or {}).items():
                    v = round(float(val or 0), 2)
                    if v <= 0:
                        continue
                    label = _crop_label(crop)
                    area_today_by_firm_crop.setdefault(label, {})
                    area_today_by_firm_crop[label][firm] = round(
                        area_today_by_firm_crop[label].get(firm, 0.0) + v, 2
                    )
        except Exception as exc:  # noqa: BLE001 — сверка не должна падать из-за доп. листа
            print(f"  [proverka] сводка 5: пропускаем разбивку по культуре ({exc})")
    details = m.build_all_firm_stats(
        tasks_today, tasks_season, mtfmi, field_to_firm, day_start, cutoff, report_date,
        m.ALL_WT_IDS, m.MIN_DETAIL_AREA_HA, crop_names_by_id,
        min_count_area_ha=m.MIN_DETAIL_AREA_HA,
        field_crop_by_id=field_crop_by_id,
        available_field_ids=available_field_ids,
        fact_by_all_fields=True,
    )
    by_firm = {firm: float(details.get(firm, {}).get("area_today") or 0) for firm in m.AGROFIRMS}
    # details[firm]["machines_today"] — как в 5_main_fixed.py: len(pm_counted).
    machines_today_by_firm = {
        firm: int(details.get(firm, {}).get("machines_today") or 0) for firm in m.AGROFIRMS
    }
    season_filter = "+".join(str(y) for y in task_years) if task_years else None
    operations = None
    if spec.work_groups:
        firm_order = list(m.AGROFIRMS.keys())
        operations = {
            gn: _op_from_firm_dict(
                firm_order, by_firm,
                work_type_ids=sorted(int(x) for x in m.ALL_WT_IDS),
                area_today_by_firm_crop=area_today_by_firm_crop,
            )
            for gn in spec.work_groups
        }
        # machines_today_by_firm/total_machines проставит _pack (см. ниже).
    group = spec.work_groups[0] if spec.work_groups else "4.0 Посев"
    _remember_shift_facts(
        report_id=spec.report_id,
        group=group,
        report_date=report_date,
        tasks_today=tasks_today,
        mtfmi_map=mtfmi,
        field_to_firm=field_to_firm,
        field_to_crop=field_crop_by_id,
        cutoff=cutoff,
        parse_dt=m.parse_dt,
        machines_map=machines_map,
        hired_group_id=getattr(m, "HIRED_GROUP_ID", None),
        min_machine_ha=m.MIN_DETAIL_AREA_HA,
        work_type_ids=set(m.ALL_WT_IDS),
        covered_area_if_zero=True,
    )
    return _pack(
        spec, report_date, day_start, cutoff, m, by_firm,
        season_filter=season_filter,
        work_type_ids=set(m.ALL_WT_IDS),
        tasks_today_count=len(tasks_today),
        raw_details=details,
        operations=operations,
        machines_today_by_firm=machines_today_by_firm,
        run_override=run_override,
    )


async def _extract_6(
    spec: ReportSpec,
    report_date: date,
    cfg: dict | None = None,
    run_override: dict | None = None,
) -> dict[str, Any]:
    import httpx

    cfg = cfg or {}
    m = importlib.import_module("6_main_3")
    day_start, cutoff = m.compute_time_range(report_date, m.DAY_START_HOUR, m.DAY_END_HOUR)
    spray_cfg = resolve_group_season("3.1 Опрыскивание", report_date, run_override)
    veg_cfg = resolve_group_season("3.2 Опрыскивание (овощи)", report_date, run_override)
    field_task_years = list(spray_cfg.get("task_season_years") or [])
    veg_task_years = list(veg_cfg.get("task_season_years") or [])
    ref_cfg = {
        **cfg,
        "history_years": spray_cfg.get("history_years") or cfg.get("history_years"),
        "task_season_years": field_task_years or cfg.get("task_season_years"),
    }
    _load_spray_reference_data(m, ref_cfg, report_date, run_override)
    if field_task_years:
        ys = ",".join(str(y) for y in field_task_years)
        os.environ["SVODKI_SPRAY_TASK_SEASON_YEARS"] = ys
        from season_unified import group_env_key

        os.environ[group_env_key("3.1 Опрыскивание", "TASK_SEASON_YEARS")] = ys
    if veg_task_years:
        from season_unified import group_env_key

        os.environ[group_env_key("3.2 Опрыскивание (овощи)", "TASK_SEASON_YEARS")] = ",".join(
            str(y) for y in veg_task_years
        )
    headers = {"X-User-Api-Token": m.API_TOKEN}
    sem = asyncio.Semaphore(m.SEMAPHORE_LIMIT)
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        all_details, veg_details = await asyncio.gather(
            m.fetch_all_data(
                client, sem, day_start, cutoff,
                task_season_years=field_task_years or None,
            ),
            m.fetch_all_data(
                client, sem, day_start, cutoff,
                wt_ids=m.VEG_WT_IDS,
                agrofirms=m.VEG_AGROFIRMS,
                min_detail_area=0.0,
                track_veg_crops=True,
                task_season_years=veg_task_years or None,
            ),
        )
    firm_order = list(m.AGROFIRMS.keys())
    spray = {
        f: float(all_details.get(f, {}).get("area_today") or 0) for f in firm_order
    }
    veg = {
        f: float(veg_details.get(f, {}).get("area_today") or 0) for f in firm_order
    }
    by_firm = {f: round(spray[f] + veg[f], 2) for f in firm_order}
    # machines_today — как в 6_main_3.py: len(pm_count), считается той же
    # fetch_all_data() отдельно для основного и овощного прогона.
    spray_machines = {
        f: int(all_details.get(f, {}).get("machines_today") or 0) for f in firm_order
    }
    veg_machines = {
        f: int(veg_details.get(f, {}).get("machines_today") or 0) for f in firm_order
    }
    spray_day_stats = {
        f: {"area_by_crop": all_details.get(f, {}).get("today_by_crop") or {}}
        for f in firm_order
    }
    veg_day_stats = {
        f: {"area_by_crop": veg_details.get(f, {}).get("veg_today_by_crop") or {}}
        for f in firm_order
    }
    spray_crop_bd = _crop_breakdown_from_day_stats(spray_day_stats, firm_order)
    veg_crop_bd = _crop_breakdown_from_day_stats(veg_day_stats, firm_order)
    op_spray = _op_from_firm_dict(
        firm_order, spray, area_today_by_firm_crop=spray_crop_bd or None,
    )
    op_spray["machines_today_by_firm"] = spray_machines
    op_spray["total_machines"] = sum(spray_machines.values())
    op_veg = _op_from_firm_dict(
        firm_order, veg, area_today_by_firm_crop=veg_crop_bd or None,
    )
    op_veg["machines_today_by_firm"] = veg_machines
    op_veg["total_machines"] = sum(veg_machines.values())
    operations = {
        "3.1 Опрыскивание": op_spray,
        "3.2 Опрыскивание (овощи)": op_veg,
    }
    season_years = sorted(set(field_task_years) | set(veg_task_years))
    season_filter = "+".join(str(y) for y in season_years) if season_years else None
    return _pack(
        spec, report_date, day_start, cutoff, m, by_firm,
        season_filter=season_filter,
        work_type_ids=set(m.ALL_WT_IDS), raw_details=all_details,
        operations=operations, run_override=run_override,
    )


async def _extract_7_8(
    spec: ReportSpec,
    report_date: date,
    module_name: str,
    cfg: dict | None = None,
    run_override: dict | None = None,
) -> dict[str, Any]:
    import httpx

    cfg = cfg or {}
    m = importlib.import_module(module_name)
    day_start, cutoff = m.compute_time_range(report_date, m.DAY_START_HOUR, m.DAY_END_HOUR)
    season_start_date = sync_module_season_start(m, report_date, run_override)
    season_start = datetime(
        season_start_date.year, season_start_date.month, season_start_date.day,
        tzinfo=m.TZ_MSK,
    )
    headers = {"X-User-Api-Token": m.API_TOKEN}
    sem = asyncio.Semaphore(m.SEMAPHORE_LIMIT)
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    task_years = cfg.get("task_season_years") or []
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        tasks_today, tasks_season, mtfmi = await m.fetch_all_tasks_and_mtfmi(
            client, sem, day_start, cutoff, season_start, m.ALL_WT_IDS,
        )
        tasks_today = filter_tasks_by_seasons(m, tasks_today, task_years)
        tasks_season = filter_tasks_by_seasons(m, tasks_season, task_years)
        from report_resources_fetch import load_report_resources

        all_tasks = tasks_today + tasks_season
        field_to_firm, machines_map, implements_dict = await load_report_resources(
            client, m.BASE_URL, sem, all_tasks, mtfmi, m.get_all_from_id, m._build_field_to_firm,
        )

    if module_name == "8_main_fixed":
        from report_resources_fetch import collect_report_resource_ids

        field_ids, _, _ = collect_report_resource_ids(all_tasks, mtfmi)
        _field_to_crop, _available_field_ids = m.load_field_filters(report_date, field_ids)
        cult_details, _ = m.build_all_firm_stats(
            tasks_today, tasks_season, mtfmi, season_start, day_start, cutoff,
            field_to_firm, None, None, implements_dict,
        )
        details = cult_details
    else:
        from report_resources_fetch import collect_report_resource_ids

        field_ids, _, _ = collect_report_resource_ids(all_tasks, mtfmi)
        disk_crops = m.load_disk_crops(report_date, field_ids)
        details = m.build_all_firm_stats(
            tasks_today, tasks_season, mtfmi, season_start, day_start, cutoff,
            field_to_firm, implements_dict, disk_crops,
        )
    by_firm = {firm: float(details[firm]["area_today"]) for firm in m.AGROFIRMS}
    # details[firm]["machines_today"] — общий ключ и для 7_main__4_.py
    # (build_all_firm_stats), и для 8_main_fixed.py (cult_details из
    # _aggregate_tasks_for_firms внутри build_all_firm_stats).
    machines_today_by_firm = {
        firm: int(details[firm].get("machines_today") or 0) for firm in m.AGROFIRMS
    }
    firm_order = list(m.AGROFIRMS.keys())
    field_to_crop_map: dict[int, str] = {}
    if module_name == "8_main_fixed":
        field_to_crop_map = _field_to_crop
        per_firm_crop = _per_firm_area_by_crop_from_mtfmi(
            tasks_today, mtfmi, field_to_firm, field_to_crop_map, cutoff,
        )
        crop_breakdown = _crop_breakdown_from_firm_area_by_crop(per_firm_crop, firm_order)
    else:
        # Та же культура, по которой build_all_firm_stats уже вычел исключённые гектары.
        field_to_crop_map = disk_crops.field_to_crop
        crop_breakdown = _crop_breakdown_from_day_stats(details, firm_order)
    operations = _operations_with_crop_breakdown(
        spec, firm_order, by_firm, crop_breakdown,
        work_type_ids=m.ALL_WT_IDS,
    )
    if module_name != "8_main_fixed":
        group = spec.work_groups[0] if spec.work_groups else ""
        _remember_shift_facts(
            report_id=spec.report_id,
            group=group,
            report_date=report_date,
            tasks_today=tasks_today,
            mtfmi_map=mtfmi,
            field_to_firm=field_to_firm,
            field_to_crop=field_to_crop_map,
            cutoff=cutoff,
            parse_dt=m.parse_dt,
            machines_map=machines_map,
            hired_group_id=getattr(m, "HIRED_GROUP_ID", None),
            min_machine_ha=m.MIN_DETAIL_AREA_HA,
        )
    return _pack(
        spec, report_date, day_start, cutoff, m, by_firm,
        work_type_ids=set(m.ALL_WT_IDS),
        tasks_today_count=len(tasks_today),
        raw_details=details,
        machines_today_by_firm=machines_today_by_firm,
        operations=operations,
        run_override=run_override,
    )


async def _extract_9(
    spec: ReportSpec,
    report_date: date,
    cfg: dict | None = None,
    run_override: dict | None = None,
) -> dict[str, Any]:
    import httpx

    cfg = cfg or {}
    m = importlib.import_module("9_main_fixed")
    task_years = cfg.get("task_season_years") or []
    plow_only = bool(cfg.get("task_season_plow_only"))
    day_start, cutoff = m.compute_time_range(report_date, m.DAY_START_HOUR, m.DAY_END_HOUR)
    season_start_date = sync_module_season_start(m, report_date, run_override)
    season_start = datetime(
        season_start_date.year, season_start_date.month, season_start_date.day,
        tzinfo=m.TZ_MSK,
    )
    field_crop_period = date(report_date.year, 3, 1)
    field_to_crop, _ = m.load_field_to_crop_and_available(report_date, field_crop_period)
    headers = {"X-User-Api-Token": m.API_TOKEN}
    sem = asyncio.Semaphore(m.SEMAPHORE_LIMIT)
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        today_task_ids, tasks_season, mtfmi = await m.fetch_all_tasks_and_mtfmi(
            client, sem, day_start, cutoff, season_start, m.ALL_WT_IDS,
        )
        field_ids, machine_ids, implement_ids = m.collect_report_resource_ids(tasks_season, mtfmi)
        field_to_firm, _, implements_dict = await asyncio.gather(
            m.load_field_to_firm_async(client, sem, field_ids),
            m.load_machines_map_async(client, sem, machine_ids),
            m.load_implements_dict_async(client, sem, implement_ids),
        )
    details = m.build_all_firm_stats(
        today_task_ids, tasks_season, mtfmi, season_start, day_start, cutoff,
        field_to_firm, field_to_crop, implements_dict, available_field_ids=None,
    )
    firm_order = list(m.FIRM_ORDER)
    plow = {f: float(details.get(f, {}).get("plow_today") or 0) for f in firm_order}
    deep = {f: float(details.get(f, {}).get("deep_today") or 0) for f in firm_order}
    by_firm = {f: round(plow[f] + deep[f], 2) for f in firm_order}
    # plow_machines_today / deep_machines_today — отдельные счётчики машин
    # по каждой из двух операций (см. 9_main_fixed.py: plow_mach_ids/
    # deep_mach_ids, множества могут пересекаться, если машина работала
    # и на вспашке, и на глубокорыхлении — это ожидаемо и не баг).
    plow_machines = {
        f: int(details.get(f, {}).get("plow_machines_today") or 0) for f in firm_order
    }
    deep_machines = {
        f: int(details.get(f, {}).get("deep_machines_today") or 0) for f in firm_order
    }
    from proverka.season_runtime import resolve_group_season

    plow_cfg = resolve_group_season("2.4 Вспашка", report_date, run_override)
    plow_seasons = {
        int(y) for y in (plow_cfg.get("task_season_years") or list(getattr(m, "PLOW_FACT_SEASONS", ()) or ()))
    }
    try:
        from season_unified import editor_check_years

        ey = editor_check_years("2.4 Вспашка", report_date)
        if ey:
            plow_seasons = {int(y) for y in ey}
    except Exception:
        pass
    plow_wt = set(getattr(m, "PLOW_WT_IDS", set()) or set())
    deep_wt = set(getattr(m, "DEEP_WT_IDS", set()) or set())
    deep_seasons = {
        int(t.get("season"))
        for t in tasks_season
        if t.get("id") in today_task_ids
        and int(t.get("work_type_id") or 0) in deep_wt
        and t.get("season") is not None
    }
    if not deep_seasons:
        deep_seasons = {
            int(y) for y in (
                resolve_group_season("2.8 Глубокорыхление", report_date, run_override).get(
                    "task_season_years",
                )
                or list(getattr(m, "DEEP_FACT_SEASONS", ()) or ())
            )
        }
    plow_crop_bd = await asyncio.to_thread(
        lambda: _report9_crop_bd_cropwise_api(
            m, today_task_ids, tasks_season, mtfmi, cutoff, field_to_firm,
            plow_wt, plow_seasons, plow_crop_names=True, day_start=day_start,
        ),
    )
    deep_crop_bd = await asyncio.to_thread(
        lambda: _report9_crop_bd_cropwise_api(
            m, today_task_ids, tasks_season, mtfmi, cutoff, field_to_firm,
            deep_wt, deep_seasons, plow_crop_names=False, day_start=day_start,
        ),
    )
    op_plow = _op_from_firm_dict(
        firm_order, plow, area_today_by_firm_crop=plow_crop_bd or None,
    )
    op_plow["machines_today_by_firm"] = plow_machines
    op_plow["total_machines"] = sum(plow_machines.values())
    op_plow["work_type_ids"] = sorted(plow_wt)
    op_deep = _op_from_firm_dict(
        firm_order, deep, area_today_by_firm_crop=deep_crop_bd or None,
    )
    op_deep["machines_today_by_firm"] = deep_machines
    op_deep["total_machines"] = sum(deep_machines.values())
    op_deep["work_type_ids"] = sorted(deep_wt)
    operations = {
        "2.4 Вспашка": op_plow,
        "2.8 Глубокорыхление": op_deep,
    }
    season_filter = None
    if plow_only and task_years:
        season_filter = f"plow={'+'.join(str(y) for y in task_years)}"
    elif task_years:
        season_filter = "+".join(str(y) for y in task_years)
    return _pack(
        spec, report_date, day_start, cutoff, m, by_firm,
        season_filter=season_filter,
        work_type_ids=set(m.ALL_WT_IDS),
        tasks_today_count=len(today_task_ids),
        raw_details=details,
        operations=operations,
        run_override=run_override,
    )


HARVEST_GRAIN_GROUP = "5.0 Уборка"
_REPORT14_MODULE_NAME = "report14_main_proverka"


def _load_report14_module() -> Any:
    """14.main.py — имя файла с точкой, обычный import_module не подходит."""
    if _REPORT14_MODULE_NAME in sys.modules:
        return sys.modules[_REPORT14_MODULE_NAME]
    plan_dir = Path(__file__).resolve().parents[1]
    path = plan_dir / "14.main.py"
    spec = importlib.util.spec_from_file_location(_REPORT14_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_REPORT14_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


def _harvest_machines_from_report14_summary(
    m: Any,
    summary_df: Any,
    folder_to_name: dict[int, str],
) -> tuple[dict[str, int], dict[str, float]]:
    """machines_today_by_firm и area_today_by_firm из листа «Сводка» сводки 14."""
    firm_order = list(getattr(m, "AGROFIRMS", {}).keys())
    machines_by_firm = {f: 0 for f in firm_order}
    area_by_firm = {f: 0.0 for f in firm_order}
    if summary_df is None or summary_df.empty:
        return machines_by_firm, area_by_firm
    data_rows = summary_df[summary_df["Агрофирмы"].astype(str) != "Итого"]
    for _, row in data_rows.iterrows():
        display_name = str(row["Агрофирмы"])
        firm_key = None
        for fk, (folder_id, _region_id) in m.AGROFIRMS.items():
            disp = m.get_firm_display_name(fk, folder_id, folder_to_name)
            if disp == display_name:
                firm_key = fk
                break
        key = firm_key or display_name
        if key not in machines_by_firm:
            machines_by_firm[key] = 0
            area_by_firm[key] = 0.0
        machines_by_firm[key] = int(row.get("Количество комбайнов на полях") or 0)
        area_by_firm[key] = round(float(row.get("Убрано за сутки, га") or 0), 2)
    return machines_by_firm, area_by_firm


def _build_report14_harvest_facts(report_date: date) -> dict[str, Any]:
    """Факт 5.0 Уборка из сводки 14 — как лист «Сводка» (комбайны на полях)."""
    m = _load_report14_module()
    os.environ["REPORT_DATE"] = report_date.isoformat()
    m.REPORT_DATE = report_date
    day_start, cutoff = m._shift_datetimes(report_date, m.DAY_START_HOUR)
    all_machines = m.load_machines()
    harvesters, _hired, harvester_ids, hired_ids, _machine_group = m.split_machines(
        all_machines,
    )
    (
        field_to_fg,
        fg_to_folder,
        folder_to_name,
        field_to_firm_folder,
        _field_name_to_firm_folder,
    ) = m.load_geo_reference()
    task_machine_ids = harvester_ids | hired_ids
    tasks = m.fetch_machine_tasks(task_machine_ids, report_date)
    task_ids = {t["id"] for t in tasks}
    fmi_list = m.fetch_fmi(task_ids, report_date)
    task_to_wt = {
        int(t["id"]): int(t["work_type_id"])
        for t in tasks
        if t.get("work_type_id") is not None
    }
    fmi_list = m._normalize_fmi_for_shift(
        fmi_list, report_date, task_to_wt,
    )
    summary = m.build_summary_report(
        report_date,
        folder_to_name,
        tasks=tasks,
        fmi_list=fmi_list,
        field_to_fg=field_to_fg,
        fg_to_folder=fg_to_folder,
        field_to_firm_folder=field_to_firm_folder,
    )
    machines_by_firm, area_by_firm = _harvest_machines_from_report14_summary(
        m, summary, folder_to_name,
    )
    from proverka.work_groups import work_type_ids_for_group

    wt_ids = work_type_ids_for_group(HARVEST_GRAIN_GROUP)
    grain_op = _op_from_firm_dict(
        list(machines_by_firm.keys()),
        area_by_firm,
        work_type_ids=wt_ids or None,
    )
    grain_op["machines_today_by_firm"] = machines_by_firm
    grain_op["total_machines"] = sum(machines_by_firm.values())
    return {
        "module": m,
        "day_start": day_start,
        "cutoff": cutoff,
        "operations": {HARVEST_GRAIN_GROUP: grain_op},
        "area_by_firm": area_by_firm,
        "wt_ids": wt_ids,
    }


async def _grain_harvest_operation_for_proverka(
    report_date: date,
    run_override: dict | None = None,
) -> dict[str, Any]:
    """
    Зерновая уборка 5.0 — как лист «Общий намолот» сводки 10.
    Сводки 1/2/11 в PDF берут этот же источник (build_general_harvest),
    а не локальный пересчёт по machine_tasks (даёт ~41 га овощей вместо зерна).
    """
    m = importlib.import_module("10_main_harvest")
    if hasattr(m, "configure_for_report_date"):
        m.configure_for_report_date(report_date)
    else:
        m.REPORT_DATE = report_date
    from proverka.reports_config import REPORTS

    harvest_spec = REPORTS["10"]
    with season_context(harvest_spec, report_date, run_override) as cfg:
        reload_report_module("10_main_harvest")
        apply_module_season_attrs(m, report_date, cfg)
        cache = None
        if not getattr(m, "USE_FAST_MODE", False):
            cache = await asyncio.to_thread(m.prepare_harvest_cache, report_date)
        return await asyncio.to_thread(
            _grain_harvest_op_main10, m, report_date, cache,
        )


def _grain_harvest_op_main10(
    m: Any,
    report_date: date,
    cache: Any | None,
) -> dict[str, Any]:
    """
    Зерновая уборка 5.0 — как лист «Общий намолот» сводки 10.

    По фирмам — build_general_harvest_by_firms (как в PDF).
    Итого — сумма по фирмам: строка «Итого» build_general_harvest без полного
    cache иногда занижает (~1772 vs ~1780), если суммировать только по культурам.
    """
    m13 = importlib.import_module("13_holding_overview")
    firms_df = m13.build_general_harvest_by_firms(m, report_date, cache)
    firm_order = list(getattr(m, "AGROFARM_ORDER", []) or [])
    by_firm = {f: 0.0 for f in firm_order}
    if firms_df is not None and not firms_df.empty:
        data_rows = firms_df[firms_df["Агрофирма"].astype(str) != "Итого"]
        for _, row in data_rows.iterrows():
            firm = str(row["Агрофирма"])
            if firm in by_firm:
                by_firm[firm] = round(float(row["Факт за сутки, га"] or 0), 2)

    firm_sum = round(sum(by_firm.values()), 2)
    grain_total = firm_sum
    if firm_sum < 0.01:
        general = m.build_general_harvest(report_date, cache)
        if general is not None and not general.empty and "Культура" in general.columns:
            total_row = general[general["Культура"].astype(str) == "Итого"]
            if not total_row.empty and "Факт за сутки, га" in total_row.columns:
                grain_total = float(total_row.iloc[0]["Факт за сутки, га"] or 0)

    from proverka.work_groups import work_type_ids_for_group

    wt_ids = work_type_ids_for_group(HARVEST_GRAIN_GROUP)
    crop_breakdown = _harvest_crop_breakdown_from_main10(m, cache)
    op = _op_from_firm_dict(
        firm_order, by_firm, work_type_ids=wt_ids or None,
        area_today_by_firm_crop=crop_breakdown or None,
    )
    op["total_ha"] = round(grain_total, 2)
    return op


async def _extract_10(
    spec: ReportSpec,
    report_date: date,
    cfg: dict | None = None,
    run_override: dict | None = None,
) -> dict[str, Any]:
    m = importlib.import_module("10_main_harvest")
    if hasattr(m, "configure_for_report_date"):
        m.configure_for_report_date(report_date)
    else:
        m.REPORT_DATE = report_date
    day_start, cutoff = m.compute_time_range(report_date, m.DAY_START_HOUR, m.DAY_END_HOUR)
    df = await asyncio.to_thread(m.build_vegetables_report, report_date)
    grain_op = await _grain_harvest_operation_for_proverka(report_date, run_override)
    firm_order = list(getattr(m, "AGROFARM_ORDER", []) or [])
    veg_firm = getattr(m, "_VEG_HARVEST_FIRM", "АФ Чистопольская")
    total = 0.0
    if df is not None and not df.empty and "Факт за сутки, га" in df.columns:
        mask = df.get("Культура", df.index).astype(str) != "Итого"
        total = round(float(df.loc[mask, "Факт за сутки, га"].sum()), 2)
    by_firm = {f: 0.0 for f in firm_order}
    if veg_firm in by_firm:
        by_firm[veg_firm] = total
    elif firm_order:
        by_firm[firm_order[0]] = total
    else:
        by_firm[veg_firm] = total
        firm_order = [veg_firm]
    wt_ids = list(getattr(m, "VEGETABLE_WORK_TYPE_IDS", []) or [76])
    operations = {
        "5.0 Уборка": grain_op,
        "5.4 Уборка картофеля, лука": _op_from_firm_dict(
            firm_order, by_firm, work_type_ids=wt_ids,
        ),
    }
    # Кол-во уникальных машин за смену на уборке овощей — считается прямо
    # внутри build_vegetables_report (см. 10_main_harvest.py) и читается
    # отсюда как побочный результат того же вызова, без повторного похода
    # за данными.
    machines_today_by_firm = None
    if hasattr(m, "get_vegetables_machines_today_by_firm"):
        machines_today_by_firm = m.get_vegetables_machines_today_by_firm()
    return _pack(
        spec, report_date, day_start, cutoff, m, by_firm,
        work_type_ids=set(int(x) for x in wt_ids),
        operations=operations,
        machines_today_by_firm=machines_today_by_firm,
        run_override=run_override,
    )


async def _extract_13(
    spec: ReportSpec,
    report_date: date,
    cfg: dict | None = None,
    run_override: dict | None = None,
) -> dict[str, Any]:
    m13 = importlib.import_module("13_holding_overview")
    main10 = m13._load_main10()
    configure = getattr(main10, "configure_for_report_date", None)
    if callable(configure):
        configure(report_date)
    else:
        main10.REPORT_DATE = report_date
    day_start, cutoff = main10.compute_time_range(
        report_date, main10.DAY_START_HOUR, main10.DAY_END_HOUR,
    )
    grain_op = await _grain_harvest_operation_for_proverka(report_date, run_override)
    firm_order = list(getattr(main10, "AGROFARM_ORDER", []) or [])
    by_firm = dict(grain_op.get("area_today_by_firm") or {})
    wt_ids = grain_op.get("work_type_ids") or []
    operations = {"5.0 Уборка": grain_op}
    return _pack(
        spec, report_date, day_start, cutoff, main10, by_firm,
        work_type_ids=set(int(x) for x in wt_ids),
        operations=operations,
        run_override=run_override,
    )


async def _extract_14(
    spec: ReportSpec,
    report_date: date,
    cfg: dict | None = None,
    run_override: dict | None = None,
) -> dict[str, Any]:
    """Сводка 14 — эталон кол-ва комбайнов на уборке (лист «Сводка»)."""
    built = await asyncio.to_thread(_build_report14_harvest_facts, report_date)
    m = built["module"]
    return _pack(
        spec,
        report_date,
        built["day_start"],
        built["cutoff"],
        m,
        built["area_by_firm"],
        work_type_ids=set(int(x) for x in (built["wt_ids"] or [])),
        operations=built["operations"],
        run_override=run_override,
    )


async def _extract_11(
    spec: ReportSpec,
    report_date: date,
    cfg: dict | None = None,
    run_override: dict | None = None,
) -> dict[str, Any]:
    import httpx

    m = importlib.import_module("11_main_harvest")
    day_start, cutoff = m.compute_time_range(report_date, m.DAY_START_HOUR, m.DAY_END_HOUR)
    season_start_date = sync_module_season_start(m, report_date, run_override)
    season_start = datetime(
        season_start_date.year, season_start_date.month, season_start_date.day,
        m.DAY_START_HOUR, 0, 0, tzinfo=m.TZ_MSK,
    )
    field_to_firm = m.load_field_to_firm()
    m10 = m._load_main10_harvest()
    field_to_crop = m10.get_field_to_crop()
    veg_ctx = m._veg_harvest_context()
    implements_map = m._load_implements_map()
    headers = {"X-User-Api-Token": m.API_TOKEN}
    sem = asyncio.Semaphore(m.SEMAPHORE_LIMIT)
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        tasks_today, tasks_season, mtfmi = await m._load_harvest_resources_async(
            client, sem, day_start, cutoff, season_start,
        )
    details = m.aggregate_harvest_by_firm(
        tasks_today, tasks_season, mtfmi, field_to_firm, day_start, cutoff,
        field_to_crop=field_to_crop, veg_ctx=veg_ctx, implements_map=implements_map,
    )
    grain_op = await _grain_harvest_operation_for_proverka(report_date, run_override)
    firm_order = list(m.AGROFIRMS)
    grain_op["machines_today_by_firm"] = {
        firm: int(
            details[firm].get("machines_grain")
            or details[firm].get("machines_today")
            or 0
        )
        for firm in firm_order
    }
    grain_op["total_machines"] = sum(grain_op["machines_today_by_firm"].values())
    by_firm = {
        f: float(grain_op["area_today_by_firm"].get(f, 0) or 0) for f in firm_order
    }
    return _pack(
        spec, report_date, day_start, cutoff, m, by_firm,
        tasks_today_count=len(tasks_today), raw_details=details,
        operations={HARVEST_GRAIN_GROUP: grain_op},
        run_override=run_override,
    )


EXTRACTORS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "1": _extract_1,
    "2": _extract_2,
    "3": _extract_3,
    "4": _extract_4,
    "5": _extract_5,
    "6": _extract_6,
    "7": lambda spec, d, cfg=None, run_override=None: _extract_7_8(
        spec, d, "7_main__4_", cfg, run_override
    ),
    "8": lambda spec, d, cfg=None, run_override=None: _extract_7_8(
        spec, d, "8_main_fixed", cfg, run_override
    ),
    "9": _extract_9,
    "10": _extract_10,
    "11": _extract_11,
    "13": _extract_13,
    "14": _extract_14,
}


def _remember_shift_facts(**kwargs: Any) -> None:
    """Повторный проход по уже загруженным заданиям. Ошибка не роняет сводку."""
    try:
        from proverka.task_facts import add_rows, build_shift_task_facts, capture_on

        if not capture_on():
            return
        add_rows(build_shift_task_facts(**kwargs))
    except Exception:
        return


def _store_task_facts(result: dict[str, Any], report_id: str, report_date: date, rows: list) -> None:
    """Отдельный TSV. Не меняет area_today; сбой записи остаётся в meta."""
    from proverka.task_facts import safe_store_task_facts

    meta = safe_store_task_facts(
        rows,
        report_id=report_id,
        report_date=report_date,
        operations=result.get("operations") if isinstance(result, dict) else None,
    )
    if meta and isinstance(result, dict):
        result["task_facts_meta"] = meta


async def compute_report_facts(
    spec: ReportSpec,
    report_date: date,
    run_override: dict | None = None,
) -> dict[str, Any]:
    from proverka.task_facts import begin_task_facts, discard_task_facts, take_task_facts

    fn = EXTRACTORS.get(spec.report_id)
    if not fn:
        raise ValueError(f"Нет экстрактора для сводки {spec.report_id}")
    tokens = begin_task_facts()
    try:
        if spec.report_id == "14":
            result = await fn(spec, report_date, None, run_override)
        else:
            with season_context(spec, report_date, run_override) as cfg:
                reload_report_module(spec.module)
                mod = importlib.import_module(spec.module)
                apply_module_season_attrs(mod, report_date, cfg)
                result = await fn(spec, report_date, cfg, run_override)
    except Exception:
        discard_task_facts(tokens)
        raise
    rows = take_task_facts(tokens)
    if isinstance(result, dict):
        _store_task_facts(result, spec.report_id, report_date, rows)
        try:
            from proverka.data_checks import collect_data_checks

            result["data_checks"] = collect_data_checks()
        except Exception:
            pass
    return result
