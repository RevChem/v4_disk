#!/usr/bin/env python3
"""Run report 5 logic and compare with independent Cropwise API aggregation."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

# run from svodki/plan
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# load .env from project root
env_path = ROOT.parents[1] / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

os.environ.setdefault("REPORT_DATE", "2026-09-11")
REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

import httpx
import requests

import importlib
m5 = importlib.import_module("5_main_fixed")
# sync module-level dates after import (5_main reads REPORT_DATE at load)
m5.REPORT_DATE = REPORT_DATE
m5.SEASON_YEAR = REPORT_DATE.year
m5.SEASON_TASK_YEAR_STR = str(m5.SEASON_YEAR + 1)


def fetch_work_json_reference(day_start: datetime, cutoff: datetime) -> dict:
    """UI endpoint /machines/reports/work.json — needs CROPWISE_SESSION_COOKIE."""
    from cropwise_web_session import cookie_setup_hint, fetch_work_report_json, make_web_session

    t0 = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    t1 = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    web_session, source = make_web_session()
    if source == "none":
        return {
            "status": 401,
            "error": "No web session. " + cookie_setup_hint().replace("\n", " "),
            "auth": "missing_cookie",
        }
    result = fetch_work_report_json(
        web_session,
        time_from=t0,
        time_to=t1,
        work_type_ids=m5.ALL_WT_IDS,
    )
    result["auth"] = source
    return result


async def fetch_simple_task_area_ref(
    day_start: datetime,
    cutoff: datetime,
    field_to_firm: dict[int, str],
) -> dict[str, float]:
    """Independent ref: machine_tasks season=2027 + mtfmi covered_area (no hourly cut)."""
    from machine_tasks_batch_fetch import fetch_machine_tasks_by_wt_async
    from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks

    token = m5.API_TOKEN
    base = m5.BASE_URL
    headers = {"X-User-Api-Token": token}
    sem = asyncio.Semaphore(m5.SEMAPHORE_LIMIT)
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        tasks = await fetch_machine_tasks_by_wt_async(
            m5.fetch_all_pages,
            client,
            base,
            m5.ALL_WT_IDS,
            {"end_time_gt_eq": day_start_str, "end_time_lt": cutoff_str},
            sem,
            log_prefix="[ref] ",
        )
        tasks = m5._filter_tasks_by_cropwise_season(tasks)
        mtfmi = await fetch_mtfmi_map_for_tasks(
            client, sem, base, [t["id"] for t in tasks], log_prefix="[ref] "
        )

    firm_area: dict[str, float] = {}
    for t in tasks:
        items = mtfmi.get(t["id"], [])
        for item in items:
            fid = item.get("field_id")
            if fid is None:
                continue
            firm = field_to_firm.get(int(fid))
            if not firm:
                continue
            area = float(item.get("covered_area") or 0)
            if area <= 0:
                continue
            firm_area[firm] = firm_area.get(firm, 0.0) + area
    return {k: round(v, 2) for k, v in sorted(firm_area.items())}


async def run_report5_json(report_date: date) -> dict:
    day_start, cutoff = m5.resolve_time_range(report_date, m5.DAY_START_HOUR, m5.DAY_END_HOUR)
    season_start = datetime(m5.SEASON_YEAR, 1, 1, tzinfo=m5.TZ_MSK)

    crops = m5.get_all_from_id("crops")
    crop_names_by_id = {
        int(c["id"]): str(c.get("name") or c.get("title") or "").strip()
        for c in crops if c.get("id") is not None
    }

    all_history: list[dict] = []
    for crop_year in (m5.SEASON_YEAR, m5.SEASON_YEAR + 1):
        all_history.extend(m5.get_all_from_id("history_items", {"year": str(crop_year)}))

    field_crop_by_id = m5.build_field_to_crop(
        crop_map=crop_names_by_id,
        history_items=all_history,
        report_date=report_date,
        period_start=date(m5.SEASON_YEAR, 1, 1),
        skip_crops=m5.SKIP_CROPS,
        season_year=m5.SEASON_YEAR + 1,
    )

    period_start = date(m5.SEASON_YEAR, 1, 1)
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

    headers = {"X-User-Api-Token": m5.API_TOKEN}
    sem = asyncio.Semaphore(m5.SEMAPHORE_LIMIT)
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)

    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        tasks_today_seed, tasks_season_seed, mtfmi_seed = await m5.fetch_all_tasks_and_mtfmi(
            client, sem, day_start, cutoff, season_start, m5.ALL_WT_IDS, label="Посев",
        )
        tasks_today_seed = m5._filter_tasks_by_cropwise_season(tasks_today_seed)
        tasks_season_seed = m5._filter_tasks_by_cropwise_season(tasks_season_seed)

        from report_resources_fetch import load_report_resources

        all_tasks = tasks_today_seed + tasks_season_seed
        field_to_firm, machines_map, _ = await load_report_resources(
            client, m5.BASE_URL, sem, all_tasks, mtfmi_seed,
            m5.get_all_from_id, m5._build_field_to_firm,
        )

    seeding_details = m5.build_all_firm_stats(
        tasks_today_seed, tasks_season_seed, mtfmi_seed,
        field_to_firm, day_start, cutoff, report_date,
        m5.ALL_WT_IDS, m5.MIN_DETAIL_AREA_HA, crop_names_by_id,
        min_count_area_ha=m5.MIN_DETAIL_AREA_HA,
        field_crop_by_id=field_crop_by_id,
        available_field_ids=available_field_ids,
        fact_by_all_fields=True,
    )

    report5_by_firm = {
        firm: float(details.get("area_today") or 0)
        for firm, details in seeding_details.items()
    }
    total_report5 = round(sum(report5_by_firm.values()), 2)

    simple_ref = await fetch_simple_task_area_ref(day_start, cutoff, field_to_firm)
    total_simple = round(sum(simple_ref.values()), 2)

    work_json = fetch_work_json_reference(day_start, cutoff)

    return {
        "report_date": report_date.isoformat(),
        "window": {
            "from": day_start.isoformat(),
            "to": cutoff.isoformat(),
        },
        "season_task_filter": m5.SEASON_TASK_YEAR_STR,
        "work_type_ids": sorted(m5.ALL_WT_IDS),
        "report5_area_today_by_firm": report5_by_firm,
        "report5_total_ha": total_report5,
        "api_ref_mtfmi_covered_area_by_firm": simple_ref,
        "api_ref_total_ha": total_simple,
        "work_json_endpoint": work_json,
        "diff_report5_vs_api_ref": {
            firm: round(report5_by_firm.get(firm, 0) - simple_ref.get(firm, 0), 2)
            for firm in sorted(set(report5_by_firm) | set(simple_ref))
        },
        "total_diff_ha": round(total_report5 - total_simple, 2),
        "tasks_today_count": len(tasks_today_seed),
    }


def main() -> int:
    print(f"=== Compare report 5 vs Cropwise ref | {REPORT_DATE} | season tasks {m5.SEASON_TASK_YEAR_STR} ===")
    result = asyncio.run(run_report5_json(REPORT_DATE))
    out_dir = Path(os.environ.get("COMPARE_OUT_DIR", "/tmp"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"compare_report5_{REPORT_DATE.isoformat()}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n--- Report 5 area_today by firm (ha) ---")
    for firm, ha in sorted(result["report5_area_today_by_firm"].items(), key=lambda x: -x[1]):
        if ha > 0:
            print(f"  {firm:30s} {ha:10.2f}")

    print(f"\nReport 5 TOTAL: {result['report5_total_ha']} ha")
    print(f"API ref TOTAL (mtfmi covered_area): {result['api_ref_total_ha']} ha")
    print(f"Diff TOTAL: {result['total_diff_ha']} ha")

    wj = result["work_json_endpoint"]
    print(f"\nwork.json endpoint: status={wj.get('status')}")
    if wj.get("status") == 200:
        print("  work.json доступен — см. JSON файл")
    else:
        print(f"  {str(wj.get('error', wj))[:200]}")

    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
