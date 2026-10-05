"""
check3_season_mismatch.py
=========================
Отчёт: «Несоответствие сезона задачи и агрооперации»
Ищет случаи, когда machine_task и связанная с ней agro_operation
находятся в разных сезонах.

Связь: machine_task_agro_operation_mapping_items
  machine_task_id  →  machine_task.season
  agro_operation_id → agro_operation.season

Пример нарушения:
  machine_task #376071 (season=2025, май 2025)
  связан с agro_operation #89717 (season=2026)

Алгоритм:
  1. Загружаем machine_tasks за SCAN_SEASONS (season_gt_eq/season_lt_eq,
     проверено на реальном API) ОКНАМИ по created_at (created_at_gt_eq /
     created_at_lt, проверено на реальном API), окна качаются ПАРАЛЛЕЛЬНО
     (см. client._fetch_windowed) → dict[id → task]. Раньше это был один
     последовательный курсор (~335 страниц, ~11 минут).
  2. ОДНОВРЕМЕННО с п.1 грузим ВСЕ mapping_items (тоже окнами по created_at),
     в памяти оставляем только пары (machine_task_id, agro_operation_id) и
     после загрузки отсекаем те, чьей задачи нет в tasks_map. Друг от друга
     эти два шага не зависят, поэтому идут параллельно.
  3. Из отфильтрованных пар собираем ТОЧНОЕ множество agro_operation_id и
     грузим ИМЕННО их через POST /agro_operations/mass_request (пачками по
     500 id) — агрооперация задачи может быть из СОВСЕМ ДРУГОГО сезона (в
     этом и суть проверки), поэтому фильтр ops по SCAN_SEASONS терял бы такие
     нарушения.
  4. Для каждой пары: год задачи != год агрооперации → КАНДИДАТ в нарушения.
  5. Транзитные пары (machine_task_field_mapping_items, in_transit=True)
     запрашиваем ТОЛЬКО для задач-кандидатов (по machine_task_id пачками) и
     исключаем из результата. Транзит — чисто исключающий фильтр поверх
     найденных нарушений, поэтому результат тот же, что при полной выгрузке
     mtfmi, но без выгрузки всей таблицы.

  mass_request НЕ применяется к mapping_items/mtfmi: он возвращает объекты
  ресурса по их СОБСТВЕННЫМ id, а у нас известен только внешний ключ.

Колонки: Агрофирма | Поле | Вид работ | Сезон задачи | Сезон агроопер. |
          Начало задачи | Конец задачи | ID задачи | ID агроопер.
"""
from __future__ import annotations
import asyncio
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import aiohttp
import httpx
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.cropwise.client import (
    CROPWISE_HEADERS,
    _fetch_mass_request,
    _fetch_mtfmi_by_task_ids,
    _fetch_windowed,
    make_created_at_windows,
)
from app.logging_config import get_logger

logger = get_logger("cropwise.check12.sync")
_ECHO = False  # True при запуске как скрипта: дублируем лог в stdout


def _log(msg: str) -> None:
    logger.info(msg)
    if _ECHO:
        print(msg, flush=True)

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================
try:
    from plans import (
        API_TOKEN, BASE_URL, TZ_MSK,
        FIRM_ORDER, FIRM_TO_FOLDER, FOLDER_TO_FIRM,
    )
except ImportError:
    try:
        from plans_1 import (
            API_TOKEN, BASE_URL, TZ_MSK,
            FIRM_ORDER, FIRM_TO_FOLDER, FOLDER_TO_FIRM,
        )
    except ImportError:
        API_TOKEN      = "6DayNcBDr9BWEZgst1Dy"
        BASE_URL       = "https://operations.cropwise.com/api/v3"
        TZ_MSK         = timezone(timedelta(hours=3))
        FIRM_ORDER     = [
            "АФ Чистопольская", "АгроРаздолье", "Аксу-Агро",
            "НурлатАгро", "Дрожжаное Рассвет Агро", "Актай",
            "Лениногорская Птицефабрика",
        ]
        FIRM_TO_FOLDER = {
            "АФ Чистопольская": 3, "АгроРаздолье": 4, "Аксу-Агро": 5,
            "НурлатАгро": 11, "Дрожжаное Рассвет Агро": 6,
            "Актай": 7, "Лениногорская Птицефабрика": 9,
        }
        FOLDER_TO_FIRM = {v: k for k, v in FIRM_TO_FOLDER.items()}

