"""Сводка «Поля с начала сезона»."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.adapters.inbound.api.deps import (
    get_client_ip,
    require_generate_access,
    require_generate_access_flexible,
)
from app.application.audit_activity import record_activity
from app.config import MISSING_CROPWISE_TOKEN_DETAIL, get_cropwise_token
from app.database import get_db
from app.legacy.field_season_summary import (
    _date_sort_key,
    build_field_excel,
    build_field_pdf,
    field_generation_status,
    generate_cache_async,
    load_cache,
)
from app.models import User
from app.schemas import FieldSeasonStateOut, FieldSeasonTableOut
from sqlalchemy.orm import Session

router = APIRouter(prefix="/field-season", tags=["field-season"])


def _parse_field_ids(raw: str | None) -> list[int]:
    out: list[int] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value > 0 and value not in out:
            out.append(value)
    return out


@router.get("", response_model=FieldSeasonStateOut)
def get_field_season_state(_: User = Depends(require_generate_access)):
    cache = load_cache()
    status = field_generation_status()
    return {
        "exists": cache is not None,
        "season_year": cache.get("season_year") if cache else None,
        "created_at": cache.get("created_at") if cache else None,
        "fields": cache.get("fields", []) if cache else [],
        "running": bool(status.get("running")),
        "error": str(status.get("error") or ""),
        "warnings": [str(item) for item in (cache.get("warnings") or [])] if cache else [],
    }


@router.post("/generate", response_model=FieldSeasonStateOut)
def generate_field_season(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_generate_access),
):
    if not get_cropwise_token():
        raise HTTPException(status_code=400, detail=MISSING_CROPWISE_TOKEN_DETAIL)
    generate_cache_async()
    record_activity(db, action="field_season.generate", user_id=user.id, ip=get_client_ip(request))
    cache = load_cache()
    status = field_generation_status()
    return {
        "exists": cache is not None,
        "season_year": cache.get("season_year") if cache else None,
        "created_at": cache.get("created_at") if cache else None,
        "fields": cache.get("fields", []) if cache else [],
        "running": bool(status.get("running")),
        "error": str(status.get("error") or ""),
        "warnings": [str(item) for item in (cache.get("warnings") or [])] if cache else [],
    }


@router.get("/table", response_model=FieldSeasonTableOut)
def get_field_season_table(
    field_id: int = Query(0),
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    _: User = Depends(require_generate_access),
):
    cache = load_cache()
    if not cache:
        raise HTTPException(status_code=404, detail="Сводка по полям ещё не создана.")
    rows_by_field = cache.get("rows_by_field") or {}
    fields = cache.get("fields") or []
    if field_id in (0, None):
        rows: list[dict[str, Any]] = []
        for value in rows_by_field.values():
            if isinstance(value, list):
                rows.extend(value)
        rows = sorted(rows, key=lambda x: (str(x.get("field_name") or ""), str(x.get("date_start") or "")))
        page = rows[offset : offset + limit]
        next_offset = offset + len(page)
        return {
            "field_id": 0,
            "field_name": "Общая",
            "rows": page,
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "has_more": next_offset < len(rows),
            "next_offset": next_offset if next_offset < len(rows) else None,
        }
    field_name = ""
    for item in fields:
        if int(item.get("field_id") or 0) == int(field_id):
            field_name = str(item.get("field_name") or "")
            break
    rows = rows_by_field.get(str(field_id), [])
    if not field_name:
        raise HTTPException(status_code=404, detail="Поле не найдено в готовой сводке.")
    rows = sorted(
        rows,
        key=lambda x: (
            _date_sort_key(x.get("date_start")),
            _date_sort_key(x.get("date_end")),
        ),
    )
    total = len(rows)
    page = rows[offset : offset + limit]
    next_off = offset + len(page)
    return {
        "field_id": int(field_id),
        "field_name": field_name,
        "rows": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": next_off < total,
        "next_offset": next_off if next_off < total else None,
    }


@router.get("/pdf")
def download_field_season_pdf(
    field_id: int = Query(0),
    field_ids: str | None = Query(None),
    _: User = Depends(require_generate_access_flexible),
):
    try:
        path, name = build_field_pdf(field_id=field_id, field_ids=_parse_field_ids(field_ids))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(path, media_type="application/pdf", filename=f"{name}.pdf")


@router.get("/excel")
def download_field_season_excel(
    field_id: int = Query(0),
    field_ids: str | None = Query(None),
    _: User = Depends(require_generate_access_flexible),
):
    try:
        path, name = build_field_excel(field_id=field_id, field_ids=_parse_field_ids(field_ids))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{name}.xlsx",
    )
