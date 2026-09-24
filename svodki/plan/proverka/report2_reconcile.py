"""Сводки 1/2: каждая операция vs Cropwise work.json (как сводки 3–11)."""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any

from proverka.cropwise_ui import fetch_work_report_resilient, parse_work_report_body
from proverka.work_groups import work_group_wt_map, work_type_ids_for_group
from proverka.operation_compare import operations_from_svodka
from proverka.operation_registry import canonical_cropwise_report
from proverka.reports_config import REPORTS, ReportSpec


def _thematic_cropwise_ids(batch_report_ids: set[str] | None) -> set[str]:
    if not batch_report_ids:
        return set()
    return {
        rid for rid in batch_report_ids
        if (spec := REPORTS.get(rid))
        and spec.cropwise_comparable
        and not spec.cropwise_per_operation
    }


def filter_ops_for_batch(
    ops: dict[str, dict[str, Any]],
    batch_report_ids: set[str] | None,
) -> dict[str, dict[str, Any]]:
    """
    Сводка 1/2 + тематические в одном batch: сверяем только операции,
    для которых выбрана тематическая сводка (3, 6, 7…).
    Если выбрана только 1/2 — все операции.
    """
    if not ops or not batch_report_ids:
        return ops
    per_op = any(REPORTS.get(r) and REPORTS[r].cropwise_per_operation for r in batch_report_ids)
    thematic = _thematic_cropwise_ids(batch_report_ids)
    if not per_op or not thematic:
        return ops
    filtered: dict[str, dict[str, Any]] = {}
    for gn, op in ops.items():
        canon = canonical_cropwise_report(gn)
        if canon is None or canon in thematic or _always_reconcile_per_op(gn):
            filtered[gn] = op
    return filtered


def _always_reconcile_per_op(group_name: str) -> bool:
    """Операции с эталоном вне batch (сводка 10 для 5.4) — сверяем всегда."""
    return group_name == "5.4 Уборка картофеля, лука"
from proverka.season_cropwise import (
    ShiftWindowCache,
    compute_season_aware_cropwise,
    preferred_ui_session,
    ui_session_ok_for_years,
    work_type_ids_for_report,
)
from proverka.season_policy import resolve_group_season_policy
from season_unified import season_rule_for_group
from proverka.svodka_facts import compute_svodka_facts

_WORK_GROUP_WT: dict[str, list[int]] = work_group_wt_map()


def _wt_ids_for_operation(group_name: str, op: dict[str, Any]) -> list[int]:
    canonical = work_type_ids_for_group(group_name)
    if canonical:
        return canonical
    op_wts = [int(x) for x in (op.get("work_type_ids") or [])]
    if op_wts:
        return op_wts
    return [int(x) for x in (_WORK_GROUP_WT.get(group_name) or [])]


def _max_op_workers(count: int) -> int:
    default = max(1, min(4, int(os.environ.get("PROVERKA_MAX_WORKERS", "4"))))
    return max(1, min(default, count))


