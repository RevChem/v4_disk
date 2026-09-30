"""Use case: генерация отчётов."""
from __future__ import annotations

import json
import threading
import time
from datetime import date

from sqlalchemy.orm import Session

from app.adapters.outbound.persistence import repositories as repo
from app.application.activity_log import _log_lines_for_report
from app.application.audit_activity import record_activity, report_title
from app.application.generation_cancel import (
    cancel_all_requested,
    make_cancel_checker,
    request_generation_cancel,
)
from app.application.post_generation_hooks import apply_post_generation_hooks
from app.adapters.outbound.reports.service import ReportGenerationError, generate_reports_to_disk
from app.config import settings
from app.infrastructure.log_context import bind_generation_context, clear_generation_context
from app.infrastructure.logging import get_logger
from app.legacy.report_generation import kill_generation_job_subprocesses
from app.legacy.report_runner import clear_job_progress, init_job_progress
from app.models import GenerationJob

log = get_logger(__name__)


def _user_email(db: Session, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    user = repo.get_user_by_id(db, user_id)
    if user is None:
        return None
    return user.email or user.username


def cancel_generation_job(
    db: Session,
    job: GenerationJob,
    *,
    report_ids: list[str] | None = None,
) -> GenerationJob:
    job = request_generation_cancel(db, job, report_ids=report_ids)
    kill_generation_job_subprocesses(job.id)
    if settings.celery_enabled and job.celery_task_id:
        try:
            from app.infrastructure.celery_app import celery_app

            celery_app.control.revoke(job.celery_task_id, terminate=True, signal="SIGTERM")
        except Exception as exc:
            log.warning("reports.generation.revoke_failed", job_id=job.id, error=str(exc))
    return job


def run_generation_job(db: Session, job: GenerationJob) -> GenerationJob:
    db.refresh(job)
    if job.status == "cancelled":
        return job
    if cancel_all_requested(job):
        return repo.update_generation_job(db, job, status="cancelled")

    report_ids = json.loads(job.report_ids or "[]")
    summary_date = job.summary_date.isoformat()
    user_email = _user_email(db, job.user_id)
    bind_generation_context(
        job_id=job.id,
        user_id=job.user_id,
        user_email=user_email,
        summary_date=summary_date,
        report_ids=report_ids,
    )
    init_job_progress(job.id)
    repo.update_generation_job(db, job, status="running")
    log.info(
        "reports.generation.started",
        job_id=job.id,
        report_ids=report_ids,
        summary_date=summary_date,
        user_id=job.user_id,
        user_email=user_email,
    )
    cancelled: list[dict[str, str]] = []
    is_cancelled = make_cancel_checker(job.id)
    started_monotonic = time.monotonic()
    try:
        created, failed, logs = generate_reports_to_disk(
            job.summary_date,
            report_ids or None,
            generation_job_id=job.id,
            is_cancelled=is_cancelled,
            cancelled=cancelled,
        )
        apply_post_generation_hooks(db, summary_date=job.summary_date, created=created, logs=logs)
        total_duration_seconds = round(time.monotonic() - started_monotonic, 1)
        db.refresh(job)
        final_status = "completed"
        if job.cancel_requested and cancel_all_requested(job):
            final_status = "cancelled"
        elif job.cancel_requested and cancelled and not created and not failed:
            final_status = "cancelled"
        result = {
            "created": [
                {
                    "report_id": c.report_id,
                    "title": c.title,
                    "xlsx_filename": c.xlsx_path.name if c.xlsx_path else None,
                    "pdf_filename": c.pdf_path.name if c.pdf_path else None,
                    "duration_seconds": c.duration_seconds,
                }
                for c in created
            ],
            "failed": failed,
            "cancelled": cancelled,
            "logs": logs,
            # Полное время формирования всех сводок задания (для «Логов» и профиля).
            "total_duration_seconds": total_duration_seconds,
        }
        for item in failed:
            rid = str(item.get("report_id") or "")
            log.error(
                "reports.generation.report_failed",
                job_id=job.id,
                report_id=rid,
                error=item.get("error"),
            )
            record_activity(
                db,
                action="reports.generation.report_failed",
                user_id=job.user_id,
                job_id=job.id,
                report_id=rid,
                title=item.get("title") or report_title(rid),
                error=str(item.get("error") or ""),
                summary_date=summary_date,
                log_lines=_log_lines_for_report(logs, rid),
            )
        job = repo.update_generation_job(
            db,
            job,
            status=final_status,
            created_count=len(created),
            failed_count=len(failed),
            result_json=json.dumps(result, ensure_ascii=False),
        )
        if job.user_id and created:
            try:
                from app.adapters.outbound.proverka.monitoring import enqueue_report_checks

                created_ids = [c.report_id for c in created if getattr(c, "report_id", None)]
                enqueue_report_checks(job.user_id, job.summary_date, created_ids)
            except Exception as exc:
                log.warning("proverka.monitoring.enqueue_failed", error=str(exc))
        log.info(
            "reports.generation.completed",
            job_id=job.id,
            created_count=len(created),
            failed_count=len(failed),
            cancelled_count=len(cancelled),
            user_email=user_email,
            status=final_status,
        )
        record_activity(
            db,
            action="reports.generation.completed" if final_status == "completed" else "reports.generation.cancelled",
            user_id=job.user_id,
            job_id=job.id,
            summary_date=summary_date,
            created=len(created),
            failed=len(failed),
            cancelled=len(cancelled),
            report_ids=report_ids,
        )
        return job
    except ReportGenerationError as exc:
        job = repo.update_generation_job(db, job, status="failed", error_message=str(exc))
        log.error(
            "reports.generation.failed",
            job_id=job.id,
            error=str(exc),
            user_email=user_email,
        )
        record_activity(
            db,
            action="reports.generation.failed",
            user_id=job.user_id,
            job_id=job.id,
            summary_date=summary_date,
            error=str(exc),
            report_ids=report_ids,
        )
        return job
    except Exception as exc:
        job = repo.update_generation_job(db, job, status="failed", error_message=str(exc))
        log.exception(
            "reports.generation.failed",
            job_id=job.id,
            user_email=user_email,
            error=str(exc),
        )
        return job
    finally:
        clear_job_progress(job.id)
        clear_generation_context()


def _run_job_in_thread(job_id: str) -> None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        job = repo.get_generation_job(db, job_id)
        if job is not None:
            run_generation_job(db, job)
    finally:
        db.close()


def enqueue_or_run(
    db: Session,
    *,
    user_id: int | None,
    summary_date: date,
    report_ids: list[str] | None,
    job_options: dict | None = None,
) -> tuple[GenerationJob, str | None]:
    ids = report_ids or []
    job = repo.create_generation_job(db, user_id=user_id, summary_date=summary_date, report_ids=ids)
    if job_options:
        from app.legacy.report_runner import save_job_options

        save_job_options(job.id, job_options)
    if settings.celery_enabled:
        from app.adapters.inbound.celery.tasks import run_generation_job_task

        async_result = run_generation_job_task.delay(job.id)
        repo.update_generation_job(db, job, celery_task_id=async_result.id)
        return job, async_result.id
    threading.Thread(target=_run_job_in_thread, args=(job.id,), daemon=True).start()
    return job, None
