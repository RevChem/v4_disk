#!/usr/bin/env python3
"""Проверка календаря сезонов: 1.1 MУ только 2027 → ~1275.61 га."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path

PLAN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLAN))

REPORT_DATE = date.fromisoformat(os.environ.get("REPORT_DATE", "2026-09-11"))


def main() -> int:
    upload = Path(os.environ.get("UPLOAD_DIR", "/app/uploads"))
    upload.mkdir(parents=True, exist_ok=True)
    path = upload / "season_ranges.json"
    store = {
        "calendar_seasons": {
            "2026": {"period_start": "2025-07-01", "period_end": "2026-07-01"},
            "2027": {"period_start": "2026-07-01", "period_end": "2027-07-01"},
        },
        "by_group": {
            "1.1 Внесение минеральных удобрений": {
                "season_years": [2027],
                "season_year": 2027,
            },
        },
    }
    path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    os.environ["SVODKI_SEASON_RANGES_PATH"] = str(path)
    os.environ["SVODKI_UPLOAD_DIR"] = str(upload)
    os.environ["REPORT_DATE"] = REPORT_DATE.isoformat()

    from season_calendar import load_calendar_seasons, merged_period_for_years

    cal = load_calendar_seasons(REPORT_DATE)
    ps, pe = merged_period_for_years([2027], REPORT_DATE, cal)
    print(f"merged 2027 only: {ps} – {pe}")

    from proverka.extractors import compute_report_facts
    from proverka.reports_config import REPORTS

    facts = asyncio.run(compute_report_facts(REPORTS["2"], REPORT_DATE))
    ops = facts.get("operations") or {}
    fert = ops.get("1.1 Внесение минеральных удобрений") or {}
    by_firm = fert.get("area_today_by_firm") or {}
    total = round(sum(float(v or 0) for v in by_firm.values()), 2)
    print(f"Report 2 · 1.1 total: {total} га")
    for firm, ha in sorted(by_firm.items()):
        if float(ha or 0):
            print(f"  {firm}: {float(ha):.2f}")
    target = 1275.61
    diff = abs(total - target)
    print(f"Target Cropwise UI: {target}, |diff|={diff:.2f}")
    return 0 if diff < 5 else 1


if __name__ == "__main__":
    raise SystemExit(main())
