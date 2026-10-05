"""
cropwise/svodka2_router.py
==========================
Эндпоинт: «2 Сводка» — компактный отчёт по культурам за произвольный диапазон дат.
POST /api/svodka2  — возвращает Excel-файл.

Тело запроса (JSON):
  {
    "date_from": "2025-06-01",   # включительно
    "date_to":   "2025-07-31"    # включительно
  }

Логика: импортирует функции из 2_svodka.py и запускает их в отдельном потоке
(через asyncio.to_thread), чтобы не блокировать event loop FastAPI.
Файл 2_svodka.py должен лежать рядом: backend/app/cropwise/2_svodka.py.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import sys
import types
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth.dependencies import get_current_admin_user
from app.logging_config import get_logger
from app.users.models import User

logger = get_logger("cropwise.svodka2_router")

router = APIRouter(prefix="/api/svodka2", tags=["2 Сводка"])


# ── Схема запроса ─────────────────────────────────────────────────────────────

class Svodka2Request(BaseModel):
    date_from: str   # YYYY-MM-DD включительно
    date_to:   str   # YYYY-MM-DD включительно


# ── Загрузка модуля 2_svodka.py ───────────────────────────────────────────────

def _load_svodka2_module() -> types.ModuleType:
    """
    Загружает модуль из файла 2_svodka.py, который лежит в той же директории,
    что и этот роутер (backend/app/cropwise/).
    Используем importlib, т.к. имя файла начинается с цифры (не валидный
    Python-идентификатор для обычного import).
    Временно добавляем папку cropwise в sys.path, чтобы внутри 2_svodka.py
    сработал "from plans_1 import *" и другие локальные импорты.
    """
    here = Path(__file__).parent
    script_path = here / "2_svodka.py"
    if not script_path.exists():
        raise FileNotFoundError(
            f"Файл 2_svodka.py не найден по пути: {script_path}. "
            "Скопируйте его в backend/app/cropwise/."
        )
    here_str = str(here)
    path_added = here_str not in sys.path
    if path_added:
        sys.path.insert(0, here_str)
    try:
        spec = importlib.util.spec_from_file_location("svodka2_module", script_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if path_added and here_str in sys.path:
            sys.path.remove(here_str)
    return mod


def _run_svodka2(date_from: str, date_to: str) -> bytes:
    """
    Запускает генерацию отчёта в текущем потоке (вызывается через asyncio.to_thread).
    Переопределяет START_DATE / TODAY в загруженном модуле, затем вызывает build_report().
    Перехватывает wb.save() и возвращает байты xlsx вместо записи на диск.
    """
    import openpyxl

    # Устанавливаем переменные окружения — 2_svodka.py читает их при старте,
    # но модуль уже загружен, поэтому патчим атрибуты напрямую после загрузки.
    os.environ["DATE_FROM"] = date_from
    os.environ["DATE_TO"]   = date_to

    mod = _load_svodka2_module()

    # Патчим OUTPUT_FILE чтобы wb.save() не писал на диск (перехватим ниже)
    buf = io.BytesIO()

    # Перехватываем wb.save() через monkey-patch openpyxl.Workbook.save
    _original_save = openpyxl.Workbook.save

    def _patched_save(self, filename):
        self.save = _original_save.__get__(self, type(self))
        self.save(buf)

    openpyxl.Workbook.save = _patched_save

    try:
        mod.build_report()
    finally:
        openpyxl.Workbook.save = _original_save
        # Убираем env-переменные чтобы не влиять на другие запросы
        os.environ.pop("DATE_FROM", None)
        os.environ.pop("DATE_TO",   None)

    buf.seek(0)
    return buf.read()


# ── Эндпоинт ─────────────────────────────────────────────────────────────────

@router.post(
    "",
    summary="2 Сводка по культурам — Excel",
    description=(
        "Генерирует компактный отчёт по выполнению работ за произвольный диапазон дат. "
        "Возвращает xlsx-файл."
    ),
)
async def post_svodka2(
    body: Svodka2Request,
    current_user: User = Depends(get_current_admin_user),
):
    # Валидация дат
    try:
        df = date.fromisoformat(body.date_from)
        dt = date.fromisoformat(body.date_to)
    except ValueError:
        raise HTTPException(status_code=422, detail="Неверный формат даты. Используйте YYYY-MM-DD.")

    if df > dt:
        raise HTTPException(status_code=422, detail="date_from не может быть позже date_to.")

    max_days = 366
    if (dt - df).days > max_days:
        raise HTTPException(
            status_code=422,
            detail=f"Диапазон не может превышать {max_days} дней.",
        )

    logger.info(
        f"Svodka2: запрос от user_id={current_user.id} "
        f"({body.date_from} — {body.date_to})"
    )

    try:
        xlsx_bytes = await asyncio.to_thread(_run_svodka2, body.date_from, body.date_to)
    except FileNotFoundError as e:
        logger.error(f"Svodka2: файл не найден: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Svodka2: ошибка генерации отчёта: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка генерации отчёта: {e}")

    filename = f"2_svodka_{body.date_from}_{body.date_to}.xlsx"
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
