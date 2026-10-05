# cropwise/assignment_model.py
#
# Таблица хранит текущее назначение пользователя из Cropwise:
# какая машина и задача сейчас за ним закреплены.
# Обновляется Celery-задачей каждые 10 минут.
# Одна запись на пользователя (upsert по user_id).

from datetime import datetime
from sqlalchemy import Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class UserAssignment(Base):
    """Текущее назначение пользователя из Cropwise (машина + задача)."""

    __tablename__ = "user_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Связь с пользователем (user.id == Cropwise user id)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,   # одна запись на пользователя
        index=True,
    )

    # Данные из Cropwise — все nullable: назначения может не быть
    registration_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    machine_subtype:     Mapped[str | None] = mapped_column(String(50), nullable=True)
    machine_subtype_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    work_type:           Mapped[str | None] = mapped_column(String(100), nullable=True)
    task_label:          Mapped[str | None] = mapped_column(String(255), nullable=True)
    machine_task_id:     Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Когда последний раз Cropwise вернул данные для этого пользователя
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self):
        return (
            f"<UserAssignment(user_id={self.user_id}, "
            f"reg={self.registration_number}, "
            f"task={self.machine_task_id})>"
        )
