"""
cropwise/violations_sync.py
============================
Логика обновления кэша нарушений:
  check1 — нет агроопераций к полям
  check3 — не указан механизатор
  check4 — не привязано оборудование

Один вызов refresh_violations() загружает справочники и вычисляет
запрошенные checks (или все, если checks=None), сохраняя в Redis через
violations_router.merge_cache() — частичное обновление ключей кэша.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

ProgressFn = Callable[[str], None] | None

import aiohttp

from app.cropwise.client import (
    CROPWISE_HEADERS,
    FIO_NOT_FOUND,
    _fetch_all_pages,
    _fetch_history_items_for_date_range,
    _fetch_mass_request,
    _fetch_mtfmi_by_task_ids,
    _fetch_task_agro_op_links_by_task_ids,
    fetch_local_fio_map,
)
from app.forbidden_ops.router import get_forbidden_ops_config
from app.logging_config import get_logger

logger = get_logger("cropwise.violations_sync")


def _emit_progress(progress: ProgressFn, message: str) -> None:
    if not progress:
        return
    try:
        progress(message)
    except Exception:
        logger.debug("progress callback failed", exc_info=True)

# machine_group_id машин-наёмников (как в plans_1). check3 («Нет механизатора»)
# к наёмной технике не применяется — механизатора наёмника в Cropwise не ведут.
HIRED_GROUP_ID = 7

# Мин. выработка на поле в задании, чтобы учитывать поле (check3/4/5).
# Раньше было 0.5 га — задачи с 0.1–0.5 га не попадали в «Нет механизатора».
MIN_FIELD_AREA_HA = 0.1

# check3: суммарный порог по задаче
CHECK3_MIN_FIELD_AREA_HA = MIN_FIELD_AREA_HA
CHECK3_MIN_TOTAL_AREA_HA = MIN_FIELD_AREA_HA

TZ_MSK         = timezone(timedelta(hours=3))
DAY_START_HOUR = 6
SEMAPHORE_LIMIT = 20
# check1 mapping/mass_request — отдельный лимит параллелизма к Cropwise.
CHECK1_SEMAPHORE_LIMIT = 20
CROPWISE_TASK_BASE = "https://operations.cropwise.com/machines"

FIRM_TO_FOLDER = {
    "АФ Чистопольская": 3, "АгроРаздолье": 4, "Аксу-Агро": 5,
    "НурлатАгро": 11, "Дрожжаное Рассвет Агро": 6,
    "Актай": 7, "Лениногорская Птицефабрика": 9,
}
FOLDER_TO_FIRM = {v: k for k, v in FIRM_TO_FOLDER.items()}
FIRM_ORDER = [
    "АФ Чистопольская", "АгроРаздолье", "Аксу-Агро",
    "НурлатАгро", "Дрожжаное Рассвет Агро", "Актай",
    "Лениногорская Птицефабрика",
]

# Виды работ, начинающиеся на "7" или "8" — транспортные, исключаем
_TRANSPORT_PREFIXES = ("7", "8")

# =============================================================================
# CHECK1 — ВИДЫ РАБОТ И ИСКЛЮЧЕНИЯ (перенесено 1:1 из check1_no_agro_ops.py)
# =============================================================================

CHECK1_WORK_GROUPS: list[tuple[str, list[int]]] = [
    ("1.1 Внесение минеральных удобрений", [55]),
    ("2.1 Боронование",                    [103, 81, 97, 12]),
    ("2.9 Дискование",                     [60]),
    ("2.4 Вспашка",                        [49, 57, 171]),
    ("2.11 Культивация",                   [20, 50, 52, 63, 96]),
    ("4.0 Посев",                          [3, 4, 69, 70, 71, 72, 119, 120, 159, 160, 161, 163]),
    ("4.1 Посев (овощи)",                  [68, 148, 172]),
    ("3.1 Опрыскивание",                   [1, 64, 65, 66, 153]),
    ("2.12 Прикатывание",                  [13, 67, 142]),
    ("2.7 Гребнеобразование",              [62]),
    ("2.6 Фрезерование",                   [73]),
    ("2.8 Глубокорыхление",                [11, 106, 121, 150]),
    ("5.0 Уборка",                         [6, 7, 75, 76, 77, 111, 118,
                                            125, 126, 145]),
    ("5.4 Уборка картофеля, лука",         [76]),
]

# Виды работ, ИСКЛЮЧЁННЫЕ из проверки check1:
#   (ранее исключались вспашка, гребнеобразование, глубокорыхление,
#   посев (овощи), уборка (5.x) — теперь эти виды работ тоже проверяются)
_CHECK1_EXCLUDED_GROUP_NAMES: set[str] = set()
CHECK1_EXCLUDED_WORK_TYPE_IDS: frozenset[int] = frozenset(
    wt_id
    for name, ids in CHECK1_WORK_GROUPS
    if name in _CHECK1_EXCLUDED_GROUP_NAMES
    for wt_id in ids
)

# Минимальная выработка на поле в задании, чтобы проверять агрооперацию (check1)
CHECK1_MIN_FIELD_AREA_HA = 0.01


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def _parse_dt(s) -> Optional[datetime]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        ts = float(s)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except Exception:
            return None
    s = str(s).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=TZ_MSK) if dt.tzinfo is None else dt.astimezone(TZ_MSK)


def _parse_hourly_key(s) -> Optional[datetime]:
    """Ключ часа из covered_area_hourly / work_duration_hourly — наивный
    'YYYY-MM-DD HH:MM:SS' (локальное время Cropwise), не ISO hour_start GPS."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_MSK)
    except ValueError:
        return _parse_dt(s)


def _hourly_entries(raw) -> list[tuple]:
    if not raw:
        return []
    if isinstance(raw, dict):
        return list(raw.items())
    if isinstance(raw, list):
        return [
            (e[0], e[1]) for e in raw
            if isinstance(e, (list, tuple)) and len(e) >= 2
        ]
    return []


def _shift_window(report_date: date):
    day_start = datetime(
        report_date.year, report_date.month, report_date.day,
        DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    ) - timedelta(days=1)                  # ← сдвигаем начало на сутки назад
    day_end = day_start + timedelta(days=2) # ← окно = 2 дня
    now     = datetime.now(tz=TZ_MSK)
    cutoff  = min(day_end, now)
    return day_start, cutoff


# check1 нужен отдельный, более широкий горизонт: задача без агрооперации
# не перестаёт быть нарушением через 2 дня, поэтому check1 смотрит дальше
# 2-дневного окна — отдельным запросом, не трогая остальные чеки и GPS.
CHECK1_LOOKBACK_DAYS = 16

# Сколько work_type_id передавать в одном GET machine_tasks (через запятую).
# Если API не примет список — fallback на один work_type_id на запрос.
CHECK1_WT_ID_BATCH_SIZE = 25


def _check1_window(report_date: date):
    day_start = datetime(
        report_date.year, report_date.month, report_date.day,
        DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    ) - timedelta(days=CHECK1_LOOKBACK_DAYS)
    day_end = day_start + timedelta(days=CHECK1_LOOKBACK_DAYS + 1)  # запас, чтобы cutoff = now
    now     = datetime.now(tz=TZ_MSK)
    cutoff  = min(day_end, now)
    return day_start, cutoff


def _folder_parent_map(gfs: list[dict]) -> dict[int, Optional[int]]:
    out: dict[int, Optional[int]] = {}
    for gf in gfs:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = (gf.get("parent_id") or gf.get("parent_group_folder_id")
               or gf.get("group_folder_parent_id"))
        out[int(fid)] = int(pid) if pid is not None else None
    return out


def _resolve_firm(folder_id, parent_map, root_ids):
    if folder_id is None:
        return None
    cur, seen = int(folder_id), set()
    for _ in range(64):
        if cur in root_ids:
            return cur
        if cur in seen:
            return None
        seen.add(cur)
        nxt = parent_map.get(cur)
        if nxt is None:
            return None
        cur = int(nxt)
    return None


