"""Сезоны и диапазоны дат для proverka — единый season_unified + season_ranges.json."""
from __future__ import annotations

import importlib
import json
import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator

from proverka.reports_config import ReportSpec

# Тематические сводки задают SEASON_START сами (как при генерации Excel),
# а не через season_unified period_start (14 мар / 1 янв / 1 июн и т.д.).
MODULES_OWN_SEASON_START = frozenset({
    "3_main_real",
    "4_main_fixed",
    "5_main_fixed",
    "6_main_3",
    "7_main__4_",
    "8_main_fixed",
    "9_main_fixed",
    "10_main_harvest",
    "11_main_harvest",
})

from season_unified import (
    inject_season_range_env as _inject_season_env,
    resolve_group_season_config,
    resolve_report_season_config as _resolve_report_cfg,
    season_env_for_group,
)


def _upload_dir() -> Path:
    raw = (os.environ.get("UPLOAD_DIR") or os.environ.get("SVODKI_UPLOAD_DIR") or "uploads").strip()
    p = Path(raw)
    return p if p.is_absolute() else Path.cwd() / p


def season_ranges_path() -> Path:
    configured = (os.environ.get("SVODKI_SEASON_RANGES_PATH") or "").strip()
    if configured:
        return Path(configured)
    return _upload_dir() / "season_ranges.json"


def load_season_range_overrides() -> dict[str, dict]:
    from season_unified import load_season_store

    return dict(load_season_store().get("by_group") or {})


def default_task_season_years(spec: ReportSpec, report_date: date) -> list[int] | None:
    from season_unified import default_task_years_for_report_rule

    years = default_task_years_for_report_rule(spec.season_rule, report_date)
    return years or None


def default_history_years(spec: ReportSpec, report_date: date) -> list[int]:
    from season_unified import default_history_years_for_report_rule

    return default_history_years_for_report_rule(spec.season_rule, report_date)


