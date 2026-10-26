"""Кнопка Namolot: живой отчёт 1С за период."""
from __future__ import annotations

from datetime import date
from urllib.parse import quote

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.adapters.inbound.api.deps import require_generate_access
from app.application import namolot_view, plate_mappings
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


@router.get("/namolot/season")
def get_namolot_season(
    report_date: date = Query(..., alias="report_date"),
    _: User = Depends(require_generate_access),
):
    """Весь сезон до 05:59 даты сводки. Агрофирма и культура не обязательны."""
    return _call(report_date, report_date, lambda _begin, _end: namolot_view.build_season(report_date))


@router.get("/namolot/brief")
def get_namolot_brief(
    report_date: date = Query(..., alias="report_date"),
    firm: str = Query(...),
    crop: str = Query(...),
    _: User = Depends(require_generate_access),
):
    """Тонны одной культуры и агрофирмы: смена 06:00–05:59 и сезон до 05:59."""
    return _call(report_date, report_date, lambda _begin, _end: namolot_view.build_brief(report_date, firm, crop))


@router.get("/namolot")
def get_namolot_view(
    date_begin: date = Query(..., alias="date_begin"),
    date_end: date = Query(..., alias="date_end"),
    _: User = Depends(require_generate_access),
):
    """Тонны по агрофирме, организации и культуре. Источник — живой Namolot, не кэш сводок."""
    return _call(date_begin, date_end, namolot_view.build_view)


class PlateMappingIn(BaseModel):
    namolot: str = ""
    cropwise: str = ""


class PlateMappingsIn(BaseModel):
    items: list[PlateMappingIn] = Field(default_factory=list, max_length=300)


@router.get("/plate-mappings")
def get_plate_mappings(_: User = Depends(require_generate_access)):
    """Пары госномеров. Пока сайт не сохранял список, отдаётся файл по умолчанию."""
    try:
        return {"items": plate_mappings.load_items()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/plate-mappings")
def put_plate_mappings(body: PlateMappingsIn, _: User = Depends(require_generate_access)):
    """Сохранить пары. Следующая сводка 14 читает этот файл."""
    try:
        items = plate_mappings.save_items([item.model_dump() for item in body.items])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": items}


class NamolotTableExcelIn(BaseModel):
    mode: str = "rows"
    rows: list[dict] = Field(default_factory=list, max_length=50_000)


def _excel_response(payload: bytes, filename: str) -> Response:
    # filename= только ASCII: заголовок HTTP не держит кириллицу и даёт 500.
    ascii_name = "namolot.xlsx"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )


@router.post("/namolot/excel")
def post_namolot_excel(body: NamolotTableExcelIn, _: User = Depends(require_generate_access)):
    """Уже отфильтрованная таблица с экрана: записи, госномера или фамилии."""
    mode = body.mode if body.mode in {"rows", "plates", "surnames"} else "rows"
    payload = namolot_view.table_excel(mode, body.rows)
    return _excel_response(payload, "namolot.xlsx")


@router.get("/namolot/excel")
def get_namolot_excel(
    date_begin: date | None = Query(None, alias="date_begin"),
    date_end: date | None = Query(None, alias="date_end"),
    report_date: date | None = Query(None, alias="report_date"),
    firm: str | None = Query(None),
    crop: str | None = Query(None),
    _: User = Depends(require_generate_access),
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
    return _excel_response(payload, filename)