def _build_field_to_firm(fields_all, fgs_all, gfs_all) -> dict[int, str]:
    fgs_map    = {int(fg["id"]): fg for fg in fgs_all if fg.get("id") is not None}
    parent_map = _folder_parent_map(gfs_all)
    root_ids   = set(FOLDER_TO_FIRM.keys())
    out: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        raw_folder = f.get("group_folder_id")
        fgid = f.get("field_group_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(int(fgid), {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None
        root = _resolve_firm(rint, parent_map, root_ids)
        if root is not None:
            firm = FOLDER_TO_FIRM.get(root)
            if firm:
                out[int(fid)] = firm
    return out


def _is_transport_work_type(wt_name: str) -> bool:
    return wt_name.strip().startswith(_TRANSPORT_PREFIXES)


def _check1_allowed_work_type_ids(work_types_raw: list[dict]) -> list[int]:
    """work_type_id для machine_tasks API: всё кроме транспорта (7.x/8.x) и CHECK1_EXCLUDED."""
    allowed: list[int] = []
    for w in work_types_raw:
        wid = w.get("id")
        if wid is None:
            continue
        wid = int(wid)
        name = (w.get("name") or w.get("title") or "").strip()
        if _is_transport_work_type(name):
            continue
        if wid in CHECK1_EXCLUDED_WORK_TYPE_IDS:
            continue
        allowed.append(wid)
    return allowed


async def _fetch_check1_machine_tasks(
    session: aiohttp.ClientSession,
    start_str: str,
    end_str: str,
    work_type_ids: list[int],
) -> list[dict]:
    """machine_tasks за окно check1 с фильтром work_type_id на стороне API.

    Сначала пробуем batch work_type_id=1,2,3,...; при пустом ответе — по одному id
    на запрос (как work_time_report_router).
    """
    if not work_type_ids:
        return []

    base_params = {
        "start_time_gt_eq": start_str,
        "start_time_lt":    end_str,
    }
    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
    batches = [
        work_type_ids[i:i + CHECK1_WT_ID_BATCH_SIZE]
        for i in range(0, len(work_type_ids), CHECK1_WT_ID_BATCH_SIZE)
    ]

    async def _fetch_batch(batch: list[int]) -> list[dict]:
        async with sem:
            return await _fetch_all_pages(
                session,
                "machine_tasks",
                {
                    **base_params,
                    "work_type_id": ",".join(str(wid) for wid in batch),
                },
                sem=sem,
            )

    async def _fetch_one_wt(wt_id: int) -> list[dict]:
        async with sem:
            return await _fetch_all_pages(
                session,
                "machine_tasks",
                {**base_params, "work_type_id": wt_id},
                sem=sem,
            )

    batch_pages = await asyncio.gather(
        *[_fetch_batch(batch) for batch in batches],
        return_exceptions=True,
    )

    all_tasks: list[dict] = []
    failed_batches: list[list[int]] = []
    for batch, result in zip(batches, batch_pages):
        if isinstance(result, BaseException):
            logger.warning(
                f"check1 machine_tasks batch wt (n={len(batch)}) failed: {result}"
            )
            failed_batches.append(batch)
            continue
        all_tasks.extend(result)

    if not all_tasks:
        logger.info(
            "check1 machine_tasks: batch work_type_id filter returned 0, "
            "fallback to one request per work_type_id"
        )
        per_wt_pages = await asyncio.gather(
            *[_fetch_one_wt(wid) for wid in work_type_ids],
            return_exceptions=True,
        )
        for wt_id, result in zip(work_type_ids, per_wt_pages):
            if isinstance(result, BaseException):
                logger.warning(f"check1 machine_tasks wt_id={wt_id} failed: {result}")
                continue
            all_tasks.extend(result)
    elif failed_batches:
        retry_pages = await asyncio.gather(
            *[_fetch_one_wt(wid) for batch in failed_batches for wid in batch],
            return_exceptions=True,
        )
        for result in retry_pages:
            if isinstance(result, BaseException):
                continue
            all_tasks.extend(result)

    by_id: dict[int, dict] = {}
    for t in all_tasks:
        tid = t.get("id")
        if tid is not None:
            by_id[int(tid)] = t

    logger.info(
        f"check1 machine_tasks fetch: work_type_ids={len(work_type_ids)}, "
        f"batches={len(batches)}, raw={len(all_tasks)}, unique={len(by_id)}"
    )
    return list(by_id.values())


def _task_link(machine_id, task_id) -> str:
    if machine_id and task_id:
        return f"{CROPWISE_TASK_BASE}/{machine_id}/tasks/{task_id}"
    return ""


def _item_cov_before_cutoff(item: dict, cutoff: datetime) -> float:
    """Выработка по MTFMI-элементу до момента cutoff (перенесено из
    check1_no_agro_ops.py: используется почасовая раскладка, если есть,
    иначе — суммарная covered_area)."""
    hourly = item.get("covered_area_hourly") or []
    if hourly:
        total = 0.0
        for k, v in _hourly_entries(hourly):
            dt = _parse_hourly_key(k)
            if dt and dt < cutoff:
                total += float(v or 0)
        return total
    return float(item.get("covered_area") or 0)


def _field_cov_from_mtfmi(
    items: list[dict],
    cutoff: datetime,
    min_field_ha: float,
) -> dict[int, float]:
    field_cov: dict[int, float] = defaultdict(float)
    for item in items:
        fid_raw = item.get("field_id")
        if not fid_raw:
            continue
        fid = int(fid_raw)
        cov = _item_cov_before_cutoff(item, cutoff)
        if cov >= min_field_ha:
            field_cov[fid] += cov
    return field_cov


# =============================================================================
# ASYNC FETCH MTFMI (machine_task_field_mapping_items) ДЛЯ НАБОРА ЗАДАЧ
# =============================================================================

async def _fetch_mtfmi_batch(
    session: aiohttp.ClientSession,
    task_ids: list[int],
) -> dict[int, list[dict]]:
    """MTFMI для списка machine_task_id — пачками по _TASK_LINKS_BATCH_SIZE
    через запятую в одном GET (см. client._fetch_mtfmi_by_task_ids).

    Используется для agro_tasks (check3/4/5/9/10/2) и отдельно для
    check1_task_ids за CHECK1_LOOKBACK_DAYS. Пачки ограничивают
    пиковую память и число HTTP-запросов."""
    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
    return await _fetch_mtfmi_by_task_ids(
        session,
        task_ids,
        batch_size=_TASK_LINKS_BATCH_SIZE,
        sem=sem,
    )


# =============================================================================
# CHECK1 — ЗАДАЧИ, ПОДЛЕЖАЩИЕ ПРОВЕРКЕ
# =============================================================================

def _check1_eligible_tasks(
    agro_tasks: list[dict],
    work_type_map: dict[int, str],
) -> list[dict]:
    """Задачи, подлежащие проверке check1 («нет агроопераций к полям»).

    Из общего списка агро-задач (грузовые/скрытые машины уже отфильтрованы
    выше по пайплайну в machine_is_agro) исключаются:
      - транспортные виды работ (код вида работ начинается на "7"/"8");
      - виды работ, не подлежащие проверке по площади — вспашка,
        гребнеобразование, глубокорыхление, посев (овощи), уборка (5.x).
    Перенесено 1:1 из check1_no_agro_ops.py::collect_data.
    """
    out: list[dict] = []
    for t in agro_tasks:
        wt_id   = t.get("work_type_id")
        wt_name = work_type_map.get(wt_id, "") if wt_id else ""
        if _is_transport_work_type(wt_name):
            continue
        if wt_id in CHECK1_EXCLUDED_WORK_TYPE_IDS:
            continue
        out.append(t)
    return out


# =============================================================================
# ASYNC FETCH AGRO_OPERATIONS — прямая связь machine_task -> agro_operation
# =============================================================================
#
# ВАЖНОЕ ИЗМЕНЕНИЕ (см. check1_no_agro_ops.py): вместо косвенного матчинга по
# паре (history_item_id, work_type_id) используется скрытый (не описанный в
# официальной доке Cropwise API v3) эндпоинт
# /machine_task_agro_operation_mapping_items с полями
# {machine_task_id, agro_operation_id}, который даёт ПРЯМУЮ и единственно
# верную связь агрооперации с конкретным заданием. Привязанная через этот
# эндпоинт операция может иметь ЛЮБОЙ статус (в т.ч. 'planned') и ЛЮБОЙ
# completed_percents (в т.ч. 0.0) — сам факт привязки уже означает, что
# задание учтено в этой операции. Поле считается "закрытым", если среди
# agro_operations, привязанных к данному machine_task через mapping-эндпоинт,
# есть хотя бы одна с field_id, совпадающим с полем из mtfmi-item.
#
# Загрузка mapping_items — batch GET machine_task_id=1,2,3,... (см.
# client._fetch_task_agro_op_links_by_task_ids, 2_main_ideal_compact.py).

# Размер пачки machine_task_id в batch GET (mtfmi и mtaomi).
_TASK_LINKS_BATCH_SIZE = 300


async def _fetch_op_field_map(
    op_ids: set[int],
    *,
    sem_limit: int = SEMAPHORE_LIMIT,
) -> dict[int, Optional[int]]:
    """Для набора agro_operation_id возвращает {op_id: field_id}.

    op_id — собственный id ресурса agro_operations, поэтому вместо
    GET /agro_operations/{op_id} по одному на каждый id используем
    POST /agro_operations/mass_request (см. _fetch_mass_request в client.py) —
    вместо N HTTP-запросов делается ceil(N / chunk_size) запросов.
    """
    if not op_ids:
        return {}
    sem = asyncio.Semaphore(sem_limit)
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        records = await _fetch_mass_request(session, "agro_operations", list(op_ids), sem=sem)
    result: dict[int, Optional[int]] = {}
    for op in records:
        op_id = op.get("id")
        if op_id is None:
            continue
        fid = op.get("field_id")
        result[int(op_id)] = int(fid) if fid is not None else None
    # Операции, которые Cropwise не вернул (удалены/недоступны), — как и
    # раньше при resp.status_code != 200, помечаем field_id=None.
    for op_id in op_ids:
        result.setdefault(int(op_id), None)
    return result


# =============================================================================
# COMPUTE CHECK1 — НЕТ ПРИВЯЗКИ К АГРООПЕРАЦИИ
# =============================================================================

def _compute_check1(
    tasks: list[dict],
    mtfmi_map: dict[int, list[dict]],
    task_op_links: dict[int, list[int]],
    op_field_map: dict[int, Optional[int]],
    machine_name: dict[int, str],
    operators_map: dict[int, str],
    work_type_map: dict[int, str],
    field_to_firm: dict[int, str],
    cutoff: datetime,
    machine_plate: dict[int, str] | None = None,
) -> list[dict]:
    """Нет агроопераций к полям.

    Нарушение: machine_task без транзита (ни transit_distance на уровне
    задания, ни in_transit на уровне отдельного mtfmi-item), у которого
    хотя бы на одном поле с выработкой >= CHECK1_MIN_FIELD_AREA_HA до cutoff
    нет ни одной agro_operation, НАПРЯМУЮ привязанной к этому machine_task
    через эндпоинт machine_task_agro_operation_mapping_items (см.
    client._fetch_task_agro_op_links_by_task_ids / _fetch_op_field_map).

    Поле считается "закрытым", если среди agro_operations, привязанных к
    данному machine_task через mapping-эндпоинт, есть хотя бы одна с
    field_id, совпадающим с полем из mtfmi-item — привязанная операция
    может иметь ЛЮБОЙ статус и ЛЮБОЙ completed_percents, сам факт привязки
    уже означает, что задание учтено.

    `tasks` — задачи, уже прошедшие фильтр _check1_eligible_tasks (без
    транспортных видов работ и без видов работ из CHECK1_EXCLUDED_WORK_TYPE_IDS).
    Перенесено из check1_no_agro_ops.py::collect_data (новая логика матчинга
    от 2026-07-21).
    """
    rows: list[dict] = []
    machine_plate = machine_plate or {}
    for task in tasks:
        tid   = task["id"]
        mid   = task.get("machine_id")
        wt_id = task.get("work_type_id")
        wt_name = work_type_map.get(wt_id, "—") if wt_id else "—"
        mach = machine_name.get(int(mid), f"machine_{mid}") if mid else "—"
        plate = machine_plate.get(int(mid)) if mid else None

        try:
            op_id = int(task["driver_id"]) if task.get("driver_id") is not None else None
        except (TypeError, ValueError):
            op_id = None
        operator_name = operators_map.get(op_id, FIO_NOT_FOUND) if op_id else "—"

        transit = float(task.get("transit_distance") or 0)
        if transit > 0:
            continue

        items_for_task = mtfmi_map.get(tid, [])

        # Множество полей, реально покрытых привязанными к заданию
        # агрооперациями (независимо от статуса/процента — сам факт
        # привязки через mapping-эндпоинт достаточен).
        linked_op_ids  = task_op_links.get(tid, [])
        covered_fields = {
            op_field_map[oid] for oid in linked_op_ids
            if op_field_map.get(oid) is not None
        }

        firm_from_field   = "—"
        has_field_work    = False
        missing_done_agro = False

        for item in items_for_task:
            if item.get("in_transit") is True:
                continue
            fid_raw = item.get("field_id")
            if not fid_raw:
                continue
            fid = int(fid_raw)
            cov = _item_cov_before_cutoff(item, cutoff)
            if cov < CHECK1_MIN_FIELD_AREA_HA:
                continue

            has_field_work = True
            if firm_from_field == "—":
                firm_from_field = field_to_firm.get(fid, "—")

            if fid not in covered_fields:
                missing_done_agro = True

        if firm_from_field == "—":
            direct_fid = task.get("field_id")
            if direct_fid:
                firm_from_field = field_to_firm.get(int(direct_fid), "—")

        if not has_field_work or not missing_done_agro:
            continue

        start_dt = _parse_dt(task.get("start_time"))
        end_dt   = _parse_dt(task.get("end_time"))

        rows.append({
            "firm":       firm_from_field,
            "machine":    mach,
            "license_plate": plate or "—",
            "operator":   operator_name,
            "work_type":  wt_name,
            "start":      start_dt.strftime("%d.%m %H:%M") if start_dt else "—",
            "end":        end_dt.strftime("%d.%m %H:%M")   if end_dt   else "—",
            "start_ts":   start_dt.isoformat() if start_dt else None,
            "end_ts":     end_dt.isoformat() if end_dt else None,
            "task_id":    tid,
            "machine_id": mid,
            "link":       _task_link(mid, tid),
        })
    return rows


# =============================================================================
# COMPUTE CHECK3 — НЕ УКАЗАН МЕХАНИЗАТОР
# =============================================================================

def _compute_check3(
    agro_tasks: list[dict],
    mtfmi_map: dict[int, list[dict]],
    machine_name: dict[int, str],
    implements_map: dict[int, dict],
    work_type_map: dict[int, str],
    field_to_firm: dict[int, str],
    field_name: dict[int, str],
    field_to_crop: dict[int, str],
    cutoff: datetime,
    machine_is_hired: dict[int, bool] | None = None,
) -> list[dict]:
    machine_is_hired = machine_is_hired or {}
    rows: list[dict] = []
    for task in agro_tasks:
        if task.get("driver_id"):
            continue

        tid   = task["id"]
        mid   = task.get("machine_id")
        wt_id = task.get("work_type_id")
        wt_name = work_type_map.get(wt_id, "—") if wt_id else "—"

        if _is_transport_work_type(wt_name):
            continue

        if mid and machine_is_hired.get(int(mid)):
            continue

        mach = machine_name.get(int(mid), f"machine_{mid}") if mid else "—"

        impl_id  = task.get("implement_id")
        impl_rec = implements_map.get(impl_id) if impl_id else None
        implement_name = "—"
        if impl_rec:
            implement_name = str(
                impl_rec.get("custom_name") or impl_rec.get("name") or "—"
            ).strip() or "—"

        items_for_task = mtfmi_map.get(tid, [])
        field_cov = _field_cov_from_mtfmi(
            items_for_task, cutoff, CHECK3_MIN_FIELD_AREA_HA,
        )

        if not field_cov:
            direct_fid = task.get("field_id")
            task_area = float(
                task.get("completed_area") or task.get("covered_area") or 0
            )
            if direct_fid and task_area > 0:
                field_cov[int(direct_fid)] = task_area
            else:
                for item in items_for_task:
                    fid_raw = item.get("field_id")
                    if not fid_raw:
                        continue
                    cov = _item_cov_before_cutoff(item, cutoff)
                    if cov > 0:
                        field_cov[int(fid_raw)] += cov
                if not field_cov and task_area > 0:
                    for item in items_for_task:
                        fid_raw = item.get("field_id")
                        if fid_raw:
                            field_cov[int(fid_raw)] = task_area
                            break

        start_dt = _parse_dt(task.get("start_time"))
        end_dt   = _parse_dt(task.get("end_time"))
        link     = _task_link(mid, tid)

        firm = "—"
        crop = "—"
        if field_cov:
            first_fid = next(iter(field_cov))
            firm = field_to_firm.get(first_fid, "—")
            crop = field_to_crop.get(first_fid, "—")
        elif task.get("field_id"):
            fid0 = int(task["field_id"])
            firm = field_to_firm.get(fid0, "—")
            crop = field_to_crop.get(fid0, "—")

        if field_cov:
            fields_str = ", ".join(
                field_name.get(fid, f"field_{fid}") for fid in field_cov
            )
            total_area = round(sum(field_cov.values()), 2)
        else:
            fields_str = "—"
            total_area = 0.0

        if total_area < CHECK3_MIN_TOTAL_AREA_HA:
            continue

        rows.append({
            "firm":       firm,
            "crop":       crop,
            "field":      fields_str,
            "machine":    mach,
            "implement":  implement_name,
            "work_type":  wt_name,
            "area":       total_area,
            "start":      start_dt.strftime("%d.%m %H:%M") if start_dt else "—",
            "end":        end_dt.strftime("%d.%m %H:%M")   if end_dt   else "—",
            "task_id":    tid,
            "machine_id": mid,
            "link":       link,
        })

    return rows


# =============================================================================
# COMPUTE CHECK4 — НЕ ПРИВЯЗАНО ОБОРУДОВАНИЕ
# =============================================================================

def _compute_check4(
    agro_tasks: list[dict],
    mtfmi_map: dict[int, list[dict]],
    machine_name: dict[int, str],
    operators_map: dict[int, str],
    work_type_map: dict[int, str],
    field_to_firm: dict[int, str],
    field_name: dict[int, str],
    field_to_crop: dict[int, str],
    cutoff: datetime,
) -> list[dict]:
    rows: list[dict] = []
    for task in agro_tasks:
        if task.get("implement_id"):
            continue

        tid   = task["id"]
        mid   = task.get("machine_id")
        wt_id = task.get("work_type_id")
        wt_name = work_type_map.get(wt_id, "—") if wt_id else "—"

        if _is_transport_work_type(wt_name):
            continue

        mach = machine_name.get(int(mid), f"machine_{mid}") if mid else "—"
        try:
            op_id = int(task["driver_id"]) if task.get("driver_id") is not None else None
        except (TypeError, ValueError):
            op_id = None
        operator_name = operators_map.get(op_id, FIO_NOT_FOUND) if op_id else "—"

        items_for_task = mtfmi_map.get(tid, [])
        field_cov = _field_cov_from_mtfmi(items_for_task, cutoff, MIN_FIELD_AREA_HA)

        if not field_cov:
            direct_fid = task.get("field_id")
            if direct_fid:
                field_cov[int(direct_fid)] = float(
                    task.get("completed_area") or task.get("covered_area") or 0
                )

        start_dt = _parse_dt(task.get("start_time"))
        end_dt   = _parse_dt(task.get("end_time"))
        link     = _task_link(mid, tid)

        def _base_row(fid=None, area=0.0):
            return {
                "firm":       field_to_firm.get(fid, "—") if fid else "—",
                "crop":       field_to_crop.get(fid, "—") if fid else "—",
                "field":      field_name.get(fid, f"field_{fid}") if fid else "—",
                "operator":   operator_name,
                "machine":    mach,
                "work_type":  wt_name,
                "area":       round(area, 2),
                "start":      start_dt.strftime("%d.%m %H:%M") if start_dt else "—",
                "end":        end_dt.strftime("%d.%m %H:%M")   if end_dt   else "—",
                "task_id":    tid,
                "machine_id": mid,
                "link":       link,
            }

        if field_cov:
            for fid, area in field_cov.items():
                rows.append(_base_row(fid, area))
        else:
            rows.append(_base_row())

    return rows

# =============================================================================
# COMPUTE CHECK5 — ПЛОЩАДЬ ЗАДАНА ВРУЧНУЮ
# =============================================================================

def _compute_check5(
    agro_tasks: list[dict],
    mtfmi_map: dict[int, list[dict]],
    machine_name: dict[int, str],
    operators_map: dict[int, str],
    work_type_map: dict[int, str],
    field_to_firm: dict[int, str],
    field_name: dict[int, str],
    field_to_crop: dict[int, str],
    cutoff: datetime,
) -> list[dict]:
    rows: list[dict] = []
    for task in agro_tasks:
        manual_area = float(task.get("manual_covered_area") or 0)
        if manual_area <= 0:
            continue

        tid   = task["id"]
        mid   = task.get("machine_id")
        wt_id = task.get("work_type_id")
        wt_name = work_type_map.get(wt_id, "—") if wt_id else "—"

        if _is_transport_work_type(wt_name):
            continue

        mach = machine_name.get(int(mid), f"machine_{mid}") if mid else "—"
        try:
            op_id = int(task["driver_id"]) if task.get("driver_id") is not None else None
        except (TypeError, ValueError):
            op_id = None
        operator_name = operators_map.get(op_id, FIO_NOT_FOUND) if op_id else "—"

        items_for_task = mtfmi_map.get(tid, [])
        field_cov = _field_cov_from_mtfmi(items_for_task, cutoff, MIN_FIELD_AREA_HA)

        if not field_cov:
            direct_fid = task.get("field_id")
            if direct_fid:
                field_cov[int(direct_fid)] = float(
                    task.get("completed_area") or task.get("covered_area") or 0
                )

        auto_area = float(task.get("covered_area") or 0)
        start_dt  = _parse_dt(task.get("start_time"))
        end_dt    = _parse_dt(task.get("end_time"))
        link      = _task_link(mid, tid)

        # Определяем агрофирму и культуру через первое подходящее поле
        firm = "—"
        crop = "—"
        if field_cov:
            first_fid = next(iter(field_cov))
            firm = field_to_firm.get(first_fid, "—")
            crop = field_to_crop.get(first_fid, "—")
        elif task.get("field_id"):
            fid0 = int(task["field_id"])
            firm = field_to_firm.get(fid0, "—")
            crop = field_to_crop.get(fid0, "—")

        # Список полей через запятую
        if field_cov:
            fields_str = ", ".join(
                field_name.get(fid, f"field_{fid}") for fid in field_cov
            )
            total_area = round(sum(field_cov.values()), 2)
        else:
            fields_str = "—"
            total_area = 0.0

        rows.append({
            "firm":        firm,
            "crop":        crop,
            "field":       fields_str,
            "operator":    operator_name,
            "machine":     mach,
            "work_type":   wt_name,
            "area":        total_area,
            "auto_area":   round(auto_area, 2),
            "manual_area": round(manual_area, 2),
            "start":       start_dt.strftime("%d.%m %H:%M") if start_dt else "—",
            "end":         end_dt.strftime("%d.%m %H:%M")   if end_dt   else "—",
            "task_id":     tid,
            "machine_id":  mid,
            "link":        link,
        })

    return rows
    
CROPWISE_GPS_URL = "https://operations.cropwise.com/api/v3/gps_aggregated_data"

# Единый порог для check6 (агро) и check6t (транспорт): нарушение фиксируется
# по КОНКРЕТНОЙ ДАТЕ, если пробег машины в эту дату >= порога И в эту же дату
# у машины нет ни одной задачи (а не по сумме пробега за весь период, как
# было раньше, и не по наличию хоть одной задачи где-то в периоде).
# Порог пробега за 12-ЧАСОВОЙ ПЕРИОД (не за календарную дату!), выше
# которого считаем, что движение было.
GPS_KM_THRESHOLD_PER_PERIOD = 0.5   # км (500 м) — порог пробега за период без задания

# Длина одного проверяемого периода. Окно (2 суток, начинается в
# DAY_START_HOUR) естественным образом делится на 4 таких периода, выровненных
# по 06:00/18:00 при DAY_START_HOUR=6: [06:00–18:00), [18:00–06:00) и т.д.
PERIOD_HOURS = 12


def _generate_periods(window_start: datetime, window_end: datetime) -> list[datetime]:
    """Список НАЧАЛ 12-часовых периодов, полностью покрывающих окно
    [window_start, window_end). window_start уже выровнен на DAY_START_HOUR
    (см. _shift_window), поэтому периоды естественно ложатся на 06:00/18:00."""
    periods, cur = [], window_start
    while cur < window_end:
        periods.append(cur)
        cur += timedelta(hours=PERIOD_HOURS)
    return periods


def _period_end(period_start: datetime) -> datetime:
    return period_start + timedelta(hours=PERIOD_HOURS)


def _period_label(period_start: datetime) -> str:
    return f"{period_start:%d.%m.%Y %H:%M}–{_period_end(period_start):%H:%M}"

# GPS: гипотеза, подтверждённая на диагностическом скрипте — "тяжёлые"
# запросы на много машин сразу (было 50) считаются на сервере ощутимо дольше
# нелинейно (запрос machine_tasks вообще БЕЗ фильтра по машинам отрабатывал
# за секунды, а один GPS-чанк на полсотни машин — нет). Поэтому идём МЕЛКИМИ
# пачками с высокой параллельностью — каждый отдельный запрос лёгкий для
# сервера, общее время определяется параллельностью, а не размером запроса.
GPS_CHUNK_SIZE      = 10   # машин в одном GPS-запросе
GPS_SEMAPHORE_LIMIT = 15   # параллельных GPS-запросов (отдельно от общего SEMAPHORE_LIMIT)
GPS_REQUEST_TIMEOUT = 30.0  # секунд на один page-запрос GPS
GPS_AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=60.0, connect=10.0,
                                             sock_connect=10.0,
                                             sock_read=GPS_REQUEST_TIMEOUT)

