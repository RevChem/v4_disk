"""Структурированное логирование (structlog → JSON stdout → Promtail → Loki)."""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict

from app.config import settings

_SENSITIVE_KEYS = {
    "password",
    "confirm_password",
    "hashed_password",
    "smtp_password",
    "access_token",
    "refresh_token",
    "mfa_pending_token",
    "mfa_secret",
    "mfa_code",
    "cropwise_api_token",
    "jwt_secret",
    "authorization",
}


def _redact_value(key: str, value: object) -> object:
    if key.lower() in _SENSITIVE_KEYS:
        return "***"
    if isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, item) for item in value]
    return value


def redact_sensitive_fields(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    return {k: _redact_value(k, v) for k, v in event_dict.items()}


def add_service_name(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    service = (settings.svodki_service or "").strip()
    if service:
        event_dict.setdefault("service", service)
    return event_dict


def setup_logging(log_level: str = "INFO") -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        add_service_name,
        redact_sensitive_fields,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]
    structlog.configure(
        processors=shared + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "httpx", "hpack", "celery"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
