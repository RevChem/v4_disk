"""Запуск проверки количества машин (svodki/plan/proverka/machine_runner.py)
из API. Читает уже готовые Excel сводок, ничего не пересчитывает.

Полностью независим от service.py (проверка сводок по гектарам ↔
Cropwise): свой каталог заданий на диске, свои функции create/run/read,
свой формат статуса. Не импортирует ничего из service.py и не пишет в его
job_dir, чтобы список задач одной проверки никак не пересекался с другой.
"""
from __future__ import annotations

import json
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


def machine_jobs_root() -> Path:
    return Path(settings.upload_dir) / "proverka_machines"


def job_dir(job_id: str) -> Path:
    return machine_jobs_root() / job_id


def write_job_status(job_id: str, payload: dict[str, Any]) -> None:
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def read_job_status(job_id: str) -> dict[str, Any] | None:
    path = job_dir(job_id) / "status.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _job_mtime(job_id: str) -> float:
    path = job_dir(job_id) / "status.json"
    if not path.is_file():
        return 0.0
    return path.stat().st_mtime


def list_job_statuses(*, report_date: date | None = None) -> list[dict[str, Any]]:
    root = machine_jobs_root()
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
    root = machine_jobs_root()
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


def make_cancel_checker(job_id: str) -> Callable[[str | None], bool]:
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


def request_cancel(job_id: str, *, report_ids: list[str] | None = None) -> dict[str, Any]:
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
    if st.get("status") == "pending" and not ids:
        st["status"] = "cancelled"
        st["progress"] = "Отменено"
    else:
        st["progress"] = st.get("progress") or "Отмена…"
    write_job_status(job_id, st)
    return st


def create_job(
    *,
    report_date: date,
    report_ids: list[str],
    user_id: int | None = None,
) -> str:
    job_id = str(uuid.uuid4())
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    params = {
        "report_date": report_date.isoformat(),
        "report_ids": report_ids,
        "user_id": user_id,
    }
    (d / "params.json").write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    write_job_status(
        job_id,
        {
            "job_id": job_id,
            "status": "pending",
            "report_date": report_date.isoformat(),
            "report_ids": report_ids,
            "progress": "Ожидание…",
            "error": None,
            "machine_cross_check": {},
            "machine_cross_check_rows": [],
        },
    )
    return job_id


def run_job(job_id: str) -> None:
    _ensure_plan_path()
    from report_platform import reload_editor_modules

    reload_editor_modules()
    from proverka.machine_runner import MachineProverkaCancelled, run_machine_proverka_batch

    d = job_dir(job_id)
    params_path = d / "params.json"
    if not params_path.is_file():
        write_job_status(job_id, {"job_id": job_id, "status": "failed", "error": "params not found"})
        return

    existing = read_job_status(job_id) or {}
    if existing.get("status") == "cancelled":
        return

    is_cancelled = make_cancel_checker(job_id)
    if is_cancelled(None):
        write_job_status(
            job_id,
            {**existing, "job_id": job_id, "status": "cancelled", "progress": "Отменено"},
        )
        params_path.unlink(missing_ok=True)
        return

    params = json.loads(params_path.read_text(encoding="utf-8"))
    report_date = date.fromisoformat(params["report_date"])
    report_ids = list(params["report_ids"])

    def on_progress(msg: str) -> None:
        st = read_job_status(job_id) or {}
        st["status"] = "running"
        st["progress"] = msg
        write_job_status(job_id, st)

    write_job_status(
        job_id,
        {**(read_job_status(job_id) or {}), "status": "running", "progress": "Старт…"},
    )

    try:
        result = run_machine_proverka_batch(
            report_date,
            report_ids,
            d,
            write_json=True,
            progress=on_progress,
            should_cancel_report=is_cancelled,
        )
        params_path.unlink(missing_ok=True)
        cancelled_ids = result.get("cancelled_report_ids") or []
        final_status = "cancelled" if is_cancelled(None) else "completed"
        write_job_status(
            job_id,
            {
                "job_id": job_id,
                "status": final_status,
                "report_date": report_date.isoformat(),
                "report_ids": report_ids,
                "cancel_report_ids": cancelled_ids,
                "progress": "Отменено" if final_status == "cancelled" else "Готово",
                "error": None,
                "file_report_ids": result.get("file_report_ids") or [],
                "missing_file_report_ids": result.get("missing_file_report_ids") or [],
                "no_machine_data_report_ids": result.get("no_machine_data_report_ids") or [],
                "file_read_errors": result.get("file_read_errors") or {},
                "unknown_report_ids": result.get("unknown_report_ids") or [],
                "machine_cross_check": result.get("machine_cross_check") or {},
                "machine_cross_check_rows": result.get("machine_cross_check_rows") or [],
            },
        )
    except MachineProverkaCancelled:
        params_path.unlink(missing_ok=True)
        write_job_status(
            job_id,
            {**(read_job_status(job_id) or {}), "status": "cancelled", "progress": "Отменено"},
        )
    except Exception as exc:  # noqa: BLE001
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


def dashboard_for_date(report_date: date) -> dict[str, Any]:
    latest = latest_job_for_date(report_date)
    if not latest:
        return {
            "report_date": report_date.isoformat(),
            "status": "none",
            "job_id": None,
            "checked_at": None,
            "progress": None,
        }
    st = dict(latest)
    mtime = st.pop("_mtime", 0)
    if mtime:
        st["checked_at"] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    st.setdefault("report_date", report_date.isoformat())
    return st