# Кэш GPS-записей по ПОЛНОСТЬЮ ЗАВЕРШЁННЫМ суткам (date_at < сегодня). Такие
# сутки больше не меняются, поэтому перекачивать их при каждом обновлении
# бессмысленно — актуально, т.к. окно проверки (day_start..cutoff) обычно
# захватывает вчерашний день целиком. Живёт на уровне процесса (модульный
# словарь), сбрасывается при перезапуске сервиса.
_gps_day_cache: dict[str, list[dict]] = {}


# =============================================================================
# GPS ДЛЯ АГРО-МАШИН (CHECK6) / ТРАНСПОРТА (CHECK6T)
# =============================================================================

async def _gps_chunk_aiohttp(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    date_at: str,
    machine_ids: list[int],
) -> list[dict]:
    """
    Запрашивает GPS-агрегат за одну дату для чанка машин.

    Пагинация — по номеру page (в отличие от get_all-подобных запросов с
    from_id-курсором она предсказуема заранее), поэтому при необходимости
    нескольких страниц они догружаются ПАЧКАМИ параллельно, а не по одной.

    По документации Cropwise API у /gps_aggregated_data только 2 фильтра:
    date_at (обязательный) и machine_ids. Часового фильтра НЕТ — сузить
    выборку на сервере нельзя, date_at всегда отдаёт ВСЕ часы суток. Поэтому
    отсечение по границам окна (day_start/cutoff) делается на своей стороне,
    в _fetch_gps_for_agri, после получения данных.
    """
    params = {
        "date_at":     date_at,
        "machine_ids": ",".join(str(mid) for mid in machine_ids),
        "per_page":    1000,
    }

    async def get_page(page: int) -> list[dict]:
        async with semaphore:
            async with session.get(CROPWISE_GPS_URL, params={**params, "page": page},
                                    timeout=GPS_AIOHTTP_TIMEOUT) as resp:
                body = await resp.json()
        return body if isinstance(body, list) else body.get("data", [])

    first = await get_page(1)
    results: list[dict] = list(first)
    if len(first) < 1000:
        return results  # единственная страница — экономим лишние запросы

    PAGE_BATCH = 4
    page = 2
    while True:
        batch = await asyncio.gather(*(get_page(p) for p in range(page, page + PAGE_BATCH)))
        stop = False
        for chunk in batch:
            results.extend(chunk)
            if len(chunk) < 1000:
                stop = True
        if stop:
            break
        page += PAGE_BATCH
    return results


