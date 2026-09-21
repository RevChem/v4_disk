"""Сборка ленты «Пользователи» и «Сводки» для UI."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.outbound.persistence import repositories as repo
from app.application.audit_activity import (
    compact_report_ids,
    format_action_message,
    report_title,
    user_label,
)
from app.models import User


# Записи аудита, которые не показываем в ленте «Логи».
# Вход в систему остаётся в audit_logs — по нему строится профиль пользователя.
HIDDEN_FEED_ACTIONS: tuple[str, ...] = (
    "reports.generation.requested",
    "reports.generation.completed",
    "reports.generation.failed",
    "auth.login.success",
)

_FINISHED_JOB_STATUSES = ("completed", "failed", "cancelled")


def as_utc(dt: datetime) -> datetime:
    """SQLite отдаёт naive-время (UTC), PostgreSQL — aware."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def iso_utc(dt: datetime | None) -> str | None:
    return as_utc(dt).isoformat() if dt else None


def job_total_seconds(job: Any, result: dict[str, Any] | None = None) -> float | None:
    """Полное время формирования всех сводок задания, сек.

    Новые задания хранят точное значение в result_json.total_duration_seconds.
    Для старых заданий (до появления этого поля) берём разницу updated_at − created_at.
    """
    raw = (result or {}).get("total_duration_seconds")
    if isinstance(raw, (int, float)) and raw >= 0:
        return float(raw)
    if job.status != "completed" or not job.created_at or not job.updated_at:
        return None
    delta = (as_utc(job.updated_at) - as_utc(job.created_at)).total_seconds()
    return round(delta, 1) if delta >= 0 else None


def _parse_details(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {"raw": raw}


def _log_lines_for_report(logs: list[str], report_id: str) -> list[str]:
    if not logs:
        return []
    rid = str(report_id)
    patterns = (
        re.compile(rf"\b{re.escape(rid)}\b", re.I),
        re.compile(rf"report\s*{re.escape(rid)}\b", re.I),
        re.compile(rf"сводк[aи]?\s*{re.escape(rid)}\b", re.I),
    )
    out: list[str] = []
    for line in logs:
        s = str(line).strip()
        if not s:
            continue
        if any(p.search(s) for p in patterns):
            out.append(s)
    if not out and logs:
        # fallback: последние строки с error по всему job
        out = [
            str(x).strip()
            for x in logs[-40:]
            if re.search(r"error|ошибк|failed|exception|traceback", str(x), re.I)
        ]
    return out[-30:]


def build_activity_feed(
    db: Session,
    *,
    viewer: User,
    limit: int = 80,
) -> dict[str, Any]:
    """user_events — действия людей; report_events — ошибки/детали по сводкам."""
    see_all = viewer.role in ("admin", "developer")
    viewer_id = viewer.id

    audit_rows = repo.list_audit_logs(
        db,
        limit=min(limit * 3, 300),
        offset=0,
        exclude_actions=HIDDEN_FEED_ACTIONS,
    )
    user_cache: dict[int, User | None] = {}

    def _user(uid: int | None) -> User | None:
        if uid is None:
            return None
        if uid not in user_cache:
            user_cache[uid] = repo.get_user_by_id(db, uid)
        return user_cache[uid]

    user_events: list[dict[str, Any]] = []
    report_events: list[dict[str, Any]] = []
    seen_report_keys: set[str] = set()
    seen_user_ids: set[str] = set()

    for row in audit_rows:
        if row.action in HIDDEN_FEED_ACTIONS:
            continue
        if not see_all and row.user_id not in (None, viewer_id):
            continue
        details = _parse_details(row.details)
        user = _user(row.user_id)
        username = details.get("username") or user_label(user)
        message = details.get("message") or format_action_message(
            row.action, username, details,
        )
        category = details.get("category") or (
            "report" if row.action == "reports.generation.report_failed" else "user"
        )

        base = {
            "id": f"audit-{row.id}",
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "action": row.action,
            "message": message,
            "user_id": row.user_id,
            "username": username,
            "ip": row.ip,
            "job_id": details.get("job_id"),
            "summary_date": details.get("summary_date") or details.get("report_date"),
            "report_ids": details.get("report_ids") or [],
            "details": details,
        }

        if category == "report" or row.action == "reports.generation.report_failed":
            rid = str(details.get("report_id") or "")
            key = f"{details.get('job_id')}:{rid}"
            if key not in seen_report_keys:
                seen_report_keys.add(key)
                report_events.append({
                    **base,
                    "report_id": rid,
                    "title": details.get("title") or report_title(rid),
                    "status": "error",
                    "error": details.get("error") or message,
                    "log_lines": details.get("log_lines") or [],
                })
        else:
            uid = base["id"]
            if uid not in seen_user_ids:
                seen_user_ids.add(uid)
                user_events.append(base)

    # generation_jobs — детализация по сводкам (ok/error)
    jobs = repo.list_generation_jobs(db, limit=min(limit, 100))
    for job in jobs:
        if not see_all and job.user_id != viewer_id:
            continue
        user = _user(job.user_id)
        username = user_label(user)
        result = {}
        if job.result_json:
            try:
                result = json.loads(job.result_json)
            except json.JSONDecodeError:
                result = {}
        failed_map = {str(x.get("report_id")): x for x in (result.get("failed") or [])}
        created_map = {str(x.get("report_id")): x for x in (result.get("created") or [])}
        logs = result.get("logs") or []
        try:
            rids = json.loads(job.report_ids or "[]")
        except json.JSONDecodeError:
            rids = []

        reports_summary: list[dict[str, Any]] = []
        for rid in rids:
            rs = str(rid)
            if rs in failed_map:
                item = failed_map[rs]
                status = "error"
                error = str(item.get("error") or "ошибка")
                title = item.get("title") or report_title(rs)
                log_lines = _log_lines_for_report(logs, rs)
                key = f"{job.id}:{rs}"
                if key not in seen_report_keys:
                    seen_report_keys.add(key)
                    report_events.append({
                        "id": f"job-{job.id}-{rs}",
                        "created_at": (job.updated_at or job.created_at).isoformat(),
                        "action": "reports.generation.report_failed",
                        "message": f"Сводка {rs} ({title}): {error[:200]}",
                        "user_id": job.user_id,
                        "username": username,
                        "job_id": job.id,
                        "summary_date": job.summary_date.isoformat(),
                        "report_id": rs,
                        "title": title,
                        "status": "error",
                        "error": error,
                        "log_lines": log_lines,
                    })
            else:
                c = created_map.get(rs) or {}
                status = "ok"
                error = None
                title = c.get("title") or report_title(rs)
            reports_summary.append({
                "report_id": rs,
                "title": title,
                "status": status,
                "error": error,
            })

        has_error = bool(failed_map) or job.status == "failed"
        if rids:
            labels = compact_report_ids([str(x) for x in rids])
            msg = f"{username} — сводки {labels} за {job.summary_date.isoformat()}"
            if has_error:
                msg += f" (ошибок: {len(failed_map) or 1})"
            elif job.status == "completed":
                msg += " — готово"
            elif job.status == "running":
                msg += " — выполняется…"
            else:
                msg += f" — {job.status}"

            # одна карточка на job в ленте пользователей (если ещё нет audit completed)
            jid = f"job-{job.id}"
            if jid in seen_user_ids:
                continue
            seen_user_ids.add(jid)
            user_events.append({
                "id": jid,
                "created_at": (job.created_at or datetime.utcnow()).isoformat(),
                "action": "reports.generation.job",
                "message": msg,
                "user_id": job.user_id,
                "username": username,
                "job_id": job.id,
                "summary_date": job.summary_date.isoformat(),
                "report_ids": [str(x) for x in rids],
                "reports": reports_summary,
                "has_error": has_error,
                "job_status": job.status,
                "duration_seconds": job_total_seconds(job, result),
            })

    user_events.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    report_events.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    if see_all:
        user_rows = (
            db.query(User)
            .filter(User.is_active.is_(True))
            .order_by(User.username.asc())
            .all()
        )
        users = [{"id": u.id, "username": user_label(u)} for u in user_rows]
    else:
        users = [{"id": viewer.id, "username": user_label(viewer)}]

    return {
        "user_events": user_events[:limit],
        "report_events": report_events[:limit],
        "users": users,
        "viewer_can_see_all": see_all,
    }


def job_detail(db: Session, job_id: str, *, viewer: User) -> dict[str, Any] | None:
    job = repo.get_generation_job(db, job_id)
    if job is None:
        return None
    if viewer.role not in ("admin", "developer") and job.user_id != viewer.id:
        return None
    user = repo.get_user_by_id(db, job.user_id) if job.user_id else None
    result = {}
    if job.result_json:
        try:
            result = json.loads(job.result_json)
        except json.JSONDecodeError:
            result = {}
    logs = result.get("logs") or []
    reports: list[dict[str, Any]] = []
    try:
        rids = json.loads(job.report_ids or "[]")
    except json.JSONDecodeError:
        rids = []
    failed_map = {str(x.get("report_id")): x for x in (result.get("failed") or [])}
    for rid in rids:
        rs = str(rid)
        failed = failed_map.get(rs)
        reports.append({
            "report_id": rs,
            "title": (failed or {}).get("title") or report_title(rs),
            "status": "error" if failed else ("ok" if job.status == "completed" else job.status),
            "error": failed.get("error") if failed else job.error_message,
            "log_lines": _log_lines_for_report(logs, rs) if failed else [],
        })
    return {
        "job_id": job.id,
        "status": job.status,
        "summary_date": job.summary_date.isoformat(),
        "username": user_label(user),
        "user_id": job.user_id,
        "reports": reports,
        "logs": logs[-200:],
        "error_message": job.error_message,
    }
