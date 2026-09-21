"""Celery tasks."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.adapters.outbound.persistence import repositories as repo
from app.application.audit_activity import record_activity
from app.application.schedule_config import run_state_key
from app.application.schedule_mail import send_scheduled_reports
from app.application.generation_cancel import reconcile_stale_generation_jobs
from app.application.use_cases.generate_reports import enqueue_or_run, run_generation_job
from app.config import settings
from app.database import SessionLocal
from app.infrastructure.celery_app import celery_app
from app.infrastructure.logging import get_logger, setup_logging
from celery.signals import worker_ready

setup_logging(settings.log_level)
log = get_logger(__name__)


@worker_ready.connect
def _reconcile_stale_generation_jobs_on_worker_start(**kwargs) -> None:
    db = SessionLocal()
    try:
        count = reconcile_stale_generation_jobs(db)
        if count:
            log.info("reports.generation.stale_jobs_reconciled", count=count)
    finally:
        db.close()


@celery_app.task(name="app.adapters.inbound.celery.tasks.run_proverka_task")
def run_proverka_task(job_id: str) -> dict:
    from app.adapters.outbound.proverka.service import run_job

    try:
        run_job(job_id)
        return {"job_id": job_id, "status": "completed"}
    except Exception as exc:
        log.exception("proverka.failed", job_id=job_id, error=str(exc))
        return {"job_id": job_id, "status": "failed", "error": str(exc)}


@celery_app.task(name="app.adapters.inbound.celery.tasks.run_proverka_machines_task")
def run_proverka_machines_task(job_id: str) -> dict:
    """Независимая от run_proverka_task проверка кол-ва машин по операциям."""
    from app.adapters.outbound.proverka.machines_service import run_job as run_machines_job

    try:
        run_machines_job(job_id)
        return {"job_id": job_id, "status": "completed"}
    except Exception as exc:
        log.exception("proverka.machines.failed", job_id=job_id, error=str(exc))
        return {"job_id": job_id, "status": "failed", "error": str(exc)}


@celery_app.task(name="app.adapters.inbound.celery.tasks.run_proverka_ai_task")
def run_proverka_ai_task(job_id: str) -> dict:
    from app.adapters.outbound.proverka.service import run_ai_job

    try:
        result = run_ai_job(job_id)
        return {"job_id": job_id, "ai_status": result.get("ai_status"), "status": "completed"}
    except Exception as exc:
        log.exception("proverka.ai.failed", job_id=job_id, error=str(exc))
        return {"job_id": job_id, "status": "failed", "error": str(exc)}


@celery_app.task(name="app.adapters.inbound.celery.tasks.daily_proverka_ai")
def daily_proverka_ai() -> dict:
    """21:00 MSK: proverka за вчера и сегодня + AI + email с Excel."""
    from app.adapters.outbound.proverka.machines_service import (
        daily_proverka_machines_job,
        read_job_status as read_machines_job_status,
        run_job as run_machines_job,
    )
    from app.adapters.outbound.proverka.service import daily_proverka_ai_job, read_job_status, run_job
    from app.application.proverka_mail import send_evening_proverka_report

    today = _today_msk()
    yesterday = today - timedelta(days=1)
    state_key = f"daily_proverka_evening:{today.isoformat()}"
    db = SessionLocal()
    try:
        if _already_ran_today(db, state_key, today):
            return {"skipped": "already_ran", "report_date": today.isoformat()}
        job_results: list[tuple[date, str | None, dict]] = []
        machines_job_results: list[tuple[date, str | None, dict]] = []
        for report_date in (yesterday, today):
            job_id: str | None = None
            status: dict = {"status": "failed", "error": "не запущено"}
            try:
                job_id = daily_proverka_ai_job(report_date)
                run_job(job_id)
                status = read_job_status(job_id) or {"status": "failed", "error": "нет status.json"}
                log.info(
                    "daily_proverka_ai.completed",
                    job_id=job_id,
                    report_date=report_date.isoformat(),
                    violations=status.get("violation_count"),
                )
            except Exception as exc:
                log.exception(
                    "daily_proverka_ai.job_failed",
                    report_date=report_date.isoformat(),
                    error=str(exc),
                )
                status = {"status": "failed", "error": str(exc)}
            job_results.append((report_date, job_id, status))

            machines_job_id: str | None = None
            machines_status: dict = {"status": "failed", "error": "не запущено"}
            try:
                machines_job_id = daily_proverka_machines_job(report_date)
                run_machines_job(machines_job_id)
                machines_status = read_machines_job_status(machines_job_id) or {
                    "status": "failed",
                    "error": "нет status.json",
                }
                log.info(
                    "daily_proverka_machines.completed",
                    job_id=machines_job_id,
                    report_date=report_date.isoformat(),
                )
            except Exception as exc:
                log.exception(
                    "daily_proverka_machines.job_failed",
                    report_date=report_date.isoformat(),
                    error=str(exc),
                )
                machines_status = {"status": "failed", "error": str(exc)}
            machines_job_results.append((report_date, machines_job_id, machines_status))

        mail_result = send_evening_proverka_report(job_results, machines_job_results)
        repo.mark_schedule_enqueued(db, state_key, today)
        record_activity(
            db,
            action="proverka.evening_report",
            dates=[d.isoformat() for d, _, _ in job_results],
            mail=mail_result,
        )
        return {
            "report_dates": [d.isoformat() for d, _, _ in job_results],
            "jobs": [
                {"date": d.isoformat(), "job_id": jid, "status": st.get("status")}
                for d, jid, st in job_results
            ],
            "machines_jobs": [
                {"date": d.isoformat(), "job_id": jid, "status": st.get("status")}
                for d, jid, st in machines_job_results
            ],
            "mail": mail_result,
        }
    except Exception as exc:
        log.exception("daily_proverka_ai.failed", error=str(exc))
        return {"error": str(exc)}
    finally:
        db.close()


@celery_app.task(name="app.adapters.inbound.celery.tasks.run_generation_job_task", bind=True)
def run_generation_job_task(self, job_id: str) -> dict:
    db = SessionLocal()
    try:
        job = repo.get_generation_job(db, job_id)
        if job is None:
            return {"error": "job_not_found"}
        job = run_generation_job(db, job)
        return {"job_id": job.id, "status": job.status}
    finally:
        db.close()


def _today_msk() -> date:
    return datetime.now(ZoneInfo(settings.schedule_timezone)).date()


def _now_msk() -> datetime:
    return datetime.now(ZoneInfo(settings.schedule_timezone))


def _should_run_now(entry: dict, now: datetime) -> bool:
    return now.hour == int(entry.get("hour", 0)) and now.minute == int(entry.get("minute", 0))


def _already_ran_today(db, state_key: str, today: date) -> bool:
    state = repo.get_schedule_run_state(db, state_key)
    return bool(state and state.last_enqueued_on == today)


@celery_app.task(name="app.adapters.inbound.celery.tasks.check_schedules")
def check_schedules() -> dict:
    """Каждую минуту: автогенерация и рассылка по расписанию из БД."""
    db = SessionLocal()
    results: dict[str, list] = {"generation": [], "mail": []}
    try:
        config = repo.get_schedule_config(db)
        now = _now_msk()
        today = now.date()

        for entry in config.get("generation") or []:
            if not entry.get("enabled", True):
                continue
            if not _should_run_now(entry, now):
                continue
            entry_id = str(entry.get("id") or "")
            state_key = run_state_key("gen", entry_id, int(entry["hour"]), int(entry["minute"]))
            if _already_ran_today(db, state_key, today):
                results["generation"].append({"id": entry_id, "skipped": "already_ran"})
                continue
            report_ids = [str(x) for x in entry.get("report_ids") or []]
            job, _ = enqueue_or_run(
                db,
                user_id=None,
                summary_date=today,
                report_ids=report_ids,
            )
            repo.mark_schedule_enqueued(db, state_key, today)
            log.info(
                "schedule.generation.enqueued",
                entry_id=entry_id,
                report_ids=report_ids,
                summary_date=today.isoformat(),
                job_id=job.id,
            )
            record_activity(
                db,
                action="schedule.generation.enqueued",
                job_id=job.id,
                report_ids=report_ids,
                summary_date=today.isoformat(),
                time=f"{entry['hour']:02d}:{entry['minute']:02d}",
                entry_id=entry_id,
            )
            results["generation"].append({"id": entry_id, "job_id": job.id})

        for entry in config.get("mail") or []:
            if not entry.get("enabled", True):
                continue
            if not _should_run_now(entry, now):
                continue
            entry_id = str(entry.get("id") or "")
            state_key = run_state_key("mail", entry_id, int(entry["hour"]), int(entry["minute"]))
            if _already_ran_today(db, state_key, today):
                results["mail"].append({"id": entry_id, "skipped": "already_ran"})
                continue
            report_ids = [str(x) for x in entry.get("report_ids") or []]
            emails = [str(x) for x in entry.get("emails") or []]
            mail_result = send_scheduled_reports(
                report_ids=report_ids,
                emails=emails,
                summary_date=today,
                subject=entry.get("subject"),
            )
            repo.mark_schedule_enqueued(db, state_key, today)
            if mail_result.get("ok"):
                log.info(
                    "schedule.mail.sent",
                    entry_id=entry_id,
                    report_ids=report_ids,
                    emails=mail_result.get("sent"),
                )
                record_activity(
                    db,
                    action="schedule.mail.sent",
                    report_ids=report_ids,
                    summary_date=today.isoformat(),
                    emails=mail_result.get("sent"),
                    time=f"{entry['hour']:02d}:{entry['minute']:02d}",
                    entry_id=entry_id,
                )
                results["mail"].append({"id": entry_id, "sent": mail_result.get("sent")})
            else:
                log.warning(
                    "schedule.mail.failed",
                    entry_id=entry_id,
                    error=mail_result.get("error"),
                )
                record_activity(
                    db,
                    action="schedule.mail.failed",
                    report_ids=report_ids,
                    summary_date=today.isoformat(),
                    error=mail_result.get("error"),
                    entry_id=entry_id,
                )
                results["mail"].append({"id": entry_id, "error": mail_result.get("error")})
        return results
    finally:
        db.close()