async def _fetch_gps_for_agri(
    session: aiohttp.ClientSession,
    agri_ids: list[int],
    gps_dates: list[str],
    window_start: datetime,
    window_end: datetime,
    periods: list[datetime],
) -> tuple[dict[int, float], dict[int, dict[datetime, float]]]:
    """
    Запрашивает GPS за все нужные даты параллельно, возвращает:
      - km_by_machine: {machine_id: суммарный_пробег_км} (только для логов)
      - km_by_machine_by_period: {machine_id: {period_start: пробег_км}} —
        бакетировано по 12-часовым периодам (см. _generate_periods), а НЕ по
        календарным датам (нужно, чтобы показать в check6/check6t, за какой
        именно период зафиксировано движение без задания)

    Полностью завершённые сутки (date_at < сегодня) берутся из
    _gps_day_cache вместо повторного запроса. Сам запрос к API как был по
    календарным суткам, так и остался (API не поддерживает более узкий
    фильтр — только date_at и machine_ids, подтверждено документацией),
    меняется только то, во что раскладывается результат.

    ВАЖНО про границы: date_at отдаёт ВСЮ календарную дату целиком, а окно
    проверки — day_start (например, вчера 06:00, см. DAY_START_HOUR) .. cutoff
    (сейчас), т.е. НЕ обязательно совпадает с границами суток. Без фильтра
    по hour_start часы вне окна (и вне конкретного 12-часового периода)
    ошибочно попали бы в подсчёт — поэтому здесь отбрасываем записи вне
    [window_start, window_end) и определяем период уже по фактическому
    hour_start, после получения данных (сервер такой фильтр не умеет —
    см. _gps_chunk_aiohttp).
    """
    today_str = datetime.now(tz=TZ_MSK).date().isoformat()
    gps_sem = asyncio.Semaphore(GPS_SEMAPHORE_LIMIT)

    date_records: dict[str, list[dict]] = {}
    to_fetch: list[str] = []
    for date_str in gps_dates:
        if date_str < today_str and date_str in _gps_day_cache:
            date_records[date_str] = _gps_day_cache[date_str]
            logger.debug(f"GPS: {date_str} — сутки завершены, беру из кэша "
                        f"({len(_gps_day_cache[date_str])} записей)")
        else:
            to_fetch.append(date_str)

    if to_fetch:
        coros = [
            _gps_chunk_aiohttp(session, gps_sem, date_str, agri_ids[i:i + GPS_CHUNK_SIZE])
            for date_str in to_fetch
            for i in range(0, len(agri_ids), GPS_CHUNK_SIZE)
        ]
        chunks_per_date = max(1, (len(agri_ids) + GPS_CHUNK_SIZE - 1) // GPS_CHUNK_SIZE)
        logger.debug(f"GPS: запускаю {len(coros)} параллельных запросов "
                    f"({len(to_fetch)} дат × {chunks_per_date} чанков по {GPS_CHUNK_SIZE} машин)")
        all_chunks = await asyncio.gather(*coros)
        # Разбираем плоский список чанков обратно по датам (порядок совпадает
        # с порядком генерации coros: для каждой даты — chunks_per_date чанков подряд).
        idx = 0
        for date_str in to_fetch:
            day_chunks = all_chunks[idx:idx + chunks_per_date]
            idx += chunks_per_date
            day_records = [rec for chunk in day_chunks for rec in chunk]
            date_records[date_str] = day_records
            if date_str < today_str:
                _gps_day_cache[date_str] = day_records
                logger.debug(f"GPS: {date_str} — сутки завершены, сохранил "
                            f"{len(day_records)} записей в кэш")

    km_by_machine: dict[int, float] = {}
    km_by_machine_by_period: dict[int, dict[datetime, float]] = {}
    skipped_outside_window = 0
    for date_str, records in date_records.items():
        for rec in records:
            mid_raw = rec.get("machine_id")
            dist_raw = rec.get("distance")
            if mid_raw is None or dist_raw is None:
                continue
            # Отсечение по фактическому часу и определение периода — см. докстринг выше.
            hour_dt = _parse_dt(rec.get("hour_start"))
            if hour_dt is None or not (window_start <= hour_dt < window_end):
                skipped_outside_window += 1
                continue
            p_start = next((p for p in periods if p <= hour_dt < _period_end(p)), None)
            if p_start is None:
                skipped_outside_window += 1
                continue
            mid = int(mid_raw)
            km = float(dist_raw) / 1000.0
            km_by_machine[mid] = km_by_machine.get(mid, 0.0) + km
            period_map = km_by_machine_by_period.setdefault(mid, {})
            period_map[p_start] = period_map.get(p_start, 0.0) + km
    if skipped_outside_window:
        logger.debug(f"GPS: отброшено {skipped_outside_window} записей вне окна/периодов "
                    f"[{window_start}, {window_end})")
    return km_by_machine, km_by_machine_by_period


def _task_covered_periods(task: dict, periods: list[datetime]) -> set[datetime]:
    """12-часовые периоды, которые ПЕРЕСЕКАЕТ задача — start_time < конец
    периода И (end_time > начало периода ИЛИ end_time отсутствует — задание
    ещё не закрыто)."""
    start_dt = _parse_dt(task.get("start_time"))
    if start_dt is None:
        return set()
    end_dt = _parse_dt(task.get("end_time"))  # None — задание ещё идёт/не закрыто
    covered: set[datetime] = set()
    for p_start in periods:
        p_end = _period_end(p_start)
        if start_dt < p_end and (end_dt is None or end_dt > p_start):
            covered.add(p_start)
    return covered


def _build_task_periods_by_machine(
    tasks: list[dict], periods: list[datetime],
) -> dict[int, set[datetime]]:
    """{machine_id: {period_start, ...}} — 12-часовые периоды, в которые у
    машины есть хотя бы одна задача, пересекающая период. Используется в
    check6/check6t, чтобы проверять отсутствие задания ПО КОНКРЕТНОМУ
    12-часовому периоду, а не по всему окну целиком."""
    out: dict[int, set[datetime]] = {}
    for t in tasks:
        mid_raw = t.get("machine_id")
        if mid_raw is None:
            continue
        mid = int(mid_raw)
        out.setdefault(mid, set()).update(_task_covered_periods(t, periods))
    return out


# =============================================================================
# COMPUTE CHECK6 / CHECK6T — АГРО/ТРАНСПОРТ БЕЗ ЗАДАНИЯ В КОНКРЕТНЫЙ
# 12-ЧАСОВОЙ ПЕРИОД, НО С GPS-ПРОБЕГОМ >= 1000 М В ЭТОТ ЖЕ ПЕРИОД
# =============================================================================

def _compute_missing_task_rows(
    machine_name: dict[int, str],
    machine_flag: dict[int, bool],
    machine_to_firm: dict[int, str],
    task_periods_by_machine: dict[int, set[datetime]],
    km_by_machine_by_period: dict[int, dict[datetime, float]],
) -> list[dict]:
    """
    Общая логика для check6 (агро) и check6t (транспорт): нарушение — это
    КОНКРЕТНЫЙ 12-ЧАСОВОЙ ПЕРИОД, в который пробег машины
    >= GPS_KM_THRESHOLD_PER_PERIOD (по умолчанию 1000 м) И при этом у машины
    нет ни одной задачи, ПЕРЕСЕКАЮЩЕЙ именно этот период.

    Одна строка = один период (date_key = ISO начала периода), чтобы статус
    «Отработано» не «переезжал» между датами в одной агрегированной строке.

    `machine_flag` — machine_is_agro либо machine_is_truck (фильтр, к какому
    типу техники применяется чек). `task_periods_by_machine` — периоды,
    покрытые заданиями, по каждой машине (см. _build_task_periods_by_machine).
    """
    rows: list[dict] = []
    for mid, period_map in km_by_machine_by_period.items():
        if not machine_flag.get(mid, False):
            continue
        task_periods = task_periods_by_machine.get(mid, set())
        violating_periods = {
            p: km for p, km in period_map.items()
            if km >= GPS_KM_THRESHOLD_PER_PERIOD and p not in task_periods
        }
        if not violating_periods:
            continue
        firm = machine_to_firm.get(mid, "—")
        machine = machine_name.get(mid, f"machine_{mid}")
        link = f"{CROPWISE_TASK_BASE}/{mid}"
        for p_start in sorted(violating_periods):
            km = violating_periods[p_start]
            rows.append({
                "firm":       firm,
                "machine":    machine,
                "machine_id": mid,
                "km":         round(km, 2),
                "dates":      _period_label(p_start),
                "date_key":   p_start.isoformat(),
                "link":       link,
            })
    rows.sort(key=lambda r: (r["firm"], r["dates"], -r["km"]))
    return rows


def _compute_check6(
    machine_name: dict[int, str],
    machine_is_agro: dict[int, bool],
    machine_to_firm: dict[int, str],
    task_periods_by_machine: dict[int, set[datetime]],
    km_by_machine_by_period: dict[int, dict[datetime, float]] | None = None,
) -> list[dict]:
    """Агро-машины: 12-часовые периоды, когда пробег >= 1000 м и в этот
    период нет задания, пересекающего его."""
    return _compute_missing_task_rows(
        machine_name, machine_is_agro, machine_to_firm,
        task_periods_by_machine, km_by_machine_by_period or {},
    )


def _compute_check6_transport(
    machine_name: dict[int, str],
    machine_is_truck: dict[int, bool],
    machine_to_firm: dict[int, str],
    task_periods_by_machine: dict[int, set[datetime]],
    km_by_machine_by_period: dict[int, dict[datetime, float]] | None = None,
) -> list[dict]:
    """Грузовые машины: 12-часовые периоды, когда пробег >= 1000 м и в этот
    период нет задания, пересекающего его."""
    return _compute_missing_task_rows(
        machine_name, machine_is_truck, machine_to_firm,
        task_periods_by_machine, km_by_machine_by_period or {},
    )




# =============================================================================
# COMPUTE CHECK9 — ВОЗМОЖНЫЙ ТРАНЗИТ (0.1 га < площадь < 5 га, нет in_transit)
# =============================================================================

AREA_MIN_HA       = 0.1   # нижняя граница — исключаем нулевые/почти нулевые
AREA_THRESHOLD_HA = 5.0   # верхняя граница — выше этого уже не транзит

# Порог, ниже которого forbidden_ops (_sync_forbidden_ops_async в tasks.py)
# отбрасывает строку целиком (`if cov < 0.5: continue`). check9 должен
# «подхватывать» именно эти отброшенные строки — см. FORBIDDEN_AREA_MIN_HA.
FORBIDDEN_AREA_MIN_HA = 0.5

# Культуры, для которых forbidden_ops считает совпадение автоматическим
# (без проверки списка запрещённых операций) — см. SKIP_CROPS в tasks.py::
# _sync_forbidden_ops_async. Держать в синхроне с тем списком.
SKIP_CROPS_CHECK9: frozenset[str] = frozenset({
    "Залежь, лес", "Залежь", "Введение в оборот 2026 год", "Введение в оборот 2024 год", "Введение в оборот 2025 год",
    "Луговые травы", "Обмен", "В пользовании у фермера",
    "Введение в оборот 2027 год", "Введение в оборот", "Многолетние травы 2025", "Многолетние травы 2021", "Многолетние травы 2022", "Многолетние травы 2024",
    "Передано в аренду", "Дороги/лесополосы", "Овраги/неудобия", "crop_None",
})

# Виды работ, для которых проверка площадью не применяется
_EXCLUDED_CHECK9_NAMES: frozenset[str] = frozenset({
    "2.4 Вспашка",
    "2.7 Гребнеобразование",
    "2.8 Глубокорыхление",
    "4.1 Посев (овощи)",
    "5.0 Уборка",
    "5.4 Уборка картофеля, лука",
})

WORK_GROUPS_CHECK9: list[tuple[str, list[int]]] = [
    ("1.1 Внесение минеральных удобрений", [55]),
    ("2.1 Боронование",                    [103, 81, 97, 12]),
    ("2.9 Дискование",                     [60]),
    ("2.4 Вспашка",                        [49, 57, 171]),
    ("2.11 Культивация",                   [20, 50, 52, 63, 96]),
    ("4.0 Посев",                          [3, 4, 69, 70, 71, 72, 119, 120, 159, 160, 161, 163]),
    ("4.1 Посев (овощи)",                  [68, 148, 172]),
    ("3.1 Опрыскивание",                   [1, 64, 65, 66, 153]),
    ("2.12 Прикатывание",                  [13, 67, 142]),
    ("2.7 Гребнеобразование",              [62]),
    ("2.6 Фрезерование",                   [73]),
    ("2.8 Глубокорыхление",                [11, 106, 121, 150]),
    ("5.0 Уборка",                         [6, 7, 75, 76, 77, 111, 118, 125, 126, 145]),
    ("5.4 Уборка картофеля, лука",         [76]),
]
_EXCLUDED_CHECK9_IDS: frozenset[int] = frozenset(
    wt_id
    for name, ids in WORK_GROUPS_CHECK9
    if name in _EXCLUDED_CHECK9_NAMES
    for wt_id in ids
)


def _compute_check9(
    agro_tasks: list[dict],
    mtfmi_map: dict[int, list[dict]],
    work_type_map: dict[int, str],
    field_to_firm: dict[int, str],
    field_to_crop: dict[int, str],
    cutoff: datetime,
    forbidden_ops: dict[str, set[str]] | None = None,
) -> list[dict]:
    """
    Возможный транзит = СУММА (объединение) двух независимых условий:

      A) прежняя эвристика:
         - вид работ НЕ входит в _EXCLUDED_CHECK9_IDS
         - ни один MTFMI-item НЕ имеет in_transit=True
         - суммарная covered_area строго в диапазоне (AREA_MIN_HA, AREA_THRESHOLD_HA)

      B) то же сопоставление «культура → запрещённый для неё вид работ»,
         что и в панели «Запрещённые операции по культурам»
         (_sync_forbidden_ops_async в tasks.py), но для строк, которые
         forbidden_ops отбрасывает целиком из-за `cov < 0.5`
         (FORBIDDEN_AREA_MIN_HA) — т.е. «было бы нарушением, но площадь
         слишком мала, похоже на проезд, а не на работу».

    В обоих случаях транспортные виды работ (7.x / 8.x) исключены заранее.
    """
    forbidden_ops = forbidden_ops or {}
    rows: list[dict] = []
    for task in agro_tasks:
        tid   = task["id"]
        mid   = task.get("machine_id")
        wt_id = task.get("work_type_id")

        # Исключаем транспортные виды работ (7.x / 8.x) — общее для A и B
        wt_name = work_type_map.get(wt_id, "—") if wt_id else "—"
        if _is_transport_work_type(wt_name):
            continue

        items_for_task = mtfmi_map.get(tid, [])

        has_transit             = False
        total_covered_area      = 0.0
        firm_from_field         = "—"
        forbidden_small_area_hit = False  # условие B

        for item in items_for_task:
            if item.get("in_transit") is True:
                has_transit = True
            fid_raw = item.get("field_id")
            if not fid_raw:
                continue
            fid = int(fid_raw)
            cov = _item_cov_before_cutoff(item, cutoff)
            total_covered_area += cov
            if firm_from_field == "—" and cov > 0:
                firm_from_field = field_to_firm.get(fid, "—")

            # Условие B: то же правило, что в forbidden_ops —
            # SKIP_CROPS/"—" считаются автосовпадением, иначе вид работ
            # должен быть в списке запрещённых для данной культуры.
            crop = field_to_crop.get(fid, "—")
            if crop in SKIP_CROPS_CHECK9 or crop == "—":
                is_forbidden_match = True
            else:
                is_forbidden_match = wt_name in forbidden_ops.get(crop, set())
            if is_forbidden_match and cov < FORBIDDEN_AREA_MIN_HA:
                forbidden_small_area_hit = True

        # Fallback: агрофирма из прямого field_id задания
        if firm_from_field == "—":
            direct_fid = task.get("field_id")
            if direct_fid:
                firm_from_field = field_to_firm.get(int(direct_fid), "—")

        # Условие A: прежняя эвристика (площадь 0.1–5.0 га, свой список
        # исключённых видов работ, без флага транзита)
        excluded_by_group = bool(wt_id) and wt_id in _EXCLUDED_CHECK9_IDS
        condition_a = (
            not has_transit
            and not excluded_by_group
            and AREA_MIN_HA < total_covered_area < AREA_THRESHOLD_HA
        )

        # Условие B: совпадение по запрещённым операциям культуры с
        # площадью ниже порога, который forbidden_ops отбрасывает
        condition_b = forbidden_small_area_hit and not has_transit

        if not (condition_a or condition_b):
            continue

        start_dt = _parse_dt(task.get("start_time"))
        end_dt   = _parse_dt(task.get("end_time"))

        rows.append({
            "firm":       firm_from_field,
            "work_type":  wt_name,
            "area":       round(total_covered_area, 2),
            "start":      start_dt.strftime("%d.%m %H:%M") if start_dt else "—",
            "end":        end_dt.strftime("%d.%m %H:%M")   if end_dt   else "—",
            "task_id":    tid,
            "machine_id": mid,
            "link":       _task_link(mid, tid),
        })

    rows.sort(key=lambda r: (
        FIRM_ORDER.index(r["firm"]) if r["firm"] in FIRM_ORDER else 999,
        r["work_type"],
    ))
    logger.info(f"check9 (possible transit): {len(rows)} rows")
    return rows


# =============================================================================
# COMPUTE CHECK10 — 2+ ТМЦ В ОДНОМ ЗАДАНИИ
# =============================================================================

async def _fetch_agro_ops_one_full(
    client: aiohttp.ClientSession,
    history_item_id: int,
    work_type_id: int,
    semaphore: asyncio.Semaphore,
) -> tuple[tuple[int, int], list[dict]]:
    """Загружает все agro_operations по паре (history_item_id, work_type_id)."""
    items, from_id = [], 0
    url = "https://operations.cropwise.com/api/v3/agro_operations"
    while True:
        async with semaphore:
            async with client.get(url, params={
                "history_item_id": history_item_id,
                "work_type_id":    work_type_id,
                "from_id":         from_id,
                "limit":           1000,
            }) as resp:
                body = await resp.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        items.extend(chunk)
        meta    = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = last_id + 1
    return (history_item_id, work_type_id), items


async def _fetch_mix_items_one(
    client: aiohttp.ClientSession,
    agro_op_id: int,
    semaphore: asyncio.Semaphore,
) -> tuple[int, list[dict]]:
    """Загружает application_mix_items для одной agro_operation."""
    items, from_id = [], 0
    url = "https://operations.cropwise.com/api/v3/application_mix_items"
    while True:
        async with semaphore:
            async with client.get(url, params={
                "agro_operation_id": agro_op_id,
                "from_id":           from_id,
                "limit":             1000,
            }) as resp:
                body = await resp.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        items.extend(chunk)
        meta    = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = last_id + 1
    return agro_op_id, items


async def _compute_check10(
    session: aiohttp.ClientSession,
    agro_tasks: list[dict],
    mtfmi_map: dict[int, list[dict]],
    machine_name: dict[int, str],
    operators_map: dict[int, str],
    work_type_map: dict[int, str],
    field_to_firm: dict[int, str],
    material_map: dict[int, str],
) -> list[dict]:
    """
    2+ ТМЦ в одном задании:
      MTFMI → agro_operations → application_mix_items → 2+ разных материала.
    """
    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)

    # Только задачи action_type=="agri" и work_area > 0.1
    tasks = [
        t for t in agro_tasks
        if t.get("action_type") == "agri" and float(t.get("work_area") or 0) > 0.1
    ]

    # Шаг 1: собираем уникальные пары (history_item_id, work_type_id)
    pairs: set[tuple[int, int]] = set()
    for task in tasks:
        wt_id = task.get("work_type_id")
        if not wt_id:
            continue
        for item in mtfmi_map.get(task["id"], []):
            hist_id = item.get("history_item_id")
            if hist_id:
                pairs.add((int(hist_id), int(wt_id)))

    if not pairs:
        return []

    # Шаг 2: загружаем agro_operations
    ops_results = await asyncio.gather(*[
        _fetch_agro_ops_one_full(session, h, w, sem) for h, w in pairs
    ])
    ops_by_pair: dict[tuple[int, int], list[dict]] = dict(ops_results)

    # Шаг 3: собираем уникальные op_id и загружаем mix_items
    all_op_ids: set[int] = set()
    for op_list in ops_by_pair.values():
        for op in op_list:
            if op.get("id"):
                all_op_ids.add(int(op["id"]))

    mix_results = await asyncio.gather(*[
        _fetch_mix_items_one(session, oid, sem) for oid in all_op_ids
    ])
    mix_by_op: dict[int, list[dict]] = dict(mix_results)

    # Шаг 4: анализ
    rows: list[dict] = []
    for task in tasks:
        tid   = task["id"]
        wt_id = task.get("work_type_id")
        mid   = task.get("machine_id")
        items = mtfmi_map.get(tid, [])

        if not wt_id or not items:
            continue

        # Все agro_operations этого задания
        task_ops: list[dict] = []
        for item in items:
            hist_id = item.get("history_item_id")
            if hist_id:
                task_ops.extend(ops_by_pair.get((int(hist_id), int(wt_id)), []))

        if not task_ops:
            continue

        # Материалы через mix_items
        material_ids:   set[int] = set()
        material_names: set[str] = set()
        for op in task_ops:
            op_id_val = op.get("id")
            if not op_id_val:
                continue
            for mix_item in mix_by_op.get(int(op_id_val), []):
                for key in ("fertilizer_id", "seed_id", "chemical_id", "material_id"):
                    mat_id = mix_item.get(key)
                    if mat_id:
                        material_ids.add(int(mat_id))
                        material_names.add(
                            material_map.get(int(mat_id)) or
                            mix_item.get("name") or mix_item.get("title") or
                            f"id={mat_id}"
                        )

        if len(material_ids) < 2:
            continue

        # Агрофирма через поля MTFMI
        firm = "—"
        for item in items:
            fid_raw = item.get("field_id")
            if fid_raw:
                f = field_to_firm.get(int(fid_raw))
                if f:
                    firm = f
                    break

        mach = machine_name.get(int(mid), f"machine_{mid}") if mid else "—"
        try:
            drv_id = int(task["driver_id"]) if task.get("driver_id") is not None else None
        except (TypeError, ValueError):
            drv_id = None
        operator_name = operators_map.get(drv_id, FIO_NOT_FOUND) if drv_id else "—"

        wt_name  = work_type_map.get(wt_id, f"wt_{wt_id}")
        start_dt = _parse_dt(task.get("start_time"))
        end_dt   = _parse_dt(task.get("end_time"))

        rows.append({
            "firm":           firm,
            "machine":        mach,
            "operator":       operator_name,
            "work_type":      wt_name,
            "start":          start_dt.strftime("%d.%m %H:%M") if start_dt else "—",
            "end":            end_dt.strftime("%d.%m %H:%M")   if end_dt   else "—",
            "material_count": len(material_ids),
            "materials":      ", ".join(sorted(material_names)),
            "task_id":        tid,
            "machine_id":     mid,
            "link":           _task_link(mid, tid),
        })

    rows.sort(key=lambda r: (
        FIRM_ORDER.index(r["firm"]) if r["firm"] in FIRM_ORDER else 999,
        r["machine"],
    ))
    logger.info(f"check10 (2+ ТМЦ): {len(rows)} rows")
    return rows