_cur = date.today().year
SCAN_SEASONS: list[int] = [
    int(y) for y in os.environ.get(
        "SCAN_SEASONS",
        f"{_cur - 1},{_cur},{_cur + 1}",
    ).split(",")
]

# Параллельных запросов к Cropwise (общий семафор на выгрузку задач/mapping_items)
SEMAPHORE_LIMIT = int(os.environ.get("CHECK12_CONCURRENCY", "40"))
# Окна по created_at для параллельной выгрузки tasks и mapping_items.
# Первое окно открыто слева, последнее справа, поэтому WINDOW_START влияет
# только на баланс нагрузки, а не на полноту данных.
WINDOW_START = date.fromisoformat(os.environ.get("CHECK12_WINDOW_START", "2023-09-01"))
WINDOW_STEP_DAYS = int(os.environ.get("CHECK12_WINDOW_STEP_DAYS", "14"))
# Игнорируем агрооперации строго ниже этого года
MIN_OP_YEAR = int(os.environ.get("MIN_OP_YEAR", "2025"))
UPDATE_INTERVAL = 3600
OUTPUT_FILE     = "check3_season_mismatch.xlsx"

# Статусы, которые принимает Cropwise API (см. field_area_router.py — тот же справочник)
STATUS_LABELS = {
    "planned":     "Запланировано",
    "in_progress": "В процессе",
    "done":        "Выполнено",
    "canceled":    "Отменено",
}
STATUS_COLORS = {
    "planned":     "DDEBF7",   # голубой
    "in_progress": "FCE4D6",   # оранжевый
    "done":        "E2EFDA",   # зелёный
    "canceled":    "F2F2F2",   # серый
}

# =============================================================================
# ASYNC HTTP
# =============================================================================
def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"X-User-Api-Token": API_TOKEN},
        timeout=120,
    )

async def _get_all(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict | None = None,
    limit: int = 1000,
) -> list[dict]:
    """Cursor-based пагинация (from_id).

    Останавливаемся только когда:
      - chunk пустой, ИЛИ
      - API не вернул last_record_id
    НЕ останавливаемся по obtained_records < limit — разные эндпоинты
    имеют разный максимальный размер страницы (fields отдаёт по 100,
    а не по 1000), и проверка < limit обрывала бы пагинацию после первой страницы.
    """
    p, results, from_id = dict(params or {}), [], 0
    for _ in range(10_000):
        r = await client.get(
            f"{BASE_URL}/{endpoint}",
            params={**p, "from_id": from_id, "limit": limit},
        )
        r.raise_for_status()
        body  = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        results.extend(chunk)
        meta    = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id:
            break
        from_id = last_id + 1
    return results

async def _get_all_page(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict | None = None,
) -> list[dict]:
    """Page-based пагинация."""
    p = dict(params or {})
    p.setdefault("per_page", 1000)
    results, page = [], 1
    while True:
        r = await client.get(
            f"{BASE_URL}/{endpoint}",
            params={**p, "page": page},
        )
        r.raise_for_status()
        body  = r.json()
        chunk = body if isinstance(body, list) else body.get("data", [])
        results.extend(chunk)
        if len(chunk) < p["per_page"]:
            break
        page += 1
    return results

async def _get_any(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict | None = None,
) -> list[dict]:
    data = await _get_all(client, endpoint, params)
    if not data:
        data = await _get_all_page(client, endpoint, params)
    return data

# =============================================================================
# ПАРСИНГ
# =============================================================================
def parse_dt(s) -> Optional[datetime]:
    if not s:
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
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_MSK)
    else:
        dt = dt.astimezone(TZ_MSK)
    return dt

def _season_int(raw) -> Optional[int]:
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None

# =============================================================================
# ИЕРАРХИЯ ПАПОК
# =============================================================================
def _folder_parent_map(gfs: list[dict]) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
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

def build_field_to_firm(fields_all, fgs_all, gfs_all) -> dict[int, str]:
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