def resolve_report_season_config(
    spec: ReportSpec,
    report_date: date,
    run_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Итоговые сезоны: те же, что resolve_group_season_config у редактора и сводки."""
    cfg = _resolve_report_cfg(spec.work_groups, spec.season_rule, report_date, run_override)
    cfg["report_id"] = spec.report_id
    cfg["from_file"] = season_ranges_path().is_file()
    return cfg


def resolve_group_season(
    group_name: str,
    report_date: date,
    run_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Сезоны одной операции (сводки 1/2, per-op proverka)."""
    return resolve_group_season_config(group_name, report_date, run_override=run_override)


def _apply_env_from_config(spec: ReportSpec, report_date: date, cfg: dict[str, Any]) -> None:
    try:
        from report_platform import bootstrap_platform

        bootstrap_platform(report_date, do_reload_plans=False)
    except Exception:
        os.environ["REPORT_DATE"] = report_date.isoformat()

    env: dict[str, str] = {"REPORT_DATE": report_date.isoformat()}
    _inject_season_env(env, report_date)

    per_group = cfg.get("by_group") or {}
    groups = list(spec.work_groups) or list(per_group.keys())
    own_season = spec.module in MODULES_OWN_SEASON_START
    for gn in groups:
        gcfg = per_group.get(gn) or resolve_group_season_config(gn, report_date)
        genv = dict(season_env_for_group(gn, gcfg))
        if own_season:
            genv.pop("SVODKI_SEASON_START_DATE", None)
            genv.pop("SEASON_START_DATE", None)
            if spec.module == "11_main_harvest":
                genv.pop("SVODKI_COMBINE_SEASON_START", None)
        for k, v in genv.items():
            os.environ[k] = v

    if cfg.get("period_start") and not own_season:
        iso = str(cfg["period_start"]).split("T")[0]
        os.environ.setdefault("SVODKI_SEASON_START_DATE", iso)
        os.environ.setdefault("SEASON_START_DATE", iso)

    # Сводки 1/2 (cropwise_per_operation без work_groups): history_items и
    # task season резолвятся внутри модуля по каждой операции через
    # resolve_group_season_config. Глобальный SEASON_YEAR=год отчёта обрезает
    # history до одного года и обнуляет опрыскивание/посев в proverka.
    years = cfg.get("history_years") or []
    if years and not (spec.cropwise_per_operation and not spec.work_groups):
        ys = ",".join(str(y) for y in years)
        os.environ.setdefault("SVODKI_SEASON_YEARS", ys)
        os.environ.setdefault("SEASON_YEAR", str(years[-1]))

    task_years = cfg.get("task_season_years") or []
    if len(task_years) == 1:
        os.environ.setdefault("SEASON_TASK_YEAR_STR", str(task_years[0]))

    if cfg.get("shift_start_hour") is not None:
        os.environ["PROVERKA_DAY_START_HOUR"] = str(int(cfg["shift_start_hour"]))
    if cfg.get("shift_end_hour") is not None:
        os.environ["PROVERKA_DAY_END_HOUR"] = str(int(cfg["shift_end_hour"]))

    os.environ["PROVERKA_SEASON_CONFIG"] = json.dumps(cfg, ensure_ascii=False)


def reload_report_module(module_name: str) -> Any:
    mod = importlib.import_module(module_name)
    return importlib.reload(mod)


def _parse_season_start_env(*keys: str) -> date | None:
    for key in keys:
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            continue
        try:
            return date.fromisoformat(raw.split("T")[0])
        except ValueError:
            continue
    return None


def sync_module_season_start(
    module: Any,
    report_date: date,
    run_override: dict[str, Any] | None = None,
) -> date | None:
    """
    SEASON_START как при генерации Excel, не season_unified period_start.
    Явный run_override['period_start'] — единственное исключение для тематических сводок.
    """
    if run_override and run_override.get("period_start"):
        if hasattr(module, "SEASON_START"):
            module.SEASON_START = date.fromisoformat(
                str(run_override["period_start"]).split("T")[0]
            )
            return module.SEASON_START

    if hasattr(module, "sync_season_start_for_report"):
        module.sync_season_start_for_report(report_date)
        return getattr(module, "SEASON_START", None)

    name = getattr(module, "__name__", "")
    if not hasattr(module, "SEASON_START"):
        return None

    if hasattr(module, "REPORT_DATE"):
        module.REPORT_DATE = report_date

    if name == "11_main_harvest":
        default = date(report_date.year, 6, 10)
        if report_date < default:
            default = date(report_date.year - 1, 6, 10)
        parsed = _parse_season_start_env(
            "SVODKI_SEASON_START_DATE",
            "SEASON_START_DATE",
            "SVODKI_COMBINE_SEASON_START",
        )
        module.SEASON_START = parsed or default
        return module.SEASON_START

    defaults: dict[str, Callable[[date], date]] = {
        "3_main_real": lambda rd: date(rd.year, 3, 1),
        "6_main_3": lambda rd: date(rd.year, 1, 1),
        "7_main__4_": lambda rd: date(rd.year, 3, 15),
        "8_main_fixed": lambda rd: date(rd.year, 3, 25),
        "9_main_fixed": lambda rd: date(rd.year, 3, 14),
    }
    factory = defaults.get(name)
    if factory is not None:
        parsed = _parse_season_start_env("SVODKI_SEASON_START_DATE", "SEASON_START_DATE")
        module.SEASON_START = parsed or factory(report_date)
        return module.SEASON_START

    return getattr(module, "SEASON_START", None)


def apply_module_season_attrs(
    module: Any,
    report_date: date,
    cfg: dict[str, Any],
) -> None:
    """Патч атрибутов модуля после reload (многие сводки читают константы при import)."""
    if hasattr(module, "REPORT_DATE"):
        module.REPORT_DATE = report_date

    history = cfg.get("history_years") or []
    task_years = cfg.get("task_season_years") or []
    if hasattr(module, "SEASON_YEAR"):
        if task_years:
            module.SEASON_YEAR = int(task_years[-1])
        elif history:
            module.SEASON_YEAR = int(history[-1])

    if task_years and hasattr(module, "SEASON_TASK_YEAR_STR"):
        module.SEASON_TASK_YEAR_STR = str(task_years[0])

    if (
        cfg.get("period_start")
        and hasattr(module, "SEASON_START")
        and getattr(module, "__name__", "") not in MODULES_OWN_SEASON_START
    ):
        module.SEASON_START = date.fromisoformat(str(cfg["period_start"]).split("T")[0])

    if cfg.get("shift_start_hour") is not None and hasattr(module, "DAY_START_HOUR"):
        module.DAY_START_HOUR = int(cfg["shift_start_hour"])
    if cfg.get("shift_end_hour") is not None and hasattr(module, "DAY_END_HOUR"):
        module.DAY_END_HOUR = int(cfg["shift_end_hour"])


def filter_tasks_by_seasons(module: Any, tasks: list[dict], task_years: list[int]) -> list[dict]:
    if not task_years:
        return tasks
    if len(task_years) == 1 and hasattr(module, "_filter_tasks_by_cropwise_season"):
        prev = getattr(module, "SEASON_TASK_YEAR_STR", None)
        module.SEASON_TASK_YEAR_STR = str(task_years[0])
        try:
            return module._filter_tasks_by_cropwise_season(tasks)
        finally:
            if prev is not None:
                module.SEASON_TASK_YEAR_STR = prev
    fn = getattr(module, "_task_season_value", None)
    allowed = {str(y) for y in task_years}
    if fn:
        return [t for t in tasks if fn(t) in allowed]
    return [t for t in tasks if str(t.get("season") or t.get("year") or "") in allowed]


def season_overrides_from_editor(
    report_date: date,
    report_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Сезоны/диапазоны из редактора планов → overrides для proverka job."""
    from proverka.reports_config import REPORTS

    out: dict[str, dict[str, Any]] = {}
    for rid in report_ids:
        spec = REPORTS.get(str(rid))
        if not spec:
            continue
        cfg = resolve_report_season_config(spec, report_date, None)
        ov: dict[str, Any] = {}
        for key in ("task_season_years", "history_years", "period_start", "period_end"):
            val = cfg.get(key)
            if val:
                ov[key] = val
        if cfg.get("task_season_plow_only"):
            ov["task_season_plow_only"] = True
        if cfg.get("by_group"):
            ov["by_group"] = cfg["by_group"]
        if ov:
            out[str(rid)] = ov
    return out


_SEASON_ENV_KEYS = (
    "REPORT_DATE",
    "SVODKI_SEASON_START_DATE",
    "SEASON_START_DATE",
    "SVODKI_SEASON_YEARS",
    "SEASON_YEAR",
    "SEASON_TASK_YEAR_STR",
    "SVODKI_FERT_PERIOD_START",
    "SVODKI_FERT_PERIOD_END",
    "SVODKI_FERT_SEASON_YEARS",
    "SVODKI_SEEDING_SEASON_YEARS",
    "SVODKI_SPRAY_SEASON_YEARS",
    "SVODKI_SPRAY_TASK_SEASON_YEARS",
    "SVODKI_SPRAY_PERIOD_START",
    "SVODKI_SPRAY_PERIOD_END",
    "SVODKI_COMBINE_SEASON_START",
    "PROVERKA_DAY_START_HOUR",
    "PROVERKA_DAY_END_HOUR",
    "PROVERKA_SEASON_CONFIG",
    "SVODKI_SEASON_RANGES_PATH",
    "SVODKI_UPLOAD_DIR",
)


@contextmanager
def season_context(
    spec: ReportSpec,
    report_date: date,
    run_override: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Применить сезон к env; после выхода восстановить прежние значения."""
    cfg = resolve_report_season_config(spec, report_date, run_override)
    saved = {k: os.environ.get(k) for k in _SEASON_ENV_KEYS}
    saved.update({k: os.environ.get(k) for k in os.environ if k.startswith("PROVERKA_")})
    _apply_env_from_config(spec, report_date, cfg)
    try:
        yield cfg
    finally:
        touched = set(_SEASON_ENV_KEYS) | {k for k in os.environ if k.startswith("PROVERKA_")}
        for k in touched:
            os.environ.pop(k, None)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
