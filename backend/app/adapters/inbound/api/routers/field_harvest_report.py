"""Кнопка «Валовый сбор»: поле, гектары, сорт и физический вес за сезон."""
from __future__ import annotations

from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.adapters.inbound.api.deps import require_developer_access
from app.application.field_harvest_report import FieldHarvestError, build_excel
from app.models import User

router = APIRouter(prefix="/field-harvest", tags=["field-harvest"])


class FieldHarvestIn(BaseModel):
    year: int
    firms: list[str]


@router.post("/excel")
def post_excel(body: FieldHarvestIn, _: User = Depends(require_developer_access)) -> Response:
    try:
        payload, filename = build_excel(body.year, body.firms)
    except FieldHarvestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Cropwise не ответил.") from exc
    disposition = f"attachment; filename=\"valovyi.xlsx\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )
