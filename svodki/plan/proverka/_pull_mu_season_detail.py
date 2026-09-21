#!/usr/bin/env python3
"""Внесение МУ: work.json + API v3 по сезонам, агрофирмам и задачам."""
from __future__ import annotations

import html
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

PLAN = Path(__file__).resolve().parent.parent
if str(PLAN) not in sys.path:
    sys.path.insert(0, str(PLAN))

from plans import API_TOKEN, BASE_URL, FIRM_ORDER, FOLDER_TO_FIRM, TZ_MSK  # noqa: E402
from proverka.cropwise_ui import (  # noqa: E402
    WORK_JSON_URL,
    WORK_PAGE_URL,
    _pick_fact_column,
    _row_metric_ha,
    aggregate_ui_task_rows,
    parse_work_report_body,
    warm_web_session,
)
from proverka.reconcile import load_env, session_cookie  # noqa: E402

DAY_START = datetime(2026, 9, 13, 6, 0, tzinfo=TZ_MSK)
CUTOFF = datetime(2026, 9, 14, 6, 0, tzinfo=TZ_MSK)
T0 = DAY_START.strftime("%Y-%m-%dT%H:%M:%S")
T1 = CUTOFF.strftime("%Y-%m-%dT%H:%M:%S")
WT = 55
HINTS = ["Минераль", "Удобрен", "Внесен"]
OUT = PLAN.parents[1] / "mu_seasons_2026-09-14_detailed.json"


def parse_dt(s):
    if not s:
        return None
    if isinstance(s, (int, float)):
        try:
            ts = float(s)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_MSK)
    return dt.astimezone(TZ_MSK)


