"""
cropwise/violations_router.py
================================
Роутер: панель «Нарушения» — проверки Cropwise-заданий за текущие сутки.

Проверки:
  check1 — «Нет агроопераций к полям»       (нет поля И нет транзита)
  check3 — «Не указан механизатор»           (driver_id is null)
  check4 — «Не привязано оборудование»       (implement_id is null)
  check5 — «Площадь задана вручную»          (manual_covered_area > 0)
  check6 — «Агро без задания»                (agri-машина, GPS >= 2 км, нет задания)
  check6t — «Transport без задания»          (transport-машина, GPS >= 2 км, нет задания)
  check9 — «Возможный транзит»               (0.1 га < площадь < 5 га, нет in_transit)

Данные хранятся в Redis (ключ: violations_cache) и обновляются Celery:
  check1 — violations_refresh_check1 раз в 1,5 ч
  остальные checks — violations_refresh раз в час (6–19 МСК)

Эндпоинты:
  GET  /api/violations        → { generated_at, check1: [...], check3: [...], check4: [...], check5: [...], check6: [...], check6t: [...], check9: [...] }
  POST /api/violations/refresh → запустить violations_refresh_task через Celery
                                   (опционально body: { "checks": ["check3"] })
  GET  /api/violations/row-status → { key: status, ... } статусы «Отработано» всех строк (общие для всех пользователей)
  POST /api/violations/row-status → { key, status } установить статус строки (видно всем пользователям)
  POST /api/violations/row-status/bulk → { keys: [...], status } то же для списка строк одной транзакцией
  GET  /api/violations/resolved-stats → кто сколько строк перевёл в статус «Отработана» за период
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

SERVER_TZ = timezone(timedelta(hours=3))  # МСК — та же зона, что и в downtime/router.py

import redis
from celery.result import AsyncResult
from fastapi import APIRouter, Depends, Query, HTTPException

from app.auth.dependencies import get_current_admin_user
from app.users.models import User
from app.logging_config import get_logger
from app.config import get_redis_url
from app.database import async_session_maker
from app.cropwise.violation_status_model import ViolationStatus
from app.cropwise.schemas import (
    ViolationRowStatusUpdate,
    ViolationRowStatusBulkUpdate,
    ViolationResolutionResponse,
    ViolationsRefreshRequest,
)

logger = get_logger("cropwise.violations")

router = APIRouter(
    prefix="/api/violations",
    tags=["Нарушения"],
)

REDIS_KEY = "violations_cache"
TZ_MSK    = timezone(timedelta(hours=3))

_CACHE_CHECK_KEYS = (
    "check1", "check3", "check4", "check5", "check6", "check6t",
    "check9", "check10", "check2",
)

_redis = redis.from_url(get_redis_url(), decode_responses=True)


def _empty_cache() -> dict:
    return {k: [] for k in _CACHE_CHECK_KEYS}


def merge_cache(updates: dict[str, list], generated_at: datetime) -> None:
    """Частичное обновление Redis-кэша: только переданные ключи checks."""
    raw = _redis.get(REDIS_KEY)
    data = json.loads(raw) if raw else _empty_cache()
    for key in _CACHE_CHECK_KEYS:
        data.setdefault(key, [])
    for key, value in updates.items():
        if key in _CACHE_CHECK_KEYS:
            data[key] = value
    data["generated_at"] = generated_at.strftime("%d.%m.%Y %H:%M")
    _redis.set(REDIS_KEY, json.dumps(data, ensure_ascii=False, default=str))
    logger.info(
        f"violations cache merged: updated={sorted(updates.keys())}, "
        + ", ".join(f"{k}={len(data[k])}" for k in sorted(updates.keys()))
        + f", at={data['generated_at']}"
    )


def update_cache(check1, check3, check4, check5, check6, check6t, check9, check10, check2, generated_at) -> None:
    data = {
        "check1":       check1,
        "check3":       check3,
        "check4":       check4,
        "check5":       check5,
        "check6":       check6,
        "check6t":      check6t,
        "check9":       check9,
        "check10":      check10,
        "check2":       check2,
        "generated_at": generated_at.strftime("%d.%m.%Y %H:%M"),
    }
    _redis.set(REDIS_KEY, json.dumps(data, ensure_ascii=False, default=str))
    logger.info(
        f"violations cache updated: check1={len(check1)}, "
        f"check3={len(check3)}, check4={len(check4)}, check5={len(check5)}, "
        f"check6={len(check6)}, check6t={len(check6t)}, check9={len(check9)}, "
        f"check10={len(check10)}, check2={len(check2)}, "
        f"at={data['generated_at']}"
    )


@router.get("", summary="Нарушения Cropwise-заданий за текущие сутки")
async def get_violations(
    current_user: User = Depends(get_current_admin_user),
):
    """
    Возвращает списки нарушений за текущий день:
      check1 — нет агроопераций к полям
      check3 — не указан механизатор
      check4 — не привязано оборудование
      check5 — площадь задана вручную
      check6 — агро-машина без задания с GPS >= 2 км
      check6t — transport-машина без задания с GPS >= 2 км
    Кэш обновляется Celery: check1 — раз в 1,5 ч, остальные — раз в час.
    """
    raw = _redis.get(REDIS_KEY)
    if not raw:
        return {"generated_at": None, "check1": [], "check3": [], "check4": [], "check5": [], "check6": [], "check6t": [], "check9": [], "check10": [], "check2": []}
    data = json.loads(raw)
    # Обратная совместимость: старый кэш может не иметь более новых ключей
    for key in ("check6t", "check9", "check10", "check2"):
        if key not in data:
            data[key] = []
    return data


@router.post("/refresh", summary="Принудительно обновить данные нарушений (админ)")
async def trigger_violations_refresh(
    body: ViolationsRefreshRequest | None = None,
    current_user: User = Depends(get_current_admin_user),
):
    """Ставит в очередь одну или две Celery-задачи (check1 и main — параллельно).

    ``{"checks": ["check1", "check3"]}`` → две задачи в разных процессах.
    ``{"checks": ["check3"]}`` → только main. ``{"checks": ["check1"]}`` → только check1.
    Без тела — обе партиции (полное обновление основных checks).
    """
    try:
        from app.cropwise.tasks import (
            violations_refresh_check1_task,
            violations_refresh_task,
        )
        from app.cropwise.violations_sync import split_violation_refresh_partitions

        checks = body.checks if body else None
        run_check1, main_checks = split_violation_refresh_partitions(checks)

        queued: list[dict] = []
        if run_check1:
            t = violations_refresh_check1_task.delay()
            queued.append({"partition": "check1", "task_id": t.id})
        if main_checks:
            t = violations_refresh_task.delay(checks=main_checks)
            queued.append({"partition": "main", "task_id": t.id})

        if not queued:
            raise HTTPException(status_code=400, detail="Нет checks для обновления")

        logger.info(
            f"violations_refresh triggered manually: {queued}, "
            f"checks={checks or 'all'}, by={current_user.fio}"
        )
        return {
            "status": "queued",
            "tasks": queued,
            "task_id": queued[0]["task_id"],
            "checks": checks,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"trigger_violations_refresh: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи: {e}")


@router.get("/refresh/status/{task_id}", summary="Статус задачи обновления нарушений")
async def get_violations_refresh_status(
    task_id: str,
    current_user: User = Depends(get_current_admin_user),
):
    """Поллинг статуса Celery-задачи violations_refresh_task."""
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
            info = result.info or {}
            return {"status": "RUNNING", "meta": info}
        return {"status": "PENDING"}
    except Exception as e:
        logger.error(f"violations refresh status error: {e}")
        return {"status": "UNKNOWN", "error": str(e)}


# ============================================================================
# Статус строки «Отработано» (кнопка В работу / В работе / Отработана)
# ----------------------------------------------------------------------------
# Раньше хранился в localStorage браузера (виден только тому, кто поставил).
# Теперь хранится в таблице violation_statuses и виден всем пользователям сайта.
# Запросы лёгкие (один SELECT/UPSERT по индексированному key), заметной
# нагрузки на сервер не создают даже при частых кликах.
# ============================================================================

@router.get("/row-status", summary="Получить все статусы строк нарушений (для всех пользователей)")
async def get_violation_row_statuses(
    current_user: User = Depends(get_current_admin_user),
):
    """Возвращает {key: {status, fio, updatedAt}} по всем строкам, у которых статус
    когда-либо менялся. fio/updatedAt — чтобы в панели «Нарушения» вместо просто
    цветной подписи «В работе»/«Отработана» показывать фамилию того, кто отметил
    статус (как уже сделано в «Истории простоев»)."""
    from app.bot_api.services import violations as violation_service

    return await violation_service.fetch_all_row_statuses()


@router.post("/row-status", summary="Установить статус строки нарушения (видно всем пользователям)")
async def set_violation_row_status(
    payload: ViolationRowStatusUpdate,
    current_user: User = Depends(get_current_admin_user),
):
    """Upsert статуса по ключу строки. Возвращает сохранённое значение."""
    from app.bot_api.services import violations as violation_service

    return await violation_service.upsert_row_status(
        payload.key, payload.status, current_user.id
    )


@router.post("/row-status/bulk", summary="Массово установить статус строк нарушений (видно всем пользователям)")
async def set_violation_row_statuses_bulk(
    payload: ViolationRowStatusBulkUpdate,
    current_user: User = Depends(get_current_admin_user),
):
    """Upsert статуса сразу для списка ключей (кнопка «Отметить все» в модалке «URL»
    вкладки «Нет агроопераций»). Автор отметки — текущий пользователь, как и при
    клике по одной кнопке, поэтому в «Кто отработал» (resolved-stats) эти строки
    засчитываются ему же."""
    from app.bot_api.services import violations as violation_service

    result = await violation_service.upsert_row_statuses_bulk(
        payload.keys, payload.status, current_user.id
    )
    logger.info(
        f"violations bulk row-status: user_id={current_user.id}, "
        f"status={payload.status}, count={result['count']}"
    )
    return result


@router.get(
    "/resolved-stats",
    response_model=ViolationResolutionResponse,
    summary="Кто сколько строк нарушений отработал за период",
)
async def get_violation_resolved_stats(
    date_from: datetime = Query(..., description="Начало периода, например 2026-06-01T00:00:00"),
    date_to: datetime = Query(..., description="Конец периода, например 2026-06-19T23:59:59"),
    current_user: User = Depends(get_current_admin_user),
):
    """
    Считает, сколько строк нарушений каждый пользователь перевёл в финальный
    статус «Отработана» (status=2), у которых дата последнего изменения
    (updated_at) попадает в [date_from, date_to].

    Учитывается только ТЕКУЩИЙ статус строки — если её отметили «Отработана»,
    а затем статус сменили обратно на «В работу»/«В работе», строка в счёт
    не попадает (история промежуточных переходов не хранится).
    """
    from sqlalchemy import select, func

    async with async_session_maker() as session:
        q = (
            select(
                User.id.label("user_id"),
                User.fio.label("user_fio"),
                User.role.label("user_role"),
                func.count(ViolationStatus.id).label("resolved"),
            )
            .join(User, User.id == ViolationStatus.updated_by)
            .where(
                ViolationStatus.status == 2,
                ViolationStatus.updated_at >= date_from,
                ViolationStatus.updated_at <= date_to,
            )
            .group_by(User.id, User.fio, User.role)
            .order_by(func.count(ViolationStatus.id).desc())
        )
        rows = (await session.execute(q)).all()

    return {
        "items": [
            {"user_id": r.user_id, "user_fio": r.user_fio, "user_role": r.user_role, "resolved": r.resolved}
            for r in rows
        ]
    }

