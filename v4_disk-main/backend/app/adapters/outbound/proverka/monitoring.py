"""Cropwise-мониторинг: cookie пользователя, авто-проверка после генерации."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.adapters.outbound.auth.security import decrypt_mfa_secret, encrypt_mfa_secret
from app.config import settings


def _cookie_ttl_hours() -> int:
    return max(1, int(settings.proverka_monitoring_cookie_ttl_hours))


def _monitor_root() -> Path:
    root = Path(settings.upload_dir) / "cropwise_monitor"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _user_state_path(user_id: int) -> Path:
    return _monitor_root() / f"user_{user_id}.json"


def _checks_path(user_id: int, report_date: date) -> Path:
    d = _monitor_root() / "checks" / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{report_date.isoformat()}.json"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def load_user_state(user_id: int) -> dict[str, Any]:
    path = _user_state_path(user_id)
    if not path.is_file():
        return {"user_id": user_id, "monitoring_enabled": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"user_id": user_id, "monitoring_enabled": False}
    return data if isinstance(data, dict) else {"user_id": user_id, "monitoring_enabled": False}


def save_user_state(user_id: int, state: dict[str, Any]) -> None:
    state = dict(state)
    state["user_id"] = user_id
    _user_state_path(user_id).write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _expire_if_needed(state: dict[str, Any]) -> dict[str, Any]:
    expires = _parse_iso_dt(state.get("cookie_expires_at"))
    if expires and _utcnow() >= expires:
        state.pop("cookie_encrypted", None)
        state["cookie_expires_at"] = None
        state["monitoring_enabled"] = False
    return state


def get_cookie_for_user(user_id: int) -> str | None:
    state = _expire_if_needed(load_user_state(user_id))
    enc = state.get("cookie_encrypted")
    if not enc:
        save_user_state(user_id, state)
        return None
    try:
        cookie = decrypt_mfa_secret(str(enc))
    except ValueError:
        state.pop("cookie_encrypted", None)
        state["monitoring_enabled"] = False
        save_user_state(user_id, state)
        return None
    save_user_state(user_id, state)
    return cookie.strip() or None


def is_monitoring_enabled(user_id: int) -> bool:
    state = _expire_if_needed(load_user_state(user_id))
    save_user_state(user_id, state)
    return bool(state.get("monitoring_enabled")) and bool(state.get("cookie_encrypted"))


def set_cookie_and_enable(user_id: int, cookie: str) -> dict[str, Any]:
    cookie = (cookie or "").strip()
    if not cookie:
        raise ValueError("Введите cookie браузера Cropwise")
    expires = _utcnow() + timedelta(hours=_cookie_ttl_hours())
    state = {
        "user_id": user_id,
        "monitoring_enabled": True,
        "cookie_encrypted": encrypt_mfa_secret(cookie),
        "cookie_expires_at": expires.isoformat(),
        "cookie_set_at": _utcnow().isoformat(),
    }
    save_user_state(user_id, state)
    return state


def enable_monitoring(user_id: int) -> dict[str, Any]:
    state = _expire_if_needed(load_user_state(user_id))
    if not state.get("cookie_encrypted"):
        raise ValueError("Сначала введите cookie")
    state["monitoring_enabled"] = True
    save_user_state(user_id, state)
    return state


def disable_monitoring(user_id: int) -> dict[str, Any]:
    state = _expire_if_needed(load_user_state(user_id))
    state["monitoring_enabled"] = False
    save_user_state(user_id, state)
    return state


def load_checks(user_id: int, report_date: date) -> dict[str, Any]:
    path = _checks_path(user_id, report_date)
    if not path.is_file():
        return {"report_date": report_date.isoformat(), "jobs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"report_date": report_date.isoformat(), "jobs": {}}
    return data if isinstance(data, dict) else {"report_date": report_date.isoformat(), "jobs": {}}


def save_checks(user_id: int, report_date: date, data: dict[str, Any]) -> None:
    _checks_path(user_id, report_date).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def record_job_result(user_id: int, report_date: date, job_id: str, status: dict[str, Any]) -> None:
    store = load_checks(user_id, report_date)
    jobs = store.setdefault("jobs", {})
    jobs[job_id] = {
        "job_id": job_id,
        "report_ids": status.get("report_ids") or [],
        "status": status.get("status"),
        "violation_count": status.get("violation_count", 0),
        "cropwise_diff_count": status.get("cropwise_diff_count", 0),
        "violations": status.get("violations") or [],
        "error_logs": status.get("error_logs") or [],
        "updated_at": _utcnow().isoformat(),
    }
    save_checks(user_id, report_date, store)


def aggregate_violations(user_id: int, report_date: date) -> dict[str, Any]:
    store = load_checks(user_id, report_date)
    jobs: dict[str, Any] = store.get("jobs") or {}
    violations: list[dict[str, Any]] = []
    error_logs: list[dict[str, str]] = []
    seen: set[str] = set()
    total = 0
    cropwise_diff = 0
    for job in jobs.values():
        if job.get("status") != "completed":
            continue
        total += int(job.get("violation_count") or 0)
        cropwise_diff += int(job.get("cropwise_diff_count") or 0)
        for v in job.get("violations") or []:
            key = f"{v.get('kind')}-{v.get('report_id')}-{v.get('detail')}"
            if key not in seen:
                seen.add(key)
                violations.append(v)
        for e in job.get("error_logs") or []:
            error_logs.append(e)
    return {
        "violation_count": total,
        "cropwise_diff_count": cropwise_diff,
        "violations": violations,
        "error_logs": error_logs,
    }


def monitoring_status(user_id: int, report_date: date | None = None) -> dict[str, Any]:
    state = _expire_if_needed(load_user_state(user_id))
    save_user_state(user_id, state)
    cookie_valid = bool(state.get("cookie_encrypted"))
    monitoring_on = bool(state.get("monitoring_enabled")) and cookie_valid
    expires = state.get("cookie_expires_at")
    out: dict[str, Any] = {
        "monitoring_enabled": monitoring_on,
        "cookie_valid": cookie_valid,
        "cookie_expires_at": expires,
        "violation_count": 0,
        "cropwise_diff_count": 0,
        "violations": [],
        "error_logs": [],
    }
    if report_date:
        agg = aggregate_violations(user_id, report_date)
        out.update(agg)
    return out


def enqueue_report_checks(
    user_id: int,
    report_date: date,
    report_ids: list[str],
) -> list[str]:
    """Запуск proverka для каждой сводки, если мониторинг включён."""
    if not is_monitoring_enabled(user_id):
        return []
    cookie = get_cookie_for_user(user_id)
    if not cookie:
        return []

    from app.adapters.outbound.proverka.service import (
        _ensure_plan_path,
        create_job,
        editor_season_overrides,
        run_job,
    )

    _ensure_plan_path()
    from proverka.reports_config import REPORTS

    comparable = [
        str(rid) for rid in report_ids
        if REPORTS.get(str(rid))
        and (REPORTS[str(rid)].cropwise_comparable or REPORTS[str(rid)].cropwise_per_operation)
    ]
    all_overrides = editor_season_overrides(report_date, comparable)

    job_ids: list[str] = []
    for rid in comparable:
        spec = REPORTS[str(rid)]
        rid_overrides = {rid: all_overrides[rid]} if rid in all_overrides else None
        job_id = create_job(
            report_date=report_date,
            report_ids=[rid],
            compare_cropwise=True,
            cookie=cookie,
            season_overrides=rid_overrides,
            user_id=user_id,
        )
        job_ids.append(job_id)
        try:
            from app.config import settings

            if settings.celery_enabled:
                from app.adapters.inbound.celery.tasks import run_proverka_task

                run_proverka_task.delay(job_id)
            else:
                import threading

                threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
        except Exception:
            run_job(job_id)
    return job_ids