# =============================================================================
# ЗАГРУЗКА СПРАВОЧНИКОВ
# =============================================================================
async def load_reference_data_async() -> tuple:
    async with _make_client() as client:
        print("  Справочники (параллельно)...")
        fields_all, fgs_all, gfs_all, wt_raw = await asyncio.gather(
            _get_any(client, "fields"),
            _get_all_page(client, "field_groups"),
            _get_any(client, "group_folders"),
            _get_any(client, "work_types"),
        )
    field_to_firm = build_field_to_firm(fields_all, fgs_all, gfs_all)
    field_name: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        field_name[int(fid)] = f.get("name") or f.get("title") or f"field_{fid}"

    field_slug: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        field_slug[int(fid)] = f.get("external_id") or f.get("slug") or str(fid)

    work_type_map: dict[int, str] = {
        w["id"]: (w.get("name") or w.get("title") or f"wt_{w['id']}")
        for w in wt_raw
    }
    print(f"    Полей: {len(field_name)}, видов работ: {len(work_type_map)}")
    return field_to_firm, field_slug, field_name, work_type_map

# =============================================================================
# ЗАГРУЗКА machine_tasks (ОКНАМИ по created_at, параллельно)
# =============================================================================
async def load_tasks_async(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    windows: list[dict],
) -> dict[int, dict]:
    """
    machine_tasks за SCAN_SEASONS.

    Фильтр season_gt_eq/season_lt_eq на machine_tasks работает (проверено на
    реальном API), но один курсор по ~335 тысячам задач — это ~335
    последовательных страниц. Поэтому запрос разбит на окна по created_at
    (точное разбиение оси времени, см. make_created_at_windows), окна
    качаются параллельно.

    SCAN_SEASONS может быть непрерывным не обязательно (напр. "2024,2026"), поэтому
    после выгрузки диапазона дополнительно фильтруем по ТОЧНОМУ множеству
    сезонов.

    Каждое окно проецируется сразу в нужные поля (start_time/end_time/
    work_type_id/machine_id) — сырые объекты (~20-30 полей) не копятся.
    """
    seasons_set = set(SCAN_SEASONS)
    season_min, season_max = min(SCAN_SEASONS), max(SCAN_SEASONS)
    raw_total = 0

    def project(records: list[dict]) -> list[tuple[int, dict]]:
        nonlocal raw_total
        raw_total += len(records)
        out: list[tuple[int, dict]] = []
        for t in records:
            if _season_int(t.get("season")) not in seasons_set:
                continue
            tid = t.get("id")
            if tid is None:
                continue
            out.append((int(tid), {
                "start_time":   t.get("start_time"),
                "end_time":     t.get("end_time"),
                "work_type_id": t.get("work_type_id"),
                "machine_id":   t.get("machine_id"),
            }))
        return out

    items = await _fetch_windowed(
        session, "machine_tasks", windows,
        {"season_gt_eq": season_min, "season_lt_eq": season_max},
        project=project, sem=sem, label="machine_tasks",
    )
    tasks_map = dict(items)  # дедуп по id
    _log(
        f"    Задач (machine_tasks) за сезоны {SCAN_SEASONS}: {len(tasks_map)} "
        f"(получено от API {raw_total})"
    )
    return tasks_map


# =============================================================================
# MASS_REQUEST (POST .../mass_request по СОБСТВЕННЫМ id ресурса)
# =============================================================================
# Раньше здесь была локальная копия этой функции (httpx, без retry).
# Теперь используем общий app.cropwise.client._fetch_mass_request — тот же
# алгоритм (POST .../mass_request чанками по 500 id, см. "JSON format for
# requesting of big number of objects" в документации Cropwise), но с
# retry на timeout/5xx/обрыв соединения, как в violations_sync.py и
# speed_violations_sync.py. Применим он только когда заранее известны id
# самих объектов ресурса — не путать с фильтрацией по внешнему ключу
# (для неё используется обычный GET-список с параметром, см. _get_any).

