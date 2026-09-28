"""Запуск proverka из API (svodki/plan/proverka)."""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.config import settings

PLAN_DIR = Path(settings.svodki_root) / "plan"


def _ensure_plan_path() -> None:
    if str(PLAN_DIR) not in sys.path:
        sys.path.insert(0, str(PLAN_DIR))
    try:
        from plan_paths import ensure_plan_sys_path

        ensure_plan_sys_path()
    except ImportError:
        pass


def proverka_jobs_root() -> Path:
    return Path(settings.upload_dir) / "proverka"


def job_dir(job_id: str) -> Path:
    return proverka_jobs_root() / job_id


def write_job_status(job_id: str, payload: dict[str, Any]) -> None:
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_proverka_cancel_checker(job_id: str) -> Callable[[str | None], bool]:
    def is_cancelled(report_id: str | None = None) -> bool:
        st = read_job_status(job_id) or {}
        if st.get("status") == "cancelled":
            return True
        if not st.get("cancel_requested"):
            return False
        ids = [str(x).strip() for x in (st.get("cancel_report_ids") or []) if str(x).strip()]
        if not ids:
            return True
        if report_id is None:
            return False
        return str(report_id) in ids

    return is_cancelled


def request_proverka_cancel(job_id: str, *, report_ids: list[str] | None = None) -> dict[str, Any]:
    st = read_job_status(job_id)
    if not st:
        raise ValueError("job_not_found")
    if st.get("status") not in ("pending", "running"):
        raise ValueError("job_not_cancellable")
    ids = [str(x).strip() for x in (report_ids or []) if str(x).strip()]
    existing = [str(x).strip() for x in (st.get("cancel_report_ids") or []) if str(x).strip()]
    if ids and existing:
        ids = sorted(set(existing) | set(ids))
    st["cancel_requested"] = True
    st["cancel_report_ids"] = ids
    st["error"] = None
    if not ids:
        # Полная отмена: файл виден worker-у сразу, статус — чтобы UI отпустило
        # кнопку, не дожидаясь, пока дочерний процесс сам заметит флаг.
        (job_dir(job_id) / "CANCEL").write_text("1", encoding="utf-8")
        st["status"] = "cancelled"
        st["progress"] = "Отменено"
    else:
        st["progress"] = st.get("progress") or "Отмена…"
    write_job_status(job_id, st)
    return st


def reconcile_cancelled_proverka_jobs() -> int:
    """После рестарта worker добивает задачи, которые уже просили отменить."""
    root = proverka_jobs_root()
    if not root.is_dir():
        return 0
    updated = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        st = read_job_status(child.name)
        if not st or st.get("status") not in ("pending", "running"):
            continue
        ids = [str(x).strip() for x in (st.get("cancel_report_ids") or []) if str(x).strip()]
        full_cancel = bool(st.get("cancel_requested")) and not ids
        if not full_cancel and not (child / "CANCEL").is_file():
            continue
        st["status"] = "cancelled"
        st["cancel_requested"] = True
        st["progress"] = "Отменено"
        st["error"] = None
        write_job_status(child.name, st)
        updated += 1
    return updated


def read_job_status(job_id: str) -> dict[str, Any] | None:
    path = job_dir(job_id) / "status.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def enrich_job_file_column(status: dict[str, Any]) -> dict[str, Any]:
    """В строки сверки подмешивает гектары из файла каждой сводки."""
    if status.get("file_enriched"):
        return status
    if status.get("status") != "completed":
        return status
    raw_date = str(status.get("report_date") or "").strip()
    if not raw_date:
        return status
    rows = status.get("cross_check_rows") or []
    ops = ((status.get("cross_check") or {}).get("operations")) or []
    if not rows and not ops:
        return status
    try:
        _ensure_plan_path()
        os.environ["UPLOAD_DIR"] = str(Path(settings.upload_dir))
        os.environ["SVODKI_UPLOAD_DIR"] = os.environ["UPLOAD_DIR"]
        from datetime import date as date_cls

        from proverka.file_values import apply_file_totals_to_rows

        report_day = date_cls.fromisoformat(raw_date)
        apply_file_totals_to_rows(rows, report_day)
        if ops is not rows:
            apply_file_totals_to_rows(ops, report_day)
        _refresh_sheet_checks(status, report_day)
        status["file_enriched"] = True
    except Exception:
        return status
    return status