# =============================================================================
# COMPUTE CHECK2 — ВЕРОЯТНЫЙ ДВОЙНОЙ СЛЕД (work_area / covered_area >= 1.75)
# =============================================================================

# Коэффициент: полная / чистая площадь
RATIO_THRESHOLD_CHECK2 = 1.75

# Исключённые виды работ (те же, что в check9)
_EXCLUDED_CHECK2_IDS: frozenset[int] = _EXCLUDED_CHECK9_IDS


def _compute_check2(
    agro_tasks: list[dict],
    mtfmi_map: dict[int, list[dict]],
    machine_name: dict[int, str],
    operators_map: dict[int, str],
    work_type_map: dict[int, str],
    field_to_firm: dict[int, str],
) -> list[dict]:
    """
    Вероятный двойной след:
      work_area (полная) / covered_area (чистая) >= 1.75
    Колонки: Агрофирма | ФИО водителя | Вид работ |
             Чистая пл. | Полная пл. | Коэф. | Ссылка
    (Машина, Начало, Окончание — убраны по заданию)
    """
    rows: list[dict] = []
    for task in agro_tasks:
        tid   = task["id"]
        mid   = task.get("machine_id")
        wt_id = task.get("work_type_id")

        wt_name = work_type_map.get(wt_id, "—") if wt_id else "—"
        if _is_transport_work_type(wt_name):
            continue
        if wt_id and wt_id in _EXCLUDED_CHECK2_IDS:
            continue

        # Суммируем covered_area и work_area по всем items задания
        items_for_task  = mtfmi_map.get(tid, [])
        total_covered   = 0.0
        total_work      = 0.0
        firm_from_field = "—"

        for item in items_for_task:
            fid_raw = item.get("field_id")
            cov  = float(item.get("covered_area") or 0)
            work = float(item.get("work_area")    or 0)
            total_covered += cov
            total_work    += work
            if firm_from_field == "—" and fid_raw and cov > 0:
                firm_from_field = field_to_firm.get(int(fid_raw), "—")

        # Fallback: агрофирма из прямого field_id задания
        if firm_from_field == "—":
            direct_fid = task.get("field_id")
            if direct_fid:
                firm_from_field = field_to_firm.get(int(direct_fid), "—")

        if total_covered <= 0:
            continue
        ratio = total_work / total_covered
        if ratio < RATIO_THRESHOLD_CHECK2:
            continue

        try:
            op_id = int(task["driver_id"]) if task.get("driver_id") is not None else None
        except (TypeError, ValueError):
            op_id = None
        operator_name = operators_map.get(op_id, FIO_NOT_FOUND) if op_id else "—"

        rows.append({
            "firm":         firm_from_field,
            "operator":     operator_name,
            "work_type":    wt_name,
            "covered_area": round(total_covered, 2),
            "work_area":    round(total_work, 2),
            "ratio":        round(ratio, 2),
            "task_id":      tid,
            "machine_id":   mid,
            "link":         _task_link(mid, tid),
        })

    rows.sort(key=lambda r: (
        FIRM_ORDER.index(r["firm"]) if r["firm"] in FIRM_ORDER else 999,
        r["operator"],
    ))
    logger.info(f"check2 (двойной след): {len(rows)} rows")
    return rows


