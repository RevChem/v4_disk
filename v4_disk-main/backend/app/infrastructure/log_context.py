"""Контекст structlog для корреляции задач генерации."""
from __future__ import annotations

import structlog

_GENERATION_KEYS = ("job_id", "user_id", "user_email", "summary_date", "report_ids")


def bind_generation_context(
    *,
    job_id: str,
    user_id: int | None = None,
    user_email: str | None = None,
    summary_date: str | None = None,
    report_ids: list[str] | None = None,
) -> None:
    payload: dict[str, object] = {"job_id": job_id}
    if user_id is not None:
        payload["user_id"] = user_id
    if user_email:
        payload["user_email"] = user_email
    if summary_date:
        payload["summary_date"] = summary_date
    if report_ids is not None:
        payload["report_ids"] = report_ids
    structlog.contextvars.bind_contextvars(**payload)


def clear_generation_context() -> None:
    structlog.contextvars.unbind_contextvars(*_GENERATION_KEYS)
