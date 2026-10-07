"""Регистрация «admin» / admin@local.dev не должна давать роль developer при перезапуске."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.infrastructure import bootstrap
from app.models import User


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _add(db, username: str, email: str, role: str = "user") -> User:
    user = User(username=username, email=email, password_hash="hash-original", role=role)
    db.add(user)
    db.commit()
    return user


def test_registered_admin_is_not_promoted_when_developer_exists(db) -> None:
    _add(db, "boss", "boss@example.com", role="developer")
    attacker = _add(db, "admin", "admin@local.dev")
    assert bootstrap._migrate_legacy_bootstrap_admin(db) is False
    db.refresh(attacker)
    assert attacker.role == "user"
    assert attacker.password_hash == "hash-original"
    assert attacker.username == "admin"


def test_registered_admin_is_not_promoted_when_other_users_exist(db) -> None:
    _add(db, "someone", "someone@example.com")
    attacker = _add(db, "admin", "admin@local")
    assert bootstrap._migrate_legacy_bootstrap_admin(db) is False
    db.refresh(attacker)
    assert attacker.role == "user"


def test_only_legacy_admin_without_developer_is_migrated_without_password_reset(db) -> None:
    legacy = _add(db, "admin", "admin@local")
    assert bootstrap._migrate_legacy_bootstrap_admin(db) is True
    db.refresh(legacy)
    assert legacy.role == "developer"
    assert legacy.password_hash == "hash-original"


def test_repeated_startup_does_not_crash_or_change_anything(db) -> None:
    dev = _add(db, "developer", "dev@example.com", role="developer")
    admin = _add(db, "admin", "admin@local.dev")
    for _ in range(2):
        bootstrap._migrate_legacy_bootstrap_admin(db)
        bootstrap._migrate_roles(db)
    db.refresh(dev)
    db.refresh(admin)
    assert (dev.role, admin.role) == ("developer", "user")


def test_sitcentr_becomes_user_only(db) -> None:
    u = _add(db, "old", "old@example.com", role="sitcentr")
    bootstrap._migrate_roles(db)
    db.refresh(u)
    assert u.role == "user"