async def load_ops_by_ids(op_ids: set[int]) -> dict[int, dict]:
    """
    Агрооперации грузим ТОЧЕЧНО — по конкретным id, реально встреченным в
    mapping_items наших задач (POST /agro_operations/mass_request), а НЕ по
    фильтру season за SCAN_SEASONS, как раньше.

    Почему это важнее, чем просто "быстрее": агрооперация, с которой
    сопоставлена задача, может быть из СОВСЕМ ДРУГОГО сезона — в этом и
    состоит проверяемое нарушение. При загрузке ops только за SCAN_SEASONS
    такие случаи (op.season вне диапазона SCAN_SEASONS) раньше не находились
    вообще: op не попадал в ops_map → строка тихо пропускалась (op is None).
    mass_request по конкретным id закрывает эту дыру и не требует
    вытягивать вообще все агрооперации организации за 3 сезона.

    Используем общий _fetch_mass_request из client.py (как в
    violations_sync.py::_fetch_op_field_map / speed_violations_sync.py) —
    та же логика чанками по 500 id, но с retry на timeout/5xx/обрыв
    соединения, вместо локальной копии без повторов. И, как и в
    load_tasks_async, сразу проецируем каждую операцию до используемых
    ниже по коду полей — planned_start_date/field_id/status.
    """
    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        records = await _fetch_mass_request(
            session, "agro_operations", list(op_ids), sem=sem,
        )

    ops_map: dict[int, dict] = {}
    for op in records:
        oid = op.get("id")
        if oid is None:
            continue
        ops_map[int(oid)] = {
            "planned_start_date": op.get("planned_start_date"),
            "field_id":           op.get("field_id"),
            "status":             op.get("status"),
        }
    del records

    print(
        f"    Агроопераций по id из mapping_items (mass_request): "
        f"{len(ops_map)} из {len(op_ids)} запрошенных"
    )
    return ops_map

# =============================================================================
# ЗАГРУЗКА ВСЕХ MAPPING ITEMS (окнами по created_at, фильтр в памяти)
# =============================================================================
async def load_mapping_items_windowed(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    windows: list[dict],
) -> dict[int, tuple[int, int]]:
    """
    Грузит ВСЕ mapping_items окнами по created_at (параллельно) и возвращает
    {id записи: (machine_task_id, agro_operation_id)}.

    Не зависит от tasks_map, поэтому идёт одновременно с загрузкой задач;
    фильтрация по нужным задачам делается позже, в collect_data_async.
    Параметр created_at[from/to] сервер ИГНОРИРУЕТ, а created_at_gt_eq /
    created_at_lt — работают (проверено на реальном API).

    Дедуп по id записи гарантирует, что стык окон не создаст дубль, а
    порядок пар детерминирован (по id) — как при курсорной выгрузке.
    Каждое окно проецируется сразу в три целых числа.
    """
    def project(records: list[dict]) -> list[tuple[int, int, int]]:
        out: list[tuple[int, int, int]] = []
        for item in records:
            iid = item.get("id")
            tid_raw = item.get("machine_task_id")
            oid_raw = item.get("agro_operation_id")
            if iid is None or tid_raw is None or oid_raw is None:
                continue
            out.append((int(iid), int(tid_raw), int(oid_raw)))
        return out

    triples = await _fetch_windowed(
        session, "machine_task_agro_operation_mapping_items", windows,
        project=project, sem=sem, label="mapping_items",
    )
    items = {iid: (tid, oid) for iid, tid, oid in triples}
    _log(f"    mapping_items всего: {len(items)}")
    return items


async def load_transit_pairs_for_tasks(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    task_ids: set[int],
) -> frozenset[tuple[int, int]]:
    """
    Транзитные пары (task_id, field_id) из machine_task_field_mapping_items
    (in_transit=True) ТОЛЬКО для переданных задач — пачками по machine_task_id
    (client._fetch_mtfmi_by_task_ids). Вызывается для задач-кандидатов в
    нарушения, а не для всей таблицы mtfmi.
    """
    if not task_ids:
        return frozenset()
    mtfmi_map = await _fetch_mtfmi_by_task_ids(session, sorted(task_ids), sem=sem)
    transit_pairs: set[tuple[int, int]] = set()
    for tid, items in mtfmi_map.items():
        for item in items:
            if not item.get("in_transit"):
                continue
            fid_raw = item.get("field_id")
            if fid_raw is None:
                continue
            transit_pairs.add((int(tid), int(fid_raw)))
    return frozenset(transit_pairs)

