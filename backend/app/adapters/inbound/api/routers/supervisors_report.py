"""Кнопка «Отсутствие данных по топливу»: задания машин за период, только роль «Разработчик»."""
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.adapters.inbound.api.deps import require_developer_access
from app.application.supervisors_report import SupervisorsReportError, build_excel, list_work_groups
from app.models import User

router = APIRouter(prefix="/supervisors-report", tags=["supervisors-report"])


class SupervisorsReportIn(BaseModel):
    start: datetime
    end: datetime
    work_group_ids: list[int] = Field(default_factory=list)


@router.get("/work-groups")
def get_work_groups(_: User = Depends(require_developer_access)) -> dict:
    try:
        groups = list_work_groups()
    except SupervisorsReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Cropwise не ответил.") from exc
    return {"items": groups}


@router.post("/excel")
def post_excel(body: SupervisorsReportIn, _: User = Depends(require_developer_access)) -> Response:
    try:
        payload, filename = build_excel(body.start, body.end, body.work_group_ids)
    except SupervisorsReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Cropwise не ответил.") from exc
    disposition = f"attachment; filename=\"otchet.xlsx\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )
