"""
cropwise/vehicle_check_router.py
==================================
Роутер вкладки «Проверка техники»: техника с активным агрозаданием
(Опрыскивание / Уборка / Культивация / Посев) в Cropwise, у которой прямо
сейчас зафиксирована потеря связи с GPS-терминалом продолжительностью
более 3 часов (см. MIN_DISPLAY_DURATION_SECONDS в vehicle_check_sync.py).

Данные хранятся в Redis (ключ vehicle_check_cache) и обновляются
Celery-задачей vehicle_check_refresh раз в 3 часа (см.
celery_config.py). Причины отсутствия связи не определяются и не
показываются — раздел только фиксирует сам факт (см. ТЗ).

Статус строки «Отработано» переиспользует общую таблицу
violation_statuses и эндпоинты /api/violations/row-status — тот же
механизм, что и во всех остальных вкладках панели «Нарушения». Ключ
строки: "vc_{machine_id}_{task_id}".

Эндпоинты:
  GET  /api/vehicle-check          → закэшированные данные
  POST /api/vehicle-check/refresh  → запустить обновление немедленно (админ)
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

logger = get_logger("cropwise.vehicle_check")

router = APIRouter(prefix="/api/vehicle-check", tags=["Проверка техники"])

REDIS_KEY = "vehicle_check_cache"
TZ_MSK    = timezone(timedelta(hours=3))

_redis = redis.from_url(get_redis_url(), decode_responses=True)


def update_cache(rows: list[dict], generated_at: datetime) -> None:
    data = {
        "rows":         rows,
        "generated_at": generated_at.strftime("%d.%m.%Y %H:%M:%S"),
    }
    _redis.set(REDIS_KEY, json.dumps(data, ensure_ascii=False, default=str))
    logger.info(f"vehicle_check cache updated: {len(rows)} rows at {data['generated_at']}")


@router.get("", summary="Техника с потерей связи GPS-терминала по активным агрозаданиям")
async def get_vehicle_check(
    current_user: User = Depends(get_current_admin_user),
):
    """
    Возвращает закэшированный список техники, у которой прямо сейчас
    зафиксирована потеря связи (см. cropwise/vehicle_check_sync.py).
    Если кэш ещё пуст (сервис только что перезапущен) — rows=[].
    """
    raw = _redis.get(REDIS_KEY)
    if not raw:
        return {"generated_at": None, "rows": []}
    return json.loads(raw)


@router.post("/refresh", summary="Принудительно обновить проверку техники (админ)")
async def trigger_vehicle_check_refresh(
    current_user: User = Depends(get_current_admin_user),
):
    """Немедленно запускает vehicle_check_refresh через Celery."""
    try:
        from app.cropwise.tasks import vehicle_check_refresh_task
        task = vehicle_check_refresh_task.delay()
        logger.info(
            f"vehicle_check_refresh triggered manually: task_id={task.id}, "
            f"by={current_user.fio}"
        )
        return {"status": "queued", "task_id": task.id}
    except Exception as e:
        logger.error(f"trigger_vehicle_check_refresh: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи: {e}")
