"""Отмена задач генерации сводок (флаг в БД + проверка по report_id)."""
from __future__ import annotations

import json
import time
from typing import Callable

from sqlalchemy.orm import Session

from app.adapters.outbound.persistence import repositories as repo
from app.models import GenerationJob


def _parse_cancel_report_ids(raw: str | None) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x).strip() for x in parsed if str(x).strip()]


def cancel_all_requested(job: GenerationJob) -> bool:
    return bool(job.cancel_requested) and not _parse_cancel_report_ids(job.cancel_report_ids)


def cancelled_report_ids(job: GenerationJob) -> set[str] | None:
    """None = отменить все оставшиеся; set = только указанные."""
    if not job.cancel_requested:
        return set()
    ids = _parse_cancel_report_ids(job.cancel_report_ids)
    if not ids:
        return None
    return set(ids)


def is_report_cancelled(job: GenerationJob, report_id: str) -> bool:
    ids = cancelled_report_ids(job)
    if ids is None:
        return True
    return report_id in ids


def make_cancel_checker(job_id: str, *, refresh_sec: float = 0.4) -> Callable[[str | None], bool]:
    """Возвращает is_cancelled(report_id). None report_id — отмена всей задачи."""
    from app.database import SessionLocal

    cache: dict[str, object] = {"at": 0.0, "all": False, "ids": set()}

    def _refresh() -> None:
        now = time.monotonic()
        if now - float(cache["at"]) < refresh_sec:
            return
        db = SessionLocal()
        try:
            job = repo.get_generation_job(db, job_id)
            if job is None or not job.cancel_requested:
                cache["all"] = False
                cache["ids"] = set()
            else:
                ids = _parse_cancel_report_ids(job.cancel_report_ids)
                cache["all"] = not ids
                cache["ids"] = set(ids) if ids else set()
        finally:
            db.close()
            cache["at"] = now

    def is_cancelled(report_id: str | None = None) -> bool:
        _refresh()
        if not cache["all"] and not cache["ids"]:
            return False
        if report_id is None:
            return bool(cache["all"])
        if cache["all"]:
            return True
        return str(report_id) in cache["ids"]

    return is_cancelled


def request_generation_cancel(
    db: Session,
    job: GenerationJob,
    *,
    report_ids: list[str] | None = None,
) -> GenerationJob:
    ids = [str(x).strip() for x in (report_ids or []) if str(x).strip()]
    existing = _parse_cancel_report_ids(job.cancel_report_ids)
    if ids and existing:
        ids = sorted(set(existing) | set(ids))
    cancel_json = json.dumps(ids, ensure_ascii=False)
    job = repo.update_generation_job(
        db,
        job,
        cancel_requested=True,
        cancel_report_ids=cancel_json,
    )
    if not ids and job.status in ("pending", "running"):
        job = repo.update_generation_job(db, job, status="cancelled")
    return job


def stale_running_job_outcome(
    *,
    cancel_all: bool,
    celery_task_id: str,
    active_task_ids: set[str] | None,
) -> str | None:
    """cancelled, worker_lost или None — задачу не трогать.

    Пустой или неудавшийся ответ Celery не доказывает, что воркер умер.
    Частичная отмена (список сводок) тоже не закрывает всю задачу.
    """
    if cancel_all:
        return "cancelled"
    if not active_task_ids:
        return None
    celery_id = (celery_task_id or "").strip()
    if celery_id and celery_id not in active_task_ids:
        return "worker_lost"
    return None


def reconcile_stale_generation_jobs(db: Session) -> int:
    """Помечает зависшие задачи после рестарта worker (полная отмена или точно мёртвый Celery)."""
    from app.config import settings
    from app.infrastructure.celery_app import celery_app

    active_task_ids: set[str] | None = None
    if settings.celery_enabled:
        try:
            inspect = celery_app.control.inspect(timeout=1.0)
            active = inspect.active()
            found: set[str] = set()
            if active:
                for worker_tasks in active.values():
                    for task in worker_tasks or []:
                        tid = task.get("id")
                        if tid:
                            found.add(str(tid))
            if found:
                active_task_ids = found
        except Exception:
            active_task_ids = None

    updated = 0
    for job in db.query(GenerationJob).filter(GenerationJob.status == "running").all():
        outcome = stale_running_job_outcome(
            cancel_all=cancel_all_requested(job),
            celery_task_id=job.celery_task_id or "",
            active_task_ids=active_task_ids,
        )
        if outcome == "cancelled":
            repo.update_generation_job(db, job, status="cancelled")
            updated += 1
        elif outcome == "worker_lost":
            repo.update_generation_job(
                db,
                job,
                status="failed",
                error_message="worker_lost: задача прервана при перезапуске worker",
            )
            updated += 1
    return updated
