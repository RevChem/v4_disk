"""
cropwise/duplicates_router.py
===================================
Роутер вкладки «Дубли» панели «Нарушения»: дубли ФИО пользователей и
дубли машин по госномеру в Cropwise (см. cropwise/duplicates_sync.py,
адаптация автономного скрипта cropwise_audit.py).

Данные хранятся в Redis (ключ duplicates_cache). В ОТЛИЧИЕ от вкладок
с Celery beat-расписанием — НЕ обновляются автоматически, только вручную
по кнопке (тот же принцип, что и «GPS на нескольких машинах», см.
gps_duplicate_router.py). Статус строки «Отработано» переиспользует общую
таблицу violation_statuses и эндпоинты /api/violations/row-status. Ключ строки:
  - дубли ФИО:    "dupname_{id_a}_{id_b}"
  - дубли машин:  "dupplate_{id_a}_{id_b}"

Эндпоинты:
  GET  /api/duplicates           → закэшированные данные (2 набора строк)
  POST /api/duplicates/refresh   → запустить обновление немедленно (админ, только вручную)
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

logger = get_logger("cropwise.duplicates_router")

router = APIRouter(prefix="/api/duplicates", tags=["Дубли (ФИО и машины)"])

REDIS_KEY = "duplicates_cache"
TZ_MSK = timezone(timedelta(hours=3))

_redis = redis.from_url(get_redis_url(), decode_responses=True)


def update_cache(name_rows: list[dict], plate_rows: list[dict], generated_at: datetime) -> None:
    data = {
        "names": name_rows,
        "plates": plate_rows,
        "generated_at": generated_at.strftime("%d.%m.%Y %H:%M:%S"),
    }
    _redis.set(REDIS_KEY, json.dumps(data, ensure_ascii=False, default=str))
    logger.info(
        f"duplicates cache updated: names={len(name_rows)}, "
        f"plates={len(plate_rows)} at {data['generated_at']}"
    )


@router.get("", summary="Дубли ФИО пользователей и машин по госномеру")
async def get_duplicates(
    current_user: User = Depends(get_current_admin_user),
):
    """
    Возвращает закэшированные данные (см. cropwise/duplicates_sync.py) —
    два набора строк: "names" (дубли ФИО) и "plates" (дубли машин по
    госномеру). Кэш заполняется ТОЛЬКО по кнопке «Обновить» — если сервис
    только что перезапущен и обновление ещё не запускали, names=[], plates=[].
    """
    raw = _redis.get(REDIS_KEY)
    if not raw:
        return {"generated_at": None, "names": [], "plates": []}
    return json.loads(raw)


@router.post("/refresh", summary="Принудительно обновить проверку дублей (админ)")
async def trigger_duplicates_refresh(
    current_user: User = Depends(get_current_admin_user),
):
    """Немедленно запускает duplicates_refresh через Celery (единственный способ обновления — авто-расписания нет)."""
    try:
        from app.cropwise.tasks import duplicates_refresh_task
        task = duplicates_refresh_task.delay()
        logger.info(
            f"duplicates_refresh triggered manually: task_id={task.id}, "
            f"by={current_user.fio}"
        )
        return {"status": "queued", "task_id": task.id}
    except Exception as e:
        logger.error(f"trigger_duplicates_refresh: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи: {e}")
