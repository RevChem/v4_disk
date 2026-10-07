"""Сводки 1/2: каждая операция vs Cropwise work.json (как сводки 3–11)."""
from __future__ import annotations

import asyncio
import logging
import os
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any

from proverka.cropwise_ui import parse_work_report_body
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
    from proverka.task_names import tasks_for_group

    return tasks_for_group(group_name)


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
    from proverka.cropwise_ui import (
        WorkLoadJob,
        load_works_for_seasons,
        pack_loaded_work,
        ui_check_years,
    )
    from proverka.reconcile import _shift_window_iso

    cw_spec = _cropwise_spec_for_group(group_name)
    t0, t1, _ = _shift_window_iso(cw_spec, report_date)
    wt_ids = work_type_ids_for_group(group_name) or [
        int(x) for x in (_WORK_GROUP_WT.get(group_name) or [])
    ]
    hints = list(_fact_hints_for_group(group_name)) or list(cw_spec.fact_column_hints)
    years = ui_check_years(report_date)
    loaded = load_works_for_seasons(
        cookie,
        [WorkLoadJob(group_name, t0, t1, wt_ids, tuple(hints))],
        years=years,
    )
    return pack_loaded_work(loaded.get(group_name) or {}, years)


def prefetch_editor_season_works(
    group_names: list[str],
    cookie: str,
    report_date: date,
) -> dict[str, dict[str, Any]]:
    """
    work.json по сезонам года отчёта и следующего.

    Один последовательный проход по сессиям Cropwise на все операции.
    """
    from proverka.cropwise_ui import (
        WorkLoadJob,
        load_works_for_seasons,
        pack_loaded_work,
        ui_check_years,
    )
    from proverka.reconcile import _shift_window_iso

    years = ui_check_years(report_date)
    jobs: list[WorkLoadJob] = []
    for name in group_names:
        wt_ids = work_type_ids_for_group(name)
        if not wt_ids:
            continue
        spec = _cropwise_spec_for_group(name)
        t0, t1, _ = _shift_window_iso(spec, report_date)
        hints = list(_fact_hints_for_group(name)) or list(spec.fact_column_hints)
        jobs.append(WorkLoadJob(name, t0, t1, wt_ids, tuple(hints)))
    if not jobs:
        return {}
    loaded = load_works_for_seasons(cookie, jobs, years=years)
    return {
        job.key: pack_loaded_work(loaded.get(job.key) or {}, years)
        for job in jobs
    }


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
    canon_spec, season_cfg, task_years, season_rule = _canonical_season_for_operation(
        group_name, report_date, run_override,
    )
    hints = list(_fact_hints_for_group(group_name)) or list(canon_spec.fact_column_hints)
    wt_ids = _wt_ids_for_operation(group_name, op)
    if not wt_ids:
        return {
            "group_name": group_name,
            "canonical_report": canonical_cropwise_report(group_name),
            "error": "нет work_type_ids",
        }

    if isinstance(prefetched_work, dict):
        work = prefetched_work
    else:
        work = {"ok": False, "by_year": {}, "years": [], "error": "work.json не загружен"}

    display_op = op_local if op_local is not None else op
    svodka_total = float(display_op.get("total_ha") or 0)
    svodka_by_firm = display_op.get("area_today_by_firm") or {}
    canon_rid = _svodka_fact_report_id(group_name, op, source_report_id=source_report_id)
    if not season_cfg.get("task_season_years"):
        season_cfg = resolve_group_season_policy(group_name, report_date, run_override)
        task_years = list(season_cfg.get("task_season_years") or [])
        season_rule = str(season_cfg.get("season_rule") or season_rule)

    from proverka.cropwise_crops import build_season_cropwise

    combined = build_season_cropwise(
        work,
        report_date=report_date,
        firm_names=firm_names,
        fact_hints=hints,
        season_rule=season_rule,
        task_years=list(task_years or []),
        group_name=group_name,
        shift_cache=shift_cache,
        wt_ids=wt_ids,
    )
    parsed = combined.get("parsed") or {}
    seasons = combined["seasons"]
    cropwise_by_firm = combined["by_firm"]
    cropwise_total = float(combined["compare_ha"])
    ui_note = combined["note"]
    ui_session = None
    ui_session_blocked = False
    required_ui_session = None
    fetch_note = ""
    work["editor_crop_areas"] = combined["crops"]
    season_cropwise = {
        "cropwise_api_ha": combined.get("api_ha"),
        "cropwise_ui_ha": combined["both_seasons_ha"],
        "cropwise_ui_vs_api_ha": combined.get("ui_vs_api_ha"),
        "cropwise_api_fallback": False,
    }

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
        out["comparison"] = {
            "svodka_total_ha": svodka_total,
            "cropwise_fact_total_ha": 0.0,
            "total_diff_ha": round(svodka_total, 2),
            "cropwise_incomplete": True,
            "cropwise_api_fallback": False,
            "ui_degraded": False,
            "note": ui_note,
        }
        return out

    out["cropwise_parsed"] = parsed
    if seasons:
        out["cropwise_seasons"] = seasons

    out["comparison"] = {
        "svodka_total_ha": svodka_total,
        "cropwise_fact_total_ha": cropwise_total,
        "total_diff_ha": round(svodka_total - cropwise_total, 2),
        "fact_column": parsed.get("fact_column"),
        "season_policy": season_cfg,
        "ui_session_seasonValue": ui_session,
        "required_ui_sessionValue": required_ui_session,
        "ui_session_blocked": ui_session_blocked,
        "cropwise_api_fallback": False,
        "cropwise_incomplete": bool(combined["incomplete"]),
        "cropwise_warnings": list(combined["warnings"]),
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
        "ui_degraded": False,
    }
    for year, block in seasons.items():
        out["comparison"][f"cropwise_season_{year}_ha"] = block.get("total_ha")
        out["comparison"][f"cropwise_season_{year}_source"] = block.get("source")
        out["comparison"][f"cropwise_season_{year}_status"] = block.get("status")
    out["comparison"]["cropwise_both_seasons_ha"] = combined["both_seasons_ha"]
    if fetch_note:
        out["comparison"]["note"] += f" {fetch_note}"

    svodka_crop = display_op.get("area_today_by_firm_crop") or {}
    if svodka_crop and not combined["incomplete"]:
        from proverka.crop_compare import compare_svodka_cropwise_crops

        cw_crop = work.get("editor_crop_areas") or {}
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


