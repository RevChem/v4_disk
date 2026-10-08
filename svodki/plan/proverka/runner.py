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


def _per_op_prefetch_groups(report_ids: list[str]) -> list[tuple[str, list[int]]]:
    from proverka.work_groups import work_group_wt_map
    from proverka.operation_registry import OPERATION_PEERS, peer_reports_for_group

    per_op_rids = {rid for rid in report_ids if REPORTS[rid].cropwise_per_operation}
    if not per_op_rids:
        return []

    wt_map = work_group_wt_map()
    out: list[tuple[str, list[int]]] = []
    seen: set[str] = set()
    for gn in OPERATION_PEERS:
        peers = set(peer_reports_for_group(gn))
        if not peers & per_op_rids:
            continue
        if gn in seen:
            continue
        wt = wt_map.get(gn)
        if wt:
            out.append((gn, list(wt)))
            seen.add(gn)
    return out


def _prefetch_work_json(
    cookie: str,
    report_date: date,
    report_ids: list[str],
    prog: ProgressFn,
    also_groups: dict[str, tuple[str, ...]] | None = None,
) -> tuple[dict[str, dict[str, Any]], int | None]:
    """Один последовательный загрузчик work.json на оба сезона смены."""
    from proverka.cropwise_ui import (
        WorkLoadJob,
        load_works_for_seasons,
        pack_loaded_work,
        ui_check_years,
    )
    from proverka.reconcile import _shift_window_iso
    from proverka.report2_reconcile import _cropwise_spec_for_group, _fact_hints_for_group
    from proverka.work_groups import work_type_ids_for_group

    years = ui_check_years(report_date)
    comparable = [rid for rid in report_ids if _needs_cropwise_ui(REPORTS[rid])]
    prefetched: dict[str, dict[str, Any]] = {}
    if not comparable:
        return prefetched, None

    per_op_rids = [rid for rid in comparable if REPORTS[rid].cropwise_per_operation]
    per_op_groups = [gn for gn, _wt in _per_op_prefetch_groups(comparable)]
    std_rids = [rid for rid in comparable if not REPORTS[rid].cropwise_per_operation]
    group_names: list[str] = []
    seen: set[str] = set()
    for name in list(per_op_groups):
        if name not in seen:
            group_names.append(name)
            seen.add(name)
    report_jobs: list[str] = []
    for rid in std_rids:
        groups = tuple(REPORTS[rid].work_groups or ())
        extras = tuple((also_groups or {}).get(rid) or ())
        if len(groups) == 1 and not extras:
            if groups[0] not in seen:
                group_names.append(groups[0])
                seen.add(groups[0])
        elif groups or extras:
            for name in (*groups, *extras):
                if name not in seen:
                    group_names.append(name)
                    seen.add(name)
        else:
            report_jobs.append(rid)

    jobs: list[WorkLoadJob] = []
    for name in group_names:
        wt_ids = work_type_ids_for_group(name)
        if not wt_ids:
            continue
        spec = _cropwise_spec_for_group(name)
        t0, t1, _ = _shift_window_iso(spec, report_date)
        hints = list(_fact_hints_for_group(name)) or list(spec.fact_column_hints)
        jobs.append(WorkLoadJob(name, t0, t1, list(wt_ids), tuple(hints)))
    for rid in report_jobs:
        spec = REPORTS[rid]
        t0, t1, wt_ids = _shift_window_iso(spec, report_date)
        jobs.append(WorkLoadJob(
            f"report:{rid}", t0, t1, list(wt_ids), tuple(spec.fact_column_hints),
        ))
    if not jobs:
        return prefetched, None

    prog(
        f"Cropwise UI: сезоны {years[0]} и {years[1]}, "
        f"{len(jobs)} запросов work.json по очереди…"
    )
    loaded = load_works_for_seasons(cookie, jobs, years=years)
    packed_groups = {
        name: pack_loaded_work(loaded.get(name) or {}, years)
        for name in group_names
        if name in loaded or any(job.key == name for job in jobs)
    }
    if per_op_groups:
        op_map = {gn: packed_groups[gn] for gn in per_op_groups if gn in packed_groups}
        for rid in per_op_rids:
            prefetched[rid] = op_map
    for rid in std_rids:
        groups = tuple(REPORTS[rid].work_groups or ())
        extras = tuple((also_groups or {}).get(rid) or ())
        names = tuple(dict.fromkeys((*groups, *extras)))
        if len(names) == 1 and names[0] in packed_groups:
            prefetched[rid] = packed_groups[names[0]]
        elif len(names) > 1:
            prefetched[rid] = {
                name: packed_groups[name] for name in names if name in packed_groups
            }
        elif f"report:{rid}" in loaded:
            prefetched[rid] = pack_loaded_work(loaded[f"report:{rid}"], years)
    return prefetched, None



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
    """Повтор после сводок подтягивал больший ответ к факту сводки. Больше не делается."""
    del cookie, report_date, report_ids, svodka_cache, prog
    return prefetched_work, None


def _wait_for_cropwise_ui(
    cookie: str,
    report_date: date,
    prog: ProgressFn,
    prefetched_mu: dict[str, Any] | None,
    ui_session: int | None,
) -> None:
    """Канарейка смотрит уже загруженные сезоны и сама в Cropwise не ходит."""
    del cookie, report_date, ui_session
    from proverka.cropwise_ui import cropwise_total_ha

    spec = REPORTS.get(_CROPWISE_CANARY_REPORT)
    if not spec or not _needs_cropwise_ui(spec):
        return
    min_ha = _canary_min_ha()
    if min_ha <= 0 or not isinstance(prefetched_mu, dict):
        return
    total = 0.0
    for slot in (prefetched_mu.get("by_year") or {}).values():
        if isinstance(slot, dict) and slot.get("load_status") == "loaded":
            total += cropwise_total_ha(slot, spec.fact_column_hints)
    if total >= min_ha:
        prog(f"Cropwise UI: канарейка OK ({total:.0f} га)")
    else:
        prog(
            f"Cropwise UI: канарейка {total:.0f} га при пороге {min_ha:.0f}. "
            "Повтор с выбором большего ответа не запускается."
        )



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
    try:
        from svodki_sanity import log_cross_op_data_check

        log_cross_op_data_check(all_results)
    except Exception:
        pass
    attach_file_values(all_results, report_date)
    try:
        from svodki_sanity import check_file_total_after_save

        for rid, result in (all_results.get("reports") or {}).items():
            if not isinstance(result, dict):
                continue
            calc = (result.get("svodka") or {}).get("total_ha")
            if calc is None:
                cmp_ = result.get("comparison") or {}
                calc = cmp_.get("svodka_total_ha")
            check_file_total_after_save(
                float(calc) if calc is not None else None,
                report_date=report_date,
                report_id=str(rid),
            )
    except Exception:
        pass
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
