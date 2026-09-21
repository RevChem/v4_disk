"""Репозитории SQLAlchemy (sync)."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.adapters.outbound.auth.security import hash_token
from app.config import settings
from app.models import (
    AuditLog,
    EmailVerificationToken,
    GenerationJob,
    RefreshToken,
    ScheduleConfig,
    ScheduleRunState,
    User,
)


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.query(User).filter(User.email == email).first()


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.query(User).filter(User.username == username).first()


def get_user_by_id(db: Session, user_id: int) -> User | None:
    return db.query(User).filter(User.id == user_id).first()


def count_users(db: Session) -> int:
    return db.query(User).count()


def list_users(db: Session) -> list[User]:
    return db.query(User).order_by(User.id.asc()).all()


def create_user(
    db: Session,
    *,
    username: str,
    email: str | None,
    password_hash: str,
    role: str = "user",
    email_verified: bool = False,
    full_name: str | None = None,
) -> User:
    user = User(
        username=username,
        full_name=full_name,
        email=email,
        password_hash=password_hash,
        role=role,
        email_verified=email_verified,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def is_locked(user: User) -> bool:
    if user.locked_until is None:
        return False
    now = datetime.now(UTC)
    locked = user.locked_until
    if locked.tzinfo is None:
        locked = locked.replace(tzinfo=UTC)
    return locked > now


def record_failed_login(db: Session, user: User) -> None:
    user.failed_login_attempts += 1
    if user.failed_login_attempts >= settings.max_failed_login_attempts:
        user.locked_until = datetime.now(UTC) + timedelta(minutes=settings.lockout_minutes)
    db.commit()


def reset_failed_login(db: Session, user: User) -> None:
    user.failed_login_attempts = 0
    user.locked_until = None
    db.commit()


def save_refresh_token(
    db: Session,
    user_id: int,
    jti: str,
    family_id: str,
    expires_at: datetime,
) -> None:
    db.add(
        RefreshToken(
            user_id=user_id,
            jti_hash=hash_token(jti),
            family_id=family_id,
            expires_at=expires_at,
        )
    )
    db.commit()


def consume_refresh_token(db: Session, jti: str) -> RefreshToken | None:
    record = (
        db.query(RefreshToken)
        .filter(RefreshToken.jti_hash == hash_token(jti), RefreshToken.revoked.is_(False))
        .first()
    )
    if record is None:
        return None
    record.revoked = True
    db.commit()
    return record


def get_refresh_by_jti(db: Session, jti: str) -> RefreshToken | None:
    return db.query(RefreshToken).filter(RefreshToken.jti_hash == hash_token(jti)).first()


def revoke_refresh_family(db: Session, family_id: str) -> None:
    db.query(RefreshToken).filter(RefreshToken.family_id == family_id).update({"revoked": True})
    db.commit()


def revoke_all_refresh_for_user(db: Session, user_id: int) -> None:
    db.query(RefreshToken).filter(RefreshToken.user_id == user_id).update({"revoked": True})
    db.commit()


def bump_token_version(db: Session, user: User) -> None:
    user.token_version += 1
    db.commit()


def create_email_verification_token(db: Session, user_id: int, token: str) -> None:
    db.add(
        EmailVerificationToken(
            user_id=user_id,
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
    )
    db.commit()


def get_valid_email_verification_token(db: Session, token: str) -> EmailVerificationToken | None:
    record = (
        db.query(EmailVerificationToken)
        .filter(
            EmailVerificationToken.token_hash == hash_token(token),
            EmailVerificationToken.used_at.is_(None),
            EmailVerificationToken.expires_at > datetime.now(UTC),
        )
        .first()
    )
    return record


def mark_email_verified(db: Session, user: User, record: EmailVerificationToken) -> None:
    user.email_verified = True
    record.used_at = datetime.now(UTC)
    db.commit()


def write_audit(
    db: Session,
    *,
    action: str,
    user_id: int | None = None,
    details: dict | None = None,
    ip: str | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            details=json.dumps(details or {}, ensure_ascii=False),
            ip=ip,
        )
    )
    db.commit()


def list_audit_logs(
    db: Session,
    *,
    limit: int = 100,
    offset: int = 0,
    exclude_actions: tuple[str, ...] | None = None,
) -> list[AuditLog]:
    q = db.query(AuditLog)
    if exclude_actions:
        q = q.filter(AuditLog.action.notin_(exclude_actions))
    return q.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit).all()


def list_user_audit_logs(
    db: Session,
    user_id: int,
    *,
    action: str,
    limit: int = 50,
) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.user_id == user_id, AuditLog.action == action)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .all()
    )


def list_user_audit_by_actions(
    db: Session,
    user_id: int,
    *,
    actions: tuple[str, ...],
    limit: int = 100,
) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.user_id == user_id, AuditLog.action.in_(actions))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .all()
    )


def count_user_audit_by_actions(db: Session, user_id: int, *, actions: tuple[str, ...]) -> int:
    return (
        db.query(AuditLog)
        .filter(AuditLog.user_id == user_id, AuditLog.action.in_(actions))
        .count()
    )


def count_user_audit_logs(db: Session, user_id: int, *, action: str) -> int:
    return (
        db.query(AuditLog)
        .filter(AuditLog.user_id == user_id, AuditLog.action == action)
        .count()
    )


def create_generation_job(
    db: Session,
    *,
    user_id: int | None,
    summary_date: date,
    report_ids: list[str],
) -> GenerationJob:
    job = GenerationJob(
        id=str(uuid.uuid4()),
        user_id=user_id,
        summary_date=summary_date,
        report_ids=json.dumps(report_ids),
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_generation_job(db: Session, job_id: str) -> GenerationJob | None:
    return db.query(GenerationJob).filter(GenerationJob.id == job_id).first()


def list_generation_jobs(db: Session, *, limit: int = 50) -> list[GenerationJob]:
    return (
        db.query(GenerationJob)
        .order_by(GenerationJob.created_at.desc())
        .limit(limit)
        .all()
    )


def list_generation_jobs_for_user(
    db: Session,
    user_id: int,
    *,
    limit: int = 50,
) -> list[GenerationJob]:
    return (
        db.query(GenerationJob)
        .filter(GenerationJob.user_id == user_id)
        .order_by(GenerationJob.created_at.desc())
        .limit(limit)
        .all()
    )


def count_generation_jobs_for_user(db: Session, user_id: int) -> int:
    return db.query(GenerationJob).filter(GenerationJob.user_id == user_id).count()


def update_generation_job(db: Session, job: GenerationJob, **fields) -> GenerationJob:
    for k, v in fields.items():
        setattr(job, k, v)
    db.commit()
    db.refresh(job)
    return job


def get_schedule_config(db: Session) -> dict:
    from app.application.schedule_config import DEFAULT_SCHEDULE, normalize_schedule_config

    row = db.query(ScheduleConfig).first()
    if row is None:
        return DEFAULT_SCHEDULE.copy()
    try:
        raw = json.loads(row.config_json)
    except json.JSONDecodeError:
        return DEFAULT_SCHEDULE.copy()
    try:
        return normalize_schedule_config(raw)
    except ValueError:
        return DEFAULT_SCHEDULE.copy()


def save_schedule_config(db: Session, config: dict) -> dict:
    from app.application.schedule_config import normalize_schedule_config

    normalized = normalize_schedule_config(config)
    row = db.query(ScheduleConfig).first()
    payload = json.dumps(normalized, ensure_ascii=False)
    if row is None:
        row = ScheduleConfig(config_json=payload)
        db.add(row)
    else:
        row.config_json = payload
    db.commit()
    return normalized


def get_schedule_run_state(db: Session, slot: str) -> ScheduleRunState | None:
    return db.query(ScheduleRunState).filter(ScheduleRunState.slot == slot).first()


def mark_schedule_enqueued(db: Session, slot: str, on_date: date) -> None:
    row = get_schedule_run_state(db, slot)
    if row is None:
        row = ScheduleRunState(slot=slot, last_enqueued_on=on_date)
        db.add(row)
    else:
        row.last_enqueued_on = on_date
    db.commit()