# =============================================================================
# СБОР ДАННЫХ
# =============================================================================
async def collect_data_async(
    field_to_firm: dict,
    field_slug: dict,
    field_name: dict,
    work_type_map: dict,
) -> list[dict]:
    t_total = time.monotonic()
    timings: dict[str, float] = {}
    windows = make_created_at_windows(WINDOW_START, WINDOW_STEP_DAYS)
    _log(
        f"\n  Загружаем задачи и mapping_items параллельно: сезоны {SCAN_SEASONS}, "
        f"окон по created_at: {len(windows)}, параллелизм: {SEMAPHORE_LIMIT}"
    )

    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)

    async def _timed(name: str, coro):
        t0 = time.monotonic()
        try:
            return await coro
        finally:
            timings[name] = time.monotonic() - t0

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        tasks_map, mapping_items = await asyncio.gather(
            _timed("tasks", load_tasks_async(session, sem, windows)),
            _timed("mapping_items", load_mapping_items_windowed(session, sem, windows)),
        )
        if not tasks_map:
            _log("    Задач не найдено, выход.")
            return []

        # Только пары наших задач; порядок — по id записи mapping_item
        mapping_pairs = [
            pair for _iid, pair in sorted(mapping_items.items())
            if pair[0] in tasks_map
        ]
        del mapping_items
        _log(f"    Всего mapping_items (наши сезоны): {len(mapping_pairs)}")

        t0 = time.monotonic()
        op_ids_needed = {oid for _tid, oid in mapping_pairs}
        ops_map = await load_ops_by_ids(op_ids_needed)
        timings["agro_operations"] = time.monotonic() - t0

        # --- Фаза 1: кандидаты (год задачи != год агрооперации) ---------------
        candidates: list[tuple] = []
        for task_id, op_id in mapping_pairs:
            task = tasks_map.get(task_id)
            op   = ops_map.get(op_id)

            if task is None or op is None:
                continue

            # --- Сравниваем год задачи и год planned_start_date агрооперации ---
            task_start_dt = parse_dt(task.get("start_time"))
            if task_start_dt is None:
                continue

            op_planned_start = parse_dt(op.get("planned_start_date"))
            if op_planned_start is None:
                continue

            task_year = task_start_dt.year
            op_year   = op_planned_start.year

            # Пропускаем агрооперации 2024 года и старше
            if op_year < MIN_OP_YEAR:
                continue

            if task_year == op_year:
                continue  # всё нормально

            fid_raw = op.get("field_id")
            fid     = int(fid_raw) if fid_raw else None
            candidates.append(
                (task_id, op_id, task, op, task_start_dt, op_planned_start, fid)
            )

        _log(f"    Кандидатов (год задачи ≠ год начала агрооперации): {len(candidates)}")

        # --- Фаза 2: транзитные пары только для задач-кандидатов --------------
        t0 = time.monotonic()
        cand_task_ids = {c[0] for c in candidates if c[6] is not None}
        transit_pairs = await load_transit_pairs_for_tasks(session, sem, cand_task_ids)
        timings["transit"] = time.monotonic() - t0
        _log(
            f"    Транзитных пар для {len(cand_task_ids)} задач-кандидатов: "
            f"{len(transit_pairs)}"
        )

    rows: list[dict] = []
    for task_id, op_id, task, op, task_start_dt, op_planned_start, fid in candidates:
        task_year = task_start_dt.year
        op_year   = op_planned_start.year

        # Пропускаем, если именно это поле агрооперации помечено транзитом в задаче
        if fid is not None and (int(task_id), fid) in transit_pairs:
            continue

        firm    = field_to_firm.get(fid, "—") if fid else "—"
        fname   = field_name.get(fid, f"field_{fid}") if fid else "—"

        wt_id   = task.get("work_type_id")
        wt_name = work_type_map.get(wt_id, "—") if wt_id else "—"

        end_dt = parse_dt(task.get("end_time"))

        # Статус — у агрооперации (op), а не у задания (task): именно это поле
        # фильтруется и отображается в панели «Обработанная площадь поля».
        api_status   = (op.get("status") or "").lower()
        status_key   = api_status
        status_label = STATUS_LABELS.get(api_status, api_status or "—")

        slug = field_slug.get(fid, str(fid)) if fid else None
        ao_link = (
            f"https://operations.cropwise.com/fields/{slug}/agro_operations/{op_id}"
            if slug and op_id else ""
        )
        mid = task.get("machine_id")
        task_link = (
            f"https://operations.cropwise.com/machines/{mid}/tasks/{task_id}"
            if mid and task_id else ""
        )

        rows.append({
            "firm":      firm,
            "field":     fname,
            "work_type": wt_name,
            "task_year": str(task_year),
            "op_year":   str(op_year),
            "op_planned_start": op_planned_start.strftime("%d.%m.%Y"),
            "task_start": task_start_dt.strftime("%d.%m.%Y %H:%M"),
            "task_end":   end_dt.strftime("%d.%m.%Y %H:%M") if end_dt else "—",
            "status_key":   status_key,
            "status_label": status_label,
            "task_id":   task_id,
            "op_id":     op_id,
            "task_link": task_link,
            "ao_link":   ao_link,
        })

    total = time.monotonic() - t_total
    _log(
        "    Тайминги, c: "
        + ", ".join(f"{k}={v:.0f}" for k, v in timings.items())
        + f" | параллельный блок (tasks||mapping)="
        + f"{max(timings.get('tasks', 0), timings.get('mapping_items', 0)):.0f}"
        + f" | итого collect={total:.0f}"
    )
    _log(f"    Нарушений (год задачи ≠ год начала агрооперации): {len(rows)}")
    return rows

