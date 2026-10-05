"""
cropwise/gps_duplicate_router.py
===================================
Роутер вкладки «GPS трекер на нескольких машинах»: физический
GPS-логгер числится активным сразу у нескольких машин одновременно
(см. cropwise/gps_duplicate_sync.py, адаптация автономного
gps_tracker.py / check_gps_duplicate.py).

Данные хранятся в Redis (ключ gps_duplicate_cache). В ОТЛИЧИЕ от
остальных вкладок панели «Нарушения» — НЕ обновляются автоматически
по расписанию Celery beat, только вручную по кнопке (см. ТЗ). Статус
строки «Отработано» переиспользует общую таблицу violation_statuses и
эндпоинты /api/violations/row-status. Ключ строки:
  - установка трекера:  "gpsmap_{logger_id}_{machine_id}_{start_time}"
  - источник данных:    "gpssrc_{logger_id}_{machine_id}_{start_time}"

Эндпоинты:
  GET  /api/gps-duplicate           → закэшированные данные (2 листа)
  POST /api/gps-duplicate/refresh   → запустить обновление немедленно (админ, только вручную)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import redis
from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import get_current_admin_user
from app.users.models import User
from app.logging_config import get_logger
from app.config import get_redis_url

logger = get_logger("cropwise.gps_duplicate_router")

router = APIRouter(prefix="/api/gps-duplicate", tags=["GPS трекер на нескольких машинах"])

REDIS_KEY = "gps_duplicate_cache"
TZ_MSK    = timezone(timedelta(hours=3))

_redis = redis.from_url(get_redis_url(), decode_responses=True)


def update_cache(mapping_rows: list[dict], source_rows: list[dict], generated_at: datetime) -> None:
    """
    Кэш хранит данные в том же виде, что и два листа Excel-версии отчёта
    (gps_tracker.py): "mapping" ~ лист «Установка трекера», "source" ~
    лист «Источник данных».
    """
    data = {
        "mapping":      mapping_rows,
        "source":       source_rows,
        "generated_at": generated_at.strftime("%d.%m.%Y %H:%M:%S"),
    }
    _redis.set(REDIS_KEY, json.dumps(data, ensure_ascii=False, default=str))
    logger.info(
        f"gps_duplicate cache updated: mapping={len(mapping_rows)}, "
        f"source={len(source_rows)} at {data['generated_at']}"
    )


@router.get("", summary="GPS-логгеры, активные сразу у нескольких машин")
async def get_gps_duplicate(
    current_user: User = Depends(get_current_admin_user),
):
    """
    Возвращает закэшированные данные (см. cropwise/gps_duplicate_sync.py)
    в виде двух наборов строк — "mapping" (установка трекера) и "source"
    (источник GPS-данных), как два листа Excel-версии отчёта. Кэш
    заполняется ТОЛЬКО по кнопке «Обновить» — если сервис только что
    перезапущен и обновление ещё не запускали, mapping=[], source=[].
    """
    raw = _redis.get(REDIS_KEY)
    if not raw:
        return {"generated_at": None, "mapping": [], "source": []}
    return json.loads(raw)


@router.post("/refresh", summary="Принудительно обновить проверку GPS-дублей (админ)")
async def trigger_gps_duplicate_refresh(
    current_user: User = Depends(get_current_admin_user),
):
    """Немедленно запускает gps_duplicate_refresh через Celery (единственный способ обновления — авто-расписания нет)."""
    try:
        from app.cropwise.tasks import gps_duplicate_refresh_task
        task = gps_duplicate_refresh_task.delay()
        logger.info(
            f"gps_duplicate_refresh triggered manually: task_id={task.id}, "
            f"by={current_user.fio}"
        )
        return {"status": "queued", "task_id": task.id}
    except Exception as e:
        logger.error(f"trigger_gps_duplicate_refresh: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи: {e}")
