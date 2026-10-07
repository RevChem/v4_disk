"""API редактора подписей «ВИД РАБОТ» в PDF."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.adapters.inbound.api.deps import get_client_ip, require_plans_access
from app.application.audit_activity import record_activity
from app.database import get_db
from app.infrastructure.logging import get_logger
from app.models import User
from app.legacy.pdf_header_labels import (
    get_editor_state,
    reset_pdf_header_labels,
    strip_work_line_group_numbers,
    update_work_line_map,
)
from app.schemas import PdfHeaderLabelsOut, PdfHeaderLabelsUpdateIn

router = APIRouter(prefix="/pdf-header-labels", tags=["pdf-header-labels"])
log = get_logger(__name__)


def _audit(db: Session, request: Request, user: User, action: str, **details) -> None:
    try:
        record_activity(db, action=action, user_id=user.id, ip=get_client_ip(request), **details)
    except Exception:
        db.rollback()
        log.warning("pdf_header_labels.audit_failed", action=action, exc_info=True)


@router.get("", response_model=PdfHeaderLabelsOut)
def get_pdf_header_labels(_: object = Depends(require_plans_access)):
    return get_editor_state()


@router.put("", response_model=PdfHeaderLabelsOut)
def save_pdf_header_labels(
    payload: PdfHeaderLabelsUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_plans_access),
):
    updates = {item.default_work_line: item.work_line for item in payload.items}
    try:
        state = update_work_line_map(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(db, request, user, "pdf_header_labels.update", items_count=len(updates))
    return state


@router.post("/reset", response_model=PdfHeaderLabelsOut)
def reset_pdf_header_labels_api(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_plans_access),
):
    reset_pdf_header_labels()
    _audit(db, request, user, "pdf_header_labels.reset")
    return get_editor_state()


@router.post("/strip-numbers", response_model=PdfHeaderLabelsOut)
def strip_pdf_header_group_numbers(
    payload: PdfHeaderLabelsUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_plans_access),
):
    """Подставить вариант без номеров групп (2.11 …) для выбранных строк."""
    updates: dict[str, str | None] = {}
    for item in payload.items:
        default_line = str(item.default_work_line or "").strip()
        if not default_line:
            continue
        stripped = strip_work_line_group_numbers(default_line)
        updates[default_line] = stripped if stripped != default_line else None
    try:
        state = update_work_line_map(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(db, request, user, "pdf_header_labels.strip_numbers", items_count=len(updates))
    return state
