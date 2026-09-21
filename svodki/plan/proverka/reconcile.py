#!/usr/bin/env python3
"""
Сверка сводок (machine work) с UI Cropwise /machines/reports/work.json

  python3 proverka/reconcile.py --report 5
  python3 proverka/reconcile.py --all
  REPORT_DATE=2026-09-11 python3 proverka/reconcile.py --report 5

Docker:
  docker exec -e REPORT_DATE=2026-09-11 v3_disk-api-1 \\
    python3 /app/svodki/plan/proverka/reconcile.py --report 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

PLAN_DIR = Path(__file__).resolve().parent.parent
if str(PLAN_DIR) not in sys.path:
    sys.path.insert(0, str(PLAN_DIR))

from plan_paths import ensure_plan_sys_path  # noqa: E402

ensure_plan_sys_path()

from proverka.cropwise_ui import (  # noqa: E402
    fetch_work_report,
    fetch_work_report_resilient,
    make_web_session,
    parse_work_report_body,
)
from proverka.season_cropwise import (  # noqa: E402
    compute_season_aware_cropwise,
    work_type_ids_for_report,
)
from proverka.reports_config import REPORTS, ReportSpec, list_report_ids  # noqa: E402
from proverka.operation_compare import build_cross_check  # noqa: E402
from proverka.svodka_facts import compute_svodka_facts  # noqa: E402
from proverka.summary import build_rows, print_table  # noqa: E402

# =============================================================================
# COOKIE — только из окружения: CROPWISE_SESSION_COOKIE в .env.
CROPWISE_SESSION_COOKIE = ""
# =============================================================================


def load_env() -> None:
    env_path = PLAN_DIR.parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def session_cookie() -> str:
    return (
        os.environ.get("CROPWISE_SESSION_COOKIE", "").strip()
        or CROPWISE_SESSION_COOKIE.strip()
    )


def _firm_names(spec: ReportSpec) -> list[str]:
    import importlib

    mod = importlib.import_module(spec.module)
    if hasattr(mod, "FIRM_ORDER"):
        return list(mod.FIRM_ORDER)
    return list(mod.AGROFIRMS.keys())


def _work_type_ids_for_spec(spec: ReportSpec) -> list[int]:
    import importlib

    mod = importlib.import_module(spec.module)
    wt_ids: set[int] = set(getattr(mod, "WORK_TYPE_IDS", set()) or set())
    wt_ids |= set(getattr(mod, "ALL_WT_IDS", set()) or set())
    wt_ids |= set(getattr(mod, "SPRAY_WT_IDS", set()) or set())
    return sorted(int(x) for x in wt_ids)


def _shift_window_iso(spec: ReportSpec, report_date: date) -> tuple[str, str, list[int]]:
    """Окно смены 06:00–06:00 и work_type_ids без тяжёлого расчёта сводки."""
    import importlib
    import inspect
    from datetime import datetime, timedelta

    mod = importlib.import_module(spec.module)
    fn = getattr(mod, "compute_time_range", None) or getattr(mod, "resolve_time_range", None)
    if fn is not None:
        h0 = getattr(mod, "DAY_START_HOUR", 6)
        h1 = getattr(mod, "DAY_END_HOUR", h0)
        if len(inspect.signature(fn).parameters) == 1:
            result = fn(report_date)
        else:
            result = fn(report_date, h0, h1)
        day_start, cutoff = result[0], result[1]
    else:
        from plans import TZ_MSK

        shift = report_date - timedelta(days=1)
        h = getattr(mod, "DAY_START_HOUR", 6)
        day_start = datetime(shift.year, shift.month, shift.day, h, 0, tzinfo=TZ_MSK)
        cutoff = datetime(report_date.year, report_date.month, report_date.day, h, 0, tzinfo=TZ_MSK)
    t0 = day_start.isoformat()[:19].replace("+03:00", "")
    t1 = cutoff.isoformat()[:19].replace("+03:00", "")
    try:
        wt_ids = work_type_ids_for_report(spec.report_id)
    except ValueError:
        wt_ids = _work_type_ids_for_spec(spec)
    return t0, t1, wt_ids


def _fetch_cropwise_ui(cookie: str, t0: str, t1: str, work_type_ids: list[int]) -> dict[str, Any]:
    """Отдельная web-сессия на каждый запрос work.json (не после API v3)."""
    return fetch_work_report(make_web_session(cookie), t0, t1, work_type_ids)


def _cropwise_total(work: dict[str, Any], fact_hints: tuple[str, ...]) -> float:
    from proverka.cropwise_ui import cropwise_total_ha

    return cropwise_total_ha(work, fact_hints)


def _cropwise_looks_degraded(
    work: dict[str, Any],
    svodka_total: float,
    fact_hints: tuple[str, ...],
    *,
    cropwise_total: float | None = None,
    cropwise_api_fallback: bool = False,
) -> bool:
    if not work.get("ok"):
        return True
    body = work.get("body")
    if not isinstance(body, dict):
        return True
    if body.get("reportBlank"):
        if cropwise_api_fallback and cropwise_total and cropwise_total > 0:
            if svodka_total <= 1.0:
                return False
            return cropwise_total < svodka_total * 0.25
        return svodka_total > 1.0
    cw_total = cropwise_total
    if cw_total is None:
        parsed = parse_work_report_body(body, [], list(fact_hints))
        cw_total = float(parsed.get("fact_total_ha") or 0)
    if svodka_total <= 1.0:
        return False
    if cw_total <= 0:
        return True
    return cw_total < svodka_total * 0.25


def _cropwise_retry_warranted(
    svodka_total: float,
    cropwise_total: float,
    *,
    cropwise_api_fallback: bool = False,
    ui_session: int | None = None,
    preferred_ui: int | None = None,
    acceptable_ui_seasons: list[int] | None = None,
) -> bool:
    """
    Повтор work.json: частичный UI (~1941 vs ~2226), API fallback или неподходящий сезон сессии.

    acceptable_ui_seasons — сезоны сводки: если сессия Cropwise уже отдаёт один
    из них, повторять запрос бессмысленно (сезон сессии сменить запросом нельзя).
    """
    if svodka_total <= 1.0:
        return False
    diff = svodka_total - cropwise_total
    tol = max(5.0, svodka_total * 0.02)
    if diff > tol:
        return True
    if cropwise_api_fallback and abs(diff) > 0.5:
        return True
    if preferred_ui is not None and ui_session is not None and ui_session != preferred_ui:
        from proverka.season_cropwise import ui_session_ok_for_years

        return not ui_session_ok_for_years(ui_session, acceptable_ui_seasons)
    return False


def _annotate_quality(cmp_: dict[str, Any], parsed: dict[str, Any], work: dict[str, Any]) -> None:
    """Явные предупреждения вместо молчаливых допущений парсера."""
    warns: list[str] = []
    if parsed.get("fact_column_confidence") == "guess":
        warns.append(
            f"колонка факта выбрана по догадке ({parsed.get('fact_column')!r}) — "
            "проверьте fact_column_hints сводки"
        )
    balance = cmp_.get("cropwise_ui_balance") or {}
    if balance and not balance.get("balanced"):
        warns.append(
            f"итог UI не сходится со строками: невязка {balance.get('residual_ha')} га "
            f"(сироты {balance.get('orphan_ha')}, без фирмы {balance.get('no_firm_ha')})"
        )
    unmatched = float(parsed.get("unmatched_firm_ha") or 0)
    if unmatched > 0:
        warns.append(f"{unmatched} га в строках UI без сопоставленной агрофирмы")
    meta = work.get("fetch_meta") or {}
    if meta.get("unstable"):
        warns.append(
            f"work.json нестабилен: разброс ответов {meta.get('total_spread_ha')} га"
        )
    # «Всего» из UI — только сезон сессии Cropwise, поэтому сверяем его с API
    # за тот же сезон; сравнение с суммой сезонов давало ложное расхождение.
    api_ha = cmp_.get("cropwise_api_session_season_ha")
    if api_ha is None:
        api_ha = cmp_.get("cropwise_api_ha")
    ui_ha = cmp_.get("cropwise_ui_ha")
    if api_ha and ui_ha and abs(float(ui_ha) - float(api_ha)) > max(5.0, float(ui_ha) * 0.05):
        warns.append(
            f"UI {ui_ha} га (сезон сессии) vs API v3 {api_ha} га — расхождение источников"
        )
    if warns:
        cmp_["warnings"] = warns
        cmp_["note"] += " ⚠ " + "; ".join(warns)


def reconcile_one(
    spec: ReportSpec,
    report_date: date,
    cookie: str,
    firm_names: list[str],
    run_override: dict | None = None,
    *,
    prefetched_work: dict[str, Any] | None = None,
    prefetched_svodka: dict[str, Any] | None = None,
    peer_svodka: dict[str, dict[str, Any]] | None = None,
    batch_report_ids: set[str] | None = None,
) -> dict[str, Any]:
    if spec.cropwise_per_operation:
        from proverka.report2_reconcile import reconcile_per_operation

        return reconcile_per_operation(
            spec, report_date, cookie, firm_names, run_override,
            prefetched_work=prefetched_work,
            prefetched_svodka=prefetched_svodka,
            peer_svodka=peer_svodka,
            batch_report_ids=batch_report_ids,
        )

    t0, t1, wt_ids = _shift_window_iso(spec, report_date)
    hints = list(spec.fact_column_hints)

    # Cropwise UI — до сотен запросов API v3; несколько попыток с паузами.
    preferred_ui = None
    if spec.season_rule in ("both_years", "year_plus_one", "plow_year_plus_one"):
        preferred_ui = report_date.year + 1

    if prefetched_work is not None:
        work = prefetched_work
    else:
        work = fetch_work_report_resilient(
            cookie, t0, t1, wt_ids, hints, attempts=2, delay_sec=2.0,
            preferred_ui_session=preferred_ui,
        )

    if prefetched_svodka is not None:
        svodka = prefetched_svodka
    else:
        svodka = asyncio.run(compute_svodka_facts(spec, report_date, run_override=run_override))

    svodka_total = float(svodka.get("total_ha") or 0)
    season = svodka.get("season_policy") or {}
    task_years = season.get("task_season_years") or []
    ui_session = work.get("ui_session_seasonValue")

    season_cropwise: dict[str, Any] | None = None
    shift_cache = None
    wt_from_groups = wt_ids
    if work.get("ok") and isinstance(work.get("body"), dict):
        try:
            wt_from_groups = work_type_ids_for_report(spec.report_id)
        except ValueError:
            wt_from_groups = wt_ids
        from proverka.season_cropwise import ShiftWindowCache

        shift_cache = ShiftWindowCache.build(
            t0, t1, wt_from_groups, log_prefix=f"[proverka r{spec.report_id}] ",
        )
        season_cropwise = compute_season_aware_cropwise(
            work["body"],
            firm_names,
            hints,
            wt_from_groups,
            t0,
            t1,
            ui_session,
            task_season_years=task_years,
            season_rule=spec.season_rule,
            shift_cache=shift_cache,
        )
        blocked = bool(season_cropwise.get("ui_session_blocked"))
        need_sess = season_cropwise.get("required_ui_sessionValue")
        if blocked and need_sess and work.get("ui_session_seasonValue") != need_sess:
            work_alt = fetch_work_report_resilient(
                cookie, t0, t1, wt_ids, hints, attempts=3, delay_sec=2.0,
                preferred_ui_session=int(need_sess),
            )
            if (
                work_alt.get("ok")
                and isinstance(work_alt.get("body"), dict)
                and work_alt.get("ui_session_seasonValue") == need_sess
            ):
                work = work_alt
                ui_session = work.get("ui_session_seasonValue")
                season_cropwise = compute_season_aware_cropwise(
                    work["body"],
                    firm_names,
                    hints,
                    wt_from_groups,
                    t0,
                    t1,
                    ui_session,
                    task_season_years=task_years,
                    season_rule=spec.season_rule,
                    shift_cache=shift_cache,
                )

    cropwise_compare_total = (
        float(season_cropwise["cropwise_total_ha"])
        if season_cropwise
        else _cropwise_total(work, spec.fact_column_hints)
    )

    fetch_note = ""
    api_fallback = bool(season_cropwise.get("cropwise_api_fallback")) if season_cropwise else False
    if _cropwise_looks_degraded(
        work, svodka_total, spec.fact_column_hints,
        cropwise_total=cropwise_compare_total,
        cropwise_api_fallback=api_fallback,
    ) or _cropwise_retry_warranted(
        svodka_total,
        cropwise_compare_total,
        cropwise_api_fallback=api_fallback,
        ui_session=ui_session,
        preferred_ui=preferred_ui,
        acceptable_ui_seasons=list(task_years),
    ):
        retry = fetch_work_report_resilient(
            cookie, t0, t1, wt_ids, hints, attempts=4, delay_sec=5.0,
            preferred_ui_session=preferred_ui,
        )
        retry_total = _cropwise_total(retry, spec.fact_column_hints)
        if retry.get("ok") and isinstance(retry.get("body"), dict):
            try:
                retry_sc = compute_season_aware_cropwise(
                    retry["body"], firm_names, hints,
                    work_type_ids_for_report(spec.report_id),
                    t0, t1, retry.get("ui_session_seasonValue"),
                    task_season_years=task_years,
                    season_rule=spec.season_rule,
                    shift_cache=shift_cache,
                )
                retry_total = float(retry_sc["cropwise_total_ha"])
            except ValueError:
                pass
        if retry_total > cropwise_compare_total + 0.01:
            work = retry
            ui_session = work.get("ui_session_seasonValue")
            try:
                season_cropwise = compute_season_aware_cropwise(
                    work["body"], firm_names, hints,
                    work_type_ids_for_report(spec.report_id),
                    t0, t1, ui_session, task_season_years=task_years,
                    season_rule=spec.season_rule,
                    shift_cache=shift_cache,
                )
                cropwise_compare_total = float(season_cropwise["cropwise_total_ha"])
            except ValueError:
                season_cropwise = None
                cropwise_compare_total = _cropwise_total(work, spec.fact_column_hints)
            fetch_note = "Cropwise UI: повтор после расчёта сводки (лучший из нескольких ответов)."
        elif _cropwise_looks_degraded(
            work, svodka_total, spec.fact_column_hints,
            cropwise_total=cropwise_compare_total,
            cropwise_api_fallback=api_fallback,
        ):
            body = work.get("body") or {}
            blank = body.get("reportBlank")
            season_years = (svodka.get("season_policy") or {}).get("task_season_years") or []
            season_hint = ""
            if blank and season_years:
                season_hint = (
                    f" Сезон сводки ({'+'.join(map(str, season_years))}): work.json отдаёт "
                    f"только сезон сессии Cropwise ({ui_session}), остальные сезоны "
                    "берутся из API v3."
                )
            fetch_note = (
                "Cropwise UI отдал урезанный/пустой ответ (часто после массовых API-запросов)."
                f"{season_hint} Проверьте work.json в браузере."
            )

    result: dict[str, Any] = {
        "report_id": spec.report_id,
        "title": spec.title,
        "svodka": svodka,
        "cropwise_work_report": work,
    }

    if not work.get("ok"):
        return result

    if season_cropwise:
        parsed = season_cropwise["parsed"]
        seasons = season_cropwise["seasons"]
        cropwise_by_firm = season_cropwise["cropwise_by_firm"]
        cropwise_total = season_cropwise["cropwise_total_ha"]
        ui_session = season_cropwise.get("ui_session_seasonValue")
        ui_session_blocked = bool(season_cropwise.get("ui_session_blocked"))
        required_ui_session = season_cropwise.get("required_ui_sessionValue")
        if ui_session is not None:
            work["ui_session_seasonValue"] = ui_session
    else:
        parsed = parse_work_report_body(
            work["body"], firm_names, list(spec.fact_column_hints),
        )
        seasons = {}
        cropwise_by_firm = parsed.get("fact_by_firm") or {}
        cropwise_total = float(parsed.get("fact_total_ha") or 0)
        ui_session_blocked = False
        required_ui_session = None

    result["cropwise_parsed"] = parsed
    if seasons:
        result["cropwise_seasons"] = seasons

    season_note = season.get("label") or ""
    if len(task_years) == 1:
        ui_note = f"Cropwise: UI season {ui_session} + API season для сводки {task_years[0]}."
    elif len(task_years) > 1:
        ui_note = (
            f"Cropwise: UI verified (season {ui_session}) + API v3 для "
            f"{'+'.join(map(str, task_years))}."
        )
    elif season_cropwise and season_cropwise.get("cropwise_api_fallback"):
        ui_note = "Cropwise: API v3 (work.json blank), задания в окне 06:00–06:00."
    else:
        ui_note = "Cropwise UI: все задания в окне 06:00–06:00."

    ui_season_years = work.get("ui_season_years") or []
    season_mismatch = bool(
        ui_session and task_years and ui_session not in task_years and len(task_years) > 1
    )
    result["comparison"] = {
        "svodka_total_ha": svodka["total_ha"],
        "cropwise_fact_total_ha": cropwise_total,
        "total_diff_ha": round(svodka["total_ha"] - cropwise_total, 2),
        "fact_column": parsed.get("fact_column"),
        "season_policy": season,
        "ui_session_seasonValue": ui_session,
        "required_ui_sessionValue": required_ui_session,
        "ui_session_blocked": ui_session_blocked,
        "cropwise_api_fallback": bool(season_cropwise.get("cropwise_api_fallback")) if season_cropwise else False,
        "cropwise_ui_season_years": ui_season_years,
        "season_mismatch": season_mismatch,
        "by_firm_diff": {
            firm: round(
                svodka["area_today_by_firm"].get(firm, 0)
                - cropwise_by_firm.get(firm, 0),
                2,
            )
            for firm in firm_names
            if svodka["area_today_by_firm"].get(firm, 0) or cropwise_by_firm.get(firm, 0)
        },
        "cropwise_ui_ha": (season_cropwise or {}).get("cropwise_ui_ha", cropwise_total),
        "cropwise_api_ha": (season_cropwise or {}).get("cropwise_api_ha"),
        "cropwise_ui_vs_api_ha": (season_cropwise or {}).get("cropwise_ui_vs_api_ha"),
        "cropwise_api_session_season_ha": (season_cropwise or {}).get(
            "cropwise_api_session_season_ha",
        ),
        # Виды работ Cropwise внутри колонки-группы, из которой взят факт.
        "fact_work_types": parsed.get("fact_work_types") or [],
        "cropwise_ui_balance": (season_cropwise or {}).get("ui_balance"),
        "cropwise_orphan_ha": parsed.get("ui_orphan_ha"),
        "cropwise_unmatched_firm_ha": parsed.get("unmatched_firm_ha"),
        "fact_column_confidence": parsed.get("fact_column_confidence"),
        "cropwise_fetch_meta": work.get("fetch_meta"),
        "note": f"Cropwise = «Всего» из work.json. {season_note}. {ui_note}",
        "ui_degraded": _cropwise_looks_degraded(
            work, svodka_total, spec.fact_column_hints,
            cropwise_total=cropwise_total,
            cropwise_api_fallback=bool(
                season_cropwise.get("cropwise_api_fallback") if season_cropwise else False
            ),
        ),
    }
    if seasons:
        for year in ("2026", "2027"):
            block = seasons.get(year) or {}
            result["comparison"][f"cropwise_season_{year}_ha"] = block.get("total_ha", 0)
            result["comparison"][f"cropwise_season_{year}_source"] = block.get("source")
        result["comparison"]["cropwise_both_seasons_ha"] = cropwise_total
        if ui_session and seasons.get(str(ui_session), {}).get("source") == "work_json_verified":
            result["comparison"]["note"] += (
                f" UI season {ui_session}: verified #task_id."
            )
    if ui_session_blocked and required_ui_session:
        result["comparison"]["note"] += (
            f" ⚠ Cropwise UI: seasonValue={ui_session} не входит в сезоны сводки "
            f"({'+'.join(map(str, task_years))}). work.json отдаёт только сезон сессии, "
            f"поэтому факт взят из API v3. Чтобы сверять с UI, откройте Cropwise "
            f"с сезоном {required_ui_session} и обновите cookie "
            f"(или задайте CROPWISE_SESSION_COOKIE_{required_ui_session})."
        )
    if fetch_note:
        result["comparison"]["note"] += f" {fetch_note}"

    _annotate_quality(result["comparison"], parsed, work)

    from proverka.task_errors import attach_task_errors

    attach_task_errors(
        result,
        report_id=spec.report_id,
        work_title=spec.title,
        wt_ids=wt_from_groups,
        t0=t0,
        t1=t1,
        task_season_years=list(task_years),
        shift_cache=shift_cache,
        season_cropwise=season_cropwise,
    )
    return result


def _print_result(spec: ReportSpec, result: dict[str, Any]) -> None:
    svodka = result["svodka"]
    work = result["cropwise_work_report"]

    print("\n" + "=" * 72)
    print(f"Сводка {spec.report_id} ({spec.title}) | {svodka['report_date']}")
    sp = svodka.get("season_policy") or {}
    if sp.get("label"):
        print(f"Сезон (сводка): {sp['label']}")
    elif svodka.get("season_task_filter"):
        print(f"Сезон: {svodka['season_task_filter']}")
    print("=" * 72)

    print(f"\n--- Сводка {spec.report_id} (area_today), га ---")
    for firm, ha in sorted(svodka["area_today_by_firm"].items(), key=lambda x: -x[1]):
        if ha > 0:
            print(f"  {firm:32s} {ha:10.2f}")
    print(f"  {'ИТОГО':32s} {svodka['total_ha']:10.2f}")

    col = (result.get("comparison") or {}).get("fact_column") or "факт"
    print(f"\n--- Cropwise UI ({col}) ---")
    if not work.get("ok"):
        print(f"  Ошибка {work.get('status')}: {str(work.get('error', ''))[:200]}")
        print("  Обновите cookie в proverka/reconcile.py или .env")
        return

    parsed = result.get("cropwise_parsed") or {}
    if parsed.get("error"):
        print(f"  {parsed['error']}")
        return

    for firm, ha in sorted(parsed.get("fact_by_firm", {}).items(), key=lambda x: -x[1]):
        s_ha = svodka["area_today_by_firm"].get(firm, 0)
        flag = "" if abs(s_ha - ha) < 0.01 else f"  (сводка={s_ha})"
        print(f"  {firm:32s} {ha:10.2f}{flag}")
    raw_total = parsed.get("fact_total_ha_raw")
    if raw_total is not None and abs(float(raw_total) - float(parsed.get("fact_total_ha") or 0)) >= 0.01:
        print(f"  {'ИТОГО (verified+seasons)':32s} {parsed.get('fact_total_ha', 0):10.2f}")
        print(f"  {'ИТОГО (raw UI cell)':32s} {raw_total:10.2f}")
    else:
        print(f"  {'ИТОГО':32s} {parsed.get('fact_total_ha', 0):10.2f}")
    seasons = result.get("cropwise_seasons") or {}
    if seasons:
        for year in ("2027", "2026"):
            block = seasons.get(year) or {}
            if block.get("total_ha"):
                print(f"    season {year} ({block.get('source', '?')}): {block['total_ha']} га")

    cmp_ = result.get("comparison")
    if cmp_:
        print(f"\n  Diff по ИТОГО (сводка − Cropwise): {cmp_['total_diff_ha']} га")
        if cmp_["total_diff_ha"] == 0:
            print("  ✓ Итоги совпадают")
        elif abs(cmp_["total_diff_ha"]) <= 5:
            print("  ✓ Итоги совпадают (допуск ±5 га)")
        elif cmp_.get("season_policy", {}).get("task_season_years"):
            print("  ℹ Расхождение по агрофирмам возможно из-за группировки в UI Cropwise.")


def main() -> int:
    load_env()

    parser = argparse.ArgumentParser(description="Сверка сводок с Cropwise work.json")
    parser.add_argument(
        "--report",
        action="append",
        dest="reports",
        metavar="ID",
        help=f"ID сводки ({', '.join(list_report_ids())}); можно указать несколько раз",
    )
    parser.add_argument("--all", action="store_true", help="Все настроенные сводки")
    parser.add_argument(
        "--date",
        dest="report_date",
        default=os.environ.get("REPORT_DATE", date.today().isoformat()),
        help="Дата отчёта YYYY-MM-DD (или env REPORT_DATE)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="В конце вывести сводную таблицу (удобно с --all)",
    )
    parser.add_argument(
        "--excel",
        action="store_true",
        help="Сохранить Excel-отчёт (удобно с --all)",
    )
    parser.add_argument(
        "--svodki-only",
        action="store_true",
        help="Только сводки между собой (без Cropwise, cookie не нужна)",
    )
    args = parser.parse_args()

    if args.all:
        report_ids = list_report_ids()
    elif args.reports:
        report_ids = args.reports
    else:
        report_ids = ["5"]

    for rid in report_ids:
        if rid not in REPORTS:
            print(f"ERROR: неизвестная сводка {rid!r}. Доступны: {', '.join(list_report_ids())}", file=sys.stderr)
            return 1

    report_date = date.fromisoformat(args.report_date)
    os.environ["REPORT_DATE"] = report_date.isoformat()

    svodki_only = bool(args.svodki_only)
    cookie = ""
    if not svodki_only:
        cookie = session_cookie()
        if not cookie:
            print(
                "ERROR: не задана cookie.\n"
                "CROPWISE_SESSION_COOKIE в proverka/reconcile.py или .env",
                file=sys.stderr,
            )
            return 1
    out_dir = Path(os.environ.get("RECONCILE_OUT_DIR", str(PLAN_DIR.parents[1])))
    exit_code = 0
    all_results: dict[str, Any] = {"report_date": report_date.isoformat(), "reports": {}}

    for rid in report_ids:
        spec = REPORTS[rid]
        needs_cw = spec.cropwise_comparable or spec.cropwise_per_operation
        if svodki_only or not needs_cw:
            from proverka.runner import _svodka_only_one

            result = _svodka_only_one(spec, report_date)
        else:
            firm_names = _firm_names(spec)
            result = reconcile_one(spec, report_date, cookie, firm_names, run_override=None)
        all_results["reports"][rid] = result
        if not svodki_only and needs_cw:
            _print_result(spec, result)
            if not result["cropwise_work_report"].get("ok"):
                exit_code = 2
        else:
            sv = result.get("svodka") or {}
            print(f"\nСводка {rid} ({spec.title}): {sv.get('total_ha', 0)} га (операций: {len(sv.get('operations') or {})})")

        single_path = out_dir / f"reconcile_report{rid}_{report_date.isoformat()}.json"
        single_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON: {single_path}")

    if len(report_ids) > 1:
        all_results["cross_check"] = build_cross_check(all_results)
        batch_path = out_dir / f"reconcile_all_{report_date.isoformat()}.json"
        batch_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nСводный JSON: {batch_path}")
        cc = all_results["cross_check"].get("summary") or {}
        if cc:
            print(
                f"Сверка операций: OK={cc.get('ok', 0)} DIFF={cc.get('diff', 0)} "
                f"partial={cc.get('partial', 0)}"
            )

    if getattr(args, "excel", False) and len(report_ids) > 1:
        from proverka.export_excel import export_excel

        xlsx_path = out_dir / f"proverka_{report_date.isoformat()}.xlsx"
        export_excel(all_results, xlsx_path)
        print(f"Excel: {xlsx_path}")

    if args.summary or (args.all and len(report_ids) > 1):
        print_table(build_rows(all_results), report_date.isoformat())
        print(
            "Подробнее: python3 proverka/summary.py "
            f"--date {report_date.isoformat()} --firms"
        )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
