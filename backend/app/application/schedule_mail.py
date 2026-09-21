"""Поиск PDF сводок и отправка по расписанию."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from app.config import settings
from app.infrastructure.logging import get_logger

log = get_logger(__name__)


def find_report_pdfs(report_ids: list[str], summary_date: date) -> dict[str, Path]:
    from app.legacy.report_runner import _report_id_from_filename, list_generated_files

    folder = Path(settings.upload_dir) / summary_date.isoformat()
    found: dict[str, Path] = {}
    for item in list_generated_files(summary_date):
        rid = item.get("report_id") or _report_id_from_filename(item.get("filename") or "")
        if rid not in report_ids:
            continue
        path = folder / item["filename"]
        if path.is_file():
            found[rid] = path
    return found


def send_scheduled_reports(
    *,
    report_ids: list[str],
    emails: list[str],
    summary_date: date,
    subject: str | None = None,
) -> dict[str, object]:
    from app.adapters.outbound.mail.smtp import send_email_with_attachments

    pdfs = find_report_pdfs(report_ids, summary_date)
    missing = [rid for rid in report_ids if rid not in pdfs]
    if missing:
        return {"ok": False, "error": f"PDF не найдены: {', '.join(missing)}", "missing": missing}

    attachments = [(path.name, path) for path in pdfs.values()]
    date_label = summary_date.strftime("%d.%m.%Y")
    mail_subject = subject or f"Сводки {date_label}"
    body = (
        f"Автоматическая рассылка сводок за {date_label}.\n\n"
        f"Сводки: {', '.join(report_ids)}\n"
    )
    sent: list[str] = []
    failed: list[str] = []
    for email in emails:
        if send_email_with_attachments(email, mail_subject, body, attachments):
            sent.append(email)
        else:
            failed.append(email)
    if failed and not sent:
        return {"ok": False, "error": "Не удалось отправить ни одному получателю", "failed": failed}
    return {"ok": True, "sent": sent, "failed": failed, "report_ids": report_ids}
