"""
cropwise/router.py
~~~~~~~~~~~~~~~~~~
Эндпоинты для получения данных из Cropwise Operations Platform.
"""

from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.dependencies import get_current_admin_user, get_current_user
from app.base import BaseDao
from app.cropwise.assignment_model import UserAssignment
from app.cropwise.client import (
    ALLOWED_SUBTYPES,
    MACHINE_SUBTYPE_LABELS,
    WORK_TYPE_LABELS,
    fetch_users_with_machines,
)
from app.cropwise.schemas import (
    CropwiseUserItem,
    CropwiseUsersFilters,
    CropwiseUsersResponse,
)
from app.cropwise.sync import sync_users_from_cropwise, sync_vehicles_from_cropwise
from app.cropwise.sync_assignments import sync_user_assignments
from app.logging_config import get_logger
from app.users.models import User

logger = get_logger("cropwise.router")
router = APIRouter(prefix="/api/cropwise", tags=["Cropwise"])

_SUBTYPE_ENUM = ", ".join(sorted(ALLOWED_SUBTYPES))
_WORK_TYPE_ENUM = ", ".join(sorted(WORK_TYPE_LABELS.keys()))


# ── DAO ───────────────────────────────────────────────────────────────────────

class UserAssignmentDAO(BaseDao):
    model = UserAssignment


# ── Схема ответа для my-assignment ───────────────────────────────────────────

class MyAssignmentResponse(BaseModel):
    """
    Текущее назначение пользователя из Cropwise.
    Все поля nullable — если Cropwise не вернул назначение,
    возвращаем объект с null-полями (не 404), чтобы фронт
    знал: синхронизация прошла, просто задачи нет.
    """
    has_assignment:        bool
    registration_number:   Optional[str]      = None
    machine_subtype:       Optional[str]      = None
    machine_subtype_label: Optional[str]      = None
    work_type:             Optional[str]      = None
    task_label:            Optional[str]      = None
    machine_task_id:       Optional[int]      = None
    synced_at:             Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── Эндпоинты ────────────────────────────────────────────────────────────────

@router.get(
    "/vehicle-types",
    summary="Список типов техники для выпадающих списков",
    description="Возвращает типы техники (machine_subtype) для селектов в формах.",
)
async def get_vehicle_types(current_user: User = Depends(get_current_user)):
    """Эндпоинт для фронта: выбор типа техники при начале/редактировании простоя."""
    items = [
        {"value": s, "label": MACHINE_SUBTYPE_LABELS.get(s, s)}
        for s in sorted(ALLOWED_SUBTYPES)
    ]
    return {"items": items, "labels": dict(MACHINE_SUBTYPE_LABELS)}


@router.get(
    "/my-assignment",
    response_model=MyAssignmentResponse,
    summary="Текущее назначение пользователя (машина + задача из Cropwise)",
    description=(
        "Возвращает машину и задачу, закреплённые за текущим пользователем "
        "в Cropwise. Данные обновляются автоматически каждые 10 минут Celery-задачей. "
        "Если назначения нет — возвращает has_assignment=false, поля null. "
        "Фронт использует эти данные для автозаполнения формы простоя."
    ),
)
async def get_my_assignment(current_user: User = Depends(get_current_user)):
    assignment = await UserAssignmentDAO.find_one_or_none(user_id=current_user.id)

    # Запись ещё не создана (первый вход до первой синхронизации)
    # или машины нет (пользователь не в активных задачах Cropwise)
    if assignment is None or assignment.registration_number is None:
        return MyAssignmentResponse(
            has_assignment=False,
            synced_at=assignment.synced_at if assignment else None,
        )

    return MyAssignmentResponse(
        has_assignment=True,
        registration_number=assignment.registration_number,
        machine_subtype=assignment.machine_subtype,
        machine_subtype_label=assignment.machine_subtype_label,
        work_type=assignment.work_type,
        task_label=assignment.task_label,
        machine_task_id=assignment.machine_task_id,
        synced_at=assignment.synced_at,
    )


