#!/usr/bin/env python3
"""
Проверка платформы: сезоны + культуры + сверка сводок.

  REPORT_DATE=2026-09-11 python3 proverka/verify_platform.py
  python3 proverka/verify_platform.py --season-seeding 2026
  python3 proverka/verify_platform.py --season-seeding 2026,2027
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

PLAN_DIR = Path(__file__).resolve().parent.parent
if str(PLAN_DIR) not in sys.path:
    sys.path.insert(0, str(PLAN_DIR))

from proverka.reconcile import load_env, session_cookie  # noqa: E402
from proverka.runner import run_proverka_batch  # noqa: E402
from report_platform import bootstrap_platform, platform_summary  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify platform: seasons + cross-check")
    parser.add_argument("--date", default=os.environ.get("REPORT_DATE", date.today().isoformat()))
    parser.add_argument("--season-seeding", default="", help="2026 or 2026,2027 for report 5")
    parser.add_argument("--hide-crop", nargs=3, metavar=("GROUP", "FIRM", "CROP"), action="append", default=[])
    parser.add_argument("--svodki-only", action="store_true", default=True)
    parser.add_argument("--out", default=str(PLAN_DIR.parents[1] / "proverka_verify"))
    args = parser.parse_args()

    load_env()
    report_date = date.fromisoformat(args.date)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    bootstrap_platform(report_date, temp_dir=out_dir)
    print("Platform:", json.dumps(platform_summary(), ensure_ascii=False, indent=2))

    season_overrides = {}
    if args.season_seeding.strip():
        years = [int(x.strip()) for x in args.season_seeding.split(",") if x.strip().isdigit()]
        if years:
            season_overrides["5"] = {
                "task_season_years": years,
                "history_years": years if len(years) > 1 else [years[0], years[0] + 1],
            }

    report_ids = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "11"]
    result = run_proverka_batch(
        report_date,
        report_ids,
        out_dir,
        cookie=session_cookie(),
        compare_cropwise=not args.svodki_only,
        write_excel=True,
        write_json=True,
        season_overrides=season_overrides or None,
    )

    cc = result.get("cross_check") or {}
    summary = cc.get("summary") or {}
    print(f"\nCross-check: OK={summary.get('ok')} DIFF={summary.get('diff')} partial={summary.get('partial')}")
    for row in (cc.get("operations") or [])[:12]:
        if row.get("group_name", "").startswith("4.0"):
            print(
                f"  {row['group_name']}: {row.get('status')} totals={row.get('totals')} "
                f"max_diff={row.get('max_diff_ha')}"
            )

    seed = next((r for r in (cc.get("operations") or []) if r.get("group_name") == "4.0 Посев"), None)
    if seed and args.season_seeding:
        print(f"\nПосев ({args.season_seeding}): {seed}")

    print(f"\nExcel: {result.get('excel_path')}")
    return int(result.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
