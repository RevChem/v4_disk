#!/usr/bin/env python3
"""work.json + API v3 по сезонам, агрофирмам и задачам (MU, опрыскивание, …)."""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PLAN = Path(__file__).resolve().parent.parent
if str(PLAN) not in sys.path:
    sys.path.insert(0, str(PLAN))

from plans import FIRM_ORDER, TZ_MSK  # noqa: E402
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
from proverka.reports_config import REPORTS  # noqa: E402
from proverka.season_cropwise import (  # noqa: E402
    build_field_to_firm,
    build_seasons_block,
    compute_api_by_season,
    get_all,
    work_type_ids_for_report,
)

REPORT_SLUG: dict[str, str] = {
    "3": "mu",
    "4": "harrow",
    "5": "seed",
    "6": "spray",
    "7": "disk",
    "8": "cult",
    "9": "plow",
    "11": "harvest",
}


def comparable_report_ids() -> list[str]:
    return sorted(
        (rid for rid, spec in REPORTS.items() if spec.cropwise_comparable),
        key=lambda x: (len(x), x),
    )


def parse_work_json_regions(
    body: dict,
    hints: list[str],
    *,
    by_subdivision_verified: dict[str, float] | None = None,
) -> list[dict]:
    cols = body.get("regionColumns") or []
    fact_col = _pick_fact_column(cols, hints)
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