# =============================================================================
# СТИЛИ
# =============================================================================
_C_HDR   = "2E4DA0"
_C_TITLE = "7B2C2C"
_C_TOTAL = "BDD7EE"
_C_ODD   = "FFFFFF"
_C_EVEN  = "FFF2CC"
_C_FIRM  = "D9E1F2"

HEADERS = [
    "Агрофирма",
    "Поле",
    "Вид работ",
    "Год задачи",
    "Год агроопер.",
    "Нач. агроопер.",
    "Начало задачи",
    "Конец задачи",
    "Статус",
    "ID задачи",
    "ID агроопер.",
]
COL_W = [22, 28, 28, 11, 13, 14, 16, 16, 14, 12, 12]

def _side() -> Border:
    t = Side(style="thin", color="BBBBBB")
    return Border(left=t, right=t, top=t, bottom=t)

def _cell(ws, row: int, col: int, value=None,
          bold=False, bg: str | None = None, color="000000",
          halign="left", wrap=False, size=9, hyperlink: str | None = None):
    c = ws.cell(row=row, column=col, value=value)
    c.font = Font(
        name="Calibri", size=size, bold=bold,
        color="1A6ABF" if hyperlink else color,
        underline="single" if hyperlink else None,
    )
    c.alignment = Alignment(horizontal=halign, vertical="center", wrap_text=wrap)
    c.border = _side()
    if bg:
        c.fill = PatternFill("solid", fgColor=bg)
    if hyperlink:
        c.hyperlink = hyperlink