def _refresh_sheet_checks(status: dict[str, Any], report_day: Any) -> None:
    """Листы сводок 1/2 из файлов (как «В файле»), нарушения пересчитываются."""
    from proverka.file_consistency import build_sheet_checks

    ids = [str(x) for x in (status.get("report_ids") or [])]
    if not ids:
        return
    fresh = build_sheet_checks(report_day, ids)
    if fresh == (status.get("sheet_checks") or []):
        return
    status["sheet_checks"] = fresh
    _attach_violations(status)


def _job_mtime(job_id: str) -> float:
    path = job_dir(job_id) / "status.json"
    if not path.is_file():
        return 0.0
    return path.stat().st_mtime


def _attach_violations(payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_plan_path()
    from proverka.summary import compute_violations

    summary = payload.get("summary_rows") or []
    cross = payload.get("cross_check_rows") or []
    v = compute_violations(
        summary,
        cross,
        job_error=payload.get("error"),
        sheet_checks=payload.get("sheet_checks") or [],
    )
    payload.update(v)
    return payload


def list_job_statuses(*, report_date: date | None = None) -> list[dict[str, Any]]:
    root = proverka_jobs_root()
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        st = read_job_status(child.name)
        if not st:
            continue
        if report_date and st.get("report_date") != report_date.isoformat():
            continue
        st.setdefault("job_id", child.name)
        st["_mtime"] = _job_mtime(child.name)
        out.append(st)
    out.sort(key=lambda x: x.get("_mtime", 0), reverse=True)
    return out


def latest_job_for_date_light(report_date: date) -> tuple[str, float] | None:
    """(job_id, mtime) последнего запуска за дату. Не читает все status.json подряд:
    идём от новых папок к старым и останавливаемся на первой подходящей."""
    root = proverka_jobs_root()
    if not root.is_dir():
        return None
    dirs = sorted(
        (c for c in root.iterdir() if c.is_dir()),
        key=lambda c: _job_mtime(c.name),
        reverse=True,
    )
    wanted = report_date.isoformat()
    for child in dirs:
        st = read_job_status(child.name)
        if st and st.get("report_date") == wanted:
            return child.name, _job_mtime(child.name)
    return None


def latest_job_for_date(report_date: date) -> dict[str, Any] | None:
    jobs = list_job_statuses(report_date=report_date)
    return jobs[0] if jobs else None


def session_cookie_from_env() -> str:
    _ensure_plan_path()
    from proverka.reconcile import load_env, session_cookie

    load_env()
    return session_cookie()


def dashboard_for_date(report_date: date) -> dict[str, Any]:
    latest = latest_job_for_date(report_date)
    ai = ai_report_for_date(report_date)
    if not latest:
        return {
            "report_date": report_date.isoformat(),
            "status": "none",
            "job_id": None,
            "violation_count": 0,
            "cropwise_diff_count": 0,
            "cropwise_violation_count": 0,
            "cross_check_violation_count": 0,
            "violations": [],
            "error_logs": [],
            "checked_at": None,
            "progress": None,
            "ai_status": ai.get("ai_status") or "none",
            "ai_violation_count": ai.get("ai_violation_count") or 0,
        }
    st = dict(latest)
    st.pop("_mtime", None)
    if st.get("status") == "completed" and "violation_count" not in st:
        st = _attach_violations(st)
    mtime = latest.get("_mtime") or 0
    if mtime:
        st["checked_at"] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    st.setdefault("report_date", report_date.isoformat())
    if not st.get("ai_status") or st.get("ai_status") == "skipped":
        st["ai_status"] = ai.get("ai_status") or st.get("ai_status")
        st["ai_violation_count"] = ai.get("ai_violation_count") or st.get("ai_violation_count") or 0
        if ai.get("ai_report"):
            st["ai_report"] = ai.get("ai_report")
    return st


def editor_season_overrides(report_date: date, report_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Сезоны и диапазоны из редактора планов для proverka."""
    _ensure_plan_path()
    os.environ.setdefault("UPLOAD_DIR", str(settings.upload_dir))
    os.environ.setdefault("SVODKI_UPLOAD_DIR", str(settings.upload_dir))
    from proverka.season_runtime import season_overrides_from_editor

    return season_overrides_from_editor(report_date, report_ids)


def refresh_dashboard_check(report_date: date, *, run_ai_analysis: bool = False) -> str:
    """Новая проверка всех сводок с Cropwise (cookie с сервера)."""
    _ensure_plan_path()
    from proverka.reports_config import list_report_ids

    cookie = session_cookie_from_env()
    if not cookie:
        raise ValueError(
            "Не задана cookie Cropwise (CROPWISE_SESSION_COOKIE в .env или proverka/reconcile.py)"
        )
    rids = list_report_ids()
    job_id = create_job(
        report_date=report_date,
        report_ids=rids,
        compare_cropwise=True,
        cookie=cookie,
        season_overrides=editor_season_overrides(report_date, rids),
        run_ai_analysis=run_ai_analysis,
    )
    return job_id


def daily_proverka_ai_job(report_date: date | None = None) -> str:
    """Cron 21:00 MSK: proverka всех сводок + AI."""
    _ensure_plan_path()
    from proverka.reports_config import list_report_ids

    rd = report_date or date.today()
    rids = list_report_ids()
    cookie = session_cookie_from_env()
    if not cookie:
        raise ValueError("CROPWISE_SESSION_COOKIE не задан для daily proverka AI")
    return create_job(
        report_date=rd,
        report_ids=rids,
        compare_cropwise=True,
        cookie=cookie,
        season_overrides=editor_season_overrides(rd, rids),
        run_ai_analysis=True,
    )


def ai_report_for_date(report_date: date) -> dict[str, Any]:
    """Последний AI-отчёт за дату (из index или job dir)."""
    root = proverka_jobs_root()

    latest = latest_job_for_date(report_date)
    if latest:
        ai_st = latest.get("ai_status")
        if ai_st in ("pending", "running", "failed"):
            return {
                "report_date": report_date.isoformat(),
                "job_id": latest.get("job_id"),
                "ai_status": ai_st,
                "ai_violation_count": latest.get("ai_violation_count") or 0,
                "ai_report": latest.get("ai_report"),
            }

    index_path = root / f"ai_index_{report_date.isoformat()}.json"
    if index_path.is_file():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass

    best: dict[str, Any] | None = None
    best_mtime = 0.0
    if root.is_dir():
        for child in root.iterdir():
            if not child.is_dir():
                continue
            ai_path = child / "ai_report.json"
            st_path = child / "status.json"
            if not ai_path.is_file() or not st_path.is_file():
                continue
            try:
                st = json.loads(st_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if st.get("report_date") != report_date.isoformat():
                continue
            if st.get("ai_status") != "completed":
                continue
            mtime = ai_path.stat().st_mtime
            if mtime > best_mtime:
                best_mtime = mtime
                try:
                    ai_raw = json.loads(ai_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                best = {
                    "report_date": report_date.isoformat(),
                    "job_id": st.get("job_id") or child.name,
                    "ai_status": st.get("ai_status"),
                    "ai_violation_count": st.get("ai_violation_count", 0),
                    "ai_report": st.get("ai_report") or {
                        "markdown": ai_raw.get("markdown"),
                        "findings": (ai_raw.get("report") or {}).get("findings"),
                        "executive_summary": (ai_raw.get("report") or {}).get("executive_summary"),
                        "violation_count": ai_raw.get("violation_count"),
                    },
                }
    return best or {
        "report_date": report_date.isoformat(),
        "job_id": None,
        "ai_status": "none",
        "ai_violation_count": 0,
        "ai_report": None,
    }


def create_job(
    *,
    report_date: date,
    report_ids: list[str],
    compare_cropwise: bool,
    cookie: str,
    season_overrides: dict[str, dict[str, Any]] | None = None,
    user_id: int | None = None,
    run_ai_analysis: bool = False,
) -> str:
    job_id = str(uuid.uuid4())
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    params = {
        "report_date": report_date.isoformat(),
        "report_ids": report_ids,
        "compare_cropwise": compare_cropwise,
        "cookie": cookie if compare_cropwise else "",
        "season_overrides": dict(season_overrides or {}),
        "user_id": user_id,
        "run_ai_analysis": bool(run_ai_analysis),
    }
    (d / "params.json").write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    write_job_status(
        job_id,
        {
            "job_id": job_id,
            "status": "pending",
            "report_date": report_date.isoformat(),
            "report_ids": report_ids,
            "compare_mode": "cropwise" if compare_cropwise else "svodki_only",
            "progress": "Ожидание…",
            "excel_filename": None,
            "error": None,
            "summary_rows": [],
            "ai_status": "pending" if run_ai_analysis else "skipped",
            "ai_violation_count": 0,
        },
    )
    return job_id


def _ai_history_path() -> Path:
    return proverka_jobs_root() / "ai_history.json"


def _append_ai_history(entry: dict[str, Any]) -> None:
    path = _ai_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                items = raw
        except (OSError, json.JSONDecodeError):
            items = []
    jid = str(entry.get("job_id") or "")
    items = [x for x in items if str(x.get("job_id") or "") != jid]
    items.insert(0, entry)
    path.write_text(json.dumps(items[:100], ensure_ascii=False, indent=2), encoding="utf-8")


def _write_ai_index(report_date: date, job_id: str, ai_payload: dict[str, Any]) -> None:
    root = proverka_jobs_root()
    updated_at = datetime.now(timezone.utc).isoformat()
    ai_report = ai_payload.get("ai_report") or {}
    summary = str(ai_report.get("executive_summary") or "")[:240]
    payload = {
        "report_date": report_date.isoformat(),
        "job_id": job_id,
        "ai_status": ai_payload.get("ai_status"),
        "ai_violation_count": ai_payload.get("ai_violation_count", 0),
        "ai_report": ai_report,
        "updated_at": updated_at,
    }
    path = root / f"ai_index_{report_date.isoformat()}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_ai_history(
        {
            "report_date": report_date.isoformat(),
            "job_id": job_id,
            "ai_status": ai_payload.get("ai_status"),
            "ai_violation_count": ai_payload.get("ai_violation_count", 0),
            "executive_summary": summary,
            "updated_at": updated_at,
        }
    )


def list_ai_report_history(*, limit: int = 50) -> list[dict[str, Any]]:
    """История сохранённых AI-отчётов (новые первые)."""
    seen: set[str] = set()
    items: list[dict[str, Any]] = []

    history_path = _ai_history_path()
    if history_path.is_file():
        try:
            raw = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for row in raw:
                    jid = str(row.get("job_id") or "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    items.append(row)
        except (OSError, json.JSONDecodeError):
            pass

    root = proverka_jobs_root()
    if root.is_dir():
        scanned: list[tuple[float, dict[str, Any]]] = []
        for child in root.iterdir():
            if not child.is_dir():
                continue
            ai_path = child / "ai_report.json"
            st_path = child / "status.json"
            if not ai_path.is_file() or not st_path.is_file():
                continue
            try:
                st = json.loads(st_path.read_text(encoding="utf-8"))
                ai_raw = json.loads(ai_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if st.get("ai_status") != "completed":
                continue
            jid = str(st.get("job_id") or child.name)
            if jid in seen:
                continue
            report = st.get("ai_report") or ai_raw.get("report") or {}
            summary = str(report.get("executive_summary") or ai_raw.get("markdown") or "")[:240]
            mtime = ai_path.stat().st_mtime
            scanned.append(
                (
                    mtime,
                    {
                        "report_date": str(st.get("report_date") or ""),
                        "job_id": jid,
                        "ai_status": st.get("ai_status"),
                        "ai_violation_count": st.get("ai_violation_count", 0),
                        "executive_summary": summary,
                        "updated_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                    },
                )
            )
        scanned.sort(key=lambda x: x[0], reverse=True)
        for _, row in scanned:
            jid = str(row.get("job_id") or "")
            if jid in seen:
                continue
            seen.add(jid)
            items.append(row)

    items.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return items[: max(1, min(limit, 200))]


def ai_report_for_job(job_id: str) -> dict[str, Any] | None:
    st = read_job_status(job_id)
    if not st:
        return None
    ai_path = job_dir(job_id) / "ai_report.json"
    ai_report = st.get("ai_report")
    if not ai_report and ai_path.is_file():
        try:
            ai_raw = json.loads(ai_path.read_text(encoding="utf-8"))
            ai_report = ai_raw.get("report") or {
                "markdown": ai_raw.get("markdown"),
                "findings": (ai_raw.get("report") or {}).get("findings"),
                "executive_summary": (ai_raw.get("report") or {}).get("executive_summary"),
            }
        except (OSError, json.JSONDecodeError):
            ai_report = None
    updated_at = None
    if ai_path.is_file():
        try:
            updated_at = datetime.fromtimestamp(ai_path.stat().st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            updated_at = None
    return {
        "report_date": str(st.get("report_date") or ""),
        "job_id": job_id,
        "ai_status": st.get("ai_status") or "none",
        "ai_violation_count": st.get("ai_violation_count") or 0,
        "ai_report": ai_report,
        "ai_error": st.get("ai_error"),
        "updated_at": updated_at,
    }


def run_ai_job(job_id: str) -> dict[str, Any]:
    """AI-разбор по готовому reconcile batch в каталоге job."""
    _ensure_plan_path()
    from proverka.ai.pipeline import run_ai_for_job

    d = job_dir(job_id)
    st = read_job_status(job_id) or {}
    st["ai_status"] = "running"
    st["progress"] = "AI-разбор…"
    write_job_status(job_id, st)

    def on_progress(msg: str) -> None:
        cur = read_job_status(job_id) or {}
        cur["progress"] = msg
        write_job_status(job_id, cur)

    ai_result = run_ai_for_job(d, prog=on_progress)
    st = read_job_status(job_id) or {}
    st.update(ai_result)
    st["progress"] = st.get("progress") or "AI готово"
    write_job_status(job_id, st)

    rd = st.get("report_date")
    if rd:
        try:
            _write_ai_index(date.fromisoformat(str(rd).split("T")[0]), job_id, ai_result)
        except ValueError:
            pass
    return ai_result


def run_job(job_id: str) -> None:
    _ensure_plan_path()
    from proverka.runner import ProverkaCancelled, run_proverka_batch

    d = job_dir(job_id)
    params_path = d / "params.json"
    if not params_path.is_file():
        write_job_status(job_id, {"job_id": job_id, "status": "failed", "error": "params not found"})
        return

    existing = read_job_status(job_id) or {}
    if existing.get("status") == "cancelled":
        return

    os.environ["PROVERKA_JOB_DIR"] = str(d)
    is_cancelled = make_proverka_cancel_checker(job_id)
    if is_cancelled(None) or (d / "CANCEL").is_file():
        write_job_status(
            job_id,
            {**existing, "job_id": job_id, "status": "cancelled", "progress": "Отменено"},
        )
        params_path.unlink(missing_ok=True)
        return

    params = json.loads(params_path.read_text(encoding="utf-8"))
    report_date = date.fromisoformat(params["report_date"])
    report_ids = list(params["report_ids"])
    compare_cropwise = bool(params.get("compare_cropwise", True))
    cookie = str(params.get("cookie") or "")
    season_overrides = dict(params.get("season_overrides") or {})

    def on_progress(msg: str) -> None:
        if (d / "CANCEL").is_file():
            raise ProverkaCancelled("cancelled")
        st = read_job_status(job_id) or {}
        if st.get("status") == "cancelled":
            raise ProverkaCancelled("cancelled")
        ids = [str(x).strip() for x in (st.get("cancel_report_ids") or []) if str(x).strip()]
        if st.get("cancel_requested") and not ids:
            raise ProverkaCancelled("cancelled")
        st["status"] = "running"
        st["progress"] = msg
        if (d / "CANCEL").is_file():
            raise ProverkaCancelled("cancelled")
        write_job_status(job_id, st)

    if (d / "CANCEL").is_file() or is_cancelled(None):
        write_job_status(
            job_id,
            {**(read_job_status(job_id) or {}), "job_id": job_id, "status": "cancelled", "progress": "Отменено"},
        )
        params_path.unlink(missing_ok=True)
        return

    write_job_status(
        job_id,
        {
            **(read_job_status(job_id) or {}),
            "status": "running",
            "progress": "Старт…",
        },
    )

    try:
        result = run_proverka_batch(
            report_date,
            report_ids,
            d,
            cookie=cookie,
            compare_cropwise=compare_cropwise,
            write_excel=True,
            write_json=True,
            progress=on_progress,
            season_overrides=season_overrides,
            should_cancel_report=is_cancelled,
        )
        params_path.unlink(missing_ok=True)
        cancelled_ids = result.get("cancelled_report_ids") or []
        final_status = "cancelled" if is_cancelled(None) else "completed"
        completed = {
            "job_id": job_id,
            "status": "running" if params.get("run_ai_analysis") and final_status != "cancelled" else final_status,
            "report_date": report_date.isoformat(),
            "report_ids": report_ids,
            "cancel_report_ids": cancelled_ids,
            "compare_mode": result.get("compare_mode"),
            "progress": "Отменено" if final_status == "cancelled" else (
                "AI-разбор…" if params.get("run_ai_analysis") else "Готово"
            ),
            "excel_filename": result.get("excel_filename"),
            "error": None,
            "summary_rows": result.get("summary_rows") or [],
            "cross_check": result.get("cross_check") or {},
            "cross_check_rows": result.get("cross_check_rows") or [],
            "sheet_checks": result.get("sheet_checks") or [],
            "report_views": result.get("report_views") or {},
            "exit_code": result.get("exit_code", 0),
        }
        if final_status != "cancelled":
            completed = _attach_violations(completed)
            completed = enrich_job_file_column(completed)
        write_job_status(job_id, completed)

        if params.get("run_ai_analysis") and final_status != "cancelled":
            if is_cancelled(None):
                completed = read_job_status(job_id) or completed
                completed["status"] = "cancelled"
                completed["progress"] = "Отменено"
                write_job_status(job_id, completed)
            else:
                try:
                    ai_payload = run_ai_job(job_id)
                    completed = read_job_status(job_id) or completed
                    if is_cancelled(None):
                        completed["status"] = "cancelled"
                        completed["progress"] = "Отменено"
                    else:
                        completed.update({
                            "ai_status": ai_payload.get("ai_status"),
                            "ai_violation_count": ai_payload.get("ai_violation_count", 0),
                            "ai_report": ai_payload.get("ai_report"),
                        })
                        completed["status"] = "completed"
                        completed["progress"] = "Готово"
                    write_job_status(job_id, completed)
                except Exception as ai_exc:
                    completed = read_job_status(job_id) or completed
                    completed["ai_status"] = "failed"
                    completed["ai_error"] = str(ai_exc)
                    completed["status"] = "completed"
                    completed["progress"] = "Готово"
                    write_job_status(job_id, completed)
        elif final_status != "cancelled":
            completed = read_job_status(job_id) or completed
            completed["status"] = "completed"
            completed["progress"] = "Готово"
            write_job_status(job_id, completed)

        uid = params.get("user_id")
        if uid is not None:
            try:
                from app.adapters.outbound.proverka.monitoring import record_job_result

                record_job_result(int(uid), report_date, job_id, completed)
            except Exception:
                pass
    except ProverkaCancelled:
        params_path.unlink(missing_ok=True)
        st = read_job_status(job_id) or {}
        write_job_status(
            job_id,
            {
                **st,
                "job_id": job_id,
                "status": "cancelled",
                "progress": "Отменено",
                "error": None,
            },
        )
    except Exception as exc:
        params_path.unlink(missing_ok=True)
        write_job_status(
            job_id,
            {
                **(read_job_status(job_id) or {}),
                "status": "failed",
                "error": str(exc),
                "progress": "Ошибка",
            },
        )
        raise


def platform_summary() -> dict[str, Any]:
    _ensure_plan_path()
    from report_platform import platform_summary as _summary

    return _summary()


def season_meta_for_reports(report_date: date, report_ids: list[str]) -> dict[str, Any]:
    _ensure_plan_path()
    os.environ.setdefault("UPLOAD_DIR", str(settings.upload_dir))
    os.environ.setdefault("SVODKI_UPLOAD_DIR", str(settings.upload_dir))

    from app.legacy.season_meta import season_meta_for_groups
    from proverka.reports_config import REPORTS
    from proverka.season_runtime import resolve_report_season_config, season_ranges_path

    groups: list[str] = []
    by_report: dict[str, Any] = {}
    for rid in report_ids:
        spec = REPORTS.get(rid)
        if not spec:
            continue
        groups.extend(spec.work_groups)
        by_report[rid] = {
            "title": spec.title,
            "work_groups": list(spec.work_groups),
            "season_rule": spec.season_rule,
            "resolved": resolve_report_season_config(spec, report_date, None),
        }
    meta = season_meta_for_groups(list(dict.fromkeys(groups)), report_date)
    return {
        "report_date": report_date.isoformat(),
        "path": str(season_ranges_path()),
        "by_group": meta.get("by_group") or {},
        "by_report": by_report,
    }


def list_catalog() -> list[dict[str, str]]:
    _ensure_plan_path()
    from proverka.reports_config import REPORTS, list_report_ids

    out: list[dict[str, str]] = []
    for rid in list_report_ids():
        spec = REPORTS[rid]
        out.append({"id": rid, "title": spec.title})
    return out


def excel_path_for_job(job_id: str) -> Path | None:
    st = read_job_status(job_id)
    if not st or st.get("status") != "completed":
        return None
    name = st.get("excel_filename")
    if not name:
        return None
    path = job_dir(job_id) / name
    return path if path.is_file() else None
