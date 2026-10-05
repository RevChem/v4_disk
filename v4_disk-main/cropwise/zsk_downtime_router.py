"""
cropwise/zsk_downtime_router.py
==================================
Роутер вкладки «Простои ЗСК»: простои по датчику «Вентилятор»
на объектах FortMonitor (smsr.online). Логика — cropwise/zsk_downtime_sync.py
(адаптация автономного fortmonitor_zsk_downtime_by_value.py).

Данные хранятся в Redis (ключ zsk_downtime_cache) и обновляются
Celery-задачей zsk_downtime_refresh каждые 30 минут (см.
celery_config.py). Статус строки «Отработано» переиспользует общую
таблицу violation_statuses и эндпоинты /api/violations/row-status —
тот же механизм, что и во всех остальных вкладках панели «Нарушения».
Ключ строки: "zsk_{zsk_id}_{start_local}".

Окно расчёта настраивается через /config (ключ Redis
zsk_downtime_config): date_from / date_to — КАЛЕНДАРНЫЕ ДНИ. Начало
дня (06:00 по местному времени) зафиксировано в zsk_downtime_sync.py
(DAY_START_HOUR) и через этот эндпоинт не редактируется. Без
сохранённого конфига (или после сброса) — по умолчанию текущий день
(и date_from, и date_to).

Классификация причин простоя — колонка "Причины" (см. блок "КЛАССИФИКАЦИЯ
СТРОК ПРОСТОЯ" ниже): справочник "КЛАССИФИКАТОР ПРОСТОЕВ ПО ПРИЧИНАМ
ВОЗНИКНОВЕНИЯ" — статичные данные в zsk_downtime_classifier.py. Сама
классификация конкретной строки (что выбрал пользователь) хранится в Redis
отдельным ключом (zsk_downtime_classification), не в zsk_downtime_cache —
чтобы пережить его пересборку Celery-задачей каждые 30 минут.

Эндпоинты:
  GET  /api/zsk-downtime            → закэшированные данные (простои)
  GET  /api/zsk-downtime/config     → текущий диапазон дат
  POST /api/zsk-downtime/config     → сохранить диапазон дат (админ)
  POST /api/zsk-downtime/refresh    → запустить обновление немедленно (админ)
  GET  /api/zsk-downtime/classifier/events        → события классификатора (1-я ступень)
  GET  /api/zsk-downtime/classifier/descriptions  → описания для события (2-я ступень)
  GET  /api/zsk-downtime/classifier/search        → быстрый поиск по событию+описанию
  GET  /api/zsk-downtime/classifier/domains       → допустимые значения причины/фактора/типа (подсказки)
  GET  /api/zsk-downtime/classification           → все сохранённые классификации строк
  POST /api/zsk-downtime/classification           → задать классификацию строки
  POST /api/zsk-downtime/classification/clear     → сбросить классификацию строки
"""
from __future__ import annotations

import json
from datetime import datetime, date, timezone, timedelta
from typing import Optional

import redis
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_admin_user
from app.users.models import User
from app.logging_config import get_logger
from app.config import get_redis_url
from app.cropwise import zsk_downtime_classifier as classifier

logger = get_logger("cropwise.zsk_downtime_router")

router = APIRouter(prefix="/api/zsk-downtime", tags=["Простои ЗСК"])

REDIS_KEY        = "zsk_downtime_cache"
REDIS_CONFIG_KEY = "zsk_downtime_config"
# Классификация строк простоя (событие/описание/причина/фактор/тип), см.
# блок "КЛАССИФИКАЦИЯ ПРОСТОЕВ" ниже. Отдельный от REDIS_KEY ключ — намеренно:
# zsk_downtime_cache целиком перезаписывается Celery-задачей каждые 30 минут
# (свежий снимок из FortMonitor), а классификация — это данные, которые вносит
# пользователь и которые должны пережить эту перезапись. Связывается со строкой
# по тому же стабильному ключу "zsk_{zsk_id}_{start_local}", что уже
# используется для статуса "Отработано" (см. модуль docstring выше).
REDIS_CLASSIFICATION_KEY = "zsk_downtime_classification"
TZ_MSK           = timezone(timedelta(hours=3))

_redis = redis.from_url(get_redis_url(), decode_responses=True)


# =============================================================================
# КЭШ ДАННЫХ
# =============================================================================

