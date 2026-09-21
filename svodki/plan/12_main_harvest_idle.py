"""
Cropwise Operations — отчёт "Простои транспорта и комбайнов при уборке"
========================================================================
Дата: REPORT_DATE (суточный диапазон: предыдущий день 06:00 → текущий день 06:00)

Структура файла:
  Лист 1 «Элеваторы» — СТАТИСТИКА ПРОСТОЯ ГРУЗОВОГО ТРАНСПОРТА НА ЭЛЕВАТОРАХ
    Все агрофирмы в одной таблице.
    Машины: грузовой транспорт — machine_type == "transport" AND
      machine_subtype == "lorrie" AND machine_group_id == 2,
      либо наёмная техника: machine_group_id == 7 (HIRED_TRANSPORT_GROUP_IDS).
    Одна строка = один простой (после склейки), не сумма за сутки.
    Фильтр: только простои длиной более 40 минут (IDLE_THRESHOLD_SEC)
      ПОСЛЕ склейки соседних визитов с разрывом < 5 мин (MERGE_GAP_SECONDS).
    Сортировка: по агрофирме, затем по убыванию длительности простоя.

  Листы 2..N — по одному на каждую агрофирму
    Заголовок: «СТАТИСТИКА ПРОСТОЯ ГРУЗОВОГО ТРАНСПОРТА И КОМБАЙНОВ ПРИ УБОРКЕ УРОЖАЯ»
    Таблица A: ПРОСТОИ ГРУЗОВОГО ТРАНСПОРТА НА ВЕСОВЫХ БОЛЕЕ 40 МИН
      Каждый длинный простой — отдельная строка (машина, объект, интервал, длительность).
    Таблица B: ПРОСТОИ КОМБАЙНОВ … БОЛЕЕ 40 МИН
      Каждый длинный простой — отдельная строка (комбайн, интервал, длительность).
      Группировка интервалов: (машина, задание, поле) — не смешиваем разные простои.
      Источник: mtfmi.stops_timetable (как driver_stops).

Элеваторы/весовые (Таблица «Элеваторы» и Таблица A):
  Источник объектов: AdditionalObjects, отфильтрованные по эталону
  data/Объекты.xlsx (фильтр «Тип объекта»: Весовая, Зерносклад,
  Зерносклад ДРА, Овощехранилища) и только с полигоном (координаты/площадь).
  Агрофирма объекта: field_group_id → group_folder → корневая папка.
  Простой считаем ТОЛЬКО если Cropwise отдал MachineObjectIntersection
  с object_id из этого списка — машина была внутри геометрии объекта.
  Визиты вне координат/площади этих объектов не учитываются.
  Дальше: склейка разрывов < MERGE_GAP_SECONDS и порог > 40 мин;
  каждая строка = один такой простой.
  Машины: см. _is_transport_machine() — transport+lorrie+group2, либо group7 (наёмные).

Комбайны (Таблица B): источник — machine_task_field_mapping_items.stops_timetable
  (отдельные интервалы простоя на поле, как в driver_stops).
  - Агрофирма: по полю из mtfmi (field → field_group → group_folder)
  - Склейка разрывов < 5 мин, затем фильтр длительности > 40 мин
  - Каждая строка таблицы = один такой простой (не сумма за сутки)
  - Загрузка: bulk machine_tasks по work_type_id + prefetch JSON

Комбайны определяются только по machine_subtype == "harvester"
(machine_group_id не используется).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties
import requests

from plans import (
    API_TOKEN,
    BASE_URL,
    TZ_MSK,
    FIRM_TO_REGION,
    FIRM_ORDER,
    FIRM_TO_FOLDER,
    FOLDER_TO_FIRM,
    FIELD_NAME_FORCE_RULES,
    WORK_GROUPS,
)

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

API_TOKEN = (
    (os.environ.get("CROPWISE_API_TOKEN") or "").strip()
    or (os.environ.get("API_TOKEN") or "").strip()
    or API_TOKEN
)
REPORT_DATE = date(2026, 7, 27)
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

# Временной диапазон суток
DAY_START_HOUR: int = 6

SEMAPHORE_LIMIT = 20

AGROFIRMS: dict[str, int] = {firm: FIRM_TO_REGION[firm] for firm in FIRM_ORDER}
REGION_TO_FIRM: dict[int, str] = {v: k for k, v in AGROFIRMS.items()}

HARVEST_WT_IDS: set[int] = {
    wt_id
    for group_name, wt_ids in WORK_GROUPS
    if group_name == "5.0 Уборка"
    for wt_id in wt_ids
}

# Запас перед day_start при выборке MachineObjectIntersections (часы):
# пересечения, начавшиеся раньше day_start, но продолжающиеся в отчётные
# сутки (например, машина простояла на элеваторе всю ночь), не должны
# теряться из-за фильтра по hour_start.
OBJECT_INTERSECTION_LOOKBACK_HOURS: int = 6

# Порог длины одного простоя для таблиц Элеваторы / A / B (секунды).
# Учитываются только интервалы строго длиннее этого порога — ПОСЛЕ склейки
# близких фрагментов (см. MERGE_GAP_SECONDS). Как в driver_stops: телематика
# часто дробит один реальный простой на несколько коротких интервалов.
IDLE_THRESHOLD_SEC: int = 40 * 60  # 40 минут

# Склейка соседних интервалов одного простоя (как MERGE_GAP в driver_stops).
# Если разрыв между концом одного и началом следующего < 5 мин — это один простой.
MERGE_GAP_SECONDS: int = 5 * 60

# Типы доп. объектов из фильтра «Объекты.xlsx» (колонка «Тип объекта»).
# Простой на весовых/складах считаем ТОЛЬКО для этих типов и только если
# Cropwise зафиксировал пересечение с геометрией объекта (координаты/площадь).
ALLOWED_OBJECT_TYPES: frozenset[str] = frozenset({
    "Весовая",
    "Зерносклад",
    "Зерносклад ДРА",
    "Овощехранилища",
})

# Эталонный список объектов (имена) из data/Объекты.xlsx — сужает API-выборку
# до тех же строк, что видны при активном фильтре в Excel.
_OBJECTS_XLSX_CANDIDATES = (
    Path(__file__).resolve().parent / "data" / "Объекты.xlsx",
    Path(__file__).resolve().parent / "Объекты.xlsx",
)

machines_map: dict[int, dict] = {}
_PREFETCH_ENDPOINTS: dict | None = None
_PREFETCH_LOAD_DONE = False
_ALLOWED_OBJECT_NAMES_NORM: set[str] | None = None
_ALLOWED_OBJECT_NAMES_LOAD_DONE = False


def _label_report_date(day_start: datetime) -> date:
    return day_start.date()


def _norm(text: object) -> str:
    return str(text or "").casefold().replace(" ", "").replace("\u00a0", "")


def _norm_object_type(text: object) -> str:
    return str(text or "").strip().replace("\u00a0", " ")


def _objects_xlsx_path() -> Path | None:
    raw = (os.environ.get("SVODKI_OBJECTS_XLSX") or "").strip()
    if raw:
        p = Path(raw)
        if p.is_file():
            return p
        print(f"  ⚠ SVODKI_OBJECTS_XLSX не найден: {p}", flush=True)
    for p in _OBJECTS_XLSX_CANDIDATES:
        if p.is_file():
            return p
    return None


def _load_allowed_object_names_from_xlsx() -> set[str] | None:
    """
    Имена объектов из Объекты.xlsx с типами из ALLOWED_OBJECT_TYPES
    (тот же фильтр, что выставлен в эталонном файле).
    None — файл не найден, фильтруем только по типу из API.
    """
    global _ALLOWED_OBJECT_NAMES_NORM, _ALLOWED_OBJECT_NAMES_LOAD_DONE
    if _ALLOWED_OBJECT_NAMES_LOAD_DONE:
        return _ALLOWED_OBJECT_NAMES_NORM
    _ALLOWED_OBJECT_NAMES_LOAD_DONE = True

    path = _objects_xlsx_path()
    if path is None:
        print(
            "  Объекты.xlsx не найден — allowlist имён отключён, "
            f"фильтр только по типам {sorted(ALLOWED_OBJECT_TYPES)}",
            flush=True,
        )
        _ALLOWED_OBJECT_NAMES_NORM = None
        return None

    try:
        from openpyxl import load_workbook
    except ImportError:
        print("  openpyxl недоступен для чтения Объекты.xlsx", flush=True)
        _ALLOWED_OBJECT_NAMES_NORM = None
        return None

    try:
        wb = load_workbook(path, data_only=True, read_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception as exc:
        print(f"  Не удалось прочитать {path.name}: {exc}", flush=True)
        _ALLOWED_OBJECT_NAMES_NORM = None
        return None

    # Ищем строку заголовка с «Тип объекта» / «Объект»
    header_idx = None
    type_col = name_col = None
    for i, row in enumerate(rows[:20]):
        cells = [str(c or "").strip() for c in (row or ())]
        if "Тип объекта" in cells and "Объект" in cells:
            header_idx = i
            type_col = cells.index("Тип объекта")
            name_col = cells.index("Объект")
            break
    if header_idx is None or type_col is None or name_col is None:
        print(f"  В {path.name} не найдены колонки «Тип объекта»/«Объект»", flush=True)
        _ALLOWED_OBJECT_NAMES_NORM = None
        return None

    names: set[str] = set()
    for row in rows[header_idx + 1:]:
        if not row:
            continue
        otype = _norm_object_type(row[type_col] if type_col < len(row) else "")
        if otype not in ALLOWED_OBJECT_TYPES:
            continue
        name = str((row[name_col] if name_col < len(row) else "") or "").strip()
        if not name:
            continue
        names.add(_norm(name))

    _ALLOWED_OBJECT_NAMES_NORM = names
    print(
        f"  Allowlist из {path.name}: типов={sorted(ALLOWED_OBJECT_TYPES)}, "
        f"объектов={len(names)}",
        flush=True,
    )
    return names


def _object_has_polygon(obj: dict) -> bool:
    """Есть ли у объекта полигон (координаты + площадь) для учёта простоя."""
    geo = obj.get("geo_json")
    if not geo:
        return False
    geom_type = (obj.get("geometry_type") or "").strip().lower()
    return "polygon" in str(geo).casefold() or geom_type in {"polygon", "multipolygon"}


def _prefetch_endpoints() -> dict | None:
    global _PREFETCH_ENDPOINTS, _PREFETCH_LOAD_DONE
    if _PREFETCH_LOAD_DONE:
        return _PREFETCH_ENDPOINTS
    _PREFETCH_LOAD_DONE = True
    raw = (os.environ.get("SVODKI_PREFETCH_DATA_PATH") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        endpoints = data.get("endpoints")
        if isinstance(endpoints, dict):
            _PREFETCH_ENDPOINTS = endpoints
            profile = str(data.get("prefetch_profile") or "").strip()
            print(
                f"  Prefetch JSON: {path.name}"
                + (f" ({profile})" if profile else ""),
                flush=True,
            )
    except Exception as exc:
        print(f"  Prefetch JSON не прочитан ({exc}), живой API", flush=True)
    return _PREFETCH_ENDPOINTS


def _mtfmi_map_from_prefetch(task_ids: set[int]) -> dict[int, list[dict]]:
    endpoints = _prefetch_endpoints()
    if not endpoints:
        return {}
    rows = endpoints.get("machine_task_field_mapping_items") or []
    out: dict[int, list[dict]] = {}
    for row in rows:
        try:
            tid = int(row.get("machine_task_id"))
        except Exception:
            continue
        if tid not in task_ids:
            continue
        out.setdefault(tid, []).append(row)
    return out


def _folder_parent_map(gfs_all: list[dict]) -> dict[int, int | None]:
    return {
        int(g["id"]): (int(g["parent_id"]) if g.get("parent_id") is not None else None)
        for g in gfs_all
        if g.get("id") is not None
    }


def _resolve_to_holding_root(
    folder_id: int | None,
    parent_map: dict[int, int | None],
    root_ids: set[int],
) -> int | None:
    if folder_id is None:
        return None
    cur = int(folder_id)
    seen: set[int] = set()
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


def load_field_to_firm() -> dict[int, str]:
    ep = _prefetch_endpoints()
    if ep:
        print("  Справочники полей из prefetch JSON...", flush=True)
        fields_all = ep.get("fields") or get_all_from_id("fields")
        fgs_all = ep.get("field_groups") or get_all_from_id("field_groups")
        gfs_all = ep.get("group_folders") or get_all_from_id("group_folders")
    else:
        print("  Загружаем fields / field_groups / group_folders...")
        fields_all = get_all_from_id("fields")
        fgs_all = get_all_from_id("field_groups")
        gfs_all = get_all_from_id("group_folders")

    fgs_map = {fg["id"]: fg for fg in fgs_all}
    parent_map = _folder_parent_map(gfs_all)
    root_ids = set(FIRM_TO_FOLDER.values())
    field_to_firm: dict[int, str] = {}

    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        fgid = f.get("field_group_id")
        raw_folder = f.get("group_folder_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(fgid, {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None
        root = _resolve_to_holding_root(rint, parent_map, root_ids)
        if root is not None:
            firm = FOLDER_TO_FIRM.get(root)
            if firm:
                field_to_firm[int(fid)] = firm

    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        blob = _norm(f"{f.get('name') or ''} {f.get('title') or ''}")
        for needle, firm_key in FIELD_NAME_FORCE_RULES:
            if needle in blob:
                field_to_firm[int(fid)] = firm_key
                break

    print(f"  Полей с привязкой к агрофирме: {len(field_to_firm)}", flush=True)
    return field_to_firm


def _machine_label(mid: int) -> str:
    m = machines_map.get(mid, {})
    return (
        m.get("name") or m.get("title") or m.get("model") or f"machine_{mid}"
    )


def _is_combine_machine(mid: int) -> bool:
    """Комбайны для таблицы простоя на поле (выгрузка бункеров).
    Только machine_subtype == 'harvester' — без учёта machine_group_id."""
    m = machines_map.get(mid, {})
    return (m.get("machine_subtype") or "") == "harvester"


# ID групп наёмной техники (machine_group_id), которые тоже нужно учитывать
# в таблице простоя на элеваторах наравне с основным грузовым транспортом.
# HIRED_GROUP_ID = 7 подтверждён пользователем (machine_group_id == 7 → НАЁМНЫЕ).
HIRED_TRANSPORT_GROUP_IDS: set[int] = {7}


def _is_transport_machine(mid: int) -> bool:
    """Машины для таблицы простоя на элеваторах:
    - штатный грузовой транспорт: machine_type == 'transport',
      machine_subtype == 'lorrie', machine_group_id == 2;
    - либо наёмная техника: machine_group_id в HIRED_TRANSPORT_GROUP_IDS."""
    m = machines_map.get(mid, {})
    gid = m.get("machine_group_id")
    if gid in HIRED_TRANSPORT_GROUP_IDS:
        return True
    mtype = (m.get("machine_type") or "").lower()
    subtype = m.get("machine_subtype") or ""
    return mtype == "transport" and subtype == "lorrie" and gid == 2


def load_additional_object_to_firm() -> tuple[dict[int, str], dict[int, dict]]:
    """
    Возвращает:
      object_id -> firm_name  (агрофирма объекта, через field_group_id → group_folder)
      object_id -> объект AdditionalObject (для имени/доп. атрибутов)

    Только объекты из фильтра Объекты.xlsx (Весовая / Зерносклад / Зерносклад ДРА /
    Овощехранилища) с полигоном. Простой считаем лишь когда Cropwise зафиксировал
    MachineObjectIntersection с этими object_id — то есть машина была внутри
    координат/площади объекта. Визиты вне этих зон в отчёт не попадают.
    """
    ep = _prefetch_endpoints()

    if ep is not None and "additional_objects" in ep:
        print("  AdditionalObjects из prefetch JSON...", flush=True)
        objs_all = ep.get("additional_objects") or []
    else:
        print("  Загружаем additional_objects...", flush=True)
        objs_all = get_all_from_id("additional_objects")

    if ep is not None:
        fgs_all = ep.get("field_groups")
        if fgs_all is None:
            fgs_all = get_all_from_id("field_groups")
        gfs_all = ep.get("group_folders")
        if gfs_all is None:
            gfs_all = get_all_from_id("group_folders")
    else:
        print("  Загружаем field_groups / group_folders...", flush=True)
        fgs_all = get_all_from_id("field_groups")
        gfs_all = get_all_from_id("group_folders")

    fgs_map = {fg["id"]: fg for fg in fgs_all}
    parent_map = _folder_parent_map(gfs_all)
    root_ids = set(FIRM_TO_FOLDER.values())

    object_to_firm: dict[int, str] = {}
    objects_map: dict[int, dict] = {}
    skipped_type = 0
    skipped_allowlist = 0
    skipped_geometry = 0

    allowed_names = _load_allowed_object_names_from_xlsx()

    for o in objs_all:
        oid = o.get("id")
        if oid is None:
            continue
        oid_i = int(oid)

        otype = _norm_object_type(o.get("object_type"))
        if otype not in ALLOWED_OBJECT_TYPES:
            skipped_type += 1
            continue

        if allowed_names is not None:
            if _norm(o.get("name") or o.get("title")) not in allowed_names:
                skipped_allowlist += 1
                continue

        if not _object_has_polygon(o):
            skipped_geometry += 1
            continue

        objects_map[oid_i] = o

        fgid = o.get("field_group_id")
        raw_folder = fgs_map.get(fgid, {}).get("group_folder_id") if fgid is not None else None
        rint = int(raw_folder) if raw_folder is not None else None
        root = _resolve_to_holding_root(rint, parent_map, root_ids)
        if root is not None:
            firm = FOLDER_TO_FIRM.get(root)
            if firm:
                object_to_firm[oid_i] = firm

    print(
        f"  AdditionalObjects: всего={len(objs_all)}, "
        f"для простоя весовых={len(objects_map)}, "
        f"с агрофирмой={len(object_to_firm)} "
        f"(отсеяно: тип={skipped_type}, не из Excel={skipped_allowlist}, "
        f"без полигона={skipped_geometry})",
        flush=True,
    )
    return object_to_firm, objects_map


def _clip_interval(
    start: datetime | None,
    end: datetime | None,
    lo: datetime,
    hi: datetime,
) -> tuple[datetime, datetime] | None:
    """Интервал [start, end], обрезанный границами [lo, hi]. None если пусто."""
    if start is None:
        return None
    if end is None:
        end = hi  # ещё не завершилось — считаем до конца отчётных суток
    s = max(start, lo)
    e = min(end, hi)
    if e <= s:
        return None
    return s, e


def _clip_interval_secs(
    start: datetime | None,
    end: datetime | None,
    lo: datetime,
    hi: datetime,
) -> float:
    """Длительность пересечения [start, end], обрезанная границами [lo, hi]."""
    clipped = _clip_interval(start, end, lo, hi)
    if clipped is None:
        return 0.0
    return (clipped[1] - clipped[0]).total_seconds()


def _merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Склеивает интервалы с разрывом < MERGE_GAP_SECONDS (логика driver_stops)."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: iv[0])
    merged: list[tuple[datetime, datetime]] = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        gap = (start - prev_end).total_seconds()
        if gap < MERGE_GAP_SECONDS:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _long_idle_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime, float]]:
    """Склеенные простои длительностью > IDLE_THRESHOLD_SEC: (start, end, secs)."""
    out: list[tuple[datetime, datetime, float]] = []
    for start, end in _merge_intervals(intervals):
        secs = (end - start).total_seconds()
        if secs > IDLE_THRESHOLD_SEC:
            out.append((start, end, secs))
    return out


# Смещение только для колонки «Интервал» в Excel/PDF.
# Длительность простоя и склейка считаются по исходным меткам API без сдвига.
INTERVAL_DISPLAY_UTC_OFFSET = timedelta(hours=3)


def _fmt_idle_interval(start: datetime, end: datetime) -> str:
    """Интервал для таблицы: к настенному времени API добавляем +3 часа."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ_MSK)
    else:
        start = start.astimezone(TZ_MSK)
    if end.tzinfo is None:
        end = end.replace(tzinfo=TZ_MSK)
    else:
        end = end.astimezone(TZ_MSK)
    start = start + INTERVAL_DISPLAY_UTC_OFFSET
    end = end + INTERVAL_DISPLAY_UTC_OFFSET
    if start.date() == end.date():
        return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
    return (
        f"{start.strftime('%d.%m %H:%M')}–{end.strftime('%d.%m %H:%M')}"
    )


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


