"""Schedule configuration API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.adapters.inbound.api.deps import get_client_ip, require_schedule_access
from app.adapters.outbound.persistence import repositories as repo
from app.application.audit_activity import record_activity
from app.application.schedule_audit import diff_schedule_config
from app.database import get_db
from app.models import User
from app.schemas_auth import ScheduleConfigIn, ScheduleConfigOut

router = APIRouter(prefix="/schedule", tags=["schedule"])


@router.get("", response_model=ScheduleConfigOut)
def get_schedule(db: Session = Depends(get_db), _: User = Depends(require_schedule_access)):
    return ScheduleConfigOut(config=repo.get_schedule_config(db))


@router.put("", response_model=ScheduleConfigOut)
def update_schedule(
    body: ScheduleConfigIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_schedule_access),
):
    before = repo.get_schedule_config(db)
    try:
        config = repo.save_schedule_config(db, body.config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    schedule_changes = diff_schedule_config(before, config)
    record_activity(
        db,
        action="schedule.updated",
        user_id=user.id,
        ip=get_client_ip(request),
        generation_count=len(config.get("generation") or []),
        mail_count=len(config.get("mail") or []),
        schedule_changes=schedule_changes,
    )
    return ScheduleConfigOut(config=config)
