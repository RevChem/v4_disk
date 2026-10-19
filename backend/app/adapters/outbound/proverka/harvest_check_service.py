"""Сверка агроопераций: Excel сводок между собой и с Cropwise (svodki/plan/proverka/harvest_check)."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.adapters.outbound.proverka.service import (
    _ensure_plan_path,
    _inject_proverka_runtime_env,
    proverka_jobs_root,
    session_cookie_from_env,
)
from app.infrastructure.logging import get_logger
from app.legacy.json_store_io import atomic_write_text

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(job_id: str) -> Path:
    uuid.UUID(str(job_id))  # job_id приходит из URL
    return proverka_jobs_root() / "harvest" / f"{job_id}.json"


def _write(job_id: str, payload: dict[str, Any]) -> None:
    atomic_write_text(_path(job_id), json.dumps(payload, ensure_ascii=False))


def read_job(job_id: str) -> dict[str, Any] | None:
    try:
        p = _path(job_id)
    except ValueError:
        return None
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def report_ids() -> list[str]:
    _ensure_plan_path()
    from proverka.harvest_check import REPORT_IDS

    return list(REPORT_IDS)


def catalog() -> dict[str, Any]:
    _ensure_plan_path()
    from proverka.harvest_check import REPORT_IDS, SPECS

    return {
        "report_ids": list(REPORT_IDS),
        "operations": list(SPECS),
        "peers": {op: list(specs) for op, specs in SPECS.items()},
    }


def _run(
    job_id: str, report_date: date, cookie: str, tolerance: float | None, tolerance_season: float | None = None,
) -> None:
    st: dict[str, Any] = read_job(job_id) or {}

    def progress(msg: str) -> None:
        st["progress"] = msg
        st["updated_at"] = _now()
        _write(job_id, st)

    st["status"] = "running"
    progress("Старт")
    try:
        _ensure_plan_path()
        _inject_proverka_runtime_env(report_date)
        from proverka.harvest_check import run_check

        st["result"] = run_check(
            report_date, cookie=cookie, tolerance=tolerance, tolerance_season=tolerance_season, progress=progress,
        )
        st["status"] = "completed"
        st["progress"] = "Готово"
    except Exception as exc:  # noqa: BLE001 — ошибка уходит в статус задачи
        log.exception("harvest_check.failed", job_id=job_id)
        st["status"] = "failed"
        st["error"] = f"{type(exc).__name__}: {exc}"
    st["updated_at"] = _now()
    _write(job_id, st)


def start_job(
    report_date: date, cookie: str | None, tolerance: float | None, tolerance_season: float | None = None,
) -> dict[str, Any]:
    cookie = (cookie or "").strip() or session_cookie_from_env()
    job_id = str(uuid.uuid4())
    st = {
        "job_id": job_id,
        "status": "pending",
        "report_date": report_date.isoformat(),
        "with_cropwise": bool(cookie),
        "created_at": _now(),
        "updated_at": _now(),
        "progress": "В очереди",
    }
    _write(job_id, st)
    threading.Thread(target=_run, args=(job_id, report_date, cookie, tolerance, tolerance_season), daemon=True).start()
    return st
