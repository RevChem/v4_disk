"""Сводка по работам 2025/2026."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.adapters.inbound.api.deps import (
    get_client_ip,
    require_generate_access,
    require_generate_access_flexible,
)
from app.application.audit_activity import record_activity
from app.config import MISSING_CROPWISE_TOKEN_DETAIL, get_cropwise_token
from app.database import get_db
from app.legacy.works_summary import (
    build_works_excel,
    build_works_excel_via_script,
    build_works_grid_view,
    filter_rows as filter_works_rows,
    generate_cache as generate_works_cache_sync,
    generate_cache_async as generate_works_cache_async,
    load_cache as load_works_cache,
    works_generation_status,
)
from app.models import User
from app.schemas import WorksSummaryGridOut, WorksSummaryStateOut, WorksSummaryTableOut

router = APIRouter(tags=["works-summary"])


def _parse_years(raw: str) -> list[int]:
    years_list: list[int] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            y = int(part)
        except ValueError:
            continue
        if y > 0:
            years_list.append(y)
    return years_list


def _works_state_payload() -> dict:
    cache = load_works_cache()
    status = works_generation_status()
    groups: list[str] = []
    crops: list[str] = []
    firms: list[str] = []
    if cache:
        rows = list(cache.get("rows") or [])
        groups = list(cache.get("groups") or [])
        crops = list(cache.get("crops") or [])
        firms = list(cache.get("firms") or [])
        g_rows = {str(r.get("group") or "").strip() for r in rows if str(r.get("group") or "").strip()}
        c_rows = {str(r.get("crop") or "").strip() for r in rows if str(r.get("crop") or "").strip()}
        f_rows = {str(r.get("firm") or "").strip() for r in rows if str(r.get("firm") or "").strip()}
        groups = sorted(set(groups) | g_rows)
        crops = sorted(set(crops) | c_rows)
        firms = sorted(set(firms) | f_rows)
    state_error = str(status.get("error") or "")
    if not cache and not state_error and not get_cropwise_token():
        state_error = MISSING_CROPWISE_TOKEN_DETAIL
    return {
        "exists": cache is not None,
        "season_year": cache.get("season_year") if cache else None,
        "prev_year": cache.get("prev_year") if cache else None,
        "created_at": cache.get("created_at") if cache else None,
        "groups": groups,
        "crops": crops,
        "firms": firms,
        "running": bool(status.get("running")),
        "error": state_error,
        "queue_job_id": None,
    }


@router.get("/works-summary", response_model=WorksSummaryStateOut)
@router.get("/field-season/works-summary", response_model=WorksSummaryStateOut)
def get_works_summary_state(_: User = Depends(require_generate_access)):
    return _works_state_payload()


@router.post("/works-summary/generate", response_model=WorksSummaryStateOut)
@router.post("/field-season/works-summary/generate", response_model=WorksSummaryStateOut)
def generate_works_summary(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_generate_access),
):
    if not get_cropwise_token():
        raise HTTPException(status_code=400, detail=MISSING_CROPWISE_TOKEN_DETAIL)
    generate_works_cache_async()
    record_activity(db, action="works_summary.generate", user_id=user.id, ip=get_client_ip(request))
    return _works_state_payload()


@router.get("/works-summary/table", response_model=WorksSummaryTableOut)
@router.get("/field-season/works-summary/table", response_model=WorksSummaryTableOut)
def get_works_summary_table(
    years: str = Query(""),
    groups: str = Query(""),
    crops: str = Query(""),
    firms: str = Query(""),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=5000),
    _: User = Depends(require_generate_access),
):
    cache = load_works_cache()
    if not cache:
        if not get_cropwise_token():
            raise HTTPException(status_code=400, detail=MISSING_CROPWISE_TOKEN_DETAIL)
        try:
            cache = generate_works_cache_sync()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Не удалось сформировать сводку по работам: {exc}") from exc
    rows = filter_works_rows(
        cache,
        years=_parse_years(years),
        groups=groups,
        crops=crops,
        firms=firms,
        date_from=date_from,
        date_to=date_to,
    )
    page = rows[offset : offset + limit]
    next_offset = offset + len(page)
    return {
        "rows": page,
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "has_more": next_offset < len(rows),
        "next_offset": next_offset if next_offset < len(rows) else None,
    }


@router.get("/works-summary/grid", response_model=WorksSummaryGridOut)
@router.get("/field-season/works-summary/grid", response_model=WorksSummaryGridOut)
def get_works_summary_grid(
    years: str = Query(""),
    groups: str = Query(""),
    crops: str = Query(""),
    firms: str = Query(""),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    _: User = Depends(require_generate_access),
):
    cache = load_works_cache()
    if not cache:
        if not get_cropwise_token():
            raise HTTPException(status_code=400, detail=MISSING_CROPWISE_TOKEN_DETAIL)
        try:
            cache = generate_works_cache_sync()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Не удалось сформировать сводку по работам: {exc}") from exc
    rows = filter_works_rows(
        cache,
        years=_parse_years(years),
        groups=groups,
        crops=crops,
        firms=firms,
        date_from=date_from,
        date_to=date_to,
    )
    if not rows:
        raise HTTPException(status_code=400, detail="Для выбранных фильтров нет данных.")
    try:
        return build_works_grid_view(
            rows,
            int(cache.get("season_year") or date.today().year),
            int(cache.get("prev_year") or (date.today().year - 1)),
            date_from=date_from,
            date_to=date_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/works-summary/excel")
@router.get("/field-season/works-summary/excel")
def download_works_summary_excel(
    years: str = Query(""),
    groups: str = Query(""),
    crops: str = Query(""),
    firms: str = Query(""),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    _: User = Depends(require_generate_access_flexible),
):
    cache = load_works_cache()
    if not cache:
        try:
            cache = generate_works_cache_sync()
        except Exception:
            cache = None
    if not cache:
        try:
            path, name = build_works_excel_via_script(
                groups=groups,
                crops=crops,
                date_from=date_from,
                date_to=date_to,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"{name}.xlsx",
        )
    rows = filter_works_rows(
        cache,
        years=_parse_years(years),
        groups=groups,
        crops=crops,
        firms=firms,
        date_from=date_from,
        date_to=date_to,
    )
    if not rows:
        try:
            path, name = build_works_excel_via_script(
                groups=groups,
                crops=crops,
                date_from=date_from,
                date_to=date_to,
            )
            return FileResponse(
                path,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename=f"{name}.xlsx",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        path, name = build_works_excel(
            rows,
            int(cache.get("season_year") or date.today().year),
            int(cache.get("prev_year") or (date.today().year - 1)),
            date_from=date_from,
            date_to=date_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{name}.xlsx",
    )
