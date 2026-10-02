"""Факт за смену — логика соответствующего модуля сводки (как PDF/Excel)."""
from __future__ import annotations

from datetime import date
from typing import Any

from proverka.extractors import compute_report_facts
from proverka.reports_config import ReportSpec


async def compute_svodka_facts(
    spec: ReportSpec,
    report_date: date,
    run_override: dict | None = None,
) -> dict[str, Any]:
    return await compute_report_facts(spec, report_date, run_override=run_override)