@router.get(
    "/users",
    response_model=CropwiseUsersResponse,
    summary="Пользователи с машинами и заданиями из Cropwise (админ)",
    description=(
        "Возвращает список пользователей с привязанными машинами и заданиями. "
        "Все параметры опциональны. "
        f"Допустимые значения `machine_subtype`: {_SUBTYPE_ENUM}. "
        f"Примеры значений `work_type`: {_WORK_TYPE_ENUM}."
    ),
)
async def get_cropwise_users(
    hours: Annotated[
        Optional[int],
        Query(ge=1, le=8760, description="Фильтр по последним N часам."),
    ] = None,
    username: Annotated[
        Optional[str],
        Query(description="Фильтр по ФИО (подстрока, без учёта регистра).", max_length=100),
    ] = None,
    machine_subtype: Annotated[
        Optional[str],
        Query(description=f"Фильтр по подтипу машины. Допустимые: `{_SUBTYPE_ENUM}`.", max_length=50),
    ] = None,
    work_type: Annotated[
        Optional[str],
        Query(description="Фильтр по типу работы (подстрока).", max_length=100),
    ] = None,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        rows = await fetch_users_with_machines(
            hours=hours,
            filter_username=username,
            filter_machine_subtype=machine_subtype,
            filter_work_type=work_type,
        )
    except Exception as e:
        logger.error(f"Error fetching Cropwise users: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка при получении данных из Cropwise: {e}")

    items = [
        CropwiseUserItem(
            username=r["username"],
            phone=r["phone"],
            reg_number=r["reg_number"],
            machine_subtype=r["machine_subtype"],
            work_type=r["work_type"],
            task_label=r["task_label"],
        )
        for r in rows
    ]

    return CropwiseUsersResponse(
        total=len(items),
        filters=CropwiseUsersFilters(
            hours=hours,
            filter_username=username,
            machine_subtype=machine_subtype,
            work_type=work_type,
        ),
        items=items,
    )


@router.post(
    "/sync/users",
    summary="Синхронизация пользователей из Cropwise в БД (админ)",
    description="Загружает пользователей из Cropwise API и создаёт/обновляет записи в таблице users.",
)
async def post_sync_users(current_user: User = Depends(get_current_admin_user)):
    try:
        from app.cropwise.tasks import sync_users_task
        task = sync_users_task.delay()
        logger.info(f"sync_cropwise_users queued: task_id={task.id}")
        return {"task_id": task.id, "status": "queued"}
    except Exception as e:
        logger.error(f"Error queuing sync_cropwise_users: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи в очередь: {e}")


@router.post(
    "/sync/vehicles",
    summary="Синхронизация транспорта из Cropwise в БД (админ)",
)
async def post_sync_vehicles(current_user: User = Depends(get_current_admin_user)):
    try:
        from app.cropwise.tasks import sync_vehicles_task
        task = sync_vehicles_task.delay()
        logger.info(f"sync_cropwise_vehicles queued: task_id={task.id}")
        return {"task_id": task.id, "status": "queued"}
    except Exception as e:
        logger.error(f"Error queuing sync_cropwise_vehicles: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи в очередь: {e}")


@router.post(
    "/sync/assignments",
    summary="Принудительная синхронизация назначений (админ)",
    description=(
        "Немедленно запускает синхронизацию назначений из Cropwise "
        "для всех пользователей. В штатном режиме это делает Celery каждые 15 минут."
    ),
)
async def post_sync_assignments(current_user: User = Depends(get_current_admin_user)):
    try:
        from app.cropwise.tasks import sync_user_assignments_task
        task = sync_user_assignments_task.delay()
        logger.info(f"sync_user_assignments queued: task_id={task.id}")
        return {"task_id": task.id, "status": "queued"}
    except Exception as e:
        logger.error(f"Error queuing sync_user_assignments: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи в очередь: {e}")


@router.post(
    "/sync/agrofirms",
    summary="Запустить синхронизацию агрофирм техники через Celery (админ)",
    description=(
        "Ставит в очередь Celery задачу sync_vehicle_agrofirms, которая обновляет поле agrofirm "
        "у каждой единицы техники по логике: field → field_group → group_folder → агрофирма. "
        "В штатном режиме запускается раз в сутки."
    ),
)
async def post_sync_agrofirms(current_user: User = Depends(get_current_admin_user)):
    try:
        from app.cropwise.tasks import sync_vehicle_agrofirms_task
        task = sync_vehicle_agrofirms_task.delay()
        logger.info(f"sync_vehicle_agrofirms queued: task_id={task.id}")
        return {"task_id": task.id, "status": "queued"}
    except Exception as e:
        logger.error(f"Error queuing sync_vehicle_agrofirms: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи в очередь: {e}")
