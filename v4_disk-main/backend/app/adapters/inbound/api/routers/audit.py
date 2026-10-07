"""Audit log API."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.adapters.inbound.api.deps import require_admin
from app.adapters.outbound.persistence import repositories as repo
from app.database import get_db
from app.models import User
from app.schemas_auth import AuditLogOut

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=list[AuditLogOut])
def list_audit(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    rows = repo.list_audit_logs(db, limit=limit, offset=offset)
    out: list[AuditLogOut] = []
    for row in rows:
        try:
            details = json.loads(row.details or "{}")
        except json.JSONDecodeError:
            details = {"raw": row.details}
        out.append(
            AuditLogOut(
                id=row.id,
                user_id=row.user_id,
                action=row.action,
                details=details,
                ip=row.ip,
                created_at=row.created_at,
            )
        )
    return out
