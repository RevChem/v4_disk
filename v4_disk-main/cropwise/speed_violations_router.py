"""
cropwise/speed_violations_router.py
=====================================
Роутер вкладки «Превышение скорости»: НАДЁЖНЫЕ превышения скоростного
режима по всей технике с заданиями (не только уборка) — сверка max_speed
против нормы из справочника «Жатки» (по оборудованию, JSON-файл
speed_implements.json) и/или plan_speed_max из Cropwise,
только по часам с подтверждённой полевой работой (площадь, work_duration,
без дороги), max_speed из gps_logger_data_items в интервалах
machine_object_intersections (поля задачи). Логика — cropwise/speed_violations_sync.py.

Данные хранятся в Redis (ключ speed_violations_cache) и обновляются
Celery-задачей speed_violations_refresh раз в час (см.
celery_config.py). Статус строки «Отработано» переиспользует общую
таблицу violation_statuses и эндпоинты /api/violations/row-status — тот
же механизм, что и во всех остальных вкладках панели «Нарушения». Ключ
строки: "sp_{machine_id}_{task_id}_{hour_start_ts}" (уникален на
конкретный часовой бакет нарушения).

Эндпоинты:
  GET  /api/speed-violations               → закэшированные данные: rows (Жатки/plan_speed_max)
                                               и harvest_rows (уборка, оборудование вне справочника, 9 км/ч)
  POST /api/speed-violations/refresh        → запустить обновление немедленно (админ)
  GET  /api/speed-violations/refresh/status/{task_id} → статус этой задачи (для индикатора на кнопке)
  GET  /api/speed-violations/harvest-config → настройки правой таблицы «Уборка» (лимит, виды работ) + список видов работ
  PUT  /api/speed-violations/harvest-config → сохранить эти настройки (применяются при следующем пересчёте)
  GET  /api/speed-violations/norms-status   → сколько оборудования в справочнике «Жатки» и когда
                                               его последний раз меняли (админ)

Справочник «Жатки» (модалка на вкладке; раньше — Excel optim.xlsx; хранится в JSON-файле,
см. speed_implements_dao.py — /app/data/speed_implements.json):
  GET    /api/speed-violations/implements               → { items: [...], day_max_* }
                                                           (в каждом item поле day_max — макс. скорость за окно
                                                           проверки, время и ссылка на задание, либо null)
  POST   /api/speed-violations/implements               → добавить оборудование
  PUT    /api/speed-violations/implements/{id}          → изменить
  DELETE /api/speed-violations/implements/{id}          → удалить
  POST   /api/speed-violations/implements/import-excel  → добавить недостающее из optim.xlsx
Изменения справочника попадают в проверку скорости при следующем обновлении
вкладки (раз в час или кнопка «Обновить»).

Макс. скорости за произвольный день (кнопка «Макс. скорости за день»):
  POST /api/speed-violations/day-max                    → поставить расчёт в очередь
                                                           {day, implement_ids[], include_harvest}
  GET  /api/speed-violations/day-max/status/{task_id}   → статус и результат
Кэш вкладки и колонку «Макс. скорость» в «Жатках» этот расчёт не меняет; результат
лежит в Redis (speed_day_max_result:{task_id}) один час.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone, timedelta

import redis
from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.auth.dependencies import get_current_admin_user
from app.users.models import User
from app.logging_config import get_logger
from app.config import get_redis_url
from app.cropwise import speed_implements_dao as implements_dao
from app.cropwise.schemas import SpeedImplementIn

logger = get_logger("cropwise.speed_violations")

router = APIRouter(prefix="/api/speed-violations", tags=["Превышение скорости"])

REDIS_KEY = "speed_violations_cache"
REDIS_IMPL_MAX_KEY = "speed_implement_max_cache"
REDIS_HARVEST_CONFIG_KEY = "speed_harvest_config"
REDIS_WORK_TYPES_KEY = "speed_work_types_cache"
DAY_MAX_RESULT_KEY = "speed_day_max_result:{}"
DAY_MAX_RESULT_TTL_SEC = 3600
WORK_TYPES_TTL_SEC = 6 * 3600

# Значения по умолчанию для правой таблицы «Уборка: прочее оборудование».
# Виды работ группы «5.0 Уборка» (+ «5.4 Уборка картофеля, лука» = id 76) — те же id, что в
# CHECK1_WORK_GROUPS (violations_sync.py) и TARGET_WORK_GROUPS["Уборка"] (vehicle_check_sync.py).
DEFAULT_HARVEST_CONFIG: dict = {
    "max_speed_kmh":        9.0,
    "work_type_ids":        [6, 7, 75, 76, 77, 111, 118, 125, 126, 145],
    # Учитывать ли задания без оборудования (самоходный комбайн работает без навесного).
    "include_no_implement": True,
}
TZ_MSK    = timezone(timedelta(hours=3))

_redis = redis.from_url(get_redis_url(), decode_responses=True)


def get_harvest_config() -> dict:
    """Настройки правой таблицы («Уборка»). Нет/битый ключ в Redis — значения по умолчанию."""
    cfg = dict(DEFAULT_HARVEST_CONFIG)
    cfg["work_type_ids"] = list(DEFAULT_HARVEST_CONFIG["work_type_ids"])
    raw = _redis.get(REDIS_HARVEST_CONFIG_KEY)
    if not raw:
        return cfg
    try:
        data = json.loads(raw)
        cfg["max_speed_kmh"] = float(data.get("max_speed_kmh", cfg["max_speed_kmh"]))
        ids = [int(x) for x in (data.get("work_type_ids") or [])]
        if ids:
            cfg["work_type_ids"] = sorted(set(ids))
        cfg["include_no_implement"] = bool(data.get("include_no_implement", cfg["include_no_implement"]))
    except Exception as e:
        logger.error(f"get_harvest_config: parse error, использую значения по умолчанию: {e}")
    return cfg


def set_harvest_config(cfg: dict) -> None:
    _redis.set(REDIS_HARVEST_CONFIG_KEY, json.dumps(cfg, ensure_ascii=False))
    logger.info(f"speed harvest config saved: {cfg}")


def update_cache(
    rows: list[dict],
    generated_at: datetime,
    harvest_rows: list[dict] | None = None,
    stats: dict | None = None,
    harvest_config: dict | None = None,
) -> None:
    """rows — превышения по «Жаткам»/plan_speed_max (левая таблица),
    harvest_rows — уборка, оборудование вне справочника (правая).
    stats — показатели прогона для индикатора «здоровья» на вкладке
    (tasks, checked_tasks, work_items, duration_sec); prev_tasks подставляется
    из предыдущего снимка — чтобы заметить резкое падение объёма выгрузки."""
    harvest_rows = harvest_rows or []
    stats = dict(stats or {})
    try:
        prev = json.loads(_redis.get(REDIS_KEY) or "{}").get("stats") or {}
        if prev.get("tasks") is not None:
            stats["prev_tasks"] = prev["tasks"]
    except Exception:
        pass
    data = {
        "rows":           rows,
        "harvest_rows":   harvest_rows,
        "generated_at":   generated_at.strftime("%d.%m.%Y %H:%M:%S"),
        "generated_ts":   int(generated_at.timestamp()),
        "stats":          stats,
        "harvest_config": harvest_config or get_harvest_config(),
    }
    _redis.set(REDIS_KEY, json.dumps(data, ensure_ascii=False, default=str))
    logger.info(
        f"speed_violations cache updated: {len(rows)} rows, {len(harvest_rows)} harvest rows, "
        f"stats={stats}, at {data['generated_at']}"
    )


def update_implement_max_cache(items: dict[int, dict], window_start: datetime, generated_at: datetime) -> None:
    """Максимальная надёжная скорость по каждому оборудованию за окно проверки
    (колонка «Макс. скорость» в модалке «Жатки»). items: {implement_id: {...}}."""
    data = {
        "generated_at": generated_at.strftime("%d.%m.%Y %H:%M:%S"),
        "window_start": window_start.strftime("%d.%m %H:%M"),
        "items":        {str(k): v for k, v in items.items()},
    }
    _redis.set(REDIS_IMPL_MAX_KEY, json.dumps(data, ensure_ascii=False, default=str))
    logger.info(f"speed_implement_max cache updated: {len(items)} implements at {data['generated_at']}")


def _load_implement_max() -> dict:
    empty = {"generated_at": None, "window_start": None, "items": {}}
    raw = _redis.get(REDIS_IMPL_MAX_KEY)
    if not raw:
        return empty
    try:
        return json.loads(raw)
    except ValueError:
        return empty


@router.get("", summary="Превышение скоростного режима на уборке")
async def get_speed_violations(
    current_user: User = Depends(get_current_admin_user),
):
    """
    Возвращает закэшированный список нарушений скоростного режима на
    уборке (см. cropwise/speed_violations_sync.py). Если кэш ещё пуст
    (сервис только что перезапущен) — rows=[].
    """
    raw = _redis.get(REDIS_KEY)
    if not raw:
        return {"generated_at": None, "rows": [], "harvest_rows": [],
                "stats": None, "harvest_config": get_harvest_config()}
    data = json.loads(raw)
    data.setdefault("harvest_rows", [])   # кэш, записанный до появления правой таблицы
    data.setdefault("stats", None)
    data.setdefault("harvest_config", get_harvest_config())
    return data


@router.post("/refresh", summary="Принудительно обновить проверку скорости (админ)")
async def trigger_speed_violations_refresh(
    current_user: User = Depends(get_current_admin_user),
):
    """Немедленно запускает speed_violations_refresh через Celery."""
    try:
        from app.cropwise.tasks import speed_violations_refresh_task
        task = speed_violations_refresh_task.delay()
        logger.info(
            f"speed_violations_refresh triggered manually: task_id={task.id}, "
            f"by={current_user.fio}"
        )
        return {"status": "queued", "task_id": task.id}
    except Exception as e:
        logger.error(f"trigger_speed_violations_refresh: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи: {e}")


@router.get("/refresh/status/{task_id}", summary="Статус задачи обновления проверки скорости (админ)")
async def get_speed_refresh_status(
    task_id: str,
    current_user: User = Depends(get_current_admin_user),
):
    """Поллинг Celery-задачи speed_violations_refresh (кнопки «Обновить» / «Обновить скорости»)."""
    try:
        result = AsyncResult(task_id)
        state = result.state
        if state == "SUCCESS":
            res = result.result
            if isinstance(res, dict) and res.get("skipped"):
                return {"status": "SKIPPED", "reason": res.get("reason", "already running")}
            return {"status": "SUCCESS", "result": res}
        if state == "FAILURE":
            return {"status": "FAILURE", "error": str(result.result)}
        if state in ("STARTED", "PROGRESS"):
            return {"status": "RUNNING"}
        return {"status": "PENDING"}
    except Exception as e:
        logger.error(f"speed refresh status error: {e}")
        return {"status": "UNKNOWN", "error": str(e)}


# ============================================================================
# Настройки правой таблицы «Уборка: прочее оборудование»
# ============================================================================

class HarvestConfigIn(BaseModel):
    max_speed_kmh:        float = Field(..., gt=0, le=100, description="Предельная скорость, км/ч")
    work_type_ids:        list[int] = Field(..., min_length=1, description="id видов работ, считающихся «Уборкой»")
    include_no_implement: bool = True

    @field_validator("work_type_ids")
    @classmethod
    def _ids_ok(cls, v):
        ids = sorted({int(x) for x in v})
        if any(x <= 0 for x in ids):
            raise ValueError("id вида работ должен быть положительным числом")
        return ids


async def _load_work_types() -> list[dict]:
    """Список видов работ Cropwise [{id, name}] для окна настроек. Кэш в Redis на 6 часов."""
    raw = _redis.get(REDIS_WORK_TYPES_KEY)
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            pass
    import aiohttp
    from app.cropwise.client import CROPWISE_HEADERS, _fetch_all_pages, _CROPWISE_TIMEOUT
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        wt_raw = await _fetch_all_pages(session, "work_types")
    items = sorted(
        (
            {"id": int(w["id"]), "name": (w.get("name") or w.get("title") or f"wt_{w['id']}")}
            for w in wt_raw if w.get("id") is not None
        ),
        key=lambda x: x["name"].lower(),
    )
    _redis.set(REDIS_WORK_TYPES_KEY, json.dumps(items, ensure_ascii=False), ex=WORK_TYPES_TTL_SEC)
    return items


@router.get("/harvest-config", summary="Настройки правой таблицы «Уборка» (админ)")
async def get_harvest_config_endpoint(
    current_user: User = Depends(get_current_admin_user),
):
    """Текущие настройки + список видов работ Cropwise для выбора."""
    try:
        work_types = await _load_work_types()
        wt_error = None
    except Exception as e:
        logger.error(f"harvest-config: не удалось загрузить work_types: {e}", exc_info=True)
        work_types, wt_error = [], f"Не удалось загрузить список видов работ из Cropwise: {e}"
    cfg = get_harvest_config()
    known = {w["id"] for w in work_types}
    # id из конфига, которых нет в списке (Cropwise недоступен / вид работ удалён) — не теряем.
    for wid in cfg["work_type_ids"]:
        if wid not in known:
            work_types.append({"id": wid, "name": f"вид работ #{wid}"})
    return {
        "config":     cfg,
        "defaults":   DEFAULT_HARVEST_CONFIG,
        "work_types": work_types,
        "warning":    wt_error,
    }


@router.put("/harvest-config", summary="Сохранить настройки правой таблицы «Уборка» (админ)")
async def put_harvest_config(
    payload: HarvestConfigIn,
    current_user: User = Depends(get_current_admin_user),
):
    cfg = {
        "max_speed_kmh":        round(float(payload.max_speed_kmh), 1),
        "work_type_ids":        payload.work_type_ids,
        "include_no_implement": payload.include_no_implement,
    }
    set_harvest_config(cfg)
    logger.info(
        f"speed harvest config updated by {current_user.fio}: "
        f"limit={cfg['max_speed_kmh']}, work_types={len(cfg['work_type_ids'])}, "
        f"no_impl={cfg['include_no_implement']}"
    )
    return {"config": cfg}


@router.get("/norms-status", summary="Статус справочника «Жатки» (админ)")
async def get_norms_status(
    current_user: User = Depends(get_current_admin_user),
):
    """Сколько оборудования в справочнике «Жатки» и когда его последний раз
    меняли — для строки статуса над таблицей."""
    return await implements_dao.get_status()


# ============================================================================
# Справочник «Жатки»: оборудование под контролем скорости
# ============================================================================

@router.get("/implements", summary="Справочник «Жатки»: список оборудования (админ)")
async def list_speed_implements(
    current_user: User = Depends(get_current_admin_user),
):
    items = await implements_dao.list_implements()
    mx = _load_implement_max()
    by_impl = mx.get("items") or {}
    for it in items:
        # None — оборудование не работало / нет надёжных замеров за окно
        it["day_max"] = by_impl.get(str(it["implement_id"]))
    return {
        "items": items,
        "day_max_generated_at": mx.get("generated_at"),
        "day_max_window_start": mx.get("window_start"),
    }


@router.post("/implements", summary="Справочник «Жатки»: добавить оборудование (админ)")
async def create_speed_implement(
    payload: SpeedImplementIn,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        item = await implements_dao.create_implement(payload.model_dump(), current_user.id, current_user.fio)
    except implements_dao.ImplementExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info(
        f"speed_implements: добавлено implement_id={item['implement_id']} "
        f"speed_max={item['speed_max']}, by={current_user.fio}"
    )
    return item


@router.put("/implements/{item_id}", summary="Справочник «Жатки»: изменить оборудование (админ)")
async def update_speed_implement(
    item_id: int,
    payload: SpeedImplementIn,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        item = await implements_dao.update_implement(item_id, payload.model_dump(), current_user.id, current_user.fio)
    except implements_dao.ImplementExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except implements_dao.ImplementNotFoundError:
        raise HTTPException(status_code=404, detail="Запись не найдена (возможно, её уже удалили)")
    logger.info(
        f"speed_implements: изменено id={item_id} implement_id={item['implement_id']} "
        f"speed_max={item['speed_max']}, by={current_user.fio}"
    )
    return item


@router.delete("/implements/{item_id}", summary="Справочник «Жатки»: удалить оборудование (админ)")
async def delete_speed_implement(
    item_id: int,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        await implements_dao.delete_implement(item_id)
    except implements_dao.ImplementNotFoundError:
        raise HTTPException(status_code=404, detail="Запись не найдена (возможно, её уже удалили)")
    logger.info(f"speed_implements: удалено id={item_id}, by={current_user.fio}")
    return {"ok": True}


@router.post("/implements/import-excel", summary="Справочник «Жатки»: добавить недостающее из optim.xlsx (админ)")
async def import_speed_implements_from_excel(
    current_user: User = Depends(get_current_admin_user),
):
    """Читает backend/app/cropwise/optim.xlsx и добавляет в справочник только то
    оборудование, которого в нём ещё нет (по ID). Существующие записи не
    трогает — ручные правки в модалке важнее файла."""
    import os
    from app.cropwise.speed_norms_excel import NORMS_PATH

    if not os.path.exists(NORMS_PATH):
        raise HTTPException(status_code=404, detail="Файл optim.xlsx не найден на сервере")
    try:
        result = await implements_dao.import_from_excel(NORMS_PATH, current_user.id, current_user.fio)
    except RuntimeError as e:  # не нашли обязательные колонки в заголовке
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"speed_implements import-excel: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Не удалось прочитать optim.xlsx: {e}")
    logger.info(
        f"speed_implements import-excel: added={result['added']}, "
        f"skipped={result['skipped_existing']}, broken={len(result['broken'])}, by={current_user.fio}"
    )
    return result


# ============================================================================
# Макс. скорости за произвольный день
# ============================================================================

class DayMaxIn(BaseModel):
    day: date
    implement_ids: list[int] = Field(default_factory=list, description="id выбранных жаток из справочника")
    include_harvest: bool = Field(False, description="Всё оборудование на видах работ «Уборки»")

    @field_validator("implement_ids")
    @classmethod
    def _ids_ok(cls, v):
        ids = sorted({int(x) for x in v})
        if any(x <= 0 for x in ids):
            raise ValueError("id оборудования должен быть положительным числом")
        return ids


def store_day_max_result(run_id: str, data: dict) -> None:
    _redis.set(
        DAY_MAX_RESULT_KEY.format(run_id),
        json.dumps(data, ensure_ascii=False, default=str),
        ex=DAY_MAX_RESULT_TTL_SEC,
    )


@router.post("/day-max", summary="Макс. скорости оборудования за выбранный день (админ)")
async def trigger_day_max(
    payload: DayMaxIn,
    current_user: User = Depends(get_current_admin_user),
):
    if not payload.implement_ids and not payload.include_harvest:
        raise HTTPException(status_code=422, detail="Выберите жатки или включите «Всё оборудование на уборке»")
    if payload.day > datetime.now(tz=TZ_MSK).date():
        raise HTTPException(status_code=422, detail="Этот день ещё не наступил")
    try:
        from app.cropwise.tasks import speed_day_max_task
        run_id = uuid.uuid4().hex
        speed_day_max_task.apply_async(
            args=[payload.day.isoformat(), payload.implement_ids, payload.include_harvest, run_id],
            task_id=run_id,
        )
        logger.info(
            f"speed_day_max queued: task_id={run_id}, day={payload.day}, "
            f"implements={len(payload.implement_ids)}, harvest={payload.include_harvest}, by={current_user.fio}"
        )
        return {"status": "queued", "task_id": run_id}
    except Exception as e:
        logger.error(f"trigger_day_max: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи: {e}")


@router.get("/day-max/status/{task_id}", summary="Статус расчёта макс. скоростей за день и его результат (админ)")
async def get_day_max_status(
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
            raw = _redis.get(DAY_MAX_RESULT_KEY.format(task_id))
            if not raw:
                return {"status": "FAILURE", "error": "Результат устарел — запустите расчёт заново"}
            return {"status": "SUCCESS", "result": json.loads(raw)}
        if state == "FAILURE":
            return {"status": "FAILURE", "error": str(result.result)}
        if state in ("STARTED", "PROGRESS"):
            return {"status": "RUNNING"}
        return {"status": "PENDING"}
    except Exception as e:
        logger.error(f"speed day-max status error: {e}")
        return {"status": "UNKNOWN", "error": str(e)}
