"""
cropwise/nonstandard_endtimes_router.py
========================================
Роутер: пользователи с нестандартным началом/окончанием задачи за текущие
сутки — задача agri-машины должна ЦЕЛИКОМ покрывать смену: начало ровно
06:00 или 18:00 МСК, конец ровно 17:59 или 05:59 МСК.

Данные хранятся в Redis и обновляются задачей sync_user_assignments
(каждые 15 минут). Никакой БД и миграций не нужно.

Поля: Пользователь | Task ID | Начало (МСК) | Окончание (МСК) | Вид работ
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import redis
from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_admin_user
from app.users.models import User
from app.logging_config import get_logger
from app.config import get_redis_url

logger = get_logger("cropwise.nonstandard_endtimes")

router = APIRouter(
    prefix="/api/nonstandard-endtimes",
    tags=["Нестандартные окончания задач"],
)

REDIS_KEY = "nonstandard_endtimes_cache"
TZ_MSK    = timezone(timedelta(hours=3))

_redis = redis.from_url(get_redis_url(), decode_responses=True)

# Стандартные (hour, minute) окончания смены в МСК
STANDARD_END_TIMES: frozenset[tuple[int, int]] = frozenset({(17, 59), (5, 59)})

# Стандартные (hour, minute) начала смены в МСК
STANDARD_START_TIMES: frozenset[tuple[int, int]] = frozenset({(6, 0), (18, 0)})


def update_cache(rows: list[dict], generated_at: datetime, overlaps: list[dict] | None = None) -> None:
    """Вызывается из sync_user_assignments после каждой синхронизации."""
    data = {
        "rows":         rows,
        "overlaps":     overlaps or [],
        "generated_at": generated_at.strftime("%d.%m.%Y %H:%M"),
    }
    bad = [r for r in rows if r.get("machine_id") == r.get("task_id") or not r.get("machine_id")]
    if bad:
        logger.warning(f"[DIAG] {len(bad)} rows with bad machine_id: {bad[:5]}")
    _redis.set(REDIS_KEY, json.dumps(data, ensure_ascii=False, default=str))
    logger.info(
        f"nonstandard_endtimes cache updated: {len(rows)} rows, "
        f"{len(overlaps or [])} overlaps at {data['generated_at']}"
    )


@router.get("", summary="Пользователи с нестандартным окончанием задачи (текущие сутки)")
async def get_nonstandard_endtimes(
    current_user: User = Depends(get_current_admin_user),
):
    """
    Возвращает список пользователей, чьи активные задачи Cropwise за
    текущие сутки не покрывают смену целиком: начало не 06:00/18:00 МСК,
    и/или конец не 17:59/05:59 МСК. Кэш обновляется каждые 15 минут вместе
    с sync_user_assignments.
    """
    raw = _redis.get(REDIS_KEY)
    if not raw:
        return {"generated_at": None, "rows": []}
    return json.loads(raw)


@router.post("/refresh", summary="Запустить sync_user_assignments немедленно (админ)")
async def trigger_sync_assignments(
    current_user: User = Depends(get_current_admin_user),
):
    """
    Немедленно запускает синхронизацию назначений через Celery.
    Обновляет кэш нестандартных окончаний.
    """
    try:
        from app.cropwise.tasks import sync_user_assignments_task
        task = sync_user_assignments_task.delay()
        logger.info(f"sync_user_assignments triggered manually: task_id={task.id}, by={current_user.fio}")
        return {"status": "queued", "task_id": task.id}
    except Exception as e:
        logger.error(f"trigger_sync_assignments: {e}", exc_info=True)
        from fastapi import HTTPException
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи: {e}")
