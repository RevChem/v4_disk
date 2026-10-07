"""Конфигурация сводок для сверки с Cropwise work.json (из report_catalog)."""
from __future__ import annotations

from dataclasses import dataclass

from proverka.task_names import tasks_for_groups
from report_catalog import REPORT_CATALOG


@dataclass(frozen=True)
class ReportSpec:
    report_id: str
    title: str
    module: str
    work_groups: tuple[str, ...]
    season_rule: str  # none | report_year | year_plus_one | both_years | plow_year_plus_one
    fact_column_hints: tuple[str, ...]
    fact_by_all_fields: bool = False
    cropwise_comparable: bool = True
    cropwise_per_operation: bool = False
    cross_check_only: bool = False


def _entry_to_spec(entry) -> ReportSpec:
    title = entry.title
    for prefix in (f"{entry.id}.", f"{entry.id} "):
        if title.startswith(prefix):
            title = title[len(prefix) :].strip()
            break
    return ReportSpec(
        report_id=entry.id,
        title=title,
        module=entry.module or entry.script.replace(".py", ""),
        work_groups=entry.work_groups,
        season_rule=entry.season_rule,
        fact_column_hints=tasks_for_groups(entry.work_groups) or entry.fact_column_hints,
        fact_by_all_fields=entry.fact_by_all_fields,
        cropwise_comparable=entry.cropwise_comparable or entry.cropwise_per_operation,
        cropwise_per_operation=entry.cropwise_per_operation,
        cross_check_only=getattr(entry, "cross_check_only", False),
    )


REPORTS: dict[str, ReportSpec] = {
    e.id: _entry_to_spec(e)
    for e in REPORT_CATALOG
    if e.module and (
        e.cropwise_comparable or e.cropwise_per_operation or e.cross_check_only
    )
}


def list_report_ids() -> list[str]:
    return sorted(REPORTS.keys(), key=lambda x: (len(x), x))