# =============================================================================
# ЗАПИСЬ EXCEL
# =============================================================================
def write_sheet(wb, rows: list[dict], generated_at: datetime) -> None:
    ws = wb.create_sheet(title="Несовпадение сезона")
    for ci, w in enumerate(COL_W, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    r = 1
    ts_str = generated_at.strftime("%d.%m.%Y %H:%M")
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
    c = ws.cell(row=r, column=1,
                value=f"Несоответствие сезона задачи и агрооперации  |  обновлено {ts_str}")
    c.font      = Font(name="Calibri", size=12, bold=True, color="FFFFFF")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill      = PatternFill("solid", fgColor=_C_TITLE)
    c.border    = _side()
    ws.row_dimensions[r].height = 26
    r += 1

    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, r, ci, h, bold=True, bg=_C_HDR, color="FFFFFF",
              halign="center", wrap=True, size=9)
    ws.row_dimensions[r].height = 36
    ws.freeze_panes = f"A{r+1}"
    r += 1

    if not rows:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
        c = ws.cell(row=r, column=1, value="Нарушений не обнаружено")
        c.font      = Font(name="Calibri", size=10, bold=True, color="375623")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.fill      = PatternFill("solid", fgColor="E2EFDA")
        c.border    = _side()
        ws.row_dimensions[r].height = 18
        return

    rows_sorted = sorted(
        rows,
        key=lambda row: (
            FIRM_ORDER.index(row["firm"]) if row["firm"] in FIRM_ORDER else 999,
            row["field"],
            row["task_start"],
        ),
    )

    current_firm = None
    firm_row_cnt = 0
    total_cnt    = 0

    def write_firm_total(end_r, firm, cnt):
        _cell(ws, end_r, 1, f"Итого {firm}: {cnt} нар.", bold=True, bg=_C_FIRM,
              halign="right", size=9)
        for ci in range(2, len(HEADERS) + 1):
            _cell(ws, end_r, ci, "", bg=_C_FIRM)
        ws.row_dimensions[end_r].height = 14

    for i, row in enumerate(rows_sorted):
        firm = row["firm"]
        if firm != current_firm:
            if current_firm is not None:
                write_firm_total(r, current_firm, firm_row_cnt)
                r += 1
            current_firm = firm
            firm_row_cnt = 0

        bg = _C_ODD if i % 2 == 0 else _C_EVEN
        _cell(ws, r, 1, row["firm"],      bg=bg)
        _cell(ws, r, 2, row["field"],     bg=bg)
        _cell(ws, r, 3, row["work_type"], bg=bg)
        # Год задачи и год агрооперации — красим несовпадение
        _cell(ws, r, 4, row["task_year"],  bg="FFCCCC", halign="center", bold=True)
        _cell(ws, r, 5, row["op_year"],    bg="FFCCCC", halign="center", bold=True)
        _cell(ws, r, 6, row["op_planned_start"], bg=bg, halign="center")
        _cell(ws, r, 7, row["task_start"], bg=bg, halign="center")
        _cell(ws, r, 8, row["task_end"],   bg=bg, halign="center")

        status_bg = STATUS_COLORS.get(row.get("status_key"), bg)
        _cell(ws, r, 9, row.get("status_label", "—"), bg=status_bg, halign="center")

        task_link = row.get("task_link", "")
        ao_link   = row.get("ao_link", "")
        if task_link:
            _cell(ws, r, 10, str(row["task_id"]), bg=bg, halign="center",
                  hyperlink=task_link)
        else:
            _cell(ws, r, 10, str(row["task_id"]), bg=bg, halign="center")
        if ao_link:
            _cell(ws, r, 11, str(row["op_id"]), bg=bg, halign="center",
                  hyperlink=ao_link)
        else:
            _cell(ws, r, 11, str(row["op_id"]), bg=bg, halign="center")

        ws.row_dimensions[r].height = 16
        firm_row_cnt += 1
        total_cnt    += 1
        r += 1

    if current_firm is not None:
        write_firm_total(r, current_firm, firm_row_cnt)
        r += 1

    _cell(ws, r, 1, f"ИТОГО: {total_cnt} нарушений", bold=True, bg=_C_TOTAL,
          halign="right", size=10)
    for ci in range(2, len(HEADERS) + 1):
        _cell(ws, r, ci, "", bg=_C_TOTAL)
    ws.row_dimensions[r].height = 16
    print(f"  Лист «Несовпадение сезона»: {total_cnt} строк")

# =============================================================================
# ОДИН ЦИКЛ
# =============================================================================
async def run_once_async() -> None:
    now = datetime.now(tz=TZ_MSK)
    print(f"\n=== Несоответствие сезона задачи/агроопер.  |  "
          f"{now.strftime('%d.%m.%Y %H:%M:%S')} ===")
    print(f"  Проверяем сезоны: {SCAN_SEASONS}\n")

    field_to_firm, field_slug, field_name, work_type_map = (
        await load_reference_data_async()
    )
    rows = await collect_data_async(
        field_to_firm, field_slug, field_name, work_type_map,
    )
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]
    write_sheet(wb, rows, now)
    wb.save(OUTPUT_FILE)
    print(f"\n✓ Сохранено: {OUTPUT_FILE}")

# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    _ECHO = True
    while True:
        try:
            asyncio.run(run_once_async())
        except Exception as exc:
            import traceback
            print(f"\n[ОШИБКА] {exc}", file=sys.stderr)
            traceback.print_exc()
        next_run = datetime.now(tz=TZ_MSK) + timedelta(seconds=UPDATE_INTERVAL)
        print(f"\nСледующее обновление в {next_run.strftime('%H:%M:%S')}. Ожидание...")
        time.sleep(UPDATE_INTERVAL)
