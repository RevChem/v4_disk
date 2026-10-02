"""Лента действий пользователей и логов по сводкам."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.adapters.inbound.api.deps import require_generate_access
from app.application.activity_log import build_activity_feed, job_detail
from app.database import get_db
from app.models import User

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("/feed")
def activity_feed(
    limit: int = Query(default=80, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_generate_access),
):
    return build_activity_feed(db, viewer=user, limit=limit)


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