def pull_report(
    report_id: str,
    report_date: date,
    out_path: Path | None = None,
    *,
    ctx: dict | None = None,
) -> dict:
    spec = REPORTS[report_id]
    wt_ids = work_type_ids_for_report(report_id)
    hints = list(spec.fact_column_hints)

    shift = report_date - timedelta(days=1)
    day_start = datetime(shift.year, shift.month, shift.day, 6, 0, tzinfo=TZ_MSK)
    cutoff = datetime(report_date.year, report_date.month, report_date.day, 6, 0, tzinfo=TZ_MSK)
    t0 = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    t1 = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    if ctx is None:
        cookie = session_cookie()
        if not cookie:
            raise SystemExit("ERROR: no cookie")
        web, csrf, _, _ = warm_web_session(cookie)
        page_html = web.get(WORK_PAGE_URL, timeout=60).text
        m = re.search(r'seasonValue":(\d{4})', html.unescape(page_html))
        ui_season = int(m.group(1)) if m else None
        ctx = {
            "web": web,
            "csrf": csrf,
            "ui_season": ui_season,
            "field_to_firm": build_field_to_firm(),
        }
    web = ctx["web"]
    csrf = ctx["csrf"]
    ui_season = ctx["ui_season"]
    field_to_firm = ctx["field_to_firm"]

    wh = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
    }

    api_by_season, valid_task_ids = compute_api_by_season(
        wt_ids, t0, t1, field_to_firm=field_to_firm,
    )

    work_body = web.post(
        WORK_JSON_URL,
        json={"from_time": t0, "to_time": t1, "work_type_ids": ",".join(map(str, wt_ids))},
        headers=wh,
        timeout=120,
    ).json()
    work_parsed = parse_work_report_body(
        work_body, FIRM_ORDER, hints, valid_task_ids=valid_task_ids,
    )
    ui_verified = aggregate_ui_task_rows(
        work_parsed.get("detail_rows") or [], valid_task_ids,
    )

    ui_by_firm = ui_verified.get("fact_by_firm") or {}
    ui_total_session = float(ui_verified.get("fact_total_ha") or 0)
    api_2026_total = float((api_by_season.get("2026") or {}).get("total_ha") or 0)
    api_2027_total = float((api_by_season.get("2027") or {}).get("total_ha") or 0)

    firm_comparison = []
    all_firms = sorted(
        set(FIRM_ORDER)
        | set(ui_by_firm)
        | set((api_by_season.get("2027") or {}).get("by_firm") or {})
        | set((api_by_season.get("2026") or {}).get("by_firm") or {})
    )
    for firm in all_firms:
        ui_ha = float(ui_by_firm.get(firm) or 0)
        a27 = float((api_by_season.get("2027") or {}).get("by_firm", {}).get(firm) or 0)
        a26 = float((api_by_season.get("2026") or {}).get("by_firm", {}).get(firm) or 0)
        if ui_ha or a27 or a26:
            firm_comparison.append({
                "firm": firm,
                "ui_session_season_ha": round(ui_ha, 2),
                "api_v3_season_2027_ha": round(a27, 2),
                "api_v3_season_2026_ha": round(a26, 2),
            })

    seasons = build_seasons_block(ui_season, ui_verified, api_by_season)
    combined_ui = round(
        float(seasons["2026"]["total_ha"]) + float(seasons["2027"]["total_ha"]),
        2,
    )

    result = {
        "report_id": report_id,
        "title": spec.title,
        "date": report_date.isoformat(),
        "window": [t0, t1],
        "work_type_ids": wt_ids,
        "ui_session_seasonValue": ui_season,
        "seasons": seasons,
        "totals": {
            "season_2027_ha": seasons["2027"]["total_ha"],
            "season_2026_ha": seasons["2026"]["total_ha"],
            "both_seasons_ha": combined_ui,
            "ui_session_season_ha": round(ui_total_session, 2),
            "api_season_2027_ha": round(api_2027_total, 2),
            "api_season_2026_ha": round(api_2026_total, 2),
            "api_both_seasons_ha": round(api_2026_total + api_2027_total, 2),
            "ui_session_plus_api_other_season_ha": round(
                ui_total_session + api_2026_total
                if ui_season == 2027
                else ui_total_session + api_2027_total,
                2,
            ),
        },
        "notes": {
            "work_json": (
                f"UI work.json при session seasonValue={ui_season}; "
                "verified = сумма #task_id из API."
            ),
            "orphan_ui_tasks": work_parsed.get("ui_orphan_tasks") or {},
            "both_seasons": (
                "2026+2027: UI показывает один сезон в сессии; "
                "другой сезон — из API v3 by machine_task.season."
            ),
        },
        "firm_comparison": firm_comparison,
        "work_json": {
            "fact_column": work_parsed.get("fact_column"),
            "total_ha_raw_cell": work_parsed.get("fact_total_ha"),
            "total_ha_verified": ui_total_session,
            "by_firm": ui_by_firm,
            "by_regions_raw": parse_work_json_regions(
                work_body, hints,
                by_subdivision_verified=ui_verified.get("by_subdivision"),
            ),
            "task_rows": ui_verified.get("task_rows") or [],
        },
        "api_v3": {
            "by_season": api_by_season,
            "total_ha": round(sum(s["total_ha"] for s in api_by_season.values()), 2),
        },
    }

    if out_path is not None:
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def init_pull_context() -> dict:
    cookie = session_cookie()
    if not cookie:
        raise SystemExit("ERROR: no cookie")
    web, csrf, _, ui_season = warm_web_session(cookie)
    if ui_season is None:
        page_html = html.unescape(web.get(WORK_PAGE_URL, timeout=60).text)
        m = re.search(r'seasonValue":(\d{4})', page_html)
        ui_season = int(m.group(1)) if m else None
    return {
        "web": web,
        "csrf": csrf,
        "ui_season": ui_season,
        "field_to_firm": build_field_to_firm(),
    }


