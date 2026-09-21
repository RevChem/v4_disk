"""
Cropwise Operations — отчёт "Уборка"
==========================================
Все виды уборки (сводка, без овощей 5.4/5.5):
  wt=6,7,77,111,118,125,126,145

Зерновые (детализация):
  wt=126  (5.2 Уборка зерновых и зернобобовых)

Овощи 5.4 (wt=75, 76, 77) — лист «Сводка овощи»: только АФ Чистопольская,
план 5 га/сут; расчёт площади как в сводке 10 (mtfmi, культуры полей, порог 0,5 га).
Детализация по 5.10 (wt=77) — опционально на листах фирм.

Логика:
- Площадь за смену: covered_area записи mtfmi для задач с end_time в [DAY_START, CUTOFF)
  (как в Cropwise); если covered_area пуст — covered_area_hourly в окне смены
- DAY_START = report_date - 1 день + DAY_START_HOUR (по умолчанию 06:00)
- CUTOFF    = report_date + DAY_END_HOUR   (по умолчанию 06:00)
- Время:   work_duration - movements_on_road_duration из machine_task
- Скорость: average_speed_on_field (м/с) × 3.6 → км/ч
- Гос. номер: поле license_plate из machines
- Сред. выработка по сезону: среднее(площадь_дня / машин_в_день)
- Агрофирма: по полю из mtfmi (field → field_group → group_folder), не по machine_region
- Загрузка: bulk machine_tasks по work_type_id + prefetch JSON (SVODKI_PREFETCH_DATA_PATH)
- Подпись «Сводка за …» = календарный день смены (day_start.date())
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import sys
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
import pandas as pd
import requests

from excel_number_fmt import summary_cell_value_and_format
from task_area_shared import (
    CORN_SILAGE_CROP,
    CUTOFF_BUFFER_HOURS,
    HARVESTER_GROUP_IDS,
    SILAGE_CORN_MOWING_WT_IDS,
    harvest_item_area_for_shift,
)
from plans import (
    API_TOKEN,
    BASE_URL,
    TZ_MSK,
    FIRM_TO_REGION,
    FIRM_ORDER,
    FIRM_TO_FOLDER,
    FOLDER_TO_FIRM,
    FIELD_NAME_FORCE_RULES,
    HIRED_GROUP_ID,
    PLAN_RATE_PER_GROUP,
    AVAILABLE_OWN,
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
REPORT_DATE = date(2025, 9, 25)
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

SEASON_START = date(REPORT_DATE.year, 6, 10)
if REPORT_DATE < SEASON_START:
    SEASON_START = date(REPORT_DATE.year - 1, 6, 10)
_season_start_raw = (
    os.environ.get("SVODKI_SEASON_START_DATE")
    or os.environ.get("SEASON_START_DATE")
    or os.environ.get("SVODKI_COMBINE_SEASON_START")
    or ""
).strip()
if _season_start_raw:
    try:
        SEASON_START = date.fromisoformat(_season_start_raw.split("T")[0])
    except ValueError:
        pass

# Настройка временного диапазона суток
# DAY_START_HOUR — час начала суток (по умолчанию 6 = 06:00)
# DAY_END_HOUR   — час конца суток (по умолчанию 6 = следующий день 06:00)
# Примеры:
#   06:00 — 06:00 следующего дня: DAY_START_HOUR=6,  DAY_END_HOUR=6
#   06:00 — 17:59 того же дня:    DAY_START_HOUR=6,  DAY_END_HOUR=18
#   07:00 — 06:59 следующего дня: DAY_START_HOUR=7,  DAY_END_HOUR=7
DAY_START_HOUR: int = 6
DAY_END_HOUR:   int = 6


WORK_TYPE_TITLE = "Уборка"
_WORK_GROUP_KEY = "5.0 Уборка"


def _wt_ids_from_plans1(group_name: str) -> set[int]:
    """
    Множество work_type_id для группы работ — строго из plans.WORK_GROUPS
    (единственный источник правды). Если группа не найдена или пуста —
    ошибка, а не молчаливый fallback на захардкоженный список.
    """
    ids = {
        wt
        for gname, wt_ids in WORK_GROUPS
        if gname == group_name
        for wt in wt_ids
    }
    if not ids:
        raise RuntimeError(
            f"Группа {group_name!r} отсутствует (или пуста) в plans.WORK_GROUPS. "
            f"Список видов уборки должен обновляться только в plans."
        )
    return ids


# Человекочитаемые подписи видов уборки — только для вывода/детализации,
# на состав видов работ (ALL_WT_IDS) не влияют.
_WT_LABELS: dict[int, str] = {
    6:   "Уборка",
    7:   "Уборка",
    75:  "5.4 Уборка картофеля, лука",
    76:  "5.4 Уборка картофеля, лука",
    77:  "5.10 Уборка свеклы",
    111: "5.9 Уборка бобовых культур",
    118: "5.3 Уборка кукурузы на зерно",
    125: "5.1 Уборка подсолнечника",
    126: "5.2 Уборка зерновых и зернобобовых",
    145: "5.8 Уборка рапса, горчицы, гречихи и проса",
}

# Все виды уборки — для общей сводки. Состав = plans.WORK_GROUPS["5.0 Уборка"]
# + силосное скашивание (кукуруза на силос).
ALL_WT_IDS: set[int] = _wt_ids_from_plans1(_WORK_GROUP_KEY) | set(SILAGE_CORN_MOWING_WT_IDS)
ALL_HARVEST_TYPES: dict[int, str] = {
    wt: _WT_LABELS.get(wt, f"wt_{wt}") for wt in ALL_WT_IDS
}

# Овощи 5.4 — отдельная сводка «Сводка овощи» (только АФ Чистопольская, как сводка 10).
# Состав = plans.WORK_GROUPS["5.4 Уборка картофеля, лука"].
VEG_HARVEST_GROUP_KEY = "5.4 Уборка картофеля, лука"
VEG_HARVEST_FIRM = "АФ Чистопольская"
VEG_SUMMARY_FIRMS: tuple[str, ...] = (VEG_HARVEST_FIRM,)
VEG_PLAN_RATE_HA: float = float(PLAN_RATE_PER_GROUP.get(VEG_HARVEST_GROUP_KEY, 5.0))
# Маркер версии (проверка на сервере: grep SVODKA11_VEG_FIX svodki/plan/11_main_harvest.py).
SVODKA11_VEG_FIX = "2026-07-15f"
VEG_WT_IDS: set[int] = _wt_ids_from_plans1(VEG_HARVEST_GROUP_KEY)

# Зерновые — для детализации: все виды уборки из "5.0 Уборка", кроме овощей (5.4)
# и обобщённых wt=6/7 ("Уборка" без уточнения культуры — не относится к зерну).
GRAIN_WT_IDS: set[int] = ALL_WT_IDS - VEG_WT_IDS - {6, 7}

AGROFIRMS: dict[str, tuple[int, float, int]] = {
    firm: (
        FIRM_TO_REGION[firm],
        float(PLAN_RATE_PER_GROUP.get(_WORK_GROUP_KEY, 250.0)),
        int((AVAILABLE_OWN.get(_WORK_GROUP_KEY) or {}).get(firm, 0)),
    )
    for firm in FIRM_ORDER
}

MANUAL_OVERRIDES_ENV = "SVODKI_MANUAL_OVERRIDES_PATH"

FIRM_LABELS_TABLE: dict[str, str] = {
    "НурлатАгро": "Нурлат Агро",
}


def _firm_table_label(firm_key: str) -> str:
    return FIRM_LABELS_TABLE.get(firm_key, firm_key)


def _load_main10_harvest():
    """Модуль сводки 10 — уборка овощей (5.4), те же wt/сезон/фильтры полей."""
    path = Path(__file__).resolve().parent / "10_main_harvest.py"
    mod_name = "main10_harvest_for_11"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    # Обязательно до exec_module: иначе @dataclass в 10_main_harvest.py падает
    # (sys.modules[cls.__module__] is None).
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _veg_harvest_context() -> dict:
    """Контекст расчёта овощей — единая логика со сводкой 10."""
    m10 = _load_main10_harvest()
    return {
        "wt_ids": frozenset(m10.VEGETABLE_WORK_TYPE_IDS),
        "firm": m10._VEG_HARVEST_FIRM,
        "season_start": m10.VEG_SEASON_START,
        "normalize_crop": m10._normalize_veg_crop_name,
        "mtfmi_cov": m10._veg_mtfmi_cov,
    }


def _veg_item_allowed(
    firm: str | None,
    crop: str | None,
    *,
    veg_ctx: dict,
) -> bool:
    if firm != veg_ctx["firm"]:
        return False
    return bool(veg_ctx["normalize_crop"](crop))


def _veg_own_count(firm_name: str) -> int:
    return int(
        (AVAILABLE_OWN.get(VEG_HARVEST_GROUP_KEY) or {}).get(firm_name, 0)
    )


def _merge_manual_report11() -> dict:
    base = {
        "plan_rate_by_firm": {k: float(v[1]) for k, v in AGROFIRMS.items()},
        "own_count_by_firm": {k: int(v[2]) for k, v in AGROFIRMS.items()},
    }
    path = os.environ.get(MANUAL_OVERRIDES_ENV)
    if not path:
        return base
    try:
        import json
        from pathlib import Path

        p = Path(path)
        if not p.is_file():
            return base
        store = json.loads(p.read_text(encoding="utf-8"))
        block = store.get("11") or {}
        if isinstance(block.get("plan_rate_by_firm"), dict):
            for k, v in block["plan_rate_by_firm"].items():
                if k in AGROFIRMS:
                    base["plan_rate_by_firm"][k] = float(v)
        if isinstance(block.get("own_count_by_firm"), dict):
            for k, v in block["own_count_by_firm"].items():
                if k in AGROFIRMS:
                    base["own_count_by_firm"][k] = int(v)
    except Exception:
        pass
    return base


SEMAPHORE_LIMIT = 40
machines_map: dict[int, dict] = {}
_PREFETCH_ENDPOINTS: dict | None = None
_PREFETCH_LOAD_DONE = False
_cached_fields: list[dict] | None = None


def _is_harvest_combine(machine_id: int | None) -> bool:
    """Комбайны (5/9/16) или наёмные (7) — как split_machines в 14.main."""
    if machine_id is None:
        return False
    grp = machines_map.get(int(machine_id), {}).get("machine_group_id")
    if grp is None:
        return False
    grp_i = int(grp)
    return grp_i in HARVESTER_GROUP_IDS or grp_i == HIRED_GROUP_ID


def _label_report_date(day_start: datetime) -> date:
    """Календарный день смены для подписи «Сводка за …»."""
    return day_start.date()


def _norm(text: object) -> str:
    return str(text or "").casefold().replace(" ", "").replace("\u00a0", "")


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


def _fields_from_prefetch_subset(field_ids: set[int]) -> list[dict] | None:
    ep = _prefetch_endpoints()
    if not ep or not ep.get("fields"):
        return None
    all_fields = ep["fields"]
    if not field_ids:
        return all_fields
    fid_set = field_ids
    return [f for f in all_fields if f.get("id") in fid_set]


async def ensure_report_fields_loaded_async(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    field_ids: set[int],
) -> None:
    """Точечная загрузка fields (mass_request) — только поля из tasks/mtfmi."""
    global _cached_fields
    if _cached_fields is not None:
        return
    pref = _fields_from_prefetch_subset(field_ids)
    if pref is not None:
        _cached_fields = pref
        print(f"  Поля из prefetch JSON: {len(_cached_fields)}", flush=True)
        return
    if field_ids:
        from report_resources_fetch import load_fields_list_async

        _cached_fields = await load_fields_list_async(
            client, BASE_URL, field_ids, semaphore,
        )
    else:
        print("  [WARN] field_ids пуст — полная загрузка fields", flush=True)
        _cached_fields = get_all_from_id("fields")
    print(f"  Поля для отчёта: {len(_cached_fields)}", flush=True)


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
    if _cached_fields is not None:
        fields_all = _cached_fields
        print(f"  field_to_firm: {len(fields_all)} полей (mass_request/prefetch)", flush=True)
    elif ep:
        print("  Справочники полей из prefetch JSON...", flush=True)
        fields_all = ep.get("fields") or get_all_from_id("fields")
    else:
        print("  Загружаем fields / field_groups / group_folders...")
        fields_all = get_all_from_id("fields")
    if ep:
        fgs_all = ep.get("field_groups") or get_all_from_id("field_groups")
        gfs_all = ep.get("group_folders") or get_all_from_id("group_folders")
    else:
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


def _load_implements_map() -> dict[int, dict]:
    ep = _prefetch_endpoints()
    rows = (ep.get("implements") if ep else None) or get_all_from_id("implements")
    out: dict[int, dict] = {}
    for row in rows:
        rid = row.get("id")
        if rid is not None:
            out[int(rid)] = row
    return out


def _task_implement_width(task: dict, implements_map: dict[int, dict]) -> float:
    imp = implements_map.get(task.get("implement_id"))
    w = float((imp.get("width") if imp else None) or 0)
    if w > 0:
        return w
    return float(task.get("real_implement_width") or 0)


def _width_use_coef(
    track_area: float,
    work_distance_km: float,
    implement_width: float,
) -> int:
    if implement_width and work_distance_km:
        return round(track_area / work_distance_km / implement_width * 10 * 100)
    return 0


# =============================================================================
# ВЫЧИСЛЕНИЕ ВРЕМЕННОГО ДИАПАЗОНА
# =============================================================================

def parse_dt(s) -> datetime | None:
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
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_MSK)
    else:
        dt = dt.astimezone(TZ_MSK)
    return dt


def _shift_date_from_dt(dt: datetime, day_start_hour: int = DAY_START_HOUR) -> date:
    cal = dt.date()
    anchor = datetime(cal.year, cal.month, cal.day, day_start_hour, 0, 0, tzinfo=TZ_MSK)
    if dt < anchor:
        return cal - timedelta(days=1)
    return cal


def _cov_from_hourly(item: dict, day_start: datetime, cutoff: datetime) -> float:
    hourly = item.get("covered_area_hourly") or []
    cov = 0.0
    for entry in hourly:
        h = parse_dt(entry[0])
        if h and day_start <= h < cutoff:
            cov += float(entry[1] or 0.0)
    return cov


def _item_area_in_shift(item: dict, day_start: datetime, cutoff: datetime) -> float:
    """
    Площадь mtfmi за смену через covered_area_hourly в окне [day_start, cutoff).

    Всегда идём через hourly, чтобы корректно учитывать задания, пересекающие
    границу суток (например, работа с 07:29 предыдущего дня до 06:49 текущего):
    hourly-записи сами показывают, в какой час сколько убрано.
    covered_area (суммарная без разбивки по времени) — только fallback,
    когда hourly-данных нет вообще (список пуст), как в 2_main_ideal_compact.py.
    Если hourly-записи есть, но ни одна не попала в окно смены — площадь
    в этой смене равна 0 (а не переносится из другой смены через covered_area).
    """
    hourly = item.get("covered_area_hourly") or []
    if not hourly:
        return float(item.get("covered_area") or 0.0)
    return _cov_from_hourly(item, day_start, cutoff)


def _item_area_upto_cutoff(item: dict, cutoff: datetime) -> float:
    """
    Площадь mtfmi по ВСЕМ почасовым записям с меткой времени < cutoff,
    БЕЗ нижней границы (day_start/season_start).

    Как в 2_main_ideal_compact.py (collect_work_data, группа "5.0 Уборка"):
        cov = sum(entry[1] for entry in hourly if parse_dt(entry[0]) < cutoff)
    Нижней границы окна там нет вовсе. Если фильтровать ещё и по
    t_from = day_start (как делает _item_area_in_shift), часть почасовых
    записей задания — тех, что помечены временем раньше day_start, но
    всё равно относящихся к этой смене по её end_time, — отбрасывается,
    и «Убрано за сутки»/«за сезон» занижается относительно эталонной
    сводки 2 (см. diag_10_vs_2_standalone.py, причина (1); перенесено
    из 10_main_harvest.py — там эта же ошибка уже была исправлена).

    Используется для зерновых (GRAIN_WT_IDS), чтобы точно совпадать со
    сводкой 2. Овощи считаются отдельно через veg_ctx["mtfmi_cov"]
    (тоже без нижней границы, см. 10_main._veg_mtfmi_cov).
    """
    hourly = item.get("covered_area_hourly") or []
    if not hourly:
        return float(item.get("covered_area") or 0.0)
    cov = 0.0
    for entry in hourly:
        h = parse_dt(entry[0])
        if h and h < cutoff:
            cov += float(entry[1] or 0.0)
    return cov


def task_covered_area(
    task:        dict,
    mtfmi_items: list[dict] | None,
    day_start:   datetime,
    cutoff:      datetime,
) -> float:
    items = mtfmi_items or []
    if not items:
        return float(task.get("covered_area") or 0.0)
    total = 0.0
    has_hourly = False
    for item in items:
        if item.get("covered_area_hourly"):
            has_hourly = True
            total += _cov_from_hourly(item, day_start, cutoff)
    if has_hourly:
        return total
    return float(task.get("covered_area") or 0.0)


def compute_time_range(
    report_date:    date,
    day_start_hour: int = DAY_START_HOUR,
    day_end_hour:   int = DAY_END_HOUR,
) -> tuple[datetime, datetime]:
    if day_start_hour == day_end_hour:
        day_start = datetime(
            report_date.year, report_date.month, report_date.day,
            day_start_hour, 0, 0, tzinfo=TZ_MSK
        ) - timedelta(days=1)
        cutoff = datetime(
            report_date.year, report_date.month, report_date.day,
            day_end_hour, 0, 0, tzinfo=TZ_MSK
        )
    else:
        day_start = datetime(
            report_date.year, report_date.month, report_date.day,
            day_start_hour, 0, 0, tzinfo=TZ_MSK
        )
        cutoff = datetime(
            report_date.year, report_date.month, report_date.day,
            day_end_hour, 0, 0, tzinfo=TZ_MSK
        )
    return day_start, cutoff


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


def get_all_from_id(endpoint: str,
                    extra_params: dict | None = None) -> list[dict]:
    params  = dict(extra_params or {})
    results = []
    from_id = 0
    while True:
        r = session.get(f"{BASE_URL}/{endpoint}",
                        params={**params, "from_id": from_id, "limit": 1000})
        r.raise_for_status()
        body  = r.json()
        chunk = body.get("data", [])
        if not chunk: break
        results.extend(chunk)
        meta    = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100: break
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
    items   = []
    from_id = 0
    async with semaphore:
        while True:
            r     = await client.get(url, params={**params,
                                                   "from_id": from_id,
                                                   "limit":   1000})
            body  = r.json()
            chunk = body.get("data", [])
            if not chunk: break
            items.extend(chunk)
            meta    = body.get("meta", {}).get("response", {})
            last_id = meta.get("last_record_id")
            if not last_id or meta.get("obtained_records", 0) < 100: break
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
    from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks

    live = await fetch_mtfmi_map_for_tasks(
        client, semaphore, BASE_URL, missing, log_prefix="[r11] ",
    )
    out.update(live)
    return out


async def fetch_all_tasks_and_mtfmi(
    client:        httpx.AsyncClient,
    semaphore:     asyncio.Semaphore,
    day_start:     datetime,
    cutoff:        datetime,
    season_start:  datetime,
) -> tuple[list[dict], list[dict], dict[int, list[dict]]]:
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    season_start_str = season_start.strftime("%Y-%m-%dT%H:%M:%S")
    # Расширяем верхнюю границу запроса на 2 часа: задания с end_time чуть
    # позже cutoff (например, 06:49) могут содержать hourly-записи внутри
    # смены. _item_area_in_shift/task_covered_area обрежет по hourly.
    cutoff_ext_str = (cutoff + timedelta(hours=CUTOFF_BUFFER_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")

    ep = _prefetch_endpoints()
    if ep and ep.get("machine_tasks"):
        all_rows = ep.get("machine_tasks") or []
        tasks_today: list[dict] = []
        tasks_season: list[dict] = []
        seen_today: set[int] = set()
        seen_season: set[int] = set()
        for t in all_rows:
            wt = t.get("work_type_id")
            if wt not in ALL_WT_IDS:
                continue
            end_dt = parse_dt(t.get("end_time"))
            if not end_dt:
                continue
            tid = t.get("id")
            if tid is None:
                continue
            tid_i = int(tid)
            # Расширяем на 2 часа: задания с end_time чуть позже cutoff тоже
            # могут иметь hourly-записи внутри смены.
            cutoff_ext = cutoff + timedelta(hours=CUTOFF_BUFFER_HOURS)
            if end_dt >= day_start and end_dt < cutoff_ext and tid_i not in seen_today:
                seen_today.add(tid_i)
                tasks_today.append(t)
            if end_dt >= season_start and end_dt < cutoff_ext and tid_i not in seen_season:
                seen_season.add(tid_i)
                tasks_season.append(t)
        print(
            f"  machine_tasks из prefetch: сутки={len(tasks_today)}, "
            f"сезон={len(tasks_season)}",
            flush=True,
        )
        from machine_tasks_batch_fetch import fetch_machine_tasks_by_wt_async

        url = f"{BASE_URL}/machine_tasks"
        silage_wts = sorted(SILAGE_CORN_MOWING_WT_IDS)
        sup_today_raw, sup_season_raw = await asyncio.gather(
            fetch_machine_tasks_by_wt_async(
                client, semaphore, silage_wts,
                {"end_time_gt_eq": day_start_str, "end_time_lt": cutoff_ext_str},
                fetch_all_pages, url=url, log_prefix="[r11] silage+ ",
            ),
            fetch_machine_tasks_by_wt_async(
                client, semaphore, silage_wts,
                {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_ext_str},
                fetch_all_pages, url=url, log_prefix="[r11] silage+ ",
            ),
        )
        for t in sup_today_raw:
            tid_i = int(t["id"])
            if tid_i not in seen_today:
                seen_today.add(tid_i)
                tasks_today.append(t)
        for t in sup_season_raw:
            tid_i = int(t["id"])
            if tid_i not in seen_season:
                seen_season.add(tid_i)
                tasks_season.append(t)
        if sup_today_raw or sup_season_raw:
            print(
                f"  machine_tasks скашивание (API): +{len(sup_today_raw)} сутки, "
                f"+{len(sup_season_raw)} сезон",
                flush=True,
            )
    else:
        from machine_tasks_batch_fetch import fetch_machine_tasks_by_wt_async

        url = f"{BASE_URL}/machine_tasks"
        tasks_today_raw, tasks_season_raw = await asyncio.gather(
            fetch_machine_tasks_by_wt_async(
                client, semaphore, ALL_WT_IDS,
                {"end_time_gt_eq": day_start_str, "end_time_lt": cutoff_ext_str},
                fetch_all_pages, url=url, log_prefix="[r11] today ",
            ),
            fetch_machine_tasks_by_wt_async(
                client, semaphore, ALL_WT_IDS,
                {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_ext_str},
                fetch_all_pages, url=url, log_prefix="[r11] season ",
            ),
        )
        seen_today: set[int] = set()
        tasks_today = []
        for t in tasks_today_raw:
            if t["id"] not in seen_today:
                seen_today.add(t["id"])
                tasks_today.append(t)
        seen_season: set[int] = set()
        tasks_season = []
        for t in tasks_season_raw:
            if t["id"] not in seen_season:
                seen_season.add(t["id"])
                tasks_season.append(t)
        print(
            f"  Заданий за сутки: {len(tasks_today)}, "
            f"за сезон (с {SEASON_START.strftime('%d.%m.%Y')}): {len(tasks_season)}",
            flush=True,
        )

    all_task_ids = list({t["id"] for t in tasks_today + tasks_season})
    mtfmi_map: dict[int, list[dict]] = {}
    if all_task_ids:
        print(f"  Загружаем mtfmi для {len(all_task_ids)} заданий...", flush=True)
        mtfmi_map = await fetch_mtfmi_for_tasks(client, all_task_ids, semaphore)

    return tasks_today, tasks_season, mtfmi_map


async def _load_harvest_resources_async(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    day_start: datetime,
    cutoff: datetime,
    season_start: datetime,
) -> tuple[list[dict], list[dict], dict[int, list[dict]]]:
    """tasks + batch mtfmi + mass_request fields/machines (как сводка 10)."""
    global machines_map
    from report_resources_fetch import collect_report_resource_ids, load_machines_map_async

    tasks_today, tasks_season, mtfmi_map = await fetch_all_tasks_and_mtfmi(
        client, semaphore, day_start, cutoff, season_start,
    )
    field_ids, machine_ids, _ = collect_report_resource_ids(
        tasks_today + tasks_season, mtfmi_map,
    )
    print(
        f"  Ресурсы отчёта: полей={len(field_ids)} (mtfmi+tasks)",
        flush=True,
    )
    await ensure_report_fields_loaded_async(client, semaphore, field_ids)

    ep = _prefetch_endpoints()
    if ep and ep.get("machines"):
        machines_map = {m["id"]: m for m in ep.get("machines") or []}
        print(f"  Машин из prefetch: {len(machines_map)}", flush=True)
    elif machine_ids:
        machines_map = await load_machines_map_async(
            client, BASE_URL, machine_ids, semaphore,
        )
        print(f"  Машин для отчёта: {len(machines_map)}", flush=True)
    else:
        machines_map = {}

    return tasks_today, tasks_season, mtfmi_map


# =============================================================================
# ПОДСЧЁТ ПО АГРОФИРМАМ (через поле)
# =============================================================================

def _empty_firm_stats() -> dict:
    return {
        "machines_today": 0,
        "area_today":     0.0,
        "grain_area_today": 0.0,
        "veg_area_today":   0.0,
        "machines_grain":   0,
        "machines_veg":     0,
        "avg_season":     0.0,
        "avg_season_grain": 0.0,
        "avg_season_veg":   0.0,
        "engine_today":   0.0,
        "engine_grain_today": 0.0,
        "engine_veg_today":   0.0,
        "grain_own":      [],
        "grain_hired":    [],
        "veg_own":        [],
        "veg_hired":      [],
    }


def _avg_season_from_daily(
    daily_area: dict[str, float],
    daily_machines: dict[str, set[int]],
) -> float:
    if not daily_area:
        return 0.0
    vals = [
        daily_area[d] / len(daily_machines[d])
        for d in daily_area
        if daily_machines.get(d)
    ]
    return round(sum(vals) / len(vals), 1) if vals else 0.0


def aggregate_harvest_by_firm(
    tasks_today:   list[dict],
    tasks_season:  list[dict],
    mtfmi_map:     dict[int, list[dict]],
    field_to_firm: dict[int, str],
    day_start:     datetime,
    cutoff:        datetime,
    *,
    field_to_crop: dict[int, str] | None = None,
    veg_ctx:       dict | None = None,
    implements_map: dict[int, dict] | None = None,
) -> dict[str, dict]:
    field_to_crop = field_to_crop or {}
    veg_ctx = veg_ctx or _veg_harvest_context()
    implements_map = implements_map or {}
    veg_wt_ids = veg_ctx["wt_ids"]
    veg_season_start = veg_ctx["season_start"]

    per_firm_machine: dict[str, dict[int, dict]] = {firm: {} for firm in AGROFIRMS}

    for t in tasks_today:
        mid = t.get("machine_id")
        wt = t.get("work_type_id")
        if mid is None:
            continue
        if wt in GRAIN_WT_IDS and not _is_harvest_combine(mid):
            continue
        items = mtfmi_map.get(t["id"], [])

        firm_items: dict[str, list[tuple[dict, float]]] = defaultdict(list)
        for item in items:
            fid = item.get("field_id")
            if fid is None:
                continue
            fid_i = int(fid)
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue
            crop = field_to_crop.get(fid_i)
            if wt in SILAGE_CORN_MOWING_WT_IDS and crop != CORN_SILAGE_CROP:
                continue
            if wt in veg_wt_ids:
                if not _veg_item_allowed(firm, crop, veg_ctx=veg_ctx):
                    continue
                cov = float(veg_ctx["mtfmi_cov"](item, cutoff))
            else:
                cov = harvest_item_area_for_shift(
                    item, cutoff,
                    day_start=day_start,
                    work_type_id=wt,
                    parse_dt=parse_dt,
                )
            if cov <= 0:
                continue
            firm_items[firm].append((item, cov))
        if not firm_items:
            continue

        total_cov_in_window = 0.0
        for item in items:
            fid = item.get("field_id")
            if fid is None:
                continue
            fid_i = int(fid)
            firm_i = field_to_firm.get(fid_i)
            if not firm_i:
                continue
            crop_i = field_to_crop.get(fid_i)
            if wt in SILAGE_CORN_MOWING_WT_IDS and crop_i != CORN_SILAGE_CROP:
                continue
            if wt in veg_wt_ids:
                if not _veg_item_allowed(firm_i, crop_i, veg_ctx=veg_ctx):
                    continue
                item_cov = float(veg_ctx["mtfmi_cov"](item, cutoff))
            else:
                item_cov = harvest_item_area_for_shift(
                    item, cutoff,
                    day_start=day_start,
                    work_type_id=wt,
                    parse_dt=parse_dt,
                )
            if item_cov > 0:
                total_cov_in_window += item_cov
        work_dur = t.get("work_duration") or 0
        road_move = t.get("movements_on_road_duration") or 0
        work_net = max(work_dur - road_move, 0)
        sp = t.get("average_speed_on_field")

        for firm, f_items in firm_items.items():
            cov = sum(c for _, c in f_items)
            if cov <= 0:
                continue

            pm = per_firm_machine[firm]
            if mid not in pm:
                pm[mid] = {
                    "cov_total":  0.0,
                    "grain_area": 0.0,
                    "veg_area":   0.0,
                    "engine_dur": 0.0,
                    "speeds":     [],
                    "track_area": 0.0,
                    "work_distance_km": 0.0,
                    "implement_width": 0.0,
                }
            pm[mid]["cov_total"] += cov
            if wt in GRAIN_WT_IDS:
                pm[mid]["grain_area"] += cov
            elif wt in veg_wt_ids:
                pm[mid]["veg_area"] += cov

            if wt in GRAIN_WT_IDS or wt in veg_wt_ids:
                for item, _item_cov in f_items:
                    pm[mid]["track_area"] += float(item.get("covered_area_by_track") or 0)
                    pm[mid]["work_distance_km"] += float(item.get("work_distance") or 0) / 1000
                if not pm[mid]["implement_width"]:
                    pm[mid]["implement_width"] = _task_implement_width(t, implements_map)

            if total_cov_in_window > 0:
                pm[mid]["engine_dur"] += work_net * (cov / total_cov_in_window)
            else:
                pm[mid]["engine_dur"] += work_net
            if sp and sp > 0:
                pm[mid]["speeds"].append(sp)

    season_daily_area: dict[str, dict[str, float]] = {
        firm: defaultdict(float) for firm in AGROFIRMS
    }
    season_daily_area_grain: dict[str, dict[str, float]] = {
        firm: defaultdict(float) for firm in AGROFIRMS
    }
    season_daily_area_veg: dict[str, dict[str, float]] = {
        firm: defaultdict(float) for firm in AGROFIRMS
    }
    season_daily_machines: dict[str, dict[str, set[int]]] = {
        firm: defaultdict(set) for firm in AGROFIRMS
    }
    season_daily_machines_grain: dict[str, dict[str, set[int]]] = {
        firm: defaultdict(set) for firm in AGROFIRMS
    }
    season_daily_machines_veg: dict[str, dict[str, set[int]]] = {
        firm: defaultdict(set) for firm in AGROFIRMS
    }
    upper = (cutoff.date() - timedelta(days=1)).isoformat()

    for t in tasks_season:
        mid = t.get("machine_id")
        wt = t.get("work_type_id")
        grain_task = wt in GRAIN_WT_IDS
        if grain_task and not _is_harvest_combine(mid):
            continue
        end_dt = parse_dt(t.get("end_time"))
        if not end_dt:
            continue
        shift = _shift_date_from_dt(end_dt, DAY_START_HOUR)
        if shift < veg_season_start:
            continue
        _, shift_cutoff = compute_time_range(
            shift, DAY_START_HOUR, DAY_END_HOUR
        )
        shift_key = shift.isoformat()
        if shift_key > upper:
            continue

        items = mtfmi_map.get(t["id"], [])
        for item in items:
            fid = item.get("field_id")
            if fid is None:
                continue
            fid_i = int(fid)
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue
            crop = field_to_crop.get(fid_i)
            if wt in SILAGE_CORN_MOWING_WT_IDS and crop != CORN_SILAGE_CROP:
                continue
            if wt in veg_wt_ids:
                if not _veg_item_allowed(firm, crop, veg_ctx=veg_ctx):
                    continue
                cov = float(veg_ctx["mtfmi_cov"](item, shift_cutoff))
            else:
                _, shift_day_start = compute_time_range(
                    shift, DAY_START_HOUR, DAY_END_HOUR,
                )
                cov = harvest_item_area_for_shift(
                    item, shift_cutoff,
                    day_start=shift_day_start,
                    work_type_id=wt,
                    parse_dt=parse_dt,
                )
            if cov <= 0:
                continue
            season_daily_area[firm][shift_key] += cov
            if wt in GRAIN_WT_IDS:
                season_daily_area_grain[firm][shift_key] += cov
                if mid is not None:
                    season_daily_machines_grain[firm][shift_key].add(int(mid))
            elif wt in veg_wt_ids:
                season_daily_area_veg[firm][shift_key] += cov
                if mid is not None:
                    season_daily_machines_veg[firm][shift_key].add(int(mid))
            if mid is not None:
                season_daily_machines[firm][shift_key].add(int(mid))

    result: dict[str, dict] = {}
    for firm in AGROFIRMS:
        empty = _empty_firm_stats()
        pm = per_firm_machine[firm]
        if not pm:
            empty = _empty_firm_stats()
            empty["avg_season"] = _avg_season_from_daily(
                season_daily_area[firm], season_daily_machines[firm],
            )
            empty["avg_season_grain"] = _avg_season_from_daily(
                season_daily_area_grain[firm], season_daily_machines_grain[firm],
            )
            empty["avg_season_veg"] = _avg_season_from_daily(
                season_daily_area_veg[firm], season_daily_machines_veg[firm],
            )
            result[firm] = empty
            continue

        grain_own: list[dict] = []
        grain_hired: list[dict] = []
        veg_own: list[dict] = []
        veg_hired: list[dict] = []

        for mid, d in pm.items():
            secs = d["engine_dur"]
            is_hired = machines_map.get(mid, {}).get("machine_group_id") == HIRED_GROUP_ID

            def make_row(area: float) -> dict:
                r_rate = round(area / (secs / 3600), 2) if secs else 0.0
                speed_val = round(sum(d["speeds"]) / len(d["speeds"]) * 3.6) \
                    if d["speeds"] else 0
                coef = _width_use_coef(
                    d["track_area"], d["work_distance_km"], d["implement_width"],
                )
                row = {
                    "Машина":                     _machine_label(int(mid)),
                    "Площадь, га":                round(area, 2),
                    "Время (сек)":                secs,
                    "Время на полях, час":        seconds_to_hms(secs),
                    "Ср. выр-ка га/час":          r_rate,
                    "Средняя скорость на полях км/ч": speed_val,
                    DETAIL_COEF_COL:              coef,
                }
                return row

            if d["grain_area"] > 0 and _is_harvest_combine(mid):
                row = make_row(d["grain_area"])
                (grain_hired if is_hired else grain_own).append(row)
            if d["veg_area"] > 0:
                row = make_row(d["veg_area"])
                (veg_hired if is_hired else veg_own).append(row)
            other = d["cov_total"] - d["grain_area"] - d["veg_area"]
            if (
                other > 0
                and d["grain_area"] == 0
                and d["veg_area"] == 0
                and _is_harvest_combine(mid)
            ):
                row = make_row(other)
                (grain_hired if is_hired else grain_own).append(row)

        for lst in (grain_own, grain_hired, veg_own, veg_hired):
            lst.sort(key=lambda x: -x["Площадь, га"])

        area_today = round(sum(d["cov_total"] for d in pm.values()), 2)
        grain_area_today = round(sum(d["grain_area"] for d in pm.values()), 2)
        veg_area_today = round(sum(d["veg_area"] for d in pm.values()), 2)
        machines_grain = sum(
            1 for mid, d in pm.items()
            if d["grain_area"] > 0 and _is_harvest_combine(mid)
        )
        machines_veg = sum(1 for d in pm.values() if d["veg_area"] > 0)
        engine_today = sum(d["engine_dur"] for d in pm.values())
        engine_grain_today = sum(d["engine_dur"] for d in pm.values() if d["grain_area"] > 0)
        engine_veg_today = sum(d["engine_dur"] for d in pm.values() if d["veg_area"] > 0)
        avg_season = _avg_season_from_daily(
            season_daily_area[firm], season_daily_machines[firm],
        )
        avg_season_grain = _avg_season_from_daily(
            season_daily_area_grain[firm], season_daily_machines_grain[firm],
        )
        avg_season_veg = _avg_season_from_daily(
            season_daily_area_veg[firm], season_daily_machines_veg[firm],
        )

        result[firm] = {
            "machines_today": len(pm),
            "area_today":     area_today,
            "grain_area_today": grain_area_today,
            "veg_area_today":   veg_area_today,
            "machines_grain":   machines_grain,
            "machines_veg":     machines_veg,
            "avg_season":     avg_season,
            "avg_season_grain": avg_season_grain,
            "avg_season_veg":   avg_season_veg,
            "engine_today":   round(engine_today / 3600, 1),
            "engine_grain_today": round(engine_grain_today / 3600, 1),
            "engine_veg_today":   round(engine_veg_today / 3600, 1),
            "grain_own":      grain_own,
            "grain_hired":    grain_hired,
            "veg_own":        veg_own,
            "veg_hired":      veg_hired,
        }

    return result


# =============================================================================
# СТИЛИ
# =============================================================================

def _make_styles() -> dict:
    thin   = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    return {
        "border":        border,
        "align_c":       Alignment(horizontal="center", vertical="center",
                                    wrap_text=True),
        "align_l":       Alignment(vertical="center", wrap_text=True),
        "fill_hdr":      PatternFill("solid", fgColor="8093B3"),
        "fill_own":      PatternFill("solid", fgColor="FFFFFF"),
        "fill_hired":    PatternFill("solid", fgColor="FFFFFF"),
        "fill_total":    PatternFill("solid", fgColor="BDD7EE"),
        # Вся "обычная" часть таблицы должна быть белой.
        "fill_sec_own":  PatternFill("solid", fgColor="FFFFFF"),
        "fill_sec_hire": PatternFill("solid", fgColor="FFFFFF"),
        "fill_block":    PatternFill("solid", fgColor="FFFFFF"),
        "font_hdr":      Font(bold=True, color="FFFFFF"),
        "font_total":    Font(bold=True),
        "font_title":    Font(bold=True, size=13),
        "font_sec":      Font(bold=True, size=12, color="111827"),
        "font_block":    Font(bold=True, size=12, color="111827"),
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


def _row_height_for_text(
    value: object,
    col_width: float | None,
    *,
    min_height: float = 20.0,
    line_height: float = 16.0,
    max_height: float = 200.0,
) -> float:
    lines = _estimate_wrapped_lines(value, col_width)
    return min(max_height, max(min_height, lines * line_height + 4))


def _auto_fit_row_heights(
    ws,
    start_row: int,
    end_row: int,
    max_col: int,
    *,
    min_height: float = 20.0,
    line_height: float = 16.0,
    max_height: float = 200.0,
) -> None:
    for r in range(start_row, end_row + 1):
        max_lines = 1
        for c in range(1, max_col + 1):
            cell = ws.cell(row=r, column=c)
            if cell.value in (None, ""):
                continue
            col_letter = get_column_letter(c)
            col_width = ws.column_dimensions[col_letter].width
            max_lines = max(max_lines, _estimate_wrapped_lines(cell.value, col_width))
        target = min(max_height, max(min_height, max_lines * line_height + 4))
        current = ws.row_dimensions[r].height
        ws.row_dimensions[r].height = max(current or 0.0, target)


def _merged_row_height(
    ws,
    row: int,
    col_start: int,
    col_end: int,
    value: object,
    *,
    min_height: float = 24.0,
) -> None:
    total_width = 0.0
    for ci in range(col_start, col_end + 1):
        w = ws.column_dimensions[get_column_letter(ci)].width
        total_width += float(w or 10)
    ws.row_dimensions[row].height = _row_height_for_text(
        value, total_width, min_height=min_height,
    )


_SUMMARY_WIDTHS_11: dict[int, float] = {
    1: 8,
    2: 22,
    3: 24,
    4: 18,
    5: 16,
    6: 22,
    7: 22,
    8: 24,
    9: 18,
}

_DETAIL_WIDTHS_11: dict[int, float] = {
    1: 7,
    2: 46,
    3: 14,
    4: 18,
    5: 16,
    6: 18,
    7: 22,
}

# Зерновая детализация — те же колонки, включая коэффициент использования
# конструкционной ширины захвата (track_area/work_distance/implement_width
# теперь считаются в aggregate_harvest_by_firm и для зерновых, и для овощей).
_GRAIN_DETAIL_WIDTHS_11: dict[int, float] = dict(_DETAIL_WIDTHS_11)


# =============================================================================
# КОЛОНКИ ДЕТАЛИЗАЦИИ
# =============================================================================

DETAIL_COEF_COL = "Коэффициент использования конструкционной ширины захвата, %"

DETAIL_COLS = [
    "№ п/п",
    "Машина",
    "Площадь, га",
    "Время на полях, час",
    "Ср. выр-ка га/час",
    "Средняя скорость на полях км/ч",
    DETAIL_COEF_COL,
]

DETAIL_COL_HEADERS = [
    "№ п/п",
    "Машина",
    "Площадь, га",
    "Время на полях, час",
    "Ср. выр-ка га/час",
    "Средняя скорость на полях км/ч",
    "Коэффициент использования\nконструкционной ширины\nзахвата, %",
]

# Зерновая детализация — те же колонки, что и овощная (включая коэффициент
# использования конструкционной ширины захвата, см. _GRAIN_DETAIL_WIDTHS_11).
GRAIN_DETAIL_COLS = list(DETAIL_COLS)
GRAIN_DETAIL_COL_HEADERS = list(DETAIL_COL_HEADERS)

# =============================================================================
# СВОДНЫЕ КОЛОНКИ
# =============================================================================

SUMMARY_COLS = [
    "№ п/п",
    "Агрофирма",
    "Плановая выработка, га/сутки",
    "Убрано за сутки, га",
    "Количество комбайнов на полях",
    "Выработка за сутки, га/1 комбайн",
    "Часовая выработка на 1 комбайн, га",
    "Средняя выработка по сезону, га/сутки",
    "Всего собств. комбайнов в наличии",
]


# =============================================================================
# ЗАПИСЬ ЛИСТА ДЕТАЛИЗАЦИИ
# =============================================================================

def _write_detail_section(
    ws,
    cur:      list,
    s:        dict,
    n:        int,
    sec_title: str,
    rows:     list[dict],
    row_fill: object,
    sec_fill: object,
    col_headers: list[str] = DETAIL_COL_HEADERS,
    include_coef: bool = True,
) -> None:
    """Записывает одну секцию (СОБСТВЕННЫЕ или НАЁМНЫЕ) внутри блока.

    col_headers/include_coef позволяют переиспользовать функцию для зерновой
    детализации (без колонки коэффициента использования ширины захвата —
    она осмысленна только для овощей, см. GRAIN_DETAIL_COL_HEADERS)."""

    def nr() -> int:
        r = cur[0]; cur[0] += 1; return r

    def mw(value, fill=None, font=None) -> None:
        r = nr()
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=n)
        c = ws.cell(row=r, column=1, value=value)
        _sc(c, s, fill=fill, font=font, align=s["align_c"])

    sec_row = nr()
    ws.merge_cells(start_row=sec_row, start_column=1, end_row=sec_row, end_column=n)
    c = ws.cell(row=sec_row, column=1, value=sec_title)
    _sc(c, s, fill=sec_fill, font=s["font_sec"], align=s["align_c"])
    _merged_row_height(ws, sec_row, 1, n, sec_title, min_height=26.0)

    # заголовки колонок
    r = nr()
    for ci, col in enumerate(col_headers, start=1):
        c = ws.cell(row=r, column=ci, value=col)
        _sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_c"])
    ws.row_dimensions[r].height = 72.0

    if not rows:
        mw("Не привлекались")
        return

    for i, rd in enumerate(rows, start=1):
        r = nr()
        vals = [
            i,
            rd["Машина"],
            rd["Площадь, га"],
            rd["Время на полях, час"],
            rd["Ср. выр-ка га/час"],
            rd["Средняя скорость на полях км/ч"],
        ]
        if include_coef:
            vals.append(rd.get(DETAIL_COEF_COL, 0))
        for ci, val in enumerate(vals, start=1):
            c = ws.cell(row=r, column=ci, value=val)
            _sc(c, s, fill=row_fill,
                align=s["align_l"] if ci == 2 else s["align_c"])
        mname = str(rd["Машина"] or "")
        ws.row_dimensions[r].height = 36.0 if len(mname) > 42 else 28.0

    # итоговая строка секции
    tc  = round(sum(rd["Площадь, га"] for rd in rows), 2)
    ts  = sum(rd["Время (сек)"] for rd in rows)
    tr  = round(tc / (ts / 3600), 2) if ts else 0.0
    asp = round(
        sum(rd["Средняя скорость на полях км/ч"] for rd in rows) / len(rows), 1,
    ) if rows else 0.0
    tail_vals = ["Итого", "", tc, seconds_to_hms(ts), tr, asp]
    if include_coef:
        acf = round(
            sum(rd.get(DETAIL_COEF_COL, 0) for rd in rows) / len(rows),
        ) if rows else 0
        tail_vals.append(acf)
    r = nr()
    for ci, val in enumerate(tail_vals, start=1):
        c = ws.cell(row=r, column=ci, value=val)
        _sc(c, s, fill=s["fill_total"], font=s["font_total"], align=s["align_c"])


def _detail_sheet_title(firm_name: str, crop_kind: str) -> str:
    """Название листа детализации; для зерновых добавляем короткий суффикс,
    чтобы не конфликтовать с листом овощей той же агрофирмы (Excel требует
    уникальные имена листов, лимит 31 символ).
    Суффикс « (зерно)» не попадает в шапку PDF — срезается в report_generation."""
    label = _firm_table_label(firm_name)
    suffix = "" if crop_kind == "veg" else " (зерно)"
    limit = 31 - len(suffix)
    return label[:limit] + suffix


def write_detail_sheet(
    wb:          openpyxl.Workbook,
    firm_name:   str,
    report_date: date,
    day_start:   datetime,
    cutoff:      datetime,
    stats:       dict,
    *,
    crop_kind:   str = "veg",
) -> None:
    """Детализация по машинам агрофирмы: без шапки, без блока 5.2.

    crop_kind="veg"   — овощи 5.4: stats["veg_own"]/["veg_hired"],
                         с колонкой коэффициента использования ширины захвата.
    crop_kind="grain" — все зерновые виды уборки (5.0 минус 5.4, без wt=6/7):
                         stats["grain_own"]/["grain_hired"], тоже с колонкой
                         коэффициента использования ширины захвата."""
    if crop_kind == "veg":
        own_rows, hired_rows = stats["veg_own"], stats["veg_hired"]
        col_headers = DETAIL_COL_HEADERS
        n = len(DETAIL_COLS)
        include_coef = True
        widths = _DETAIL_WIDTHS_11
    else:
        own_rows, hired_rows = stats["grain_own"], stats["grain_hired"]
        col_headers = GRAIN_DETAIL_COL_HEADERS
        n = len(GRAIN_DETAIL_COLS)
        include_coef = True
        widths = _GRAIN_DETAIL_WIDTHS_11

    ws  = wb.create_sheet(title=_detail_sheet_title(firm_name, crop_kind))
    s   = _make_styles()
    cur = [1]

    _write_detail_section(ws, cur, s, n,
                          "СОБСТВЕННЫЕ",
                          own_rows,
                          s["fill_own"], s["fill_sec_own"],
                          col_headers, include_coef)
    cur[0] += 1
    _write_detail_section(ws, cur, s, n,
                          "НАЁМНЫЕ",
                          hired_rows,
                          s["fill_hired"], s["fill_sec_hire"],
                          col_headers, include_coef)

    for ci, w in widths.items():
        ws.column_dimensions[get_column_letter(ci)].width = w
    _auto_fit_row_heights(ws, start_row=1, end_row=max(1, cur[0] - 1), max_col=n)


# =============================================================================
# СВОДНЫЙ ЛИСТ
# =============================================================================

def _build_summary_rows(
    firm_names:  list[str],
    all_details: dict[str, dict],
    manual:      dict | None = None,
    *,
    crop_kind:   str = "grain",
) -> list[dict]:
    merged = manual if manual is not None else _merge_manual_report11()
    plan_map = merged.get("plan_rate_by_firm") or {}
    own_map = merged.get("own_count_by_firm") or {}
    rows = []
    for i, firm_name in enumerate(firm_names, start=1):
        st = all_details[firm_name]
        if crop_kind == "veg":
            plan_day = VEG_PLAN_RATE_HA
            own_count = _veg_own_count(firm_name)
        else:
            plan_day = float(plan_map.get(firm_name, AGROFIRMS[firm_name][1]))
            own_count = int(own_map.get(firm_name, AGROFIRMS[firm_name][2]))

        if crop_kind == "veg":
            area_today = st.get("veg_area_today", 0.0)
            n_mach = st.get("machines_veg", 0)
            eng = float(st.get("engine_veg_today") or 0.0)
            avg_season = st.get("avg_season_veg", 0.0)
        else:
            area_today = st.get("grain_area_today", st.get("area_today", 0.0))
            n_mach = st.get("machines_grain", st.get("machines_today", 0))
            eng = float(st.get("engine_grain_today") or st.get("engine_today") or 0.0)
            avg_season = st.get("avg_season_grain", st.get("avg_season", 0.0))

        day_per_machine   = round(area_today / n_mach, 2) if n_mach else 0.0
        hour_per_machine  = round(area_today / eng,    2) if eng    else 0.0

        rows.append({
            "№ п/п":                                    i,
            "Агрофирма":                                _firm_table_label(firm_name),
            "Плановая выработка, га/сутки":             plan_day,
            "Убрано за сутки, га":                      area_today,
            "Количество комбайнов на полях":             n_mach,
            "Выработка за сутки, га/1 комбайн":         day_per_machine,
            "Часовая выработка на 1 комбайн, га":       hour_per_machine,
            "Средняя выработка по сезону, га/сутки":    avg_season,
            "Всего собств. комбайнов в наличии":        own_count,
        })

    if crop_kind == "veg":
        tm = sum(r["Количество комбайнов на полях"] for r in rows)
        ta = round(sum(r["Убрано за сутки, га"] for r in rows), 2)
        te_h = sum(float(all_details[f].get("engine_veg_today") or 0.0) for f in firm_names)
        avgs = [r["Средняя выработка по сезону, га/сутки"]
                for r in rows if r["Средняя выработка по сезону, га/сутки"] > 0]
    else:
        tm   = sum(r["Количество комбайнов на полях"]          for r in rows)
        ta   = round(sum(r["Убрано за сутки, га"]              for r in rows), 2)
        te_h = sum(
            float(all_details[firm_name].get("engine_grain_today")
                  or all_details[firm_name].get("engine_today") or 0.0)
            for firm_name in firm_names
        )
        avgs = [r["Средняя выработка по сезону, га/сутки"]
                for r in rows if r["Средняя выработка по сезону, га/сутки"] > 0]
    to   = sum(r["Всего собств. комбайнов в наличии"]      for r in rows)

    rows.append({
        "№ п/п":                                    "",
        "Агрофирма":                                "Итого",
        "Плановая выработка, га/сутки":
            rows[0]["Плановая выработка, га/сутки"] if rows else 0,
        "Убрано за сутки, га":                      ta,
        "Количество комбайнов на полях":             tm,
        "Выработка за сутки, га/1 комбайн":
            round(ta / tm, 2) if tm else 0.0,
        "Часовая выработка на 1 комбайн, га":
            round(ta / te_h, 1) if te_h else 0.0,
        "Средняя выработка по сезону, га/сутки":
            round(sum(avgs) / len(avgs), 1) if avgs else 0.0,
        "Всего собств. комбайнов в наличии":        to,
    })
    return rows


def write_summary_sheet(
    wb:          openpyxl.Workbook,
    report_date: date,
    day_start:   datetime,
    cutoff:      datetime,
    firm_names:  list[str],
    all_details: dict[str, dict],
    *,
    sheet_title: str = "Сводка",
    crop_kind:   str = "grain",
    sheet_index: int = 0,
) -> None:
    ws = wb.create_sheet(title=sheet_title[:31], index=sheet_index)
    s  = _make_styles()
    n  = len(SUMMARY_COLS)

    rows    = _build_summary_rows(
        firm_names, all_details, _merge_manual_report11(), crop_kind=crop_kind,
    )
    hdr_row = 1

    for ci, col in enumerate(SUMMARY_COLS, start=1):
        c = ws.cell(row=hdr_row, column=ci, value=col)
        _sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_c"])

    for ri, row_data in enumerate(rows, start=hdr_row + 1):
        is_total = row_data.get("Агрофирма") == "Итого"
        fill     = s["fill_total"] if is_total else s["fill_own"]
        font     = s["font_total"] if is_total else None
        for ci, col in enumerate(SUMMARY_COLS, start=1):
            raw = row_data.get(col, "")
            val, nf = summary_cell_value_and_format(col, raw)
            c = ws.cell(row=ri, column=ci, value=val)
            if nf:
                c.number_format = nf
            _sc(c, s, fill=fill, font=font,
                align=s["align_l"] if ci == 2 else s["align_c"])

    for ci in range(1, n + 1):
        ws.column_dimensions[get_column_letter(ci)].width = _SUMMARY_WIDTHS_11.get(ci, 20)

    end_row = hdr_row + len(rows)
    _auto_fit_row_heights(ws, start_row=1, end_row=end_row, max_col=n)
    ws.row_dimensions[hdr_row].height = max(
        ws.row_dimensions[hdr_row].height or 0.0, 78.0,
    )


# =============================================================================
# ПОСТРОЕНИЕ ОТЧЁТА
# =============================================================================

async def prepare_veg_summary_context_for_report1(report_date: date = REPORT_DATE):
    """Контекст для листа «Сводка oвощi» в standalone сводке 1."""
    global machines_map

    day_start, cutoff = compute_time_range(
        report_date, DAY_START_HOUR, DAY_END_HOUR,
    )
    season_start = datetime(
        SEASON_START.year, SEASON_START.month, SEASON_START.day,
        DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    label_date = _label_report_date(day_start)

    m10 = _load_main10_harvest()
    field_to_crop = m10.get_field_to_crop()
    veg_ctx = _veg_harvest_context()
    implements_map = _load_implements_map()

    headers = {"X-User-Api-Token": API_TOKEN}
    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        tasks_today, tasks_season, mtfmi_map = await _load_harvest_resources_async(
            client, semaphore, day_start, cutoff, season_start,
        )

    field_to_firm = load_field_to_firm()
    all_details = aggregate_harvest_by_firm(
        tasks_today, tasks_season, mtfmi_map, field_to_firm, day_start, cutoff,
        field_to_crop=field_to_crop,
        veg_ctx=veg_ctx,
        implements_map=implements_map,
    )
    return {
        "label_date": label_date,
        "day_start": day_start,
        "cutoff": cutoff,
        "all_details": all_details,
    }


def write_veg_summary_sheet_for_report1(wb, ctx: dict) -> None:
    """Записать лист «Сводка овощи» в workbook сводки 1."""
    title = "Сводка овощи"
    # Убрать заглушку / старое имя с латинской «o» (ломало порядок листов и PDF-graft).
    for stale in list(wb.sheetnames):
        if stale == title:
            del wb[stale]
            break
        if stale.replace("o", "о").replace("i", "и") == title:
            del wb[stale]
            break
    write_summary_sheet(
        wb,
        ctx["label_date"],
        ctx["day_start"],
        ctx["cutoff"],
        list(VEG_SUMMARY_FIRMS),
        ctx["all_details"],
        sheet_title=title,
        crop_kind="veg",
        sheet_index=len(wb.sheetnames),
    )


async def build_report_async(report_date: date = REPORT_DATE) -> None:
    global machines_map

    day_start, cutoff = compute_time_range(
        report_date, DAY_START_HOUR, DAY_END_HOUR
    )
    season_start = datetime(
        SEASON_START.year, SEASON_START.month, SEASON_START.day,
        DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    label_date = _label_report_date(day_start)

    print(f"Отчёт '{WORK_TYPE_TITLE}'  |  {SVODKA11_VEG_FIX}  |  Дата платформы: {report_date}")
    print(f"  Суточный диапазон: {day_start} — {cutoff}")
    print(f"  Подпись «Сводка за»: {label_date.strftime('%d.%m.%Y')}\n")

    m10 = _load_main10_harvest()
    field_to_crop = m10.get_field_to_crop()
    veg_ctx = _veg_harvest_context()
    firm_names = list(AGROFIRMS.keys())

    print(
        f"  Овощи 5.4: фирма={veg_ctx['firm']}, wt={sorted(veg_ctx['wt_ids'])}, "
        f"план={VEG_PLAN_RATE_HA} га/сут, сезон с {veg_ctx['season_start']}",
        flush=True,
    )

    implements_map = _load_implements_map()
    print(f"  Орудия (implements): {len(implements_map)}", flush=True)

    headers = {"X-User-Api-Token": API_TOKEN}
    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        tasks_today, tasks_season, mtfmi_map = await _load_harvest_resources_async(
            client, semaphore, day_start, cutoff, season_start,
        )

    field_to_firm = load_field_to_firm()

    all_details = aggregate_harvest_by_firm(
        tasks_today, tasks_season, mtfmi_map, field_to_firm, day_start, cutoff,
        field_to_crop=field_to_crop,
        veg_ctx=veg_ctx,
        implements_map=implements_map,
    )

    # консольный вывод сводки
    rows = _build_summary_rows(firm_names, all_details, _merge_manual_report11())
    df   = pd.DataFrame(rows)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 300)
    pd.set_option("display.float_format", "{:.2f}".format)
    print("\n")
    print(df.to_string(index=False))

    output_file = f"11. report_harvest_{report_date}.xlsx"
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    write_summary_sheet(
        wb, label_date, day_start, cutoff,
        firm_names, all_details,
        sheet_title="Сводка", crop_kind="grain", sheet_index=0,
    )
    write_summary_sheet(
        wb, label_date, day_start, cutoff,
        list(VEG_SUMMARY_FIRMS), all_details,
        sheet_title="Сводка овощи", crop_kind="veg", sheet_index=1,
    )

    for firm_name in firm_names:
        write_detail_sheet(
            wb, firm_name, label_date, day_start, cutoff,
            all_details[firm_name],
            crop_kind="grain",
        )

    for firm_name in VEG_SUMMARY_FIRMS:
        write_detail_sheet(
            wb, firm_name, label_date, day_start, cutoff,
            all_details[firm_name],
            crop_kind="veg",
        )

    wb.save(output_file)
    print(f"\nСохранено: {output_file}")


def build_report(report_date: date = REPORT_DATE) -> None:
    asyncio.run(build_report_async(report_date))


if __name__ == "__main__":
    build_report()
