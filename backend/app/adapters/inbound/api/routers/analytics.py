"""Аналитика по хранилищу заданий (store.sqlite): темп, техника, покрытие, культуры, качество, правки задним числом."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from app.adapters.inbound.api.deps import get_current_active_user, require_plans_access
from app.legacy.analytics import module
from app.legacy.analytics_hub import run_options, run_query
from app.models import User

# Под /summaries/* — nginx уже проксирует (отдельный /analytics часто отдаёт index.html SPA).
router = APIRouter(prefix="/summaries/analytics", tags=["analytics"])


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=f"Хранилище заданий недоступно: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _range(date_from: str | None, date_to: str | None, default_days: int = 60) -> tuple[str, str]:
    today = date.today()
    return (date_from or (today - timedelta(days=default_days)).isoformat(),
            date_to or (today + timedelta(days=1)).isoformat())


@router.get("/overview")
def overview(_: User = Depends(get_current_active_user)):
    return _run(module().overview)


@router.get("/pace")
def pace(date_from: str | None = None, date_to: str | None = None,
         groups: str = Query("", description="Группы работ через «|»; пусто — все"),
         _: User = Depends(get_current_active_user)):
    a, b = _range(date_from, date_to)
    gl = [g for g in groups.split("|") if g.strip()] or None
    return _run(module().pace, a, b, gl)


@router.get("/machines")
def machines(group: str, date_from: str | None = None, date_to: str | None = None,
             _: User = Depends(get_current_active_user)):
    a, b = _range(date_from, date_to)
    return _run(module().machines, group, a, b)


@router.get("/coverage")
def coverage(date_from: str | None = None, date_to: str | None = None, _: User = Depends(get_current_active_user)):
    a, b = _range(date_from, date_to, 400)
    return _run(module().coverage, a, b)


@router.get("/fields-missing")
def fields_missing(group: str, done_in: str | None = None, date_from: str | None = None,
                   date_to: str | None = None, _: User = Depends(get_current_active_user)):
    a, b = _range(date_from, date_to, 400)
    return _run(module().fields_missing, group, a, b, done_in or None)


@router.get("/crops")
def crops(group: str | None = None, date_from: str | None = None, date_to: str | None = None,
          _: User = Depends(get_current_active_user)):
    a, b = _range(date_from, date_to)
    return _run(module().crops, group or None, a, b)


@router.get("/quality")
def quality(_: User = Depends(get_current_active_user)):
    return _run(module().quality)


@router.get("/restatements")
def restatements(day_from: str | None = None, day_to: str | None = None, _: User = Depends(get_current_active_user)):
    a, b = _range(day_from, day_to, 30)
    return _run(module().restatements, a, b)


@router.get("/changes")
def changes(snap_a: str, snap_b: str, min_abs_ha: float = 0.5, _: User = Depends(get_current_active_user)):
    return _run(module().changes_between, snap_a, snap_b, min_abs_ha)


@router.get("/season-compare")
def season_compare(group: str | None = None, year_a: int | None = None, year_b: int | None = None,
                   _: User = Depends(get_current_active_user)):
    y = date.today().year
    return _run(module().season_compare, group or None, year_a or y - 1, year_b or y)


def _hub_range(date_from: str | None, date_to: str | None, default_days: int = 30) -> tuple[str, str]:
    """Даты проверок — включительные, как в полях ввода."""
    today = date.today()
    return (date_from or (today - timedelta(days=default_days)).isoformat(), date_to or today.isoformat())


@router.get("/hub/options/{kind}")
def hub_options(kind: str, date_from: str | None = None, date_to: str | None = None,
                _: User = Depends(require_plans_access)):
    """Списки для фильтров проверки: machines | fields | drivers."""
    a, b = _hub_range(date_from, date_to)
    return {"items": _run(run_options, kind, a, b)}


@router.get("/hub/{query_id}")
def hub_query(query_id: str, date_from: str | None = None, date_to: str | None = None,
              group: str | None = None, driver_id: int | None = None, machine_id: int | None = None,
              field_id: int | None = None, threshold: float | None = Query(None, gt=0),
              _: User = Depends(require_plans_access)):
    """Один срез проверки в едином табличном формате."""
    a, b = _hub_range(date_from, date_to)
    return _run(run_query, query_id, date_from=a, date_to=b, group=group or None, driver_id=driver_id,
                machine_id=machine_id, field_id=field_id, threshold=threshold)
