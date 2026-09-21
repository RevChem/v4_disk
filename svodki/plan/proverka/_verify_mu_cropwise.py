#!/usr/bin/env python3
"""Быстрая проверка MU: svodka vs Cropwise (API fallback)."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from pathlib import Path

PLAN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLAN))

from proverka.reconcile import load_env, _shift_window_iso, _firm_names  # noqa: E402
from proverka.reports_config import REPORTS  # noqa: E402
from proverka.season_cropwise import (  # noqa: E402
    ShiftWindowCache,
    compute_season_aware_cropwise,
    work_type_ids_for_report,
)
from proverka.cropwise_ui import fetch_work_report_resilient  # noqa: E402
from proverka.svodka_facts import compute_svodka_facts  # noqa: E402
from proverka.season_cropwise import preferred_ui_session  # noqa: E402


def main() -> int:
    load_env()
    cookie = (os.environ.get("CROPWISE_SESSION_COOKIE") or "").strip()
    if not cookie:
        print("ERROR: CROPWISE_SESSION_COOKIE не задан", file=sys.stderr)
        return 1
    rd = date.fromisoformat(os.environ.get("REPORT_DATE", "2026-09-15"))
    spec = REPORTS["3"]
    t0, t1, _ = _shift_window_iso(spec, rd)
    wt_ids = work_type_ids_for_report("3")
    sv = asyncio.run(compute_svodka_facts(spec, rd))
    sp = sv.get("season_policy") or {}
    task_years = list(sp.get("task_season_years") or [])
    pref = preferred_ui_session(spec.season_rule, task_years)
    work = fetch_work_report_resilient(
        cookie, t0, t1, wt_ids, list(spec.fact_column_hints),
        attempts=2, delay_sec=1.5, preferred_ui_session=pref,
    )
    ui_sess = work.get("ui_session_seasonValue")
    cache = ShiftWindowCache.build(t0, t1, wt_ids, log_prefix="[verify] ")
    sc = compute_season_aware_cropwise(
        work["body"],
        _firm_names(spec),
        list(spec.fact_column_hints),
        wt_ids,
        t0,
        t1,
        ui_sess,
        task_season_years=task_years,
        season_rule=spec.season_rule,
        shift_cache=cache,
    )
    sv_ha = float(sv.get("total_ha") or 0)
    cw_ha = float(sc.get("cropwise_total_ha") or 0)
    seasons = sc.get("seasons") or {}
    print(f"date={rd} ui_session={ui_sess} preferred={pref} task_years={task_years}")
    print(f"svodka={sv_ha:.2f} cropwise={cw_ha:.2f} diff={sv_ha - cw_ha:+.2f}")
    print(f"api_fallback={sc.get('cropwise_api_fallback')} blocked={sc.get('ui_session_blocked')}")
    for y in ("2026", "2027"):
        b = seasons.get(y) or {}
        print(f"  season {y}: {b.get('total_ha')} ({b.get('source')})")
    return 0 if abs(sv_ha - cw_ha) <= 5.0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
