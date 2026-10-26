"""Celery application."""
from __future__ import annotations

from celery import Celery

from app.config import settings

celery_app = Celery(
    "svodki",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.adapters.inbound.celery.tasks"],
)

# Задачи, которые идут долго и держат гигабайты RAM. Если задан CELERY_HEAVY_QUEUE,
# их забирает worker-heavy (concurrency=1, процесс перезапускается после задачи).
# check_schedules остаётся в очереди celery.
HEAVY_TASK_NAMES = (
    "app.adapters.inbound.celery.tasks.run_generation_job_task",
    "app.adapters.inbound.celery.tasks.run_proverka_task",
    "app.adapters.inbound.celery.tasks.run_proverka_machines_task",
    "app.adapters.inbound.celery.tasks.run_proverka_ai_task",
)
_heavy_queue = (settings.celery_heavy_queue or "").strip()

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone=settings.schedule_timezone,
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_routes={name: {"queue": _heavy_queue} for name in HEAVY_TASK_NAMES} if _heavy_queue else {},
    beat_schedule={
        "check-schedules": {
            "task": "app.adapters.inbound.celery.tasks.check_schedules",
            "schedule": 60.0,
        },
        "check-field-firm-mismatch": {
            "task": "app.adapters.inbound.celery.tasks.check_field_firm_mismatch",
            "schedule": float(max(settings.field_firm_check_interval_sec, 300)),
        },
    },
)