def update_cache(rows: list[dict], generated_at: datetime, meta: dict | None = None) -> None:
    data = {
        "rows":         rows,
        "generated_at": generated_at.strftime("%d.%m.%Y %H:%M:%S"),
        "meta":         meta or {},
    }
    _redis.set(REDIS_KEY, json.dumps(data, ensure_ascii=False, default=str))
    logger.info(f"zsk_downtime cache updated: {len(rows)} rows at {data['generated_at']}")


# =============================================================================
# КОНФИГУРАЦИЯ ДИАПАЗОНА ДАТ
# =============================================================================

def get_zsk_date_range() -> tuple[date, date]:
    """
    Читает сохранённый диапазон дат из Redis. Если конфига нет (или он
    некорректен) — по умолчанию текущий день (см.
    zsk_downtime_sync.default_date_range).
    """
    from app.cropwise.zsk_downtime_sync import default_date_range

    raw = _redis.get(REDIS_CONFIG_KEY)
    if raw:
        try:
            cfg = json.loads(raw)
            return date.fromisoformat(cfg["date_from"]), date.fromisoformat(cfg["date_to"])
        except Exception as e:
            logger.error(f"get_zsk_date_range: parse error, использую дефолт: {e}")

    now_server = datetime.now().replace(microsecond=0)
    return default_date_range(now_server)


def set_zsk_date_range(date_from: date, date_to: date) -> None:
    _redis.set(REDIS_CONFIG_KEY, json.dumps({
        "date_from": date_from.isoformat(),
        "date_to":   date_to.isoformat(),
    }))
    logger.info(f"zsk_downtime_config saved: {date_from} — {date_to}")


class ZskDateRangePayload(BaseModel):
    date_from: str  # "YYYY-MM-DD"; null/omit не поддерживается — сброс делается отдельной кнопкой "по умолчанию"
    date_to: str


class ZskClassificationPayload(BaseModel):
    """
    Тело запроса на классификацию одной строки простоя ЗСК.
    Два режима, различаются is_manual:
      - is_manual=False (выбор из справочника): event + event_description
        должны точно совпадать с парой в zsk_downtime_classifier — сервер сам
        подставляет reason/factor/downtime_type и игнорирует то, что пришло
        в этих трёх полях в запросе.
      - is_manual=True ("Свой вариант"): event/event_description — свободный
        текст (в т.ч. пустой), reason/factor/downtime_type задаёт клиент
        напрямую и они обязательны.
    """
    key: str = Field(..., max_length=255, description='Ключ строки: "zsk_{zsk_id}_{start_local}"')
    event: str = Field("", max_length=255)
    event_description: str = Field("", max_length=1000)
    is_manual: bool = False
    reason: Optional[str] = Field(None, max_length=100)
    factor: Optional[str] = Field(None, max_length=100)
    downtime_type: Optional[str] = Field(None, max_length=50)


class ZskClassificationClearPayload(BaseModel):
    key: str = Field(..., max_length=255)


# =============================================================================
# ЭНДПОИНТЫ
# =============================================================================

@router.get("", summary="Простои ЗСК (FortMonitor) — закэшированные данные")
async def get_zsk_downtime(
    current_user: User = Depends(get_current_admin_user),
):
    raw = _redis.get(REDIS_KEY)
    date_from, date_to = get_zsk_date_range()
    payload = {"generated_at": None, "rows": [], "meta": {}} if not raw else json.loads(raw)
    payload["config"] = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    return payload


@router.get("/config", summary="Текущий диапазон дат расчёта")
async def get_config(
    current_user: User = Depends(get_current_admin_user),
):
    date_from, date_to = get_zsk_date_range()
    return {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}


@router.post("/config", summary="Сохранить диапазон дат расчёта (админ)")
async def post_config(
    payload: ZskDateRangePayload,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        date_from = date.fromisoformat(payload.date_from)
        date_to   = date.fromisoformat(payload.date_to)
    except ValueError:
        raise HTTPException(status_code=422, detail="Даты должны быть в формате YYYY-MM-DD")

    if date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from не может быть позже date_to")

    set_zsk_date_range(date_from, date_to)
    logger.info(f"zsk_downtime_config изменён администратором {current_user.fio}: {date_from} — {date_to}")
    return {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}


@router.post("/config/reset", summary="Сбросить диапазон дат к умолчанию — текущий день (админ)")
async def reset_config(
    current_user: User = Depends(get_current_admin_user),
):
    _redis.delete(REDIS_CONFIG_KEY)
    date_from, date_to = get_zsk_date_range()
    logger.info(f"zsk_downtime_config сброшен администратором {current_user.fio}")
    return {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}


# =============================================================================
# СПРАВОЧНИК КЛАССИФИКАТОРА (статичные данные, см. zsk_downtime_classifier.py)
# =============================================================================

@router.get("/classifier/events", summary="Список событий классификатора (первая ступень выбора)")
async def get_classifier_events(
    q: str = Query("", description="Подстрока для фильтрации (регистронезависимо)"),
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_admin_user),
):
    return {"events": classifier.search_events(q, limit=limit)}


