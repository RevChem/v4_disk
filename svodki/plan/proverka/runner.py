"""Запуск пакетной сверки (CLI и API)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from datetime import date
from pathlib import Path
from typing import Any, Callable

from proverka.cropwise_ui import parse_work_report_body
from proverka.export_excel import export_excel
from proverka.file_values import attach_file_values
from proverka.operation_compare import build_cross_check
from proverka.reconcile import _firm_names, load_env
from proverka.reports_config import REPORTS, list_report_ids
from proverka.summary import build_rows, print_table
from proverka.svodka_facts import compute_svodka_facts
import asyncio


ProgressFn = Callable[[str], None] | None
CancelReportFn = Callable[[str | None], bool] | None


class ProverkaCancelled(RuntimeError):
    """Пользовательская отмена проверки."""

PLAN_DIR = Path(__file__).resolve().parent.parent


def _proverka_job_dir() -> Path | None:
    raw = os.environ.get("PROVERKA_JOB_DIR", "").strip()
    return Path(raw) if raw else None


def _cancel_file_set() -> bool:
    job = _proverka_job_dir()
    return job is not None and (job / "CANCEL").is_file()


def _track_pid(pid: int) -> None:
    job = _proverka_job_dir()
    if job is None:
        return
    pids = job / "pids"
    pids.mkdir(parents=True, exist_ok=True)
    (pids / str(pid)).write_text("", encoding="utf-8")


def _untrack_pid(pid: int) -> None:
    job = _proverka_job_dir()
    if job is None:
        return
    try:
        (job / "pids" / str(pid)).unlink(missing_ok=True)
    except OSError:
        pass


def _kill_proc_group(pid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                return
        if sig == signal.SIGTERM:
            time.sleep(0.2)


def _sleep_cancellable(seconds: float) -> None:
    end = time.monotonic() + max(0.0, seconds)
    while True:
        if _cancel_file_set():
            raise ProverkaCancelled("cancelled")
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.4, left))


def _wait_proc(proc: subprocess.Popen, *, timeout: float | None) -> int:
    """Ждёт процесс и прерывает его, как только в каталоге задачи появился файл CANCEL."""
    _track_pid(proc.pid)
    started = time.monotonic()
    try:
        while True:
            if _cancel_file_set():
                _kill_proc_group(proc.pid)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                raise ProverkaCancelled("cancelled")
            try:
                return proc.wait(timeout=0.4)
            except subprocess.TimeoutExpired:
                if timeout is not None and time.monotonic() - started > timeout:
                    _kill_proc_group(proc.pid)
                    raise subprocess.TimeoutExpired(proc.args, timeout) from None
    finally:
        _untrack_pid(proc.pid)


def _terminate_process_pool(pool: ProcessPoolExecutor) -> None:
    procs = [p for p in getattr(pool, "_processes", {}).values() if p is not None]
    for proc in procs:
        try:
            proc.terminate()
        except Exception:
            pass
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and any(p.is_alive() for p in procs):
        time.sleep(0.05)
    for proc in procs:
        if proc.is_alive():
            try:
                proc.kill()
            except Exception:
                pass


def _iter_done(futures: dict, *, pool: ProcessPoolExecutor | None = None, should_stop: Callable[[], bool] | None = None):
    """Отдаёт завершённые future и не ждёт их, если проверку уже отменили."""
    pending = set(futures)
    while pending:
        if _cancel_file_set() or (should_stop is not None and should_stop()):
            if isinstance(pool, ProcessPoolExecutor):
                _terminate_process_pool(pool)
            for fut in pending:
                fut.cancel()
            raise ProverkaCancelled("cancelled")
        finished, pending = wait(pending, timeout=0.4, return_when=FIRST_COMPLETED)
        yield from finished


def _bootstrap_plan_imports(plan_dir: Path | None = None) -> None:
    root = plan_dir or PLAN_DIR
    s = str(root)
    if s not in sys.path:
        sys.path.insert(0, s)
    from plan_paths import ensure_plan_sys_path

    ensure_plan_sys_path()


def _needs_cropwise_ui(spec) -> bool:
    return bool(spec.cropwise_comparable or spec.cropwise_per_operation)

# Канарейка: внесение МУ за смену обычно >> 500 га; 182 га — seasonValue=2026 в UI.
# Порог настраивается: в межсезонье реальные объёмы ниже, и фиксированные 500 га
# заставляли ждать 90 секунд впустую. 0 отключает проверку.
_CROPWISE_CANARY_REPORT = "3"


def _canary_min_ha() -> float:
    try:
        return float(os.environ.get("PROVERKA_CANARY_MIN_HA", "500") or 0)
    except ValueError:
        return 500.0


def _max_workers(report_count: int) -> int:
    default = max(1, min(4, int(os.environ.get("PROVERKA_MAX_WORKERS", "4"))))
    return max(1, min(default, report_count))


def _reconcile_payload_sort_key(rid: str) -> tuple[int, str]:
    """Тяжёлые сводки 1/2 (per-op) — в конец, чтобы быстрые завершались первыми."""
    spec = REPORTS.get(rid)
    if spec and spec.cropwise_per_operation:
        return (2, rid)
    return (0, rid)


def _parallel_reconcile_mode(workers: int, cropwise_count: int) -> str:
    if cropwise_count <= 1 or workers <= 1:
        return "последовательно (1 сводка или PROVERKA_MAX_WORKERS=1)"
    if _process_pool_allowed():
        return f"ProcessPool ×{workers}"
    return f"subprocess ×{workers} (Celery-safe)"


def _process_pool_allowed() -> bool:
    """
    Celery ForkPoolWorker и другие daemon-процессы не могут порождать дочерние процессы.
    ProcessPoolExecutor в них даёт: daemonic processes are not allowed to have children.
    """
    if os.environ.get("PROVERKA_FORCE_PROCESS_POOL", "").strip().lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("SVODKI_SERVICE", "").strip().lower() in ("worker", "beat"):
        return False
    try:
        from celery import current_task

        req = getattr(current_task, "request", None)
        if req is not None and getattr(req, "id", None):
            return False
    except Exception:
        pass
    try:
        from multiprocessing import current_process

        return not current_process().daemon
    except Exception:
        return False


def _noop_progress(_: str) -> None:
    pass


def _preferred_ui_session(report_date: date, report_ids: list[str]) -> int | None:
    """Осенью Cropwise UI фильтрует work.json по seasonValue=год+1."""
    if not any(_needs_cropwise_ui(REPORTS[rid]) for rid in report_ids):
        return None
    y = report_date.year
    for rid in report_ids:
        spec = REPORTS[rid]
        if not _needs_cropwise_ui(spec):
            continue
        if spec.season_rule in ("both_years", "year_plus_one", "plow_year_plus_one"):
            return y + 1
    return None


def _fetch_work_one(
    rid: str,
    cookie: str,
    report_date: date,
    preferred: int | None,
    *,
    attempts: int = 2,
    delay_sec: float = 1.5,
) -> tuple[str, dict[str, Any]]:
    from proverka.cropwise_ui import fetch_work_report_resilient
    from proverka.reconcile import _shift_window_iso

    spec = REPORTS[rid]
    t0, t1, wt_ids = _shift_window_iso(spec, report_date)
    work = fetch_work_report_resilient(
        cookie, t0, t1, wt_ids, spec.fact_column_hints,
        attempts=attempts, delay_sec=delay_sec,
        preferred_ui_session=preferred,
    )
    return rid, work


def _per_op_prefetch_groups(report_ids: list[str]) -> list[tuple[str, list[int]]]:
    from proverka.work_groups import work_group_wt_map
    from proverka.operation_registry import OPERATION_PEERS, peer_reports_for_group
    from proverka.operation_registry import canonical_cropwise_report
    from proverka.report2_reconcile import _thematic_cropwise_ids

    per_op_rids = {rid for rid in report_ids if REPORTS[rid].cropwise_per_operation}
    if not per_op_rids:
        return []

    thematic = _thematic_cropwise_ids(set(report_ids))
    wt_map = work_group_wt_map()
    out: list[tuple[str, list[int]]] = []
    seen: set[str] = set()
    for gn in OPERATION_PEERS:
        peers = set(peer_reports_for_group(gn))
        if not peers & per_op_rids:
            continue
        if per_op_rids and thematic:
            canon = canonical_cropwise_report(gn)
            if canon and canon not in thematic:
                continue
        if gn in seen:
            continue
        wt = wt_map.get(gn)
        if wt:
            out.append((gn, list(wt)))
            seen.add(gn)
    return out


def _fetch_work_per_operation(
    group_name: str,
    wt_ids: list[int],
    cookie: str,
    report_date: date,
    preferred: int | None,
) -> tuple[str, dict[str, Any]]:
    from proverka.report2_reconcile import fetch_work_for_operation

    _ = wt_ids, preferred
    work = fetch_work_for_operation(group_name, cookie, report_date, None)
    return group_name, work


def _prefetch_work_json(
    cookie: str,
    report_date: date,
    report_ids: list[str],
    prog: ProgressFn,
) -> tuple[dict[str, dict[str, Any]], int | None]:
    """work.json до bootstrap/API v3 — параллельно по сводкам."""
    from proverka.cropwise_ui import warm_web_session

    preferred = _preferred_ui_session(report_date, report_ids)
    _, _, _, ui_session = warm_web_session(cookie)
    comparable = [rid for rid in report_ids if _needs_cropwise_ui(REPORTS[rid])]
    if preferred and ui_session != preferred:
        # Не ошибка: work.json отдаёт сезон сессии, остальные сезоны сводок
        # берутся из API v3 по machine_task.season.
        prog(
            f"Cropwise UI: seasonValue={ui_session or '?'} (в UI виден этот сезон, "
            f"остальные сезоны — из API v3) — загрузка work.json…"
        )
    else:
        prog(f"Cropwise UI: seasonValue={ui_session or '?'} — загрузка work.json…")

    prefetched: dict[str, dict[str, Any]] = {}
    if not comparable:
        return prefetched, ui_session

    per_op_rids = [rid for rid in comparable if REPORTS[rid].cropwise_per_operation]
    per_op_fetch_groups = [gn for gn, _wt in _per_op_prefetch_groups(comparable)]
    std_rids = [rid for rid in comparable if not REPORTS[rid].cropwise_per_operation]
    editor_groups = list(per_op_fetch_groups)
    for rid in std_rids:
        groups = tuple(REPORTS[rid].work_groups or ())
        if len(groups) == 1 and groups[0] not in editor_groups:
            editor_groups.append(groups[0])
    packed_editor: dict[str, dict[str, Any]] = {}
    if editor_groups:
        from proverka.report2_reconcile import prefetch_editor_season_works

        prog(
            f"Cropwise UI: сезоны редактора планов, {len(editor_groups)} операций "
            "(2026 и 2027 складываются, один сезон проверяется отдельно)…"
        )
        packed_editor = prefetch_editor_season_works(editor_groups, cookie, report_date)
        if per_op_fetch_groups:
            op_map = {
                gn: packed_editor[gn]
                for gn in per_op_fetch_groups
                if gn in packed_editor
            }
            for rid in per_op_rids:
                prefetched[rid] = op_map
            for work in op_map.values():
                ui_session = work.get("ui_session_seasonValue") or ui_session
    remaining_std = []
    for rid in std_rids:
        groups = tuple(REPORTS[rid].work_groups or ())
        if len(groups) == 1 and groups[0] in packed_editor:
            prefetched[rid] = packed_editor[groups[0]]
            ui_session = packed_editor[groups[0]].get("ui_session_seasonValue") or ui_session
        else:
            remaining_std.append(rid)
    std_rids = remaining_std
    per_op_fetch_groups = [
        gn for gn in per_op_fetch_groups if gn not in packed_editor
    ]
    task_count = len(std_rids) + len(per_op_fetch_groups)
    workers = _max_workers(max(task_count, 1))

    if task_count == 0:
        pass
    elif workers == 1:
        for rid in std_rids:
            _, work = _fetch_work_one(rid, cookie, report_date, preferred)
            prefetched[rid] = work
            ui_session = work.get("ui_session_seasonValue") or ui_session
        if per_op_fetch_groups:
            from proverka.work_groups import work_group_wt_map

            wt_by_group = work_group_wt_map()
            op_map: dict[str, dict[str, Any]] = {}
            for gn0 in per_op_fetch_groups:
                _, work = _fetch_work_per_operation(
                    gn0, list(wt_by_group.get(gn0) or []), cookie, report_date, preferred,
                )
                op_map[gn0] = work
                ui_session = work.get("ui_session_seasonValue") or ui_session
            for rid in per_op_rids:
                prefetched[rid] = op_map
    else:
        label = f"{len(std_rids)} сводок"
        if per_op_fetch_groups:
            label += f" + {len(per_op_fetch_groups)} операций (св.1/2)"
        prog(f"Cropwise UI: параллельная загрузка work.json ({label}, {workers} потоков)…")
        from proverka.work_groups import work_group_wt_map

        wt_by_group = work_group_wt_map()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for rid in std_rids:
                futures[pool.submit(_fetch_work_one, rid, cookie, report_date, preferred)] = ("rid", rid)
            for gn0 in per_op_fetch_groups:
                futures[pool.submit(
                    _fetch_work_per_operation,
                    gn0,
                    list(wt_by_group.get(gn0) or []),
                    cookie,
                    report_date,
                    preferred,
                )] = ("per_op", gn0)
            op_map: dict[str, dict[str, Any]] = {}
            for fut in _iter_done(futures):
                kind, key = futures[fut]
                if kind == "rid":
                    rid, work = fut.result()
                    prefetched[rid] = work
                else:
                    gn0, work = fut.result()
                    op_map[gn0] = work
                ui_session = work.get("ui_session_seasonValue") or ui_session
            if op_map:
                for rid in per_op_rids:
                    prefetched[rid] = op_map

    if preferred and ui_session != preferred:
        prog(
            f"Cropwise UI: seasonValue={ui_session}; сезон {preferred} сверяется по API v3. "
            f"Чтобы видеть {preferred} и в UI, задайте "
            f"CROPWISE_SESSION_COOKIE_{preferred} в .env "
            f"(cookie из браузера с выбранным сезоном {preferred})."
        )
    return prefetched, ui_session


def _inject_canonical_svodka_for_cross_check(
    all_results: dict[str, Any],
    report_date: date,
    overrides_map: dict[str, dict[str, Any]],
    svodka_cache: dict[str, dict[str, Any]],
    prog: ProgressFn,
) -> None:
    """
    Подмешивает svodka-facts недостающих peer-сводок для cross-check
    (например 10/11/13 для 5.0), если пользователь их не выбирал в batch.
    """
    from proverka.operation_registry import OPERATION_PEERS, peer_reports_for_group

    reports = all_results.get("reports") or {}
    batch_ids = set(reports.keys())
    needed: set[str] = set()
    for group_name in OPERATION_PEERS:
        peers = set(peer_reports_for_group(group_name))
        if batch_ids & peers:
            needed |= peers
    to_load = sorted(rid for rid in needed if rid not in batch_ids and rid in REPORTS)
    if not to_load:
        return
    for rid in to_load:
        if rid in svodka_cache:
            sv = svodka_cache[rid]
        else:
            spec = REPORTS[rid]
            prog(f"Cross-check: факты сводки {rid} ({spec.title})…")
            sv = asyncio.run(
                compute_svodka_facts(
                    spec, report_date, run_override=overrides_map.get(rid) or None,
                ),
            )
        reports[rid] = {
            "report_id": rid,
            "title": REPORTS[rid].title,
            "svodka": sv,
        }


def _refresh_work_json_after_svodka(
    cookie: str,
    report_date: date,
    report_ids: list[str],
    prefetched_work: dict[str, dict[str, Any]],
    svodka_cache: dict[str, dict[str, Any]],
    prog: ProgressFn,
) -> tuple[dict[str, dict[str, Any]], int | None]:
    """
    После расчёта сводок (массовые API v3) UI часто отдаёт урезанный work.json (~1941 vs ~2226).
    Перезагружаем work.json с preferred seasonValue до reconcile.
    """
    from proverka.cropwise_ui import cropwise_total_ha
    from proverka.reconcile import _cropwise_retry_warranted
    from proverka.season_cropwise import ui_session_ok_for_years

    preferred = _preferred_ui_session(report_date, report_ids)
    ui_session: int | None = None
    refreshed = 0

    comparable = [rid for rid in report_ids if _needs_cropwise_ui(REPORTS[rid])]
    std_rids = [rid for rid in comparable if not REPORTS[rid].cropwise_per_operation]

    for rid in std_rids:
        work = prefetched_work.get(rid)
        if not isinstance(work, dict):
            continue
        if work.get("editor_years"):
            ui_session = work.get("ui_session_seasonValue") or ui_session
            continue
        ui_session = work.get("ui_session_seasonValue") or ui_session
        spec = REPORTS[rid]
        svodka = svodka_cache.get(rid) or {}
        sv_total = float(svodka.get("total_ha") or 0)
        # Сезон сессии Cropwise запросом не сменить, поэтому перезагружаем
        # work.json только если сессия не отдаёт ни один из сезонов сводки.
        sv_years = list((svodka.get("season_policy") or {}).get("task_season_years") or [])
        need_refresh = bool(
            preferred
            and work.get("ui_session_seasonValue") != preferred
            and not ui_session_ok_for_years(work.get("ui_session_seasonValue"), sv_years)
        )
        cw_total = 0.0
        if not need_refresh and work.get("ok") and isinstance(work.get("body"), dict) and sv_total > 1.0:
            cw_total = cropwise_total_ha(work, spec.fact_column_hints)
            need_refresh = _cropwise_retry_warranted(
                sv_total,
                cw_total,
                ui_session=work.get("ui_session_seasonValue"),
                preferred_ui=preferred,
                acceptable_ui_seasons=sv_years,
            )
        if need_refresh:
            prog(
                f"Cropwise UI: повтор work.json сводки {rid} "
                f"(svodka {sv_total:.0f} vs cropwise {cw_total:.0f}, session={work.get('ui_session_seasonValue')})…"
            )
            _, work_new = _fetch_work_one(
                rid, cookie, report_date, preferred, attempts=4, delay_sec=3.0,
            )
            if work_new.get("ok"):
                prefetched_work[rid] = work_new
                ui_session = work_new.get("ui_session_seasonValue") or ui_session
                refreshed += 1

    per_op_rids = [rid for rid in comparable if REPORTS[rid].cropwise_per_operation]
    if per_op_rids:
        groups = _per_op_prefetch_groups(report_ids)
        op_map = prefetched_work.get(per_op_rids[0])
        if not isinstance(op_map, dict):
            op_map = {}
        for gn, wt_ids in groups:
            work = op_map.get(gn)
            if not isinstance(work, dict):
                continue
            if work.get("editor_years"):
                ui_session = work.get("ui_session_seasonValue") or ui_session
                continue
            from proverka.operation_registry import canonical_cropwise_report

            canon_rid = canonical_cropwise_report(gn)
            canon = REPORTS.get(canon_rid) if canon_rid else None
            if not canon:
                continue
            sv_total = 0.0
            for rid in per_op_rids:
                sv = svodka_cache.get(rid) or {}
                for op in (sv.get("operations") or {}).values():
                    if op.get("group_name") == gn:
                        sv_total = max(sv_total, float(op.get("total_ha") or 0))
            from proverka.season_policy import resolve_group_season_policy

            gn_years = list(
                resolve_group_season_policy(gn, report_date).get("task_season_years") or []
            )
            need_refresh = bool(
                preferred
                and work.get("ui_session_seasonValue") != preferred
                and not ui_session_ok_for_years(work.get("ui_session_seasonValue"), gn_years)
            )
            cw_total = 0.0
            if not need_refresh and work.get("ok") and isinstance(work.get("body"), dict) and sv_total > 1.0:
                cw_total = cropwise_total_ha(work, canon.fact_column_hints)
                need_refresh = _cropwise_retry_warranted(
                    sv_total,
                    cw_total,
                    ui_session=work.get("ui_session_seasonValue"),
                    preferred_ui=preferred,
                    acceptable_ui_seasons=gn_years,
                )
            if need_refresh:
                prog(
                    f"Cropwise UI: повтор work.json операции «{gn}» "
                    f"(svodka {sv_total:.0f} vs cropwise {cw_total:.0f})…"
                )
                _, work_new = _fetch_work_per_operation(
                    gn, list(wt_ids), cookie, report_date, preferred,
                )
                if work_new.get("ok"):
                    op_map[gn] = work_new
                    ui_session = work_new.get("ui_session_seasonValue") or ui_session
                    refreshed += 1
        for rid in per_op_rids:
            prefetched_work[rid] = op_map

    if refreshed:
        prog(f"Cropwise UI: обновлено work.json: {refreshed}")
    return prefetched_work, ui_session


def _wait_for_cropwise_ui(
    cookie: str,
    report_date: date,
    prog: ProgressFn,
    prefetched_mu: dict[str, Any] | None,
    ui_session: int | None,
) -> None:
    """Канарейка МУ: живой ли work.json (не урезан ли ответ Cropwise)."""
    from proverka.cropwise_ui import cropwise_total_ha, fetch_work_report_resilient
    from proverka.reconcile import _shift_window_iso
    from proverka.season_cropwise import ui_session_ok_for_years
    from proverka.season_runtime import resolve_report_season_config

    spec = REPORTS.get(_CROPWISE_CANARY_REPORT)
    if not spec or not _needs_cropwise_ui(spec):
        return
    min_ha = _canary_min_ha()
    if min_ha <= 0:
        return

    need_session = report_date.year + 1
    # Сезон сессии, не входящий в сезоны сводки, — не повод пропускать канарейку:
    # такой сезон сверяется по API v3, а канарейка проверяет работоспособность UI.
    mu_years = list(
        resolve_report_season_config(spec, report_date).get("task_season_years") or []
    )
    if ui_session is not None and mu_years and not ui_session_ok_for_years(ui_session, mu_years):
        prog(
            f"⚠ Cropwise UI: seasonValue={ui_session} вне сезонов МУ "
            f"({'+'.join(map(str, mu_years))}) — сверка пойдёт по API v3."
        )
        return

    t0, t1, wt_ids = _shift_window_iso(spec, report_date)
    hints = spec.fact_column_hints
    work = prefetched_mu
    if not work or not work.get("ok"):
        work = fetch_work_report_resilient(
            cookie, t0, t1, wt_ids, hints, attempts=2, delay_sec=4.0,
            preferred_ui_session=need_session,
        )
    total = cropwise_total_ha(work, hints)
    body = work.get("body") or {}
    if not body.get("reportBlank") and total >= min_ha:
        prog(f"Cropwise UI: канарейка OK ({total:.0f} га)")
        return

    waits = (15, 30, 45)
    for pause in waits:
        prog(f"Cropwise UI: канарейка < {min_ha:.0f} га, пауза {pause}с…")
        _sleep_cancellable(pause)
        work = fetch_work_report_resilient(
            cookie, t0, t1, wt_ids, hints, attempts=2, delay_sec=4.0,
            preferred_ui_session=need_session,
        )
        total = cropwise_total_ha(work, hints)
        if not (work.get("body") or {}).get("reportBlank") and total >= min_ha:
            prog(f"Cropwise UI: канарейка OK ({total:.0f} га)")
            return
    prog("Cropwise UI: канарейка низкая — возможен UI degraded или неверный сезон в UI")


def _collect_prefetch_report_ids(report_ids: list[str]) -> list[str]:
    from proverka.operation_registry import OPERATION_PEERS, peer_reports_for_group

    out = {str(r) for r in report_ids if str(r) in REPORTS}
    for group_name in OPERATION_PEERS:
        peers = set(peer_reports_for_group(group_name))
        if out & peers:
            out |= peers
    return sorted(out, key=lambda x: (len(x), x))


def _activate_svodki_api_snapshot(
    report_date: date,
    report_ids: list[str],
    out_dir: Path,
    prog: ProgressFn,
) -> None:
    """
    Если SVODKI_USE_PREFETCH_DATA=1 — тот же JSON-снимок API, что при генерации Excel.
    """
    if os.environ.get("SVODKI_USE_PREFETCH_DATA", "0").strip().lower() not in ("1", "true", "yes"):
        return
    prefetch_path = out_dir / f"prefetch_{report_date.isoformat()}.json"
    if not prefetch_path.is_file():
        script = PLAN_DIR / "svodki_prefetch.py"
        if not script.is_file():
            prog("Prefetch: svodki_prefetch.py не найден — live API")
            return
        rids = _collect_prefetch_report_ids(report_ids)
        prog(f"Prefetch JSON для {len(rids)} сводок…")
        env = os.environ.copy()
        env["REPORT_DATE"] = report_date.isoformat()
        env["SVODKI_PREFETCH_REPORT_IDS"] = ",".join(rids)
        env["SVODKI_DISABLE_API_CACHE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        plan_s = str(PLAN_DIR)
        env["PYTHONPATH"] = plan_s + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        stdout_path = out_dir / ".prefetch.stdout"
        stderr_path = out_dir / ".prefetch.stderr"
        try:
            with stdout_path.open("w", encoding="utf-8") as so, stderr_path.open("w", encoding="utf-8") as se:
                proc = subprocess.Popen(
                    [sys.executable, str(script), str(prefetch_path)],
                    env=env,
                    cwd=str(out_dir),
                    stdout=so,
                    stderr=se,
                    start_new_session=True,
                )
                rc = _wait_proc(proc, timeout=900)
        except subprocess.TimeoutExpired:
            prog("Prefetch JSON: timeout — live API")
            return
        out_text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
        err_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
        if rc != 0 or not prefetch_path.is_file():
            tail = (err_text or out_text).strip().splitlines()
            hint = tail[-1][:200] if tail else f"rc={rc}"
            prog(f"Prefetch JSON: ошибка ({hint}) — live API")
            return
    os.environ["SVODKI_PREFETCH_DATA_PATH"] = str(prefetch_path)
    os.environ.pop("SVODKI_DISABLE_API_CACHE", None)
    try:
        import sitecustomize

        sitecustomize.install_svodki_api_hooks()
    except Exception:
        pass
    prog(f"Prefetch JSON: {prefetch_path.name}")


def _load_svodka_cache(path: str | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _svodka_worker(payload: tuple[Any, ...]) -> tuple[str, dict[str, Any]]:
    rid, report_date_iso, run_ov, plan_dir_str = payload

    _bootstrap_plan_imports(Path(plan_dir_str))
    from datetime import date as date_cls

    from proverka.reconcile import load_env
    from proverka.reports_config import REPORTS
    from proverka.svodka_facts import compute_svodka_facts

    load_env()
    try:
        import sitecustomize

        sitecustomize.install_svodki_api_hooks()
    except Exception:
        pass
    spec = REPORTS[rid]
    svodka = asyncio.run(
        compute_svodka_facts(spec, date_cls.fromisoformat(report_date_iso), run_override=run_ov),
    )
    return rid, svodka


def _prefetch_svodka_facts(
    report_ids: list[str],
    report_date: date,
    overrides_map: dict[str, dict[str, Any]],
    prog: ProgressFn,
) -> dict[str, dict[str, Any]]:
    """Один раз на batch — факты сводок (как PDF), без повторов в reconcile."""
    rids = [rid for rid in report_ids if rid in REPORTS]
    if not rids:
        return {}
    workers = _max_workers(len(rids))
    payloads = [
        (rid, report_date.isoformat(), overrides_map.get(rid) or None, str(PLAN_DIR))
        for rid in rids
    ]
    cache: dict[str, dict[str, Any]] = {}
    if workers > 1 and len(rids) > 1 and _process_pool_allowed():
        prog(f"Факты сводок: {len(rids)} параллельно ({workers} процессов)…")
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_svodka_worker, p): p[0] for p in payloads}
            for fut in _iter_done(futures, pool=pool):
                rid, sv = fut.result()
                cache[rid] = sv
    elif workers > 1 and len(rids) > 1:
        prog(f"Факты сводок: {len(rids)} параллельно ({workers} subprocess)…")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_svodka_subprocess, p): p[0] for p in payloads}
            for fut in _iter_done(futures):
                rid, sv = fut.result()
                cache[rid] = sv
    else:
        prog(f"Факты сводок: {len(rids)} последовательно…")
        for p in payloads:
            rid, sv = _svodka_worker(p)
            cache[rid] = sv
    return cache


def _reconcile_worker(payload: tuple[Any, ...]) -> tuple[str, dict[str, Any]]:
    """Отдельный процесс на сводку — без гонок os.environ между модулями."""
    rid, report_date_iso, cookie, run_ov, prefetched, plan_dir_str = payload[:6]
    svodka_cache_path = payload[6] if len(payload) > 6 else None
    batch_rids_raw = payload[7] if len(payload) > 7 else []

    _bootstrap_plan_imports(Path(plan_dir_str))
    from datetime import date as date_cls

    from proverka.reconcile import load_env, reconcile_one
    from proverka.reports_config import REPORTS

    load_env()
    spec = REPORTS[rid]
    svodka_cache = _load_svodka_cache(svodka_cache_path)
    batch_ids = set(str(x) for x in batch_rids_raw)
    result = reconcile_one(
        spec,
        date_cls.fromisoformat(report_date_iso),
        cookie,
        _firm_names(spec),
        run_ov,
        prefetched_work=prefetched,
        prefetched_svodka=svodka_cache.get(rid),
        peer_svodka=svodka_cache,
        batch_report_ids=batch_ids,
    )
    return rid, result


def _run_in_subprocess(
    helper_name: str, payload: tuple[Any, ...], *, prefix: str,
) -> tuple[str, dict[str, Any]]:
    """Один OS-процесс на задачу — Celery-safe и без гонок глобального состояния."""
    helper = Path(__file__).resolve().parent / helper_name
    with tempfile.TemporaryDirectory(prefix=prefix) as td:
        td_path = Path(td)
        inp = td_path / "in.json"
        out = td_path / "out.json"
        inp.write_text(json.dumps(list(payload), ensure_ascii=False, default=str), encoding="utf-8")
        log_out = td_path / "stdout.txt"
        log_err = td_path / "stderr.txt"
        with log_out.open("w", encoding="utf-8") as so, log_err.open("w", encoding="utf-8") as se:
            proc = subprocess.Popen(
                [sys.executable, str(helper), str(inp), str(out)],
                stdout=so,
                stderr=se,
                start_new_session=True,
            )
            rc = _wait_proc(proc, timeout=None)
        if rc != 0:
            err = ""
            if log_err.is_file():
                err = log_err.read_text(encoding="utf-8", errors="replace")
            if not err.strip() and log_out.is_file():
                err = log_out.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(f"{helper_name} failed (exit {rc}): {err.strip()[-800:]}")
        rid, result = json.loads(out.read_text(encoding="utf-8"))
        return rid, result


def _svodka_subprocess(payload: tuple[Any, ...]) -> tuple[str, dict[str, Any]]:
    return _run_in_subprocess(
        "_svodka_subprocess_main.py", payload, prefix="proverka_svodka_",
    )


def _reconcile_subprocess(payload: tuple[Any, ...]) -> tuple[str, dict[str, Any]]:
    """
    Отдельный OS-процесс на сводку — работает в Celery ForkPoolWorker
    (multiprocessing.ProcessPool там запрещён).
    """
    return _run_in_subprocess(
        "_reconcile_subprocess_main.py", payload, prefix="proverka_reconcile_",
    )


def _store_reconcile_result(
    rid: str,
    result: dict[str, Any],
    *,
    out_dir: Path,
    write_json: bool,
    all_results: dict[str, Any],
) -> int:
    code = 0
    all_results["reports"][rid] = result
    if not result["cropwise_work_report"].get("ok"):
        code = 2
    if write_json:
        report_date = all_results["report_date"]
        single = out_dir / f"reconcile_report{rid}_{report_date}.json"
        single.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return code


def _svodka_only_one(
    spec,
    report_date: date,
    run_override: dict[str, Any] | None = None,
    *,
    prefetched_svodka: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if prefetched_svodka is not None:
        svodka = prefetched_svodka
    else:
        svodka = asyncio.run(compute_svodka_facts(spec, report_date, run_override=run_override))
    return {
        "report_id": spec.report_id,
        "title": spec.title,
        "svodka": svodka,
        "cropwise_work_report": {"ok": True, "skipped": True},
        "cropwise_parsed": {
            "fact_by_firm": {},
            "fact_total_ha": 0.0,
            "detail_rows": [],
        },
        "comparison": {
            "svodka_total_ha": svodka["total_ha"],
            "cropwise_fact_total_ha": 0.0,
            "total_diff_ha": svodka["total_ha"],
            "by_firm_diff": {},
            "note": "Режим «только сводки» — Cropwise не запрашивался.",
        },
    }


def run_proverka_batch(
    report_date: date,
    report_ids: list[str],
    out_dir: Path,
    *,
    cookie: str = "",
    compare_cropwise: bool = True,
    write_excel: bool = True,
    write_json: bool = True,
    progress: ProgressFn = None,
    season_overrides: dict[str, dict[str, Any]] | None = None,
    should_cancel_report: CancelReportFn = None,
) -> dict[str, Any]:
    """
    Сверка выбранных сводок. compare_cropwise=False — только расчёт сводок + матрица в Excel.
    """
    load_env()
    out_dir.mkdir(parents=True, exist_ok=True)
    prog = progress or _noop_progress

    for rid in report_ids:
        if rid not in REPORTS:
            raise ValueError(f"Неизвестная сводка {rid!r}")

    if compare_cropwise and not cookie.strip():
        raise ValueError("Для сравнения с Cropwise нужна cookie браузера")
    cookie = cookie.strip()

    all_results: dict[str, Any] = {
        "report_date": report_date.isoformat(),
        "compare_mode": "cropwise" if compare_cropwise else "svodki_only",
        "reports": {},
    }
    exit_code = 0
    cancelled_reports: list[str] = []

    def _check_cancelled(rid: str | None = None) -> bool:
        if should_cancel_report is None:
            return False
        return bool(should_cancel_report(rid))

    def _skip_cancelled(rid: str) -> bool:
        if not _check_cancelled(rid):
            return False
        cancelled_reports.append(rid)
        prog(f"Сводка {rid}: отменена")
        return True

    if _check_cancelled(None):
        raise ProverkaCancelled("cancelled")

    overrides_map = dict(season_overrides or {})

    prefetched_work: dict[str, dict[str, Any]] = {}
    ui_session: int | None = None
    if compare_cropwise:
        prefetched_work, ui_session = _prefetch_work_json(
            cookie, report_date, report_ids, prog,
        )
        if _CROPWISE_CANARY_REPORT in report_ids:
            _wait_for_cropwise_ui(
                cookie, report_date, prog,
                prefetched_work.get(_CROPWISE_CANARY_REPORT),
                ui_session,
            )

    # Кэш карты поле→агрофирма живёт в out_dir, поэтому переживает
    # ProcessPool/subprocess: иначе fields+field_groups+group_folders
    # перезагружались в каждом процессе на каждую сводку.
    os.environ["PROVERKA_CACHE_DIR"] = str(out_dir)

    from proverka.season_cropwise import clear_field_to_firm_cache

    clear_field_to_firm_cache()

    from report_platform import bootstrap_platform

    bootstrap_platform(report_date, temp_dir=out_dir)
    _activate_svodki_api_snapshot(report_date, report_ids, out_dir, prog)

    prog("Расчёт фактов сводок (один раз на batch)…")
    svodka_cache = _prefetch_svodka_facts(report_ids, report_date, overrides_map, prog)
    svodka_cache_path = out_dir / f"svodka_cache_{report_date.isoformat()}.json"
    svodka_cache_path.write_text(
        json.dumps(svodka_cache, ensure_ascii=False, default=str), encoding="utf-8",
    )

    if compare_cropwise and prefetched_work:
        prefetched_work, ui_session = _refresh_work_json_after_svodka(
            cookie, report_date, report_ids, prefetched_work, svodka_cache, prog,
        )

    cropwise_rids = [
        rid for rid in report_ids
        if compare_cropwise and _needs_cropwise_ui(REPORTS[rid]) and not _check_cancelled(rid)
    ]
    svodka_only_rids = [
        rid for rid in report_ids if rid not in cropwise_rids and not _check_cancelled(rid)
    ]
    for rid in report_ids:
        if _check_cancelled(rid):
            cancelled_reports.append(rid)
    workers = _max_workers(len(cropwise_rids))
    use_process_pool = workers > 1 and _process_pool_allowed()
    use_subprocess_pool = workers > 1 and not use_process_pool and bool(cropwise_rids)

    from proverka.reconcile import reconcile_one

    batch_rids_list = list(report_ids)
    payloads = sorted(
        [
            (
                rid,
                report_date.isoformat(),
                cookie,
                overrides_map.get(rid) or None,
                prefetched_work.get(rid),
                str(PLAN_DIR),
                str(svodka_cache_path),
                batch_rids_list,
            )
            for rid in cropwise_rids
        ],
        key=lambda p: _reconcile_payload_sort_key(p[0]),
    )

    parallel_mode = _parallel_reconcile_mode(workers, len(cropwise_rids))
    prog(
        f"Сверка Cropwise: {len(cropwise_rids)} сводок, режим {parallel_mode}"
        + (f" (PROVERKA_MAX_WORKERS={os.environ.get('PROVERKA_MAX_WORKERS', '4')})" if len(cropwise_rids) > 1 else "")
    )

    def _store_parallel(rid: str, result: dict[str, Any], done: int, total: int) -> None:
        nonlocal exit_code
        exit_code |= _store_reconcile_result(
            rid, result, out_dir=out_dir, write_json=write_json, all_results=all_results,
        )
        title = REPORTS.get(rid, REPORTS.get(str(rid)))
        name = title.title if title else rid
        prog(f"Сводка {rid} ({name}): готово {done}/{total}")

    if _check_cancelled(None):
        raise ProverkaCancelled("cancelled")

    if cropwise_rids and use_process_pool:
        total = len(payloads)
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_reconcile_worker, p): p[0] for p in payloads}
            done = 0
            for fut in _iter_done(futures, pool=pool, should_stop=lambda: _check_cancelled(None)):
                rid, result = fut.result()
                done += 1
                _store_parallel(rid, result, done, total)
    elif cropwise_rids and use_subprocess_pool:
        total = len(payloads)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_reconcile_subprocess, p): p[0] for p in payloads}
            done = 0
            for fut in _iter_done(futures, should_stop=lambda: _check_cancelled(None)):
                rid, result = fut.result()
                done += 1
                _store_parallel(rid, result, done, total)
    else:
        for rid in cropwise_rids:
            if _skip_cancelled(rid):
                continue
            if _check_cancelled(None):
                raise ProverkaCancelled("cancelled")
            spec = REPORTS[rid]
            run_ov = overrides_map.get(rid) or None
            prog(f"Сводка {rid}: {spec.title}")
            result = reconcile_one(
                spec,
                report_date,
                cookie,
                _firm_names(spec),
                run_override=run_ov,
                prefetched_work=prefetched_work.get(rid),
                prefetched_svodka=svodka_cache.get(rid),
                peer_svodka=svodka_cache,
                batch_report_ids=set(report_ids),
            )
            exit_code |= _store_reconcile_result(
                rid, result, out_dir=out_dir, write_json=write_json, all_results=all_results,
            )

    for rid in svodka_only_rids:
        if _skip_cancelled(rid):
            continue
        if _check_cancelled(None):
            raise ProverkaCancelled("cancelled")
        spec = REPORTS[rid]
        run_ov = overrides_map.get(rid) or None
        prog(f"Сводка {rid}: {spec.title}")
        result = _svodka_only_one(
            spec, report_date, run_override=run_ov,
            prefetched_svodka=svodka_cache.get(rid),
        )
        if compare_cropwise and not _needs_cropwise_ui(spec):
            result.setdefault("comparison", {})["note"] = (
                "Сводка без сравнения с Cropwise UI."
            )
        all_results["reports"][rid] = result
        if write_json:
            single = out_dir / f"reconcile_report{rid}_{report_date.isoformat()}.json"
            single.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
            )

    batch_path = out_dir / f"reconcile_all_{report_date.isoformat()}.json"

    _inject_canonical_svodka_for_cross_check(
        all_results, report_date, overrides_map, svodka_cache, prog,
    )
    from proverka.machine_runner import apply_report1_donor_area_graft

    apply_report1_donor_area_graft(all_results)
    all_results["cross_check"] = build_cross_check(all_results)
    attach_file_values(all_results, report_date)
    from proverka.file_consistency import build_sheet_checks

    all_results["sheet_checks"] = build_sheet_checks(report_date, report_ids)
    from proverka.report_views import build_report_views

    all_results["report_views"] = build_report_views(all_results, report_date, report_ids)

    if write_json:
        batch_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")

    xlsx_path: Path | None = None
    if write_excel:
        prog("Формирование Excel…")
        xlsx_path = out_dir / f"proverka_{report_date.isoformat()}.xlsx"
        export_excel(all_results, xlsx_path)
    summary_rows = build_rows(all_results)
    cross_check_rows = all_results["cross_check"].get("operations") or []
    return {
        "exit_code": exit_code,
        "report_date": report_date.isoformat(),
        "report_ids": report_ids,
        "cancelled_report_ids": sorted(set(cancelled_reports)),
        "compare_mode": all_results["compare_mode"],
        "ui_session_seasonValue": ui_session,
        "batch_json": str(batch_path) if write_json else None,
        "excel_path": str(xlsx_path) if xlsx_path else None,
        "excel_filename": xlsx_path.name if xlsx_path else None,
        "summary_rows": summary_rows,
        "cross_check": all_results["cross_check"],
        "cross_check_rows": cross_check_rows,
        "sheet_checks": all_results.get("sheet_checks") or [],
        "report_views": all_results.get("report_views") or {},
    }
