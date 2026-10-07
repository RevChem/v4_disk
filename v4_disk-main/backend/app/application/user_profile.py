"""Профиль пользователя: ФИО, входы в систему, запуски сводок и время их формирования."""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.outbound.persistence import repositories as repo
from app.application.activity_log import iso_utc, job_total_seconds
from app.application.audit_activity import CHANGE_ACTION_AREAS, change_text, report_title
from app.models import GenerationJob, User

LOGIN_ACTION = "auth.login.success"


def _short_title(report_id: str, title: str) -> str:
    """«24. Техника с выработкой…» → «Техника с выработкой…» (номер выводим отдельно)."""
    rid = re.escape(str(report_id))
    cleaned = re.sub(rf"^\s*{rid}(?!\d)\s*\.?\s*", "", title or "").strip()
    return cleaned or title


def _job_item(job: GenerationJob) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if job.result_json:
        try:
            parsed = json.loads(job.result_json)
            result = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            result = {}
    try:
        rids = [str(x) for x in json.loads(job.report_ids or "[]")]
    except json.JSONDecodeError:
        rids = []

    failed_map = {str(x.get("report_id")): x for x in (result.get("failed") or [])}
    created_map = {str(x.get("report_id")): x for x in (result.get("created") or [])}

    reports: list[dict[str, Any]] = []
    for rid in rids:
        failed = failed_map.get(rid)
        created = created_map.get(rid)
        if failed and failed.get("cancelled"):
            status = "cancelled"
        elif failed:
            status = "error"
        elif created:
            status = "ok"
        else:
            # задание ещё идёт / упало целиком / сводка не дошла до результата
            status = job.status
        reports.append({
            "report_id": rid,
            "title": _short_title(rid, report_title(rid)),
            "status": status,
            "duration_seconds": (created or {}).get("duration_seconds"),
        })

    return {
        "job_id": job.id,
        "launched_at": iso_utc(job.created_at),
        "summary_date": job.summary_date.isoformat(),
        "status": job.status,
        "report_ids": rids,
        "reports": reports,
        "created_count": job.created_count,
        "failed_count": job.failed_count,
        "total_duration_seconds": job_total_seconds(job, result),
        "error_message": job.error_message,
    }


def _change_item(row) -> dict[str, Any]:
    try:
        details = json.loads(row.details or "{}")
        details = details if isinstance(details, dict) else {}
    except json.JSONDecodeError:
        details = {}
    return {
        "id": row.id,
        "at": iso_utc(row.created_at),
        "action": row.action,
        "area": CHANGE_ACTION_AREAS.get(row.action, "Прочее"),
        "text": change_text(row.action, details),
        "summary_date": details.get("summary_date"),
    }


def build_user_profile(
    db: Session,
    user: User,
    *,
    logins_limit: int = 50,
    jobs_limit: int = 50,
    changes_limit: int = 100,
) -> dict[str, Any]:
    login_rows = repo.list_user_audit_logs(db, user.id, action=LOGIN_ACTION, limit=logins_limit)
    logins = [{"at": iso_utc(r.created_at), "ip": r.ip} for r in login_rows]
    jobs = repo.list_generation_jobs_for_user(db, user.id, limit=jobs_limit)
    change_actions = tuple(CHANGE_ACTION_AREAS)
    change_rows = repo.list_user_audit_by_actions(
        db, user.id, actions=change_actions, limit=changes_limit
    )

    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "email": user.email,
            "role": user.role,
            "is_active": user.is_active,
            "created_at": iso_utc(user.created_at),
        },
        "logins": {
            "total": repo.count_user_audit_logs(db, user.id, action=LOGIN_ACTION),
            "last_at": logins[0]["at"] if logins else None,
            "items": logins,
        },
        "generations": {
            "total": repo.count_generation_jobs_for_user(db, user.id),
            "items": [_job_item(j) for j in jobs],
        },
        "changes": {
            "total": repo.count_user_audit_by_actions(db, user.id, actions=change_actions),
            "items": [_change_item(r) for r in change_rows],
        },
    }
