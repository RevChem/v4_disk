"""Политика сезонов — из season_ranges.json и переопределений запуска."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from proverka.reports_config import ReportSpec
from proverka.season_runtime import resolve_group_season, resolve_report_season_config


@dataclass(frozen=True)
class SeasonPolicy:
    report_id: str
    report_year: int
    task_season_years: tuple[int, ...] | None
    task_season_plow_only: bool
    history_years: tuple[int, ...]
    period_start: str | None
    period_end: str | None
    label: str
    cropwise_ui_filters_season: bool = False

    def task_season_label(self) -> str:
        if self.task_season_plow_only and self.task_season_years:
            ys = "+".join(map(str, self.task_season_years))
            return f"вспашка: season∈{{{ys}}}; глубокорыхление: все"
        if not self.task_season_years:
            return "machine_task.season — все"
        if len(self.task_season_years) == 1:
            return f"machine_task.season = {self.task_season_years[0]}"
        return f"machine_task.season ∈ {{{', '.join(map(str, self.task_season_years))}}}"

    def history_label(self) -> str:
        if not self.history_years:
            return "history_items — по умолчанию модуля"
        years = "+".join(map(str, self.history_years))
        if len(self.history_years) > 1:
            return (
                f"history_items.year: {years} "
                f"(культура полей — приоритет {self.history_years[-1]}, как Cropwise UI)"
            )
        return f"history_items.year: {years}"


def resolve_season_policy(
    spec: ReportSpec,
    report_date: date,
    run_override: dict[str, Any] | None = None,
) -> SeasonPolicy:
    cfg = resolve_report_season_config(spec, report_date, run_override)
    y = report_date.year
    task_list = cfg.get("task_season_years") or []
    task_years: tuple[int, ...] | None = tuple(task_list) if task_list else None
    hist = tuple(cfg.get("history_years") or (y,))
    plow_only = bool(cfg.get("task_season_plow_only"))

    parts = [
        f"Сводка {spec.report_id}",
        cfg.get("period_start") and cfg.get("period_end")
        and f"период {cfg['period_start']}–{cfg['period_end']}",
        SeasonPolicy("", y, task_years, plow_only, hist, None, None, "").task_season_label(),
        SeasonPolicy("", y, task_years, plow_only, hist, None, None, "").history_label(),
    ]
    if cfg.get("from_file"):
        parts.append("(+ season_ranges.json)")

    ui_filters = bool(task_years) and spec.season_rule != "none"
    return SeasonPolicy(
        spec.report_id,
        y,
        task_years,
        plow_only,
        hist,
        cfg.get("period_start"),
        cfg.get("period_end"),
        " · ".join(p for p in parts if p),
        cropwise_ui_filters_season=ui_filters,
    )


def resolve_group_season_policy(
    group_name: str,
    report_date: date,
    run_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Сезоны одной операции (сводки 1/2) — season_unified."""
    cfg = resolve_group_season(group_name, report_date, run_override)
    rule = cfg.get("season_rule") or "none"
    task_list = cfg.get("task_season_years") or []
    task_years: tuple[int, ...] | None = tuple(task_list) if task_list else None
    hist = tuple(cfg.get("history_years") or (report_date.year,))
    plow_only = bool(cfg.get("task_season_plow_only"))
    policy = SeasonPolicy(
        group_name,
        report_date.year,
        task_years,
        plow_only,
        hist,
        cfg.get("period_start"),
        cfg.get("period_end"),
        "",
        cropwise_ui_filters_season=bool(task_years) and rule != "none",
    )
    return {
        "group_name": group_name,
        "season_rule": rule,
        "task_season_years": list(task_years) if task_years else None,
        "task_season_plow_only": plow_only,
        "history_years": list(hist),
        "period_start": cfg.get("period_start"),
        "period_end": cfg.get("period_end"),
        "task_season_label": policy.task_season_label(),
        "history_label": policy.history_label(),
        "label": (
            f"{group_name} · период {cfg.get('period_start')}–{cfg.get('period_end')} · "
            f"{policy.task_season_label()}"
        ),
        "cropwise_ui_filters_season": policy.cropwise_ui_filters_season,
    }


def attach_season_policy(
    result: dict[str, Any],
    spec: ReportSpec,
    report_date: date,
    run_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = resolve_season_policy(spec, report_date, run_override)
    result["season_policy"] = {
        "task_season_years": list(policy.task_season_years) if policy.task_season_years else None,
        "task_season_plow_only": policy.task_season_plow_only,
        "history_years": list(policy.history_years),
        "period_start": policy.period_start,
        "period_end": policy.period_end,
        "task_season_label": policy.task_season_label(),
        "history_label": policy.history_label(),
        "label": policy.label,
        "cropwise_ui_filters_season": policy.cropwise_ui_filters_season,
    }
    if policy.task_season_years and not policy.task_season_plow_only and len(policy.task_season_years) == 1:
        result["season_task_filter"] = str(policy.task_season_years[0])
    elif policy.task_season_plow_only and policy.task_season_years:
        result["season_task_filter"] = f"plow={'+'.join(map(str, policy.task_season_years))}"
    else:
        result["season_task_filter"] = None
    return result