def pull_batch(
    report_ids: list[str],
    report_date: date,
    out_dir: Path,
    *,
    combined_path: Path | None = None,
) -> dict:
    ctx = init_pull_context()
    reports: dict[str, dict] = {}
    for rid in report_ids:
        slug = REPORT_SLUG.get(rid, f"r{rid}")
        out = out_dir / f"{slug}_seasons_{report_date.isoformat()}_detailed.json"
        reports[rid] = pull_report(rid, report_date, out, ctx=ctx)
        print(f"Saved: {out}")

    summary = {
        rid: {
            "title": reports[rid]["title"],
            "season_2027_ha": reports[rid]["totals"]["season_2027_ha"],
            "season_2026_ha": reports[rid]["totals"]["season_2026_ha"],
            "both_seasons_ha": reports[rid]["totals"]["both_seasons_ha"],
        }
        for rid in report_ids
    }
    combined = {
        "date": report_date.isoformat(),
        "window": reports[report_ids[0]]["window"],
        "ui_session_seasonValue": ctx["ui_season"],
        "report_ids": report_ids,
        "reports": reports,
        "summary": summary,
        "grand_total": {
            "season_2027_ha": round(sum(r["season_2027_ha"] for r in summary.values()), 2),
            "season_2026_ha": round(sum(r["season_2026_ha"] for r in summary.values()), 2),
            "both_seasons_ha": round(sum(r["both_seasons_ha"] for r in summary.values()), 2),
        },
        "notes": {
            "seasons": (
                f"UI work.json verified — сезон сессии ({ctx['ui_season']}); "
                "второй сезон — API v3 machine_task.season. "
                "Операции не пересекаются по work_type_id; сумма по отчётам — "
                "сумма площадей разных видов работ."
            ),
        },
    }
    if combined_path is not None:
        combined_path.write_text(
            json.dumps(combined, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Saved combined: {combined_path}")
    return combined


def _print_report_summary(result: dict) -> None:
    print(f"\n=== {result['title']} | {result['date']} | UI session {result['ui_session_seasonValue']} ===")
    for year in ("2027", "2026"):
        block = result["seasons"][year]
        print(f"  {year} ({block['source']}): {block['total_ha']} га")
    print(f"  Оба сезона: {result['totals']['both_seasons_ha']} га")
    orphans = result["notes"].get("orphan_ui_tasks") or {}
    if orphans:
        print(f"  исключены фантомы: {orphans}")


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="", help="ID одной сводки (3, 6, 9)")
    parser.add_argument(
        "--reports",
        default="",
        help="Пакет ID через запятую или all — все агрооперации (3–11)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Все cropwise-сводки: 3,4,5,6,7,8,9,11",
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--combined-out",
        default="",
        help="Общий JSON для --reports (по умолчанию work_seasons_<date>_batch.json)",
    )
    args = parser.parse_args()

    report_date = date.fromisoformat(args.date)
    out_dir = PLAN.parents[1]

    if args.all or args.reports:
        if args.all:
            report_ids = comparable_report_ids()
        else:
            raw = args.reports.strip().lower()
            report_ids = (
                comparable_report_ids()
                if raw in ("all", "*")
                else [x.strip() for x in args.reports.split(",") if x.strip()]
            )
        default_name = (
            f"all_operations_seasons_{report_date.isoformat()}.json"
            if args.all or args.reports.strip().lower() in ("all", "*")
            else f"work_seasons_{report_date.isoformat()}_batch.json"
        )
        combined = (
            Path(args.combined_out)
            if args.combined_out
            else out_dir / default_name
        )
        batch = pull_batch(report_ids, report_date, out_dir, combined_path=combined)
        gt = batch["grand_total"]
        print(f"\n=== Сводка {report_date} | session {batch['ui_session_seasonValue']} ===")
        for rid, row in batch["summary"].items():
            print(
                f"  [{rid}] {row['title']:30s}  "
                f"2027={row['season_2027_ha']:>10}  "
                f"2026={row['season_2026_ha']:>10}  "
                f"sum={row['both_seasons_ha']:>10}"
            )
        print(
            f"\n  ИТОГО все операции: 2027={gt['season_2027_ha']}  "
            f"2026={gt['season_2026_ha']}  sum={gt['both_seasons_ha']} га"
        )
        return 0

    report_id = args.report or "6"
    slug = REPORT_SLUG.get(report_id, f"r{report_id}")
    out = Path(args.out) if args.out else out_dir / f"{slug}_seasons_{report_date.isoformat()}_detailed.json"

    result = pull_report(report_id, report_date, out)
    print(f"Saved: {out}")
    _print_report_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
