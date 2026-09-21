#!/usr/bin/env python3
"""Quick test: season overrides vs svodka + Cropwise UI."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

PLAN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLAN))

from proverka.reconcile import _firm_names, load_env, reconcile_one, session_cookie  # noqa: E402
from proverka.reports_config import REPORTS  # noqa: E402
from proverka.svodka_facts import compute_svodka_facts  # noqa: E402
from report_platform import bootstrap_platform  # noqa: E402


async def svodka_total(override: dict | None) -> float:
    spec = REPORTS["5"]
    r = await compute_svodka_facts(spec, REPORT_DATE, run_override=override)
    return float(r["total_ha"])


def main() -> None:
    global REPORT_DATE
    REPORT_DATE = date(2026, 9, 11)
    load_env()
    bootstrap_platform(REPORT_DATE)

    print("=== SVODKA 5: season overrides ===")
    cases = [
        ("default (2027)", None),
        ("2026 only", {"task_season_years": [2026], "history_years": [2026, 2027]}),
        ("2026+2027", {"task_season_years": [2026, 2027], "history_years": [2026, 2027]}),
    ]
    for label, ov in cases:
        total = asyncio.run(svodka_total(ov))
        print(f"  {label}: {total:.2f} га")

    print("\n=== CROPWISE UI work.json (без фильтра season) ===")
    cookie = session_cookie()
    if not cookie:
        print("  NO COOKIE — пропуск UI")
        return
    spec = REPORTS["5"]
    result = reconcile_one(spec, REPORT_DATE, cookie, _firm_names(spec))
    w = result["cropwise_work_report"]
    print(f"  HTTP {w.get('status')} ok={w.get('ok')}")
    parsed = result.get("cropwise_parsed") or {}
    cmp_ = result.get("comparison") or {}
    print(f"  UI total: {parsed.get('fact_total_ha')}  col={parsed.get('fact_column')!r}")
    print(f"  UI error/blank: {parsed.get('error')}")
    print(f"  Svodka total: {result['svodka']['total_ha']}")
    print(f"  Diff: {cmp_.get('total_diff_ha')}")
    body = w.get("body") if w.get("ok") else None
    if isinstance(body, dict):
        print(f"  reportBlank={body.get('reportBlank')}")


if __name__ == "__main__":
    main()
