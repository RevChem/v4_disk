# cropwise/violation_status_model.py
#
# Таблица хранит статус обработки строки нарушения панели «⚠ Нарушения»
# (кнопка «В работу» / «В работе» / «Отработана»).
#
# Раньше статус хранился в localStorage браузера — его видел только тот,
# кто поставил отметку. Теперь статус хранится в БД и виден всем
# пользователям сайта (upsert по уникальному ключу строки).
#
# key формируется на фронтенде из префикса проверки + стабильного идентификатора
# строки (task_id, machine_id + период для check6, и т.п.) — например:
# 'c1_482910_2026-09-10T17:30:00+03:00', 'c6_12345_2026-09-09T06:00:00+03:00',
# 'ovlp_117744_117745', 'nse_446283_2026-09-10T05:59:00+03:00'.
#
# ВАЖНО: раньше в ключ подмешивался ещё и позиционный индекс строки в массиве
# (например 'c1_3_482910'). Это ломало синхронизацию статуса между пользователями:
# индекс зависел от клиентских фильтров/чекбоксов (текстовый фильтр на вкладке
# «Нестандартное окончание», чекбоксы статусов на check12), поэтому одна и та же
# строка получала разный ключ у разных пользователей (или даже у одного и того же
# при смене фильтра) — статус, поставленный одним пользователем, не находился
# другим. Индекс убрали, ключ строится только из стабильных полей данных.

from datetime import datetime
from sqlalchemy import Integer, String, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class ViolationStatus(Base):
    """Статус строки нарушения: 0 = В работу, 1 = В работе, 2 = Отработана."""

    __tablename__ = "violation_statuses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Ключ строки, который формирует фронтенд (см. _vStatus в app.js)
    key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)

    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Кто последний раз менял статус (для возможной диагностики/истории)
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.now(), nullable=True,
    )
    # ВАЖНО: onupdate=func.now() срабатывает только при ORM-обновлении (session.add + commit).
    # Текущий код пишет статус через Core-уровневый INSERT...ON CONFLICT DO UPDATE
    # (см. violations_router.set_violation_row_status), поэтому там updated_at
    # указывается явно в .values()/.set_=. Не убирайте это явное значение при рефакторинге.

    def __repr__(self):
        return f"<ViolationStatus(key={self.key!r}, status={self.status})>"
