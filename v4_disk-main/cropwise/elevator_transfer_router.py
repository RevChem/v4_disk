"""
cropwise/elevator_transfer_router.py
======================================
Роутер вкладки «Перемещение между элеваторами»: подозрительно быстрые
переезды транспорта между разными элеваторами. Визиты — пересечения
machine_object_intersections с полигонами AdditionalObject; логика —
cropwise/elevator_transfer_sync.py.

Данные хранятся в Redis (ключ elevator_transfer_cache) и обновляются
Celery-задачей elevator_transfer_refresh раз в час (см.
celery_config.py). Статус строки «Отработано»
переиспользует общую таблицу violation_statuses и эндпоинты
/api/violations/row-status — тот же механизм, что и во всех остальных
вкладках панели «Нарушения». Ключ строки (один визит одной машины на
одном элеваторе): "elev_{machine_id}_{elevator_id}_{visit_start_ts}".

Эндпоинты:
  GET  /api/elevator-transfer-violations          → закэшированные данные
  POST /api/elevator-transfer-violations/refresh   → запустить обновление немедленно (админ)
  POST /api/elevator-transfer-violations/range     → прогон за произвольный период (модалка «За период»)
  GET  /api/elevator-transfer-violations/range/status/{task_id} → статус и результат прогона за период

Прогон за период кэш вкладки НЕ меняет: результат лежит в Redis под ключом
elevator_range_result:{task_id} один час и отдаётся эндпоинтом статуса.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone, timedelta

import redis
from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import get_current_admin_user
from app.users.models import User
from app.logging_config import get_logger
from app.config import get_redis_url

logger = get_logger("cropwise.elevator_transfer")

router = APIRouter(prefix="/api/elevator-transfer-violations", tags=["Перемещение между элеваторами"])

REDIS_KEY = "elevator_transfer_cache"
RANGE_RESULT_KEY = "elevator_range_result:{}"
RANGE_RESULT_TTL_SEC = 3600
MAX_RANGE_DAYS = 31   # синхронно с elevator_transfer_sync.MAX_RANGE_DAYS
TZ_MSK    = timezone(timedelta(hours=3))

_redis = redis.from_url(get_redis_url(), decode_responses=True)


def update_cache(summary_rows: list[dict], detail_rows: list[dict], generated_at: datetime) -> None:
    """
    Кэш хранит данные в том же виде, что и два листа Excel-версии отчёта
    (elevator_transfer_report.py): "summary" ~ лист «Нарушения (сводка)»,
    "detail" ~ лист «Детализация». Формат вывода на фронте (обе таблицы +
    экспорт в Excel) должен зеркалить эти два листа один в один.
    """
    data = {
        "summary":      summary_rows,
        "detail":       detail_rows,
        "generated_at": generated_at.strftime("%d.%m.%Y %H:%M:%S"),
    }
    _redis.set(REDIS_KEY, json.dumps(data, ensure_ascii=False, default=str))
    logger.info(
        f"elevator_transfer cache updated: summary={len(summary_rows)}, "
        f"detail={len(detail_rows)} at {data['generated_at']}"
    )


@router.get("", summary="Подозрительные переезды между элеваторами")
async def get_elevator_transfer_violations(
    current_user: User = Depends(get_current_admin_user),
):
    """
    Возвращает закэшированные данные (см. cropwise/elevator_transfer_sync.py)
    в виде двух наборов строк — "summary" (сводка по машинам-нарушителям) и
    "detail" (по одной строке на визит), как два листа Excel-версии отчёта.
    Если кэш ещё пуст (сервис только что перезапущен, либо не найден CSV с
    координатами элеваторов) — summary=[], detail=[].
    """
    raw = _redis.get(REDIS_KEY)
    if not raw:
        return {"generated_at": None, "summary": [], "detail": []}
    return json.loads(raw)


@router.post("/refresh", summary="Принудительно обновить проверку элеваторов (админ)")
async def trigger_elevator_transfer_refresh(
    current_user: User = Depends(get_current_admin_user),
):
    """Немедленно запускает elevator_transfer_refresh через Celery."""
    try:
        from app.cropwise.tasks import elevator_transfer_refresh_task
        task = elevator_transfer_refresh_task.delay()
        logger.info(
            f"elevator_transfer_refresh triggered manually: task_id={task.id}, "
            f"by={current_user.fio}"
        )
        return {"status": "queued", "task_id": task.id}
    except Exception as e:
        logger.error(f"trigger_elevator_transfer_refresh: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи: {e}")


# ============================================================================
# Прогон за произвольный период
# ============================================================================

class ElevatorRangeIn(BaseModel):
    date_from: date
    date_to: date


def store_range_result(run_id: str, data: dict) -> None:
    _redis.set(
        RANGE_RESULT_KEY.format(run_id),
        json.dumps(data, ensure_ascii=False, default=str),
        ex=RANGE_RESULT_TTL_SEC,
    )


@router.post("/range", summary="Прогнать проверку элеваторов за период (админ)")
async def trigger_elevator_transfer_range(
    payload: ElevatorRangeIn,
    current_user: User = Depends(get_current_admin_user),
):
    """Ставит в Celery расчёт за date_from … date_to включительно (сутки 06:00–06:00).
    Кэш вкладки не затрагивается. Результат — через /range/status/{task_id}."""
    today = datetime.now(tz=TZ_MSK).date()
    if payload.date_to < payload.date_from:
        raise HTTPException(status_code=422, detail="Дата «по» раньше даты «с»")
    if payload.date_from > today:
        raise HTTPException(status_code=422, detail="Период ещё не начался")
    if (payload.date_to - payload.date_from).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=f"Период не должен быть длиннее {MAX_RANGE_DAYS} суток")
    try:
        from app.cropwise.tasks import elevator_transfer_range_task
        run_id = uuid.uuid4().hex
        elevator_transfer_range_task.apply_async(
            args=[payload.date_from.isoformat(), payload.date_to.isoformat(), run_id],
            task_id=run_id,
        )
        logger.info(
            f"elevator_transfer_range queued: task_id={run_id}, "
            f"{payload.date_from}..{payload.date_to}, by={current_user.fio}"
        )
        return {"status": "queued", "task_id": run_id}
    except Exception as e:
        logger.error(f"trigger_elevator_transfer_range: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи: {e}")


@router.get("/range/status/{task_id}", summary="Статус прогона за период и его результат")
async def get_elevator_transfer_range_status(
    task_id: str,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        result = AsyncResult(task_id)
        state = result.state
        if state == "SUCCESS":
            res = result.result
            if isinstance(res, dict) and res.get("skipped"):
                return {"status": "SKIPPED", "reason": res.get("reason", "already running")}
            raw = _redis.get(RANGE_RESULT_KEY.format(task_id))
            if not raw:
                return {"status": "FAILURE", "error": "Результат устарел — запустите расчёт заново"}
            return {"status": "SUCCESS", "result": json.loads(raw)}
        if state == "FAILURE":
            return {"status": "FAILURE", "error": str(result.result)}
        if state in ("STARTED", "PROGRESS"):
            return {"status": "RUNNING"}
        return {"status": "PENDING"}
    except Exception as e:
        logger.error(f"elevator range status error: {e}")
        return {"status": "UNKNOWN", "error": str(e)}