def seconds_to_hms(seconds: float) -> str:
    total = int(seconds)
    h     = total // 3600
    m     = (total % 3600) // 60
    s     = total % 60
    return f"{h}:{m:02d}:{s:02d}"


def get_all_from_id(endpoint: str, extra_params: dict | None = None) -> list[dict]:
    params = dict(extra_params or {})
    results = []
    from_id = 0
    max_attempts = max(1, int(os.environ.get("SVODKI_PREFETCH_HTTP_RETRIES", "5") or 5))
    while True:
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                r = session.get(
                    f"{BASE_URL}/{endpoint}",
                    params={**params, "from_id": from_id, "limit": 1000},
                    timeout=120,
                )
                r.raise_for_status()
                body = r.json()
                last_exc = None
                break
            except (requests.HTTPError, requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                retryable = True
                if isinstance(exc, requests.HTTPError) and exc.response is not None:
                    retryable = exc.response.status_code in {429, 502, 503, 504}
                if not retryable or attempt >= max_attempts - 1:
                    raise
                time.sleep(min(30.0, 1.5 * (2 ** attempt)))
        if last_exc is not None:
            raise last_exc
        chunk = body.get("data", [])
        if not chunk:
            break
        results.extend(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = last_id + 1
    return results


# =============================================================================
# АСИНХРОННЫЕ ЗАПРОСЫ
# =============================================================================

async def fetch_all_pages(
    client:    httpx.AsyncClient,
    url:       str,
    params:    dict,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    items: list[dict] = []
    from_id = 0
    max_attempts = max(1, int(os.environ.get("SVODKI_PREFETCH_HTTP_RETRIES", "5") or 5))
    async with semaphore:
        while True:
            last_exc: Exception | None = None
            body: dict | None = None
            for attempt in range(max_attempts):
                try:
                    r = await client.get(
                        url,
                        params={**params, "from_id": from_id, "limit": 1000},
                        timeout=120,
                    )
                    r.raise_for_status()
                    parsed = r.json()
                    if not isinstance(parsed, dict):
                        raise ValueError(f"Unexpected JSON from {url}")
                    body = parsed
                    last_exc = None
                    break
                except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
                    last_exc = exc
                    retryable = True
                    if isinstance(exc, httpx.HTTPStatusError):
                        retryable = exc.response.status_code in {429, 502, 503, 504}
                    if not retryable or attempt >= max_attempts - 1:
                        raise
                    await asyncio.sleep(min(30.0, 1.5 * (2 ** attempt)))
            if last_exc is not None or body is None:
                raise last_exc or RuntimeError(f"Empty response from {url}")
            chunk = body.get("data", [])
            if not chunk:
                break
            items.extend(chunk)
            meta = body.get("meta", {}).get("response", {})
            last_id = meta.get("last_record_id")
            if not last_id or meta.get("obtained_records", 0) < 100:
                break
            from_id = last_id + 1
    return items


async def fetch_mtfmi_for_tasks(
    client:    httpx.AsyncClient,
    task_ids:  list[int],
    semaphore: asyncio.Semaphore,
) -> dict[int, list[dict]]:
    task_set = set(task_ids)
    out = _mtfmi_map_from_prefetch(task_set)
    missing = [tid for tid in task_ids if tid not in out]
    if not missing:
        print(f"  mtfmi из prefetch JSON: {len(out)} задач (без HTTP)", flush=True)
        return out
    url = f"{BASE_URL}/machine_task_field_mapping_items"
    results = await asyncio.gather(*[
        fetch_all_pages(client, url, {"machine_task_id": tid}, semaphore)
        for tid in missing
    ])
    for tid, items in zip(missing, results):
        out[tid] = items
    return out


async def fetch_harvest_tasks_for_day(
    client:        httpx.AsyncClient,
    semaphore:     asyncio.Semaphore,
    day_start_str: str,
    cutoff_str:    str,
) -> list[dict]:
    ep = _prefetch_endpoints()
    if ep and ep.get("machine_tasks"):
        day_start = datetime.fromisoformat(day_start_str).replace(tzinfo=TZ_MSK)
        cutoff_ext = datetime.fromisoformat(cutoff_str).replace(tzinfo=TZ_MSK)
        out: list[dict] = []
        seen: set[int] = set()
        for t in ep.get("machine_tasks") or []:
            if t.get("work_type_id") not in HARVEST_WT_IDS:
                continue
            end_dt = parse_dt(t.get("end_time"))
            # cutoff_str уже расширен на 2 часа вызывающей стороной
            if not end_dt or end_dt < day_start or end_dt >= cutoff_ext:
                continue
            tid = t.get("id")
            if tid is None or int(tid) in seen:
                continue
            seen.add(int(tid))
            out.append(t)
        print(f"  machine_tasks из prefetch (сутки): {len(out)}", flush=True)
        return out

    url = f"{BASE_URL}/machine_tasks"
    from machine_tasks_batch_fetch import fetch_machine_tasks_by_wt_async

    raw = await fetch_machine_tasks_by_wt_async(
        client, semaphore, HARVEST_WT_IDS,
        {"end_time_gt_eq": day_start_str, "end_time_lt": cutoff_str},
        fetch_all_pages, url=url, log_prefix="[r12] ",
    )
    seen: set[int] = set()
    out = []
    for t in raw:
        if t["id"] not in seen:
            seen.add(t["id"])
            out.append(t)
    return out


async def fetch_object_intersections_for_day(
    client:    httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    day_start: datetime,
    cutoff:    datetime,
) -> list[dict]:
    """
    MachineObjectIntersections для AdditionalObject за отчётные сутки.
    Фильтр по hour_start с запасом OBJECT_INTERSECTION_LOOKBACK_HOURS назад,
    чтобы не терять пересечения, начавшиеся раньше day_start, но
    продолжающиеся в отчётный период. Точная обрезка интервалов — на
    стороне вызывающего кода (_clip_interval_secs).
    """
    lo = day_start - timedelta(hours=OBJECT_INTERSECTION_LOOKBACK_HOURS)

    ep = _prefetch_endpoints()
    if ep is not None and "machine_object_intersections" in ep:
        rows = ep.get("machine_object_intersections") or []
        print(f"  machine_object_intersections из prefetch: {len(rows)}", flush=True)
        return rows

    url = f"{BASE_URL}/machine_object_intersections"
    params = {
        "object_type":       "AdditionalObject",
        "hour_start_gt_eq":  lo.strftime("%Y-%m-%dT%H:%M:%S"),
        "hour_start_lt":     cutoff.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    rows = await fetch_all_pages(client, url, params, semaphore)
    print(f"  machine_object_intersections загружено: {len(rows)}", flush=True)
    return rows


def parse_dt(s) -> datetime | None:
    """Парсит метку Cropwise в TZ_MSK (UTC+3).

    - stops_timetable / work_timetable: naive («2026-07-26 11:52:49») — уже
      локальное время холдинга (как start_time заданий с суффиксом +03:00).
    - machine_object_intersections: сразу с offset «…+03:00».
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        ts = float(s)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except (OSError, ValueError, OverflowError):
            return None
    text = str(s).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # fromisoformat до 3.10 не любит пробел между датой и временем.
    if " " in text and "T" not in text[:20]:
        text = text.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_MSK)
    else:
        dt = dt.astimezone(TZ_MSK)
    return dt


def load_machine_to_firms(target_date: str) -> dict[int, set[str]]:
    """Резерв: machine_id → агрофирмы по machine_region_mapping_items."""
    ep = _prefetch_endpoints()
    if ep and ep.get("machine_region_mapping_items"):
        rows = ep.get("machine_region_mapping_items") or []
    else:
        rows = get_all_from_id("machine_region_mapping_items")

    out: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        mid = row.get("machine_id")
        region_id = row.get("machine_region_id")
        if mid is None or region_id is None:
            continue
        firm = REGION_TO_FIRM.get(int(region_id))
        if not firm:
            continue
        ds = row.get("date_start")
        de = row.get("date_end")
        if ds and str(ds)[:10] > target_date:
            continue
        if de and str(de)[:10] < target_date:
            continue
        out[int(mid)].add(firm)
    print(f"  Машин в machine_region (резерв): {len(out)}", flush=True)
    return dict(out)


def _task_firm_shares(
    task: dict,
    mtfmi_map: dict[int, list[dict]],
    field_to_firm: dict[int, str],
    machine_to_firms: dict[int, set[str]],
) -> dict[str, float]:
    items = mtfmi_map.get(task.get("id"), [])
    shares: dict[str, float] = defaultdict(float)
    for item in items:
        fid = item.get("field_id")
        if fid is None:
            continue
        firm = field_to_firm.get(int(fid))
        if not firm:
            continue
        cov = float(item.get("covered_area") or 0.0)
        if cov <= 0:
            cov = 1.0
        shares[firm] += cov
    if shares:
        return dict(shares)

    mid = task.get("machine_id")
    if mid is not None:
        firms = machine_to_firms.get(int(mid))
        if firms:
            weight = 1.0 / len(firms)
            return {firm: weight for firm in firms}
    return {}


async def collect_idle_data(report_date: date) -> dict[str, dict]:
    global machines_map

    cutoff = datetime(
        report_date.year, report_date.month, report_date.day,
        DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    day_start = cutoff - timedelta(days=1)
    label_date = _label_report_date(day_start)

    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    # Расширяем верхнюю границу запроса на 2 часа: задания с end_time чуть
    # позже cutoff (например, 06:49) тоже могут относиться к этой смене.
    # stops_on_road_duration / idle_duration — суммарные числа за всё задание,
    # поэтому достаточно включить задание в выборку целиком.
    cutoff_ext_str = (cutoff + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")

    print(f"  Дата платформы: {report_date}")
    print(f"  Подпись «Сводка за»: {label_date.strftime('%d.%m.%Y')}")
    print(f"  Диапазон: {day_start} — {cutoff}")

    ep = _prefetch_endpoints()
    if ep and ep.get("machines"):
        machines_map = {m["id"]: m for m in ep.get("machines") or []}
        print(f"  Машин из prefetch: {len(machines_map)}", flush=True)
    else:
        print("  Загружаем machines...")
        machines_map = {m["id"]: m for m in get_all_from_id("machines")}
        print(f"  Всего машин: {len(machines_map)}")

    field_to_firm = load_field_to_firm()
    machine_to_firms = load_machine_to_firms(report_date.strftime("%Y-%m-%d"))
    object_to_firm, objects_map = load_additional_object_to_firm()

    headers = {"X-User-Api-Token": API_TOKEN}
    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)

    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        tasks = await fetch_harvest_tasks_for_day(
            client, semaphore, day_start_str, cutoff_ext_str,
        )
        task_ids = list({t["id"] for t in tasks})
        mtfmi_map = await fetch_mtfmi_for_tasks(client, task_ids, semaphore) if task_ids else {}
        intersections = await fetch_object_intersections_for_day(
            client, semaphore, day_start, cutoff,
        )

    print(f"  Задач уборки за сутки: {len(tasks)}")
    print(f"  Пересечений машина/объект (сырых): {len(intersections)}")

    # --- Комбайны: простои на поле из mtfmi.stops_timetable (как driver_stops) ---
    # Сначала собираем интервалы, склеиваем разрывы < 5 мин, затем оставляем
    # только простои длиннее 40 мин. Каждый такой простой — отдельная строка.
    #
    # Ключ группировки: (фирма, машина, task_id, field_id).
    # Без task_id/field_id простои с разных заданий и полей одной машины за сутки
    # склеивались при разрыве < MERGE_GAP_SECONDS, хотя это разные физические
    # простои. Аналогично таблице «Элеваторы», где ключ включает object_id.
    combine_intervals: dict[
        tuple[str, int, int, int], list[tuple[datetime, datetime]]
    ] = defaultdict(list)

    for t in tasks:
        mid = t.get("machine_id")
        if mid is None:
            continue
        mid_i = int(mid)
        if not _is_combine_machine(mid_i):
            continue
        tid = t.get("id")
        if tid is None:
            continue
        tid_i = int(tid)
        for item in mtfmi_map.get(int(tid), []) or []:
            field_id = item.get("field_id")
            firm = None
            if field_id is not None:
                firm = field_to_firm.get(int(field_id))
            if not firm:
                shares = _task_firm_shares(t, mtfmi_map, field_to_firm, machine_to_firms)
                if len(shares) == 1:
                    firm = next(iter(shares))
                else:
                    continue
            field_key = int(field_id) if field_id is not None else 0
            for interval in item.get("stops_timetable") or []:
                if not isinstance(interval, (list, tuple)) or len(interval) < 2:
                    continue
                clipped = _clip_interval(
                    parse_dt(interval[0]), parse_dt(interval[1]), day_start, cutoff,
                )
                if clipped is None:
                    continue
                combine_intervals[(firm, mid_i, tid_i, field_key)].append(clipped)

    firm_combines: dict[str, list[dict]] = {firm: [] for firm in AGROFIRMS}
    for (firm, mid_i, _tid_i, _field_key), ivs in combine_intervals.items():
        if firm not in firm_combines:
            continue
        for start, end, secs in _long_idle_intervals(ivs):
            firm_combines[firm].append({
                "machine": _machine_label(mid_i),
                "idle_field_secs": secs,
                "stop_start": start,
                "stop_end": end,
                "interval": _fmt_idle_interval(start, end),
            })

    # --- Грузовой транспорт: простой на элеваторах/весовых ---
    # MachineObjectIntersections → склейка близких визитов → фильтр > 40 мин.
    # В отчёт попадает КАЖДЫЙ длинный простой отдельной строкой (не сумма).
    known_object_ids = set(object_to_firm.keys())
    elevator_intervals: dict[tuple[str, int, int], list[tuple[datetime, datetime]]] = (
        defaultdict(list)
    )

    skipped_unknown_object = 0
    skipped_not_transport = 0
    raw_elevator_intervals = 0
    for row in intersections:
        oid = row.get("object_id")
        if oid is None:
            continue
        oid_i = int(oid)
        if oid_i not in known_object_ids:
            skipped_unknown_object += 1
            continue

        obj_type = row.get("object_type")
        if obj_type and str(obj_type) != "AdditionalObject":
            continue

        mid = row.get("machine_id")
        if mid is None:
            continue
        mid_i = int(mid)
        if not _is_transport_machine(mid_i):
            skipped_not_transport += 1
            continue

        firm = object_to_firm.get(oid_i)
        if not firm:
            continue

        clipped = _clip_interval(
            parse_dt(row.get("start_intersection")),
            parse_dt(row.get("end_intersection")),
            day_start,
            cutoff,
        )
        if clipped is None:
            continue
        raw_elevator_intervals += 1
        elevator_intervals[(firm, mid_i, oid_i)].append(clipped)

    firm_elevator: dict[str, list[dict]] = {firm: [] for firm in AGROFIRMS}
    kept_long_elevator = 0
    skipped_short_after_merge = 0
    for (firm, mid_i, oid_i), ivs in elevator_intervals.items():
        if firm not in firm_elevator:
            continue
        obj_name = (objects_map.get(oid_i, {}) or {}).get("name") or f"object_{oid_i}"
        machine = _machine_label(mid_i)
        for start, end in _merge_intervals(ivs):
            secs = (end - start).total_seconds()
            if secs > IDLE_THRESHOLD_SEC:
                firm_elevator[firm].append({
                    "machine": machine,
                    "object": obj_name,
                    "stops_road_secs": secs,
                    "stop_start": start,
                    "stop_end": end,
                    "interval": _fmt_idle_interval(start, end),
                })
                kept_long_elevator += 1
            else:
                skipped_short_after_merge += 1

    print(
        f"  Пересечений: объектов вне списка={skipped_unknown_object}, "
        f"не-транспорт={skipped_not_transport}, "
        f"сырых визитов={raw_elevator_intervals}, "
        f"после склейки >{IDLE_THRESHOLD_SEC // 60} мин={kept_long_elevator}, "
        f"≤{IDLE_THRESHOLD_SEC // 60} мин={skipped_short_after_merge}",
        flush=True,
    )

    result: dict[str, dict] = {}
    for firm_name in AGROFIRMS:
        trucks = list(firm_elevator.get(firm_name, []))
        combines = list(firm_combines.get(firm_name, []))
        trucks.sort(key=lambda x: (-x["stops_road_secs"], x.get("interval") or ""))
        combines.sort(key=lambda x: (-x["idle_field_secs"], x.get("interval") or ""))
        result[firm_name] = {"trucks": trucks, "combines": combines}

    result["_label_date"] = label_date
    return result


# =============================================================================
# СТИЛИ
# =============================================================================

def _make_styles() -> dict:
    thin   = Side(style="thin")
    medium = Side(style="medium")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    border_med = Border(left=medium, right=medium, top=medium, bottom=medium)
    return {
        "border":       border,
        "border_med":   border_med,
        "align_c":      Alignment(horizontal="center", vertical="center",
                                   wrap_text=True),
        "align_l":      Alignment(vertical="center", wrap_text=True),
        # Палитра как в сводках 3–11: шапки 8093B3, данные белые, итог BDD7EE.
        "fill_hdr":     PatternFill("solid", fgColor="8093B3"),
        "fill_data":    PatternFill("solid", fgColor="FFFFFF"),
        "fill_total":   PatternFill("solid", fgColor="BDD7EE"),
        "font_title":   Font(bold=True, size=13, color="FFFFFF"),
        "font_hdr":     Font(bold=True, color="FFFFFF"),
        "font_sec":     Font(bold=True, size=11, color="FFFFFF"),
        "font_total":   Font(bold=True),
        "font_firm":    Font(bold=True, size=11),
    }


def _sc(cell, s, fill=None, font=None, align=None) -> None:
    cell.border = s["border"]
    if fill:  cell.fill      = fill
    if font:  cell.font      = font
    if align: cell.alignment = align


def _estimate_wrapped_lines(value: object, col_width: float | None) -> int:
    text = str(value or "").strip()
    if not text:
        return 1
    width = float(col_width or 10)
    chars_per_line = max(1, int(width * 0.78))
    parts = text.splitlines() or [text]
    lines = 0
    for part in parts:
        p = part.strip()
        if not p:
            lines += 1
            continue
        lines += max(1, math.ceil(len(p) / chars_per_line))
    return max(1, lines)


def _ensure_row_height(ws, row: int, height: float) -> None:
    """Не уменьшать высоту строки — левая и правая таблицы делят одни row."""
    cur = ws.row_dimensions[row].height
    ws.row_dimensions[row].height = max(float(cur or 0), float(height))


def _row_height_for_text(
    value: object,
    col_width: float | None,
    *,
    min_height: float = 22.0,
    line_height: float = 14.0,
    max_height: float = 44.0,
) -> float:
    lines = _estimate_wrapped_lines(value, col_width)
    return min(max_height, max(min_height, lines * line_height + 2))


def _merged_row_height(
    ws,
    row: int,
    col_start: int,
    col_end: int,
    value: object,
    *,
    min_height: float = 18.0,
    max_height: float = 32.0,
) -> None:
    total_width = 0.0
    for ci in range(col_start, col_end + 1):
        w = ws.column_dimensions[get_column_letter(ci)].width
        total_width += float(w or 10)
    h = _row_height_for_text(
        value, total_width, min_height=min_height, max_height=max_height,
    )
    _ensure_row_height(ws, row, h)


def _setup_print_layout(ws, *, end_col: int, end_row: int) -> None:
    """A4 альбомная: одна страница на лист, область печати под штамп PDF."""
    ws.sheet_view.showGridLines = False
    end_letter = get_column_letter(end_col)
    ws.print_area = f"A1:{end_letter}{max(1, end_row)}"
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True, autoPageBreaks=True)
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.page_setup.scale = None
    ws.page_margins.left = 0.35
    ws.page_margins.right = 0.35
    ws.page_margins.top = 0.35
    ws.page_margins.bottom = 0.35
    ws.print_options.horizontalCentered = True


def _merge_title(
    ws,
    row: int,
    col_start: int,
    col_end: int,
    value: str,
    s: dict,
    *,
    fill=None,
    font=None,
) -> None:
    ws.merge_cells(
        start_row=row,
        start_column=col_start,
        end_row=row,
        end_column=col_end,
    )
    c = ws.cell(row=row, column=col_start, value=value)
    _sc(c, s, fill=fill or s["fill_hdr"], font=font or s["font_hdr"], align=s["align_c"])
    _merged_row_height(ws, row, col_start, col_end, value, min_height=28.0, max_height=52.0)


def _write_side_table(
    ws,
    s: dict,
    *,
    start_row: int,
    col_start: int,
    title: str,
    headers: list[str],
    data_rows: list[list],
    empty_text: str,
    total_row: list | None,
) -> int:
    """Таблица; возвращает последнюю занятую строку."""
    col_end = col_start + len(headers) - 1
    row = start_row

    _merge_title(ws, row, col_start, col_end, title, s)
    row += 1

    for ci, col in enumerate(headers):
        c = ws.cell(row=row, column=col_start + ci, value=col)
        _sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_c"])
    hdr_w = sum(
        float(ws.column_dimensions[get_column_letter(col_start + ci)].width or 10)
        for ci in range(len(headers))
    )
    _ensure_row_height(
        ws, row,
        _row_height_for_text(
            "\n".join(headers), hdr_w, min_height=44.0, max_height=64.0, line_height=15.0,
        ),
    )
    row += 1

    if data_rows:
        name_col = col_start + 1
        name_width = float(ws.column_dimensions[get_column_letter(name_col)].width or 10)
        for vals in data_rows:
            for ci, val in enumerate(vals):
                c = ws.cell(row=row, column=col_start + ci, value=val)
                align = s["align_l"] if ci in (1, 2) else s["align_c"]
                _sc(c, s, fill=s["fill_data"], align=align)
            longest_text = max(
                (str(v or "") for v in vals[1:3]), key=len, default="",
            )
            _ensure_row_height(
                ws, row,
                _row_height_for_text(
                    longest_text, name_width, min_height=24.0, max_height=48.0, line_height=14.0,
                ),
            )
            row += 1
        if total_row:
            for ci, val in enumerate(total_row):
                c = ws.cell(row=row, column=col_start + ci, value=val)
                _sc(
                    c, s,
                    fill=s["fill_total"],
                    font=s["font_total"],
                    align=s["align_c"],
                )
            _ensure_row_height(ws, row, 26.0)
            row += 1
    else:
        ws.merge_cells(
            start_row=row,
            start_column=col_start,
            end_row=row,
            end_column=col_end,
        )
        c = ws.cell(row=row, column=col_start, value=empty_text)
        _sc(c, s, fill=s["fill_data"], align=s["align_c"])
        empty_w = sum(
            float(ws.column_dimensions[get_column_letter(col_start + ci)].width or 10)
            for ci in range(len(headers))
        )
        _ensure_row_height(
            ws, row,
            _row_height_for_text(
                empty_text, empty_w, min_height=24.0, max_height=36.0, line_height=14.0,
            ),
        )
        row += 1

    return row - 1


# =============================================================================
# ЛИСТ 1 — ЭЛЕВАТОРЫ (все агрофирмы)
# =============================================================================

ELEV_COLS = [
    "№ п/п", "Привязана к регионам", "Машина", "Объект",
    "Интервал", "Время простоя",
]


def write_elevator_sheet(
    wb:          openpyxl.Workbook,
    data:        dict[str, dict],
    report_date: date,
) -> None:
    ws = wb.create_sheet(title="Элеваторы", index=0)
    s  = _make_styles()
    n  = len(ELEV_COLS)
    _apply_elevator_sheet_layout(ws)
    cur = [1]

    def nr() -> int:
        r = cur[0]; cur[0] += 1; return r

    r = nr()
    for ci, col in enumerate(ELEV_COLS, start=1):
        c = ws.cell(row=r, column=ci, value=col)
        _sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_c"])
    hdr_w = sum(float(ws.column_dimensions[get_column_letter(ci)].width or 10) for ci in range(1, n + 1))
    ws.row_dimensions[r].height = _row_height_for_text(
        "\n".join(ELEV_COLS), hdr_w, min_height=44.0, max_height=64.0, line_height=15.0,
    )

    num = 1
    for firm_name in AGROFIRMS:
        trucks = [
            t for t in data.get(firm_name, {}).get("trucks", [])
            if t["stops_road_secs"] > IDLE_THRESHOLD_SEC
        ]
        trucks.sort(key=lambda x: (-x["stops_road_secs"], x.get("interval") or ""))
        for row in trucks:
            r = nr()
            vals = [
                num,
                firm_name,
                row["machine"],
                row["object"],
                row.get("interval") or "",
                seconds_to_hms(row["stops_road_secs"]),
            ]
            for ci, val in enumerate(vals, start=1):
                c = ws.cell(row=r, column=ci, value=val)
                _sc(c, s, fill=s["fill_data"],
                    align=s["align_l"] if ci in (2, 3, 4) else s["align_c"])
            mname = str(row["machine"] or "")
            ws.row_dimensions[r].height = 28.0 if len(mname) > 34 else 22.0
            num += 1

    end_row = cur[0] - 1
    if num == 1:
        r = nr()
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=n)
        c = ws.cell(row=r, column=1, value="Нет данных за период")
        _sc(c, s, fill=s["fill_data"], align=s["align_c"])
        end_row = cur[0] - 1

    _setup_print_layout(ws, end_col=n, end_row=end_row)


# =============================================================================
# ЛИСТЫ ПО АГРОФИРМАМ — две таблицы рядом
# =============================================================================

FIRM_TRUCK_COLS = [
    "№п/п", "Машина", "Объект", "Интервал", "Время простоя",
]
FIRM_COMBINE_COLS = [
    "№п/п", "Комбайн", "Интервал", "Время простоя",
]

LEFT_COL  = 1
RIGHT_COL = 7
GAP_COL   = 6
END_COL   = 10
_COLS_PER_SIDE_LEFT  = 5
_COLS_PER_SIDE_RIGHT = 4

TRUCK_TITLE = (
    "ПРОСТОИ ГРУЗОВОГО ТРАНСПОРТА\n"
    "НА ВЕСОВЫХ БОЛЕЕ 40 МИН"
)
COMBINE_TITLE = (
    "ПРОСТОИ КОМБАЙНОВ\n"
    "С УЧЕТОМ ВЫГРУЗКИ БУНКЕРОВ НА ПОЛЯХ БОЛЕЕ 40 МИН"
)
EMPTY_IDLE_TEXT = "Нет данных (простои ≤ 40 мин)"


def _apply_firm_sheet_layout(ws) -> None:
    for col, w in (
        ("A", 6), ("B", 22), ("C", 22), ("D", 14), ("E", 12),
        ("F", 2),
        ("G", 6), ("H", 28), ("I", 14), ("J", 12),
    ):
        ws.column_dimensions[col].width = w


def _apply_elevator_sheet_layout(ws) -> None:
    for col, w in (
        ("A", 7), ("B", 20), ("C", 30), ("D", 26), ("E", 14), ("F", 12),
    ):
        ws.column_dimensions[col].width = w


def write_firm_sheet(
    wb:          openpyxl.Workbook,
    firm_name:   str,
    report_date: date,
    trucks:      list[dict],
    combines:    list[dict],
) -> None:
    ws = wb.create_sheet(title=firm_name[:28])
    s  = _make_styles()
    _apply_firm_sheet_layout(ws)

    filtered_trucks = [
        t for t in trucks if t["stops_road_secs"] > IDLE_THRESHOLD_SEC
    ]
    filtered_trucks.sort(
        key=lambda x: (-x["stops_road_secs"], x.get("interval") or "")
    )
    truck_rows = [
        [
            i,
            row["machine"],
            row["object"],
            row.get("interval") or "",
            seconds_to_hms(row["stops_road_secs"]),
        ]
        for i, row in enumerate(filtered_trucks, start=1)
    ]
    truck_total = None
    if truck_rows:
        total_secs = sum(t["stops_road_secs"] for t in filtered_trucks)
        truck_total = ["Итого", "", "", "", seconds_to_hms(total_secs)]

    filtered_combines = [
        c for c in combines if c["idle_field_secs"] > IDLE_THRESHOLD_SEC
    ]
    filtered_combines.sort(
        key=lambda x: (-x["idle_field_secs"], x.get("interval") or "")
    )
    combine_rows = [
        [
            i,
            row["machine"],
            row.get("interval") or "",
            seconds_to_hms(row["idle_field_secs"]),
        ]
        for i, row in enumerate(filtered_combines, start=1)
    ]
    combine_total = None
    if combine_rows:
        total_secs = sum(c["idle_field_secs"] for c in filtered_combines)
        combine_total = ["Итого", "", "", seconds_to_hms(total_secs)]

    table_start = 1
    left_end = _write_side_table(
        ws, s,
        start_row=table_start,
        col_start=LEFT_COL,
        title=TRUCK_TITLE,
        headers=FIRM_TRUCK_COLS,
        data_rows=truck_rows,
        empty_text=EMPTY_IDLE_TEXT,
        total_row=truck_total,
    )
    right_end = _write_side_table(
        ws, s,
        start_row=table_start,
        col_start=RIGHT_COL,
        title=COMBINE_TITLE,
        headers=FIRM_COMBINE_COLS,
        data_rows=combine_rows,
        empty_text=EMPTY_IDLE_TEXT,
        total_row=combine_total,
    )
    end_row = max(left_end, right_end)
    _setup_print_layout(ws, end_col=END_COL, end_row=end_row)


# =============================================================================
# ПОСТРОЕНИЕ ОТЧЁТА
# =============================================================================

async def build_report_async(report_date: date = REPORT_DATE) -> None:
    print(f"Отчёт 'Простои транспорта и комбайнов'  |  Дата: {report_date}\n")

    data = await collect_idle_data(report_date)
    label_date = data.pop("_label_date", report_date)

    output_file = f"12. report_harvest_idle_{report_date}.xlsx"
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    write_elevator_sheet(wb, data, label_date)

    for firm_name in AGROFIRMS:
        firm_data = data.get(firm_name, {"trucks": [], "combines": []})
        write_firm_sheet(
            wb, firm_name, label_date,
            firm_data["trucks"],
            firm_data["combines"],
        )
        trucks_count   = len([t for t in firm_data["trucks"]
                               if t["stops_road_secs"] > IDLE_THRESHOLD_SEC])
        combines_count = len([c for c in firm_data["combines"]
                               if c["idle_field_secs"] > IDLE_THRESHOLD_SEC])
        print(f"  {firm_name}: грузовых={trucks_count}, комбайнов={combines_count}")

    wb.save(output_file)
    print(f"\nСохранено: {output_file}")

    # Консольный превью (каждый простой > 40 мин — отдельная строка)
    print("\n=== Элеваторы (простои > 40 мин) ===")
    for firm_name in AGROFIRMS:
        trucks = data.get(firm_name, {}).get("trucks", [])
        for t in trucks:
            if t["stops_road_secs"] <= IDLE_THRESHOLD_SEC:
                continue
            print(
                f"  {firm_name} | {t['machine']} | {t['object']} | "
                f"{t.get('interval', '')} | {seconds_to_hms(t['stops_road_secs'])}"
            )


def build_report(report_date: date = REPORT_DATE) -> None:
    asyncio.run(build_report_async(report_date))


if __name__ == "__main__":
    build_report()