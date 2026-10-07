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


_LEGACY_BOOTSTRAP_USERNAME = "admin"
_LEGACY_BOOTSTRAP_EMAILS = frozenset({"admin@local", "admin@local.dev"})


def _has_developer(db: Session) -> bool:
    return db.query(User).filter(User.role == "developer").first() is not None


def _migrate_legacy_bootstrap_admin(db: Session) -> bool:
    """Одноразовый перенос старого bootstrap-пользователя admin в developer.

    /register открыта, а «admin» и admin@local(.dev) проходят валидацию, поэтому
    раньше любой зарегистрированный с таким именем или почтой получал роль
    developer и публичный пароль из конфига при следующем старте API. Теперь
    перенос идёт только если в базе нет ни одного developer и этот legacy-аккаунт
    единственный. Пароль не меняется, так что доступ не открывается заново.
    """
    if _has_developer(db) or repo.count_users(db) != 1:
        return False
    only = db.query(User).first()
    if only is None:
        return False
    is_legacy = only.username == _LEGACY_BOOTSTRAP_USERNAME or only.email in _LEGACY_BOOTSTRAP_EMAILS
    if not is_legacy:
        return False
    only.role = "developer"
    only.email_verified = True
    only.is_active = True
    db.commit()
    log.warning("bootstrap.legacy_admin_promoted", user_id=only.id, username=only.username)
    return True


def _migrate_roles(db: Session) -> None:
    """sitcentr → user (без повышения прав)."""
    changed = False
    for user in db.query(User).filter(User.role == "sitcentr").all():
        user.role = "user"
        changed = True
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
        else:
            _migrate_legacy_bootstrap_admin(db)
        _migrate_roles(db)
    finally:
        db.close()
