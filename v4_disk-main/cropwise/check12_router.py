"""
cropwise/check12_router.py
==========================
Роутер: «Несоответствие сезона задачи и агрооперации» (check12).

Запускается ТОЛЬКО вручную — тяжёлая проверка (полная выгрузка mapping_items).
Кэш хранится в Redis (ключ: check12_cache).

Эндпоинты:
  GET  /api/violations/check12         → закэшированные данные (или пусто)
  POST /api/violations/check12/refresh → запустить Celery-задачу, вернуть task_id
  GET  /api/violations/check12/status/{task_id} → статус задачи
       (PENDING / RUNNING / SUCCESS / FAILURE / SKIPPED — лок занят, пересчёта не было)
"""
from __future__ import annotations

import json
from datetime import timezone, timedelta

import redis
from fastapi import APIRouter, Depends
from celery.result import AsyncResult

from app.auth.dependencies import get_current_admin_user
from app.users.models import User
from app.logging_config import get_logger
from app.config import get_redis_url

logger = get_logger("cropwise.check12")

router = APIRouter(
    prefix="/api/violations/check12",
    tags=["Нарушения"],
)

REDIS_KEY = "check12_cache"
TZ_MSK    = timezone(timedelta(hours=3))

_redis = redis.from_url(get_redis_url(), decode_responses=True)


@router.get("", summary="Несоответствие сезона: получить кэш")
async def get_check12(
    current_user: User = Depends(get_current_admin_user),
):
    """
    Возвращает закэшированный результат последнего запуска check12.
    Если кэш пуст — возвращает generated_at=None и rows=[].
    """
    raw = _redis.get(REDIS_KEY)
    if not raw:
        return {"generated_at": None, "rows": [], "total": 0}
    data = json.loads(raw)
    return data


@router.post("/refresh", summary="Запустить check12 (тяжёлая проверка, по требованию)")
async def trigger_check12_refresh(
    current_user: User = Depends(get_current_admin_user),
):
    """
    Ставит задачу check12_refresh_task в очередь Celery.
    Возвращает task_id для поллинга статуса.
    """
    try:
        from app.cropwise.tasks import check12_refresh_task
        task = check12_refresh_task.delay()
        logger.info(
            f"check12_refresh triggered manually: task_id={task.id}, "
            f"by={current_user.fio}"
        )
        return {"status": "queued", "task_id": task.id}
    except Exception as e:
        logger.error(f"trigger_check12_refresh: {e}", exc_info=True)
        from fastapi import HTTPException
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи: {e}")


@router.get("/status/{task_id}", summary="Статус задачи check12")
async def get_check12_status(
    task_id: str,
    current_user: User = Depends(get_current_admin_user),
):
    """
    Поллинг статуса Celery-задачи.
    Возвращает: status (PENDING/RUNNING/SUCCESS/FAILURE/SKIPPED), result, error.
    """
    try:
        result = AsyncResult(task_id)
        state  = result.state

        if state == "SUCCESS":
            res = result.result
            # with_lock возвращает {"skipped": True}, если лок уже занят.
            # Для UI это НЕ успех: данные не пересчитывались.
            if isinstance(res, dict) and res.get("skipped"):
                return {
                    "status": "SKIPPED",
                    "reason": res.get("reason", "already running"),
                }
            return {"status": "SUCCESS", "result": res}
        elif state == "FAILURE":
            return {"status": "FAILURE", "error": str(result.result)}
        elif state in ("STARTED", "PROGRESS"):
            info = result.info or {}
            return {"status": "RUNNING", "meta": info}
        else:
            return {"status": "PENDING"}
    except Exception as e:
        logger.error(f"check12 status error: {e}")
        return {"status": "UNKNOWN", "error": str(e)}
