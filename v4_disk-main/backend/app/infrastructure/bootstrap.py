"""Стартовая инициализация БД и bootstrap-разработчик."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.adapters.outbound.auth.security import hash_password
from app.adapters.outbound.persistence import repositories as repo
from app.config import settings
from app.database import Base, SessionLocal, engine, run_startup_schema_fixes
from app.infrastructure.logging import get_logger
from app.models import ScheduleConfig, User

log = get_logger(__name__)


def _migrate_roles(db: Session) -> None:
    """sitcentr → user; bootstrap admin → developer."""
    changed = False
    for user in db.query(User).filter(User.role == "sitcentr").all():
        user.role = "user"
        changed = True
    bootstrap = repo.get_user_by_username(db, "admin")
    if bootstrap is not None:
        bootstrap.username = "developer"
        bootstrap.email = settings.bootstrap_developer_email
        bootstrap.password_hash = hash_password(settings.bootstrap_developer_password)
        bootstrap.role = "developer"
        bootstrap.email_verified = True
        bootstrap.is_active = True
        changed = True
        log.info(
            "bootstrap.developer_migrated",
            user_id=bootstrap.id,
            username=bootstrap.username,
            email=bootstrap.email,
        )
    if changed:
        db.commit()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    run_startup_schema_fixes()
    db = SessionLocal()
    try:
        if db.query(ScheduleConfig).count() == 0:
            from app.application.schedule_config import DEFAULT_SCHEDULE

            repo.save_schedule_config(db, DEFAULT_SCHEDULE)
        _LEGACY_BOOTSTRAP_EMAILS = {"admin@local", "admin@local.dev"}

        def _fix_legacy_bootstrap(user) -> None:
            user.username = settings.bootstrap_developer_username
            user.email = settings.bootstrap_developer_email
            user.password_hash = hash_password(settings.bootstrap_developer_password)
            user.email_verified = True
            user.role = "developer"
            user.is_active = True
            db.commit()
            log.info("bootstrap.developer_migrated", user_id=user.id, email=user.email)

        for legacy_email in _LEGACY_BOOTSTRAP_EMAILS:
            legacy = repo.get_user_by_email(db, legacy_email)
            if legacy is not None:
                _fix_legacy_bootstrap(legacy)
        dev_user = repo.get_user_by_username(db, settings.bootstrap_developer_username)
        if dev_user is not None and dev_user.email in _LEGACY_BOOTSTRAP_EMAILS:
            _fix_legacy_bootstrap(dev_user)
        if repo.count_users(db) == 0:
            user = repo.create_user(
                db,
                username=settings.bootstrap_developer_username,
                email=settings.bootstrap_developer_email,
                password_hash=hash_password(settings.bootstrap_developer_password),
                role="developer",
                email_verified=True,
            )
            log.info(
                "bootstrap.developer_created",
                user_id=user.id,
                username=user.username,
                email=user.email,
            )
        _migrate_roles(db)
    finally:
        db.close()