# =============================================================================
# ВЫБОРОЧНОЕ ОБНОВЛЕНИЕ — набор checks и зависимости fetch/compute
# =============================================================================

ALL_VIOLATION_CHECKS: frozenset[str] = frozenset({
    "check1", "check3", "check4", "check5", "check6", "check6t",
    "check9", "check10", "check2",
})

NON_CHECK1_VIOLATION_CHECKS: tuple[str, ...] = tuple(
    sorted(ALL_VIOLATION_CHECKS - {"check1"})
)

_CHECKS_NEED_2D_TASKS = frozenset({
    "check3", "check4", "check5", "check6", "check6t", "check9", "check10", "check2",
})
_CHECKS_NEED_AGRO_MTFMI = frozenset({
    "check3", "check4", "check5", "check9", "check10", "check2",
})
_CHECKS_NEED_CROPS = frozenset({"check3", "check4", "check5", "check9"})


def normalize_violation_checks(checks: list[str] | None) -> frozenset[str]:
    """None / пустой список → все checks; check6 автоматически включает check6t."""
    if not checks:
        return ALL_VIOLATION_CHECKS
    out: set[str] = set()
    for raw in checks:
        c = raw.strip().lower()
        if not c:
            continue
        if c == "check6":
            out.add("check6")
            out.add("check6t")
        elif c in ALL_VIOLATION_CHECKS:
            out.add(c)
    return frozenset(out) if out else ALL_VIOLATION_CHECKS


