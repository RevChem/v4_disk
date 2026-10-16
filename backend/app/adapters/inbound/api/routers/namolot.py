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


@router.get("/namolot/brief")
def get_namolot_brief(
    report_date: date = Query(..., alias="report_date"),
    firm: str = Query(...),
    crop: str = Query(...),
    _: User = Depends(require_developer_access),
):
    """Тонны одной культуры и агрофирмы: смена 06:00–05:59 и сезон до 05:59."""
    return _call(report_date, report_date, lambda _begin, _end: namolot_view.build_brief(report_date, firm, crop))


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
    date_begin: date | None = Query(None, alias="date_begin"),
    date_end: date | None = Query(None, alias="date_end"),
    report_date: date | None = Query(None, alias="report_date"),
    firm: str | None = Query(None),
    crop: str | None = Query(None),
    _: User = Depends(require_developer_access),
):
    """Полный период либо срез одной культуры и агрофирмы за сезон до 05:59."""
    if report_date and firm and crop:
        payload = _call(
            report_date,
            report_date,
            lambda _begin, _end: namolot_view.build_brief_excel(report_date, firm, crop),
        )
        filename = f"namolot_{firm}_{crop}_{report_date.isoformat()}.xlsx".replace(" ", "_")
    else:
        if date_begin is None or date_end is None:
            raise HTTPException(status_code=400, detail="Нужны date_begin и date_end либо report_date, агрофирма и культура")
        payload = _call(date_begin, date_end, namolot_view.build_excel)
        filename = f"namolot_{date_begin.isoformat()}_{date_end.isoformat()}.xlsx"
    disposition = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )
