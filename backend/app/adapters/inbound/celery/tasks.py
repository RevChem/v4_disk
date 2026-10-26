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
from celery.signals import worker_process_init, worker_ready

setup_logging(settings.log_level)
log = get_logger(__name__)


@worker_process_init.connect
def _dispose_inherited_db_connections(**kwargs) -> None:
    """После fork у дочернего процесса нельзя оставлять соединение родителя."""
    from app.database import engine

    engine.dispose()


def _mark_generation_job_crashed(job_id: str, exc: BaseException) -> str:
    """Задача Celery умерла до записи статуса. Иначе в интерфейсе она вечно «в очереди»."""
    from app.application.generation_cancel import cancel_all_requested
    from app.database import SessionLocal, engine

    engine.dispose()
    status = "cancelled" if isinstance(exc, SystemExit) else "failed"
    db = SessionLocal()
    try:
        job = repo.get_generation_job(db, job_id)
        if job is None:
            return status
        if job.status in ("completed", "cancelled", "failed"):
            return str(job.status)
        if isinstance(exc, SystemExit) or cancel_all_requested(job):
            status = "cancelled"
        repo.update_generation_job(
            db,
            job,
            status=status,
            error_message=f"{status}: {exc.__class__.__name__}",
        )
        return status
    finally:
        db.close()


@worker_ready.connect
def _reconcile_stale_generation_jobs_on_worker_start(**kwargs) -> None:
    if not settings.celery_reconcile_on_start:
        return
    db = SessionLocal()
    try:
        count = reconcile_stale_generation_jobs(db)
        if count:
            log.info("reports.generation.stale_jobs_reconciled", count=count)
    finally:
        db.close()
    try:
        from app.adapters.outbound.proverka.service import reconcile_cancelled_proverka_jobs

        proverka_count = reconcile_cancelled_proverka_jobs()
        if proverka_count:
            log.info("proverka.stale_cancels_reconciled", count=proverka_count)
    except Exception:
        log.exception("proverka.stale_cancels_reconcile_failed")


@celery_app.task(name="app.adapters.inbound.celery.tasks.run_proverka_task")
def run_proverka_task(job_id: str) -> dict:
    from app.adapters.outbound.proverka.service import run_job

    try:
        run_job(job_id)
        from app.adapters.outbound.proverka.service import read_job_status

        status = str((read_job_status(job_id) or {}).get("status") or "completed")
        return {"job_id": job_id, "status": status}
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


@celery_app.task(name="app.adapters.inbound.celery.tasks.run_generation_job_task", bind=True)
def run_generation_job_task(self, job_id: str) -> dict:
    db = None
    try:
        db = SessionLocal()
        job = repo.get_generation_job(db, job_id)
        if job is None:
            return {"error": "job_not_found"}
        job = run_generation_job(db, job)
        return {"job_id": job.id, "status": job.status}
    except Exception as exc:
        log.exception("reports.generation.task_crashed", job_id=job_id, error=str(exc))
        status = _status_after_crash(job_id, exc, default="failed")
        return {"job_id": job_id, "status": status, "error": exc.__class__.__name__}
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        status = _status_after_crash(job_id, exc, default="cancelled")
        return {"job_id": job_id, "status": status, "error": exc.__class__.__name__}
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _status_after_crash(job_id: str, exc: BaseException, *, default: str) -> str:
    try:
        return _mark_generation_job_crashed(job_id, exc)
    except Exception:
        log.exception("reports.generation.crash_status_failed", job_id=job_id)
        return default


def _today_msk() -> date:
    return datetime.now(ZoneInfo(settings.schedule_timezone)).date()


def _now_msk() -> datetime:
    return datetime.now(ZoneInfo(settings.schedule_timezone))


def _catchup_minutes() -> int:
    try:
        return max(1, int(settings.schedule_catchup_minutes))
    except (TypeError, ValueError):
        return 1


def _slot_age(entry: dict, now: datetime) -> timedelta:
    """Сколько прошло с запланированного времени сегодня (отрицательное — слот ещё впереди)."""
    slot = now.replace(
        hour=int(entry.get("hour", 0)),
        minute=int(entry.get("minute", 0)),
        second=0,
        microsecond=0,
    )
    return now - slot


def _should_run_now(entry: dict, now: datetime, window_minutes: int | None = None) -> bool:
    """Слот наступил и ещё не вышло окно догона.

    Раньше требовалось точное совпадение минуты: тик beat раз в 60 с не привязан к границе
    минуты, поэтому при дрейфе, рестарте worker или деплое запуск за день терялся. Теперь слот
    считается «живым» ещё `schedule_catchup_minutes` минут; повторный запуск исключает
    `_already_ran_today`. Окно не переходит через полночь (дата сводки — «сегодня»).
    """
    window = _catchup_minutes() if window_minutes is None else max(1, window_minutes)
    return timedelta(0) <= _slot_age(entry, now) < timedelta(minutes=window)


def _is_last_attempt(entry: dict, now: datetime, window_minutes: int | None = None) -> bool:
    window = _catchup_minutes() if window_minutes is None else max(1, window_minutes)
    return _slot_age(entry, now) >= timedelta(minutes=window - 1)


def _already_ran_today(db, state_key: str, today: date) -> bool:
    state = repo.get_schedule_run_state(db, state_key)
    return bool(state and state.last_enqueued_on == today)


def _run_generation_entry(db, entry: dict, now: datetime, today: date, results: dict) -> None:
    entry_id = str(entry.get("id") or "")
    state_key = run_state_key("gen", entry_id, int(entry["hour"]), int(entry["minute"]))
    if _already_ran_today(db, state_key, today):
        results["generation"].append({"id": entry_id, "skipped": "already_ran"})
        return
    report_ids = [str(x) for x in entry.get("report_ids") or []]
    job_options = None
    try:
        from app.adapters.outbound.proverka.service import session_cookie_from_env

        if session_cookie_from_env():
            job_options = {"compare_cropwise": True}
    except Exception:
        log.warning("schedule.cropwise_cookie_unavailable")
    job, _ = enqueue_or_run(
        db,
        user_id=None,
        summary_date=today,
        report_ids=report_ids,
        job_options=job_options,
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
        time=f"{int(entry['hour']):02d}:{int(entry['minute']):02d}",
        entry_id=entry_id,
    )
    results["generation"].append({"id": entry_id, "job_id": job.id})


def _run_mail_entry(db, entry: dict, now: datetime, today: date, results: dict) -> None:
    entry_id = str(entry.get("id") or "")
    state_key = run_state_key("mail", entry_id, int(entry["hour"]), int(entry["minute"]))
    if _already_ran_today(db, state_key, today):
        results["mail"].append({"id": entry_id, "skipped": "already_ran"})
        return
    report_ids = [str(x) for x in entry.get("report_ids") or []]
    emails = [str(x) for x in entry.get("emails") or []]
    mail_result = send_scheduled_reports(
        report_ids=report_ids,
        emails=emails,
        summary_date=today,
        subject=entry.get("subject"),
    )
    if mail_result.get("ok"):
        # Отмечаем «выполнено» только при успехе: при сбое (PDF ещё не готовы, SMTP недоступен)
        # рассылка повторится на следующих тиках в пределах окна догона.
        repo.mark_schedule_enqueued(db, state_key, today)
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
            time=f"{int(entry['hour']):02d}:{int(entry['minute']):02d}",
            entry_id=entry_id,
        )
        results["mail"].append({"id": entry_id, "sent": mail_result.get("sent")})
        return
    last = _is_last_attempt(entry, now)
    log.warning(
        "schedule.mail.failed",
        entry_id=entry_id,
        error=mail_result.get("error"),
        final=last,
    )
    if last:  # в журнал активности — один раз, когда попытки закончились
        record_activity(
            db,
            action="schedule.mail.failed",
            report_ids=report_ids,
            summary_date=today.isoformat(),
            error=mail_result.get("error"),
            entry_id=entry_id,
        )
    results["mail"].append({"id": entry_id, "error": mail_result.get("error"), "will_retry": not last})


@celery_app.task(name="app.adapters.inbound.celery.tasks.check_field_firm_mismatch")
def check_field_firm_mismatch() -> dict:
    """Раз в 3 часа: поля, у которых пометка фирмы в Namolot расходится с папкой Cropwise."""
    from app.application.field_firm_check import run_check

    state = run_check()
    return {"status": state.get("status"), "items": len(state.get("items") or [])}


@celery_app.task(name="app.adapters.inbound.celery.tasks.check_schedules")
def check_schedules() -> dict:
    """Каждую минуту: автогенерация и рассылка по расписанию из БД."""
    db = SessionLocal()
    results: dict[str, list] = {"generation": [], "mail": []}
    try:
        config = repo.get_schedule_config(db)
        now = _now_msk()
        today = now.date()

        for kind, runner in (("generation", _run_generation_entry), ("mail", _run_mail_entry)):
            for entry in config.get(kind) or []:
                if not entry.get("enabled", True) or not _should_run_now(entry, now):
                    continue
                try:
                    runner(db, entry, now, today, results)
                except Exception as exc:
                    # Сбой одной записи не должен останавливать остальные; слот не отмечен —
                    # следующий тик повторит попытку в пределах окна догона.
                    db.rollback()
                    log.exception(
                        "schedule.entry_failed", kind=kind, entry_id=str(entry.get("id") or "")
                    )
                    results[kind].append({"id": str(entry.get("id") or ""), "error": str(exc)})
        return results
    finally:
        db.close()
