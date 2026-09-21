"""Celery application."""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "svodki",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.adapters.inbound.celery.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone=settings.schedule_timezone,
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "check-schedules": {
            "task": "app.adapters.inbound.celery.tasks.check_schedules",
            "schedule": 60.0,
        },
        "daily-proverka-ai": {
            "task": "app.adapters.inbound.celery.tasks.daily_proverka_ai",
            "schedule": crontab(hour=21, minute=0),
        },
    },
)