async def _load_canonical_svodka_ops(
    group_names: list[str],
    report_date: date,
    run_override: dict | None,
    *,
    peer_svodka: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Факт по операциям из тематических сводок (3, 6, …) с их season-политикой.
    Сводка 2 (compact) для части операций не фильтрует season так же — для Cropwise
    сравниваем с тем же эталоном, что и standalone сводка 6/3/…
    """
    rid_to_groups: dict[str, list[str]] = {}
    for gn in group_names:
        canon = canonical_cropwise_report(gn)
        if canon:
            rid_to_groups.setdefault(canon, []).append(gn)

    out: dict[str, dict[str, Any]] = {}
    peer_svodka = peer_svodka or {}

    async def _one(rid: str) -> tuple[str, dict[str, Any]]:
        if rid in peer_svodka:
            return rid, peer_svodka[rid]
        spec = REPORTS[rid]
        sv = await compute_svodka_facts(spec, report_date, run_override=run_override)
        return rid, sv

    loaded = await asyncio.gather(*[_one(rid) for rid in sorted(rid_to_groups)])
    for rid, sv in loaded:
        sv_ops = operations_from_svodka(sv)
        for gn in rid_to_groups[rid]:
            op = sv_ops.get(gn)
            if not op:
                continue
            out[gn] = {
                **op,
                "canonical_report_id": rid,
                "season_policy": resolve_group_season_policy(gn, report_date, run_override),
            }
    return out


def _fact_hints_for_group(group_name: str) -> tuple[str, ...]:
    if group_name.startswith("5."):
        return ("Убор", "Кормозаготов", "Скашиван", "Обработ")
    return ("Обработ",)


def _cropwise_spec_for_group(group_name: str) -> ReportSpec:
    """Спека Cropwise: тематическая сводка или fallback по compact 2 (только для API)."""
    canon = canonical_cropwise_report(group_name)
    if canon and canon in REPORTS:
        return REPORTS[canon]
    return ReportSpec(
        "2",
        group_name,
        "2_main_ideal_compact",
        (group_name,),
        season_rule_for_group(group_name),
        _fact_hints_for_group(group_name),
    )


def _svodka_fact_report_id(
    group_name: str,
    op: dict[str, Any],
    *,
    source_report_id: str,
) -> str:
    """ID сводки-источника факта для UI (без внутренних pseudo-id)."""
    return (
        op.get("canonical_report_id")
        or canonical_cropwise_report(group_name)
        or source_report_id
    )


def _compute_cropwise_for_operation(
    work: dict[str, Any],
    *,
    cw_spec: ReportSpec,
    hints: list[str],
    wt_ids: list[int],
    firm_names: list[str],
    t0: str,
    t1: str,
    ui_session: int | None,
    task_years: list[int],
    season_rule: str,
    shift_cache: ShiftWindowCache | None,
) -> dict[str, Any] | None:
    if not work.get("ok") or not isinstance(work.get("body"), dict):
        return None
    return compute_season_aware_cropwise(
        work["body"],
        firm_names,
        hints,
        wt_ids,
        t0,
        t1,
        ui_session,
        task_season_years=task_years,
        season_rule=season_rule,
        shift_cache=shift_cache,
    )


def fetch_work_for_operation(
    group_name: str,
    cookie: str,
    report_date: date,
    run_override: dict | None = None,
) -> dict[str, Any]:
    """
    work.json ровно по work_type_ids одной операции.

    Cropwise складывает несколько видов работ в одну колонку-группу
    («Обработка СЗР, га» = 3.1 Опрыскивание + 3.2 Опрыскивание на овощах),
    а ячейка такой колонки — список значений по work_type_names. Раньше здесь
    запрашивались wt всей тематической сводки, и обе операции получали сумму
    группы: 3.1 и 3.2 показывали одно и то же число (3688.15 га).

    Запрос по wt одной операции возвращает в группе только её вид работ,
    поэтому итог колонки равен факту именно этой операции.
    """
    from proverka.cropwise_ui import fetch_work_report_resilient
    from proverka.reconcile import _shift_window_iso

    cw_spec = _cropwise_spec_for_group(group_name)
    t0, t1, _ = _shift_window_iso(cw_spec, report_date)
    wt_ids = work_type_ids_for_group(group_name) or [
        int(x) for x in (_WORK_GROUP_WT.get(group_name) or [])
    ]
    hints = list(cw_spec.fact_column_hints)
    pref = None
    if cw_spec.season_rule in ("both_years", "year_plus_one", "plow_year_plus_one"):
        group_cfg = resolve_group_season_policy(group_name, report_date, run_override)
        task_years = list(group_cfg.get("task_season_years") or [])
        pref = preferred_ui_session(group_cfg.get("season_rule") or cw_spec.season_rule, task_years)
    return fetch_work_report_resilient(
        cookie, t0, t1, wt_ids, hints, attempts=2, delay_sec=1.5,
        preferred_ui_session=pref,
    )


def prefetch_editor_season_works(
    group_names: list[str],
    cookie: str,
    report_date: date,
) -> dict[str, dict[str, Any]]:
    """
    work.json по каждому сезону редактора планов.

    Сессия переключается один раз на год, затем читаются все операции этого года.
    Несколько сезонов потом складываются, один сезон остаётся сам по себе.
    """
    from season_unified import editor_check_years
    from proverka.cropwise_ui import fetch_work_report, set_ui_session_year, warm_web_session
    from proverka.reconcile import _shift_window_iso

    jobs: list[tuple[str, list[int], list[int]]] = []
    for name in group_names:
        years = editor_check_years(name, report_date)
        wt_ids = work_type_ids_for_group(name)
        if not years or not wt_ids:
            continue
        jobs.append((name, years, wt_ids))
    if not jobs:
        return {}
    web, csrf, _years, session = warm_web_session(cookie)
    stored: dict[str, dict[int, dict[str, Any]]] = {name: {} for name, _y, _w in jobs}
    for year in sorted({y for _n, years, _w in jobs for y in years}):
        if session != year:
            session = set_ui_session_year(web, csrf, year) or session
        for name, years, wt_ids in jobs:
            if year not in years:
                continue
            spec = _cropwise_spec_for_group(name)
            t0, t1, _ = _shift_window_iso(spec, report_date)
            stored[name][year] = fetch_work_report(
                web, t0, t1, wt_ids, csrf=csrf, ui_session_season=session,
            )
    out: dict[str, dict[str, Any]] = {}
    for name, years, _wt in jobs:
        chosen: dict[str, Any] | None = None
        for year in years:
            item = stored[name].get(year) or {}
            body = item.get("body") if item.get("ok") else None
            if isinstance(body, dict) and not body.get("reportBlank"):
                chosen = item
                break
        if chosen is None:
            chosen = stored[name].get(years[0]) or {"ok": False, "body": {}}
        packed = dict(chosen)
        packed["by_year"] = stored[name]
        packed["editor_years"] = list(years)
        out[name] = packed
    return out


def _canonical_season_for_operation(
    group_name: str,
    report_date: date,
    run_override: dict | None,
) -> tuple[ReportSpec, dict[str, Any], list[int], str]:
    """Сезоны операции — resolve_group_season_config, как редактор и сводка 6."""
    canon_rid = canonical_cropwise_report(group_name)
    canon_spec = REPORTS[canon_rid] if canon_rid and canon_rid in REPORTS else _cropwise_spec_for_group(group_name)
    cfg = resolve_group_season_policy(group_name, report_date, run_override)
    task_years = list(cfg.get("task_season_years") or [])
    season_rule = str(cfg.get("season_rule") or canon_spec.season_rule)
    return canon_spec, cfg, task_years, season_rule


def _reconcile_one_operation(
    group_name: str,
    op: dict[str, Any],
    *,
    source_report_id: str,
    report_date: date,
    cookie: str,
    firm_names: list[str],
    t0: str,
    t1: str,
    prefetched_work: dict[str, Any] | None,
    run_override: dict | None,
    shift_cache: ShiftWindowCache | None,
    op_local: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from proverka.reconcile import (
        _cropwise_looks_degraded,
        _cropwise_retry_warranted,
        _cropwise_total,
    )

    cw_spec = _cropwise_spec_for_group(group_name)
    canon_spec, season_cfg, task_years, season_rule = _canonical_season_for_operation(
        group_name, report_date, run_override,
    )
    hints = list(canon_spec.fact_column_hints)
    wt_ids = _wt_ids_for_operation(group_name, op)
    if not wt_ids:
        return {
            "group_name": group_name,
            "canonical_report": canonical_cropwise_report(group_name),
            "error": "нет work_type_ids",
        }

    preferred_ui = preferred_ui_session(season_rule, task_years or None)

    if prefetched_work is not None and not _always_reconcile_per_op(group_name):
        work = prefetched_work
        if (
            preferred_ui is not None
            and work.get("ui_session_seasonValue") != preferred_ui
            and not ui_session_ok_for_years(
                work.get("ui_session_seasonValue"), task_years,
            )
            and work.get("ok")
        ):
            work = fetch_work_for_operation(group_name, cookie, report_date, run_override)
    else:
        work = fetch_work_for_operation(group_name, cookie, report_date, run_override)

    display_op = op_local if op_local is not None else op
    svodka_total = float(display_op.get("total_ha") or 0)
    svodka_by_firm = display_op.get("area_today_by_firm") or {}
    canon_rid = _svodka_fact_report_id(group_name, op, source_report_id=source_report_id)
    if not season_cfg.get("task_season_years"):
        season_cfg = resolve_group_season_policy(group_name, report_date, run_override)
    ui_session = work.get("ui_session_seasonValue")

    season_cropwise = _compute_cropwise_for_operation(
        work,
        cw_spec=canon_spec,
        hints=hints,
        wt_ids=wt_ids,
        firm_names=firm_names,
        t0=t0,
        t1=t1,
        ui_session=ui_session,
        task_years=task_years,
        season_rule=season_rule,
        shift_cache=shift_cache,
    )

    if season_cropwise:
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
                season_cropwise = _compute_cropwise_for_operation(
                    work,
                    cw_spec=canon_spec,
                    hints=hints,
                    wt_ids=wt_ids,
                    firm_names=firm_names,
                    t0=t0,
                    t1=t1,
                    ui_session=ui_session,
                    task_years=task_years,
                    season_rule=season_rule,
                    shift_cache=shift_cache,
                )

    cropwise_compare_total = (
        float(season_cropwise["cropwise_total_ha"])
        if season_cropwise
        else _cropwise_total(work, canon_spec.fact_column_hints)
    )

    fetch_note = ""
    api_fallback = bool(season_cropwise.get("cropwise_api_fallback")) if season_cropwise else False
    if _cropwise_looks_degraded(
        work, svodka_total, canon_spec.fact_column_hints,
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
        retry = fetch_work_for_operation(group_name, cookie, report_date, run_override)
        retry_total = _cropwise_total(retry, canon_spec.fact_column_hints)
        if retry.get("ok") and isinstance(retry.get("body"), dict):
            retry_sc = _compute_cropwise_for_operation(
                retry,
                cw_spec=canon_spec,
                hints=hints,
                wt_ids=wt_ids,
                firm_names=firm_names,
                t0=t0,
                t1=t1,
                ui_session=retry.get("ui_session_seasonValue"),
                task_years=task_years,
                season_rule=season_rule,
                shift_cache=shift_cache,
            )
            if retry_sc:
                retry_total = float(retry_sc["cropwise_total_ha"])
        if retry_total >= cropwise_compare_total:
            work = retry
            ui_session = work.get("ui_session_seasonValue")
            season_cropwise = _compute_cropwise_for_operation(
                work,
                cw_spec=canon_spec,
                hints=hints,
                wt_ids=wt_ids,
                firm_names=firm_names,
                t0=t0,
                t1=t1,
                ui_session=ui_session,
                task_years=task_years,
                season_rule=season_rule,
                shift_cache=shift_cache,
            )
            fetch_note = "Cropwise UI: повтор после расчёта (лучший ответ)."

    out: dict[str, Any] = {
        "group_name": group_name,
        "canonical_report": canonical_cropwise_report(group_name),
        "svodka_op": op,
        "cropwise_work_report": work,
        "season_policy": season_cfg,
        "work_type_ids": wt_ids,
        "svodka_source_report": canon_rid,
    }
    if op_local is not None:
        out["svodka_op_local"] = op_local
        if source_report_id == "2":
            out["svodka_op_r2"] = op_local
        local_total = float(op_local.get("total_ha") or 0)
        if abs(local_total - svodka_total) >= 0.01:
            diff_local = round(local_total - svodka_total, 2)
            out["svodka_local_diff_ha"] = diff_local
            if source_report_id == "2":
                out["svodka_r2_diff_ha"] = diff_local

    if not work.get("ok"):
        return out

    if season_cropwise:
        parsed = season_cropwise["parsed"]
        seasons = season_cropwise.get("seasons") or {}
        cropwise_by_firm = season_cropwise["cropwise_by_firm"]
        cropwise_total = float(season_cropwise["cropwise_total_ha"])
        ui_session = season_cropwise.get("ui_session_seasonValue")
        ui_session_blocked = bool(season_cropwise.get("ui_session_blocked"))
        required_ui_session = season_cropwise.get("required_ui_sessionValue")
        valid_n = len(season_cropwise.get("valid_task_ids") or ())
        out["valid_task_ids_count"] = valid_n
    else:
        parsed = parse_work_report_body(work["body"], firm_names, hints)
        seasons = {}
        cropwise_by_firm = parsed.get("fact_by_firm") or {}
        cropwise_total = float(parsed.get("fact_total_ha") or 0)
        ui_session_blocked = False
        required_ui_session = None

    out["cropwise_parsed"] = parsed
    if seasons:
        out["cropwise_seasons"] = seasons

    from season_unified import editor_check_years

    editor_years = editor_check_years(group_name, report_date)
    by_year = work.get("by_year") or {}
    if editor_years and by_year:
        from proverka.cropwise_crops import combine_editor_seasons
        from proverka.season_cropwise import _parse_dt

        combined = combine_editor_seasons(
            by_year,
            editor_years,
            firm_names,
            hints,
            shift_cache=shift_cache,
            wt_ids=wt_ids,
            cutoff=_parse_dt(t1),
        )
        cropwise_total = combined["total_ha"]
        cropwise_by_firm = combined["by_firm"]
        work["editor_crop_areas"] = combined["crops"]
        work["editor_years"] = list(editor_years)
        parts = ", ".join(
            f"{year}={float(combined['by_year'].get(year) or 0):.2f}" for year in editor_years
        )
        if len(editor_years) == 1:
            ui_note = f"Cropwise: только сезон {editor_years[0]} из редактора планов ({parts})."
        else:
            ui_note = f"Cropwise: сумма сезонов редактора планов ({parts})."
        task_years = list(editor_years)
    elif task_years:
        if len(task_years) == 1:
            ui_note = f"Cropwise: UI season {ui_session} + API season {task_years[0]}."
        else:
            ui_note = (
                f"Cropwise: UI verified (season {ui_session}) + API v3 "
                f"{'+'.join(map(str, task_years))}."
            )
    elif season_rule == "none":
        api_fb = bool(season_cropwise.get("cropwise_api_fallback")) if season_cropwise else False
        if api_fb:
            ui_note = f"Cropwise: API v3 (work.json blank), wt={wt_ids}."
        else:
            ui_note = f"Cropwise: work.json verified #task_id, wt={wt_ids}."
    else:
        ui_note = "Cropwise UI: все задания в окне 06:00–06:00."

    out["comparison"] = {
        "svodka_total_ha": svodka_total,
        "cropwise_fact_total_ha": cropwise_total,
        "total_diff_ha": round(svodka_total - cropwise_total, 2),
        "fact_column": parsed.get("fact_column"),
        "season_policy": season_cfg,
        "ui_session_seasonValue": ui_session,
        "required_ui_sessionValue": required_ui_session,
        "ui_session_blocked": ui_session_blocked,
        "cropwise_api_fallback": bool(season_cropwise.get("cropwise_api_fallback")) if season_cropwise else False,
        "cropwise_by_firm": {
            firm: round(float(ha), 2)
            for firm, ha in (cropwise_by_firm or {}).items()
            if float(ha or 0) >= 0.01
        },
        "by_firm_diff": {
            firm: round(float(svodka_by_firm.get(firm, 0) or 0) - cropwise_by_firm.get(firm, 0), 2)
            for firm in firm_names
            if svodka_by_firm.get(firm, 0) or cropwise_by_firm.get(firm, 0)
        },
        "cropwise_ui_ha": (season_cropwise or {}).get("cropwise_ui_ha", cropwise_total),
        "cropwise_api_ha": (season_cropwise or {}).get("cropwise_api_ha"),
        "cropwise_ui_vs_api_ha": (season_cropwise or {}).get("cropwise_ui_vs_api_ha"),
        "cropwise_api_session_season_ha": (season_cropwise or {}).get(
            "cropwise_api_session_season_ha",
        ),
        "fact_work_types": parsed.get("fact_work_types") or [],
        "note": (
            f"Сводка {source_report_id} · {group_name} (факт сводки {canon_rid}). {ui_note}"
            if canon_rid and canon_rid != source_report_id
            else f"Сводка {source_report_id} · {group_name}. {ui_note}"
        ),
        "ui_degraded": _cropwise_looks_degraded(
            work, svodka_total, canon_spec.fact_column_hints,
            cropwise_total=cropwise_total,
            cropwise_api_fallback=bool(
                season_cropwise.get("cropwise_api_fallback") if season_cropwise else False
            ),
        ),
    }
    if fetch_note:
        out["comparison"]["note"] += f" {fetch_note}"

    svodka_crop = display_op.get("area_today_by_firm_crop") or {}
    if svodka_crop and work.get("ok"):
        from proverka.crop_compare import compare_svodka_cropwise_crops

        cw_crop = work.get("editor_crop_areas")
        if cw_crop is None:
            from proverka.cropwise_crops import cropwise_crops_all_seasons
            from proverka.season_cropwise import _parse_dt

            cw_crop = cropwise_crops_all_seasons(
                work.get("body"),
                firm_names,
                hints,
                shift_cache=shift_cache,
                task_years=list(task_years),
                ui_session=ui_session if isinstance(ui_session, int) else None,
                wt_ids=wt_ids,
                cutoff=_parse_dt(t1),
            )
        crop_cmp = compare_svodka_cropwise_crops(svodka_crop, cw_crop)
        if crop_cmp:
            out["crop_comparisons"] = crop_cmp
            out["comparison"]["crop_diff_count"] = sum(
                1 for c in crop_cmp if c.get("status") == "DIFF"
            )

    from proverka.summary import per_op_row_title
    from proverka.task_errors import attach_task_errors

    canon_rid = canonical_cropwise_report(group_name)
    try:
        wt_for_errors = work_type_ids_for_report(canon_rid) if canon_rid else wt_ids
    except ValueError:
        wt_for_errors = wt_ids

    attach_task_errors(
        out,
        report_id=source_report_id,
        work_title=per_op_row_title(source_report_id, group_name),
        wt_ids=wt_for_errors,
        t0=t0,
        t1=t1,
        task_season_years=list(task_years),
        shift_cache=shift_cache,
        season_cropwise=season_cropwise,
    )
    return out


def _collect_all_wt_ids(ops: dict[str, dict[str, Any]]) -> list[int]:
    out: set[int] = set()
    for gn, op in ops.items():
        wts = op.get("work_type_ids") or _WORK_GROUP_WT.get(gn) or []
        out.update(int(x) for x in wts)
    return sorted(out)


def reconcile_per_operation(
    spec: ReportSpec,
    report_date: date,
    cookie: str,
    firm_names: list[str],
    run_override: dict | None = None,
    *,
    prefetched_work: dict[str, Any] | None = None,
    peer_svodka: dict[str, dict[str, Any]] | None = None,
    prefetched_svodka: dict[str, Any] | None = None,
    batch_report_ids: set[str] | None = None,
) -> dict[str, Any]:
    from proverka.reconcile import _shift_window_iso

    t0, t1, _ = _shift_window_iso(spec, report_date)
    if prefetched_svodka is not None:
        svodka_local = prefetched_svodka
    else:
        svodka_local = asyncio.run(
            compute_svodka_facts(spec, report_date, run_override=run_override),
        )
    ops_local = filter_ops_for_batch(
        operations_from_svodka(svodka_local),
        batch_report_ids,
    )
    if not ops_local:
        return {
            "report_id": spec.report_id,
            "title": spec.title,
            "svodka": svodka_local,
            "operation_comparisons": {},
            "cropwise_work_report": {
                "ok": True,
                "per_operation": True,
                "operations_total": 0,
                "operations_ok": 0,
                "operations_error": 0,
                "note": "Нет операций для выбранного набора сводок",
            },
        }
    canon_ops = asyncio.run(
        _load_canonical_svodka_ops(
            list(ops_local.keys()), report_date, run_override, peer_svodka=peer_svodka,
        )
    )
    ops: dict[str, dict[str, Any]] = {}
    for gn, op_self in ops_local.items():
        ops[gn] = canon_ops.get(gn, op_self)

    op_prefetch = prefetched_work if isinstance(prefetched_work, dict) else None
    if op_prefetch is None and cookie:
        op_prefetch = prefetch_editor_season_works(list(ops_local), cookie, report_date)
    all_wt = _collect_all_wt_ids(ops_local)
    shift_cache = ShiftWindowCache.build(
        t0, t1, all_wt, log_prefix=f"[proverka r{spec.report_id}] ",
    )

    operation_comparisons: dict[str, dict[str, Any]] = {}
    workers = _max_op_workers(len(ops))

    def _run_one(group_name: str) -> tuple[str, dict[str, Any]]:
        op = ops[group_name]
        op_self = ops_local.get(group_name)
        work_pref = (op_prefetch or {}).get(group_name) if op_prefetch else None
        return group_name, _reconcile_one_operation(
            group_name,
            op,
            source_report_id=spec.report_id,
            report_date=report_date,
            cookie=cookie,
            firm_names=firm_names,
            t0=t0,
            t1=t1,
            prefetched_work=work_pref,
            run_override=run_override,
            shift_cache=shift_cache,
            op_local=op_self if group_name in canon_ops else None,
        )

    if workers > 1 and len(ops) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_one, gn): gn for gn in sorted(ops.keys())}
            for fut in as_completed(futures):
                gn, oc = fut.result()
                operation_comparisons[gn] = oc
    else:
        for group_name in sorted(ops.keys()):
            _, oc = _run_one(group_name)
            operation_comparisons[group_name] = oc

    ok_count = sum(
        1 for oc in operation_comparisons.values()
        if oc.get("comparison") and abs(float(oc["comparison"].get("total_diff_ha") or 0)) <= 5.0
    )
    err_count = sum(
        1 for oc in operation_comparisons.values()
        if not (oc.get("cropwise_work_report") or {}).get("ok")
    )

    merged_ops = dict(svodka_local.get("operations") or {})
    for gn, op_self in ops_local.items():
        oc = operation_comparisons.get(gn) or {}
        pick = op_self
        if float(pick.get("total_ha") or 0) < 0.01:
            from proverka.operation_compare import operation_for_proverka_display

            pick = operation_for_proverka_display(oc, fallback=op_self)
        if float(pick.get("total_ha") or 0) >= 0.01 or gn not in merged_ops:
            merged_ops[gn] = pick
    svodka_out = {**svodka_local, "operations": merged_ops}

    return {
        "report_id": spec.report_id,
        "title": spec.title,
        "svodka": svodka_out,
        "operation_comparisons": operation_comparisons,
        "cropwise_work_report": {
            "ok": err_count == 0,
            "per_operation": True,
            "operations_total": len(operation_comparisons),
            "operations_ok": ok_count,
            "operations_error": err_count,
        },
    }


reconcile_report2 = reconcile_per_operation