@router.get("/classifier/descriptions", summary="Описания для выбранного события (вторая ступень выбора)")
async def get_classifier_descriptions(
    event: str = Query(..., description="Точное значение события из /classifier/events"),
    q: str = Query("", description="Подстрока для фильтрации по описанию"),
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_admin_user),
):
    return {"descriptions": classifier.get_descriptions(event, q, limit=limit)}


@router.get("/classifier/search", summary="Быстрый поиск по всему классификатору (событие + описание)")
async def search_classifier(
    q: str = Query("", description="Подстрока для поиска по событию и описанию"),
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_admin_user),
):
    return {"results": classifier.search_all(q, limit=limit)}


@router.get("/classifier/domains", summary="Допустимые значения причины/фактора/типа простоя (для подсказок при ручном вводе)")
async def get_classifier_domains(
    current_user: User = Depends(get_current_admin_user),
):
    return classifier.get_domains()


# =============================================================================
# КЛАССИФИКАЦИЯ СТРОК ПРОСТОЯ (событие/описание/причина/фактор/тип)
# ----------------------------------------------------------------------------
# Хранится в Redis отдельно от zsk_downtime_cache (см. REDIS_CLASSIFICATION_KEY
# выше) — переживает пересборку кэша Celery-задачей. HASH: поле = ключ строки
# "zsk_{zsk_id}_{start_local}", значение = JSON с классификацией.
# Видно всем пользователям (общий ключ, не привязан к сессии/браузеру) — как
# и статус "Отработано" в /api/violations/row-status.
# =============================================================================

@router.get("/classification", summary="Все сохранённые классификации строк простоя ЗСК (для всех пользователей)")
async def get_classifications(
    current_user: User = Depends(get_current_admin_user),
):
    """Возвращает {key: {event, eventDescription, reason, factor, downtimeType, isManual, fio, updatedAt}}."""
    raw = _redis.hgetall(REDIS_CLASSIFICATION_KEY)
    return {key: json.loads(value) for key, value in raw.items()}


@router.post("/classification", summary="Задать классификацию строки простоя ЗСК (видно всем пользователям)")
async def set_classification(
    payload: ZskClassificationPayload,
    current_user: User = Depends(get_current_admin_user),
):
    from app.bot_api.services import zsk as zsk_service

    result = zsk_service.set_classification(
        key=payload.key,
        event=payload.event,
        event_description=payload.event_description,
        is_manual=payload.is_manual,
        reason=payload.reason,
        factor=payload.factor,
        downtime_type=payload.downtime_type,
        user=current_user,
    )
    logger.info(
        f"zsk_downtime classification set: key={payload.key} "
        f"manual={payload.is_manual} by={current_user.fio}"
    )
    return result


@router.post("/classification/clear", summary="Сбросить классификацию строки простоя ЗСК (админ)")
async def clear_classification(
    payload: ZskClassificationClearPayload,
    current_user: User = Depends(get_current_admin_user),
):
    from app.bot_api.services import zsk as zsk_service

    result = zsk_service.clear_classification(payload.key)
    logger.info(f"zsk_downtime classification cleared: key={payload.key} by={current_user.fio}")
    return result


@router.post("/refresh", summary="Принудительно обновить простои ЗСК (админ)")
async def trigger_zsk_downtime_refresh(
    current_user: User = Depends(get_current_admin_user),
):
    """Немедленно запускает zsk_downtime_refresh через Celery."""
    try:
        from app.cropwise.tasks import zsk_downtime_refresh_task
        task = zsk_downtime_refresh_task.delay()
        logger.info(
            f"zsk_downtime_refresh triggered manually: task_id={task.id}, "
            f"by={current_user.fio}"
        )
        return {"status": "queued", "task_id": task.id}
    except Exception as e:
        logger.error(f"trigger_zsk_downtime_refresh: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи: {e}")