logger = logging.getLogger(__name__)

# Отмену пользователя (ProverkaCancelled / MachineProverkaCancelled) глотать нельзя:
# иначе «Отмена» превратится в 15 «ошибок операций». Проверяем по имени класса,
# чтобы не импортировать runner (он сам импортирует этот модуль).
_CANCELLATION_NAMES = frozenset({"ProverkaCancelled", "MachineProverkaCancelled"})


def _is_cancellation(exc: BaseException) -> bool:
    return any(cls.__name__ in _CANCELLATION_NAMES for cls in type(exc).__mro__)


def _operation_failure(
    group_name: str,
    source_report_id: str,
    exc: BaseException,
) -> dict[str, Any]:
    """Результат операции, которая упала с исключением, — вместо краха всей сверки.

    Форма совместима с обычным результатом: нет ``comparison`` (не попадает в
    ``operations_ok``), ``cropwise_work_report.ok`` ложно (попадает в
    ``operations_error``). Флаг ``exception`` нужен, чтобы сводка нарушений
    показала сбой, а не приняла его за «Cropwise недоступен, свод 0 га».
    """
    message = f"{type(exc).__name__}: {exc}"
    try:
        canonical = canonical_cropwise_report(group_name)
    except Exception:
        canonical = None
    return {
        "group_name": group_name,
        "canonical_report": canonical,
        "svodka_source_report": source_report_id,
        "exception": True,
        "error": message,
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:],
        "cropwise_work_report": {"ok": False, "error": message, "exception": True},
    }


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
        # Сбой одной операции из N не должен ронять всю сверку сводки:
        # он записывается в результат этой операции (см. _operation_failure).
        try:
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
        except Exception as exc:
            if _is_cancellation(exc):
                raise
            logger.exception(
                "proverka r%s: сверка операции «%s» упала", spec.report_id, group_name,
            )
            return group_name, _operation_failure(group_name, spec.report_id, exc)

    if workers > 1 and len(ops) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_one, gn): gn for gn in sorted(ops.keys())}
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    gn, oc = fut.result()
                except Exception as exc:
                    # Сюда попадает только то, что вырвалось мимо _run_one
                    # (отмена пробрасывается выше намеренно).
                    if _is_cancellation(exc):
                        for other in futures:
                            other.cancel()
                        raise
                    logger.exception(
                        "proverka r%s: задача операции «%s» завершилась сбоем", spec.report_id, name,
                    )
                    gn, oc = name, _operation_failure(name, spec.report_id, exc)
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

    exc_count = sum(1 for oc in operation_comparisons.values() if oc.get("exception"))

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
            "operations_exception": exc_count,
        },
    }


reconcile_report2 = reconcile_per_operation
