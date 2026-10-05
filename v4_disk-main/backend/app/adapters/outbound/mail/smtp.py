"""Отправка email через SMTP (STARTTLS или SSL)."""
from __future__ import annotations

import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

from app.config import settings
from app.infrastructure.logging import get_logger

log = get_logger(__name__)


def smtp_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def _connect_smtp():
    if settings.smtp_use_ssl:
        return smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=60)
    smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60)
    if settings.smtp_use_tls:
        smtp.starttls()
    return smtp


def _send_message(msg: EmailMessage, to: str, subject: str) -> bool:
    if not smtp_configured():
        log.warning("mail.skipped", to=to, reason="smtp_not_configured")
        return False
    try:
        with _connect_smtp() as smtp:
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
        log.info("mail.sent", to=to, subject=subject)
        return True
    except Exception as exc:
        log.error("mail.failed", to=to, error=str(exc))
        return False


def send_email(to: str, subject: str, body: str) -> bool:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return _send_message(msg, to, subject)


def _attachment_mime(path: Path) -> tuple[str, str]:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and "/" in guessed:
        maintype, subtype = guessed.split("/", 1)
        return maintype, subtype
    if path.suffix.lower() == ".xlsx":
        return (
            "application",
            "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    return "application", "octet-stream"


def send_email_with_attachments(
    to: str,
    subject: str,
    body: str,
    attachments: list[tuple[str, Path]],
) -> bool:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    for filename, path in attachments:
        data = path.read_bytes()
        maintype, subtype = _attachment_mime(path)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return _send_message(msg, to, subject)
