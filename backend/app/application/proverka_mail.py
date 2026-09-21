"""Email с результатами вечерней proverka."""
from __future__ import annotations

from datetime import date
from typing import Any

from app.adapters.outbound.mail.smtp import send_email_with_attachments, smtp_configured
from app.adapters.outbound.proverka.service import excel_path_for_job
from app.config import settings
from app.infrastructure.logging import get_logger

log = get_logger(__name__)


def evening_mail_recipients() -> list[str]:
    raw = (settings.proverka_evening_mail_to or "").strip()
    if not raw:
        return []
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        email = part.strip().lower()
        if email and email not in out:
            out.append(email)
    return out


def _job_summary(report_date: date, job_id: str | None, st: dict[str, Any]) -> str:
    label = report_date.strftime("%d.%m.%Y")
    if not job_id or st.get("status") == "failed":
        err = st.get("error") or "неизвестная ошибка"
        return f"• {label}: ОШИБКА — {err}"
    vc = int(st.get("violation_count") or 0)
    ai_vc = int(st.get("ai_violation_count") or 0)
    ai_st = st.get("ai_status") or "—"
    line = f"• {label}: нарушений {vc}"
    if ai_st not in ("skipped", "none", None, "—"):
        line += f", AI: {ai_vc} ({ai_st})"
    summary = st.get("ai_report") or {}
    if isinstance(summary, dict):
        exec_sum = str(summary.get("executive_summary") or "").strip()
        if exec_sum:
            line += f"\n  {exec_sum[:400]}"
    return line


def _machines_job_summary(report_date: date, job_id: str | None, st: dict[str, Any]) -> str:
    label = report_date.strftime("%d.%m.%Y")
    if not job_id or st.get("status") == "failed":
        err = st.get("error") or "неизвестная ошибка"
        return f"• {label}: ОШИБКА — {err}"
    cc = st.get("machine_cross_check") or {}
    summary = cc.get("summary") or {}
    diff = int(summary.get("diff") or 0)
    ok = int(summary.get("ok") or 0)
    partial = int(summary.get("partial") or 0)
    line = f"• {label}: расхождений {diff}, OK {ok}"
    if partial:
        line += f", частично {partial}"
    rows = st.get("machine_cross_check_rows") or cc.get("operations") or []
    diff_rows = [
        r for r in rows
        if isinstance(r, dict) and r.get("status") == "DIFF"
    ]
    for row in diff_rows[:5]:
        name = row.get("display_name") or row.get("group_name") or "?"
        max_diff = row.get("max_diff_machines")
        line += f"\n  — {name}: Δ {max_diff} маш."
    if len(diff_rows) > 5:
        line += f"\n  … ещё {len(diff_rows) - 5} операций с DIFF"
    return line


def send_evening_proverka_report(
    job_results: list[tuple[date, str | None, dict[str, Any]]],
    machines_job_results: list[tuple[date, str | None, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Excel + краткая сводка по каждому прогону на список получателей."""
    if not settings.proverka_evening_mail_enabled:
        return {"ok": False, "skipped": "disabled"}
    emails = evening_mail_recipients()
    if not emails:
        return {"ok": False, "error": "PROVERKA_EVENING_MAIL_TO не задан"}
    if not smtp_configured():
        return {"ok": False, "error": "SMTP не настроен (SMTP_HOST / SMTP_FROM)"}

    attachments: list[tuple[str, Any]] = []
    body_lines = [
        "Автоматическая проверка сводок (proverka).",
        "",
        "Проверка сводок:",
    ]
    for report_date, job_id, st in job_results:
        body_lines.append(_job_summary(report_date, job_id, st))
        if not job_id:
            continue
        path = excel_path_for_job(job_id)
        if path is not None:
            attachments.append((path.name, path))

    if machines_job_results:
        body_lines.extend(["", "Проверка кол-ва машин:"])
        for report_date, job_id, st in machines_job_results:
            body_lines.append(_machines_job_summary(report_date, job_id, st))

    if not attachments:
        body_lines.append("")
        body_lines.append("Excel-файлы не приложены (проверка не завершилась или файл не найден).")

    subject_dates = ", ".join(rd.strftime("%d.%m") for rd, _, _ in job_results)
    subject = f"Проверка сводок — {subject_dates}"
    body = "\n".join(body_lines)

    sent: list[str] = []
    failed: list[str] = []
    for email in emails:
        if send_email_with_attachments(email, subject, body, attachments):
            sent.append(email)
        else:
            failed.append(email)

    if sent:
        log.info("proverka.evening_mail.sent", emails=sent, attachments=len(attachments))
        return {"ok": True, "sent": sent, "failed": failed, "attachments": len(attachments)}
    return {"ok": False, "error": "Не удалось отправить ни одному получателю", "failed": failed}
