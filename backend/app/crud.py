from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Summary, User


def count_summaries_by_date(db: Session, target_date: date) -> int:
    stmt = (
        select(func.count(Summary.id))
        .where(Summary.summary_date == target_date)
        .where(Summary.visible.is_(True))
    )
    return db.scalar(stmt) or 0


def list_summaries_by_date(db: Session, target_date: date, *, visible_only: bool = True) -> list[Summary]:
    # Показываем записи в естественном порядке (1, 2, 3...) по id.
    stmt = select(Summary).where(Summary.summary_date == target_date)
    if visible_only:
        stmt = stmt.where(Summary.visible.is_(True))
    stmt = stmt.order_by(Summary.id.asc())
    return list(db.scalars(stmt).all())


def get_visible_summary_by_title(db: Session, summary_date: date, title: str) -> Summary | None:
    stmt = (
        select(Summary)
        .where(
            Summary.summary_date == summary_date,
            Summary.title == title,
            Summary.visible.is_(True),
        )
        .order_by(Summary.id.desc())
        .limit(1)
    )
    return db.scalar(stmt)


def create_summary(
    db: Session,
    title: str,
    summary_date: date,
    file_path: str,
    *,
    visible: bool = True,
) -> Summary:
    item = Summary(title=title, summary_date=summary_date, file_path=file_path, visible=visible)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def list_available_dates_with_counts(db: Session) -> list[tuple[date, int]]:
    stmt = (
        select(Summary.summary_date, func.count(Summary.id))
        .where(Summary.visible.is_(True))
        .group_by(Summary.summary_date)
        .order_by(Summary.summary_date.desc())
    )
    return [(d, c) for d, c in db.execute(stmt).all()]


def get_user_by_username(db: Session, username: str) -> User | None:
    uname = str(username or "").strip()
    if not uname:
        return None
    stmt = select(User).where(User.username == uname)
    return db.scalar(stmt)


def list_users(db: Session) -> list[User]:
    stmt = select(User).order_by(User.id.asc())
    return list(db.scalars(stmt).all())


def create_user(db: Session, *, username: str, password_hash: str, role: str, is_active: bool = True) -> User:
    item = User(
        username=str(username).strip(),
        password_hash=str(password_hash),
        role=str(role).strip(),
        is_active=bool(is_active),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item