def split_violation_refresh_partitions(
    checks: list[str] | None,
) -> tuple[bool, list[str]]:
    """
    Делит запрос refresh на две независимые Celery-задачи (разные lock →
    два процесса воркера при concurrency≥2):
      • violations_refresh_check1 — «Нет агроопераций»
      • violations_refresh — все остальные checks

    checks=None / [] → обе партиции (полное обновление).
    """
    if checks is None or not checks:
        return True, list(NON_CHECK1_VIOLATION_CHECKS)

    normalized = normalize_violation_checks(checks)
    run_check1 = "check1" in normalized
    rest = sorted(normalized - {"check1"})
    return run_check1, rest


async def _fetch_check1_links_and_fields(
    session: aiohttp.ClientSession,
    check1_task_ids: list[int],
) -> tuple[dict[int, list[int]], dict[int, Optional[int]]]:
    """check1: mapping machine_task → agro_operation + field_id операций."""
    if not check1_task_ids:
        return {}, {}
    sem = asyncio.Semaphore(CHECK1_SEMAPHORE_LIMIT)
    check1_task_op_links = await _fetch_task_agro_op_links_by_task_ids(
        session,
        check1_task_ids,
        batch_size=_TASK_LINKS_BATCH_SIZE,
        sem=sem,
    )
    check1_op_ids: set[int] = set()
    for op_ids in check1_task_op_links.values():
        check1_op_ids.update(op_ids)
    check1_op_field_map = await _fetch_op_field_map(
        check1_op_ids, sem_limit=CHECK1_SEMAPHORE_LIMIT,
    )
    logger.info(
        f"check1 fetch: eligible task_ids={len(check1_task_ids)}, "
        f"linked agro_operation_id={len(check1_op_ids)}"
    )
    return check1_task_op_links, check1_op_field_map


async def _fetch_gps_phases(
    session: aiohttp.ClientSession,
    machine_is_agro: dict[int, bool],
    machine_is_truck: dict[int, bool],
    day_start: datetime,
    cutoff: datetime,
) -> tuple[
    tuple[dict, dict], tuple[dict, dict], list[datetime],
]:
    """GPS для check6 (агро) и check6t (грузовые) — параллельно между собой."""
    agri_ids = [mid for mid, flag in machine_is_agro.items() if flag]
    transport_ids = [mid for mid, flag in machine_is_truck.items() if flag]
    gps_dates = sorted({
        (day_start + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range((cutoff.date() - day_start.date()).days + 1)
    })
    gps_periods = _generate_periods(day_start, cutoff)
    agri_result, transport_result = await asyncio.gather(
        _fetch_gps_for_agri(session, agri_ids, gps_dates, day_start, cutoff, gps_periods),
        _fetch_gps_for_agri(session, transport_ids, gps_dates, day_start, cutoff, gps_periods),
    )
    logger.info(
        f"check6 GPS: agri_ids={len(agri_ids)}, active={len(agri_result[0])}; "
        f"check6t: truck_ids={len(transport_ids)}, active={len(transport_result[0])}"
    )
    return agri_result, transport_result, gps_periods


async def refresh_violations(
    checks: list[str] | None = None,
    *,
    progress: ProgressFn = None,
) -> dict:
    """
    Загружает справочники и задания, вычисляет запрошенные checks и
    сохраняет в Redis (merge — только обновлённые ключи).

    checks=None — все проверки (часовой beat и кнопка «Обновить»).
    checks=['check3','check6'] — только выбранные (без check1 и т.п.).
    """
    checks_set = normalize_violation_checks(checks)
    need_check1 = "check1" in checks_set
    need_gps = bool(checks_set & {"check6", "check6t"})
    need_check10 = "check10" in checks_set
    need_2d_tasks = bool(checks_set & _CHECKS_NEED_2D_TASKS)
    need_agro_mtfmi = bool(checks_set & _CHECKS_NEED_AGRO_MTFMI)
    need_crops = bool(checks_set & _CHECKS_NEED_CROPS)
    need_implements = "check3" in checks_set
    need_materials = need_check10

    report_date       = date.today()
    day_start, cutoff = _shift_window(report_date)
    day_start_str     = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str        = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    check1_day_start, check1_cutoff = _check1_window(report_date)
    check1_day_start_str = check1_day_start.strftime("%Y-%m-%dT%H:%M:%S")
    check1_cutoff_str    = check1_cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    _now_str = datetime.now(tz=TZ_MSK).strftime("%Y-%m-%dT%H:%M:%S")

    logger.info(
        f"violations refresh start: {day_start_str} — {cutoff_str}, "
        f"checks={sorted(checks_set)}"
    )
    if need_check1:
        logger.info(f"check1 window: {check1_day_start_str} — {check1_cutoff_str}")
        _emit_progress(
            progress,
            f"check1: окно {check1_day_start_str} — {check1_cutoff_str}",
        )

    async def _empty() -> list:
        return []

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        work_types_prefetched: list[dict] | None = None
        check1_machine_tasks_raw: list[dict] = []

        if need_check1:
            work_types_prefetched = await _fetch_all_pages(session, "work_types")
            allowed_wt_ids = _check1_allowed_work_type_ids(work_types_prefetched)
            logger.info(
                f"check1: allowed work_type_ids={len(allowed_wt_ids)} "
                f"(excl. transport 7.x/8.x and CHECK1_EXCLUDED)"
            )
            _emit_progress(
                progress,
                f"check1: allowed work_type_ids={len(allowed_wt_ids)} "
                f"(без транспорта 7.x/8.x)",
            )
            check1_machine_tasks_raw = await _fetch_check1_machine_tasks(
                session,
                check1_day_start_str,
                check1_cutoff_str,
                allowed_wt_ids,
            )
            _emit_progress(
                progress,
                f"check1 machine_tasks fetch: unique={len(check1_machine_tasks_raw)}",
            )

        fetch_tasks: list = []

        if need_2d_tasks:
            fetch_tasks.extend([
                _fetch_all_pages(session, "machine_tasks", {
                    "end_time_gt_eq": day_start_str,
                    "start_time_lt":  cutoff_str,
                }),
                _fetch_all_pages(session, "machine_tasks", {
                    "start_time_gt_eq": day_start_str,
                    "start_time_lt":  cutoff_str,
                }),
            ])
        else:
            fetch_tasks.extend([_empty(), _empty()])

        fetch_tasks.append(_empty())  # check1 tasks: prefetched above when need_check1

        fetch_tasks.extend([
            _fetch_all_pages(session, "machines"),
            (
                _empty()
                if work_types_prefetched is not None
                else _fetch_all_pages(session, "work_types")
            ),
            _fetch_all_pages(session, "fields"),
            _fetch_all_pages(session, "field_groups"),
            _fetch_all_pages(session, "group_folders"),
            _fetch_all_pages(session, "users"),
        ])

        if need_implements:
            fetch_tasks.append(_fetch_all_pages(session, "implements"))
        else:
            fetch_tasks.append(_empty())

        if need_crops:
            fetch_tasks.extend([
                _fetch_all_pages(session, "crops"),
                _fetch_history_items_for_date_range(session, report_date, report_date),
            ])
        else:
            fetch_tasks.extend([_empty(), _empty()])

        fetch_tasks.append(_fetch_all_pages(session, "inventory_history_items", {
            "historyable_type": "Machine",
            "hidden":           "true",
            "event_start_at":   _now_str,
            "event_end_at":     _now_str,
        }))

        if need_materials:
            fetch_tasks.extend([
                _fetch_all_pages(session, "fertilizers"),
                _fetch_all_pages(session, "seeds"),
                _fetch_all_pages(session, "chemicals"),
            ])
        else:
            fetch_tasks.extend([_empty(), _empty(), _empty()])

        (
            machine_tasks_by_end,
            machine_tasks_by_start,
            _check1_tasks_slot,
            machines_raw,
            work_types_from_gather,
            fields_raw,
            fgs_raw,
            gfs_raw,
            users_raw,
            implements_raw,
            crops_raw,
            history_items_raw,
            inventory_history_raw,
            fertilizers_raw,
            seeds_raw,
            chemicals_raw,
        ) = await asyncio.gather(*fetch_tasks)

        work_types_raw = (
            work_types_prefetched
            if work_types_prefetched is not None
            else work_types_from_gather
        )
        if not need_check1:
            check1_machine_tasks_raw = _check1_tasks_slot

        machine_tasks_raw: list[dict] = []
        if need_2d_tasks:
            machine_tasks_raw_by_id: dict[int, dict] = {}
            for t in (machine_tasks_by_end + machine_tasks_by_start):
                tid = t.get("id")
                if tid is not None:
                    machine_tasks_raw_by_id[int(tid)] = t
            machine_tasks_raw = list(machine_tasks_raw_by_id.values())

        logger.info(
            f"violations fetch done: tasks={len(machine_tasks_raw)} "
            f"(by_end={len(machine_tasks_by_end)}, by_start={len(machine_tasks_by_start)}), "
            f"check1_tasks_raw={len(check1_machine_tasks_raw)}, "
            f"machines={len(machines_raw)}, fields={len(fields_raw)}"
        )

        hidden_ids: set[int] = {
            int(item["historyable_id"])
            for item in inventory_history_raw
            if item.get("historyable_id") is not None
        }
        logger.info(f"hidden machines: {len(hidden_ids)}")

        seen_ids: set[int] = set()
        unique_tasks: list[dict] = []
        for t in machine_tasks_raw:
            tid = t.get("id")
            if tid not in seen_ids:
                seen_ids.add(tid)
                unique_tasks.append(t)

        check1_unique_tasks: list[dict] = []
        if need_check1:
            check1_seen_ids: set[int] = set()
            for t in check1_machine_tasks_raw:
                tid = t.get("id")
                if tid not in check1_seen_ids:
                    check1_seen_ids.add(tid)
                    check1_unique_tasks.append(t)

        TRUCK_SUBTYPES = {"lorrie", "semi_truck"}

        machine_name: dict[int, str] = {}
        machine_plate: dict[int, str] = {}
        machine_is_agro: dict[int, bool] = {}
        machine_is_truck: dict[int, bool] = {}
        machine_is_hired: dict[int, bool] = {}
        machine_to_firm: dict[int, str] = {}
        parent_map = _folder_parent_map(gfs_raw)
        root_ids   = set(FOLDER_TO_FIRM.keys())
        for m in machines_raw:
            mid = m.get("id")
            if mid is None:
                continue
            mid = int(mid)
            machine_name[mid] = (
                m.get("name") or m.get("title") or m.get("model") or f"machine_{mid}"
            )
            machine_plate[mid] = (m.get("registration_number") or "").strip() or None
            is_hidden = mid in hidden_ids
            mtype    = (m.get("machine_type") or "").lower()
            msubtype = (m.get("machine_subtype") or "").lower()
            machine_is_agro[mid]  = (mtype != "transport") and not is_hidden
            machine_is_truck[mid] = (
                mtype == "transport"
                and msubtype in TRUCK_SUBTYPES
                and not is_hidden
            )
            raw_group_id = m.get("machine_group_id")
            machine_is_hired[mid] = (
                raw_group_id is not None and int(raw_group_id) == HIRED_GROUP_ID
            )
            raw_folder = m.get("group_folder_id")
            if raw_folder is not None:
                root = _resolve_firm(int(raw_folder), parent_map, root_ids)
                if root is not None:
                    firm = FOLDER_TO_FIRM.get(root)
                    if firm:
                        machine_to_firm[mid] = firm

        work_type_map: dict[int, str] = {
            w["id"]: (w.get("name") or w.get("title") or f"wt_{w['id']}")
            for w in work_types_raw
        }

        field_name: dict[int, str] = {
            int(f["id"]): (f.get("name") or f.get("title") or f"field_{f['id']}")
            for f in fields_raw if f.get("id") is not None
        }
        field_to_firm = _build_field_to_firm(fields_raw, fgs_raw, gfs_raw)

        local_fio_by_id = await fetch_local_fio_map()
        operators_map: dict[int, str] = {}
        for u in users_raw:
            uid = u.get("id")
            if uid is None:
                continue
            uid = int(uid)
            operators_map[uid] = (
                local_fio_by_id.get(uid)
                or u.get("username") or u.get("email")
                or FIO_NOT_FOUND
            )

        implements_map: dict[int, dict] = {}
        if need_implements:
            implements_map = {
                int(impl["id"]): impl
                for impl in implements_raw if impl.get("id") is not None
            }

        material_map: dict[int, str] = {}
        if need_materials:
            for raw_list in (fertilizers_raw, seeds_raw, chemicals_raw):
                for item in raw_list:
                    mid_m = item.get("id")
                    if mid_m:
                        material_map[int(mid_m)] = (
                            item.get("name") or item.get("title") or f"id={mid_m}"
                        )

        field_to_crop: dict[int, str] = {}
        if need_crops:
            crop_id_name: dict[int, str] = {
                c["id"]: (c.get("name") or c.get("title", "")) for c in crops_raw
            }
            for hi in history_items_raw:
                fid = hi.get("field_id")
                cid = hi.get("crop_id")
                if fid and cid and hi.get("active"):
                    cname = crop_id_name.get(cid, "")
                    if cname:
                        field_to_crop[int(fid)] = cname

        agro_tasks: list[dict] = []
        if need_2d_tasks:
            agro_tasks = [
                t for t in unique_tasks
                if machine_is_agro.get(int(t["machine_id"]) if t.get("machine_id") else -1, True)
            ]
            logger.info(f"violations: agro tasks={len(agro_tasks)}")

        check1_tasks: list[dict] = []
        check1_task_ids: list[int] = []
        if need_check1:
            logger.info(
                f"check1: tasks ({CHECK1_LOOKBACK_DAYS}d, до фильтра по видам работ)="
                f"{len(check1_unique_tasks)}"
            )
            _emit_progress(
                progress,
                f"check1: tasks ({CHECK1_LOOKBACK_DAYS}d, до фильтра)="
                f"{len(check1_unique_tasks)}",
            )
            check1_tasks = _check1_eligible_tasks(check1_unique_tasks, work_type_map)
            check1_task_ids = [t["id"] for t in check1_tasks]
            _emit_progress(
                progress,
                f"check1: eligible tasks={len(check1_tasks)}",
            )

        mtfmi_map: dict[int, list[dict]] = {}
        if need_agro_mtfmi or need_check1:
            mtfmi_task_ids: list[int] = []
            if need_agro_mtfmi:
                mtfmi_task_ids.extend(t["id"] for t in agro_tasks)
            if need_check1:
                mtfmi_task_ids.extend(check1_task_ids)
            if need_check1:
                _emit_progress(
                    progress,
                    f"check1: загрузка MTFMI для {len(check1_task_ids)} задач...",
                )
            mtfmi_map = await _fetch_mtfmi_batch(session, mtfmi_task_ids)
            logger.info(f"mtfmi merged fetch: unique task_ids={len(set(mtfmi_task_ids))}")
            if need_check1:
                _emit_progress(
                    progress,
                    f"check1: MTFMI загружен для {len(check1_task_ids)} задач",
                )

        check1_mtfmi_map = (
            {tid: mtfmi_map.get(tid, []) for tid in check1_task_ids}
            if need_check1 else {}
        )

        parallel: list[tuple[str, asyncio.Task]] = []

        if need_check1:
            _emit_progress(progress, "check1: загрузка mapping agro_operations...")
            parallel.append(("check1_fetch", asyncio.create_task(
                _fetch_check1_links_and_fields(session, check1_task_ids)
            )))
        if need_gps:
            parallel.append(("gps", asyncio.create_task(
                _fetch_gps_phases(session, machine_is_agro, machine_is_truck, day_start, cutoff)
            )))
        if need_check10:
            parallel.append(("check10", asyncio.create_task(
                _compute_check10(
                    session, agro_tasks, mtfmi_map,
                    machine_name, operators_map, work_type_map,
                    field_to_firm, material_map,
                )
            )))

        phase_results: dict[str, object] = {}
        if parallel:
            names, tasks = zip(*parallel)
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for name, result in zip(names, gathered):
                if isinstance(result, Exception):
                    logger.error(f"violations phase {name} failed: {result}", exc_info=result)
                    raise result
                phase_results[name] = result

        check1_task_op_links: dict[int, list[int]] = {}
        check1_op_field_map: dict[int, Optional[int]] = {}
        if need_check1:
            check1_task_op_links, check1_op_field_map = phase_results["check1_fetch"]
            check1_op_ids_count = sum(
                len(op_ids) for op_ids in check1_task_op_links.values()
            )
            _emit_progress(
                progress,
                f"check1 fetch: eligible task_ids={len(check1_task_ids)}, "
                f"linked agro_operation_id={check1_op_ids_count}",
            )

        gps_periods: list[datetime] = []
        km_by_machine_by_period: dict = {}
        km_by_transport_by_period: dict = {}
        if need_gps:
            (_, km_by_machine_by_period), (_, km_by_transport_by_period), gps_periods = (
                phase_results["gps"]
            )

        check10_rows: list[dict] | None = None
        if need_check10:
            check10_rows = phase_results["check10"]

    updates: dict[str, list] = {}

    if need_check1:
        _emit_progress(progress, "check1: расчёт нарушений...")
        updates["check1"] = _compute_check1(
            check1_tasks, check1_mtfmi_map, check1_task_op_links, check1_op_field_map,
            machine_name, operators_map, work_type_map, field_to_firm, check1_cutoff,
            machine_plate=machine_plate,
        )
        _emit_progress(
            progress,
            f"check1: готово, нарушений={len(updates['check1'])}",
        )
    if "check3" in checks_set:
        updates["check3"] = _compute_check3(
            agro_tasks, mtfmi_map, machine_name, implements_map,
            work_type_map, field_to_firm, field_name, field_to_crop, cutoff,
            machine_is_hired=machine_is_hired,
        )
    if "check4" in checks_set:
        updates["check4"] = _compute_check4(
            agro_tasks, mtfmi_map, machine_name, operators_map,
            work_type_map, field_to_firm, field_name, field_to_crop, cutoff,
        )
    if "check5" in checks_set:
        updates["check5"] = _compute_check5(
            agro_tasks, mtfmi_map, machine_name, operators_map,
            work_type_map, field_to_firm, field_name, field_to_crop, cutoff,
        )

    if need_gps:
        _task_periods_by_machine = _build_task_periods_by_machine(unique_tasks, gps_periods)
        if "check6" in checks_set:
            updates["check6"] = _compute_check6(
                machine_name=machine_name,
                machine_is_agro=machine_is_agro,
                machine_to_firm=machine_to_firm,
                task_periods_by_machine=_task_periods_by_machine,
                km_by_machine_by_period=km_by_machine_by_period,
            )
        if "check6t" in checks_set:
            updates["check6t"] = _compute_check6_transport(
                machine_name=machine_name,
                machine_is_truck=machine_is_truck,
                machine_to_firm=machine_to_firm,
                task_periods_by_machine=_task_periods_by_machine,
                km_by_machine_by_period=km_by_transport_by_period,
            )

    if "check9" in checks_set:
        _forbidden_ops_raw = get_forbidden_ops_config()
        _forbidden_ops: dict[str, set[str]] = {
            crop: set(ops) for crop, ops in _forbidden_ops_raw.items()
        }
        updates["check9"] = _compute_check9(
            agro_tasks=agro_tasks,
            mtfmi_map=mtfmi_map,
            work_type_map=work_type_map,
            field_to_firm=field_to_firm,
            field_to_crop=field_to_crop,
            cutoff=cutoff,
            forbidden_ops=_forbidden_ops,
        )

    if need_check10:
        updates["check10"] = check10_rows or []

    if "check2" in checks_set:
        updates["check2"] = _compute_check2(
            agro_tasks=agro_tasks,
            mtfmi_map=mtfmi_map,
            machine_name=machine_name,
            operators_map=operators_map,
            work_type_map=work_type_map,
            field_to_firm=field_to_firm,
        )

    now = datetime.now(tz=TZ_MSK)

    from app.cropwise.violations_router import merge_cache
    merge_cache(updates, now)

    result = {k: len(v) for k, v in updates.items()}
    result["checks"] = sorted(checks_set)
    logger.info(f"violations done: {result}")
    return result