def get_all(endpoint: str, extra: dict | None = None) -> list[dict]:
    headers = {"X-User-Api-Token": API_TOKEN}
    out: list[dict] = []
    from_id = 0
    while True:
        params = {**(extra or {}), "from_id": from_id, "limit": 1000}
        r = requests.get(f"{BASE_URL}/{endpoint}", headers=headers, params=params, timeout=120)
        r.raise_for_status()
        body = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        out.extend(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = last_id + 1
    return out


def build_field_to_firm() -> dict[int, str]:
    fields = get_all("fields")
    fgs = get_all("field_groups")
    gfs = get_all("group_folders")
    parent_map = {g["id"]: g.get("parent_id") for g in gfs}
    root_ids = {g["id"] for g in gfs if not g.get("parent_id")}
    fg_to_folder = {fg["id"]: fg.get("group_folder_id") for fg in fgs}
    field_to_firm: dict[int, str] = {}

    def resolve_root(rid):
        seen: set[int] = set()
        while rid and rid not in seen:
            seen.add(rid)
            if rid in root_ids:
                return rid
            rid = parent_map.get(rid)
        return None

    for f in fields:
        fid = f.get("id")
        gf = f.get("field_group_id")
        if fid is None or gf is None:
            continue
        root = resolve_root(fg_to_folder.get(gf))
        firm = FOLDER_TO_FIRM.get(root) if root else None
        if firm:
            field_to_firm[int(fid)] = firm
    return field_to_firm


def area_in_shift(items: list[dict]) -> float:
    total = 0.0
    for item in items:
        for entry in item.get("covered_area_hourly") or []:
            h = parse_dt(entry[0])
            if h and DAY_START <= h < CUTOFF:
                total += float(entry[1] or 0)
    return total


def area_by_field_in_shift(items: list[dict]) -> dict[int, float]:
    by_field: dict[int, float] = defaultdict(float)
    for item in items:
        fid = item.get("field_id")
        if fid is None:
            continue
        for entry in item.get("covered_area_hourly") or []:
            h = parse_dt(entry[0])
            if h and DAY_START <= h < CUTOFF:
                by_field[int(fid)] += float(entry[1] or 0)
    return dict(by_field)


def parse_work_json_regions(
    body: dict,
    *,
    by_subdivision_verified: dict[str, float] | None = None,
) -> list[dict]:
    cols = body.get("regionColumns") or []
    fact_col = _pick_fact_column(cols, HINTS)
    regions: list[dict] = []
    for row in body.get("regionRows") or []:
        name = (row.get("name") or "").strip()
        if name.lower() in ("всего", "итого"):
            continue
        parent_cell = round(_row_metric_ha(row, fact_col), 2)
        subs = []
        verified_sub_sum = 0.0
        for ch in row.get("children") or []:
            sub_name = (ch.get("name") or "").strip()
            cell = round(_row_metric_ha(ch, fact_col), 2)
            sub_key = f"{name} → {sub_name}"
            verified = round(float((by_subdivision_verified or {}).get(sub_key) or 0), 2)
            verified_sub_sum += verified
            subs.append({
                "name": sub_name,
                "fact_ha_raw_cell": cell,
                "fact_ha": verified if by_subdivision_verified is not None else cell,
            })
        regions.append({
            "name": name,
            "fact_ha_raw_parent_cell": parent_cell,
            "fact_ha": round(verified_sub_sum, 2) if by_subdivision_verified is not None else parent_cell,
            "subdivisions": subs,
        })
    return regions


def _sum_subtree_leaves(node: dict, fact_col: dict | None) -> float:
    children = node.get("children") or []
    if not children:
        return _row_metric_ha(node, fact_col)
    return sum(_sum_subtree_leaves(ch, fact_col) for ch in children)


def main() -> int:
    load_env()
    cookie = session_cookie()
    if not cookie:
        print("ERROR: no cookie", file=sys.stderr)
        return 1

    web, csrf, _, _ = warm_web_session(cookie)
    page_html = web.get(WORK_PAGE_URL, timeout=60).text
    m = re.search(r'seasonValue":(\d{4})', html.unescape(page_html))
    ui_season = int(m.group(1)) if m else None

    wh = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
    }
    field_to_firm = build_field_to_firm()
    fields_map = {
        int(f["id"]): f.get("name") or f.get("title") or f"field_{f['id']}"
        for f in get_all("fields")
    }
    machines_map = {
        int(m["id"]): m.get("name") or m.get("registration_number") or f"m#{m['id']}"
        for m in get_all("machines")
    }

    tasks = get_all(
        "machine_tasks",
        {"work_type_id": WT, "end_time_gt_eq": T0, "end_time_lt": T1},
    )
    valid_task_ids = {int(t["id"]) for t in tasks}

    work_body = web.post(
        WORK_JSON_URL,
        json={"from_time": T0, "to_time": T1, "work_type_ids": str(WT)},
        headers=wh,
        timeout=120,
    ).json()
    work_parsed = parse_work_report_body(
        work_body, FIRM_ORDER, HINTS, valid_task_ids=valid_task_ids,
    )
    ui_verified = aggregate_ui_task_rows(
        work_parsed.get("detail_rows") or [], valid_task_ids,
    )
    mtfmi_map: dict[int, list[dict]] = defaultdict(list)
    for i in range(0, len(tasks), 300):
        batch = [t["id"] for t in tasks[i : i + 300]]
        for row in get_all(
            "machine_task_field_mapping_items",
            {"machine_task_id": ",".join(map(str, batch))},
        ):
            mtfmi_map[row["machine_task_id"]].append(row)

    by_season_firm: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    task_rows: list[dict] = []
    for t in tasks:
        tid = int(t["id"])
        season = str(t.get("season") or "none")
        items = mtfmi_map.get(tid, [])
        area = area_in_shift(items)
        if area <= 0:
            continue
        by_field = area_by_field_in_shift(items)
        firm_areas: dict[str, float] = defaultdict(float)
        field_details = []
        for fid, ha in sorted(by_field.items(), key=lambda x: -x[1]):
            firm = field_to_firm.get(fid, "?")
            firm_areas[firm] += ha
            if ha >= 0.01:
                field_details.append({
                    "field_id": fid,
                    "field_name": fields_map.get(fid, str(fid)),
                    "firm": firm,
                    "area_ha": round(ha, 2),
                })
        for firm, ha in firm_areas.items():
            by_season_firm[season][firm] += ha
        task_rows.append({
            "task_id": tid,
            "season": season,
            "machine_id": t.get("machine_id"),
            "machine": machines_map.get(int(t["machine_id"])) if t.get("machine_id") else None,
            "start_time": t.get("start_time"),
            "end_time": t.get("end_time"),
            "covered_area_task_ha": round(float(t.get("covered_area") or 0), 2),
            "area_in_shift_ha": round(area, 2),
            "by_firm": {
                k: round(v, 2)
                for k, v in sorted(firm_areas.items(), key=lambda x: -x[1])
                if v >= 0.01
            },
            "fields": field_details,
        })

    task_rows.sort(key=lambda x: (-x["area_in_shift_ha"], x["season"], x["task_id"]))

    api_by_season: dict[str, dict] = {}
    for season in sorted(by_season_firm.keys()):
        firms = {
            k: round(v, 2)
            for k, v in sorted(by_season_firm[season].items(), key=lambda x: -x[1])
            if v >= 0.01
        }
        api_by_season[season] = {
            "by_firm": firms,
            "total_ha": round(sum(firms.values()), 2),
            "tasks_count": sum(1 for tr in task_rows if tr["season"] == season),
        }

    ui_by_firm = ui_verified.get("fact_by_firm") or {}
    ui_total_2027 = float(ui_verified.get("fact_total_ha") or 0)
    ui_total_2026 = float((api_by_season.get("2026") or {}).get("total_ha") or 0)
    api_2027 = (api_by_season.get("2027") or {}).get("by_firm") or {}
    api_2026 = (api_by_season.get("2026") or {}).get("by_firm") or {}
    all_firms = sorted(set(FIRM_ORDER) | set(ui_by_firm) | set(api_2027) | set(api_2026))
    firm_comparison = []
    for firm in all_firms:
        ui_ha = float(ui_by_firm.get(firm) or 0)
        a27 = float(api_2027.get(firm) or 0)
        a26 = float(api_2026.get(firm) or 0)
        if ui_ha or a27 or a26:
            firm_comparison.append({
                "firm": firm,
                "work_json_ha": round(ui_ha, 2),
                "api_v3_season_2027_ha": round(a27, 2),
                "api_v3_season_2026_ha": round(a26, 2),
                "ui_minus_api_2027_ha": round(ui_ha - a27, 2),
            })

    result = {
        "date": "2026-09-14",
        "window": [T0, T1],
        "work_type_ids": [WT],
        "ui_session_seasonValue": ui_season,
        "totals": {
            "ui_season_2027_ha": round(ui_total_2027, 2),
            "ui_season_2026_ha": round(ui_total_2026, 2),
            "ui_both_seasons_ha": round(ui_total_2027 + ui_total_2026, 2),
            "api_both_seasons_ha": round(
                sum(s.get("total_ha", 0) for s in api_by_season.values()), 2,
            ),
        },
        "notes": {
            "work_json": (
                f"Cropwise UI work.json; session seasonValue={ui_season}. "
                "fact_ha = сумма строк-заданий (#task_id), без фантомов work.json."
            ),
            "orphan_ui_tasks": work_parsed.get("ui_orphan_tasks") or {},
            "api_v3": (
                "API v3: machine_tasks (season) + mtfmi covered_area_hourly "
                "в окне смены 06:00–06:00."
            ),
        },
        "firm_comparison": firm_comparison,
        "work_json": {
            "fact_column": work_parsed.get("fact_column"),
            "total_ha_raw_cell": work_parsed.get("fact_total_ha"),
            "total_ha": ui_total_2027,
            "by_firm": ui_by_firm,
            "by_regions_raw": parse_work_json_regions(
                work_body,
                by_subdivision_verified=ui_verified.get("by_subdivision"),
            ),
            "detail_rows": ui_verified.get("task_rows") or [],
        },
        "api_v3": {
            "by_season": api_by_season,
            "total_ha": round(sum(s["total_ha"] for s in api_by_season.values()), 2),
            "tasks": task_rows,
        },
    }

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {OUT}")

    print("\n=== work.json UI (verified task rows, season 2027) ===")
    for firm, ha in sorted(ui_by_firm.items(), key=lambda x: -x[1]):
        print(f"  {firm:32s} {ha:10.2f}")
    print(f"  ИТОГО 2027: {ui_total_2027} га")
    orphans = work_parsed.get("ui_orphan_tasks") or {}
    if orphans:
        print(f"  исключены фантомы UI: {orphans}")
    print(f"  ИТОГО 2026 (API ЛПФ): {ui_total_2026} га")
    print(f"  ИТОГО 2026+2027: {round(ui_total_2027 + ui_total_2026, 2)} га")

    for season, block in api_by_season.items():
        print(f"\n=== API v3 season {season} ===")
        for firm, ha in block["by_firm"].items():
            print(f"  {firm:32s} {ha:10.2f}")
        print(f"  ИТОГО: {block['total_ha']} га  tasks: {block['tasks_count']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
