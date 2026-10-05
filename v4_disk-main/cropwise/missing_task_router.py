"""
cropwise/missing_task_router.py
================================
Роутер вкладки «Возможное отсутствие задачи»: машина была на поле
(machine_object_intersections, object_type=Field), а задания или
агрооперации на это время нет. Логика — cropwise/missing_task_sync.py.

Данные хранятся в Redis (ключ missing_task_cache) и обновляются
Celery-задачей missing_task_refresh раз в час (см. celery_config.py).
Статус строки «Отработано» переиспользует общую таблицу
violation_statuses и эндпоинты /api/violations/row-status. Ключ строки:
"mt_{reason}_{machine_id}_{field_id}_{start_ts}".

Эндпоинты:
  GET  /api/missing-task           → закэшированные строки
  POST /api/missing-task/refresh   → запустить обновление немедленно (админ)
  GET  /api/missing-task/refresh/status/{task_id} → статус задачи
       (PENDING / RUNNING / SUCCESS / FAILURE / SKIPPED — лок занят, пересчёта не было)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import redis
from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import get_current_admin_user
from app.users.models import User
from app.logging_config import get_logger
from app.config import get_redis_url

logger = get_logger("cropwise.missing_task")

router = APIRouter(prefix="/api/missing-task", tags=["Возможное отсутствие задачи"])

REDIS_KEY = "missing_task_cache"
TZ_MSK = timezone(timedelta(hours=3))

_redis = redis.from_url(get_redis_url(), decode_responses=True)


def update_cache(rows: list[dict], generated_at: datetime, stats: dict | None = None) -> None:
    data = {
        "rows": rows,
        "generated_at": generated_at.strftime("%d.%m.%Y %H:%M:%S"),
        "stats": stats or {},
    }
    _redis.set(REDIS_KEY, json.dumps(data, ensure_ascii=False, default=str))
    logger.info(
        f"missing_task cache updated: rows={len(rows)} at {data['generated_at']}"
    )


@router.get("", summary="Машина на поле без задания или агрооперации")
async def get_missing_task(
    current_user: User = Depends(get_current_admin_user),
):
    raw = _redis.get(REDIS_KEY)
    if not raw:
        return {"generated_at": None, "rows": [], "stats": {}}
    return json.loads(raw)


@router.post("/refresh", summary="Принудительно обновить проверку (админ)")
async def trigger_missing_task_refresh(
    current_user: User = Depends(get_current_admin_user),
):
    try:
        from app.cropwise.tasks import missing_task_refresh_task
        task = missing_task_refresh_task.delay()
        logger.info(
            f"missing_task_refresh triggered manually: task_id={task.id}, "
            f"by={current_user.fio}"
        )
        return {"status": "queued", "task_id": task.id}
    except Exception as e:
        logger.error(f"trigger_missing_task_refresh: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи: {e}")


@router.get("/refresh/status/{task_id}", summary="Статус задачи обновления «Возможного отсутствия задачи»")
async def get_missing_task_refresh_status(
    task_id: str,
    current_user: User = Depends(get_current_admin_user),
):
    """Поллинг статуса Celery-задачи missing_task_refresh."""
    try:
        result = AsyncResult(task_id)
        state = result.state

        if state == "SUCCESS":
            res = result.result
            if isinstance(res, dict) and res.get("skipped"):
                return {
                    "status": "SKIPPED",
                    "reason": res.get("reason", "already running"),
                }
            return {"status": "SUCCESS", "result": res}
        if state == "FAILURE":
            return {"status": "FAILURE", "error": str(result.result)}
        if state in ("STARTED", "PROGRESS"):
            return {"status": "RUNNING", "meta": result.info or {}}
        return {"status": "PENDING"}
    except Exception as e:
        logger.error(f"missing_task refresh status error: {e}")
        return {"status": "UNKNOWN", "error": str(e)}
