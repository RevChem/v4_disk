"""Лента действий пользователей и логов по сводкам."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.adapters.inbound.api.deps import require_generate_access
from app.application.activity_log import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    build_activity_page,
    job_detail,
)
from app.database import get_db
from app.models import User

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("/feed")
def activity_feed(
    tab: str | None = Query(default=None, max_length=80, description="id вкладки; по умолчанию первая доступная"),
    page: int = Query(default=1, ge=1, description="номер страницы, с 1"),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    user_id: str | None = Query(default=None, max_length=16, description="id пользователя или «system»"),
    refresh: bool = Query(default=False, description="сбросить кэш ошибок/сезонов"),
    db: Session = Depends(get_db),
    user: User = Depends(require_generate_access),
):
    return build_activity_page(
        db,
        viewer=user,
        tab=tab,
        page=page,
        page_size=page_size,
        user_filter=user_id,
        refresh=refresh,
    )


@router.get("/jobs/{job_id}")
def activity_job_detail(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_generate_access),
):
    detail = job_detail(db, job_id, viewer=user)
    if detail is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    return detail
