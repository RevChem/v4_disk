"""Plans editor API."""
from __future__ import annotations

from datetime import date

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.adapters.inbound.api.deps import get_client_ip, require_plans_access
from app.application.audit_activity import record_activity
from app.database import get_db
from app.infrastructure.logging import get_logger
from app.models import User
from app.legacy.plans_edit_lock import (
    heartbeat_plans_presence,
    release_plans_presence,
    token_is_valid,
)
from app.legacy.plans_editor import (
    WorkTypesUnavailableError,
    fetch_work_types_catalog,
    get_editor_state,
    set_plan_value,
    set_work_groups,
    sync_hardcoded_plan_to_file,
)
from app.legacy.season_meta import set_calendar_seasons, set_season_range_override
from app.schemas import (
    PlansEditPresenceIn,
    PlansEditPresenceOut,
    PlansEditorCalendarSeasonsUpdateIn,
    PlansEditorOut,
    PlansEditorSeasonRangeUpdateIn,
    PlansEditorValueUpdateIn,
    PlansEditorWorkGroupsUpdateIn,
    PlansEditorWorkTypesOut,
)

router = APIRouter(prefix="/plans-editor", tags=["plans"])
log = get_logger(__name__)


def _audit(db: Session, request: Request, user: User, action: str, **details: Any) -> None:
    """Запись в журнал не должна ломать уже применённую правку."""
    try:
        record_activity(db, action=action, user_id=user.id, ip=get_client_ip(request), **details)
    except Exception:
        db.rollback()
        log.warning("plans.audit_failed", action=action, exc_info=True)


def _effective_before(summary_date: date | None) -> dict[str, Any]:
    try:
        return get_editor_state(summary_date).get("effective") or {}
    except Exception:
        return {}


def _old_plan_value(effective: dict[str, Any], section_key: str, keys: list[str]) -> Any:
    node: Any = effective.get(section_key)
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    return None if isinstance(node, dict) else node


def _group_names(effective: dict[str, Any]) -> list[str]:
    return [str(g.get("name")) for g in (effective.get("WORK_GROUPS") or []) if isinstance(g, dict)]


def _holder_name(user: User) -> str:
    return (
        (user.full_name or "").strip()
        or (user.username or "").strip()
        or (user.email or "").strip()
        or f"пользователь #{user.id}"
    )


@router.post("/presence", response_model=PlansEditPresenceOut)
def beat_plans_edit_presence(
    payload: PlansEditPresenceIn,
    user: User = Depends(require_plans_access),
):
    token = payload.token.strip()
    if not token_is_valid(token):
        raise HTTPException(status_code=400, detail="Некорректный ключ сессии редактора.")
    return heartbeat_plans_presence(user.id, _holder_name(user), token)


@router.post("/presence/release", response_model=PlansEditPresenceOut)
def drop_plans_edit_presence(
    payload: PlansEditPresenceIn,
    user: User = Depends(require_plans_access),
):
    token = payload.token.strip()
    if not token_is_valid(token):
        raise HTTPException(status_code=400, detail="Некорректный ключ сессии редактора.")
    return release_plans_presence(user.id, token)


@router.get("", response_model=PlansEditorOut)
def get_plans_editor(
    summary_date: date | None = Query(default=None),
    _: object = Depends(require_plans_access),
):
    try:
        return get_editor_state(summary_date)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/sync-py", response_model=PlansEditorOut)
def sync_plans_py(
    request: Request,
    summary_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_plans_access),
):
    try:
        state = sync_hardcoded_plan_to_file(summary_date)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _audit(db, request, user, "plans.sync_py", note=state.get("plans_py_note"))
    return state


@router.put("/value", response_model=PlansEditorOut)
def update_plans_editor_value(
    payload: PlansEditorValueUpdateIn,
    request: Request,
    summary_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_plans_access),
):
    keys = list(payload.keys)
    old_value = _old_plan_value(_effective_before(summary_date), payload.section_key, keys)
    try:
        state = set_plan_value(payload.section_key, keys, payload.value, report_date=summary_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        db, request, user, "plans.value.update",
        section_key=payload.section_key,
        keys=keys,
        old_value=old_value,
        new_value=payload.value,
        summary_date=summary_date.isoformat() if summary_date else None,
    )
    return state


@router.get("/work-types", response_model=PlansEditorWorkTypesOut)
def get_plans_editor_work_types(_: object = Depends(require_plans_access)):
    try:
        return {"items": fetch_work_types_catalog()}
    except WorkTypesUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("/work-groups", response_model=PlansEditorOut)
def update_plans_editor_work_groups(
    payload: PlansEditorWorkGroupsUpdateIn,
    request: Request,
    summary_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_plans_access),
):
    old_names = _group_names(_effective_before(summary_date))
    groups = [g.model_dump() for g in payload.groups]
    try:
        state = set_work_groups(groups, report_date=summary_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    new_names = [str(g.get("name")) for g in groups]
    _audit(
        db, request, user, "plans.work_groups.update",
        added=[n for n in new_names if n not in old_names],
        removed=[n for n in old_names if n not in new_names],
        groups_count=len(new_names),
    )
    return state


@router.put("/calendar-seasons", response_model=PlansEditorOut)
def update_plans_editor_calendar_seasons(
    payload: PlansEditorCalendarSeasonsUpdateIn,
    request: Request,
    summary_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_plans_access),
):
    try:
        cal = {
            str(k): v.model_dump()
            for k, v in (payload.calendar_seasons or {}).items()
        }
        set_calendar_seasons(cal, report_date=summary_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(db, request, user, "plans.calendar_seasons.update", years=sorted(cal.keys()))
    return get_editor_state(summary_date)


@router.put("/season-range", response_model=PlansEditorOut)
def update_plans_editor_season_range(
    payload: PlansEditorSeasonRangeUpdateIn,
    request: Request,
    summary_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_plans_access),
):
    try:
        set_season_range_override(
            payload.group_name,
            period_start=payload.period_start,
            period_end=payload.period_end,
            season_year=payload.season_year,
            season_years=list(payload.season_years) if payload.season_years is not None else None,
            year_ranges=(
                {str(k): v.model_dump() for k, v in payload.year_ranges.items()}
                if payload.year_ranges is not None
                else None
            ),
            clear=bool(payload.clear),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        db, request, user, "plans.season_range.update",
        group_name=payload.group_name,
        period_start=payload.period_start,
        period_end=payload.period_end,
        season_year=payload.season_year,
        clear=bool(payload.clear),
    )
    return get_editor_state(summary_date)
