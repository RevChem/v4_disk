"""Кнопка Namolot: живой отчёт 1С за период."""
from __future__ import annotations

from datetime import date
from urllib.parse import quote

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.adapters.inbound.api.deps import require_developer_access
from app.application import namolot_view
from app.models import User

router = APIRouter(prefix="/proverka", tags=["proverka"])


def _call(date_begin: date, date_end: date, builder):
    try:
        return builder(date_begin, date_end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail="Namolot API недоступен. Проверьте сеть до 1С и NAMOLOT_PASSWORD.",
        ) from exc


@router.get("/namolot")
def get_namolot_view(
    date_begin: date = Query(..., alias="date_begin"),
    date_end: date = Query(..., alias="date_end"),
    _: User = Depends(require_developer_access),
):
    """Тонны по агрофирме, организации и культуре. Источник — живой Namolot, не кэш сводок."""
    return _call(date_begin, date_end, namolot_view.build_view)


@router.get("/namolot/excel")
def get_namolot_excel(
    date_begin: date = Query(..., alias="date_begin"),
    date_end: date = Query(..., alias="date_end"),
    _: User = Depends(require_developer_access),
):
    """Те же строки, что отдаёт API: колонки как в выгрузке namolot_report."""
    payload = _call(date_begin, date_end, namolot_view.build_excel)
    filename = f"namolot_{date_begin.isoformat()}_{date_end.isoformat()}.xlsx"
    disposition = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )
