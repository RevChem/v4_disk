"""
2. Сводка ХК Детальная (полная версия, локальный анализ / история 2023–2025).

База: svodki/2_main_ideal_compact.py с сервера — REPORT_WINDOW, _cov_from_hourly,
корректный учёт уборки. Компактный plan/2_main_ideal_compact.py — урезанная копия для Docker.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from plans_1 import *

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
import requests
from excel_number_fmt import NF_DEC1, NF_DEC2, NF_INT

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

START_DATE   = date(2026, 3, 14)
TODAY        = date(2026, 4, 29)

if os.environ.get("REPORT_DATE"):
    TODAY = date.fromisoformat(os.environ["REPORT_DATE"].strip())
_season_start_raw = (
    os.environ.get("SVODKI_SEASON_START_DATE")
    or os.environ.get("SEASON_START_DATE")
    or ""
).strip()
if _season_start_raw:
    try:
        START_DATE = date.fromisoformat(_season_start_raw)
    except ValueError:
        pass

OUTPUT_FILE  = f"2. report_crop_works_detail_{TODAY}.xlsx"
REPORT_WINDOW_START_RAW = os.environ.get("REPORT_WINDOW_START", "").strip()
REPORT_WINDOW_END_RAW = os.environ.get("REPORT_WINDOW_END", "").strip()
CUSTOM_WINDOW_START: datetime | None = None
CUSTOM_WINDOW_END: datetime | None = None
CUSTOM_SHIFT_DATE_MIN: date | None = None
CUSTOM_SHIFT_DATE_MAX: date | None = None
BORON_GROUP_NAME = "2.1 Боронование"
DISK_GROUP_NAME = "2.9 Дискование"
PLOW_GROUP_NAME = "2.4 Вспашка"
MIN_DETAIL_AREA_HA = 0.5
# Счётчик агрегатов и суточная выработка на 1 агрегат — как в сводках 5, 7, 8.
MIN_COUNT_AREA_HA = 10.0
MIN_COUNT_AREA_EXEMPT_GROUPS: frozenset[str] = frozenset({
    "2.8 Глубокорыхление",
    "4.1 Посев (овощи)",
    "2.6 Фрезерование",
    "2.7 Гребнеобразование",
    "5.0 уборка",
    "5.0 Уборка",
})


def _parse_report_window(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=TZ_MSK)
        except ValueError:
            continue
    return parse_dt(text)


def _shift_date_from_dt(dt: datetime, day_start_hour: int = 6) -> date:
    cal = dt.date()
    anchor = datetime(cal.year, cal.month, cal.day, day_start_hour, 0, 0, tzinfo=TZ_MSK)
    if dt < anchor:
        return cal - timedelta(days=1)
    return cal


def _last_shift_date_before(window_end: datetime, day_start_hour: int = 6) -> date:
    """Последняя смена, попадающая в окно [*, window_end) (смена D = [D 06:00, D+1 06:00))."""
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=TZ_MSK)
    else:
        window_end = window_end.astimezone(TZ_MSK)
    return _shift_date_from_dt(window_end - timedelta(seconds=1), day_start_hour)


def _shift_overlaps_window(day_start: datetime, cutoff: datetime) -> bool:
    if CUSTOM_WINDOW_START is None or CUSTOM_WINDOW_END is None:
        return True
    return day_start < CUSTOM_WINDOW_END and cutoff > CUSTOM_WINDOW_START


def _init_custom_window() -> None:
    global CUSTOM_WINDOW_START, CUSTOM_WINDOW_END, CUSTOM_SHIFT_DATE_MIN, CUSTOM_SHIFT_DATE_MAX
    global TODAY, OUTPUT_FILE
    ws = _parse_report_window(REPORT_WINDOW_START_RAW)
    we = _parse_report_window(REPORT_WINDOW_END_RAW)
    if ws is None or we is None:
        return
    if ws >= we:
        raise ValueError(
            f"Неверное окно сводки 2: {REPORT_WINDOW_START_RAW} — {REPORT_WINDOW_END_RAW}"
        )
    CUSTOM_WINDOW_START = ws
    CUSTOM_WINDOW_END = we
    CUSTOM_SHIFT_DATE_MIN = _shift_date_from_dt(ws)
    CUSTOM_SHIFT_DATE_MAX = _last_shift_date_before(we)
    if CUSTOM_SHIFT_DATE_MAX < CUSTOM_SHIFT_DATE_MIN:
        raise ValueError(
            f"Неверное окно сводки 2: нет ни одной смены между "
            f"{REPORT_WINDOW_START_RAW} и {REPORT_WINDOW_END_RAW}"
        )
    # cal_date < TODAY: включаем смены до CUSTOM_SHIFT_DATE_MAX включительно
    TODAY = CUSTOM_SHIFT_DATE_MAX + timedelta(days=1)
    OUTPUT_FILE = (
        f"report_crop_works_compact_{CUSTOM_SHIFT_DATE_MIN.isoformat()}_"
        f"{CUSTOM_SHIFT_DATE_MAX.isoformat()}.xlsx"
    )


_init_custom_window()


def _excel_period_export() -> bool:
    """Сводка 2 за произвольный период — только Excel на сайте (без PDF)."""
    return CUSTOM_SHIFT_DATE_MIN is not None and CUSTOM_SHIFT_DATE_MAX is not None


def _hourly_window_bounds(day_start: datetime, cutoff: datetime) -> tuple[datetime, datetime] | None:
    """Границы учёта hourly: смена, обрезанная произвольным окном REPORT_WINDOW_*."""
    lo = day_start
    hi = cutoff
    if CUSTOM_WINDOW_START is not None and CUSTOM_WINDOW_END is not None:
        lo = max(day_start, CUSTOM_WINDOW_START)
        hi = min(cutoff, CUSTOM_WINDOW_END)
        if lo >= hi:
            return None
    return lo, hi


def _cov_from_hourly(item: dict, day_start: datetime, cutoff: datetime) -> float:
    bounds = _hourly_window_bounds(day_start, cutoff)
    if bounds is None:
        return 0.0
    lo, hi = bounds
    hourly = item.get("covered_area_hourly") or []
    cov = 0.0
    for entry in hourly:
        h = parse_dt(entry[0])
        if h and lo <= h < hi:
            cov += float(entry[1] or 0.0)
    return cov


def _item_area_in_shift(item: dict, day_start: datetime, cutoff: datetime) -> float:
    """Для уборки используем правило сводки 11: covered_area, hourly только как fallback."""
    ca = float(item.get("covered_area") or 0.0)
    if ca > 0:
        return ca
    return _cov_from_hourly(item, day_start, cutoff)


def _group_count_threshold(group_name: str) -> float:
    if group_name in MIN_COUNT_AREA_EXEMPT_GROUPS:
        return MIN_DETAIL_AREA_HA
    return MIN_COUNT_AREA_HA


def _day_rate_area(by_mid: dict[int, float], area_threshold: float = MIN_DETAIL_AREA_HA) -> float:
    """Площадь за день: сумма по машинам >= порога (как cov_total >= 10 в сводке 1)."""
    return sum(float(v) for v in (by_mid or {}).values() if float(v) >= area_threshold)


def _day_rate_machines(by_mid: dict[int, float], count_threshold: float) -> int:
    return sum(1 for v in (by_mid or {}).values() if round(float(v), 2) >= count_threshold)


def _season_stats_for_firm(
    mda_firm: dict[date, dict[int, float]],
    stats_dates: list[date],
    area_threshold: float = MIN_DETAIL_AREA_HA,
    count_threshold: float = MIN_DETAIL_AREA_HA,
) -> tuple[float, int]:
    """Средняя по сезону: площадь и machine_days по порогам конкретной группы."""
    season_sum = 0.0
    machine_days = 0
    for dt in stats_dates:
        by_mid = mda_firm.get(dt, {})
        day_area = _day_rate_area(by_mid, area_threshold)
        if day_area <= 0:
            continue
        season_sum += day_area
        machine_days += _day_rate_machines(by_mid, count_threshold)
    return season_sum, machine_days


MANUAL_OVERRIDES_ENV = "SVODKI_MANUAL_OVERRIDES_PATH"

SEMAPHORE_LIMIT = 5

def is_skip_crop(crop_name: str) -> bool:
    if not crop_name:
        return True
    if crop_name in SKIP_CROPS:
        return True
    if crop_name.lower().startswith("введение в оборот"):
        return True
    return False


def _crop_plan_multiplier(crop_name: str) -> float:
    if (crop_name or "").strip().lower() in OZ_RAPE_CROP_NAMES:
        return 2.0
    return 1.0

_PREFERRED_CROP_INDEX = {name.lower(): i for i, name in enumerate(PREFERRED_CROP_ORDER)}

def _crop_sort_key(name: str) -> tuple[int, int, str]:
    raw = str(name or "").strip()
    low = raw.lower()
    if low in _PREFERRED_CROP_INDEX:
        return (0, _PREFERRED_CROP_INDEX[low], low)
    # Культуры вне списка — после приоритетных, в алфавите.
    return (1, 10_000, low)


def _apply_manual_plan_rates() -> None:
    path = os.environ.get(MANUAL_OVERRIDES_ENV, "").strip()
    if not path:
        return
    p = Path(path)
    if not p.is_file():
        return
    try:
        store = json.loads(p.read_text(encoding="utf-8"))
        block = store.get("2") or {}
        raw_map = block.get("plan_rate_by_group")
        if not isinstance(raw_map, dict):
            return
        for group_name, raw in raw_map.items():
            try:
                PLAN_RATE_PER_GROUP[str(group_name)] = float(raw)
            except (TypeError, ValueError):
                continue
    except Exception:
        return

# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ИЕРАРХИИ ПАПОК
# =============================================================================

def _norm_field_label(name: str) -> str:
    return (
        (name or "")
        .strip()
        .casefold()
        .replace(" ", "")
        .replace("\u00a0", "")
    )


def _firm_from_field_name(field_rec: dict) -> str | None:
    blob = _norm_field_label(
        f"{field_rec.get('name') or ''} {field_rec.get('title') or ''}"
    )
    if not blob:
        return None
    for needle, firm_key in FIELD_NAME_FORCE_RULES:
        if needle in blob:
            return firm_key
    return None


def _folder_parent_map(group_folders: list[dict]) -> dict[int, int | None]:
    parent: dict[int, int | None] = {}
    for gf in group_folders:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = (gf.get("parent_id")
               or gf.get("parent_group_folder_id")
               or gf.get("group_folder_parent_id"))
        parent[int(fid)] = int(pid) if pid is not None else None
    return parent


def _group_folder_name_firm_hints(group_folders: list[dict]) -> dict[int, str]:
    hints: dict[int, str] = {}
    for gf in group_folders:
        fid = gf.get("id")
        if fid is None:
            continue
        blob = _norm_field_label(f"{gf.get('name') or ''} {gf.get('title') or ''}")
        if not blob:
            continue
        if any(x in blob for x in ("лениногор", "птицефабрик", "лпф", "lpf")):
            hints[int(fid)] = "Лениногорская Птицефабрика"
    return hints


def _resolve_to_holding_root(
    folder_id:  int | None,
    parent_map: dict[int, int | None],
    root_ids:   set[int],
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


def _folder_id_chain(folder_id: int | None, parent_map: dict[int, int | None]) -> list[int]:
    if folder_id is None:
        return []
    out: list[int] = []
    cur = int(folder_id)
    seen: set[int] = set()
    for _ in range(64):
        if cur in seen:
            break
        seen.add(cur)
        out.append(cur)
        nxt = parent_map.get(cur)
        if nxt is None:
            break
        cur = int(nxt)
    return out


def load_field_to_firm(fields_all: list[dict],
                       fgs_all: list[dict],
                       gfs_all: list[dict]) -> dict[int, str]:
    """
    Строит словарь field_id → firm_name через иерархию папок.
    Также применяет принудительные правила по имени поля.
    """
    fgs_map    = {fg["id"]: fg for fg in fgs_all}
    parent_map = _folder_parent_map(gfs_all)
    root_ids   = set(FIRM_TO_FOLDER.values())
    gf_hints   = _group_folder_name_firm_hints(gfs_all)

    field_to_firm: dict[int, str] = {}

    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        fgid       = f.get("field_group_id")
        raw_folder = f.get("group_folder_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(fgid, {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None

        firm_key = None
        # Сначала ищем по подсказкам имён папок в цепочке
        for cid in _folder_id_chain(rint, parent_map):
            if cid in gf_hints:
                firm_key = gf_hints[cid]
                break
        # Затем по корневым папкам холдинга
        if firm_key is None:
            root = _resolve_to_holding_root(rint, parent_map, root_ids)
            if root is not None:
                firm_key = FOLDER_TO_FIRM.get(root)
        if firm_key:
            field_to_firm[int(fid)] = firm_key

    # Принудительные правила по имени поля (перезаписывают)
    forced = 0
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        fk = _firm_from_field_name(f)
        if fk:
            field_to_firm[int(fid)] = fk
            forced += 1

    print(
        f"  Полей с привязкой: {len(field_to_firm)} "
        f"(принудительно по имени: {forced})"
    )
    return field_to_firm


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
    s = str(s).strip()
    if not s:
        return None
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


# =============================================================================
# HTTP
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})

def get_all_from_id(endpoint: str,
                    extra_params: dict | None = None) -> list[dict]:
    params  = dict(extra_params or {})
    results = []
    from_id = 0
    for _ in range(500):
        request_params = {**params, "from_id": from_id, "limit": 1000}
        last_exc: Exception | None = None
        for attempt in range(1, 6):
            try:
                r = session.get(
                    f"{BASE_URL}/{endpoint}",
                    params=request_params,
                    timeout=120,
                )
                break
            except requests.RequestException as exc:
                last_exc = exc
                wait_s = min(30, attempt * 5)
                print(
                    f"    [retry {attempt}/5] {endpoint} from_id={from_id}: {exc}; "
                    f"sleep {wait_s}s",
                    flush=True,
                )
                time.sleep(wait_s)
        else:
            raise last_exc or RuntimeError(f"Failed to load {endpoint}")
        r.raise_for_status()
        body  = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        results.extend(chunk)
        meta    = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = last_id + 1
    return results

# =============================================================================
# ЗАГРУЗКА СПРАВОЧНИКОВ
# =============================================================================

def _load_available_field_ids_on_date(target_date: date) -> set[int]:
    """
    Возвращает множество field_id, у которых available=True на конкретную дату.
    Запись активна на дату, если:
        event_start_at <= target_date
        AND (event_end_at IS NULL OR event_end_at >= target_date)
        AND available == True
    """
    print("  Загружаем inventory_history_items (архивный статус полей)...")
    ihi_all = get_all_from_id("inventory_history_items", {
        "historyable_type": "Field",
    })
    print(f"    Всего записей inventory_history_items: {len(ihi_all)}")

    available_ids: set[int] = set()
    for r in ihi_all:
        if not r.get("available"):
            continue
        fid = r.get("historyable_id")
        if fid is None:
            continue
        raw_start = r.get("event_start_at")
        raw_end   = r.get("event_end_at")
        try:
            ev_start = date.fromisoformat(str(raw_start)[:10]) if raw_start else None
        except ValueError:
            ev_start = None
        try:
            ev_end = date.fromisoformat(str(raw_end)[:10]) if raw_end else None
        except ValueError:
            ev_end = None
        if ev_start is None:
            continue
        if ev_start <= target_date and (ev_end is None or ev_end >= target_date):
            available_ids.add(int(fid))

    print(f"    Полей available=True на {target_date}: {len(available_ids)}")
    return available_ids


def _load_available_field_ids_in_period(period_start: date, period_end: date,
                                         ihi_all: list[dict]) -> set[int]:
    """
    Возвращает множество field_id, у которых available=True хотя бы в один день
    периода [period_start, period_end]. Использует уже загруженный список ihi_all.
    Пересечение: ev_start <= period_end AND (ev_end IS NULL OR ev_end >= period_start).
    """
    available_ids: set[int] = set()
    for r in ihi_all:
        if not r.get("available"):
            continue
        fid = r.get("historyable_id")
        if fid is None:
            continue
        raw_start = r.get("event_start_at")
        raw_end   = r.get("event_end_at")
        try:
            ev_start = date.fromisoformat(str(raw_start)[:10]) if raw_start else None
        except ValueError:
            ev_start = None
        try:
            ev_end = date.fromisoformat(str(raw_end)[:10]) if raw_end else None
        except ValueError:
            ev_end = None
        if ev_start is None:
            continue
        if ev_start <= period_end and (ev_end is None or ev_end >= period_start):
            available_ids.add(int(fid))

    print(f"    Полей available=True в периоде {period_start}–{period_end}: {len(available_ids)}")
    return available_ids


def load_reference_data() -> tuple[
    dict[int, str],
    dict[int, str],
    dict[int, float],
    dict[int, int],
    dict[str, float],
    dict[str, dict[str, float]],
]:
    print("  Загружаем crops...")
    crops_map = {c["id"]: c.get("name") or c.get("title", "")
                 for c in get_all_from_id("crops")}

    _crop_year = int(
        (os.environ.get("SVODKI_CROP_YEAR") or os.environ.get("SVODKI_SEASON") or "")
        or START_DATE.year
    )
    print(f"  Загружаем history_items year={_crop_year}...")
    his_all = get_all_from_id("history_items", {"year": _crop_year})
    field_to_crop: dict[int, str] = {}
    for hi in his_all:
        fid     = hi.get("field_id")
        crop_id = hi.get("crop_id")
        active  = hi.get("active")
        if fid and crop_id and crop_id in crops_map and active:
            crop_name = crops_map[crop_id]
            if crop_name in SKIP_CROPS:
                continue
            field_to_crop[fid] = crop_name

    crop_field_ids = set(field_to_crop.keys())
    print(f"    {len(crop_field_ids)} полей с культурой в сезоне {_crop_year}")

    print("  Загружаем fields / field_groups / group_folders...")
    fields_all = get_all_from_id("fields")
    fgs_all    = get_all_from_id("field_groups")
    gfs_all    = get_all_from_id("group_folders")

    # Площадь полей (все поля, не только активные — на случай нужды)
    field_area: dict[int, float] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        field_area[fid] = (f.get("tillable_area") or
                           f.get("calculated_area") or 0.0)

    # Определяем агрофирму для каждого поля через полную иерархию папок
    field_to_firm_all = load_field_to_firm(fields_all, fgs_all, gfs_all)

    # Загружаем inventory_history_items один раз — используем дважды
    print("  Загружаем inventory_history_items (архивный статус полей)...")
    ihi_all = get_all_from_id("inventory_history_items", {
        "historyable_type": "Field",
    })
    print(f"    Всего записей inventory_history_items: {len(ihi_all)}")

    # --- Для ПЛАНА: только поля, активные (available=True) на сегодня ---
    plan_available_ids: set[int] = set()
    for r in ihi_all:
        if not r.get("available"):
            continue
        fid = r.get("historyable_id")
        if fid is None:
            continue
        raw_start = r.get("event_start_at")
        raw_end   = r.get("event_end_at")
        try:
            ev_start = date.fromisoformat(str(raw_start)[:10]) if raw_start else None
        except ValueError:
            ev_start = None
        try:
            ev_end = date.fromisoformat(str(raw_end)[:10]) if raw_end else None
        except ValueError:
            ev_end = None
        if ev_start is None:
            continue
        if ev_start <= TODAY and (ev_end is None or ev_end >= TODAY):
            plan_available_ids.add(int(fid))
    print(f"    Полей available=True на сегодня ({TODAY}): {len(plan_available_ids)}")

    # field_to_firm для плана: поле есть в сезоне И активно сегодня
    field_to_firm_plan = {
        fid: firm for fid, firm in field_to_firm_all.items()
        if fid in crop_field_ids and fid in plan_available_ids
    }
    print(f"    {len(field_to_firm_plan)} полей для плана (активны сегодня, с культурой)")

    # --- Для ФАКТА: поля, активные хотя бы в один день периода ---
    period_available_ids = _load_available_field_ids_in_period(
        START_DATE, TODAY, ihi_all
    )
    # field_to_firm для сбора фактических работ: поле есть в сезоне И было активно в периоде
    field_to_firm = {
        fid: firm for fid, firm in field_to_firm_all.items()
        if fid in crop_field_ids and fid in period_available_ids
    }
    print(f"    {len(field_to_firm)} полей для факта (активны в периоде, с культурой)")

    # Плановые площади считаем только по полям, активным сегодня
    firm_plan_ha:      dict[str, float]            = defaultdict(float)
    firm_crop_area_ha: dict[str, dict[str, float]] = defaultdict(
        lambda: defaultdict(float))

    for fid, firm in field_to_firm_plan.items():
        crop = field_to_crop.get(fid, "")
        if is_skip_crop(crop):
            continue
        area = field_area.get(fid, 0.0)
        firm_plan_ha[firm] += area
        firm_crop_area_ha[firm][crop] += area

    print("  Загружаем machines...")
    machines_all = get_all_from_id("machines")
    machine_group: dict[int, int] = {
        m["id"]: (m.get("machine_group_id") or 0)
        for m in machines_all
    }

    return (
        field_to_crop,
        field_to_firm,       # для сбора факта: поля активные в периоде
        field_to_firm_all,   # все поля с привязкой к фирме (как сводка 7)
        field_area,
        machine_group,
        dict(firm_plan_ha),
        {f: dict(crops) for f, crops in firm_crop_area_ha.items()},
    )

# =============================================================================
# АСИНХРОННАЯ ЗАГРУЗКА MTFMI
# =============================================================================

async def fetch_all_pages_async(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    items   = []
    from_id = 0
    while True:
        async with semaphore:
            r = await client.get(url, params={**params,
                                              "from_id": from_id,
                                              "limit":   1000})
        body  = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        items.extend(chunk)
        meta    = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = last_id + 1
    return items

async def fetch_mtfmi_for_tasks(
    task_ids: list[int],
    headers: dict,
) -> dict[int, list[dict]]:
    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    url       = f"{BASE_URL}/machine_task_field_mapping_items"
    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        results = await asyncio.gather(*[
            fetch_all_pages_async(client, url,
                                  {"machine_task_id": tid}, semaphore)
            for tid in task_ids
        ])
    return {tid: items for tid, items in zip(task_ids, results)}

# =============================================================================
# СБОР ДАННЫХ (только для групп с заданиями)
# =============================================================================

def _flush_report17_area_data(
    data: dict,
    group_name: str,
    machine_day_area: dict[str, dict[date, dict[int, float]]],
    field_day_area: dict[str, dict[date, dict[int, float]]],
    field_to_crop: dict[int, str],
    firm_crop_fields: dict[str, dict[str, set]],
    agg: dict[str, dict[str, dict[date, float]]],
) -> bool:
    """Площадь как в сводке 7: порог 0.5 га на машину за смену, не на каждую mtfmi-запись."""
    gd = data[group_name]
    group_has = False

    for firm in FIRM_ORDER:
        gd["area_by_firm_total"][firm] = 0.0
        gd["area_by_firm_date"][firm] = defaultdict(float)

    for firm in FIRM_ORDER:
        for t_date, by_mid in machine_day_area.get(firm, {}).items():
            day_area = sum(
                float(v) for v in by_mid.values()
                if round(float(v), 2) > MIN_DETAIL_AREA_HA
            )
            if day_area <= 0:
                continue
            group_has = True
            gd["area_by_firm_date"][firm][t_date] += day_area
            gd["area_by_firm_total"][firm] += day_area

    for firm in FIRM_ORDER:
        for t_date, by_fid in field_day_area.get(firm, {}).items():
            for fid, f_cov in by_fid.items():
                if round(float(f_cov), 2) <= MIN_DETAIL_AREA_HA:
                    continue
                crop = field_to_crop.get(fid, "Не определена")
                agg[firm][crop][t_date] += float(f_cov)
                firm_crop_fields[firm][crop].add(fid)
                group_has = True

    return group_has


def _shift_window_report1(end_time_raw, day_start_hour: int = 6) -> tuple[date, datetime, datetime] | None:
    """Окно смены — как в 1_main_ammonium__1_.py."""
    end_dt = parse_dt(end_time_raw)
    if end_dt is None:
        return None
    cal_date = end_dt.date()
    anchor = datetime(
        cal_date.year, cal_date.month, cal_date.day,
        day_start_hour, 0, 0, tzinfo=TZ_MSK,
    )
    if end_dt < anchor:
        cal_date = cal_date - timedelta(days=1)
        anchor = datetime(
            cal_date.year, cal_date.month, cal_date.day,
            day_start_hour, 0, 0, tzinfo=TZ_MSK,
        )
    return cal_date, anchor, anchor + timedelta(days=1)


def _task_in_report_shift_window(
    end_time_raw,
    day_start: datetime,
    cutoff: datetime,
) -> bool:
    sw = _shift_window_report1(end_time_raw)
    if sw is None:
        return False
    _, shift_ds, shift_co = sw
    return shift_ds >= day_start and shift_co <= cutoff


def collect_work_data(
    field_to_crop: dict[int, str],
    field_to_firm: dict[int, str],
    field_to_firm_all: dict[int, str],
    field_area:    dict[int, float],
    machine_group: dict[int, int],
    headers:       dict,
) -> dict:
    data: dict[str, dict] = {}

    DAY_START_HOUR = 6  # смена начинается в 06:00 МСК
    # Сутки отчёта при смене 06:00–06:00 (REPORT_DATE = TODAY из env): [TODAY-1 06:00, TODAY 06:00)
    REPORT_DAY_CUTOFF = datetime(
        TODAY.year, TODAY.month, TODAY.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    REPORT_DAY_START = REPORT_DAY_CUTOFF - timedelta(days=1)
    REPORT_SHIFT_DATE = TODAY - timedelta(days=1)

    def _shift_date_and_window(end_time_raw) -> tuple[date, datetime, datetime] | None:
        """
        Определяет дату смены и временное окно [day_start, cutoff) по end_time задания.
        Логика как в файле 3: смена дня D = [D 06:00, D+1 06:00).
        Если end_time < D 06:00 — задание относится к смене D-1.
        Возвращает (shift_date, day_start, cutoff) или None если вне периода.
        """
        end_dt = parse_dt(end_time_raw)
        if end_dt is None:
            return None
        # Определяем дату смены: если end_time до 06:00 МСК этого дня — это смена предыдущего дня
        cal_date = end_dt.date()
        cutoff   = datetime(cal_date.year, cal_date.month, cal_date.day,
                            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
        if end_dt < cutoff:
            # end_time до 06:00 — относится к смене предыдущего календарного дня
            cal_date = cal_date - timedelta(days=1)
            cutoff   = datetime(cal_date.year, cal_date.month, cal_date.day,
                                DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK) + timedelta(days=1)
        else:
            # end_time после 06:00 — смена текущего дня, cutoff = следующий день 06:00
            cutoff = cutoff + timedelta(days=1)
        day_start = cutoff - timedelta(days=1)
        if not (START_DATE <= cal_date < TODAY):
            return None
        if not _shift_overlaps_window(day_start, cutoff):
            return None
        return cal_date, day_start, cutoff

    for group_name, wt_ids in WORK_GROUPS:
        print(f"  Загружаем {group_name}...")
        # Для 1.1 синхронизируем расчёт площади с отчётом 3:
        # берём covered_area_hourly до cutoff без нижней границы окна и без fallback на covered_area.
        # Это устраняет расхождения по фирмам (в т.ч. НурлатАгро) между сводками 2 и 3.
        use_report3_coverage_logic = (len(wt_ids) == 1 and wt_ids[0] == 55)
        # Боронование — как new/4.main.py (не hourly, сезон с 01.01, только задания с covered_area>0).
        use_report4_boron_logic = group_name == BORON_GROUP_NAME
        # Дискование — площадь как в сводках 1 и 7: без is_skip_crop, только поля с культурой в сезоне.
        use_report17_area_logic = group_name == DISK_GROUP_NAME
        all_tasks: list[dict] = []
        for wt_id in wt_ids:
            # Запрашиваем с запасом: от START_DATE 06:00 до (TODAY+1) 06:00
            # Для боронования — от 01.01 года 00:00, как в отчёте 4.
            if CUSTOM_WINDOW_START is not None and CUSTOM_WINDOW_END is not None:
                season_start_str = CUSTOM_WINDOW_START.strftime("%Y-%m-%dT%H:%M:%S")
                cutoff_dt = CUSTOM_WINDOW_END
            elif use_report4_boron_logic:
                season_start_str = f"{TODAY.year}-01-01T00:00:00"
                cutoff_dt = datetime(TODAY.year, TODAY.month, TODAY.day,
                                     DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK) + timedelta(days=1)
            else:
                season_start_dt = datetime(START_DATE.year, START_DATE.month, START_DATE.day,
                                           DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
                season_start_str = season_start_dt.strftime("%Y-%m-%dT%H:%M:%S")
                cutoff_dt = datetime(TODAY.year, TODAY.month, TODAY.day,
                                     DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK) + timedelta(days=1)
            tasks = get_all_from_id("machine_tasks", {
                "work_type_id":   wt_id,
                "end_time_gt_eq": season_start_str,
                "end_time_lt":    cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            all_tasks.extend(tasks)

        if use_report4_boron_logic:
            all_tasks = [t for t in all_tasks if (t.get("covered_area") or 0) > 0]
            print(f"    заданий с covered_area>0 (как в отчёте 4): {len(all_tasks)}")

        if not all_tasks:
            print(f"    Нет заданий, пропускаем")
            continue

        # Вычисляем дату смены для каждого задания и фильтруем вне периода
        task_shift: dict[int, tuple[date, datetime, datetime]] = {}
        filtered_tasks: list[dict] = []
        no_shift_tasks: list[dict] = []   # задания без end_time — fallback на covered_area
        for t in all_tasks:
            result = _shift_date_and_window(t.get("end_time"))
            if result is not None:
                task_shift[t["id"]] = result
                filtered_tasks.append(t)
            else:
                no_shift_tasks.append(t)   # не выбрасываем, берём covered_area напрямую
        all_tasks = filtered_tasks

        if not all_tasks and not no_shift_tasks:
            print(f"    Нет заданий в периоде после фильтра по смене, пропускаем")
            continue
        if not all_tasks:
            print(f"    Нет заданий с известной сменой, только {len(no_shift_tasks)} без end_time")

        print(f"    {len(all_tasks)} заданий за период")

        group_has_data = False  # Реальные непустые значения, пришедшие из API
        data[group_name] = {
            "crops":              {},
            "machines":           {f: defaultdict(lambda: {"own": set(), "hired": set()})
                                   for f in FIRM_ORDER},
            "area_by_firm_date":  {f: defaultdict(float) for f in FIRM_ORDER},
            "area_by_firm_total": {f: 0.0 for f in FIRM_ORDER},
        }

        all_tasks_for_mtfmi = all_tasks + no_shift_tasks
        task_machine: dict[int, int] = {t["id"]: t.get("machine_id") for t in all_tasks_for_mtfmi}
        task_ids  = [t["id"] for t in all_tasks_for_mtfmi]
        mtfmi_map = asyncio.run(fetch_mtfmi_for_tasks(task_ids, headers))

        agg: dict[str, dict[str, dict[date, float]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(float)))
        firm_crop_fields: dict[str, dict[str, set]] = defaultdict(
            lambda: defaultdict(set))
        machine_day_area: dict[str, dict[date, dict[int, float]]] = {
            f: defaultdict(lambda: defaultdict(float)) for f in FIRM_ORDER
        }
        field_day_area: dict[str, dict[date, dict[int, float]]] = {
            f: defaultdict(lambda: defaultdict(float)) for f in FIRM_ORDER
        }
        count_threshold = (
            MIN_DETAIL_AREA_HA
            if group_name in MIN_COUNT_AREA_EXEMPT_GROUPS
            else MIN_COUNT_AREA_HA
        )

        for task in all_tasks:
            tid  = task["id"]
            mid  = task_machine.get(tid)
            shift_info = task_shift.get(tid)
            if not shift_info:
                continue
            t_date, day_start, cutoff = shift_info

            if group_name == PLOW_GROUP_NAME:
                if t_date == REPORT_SHIFT_DATE:
                    if not _task_in_report_shift_window(
                        task.get("end_time"), REPORT_DAY_START, REPORT_DAY_CUTOFF,
                    ):
                        continue
                    hourly_cutoff = REPORT_DAY_CUTOFF
                else:
                    hourly_cutoff = cutoff
            else:
                hourly_cutoff = cutoff

            for item in mtfmi_map.get(tid, []):
                fid = item.get("field_id")
                if not fid:
                    continue

                firm = (field_to_firm_all if use_report17_area_logic else field_to_firm).get(int(fid))
                if not firm:
                    continue

                crop = field_to_crop.get(fid, "Не определена")
                if not use_report17_area_logic and is_skip_crop(crop):
                    continue

                if use_report4_boron_logic:
                    if int(fid) not in field_to_crop:
                        continue
                    cov = float(item.get("covered_area") or 0)
                elif group_name in {"5.0 Уборка", "5.4 Уборка картофеля, лука"}:
                    cov = _item_area_in_shift(item, day_start, cutoff)
                else:
                    cov = _cov_from_hourly(item, day_start, cutoff)
                    if not (item.get("covered_area_hourly") or []) and not use_report3_coverage_logic:
                        cov = item.get("covered_area") or 0

                if use_report17_area_logic:
                    if cov <= 0:
                        continue
                    if mid:
                        machine_day_area[firm][t_date][mid] += float(cov)
                    field_day_area[firm][t_date][int(fid)] += float(cov)
                    continue

                item_min_ha = 0.0 if group_name == PLOW_GROUP_NAME else MIN_DETAIL_AREA_HA
                if cov <= item_min_ha:
                    continue

                group_has_data = True
                agg[firm][crop][t_date]         += cov
                firm_crop_fields[firm][crop].add(fid)

                if mid:
                    machine_day_area[firm][t_date][mid] += cov

                if group_name != PLOW_GROUP_NAME:
                    data[group_name]["area_by_firm_date"][firm][t_date]  += cov
                    data[group_name]["area_by_firm_total"][firm]         += cov

        for task in no_shift_tasks:
            tid = task["id"]
            if use_report3_coverage_logic:
                continue  # для 1.1 без смены не считаем
            for item in mtfmi_map.get(tid, []):
                fid = item.get("field_id")
                if not fid:
                    continue
                firm = (field_to_firm_all if use_report17_area_logic else field_to_firm).get(int(fid))
                if not firm:
                    continue
                crop = field_to_crop.get(fid, "Не определена")
                if not use_report17_area_logic and is_skip_crop(crop):
                    continue
                if use_report4_boron_logic and int(fid) not in field_to_crop:
                    continue
                cov = float(item.get("covered_area") or 0)
                mid = task_machine.get(tid)
                if use_report17_area_logic:
                    if cov <= 0:
                        continue
                    t_date = TODAY
                    if mid:
                        machine_day_area[firm][t_date][mid] += cov
                    field_day_area[firm][t_date][int(fid)] += cov
                    continue
                item_min_ha = 0.0 if group_name == PLOW_GROUP_NAME else MIN_DETAIL_AREA_HA
                if cov <= item_min_ha:
                    continue
                group_has_data = True
                # Для no_shift заданий дата неизвестна — используем TODAY как fallback
                t_date = TODAY
                agg[firm][crop][t_date] += cov
                firm_crop_fields[firm][crop].add(fid)
                if mid:
                    machine_day_area[firm][t_date][mid] += cov
                if group_name != PLOW_GROUP_NAME:
                    data[group_name]["area_by_firm_date"][firm][t_date] += cov
                    data[group_name]["area_by_firm_total"][firm] += cov

        if use_report17_area_logic:
            group_has_data = _flush_report17_area_data(
                data,
                group_name,
                machine_day_area,
                field_day_area,
                field_to_crop,
                firm_crop_fields,
                agg,
            )

        for firm in FIRM_ORDER:
            for t_date, by_mid in machine_day_area[firm].items():
                for mid, total_cov in by_mid.items():
                    if round(total_cov, 2) < count_threshold:
                        continue
                    grp = machine_group.get(mid, 0)
                    key = "hired" if grp == HIRED_GROUP_ID else "own"
                    data[group_name]["machines"][firm][t_date][key].add(mid)

        if group_name == PLOW_GROUP_NAME:
            # В 1-й сводке суточная вспашка считается только по агрегатам >= 10 га.
            data[group_name]["area_by_firm_date"] = {
                firm: defaultdict(
                    float,
                    {
                        t_date: _day_rate_area(by_mid, MIN_COUNT_AREA_HA)
                        for t_date, by_mid in machine_day_area[firm].items()
                        if _day_rate_area(by_mid, MIN_COUNT_AREA_HA) > 0
                    },
                )
                for firm in FIRM_ORDER
            }
            data[group_name]["area_by_firm_total"] = {
                firm: sum(
                    area
                    for t_date, area in data[group_name]["area_by_firm_date"][firm].items()
                    if t_date < TODAY
                )
                for firm in FIRM_ORDER
            }

        data[group_name]["machine_day_area"] = {
            firm: {dt: dict(by_mid) for dt, by_mid in machine_day_area[firm].items()}
            for firm in FIRM_ORDER
        }

        if not group_has_data:
            # Если из API по данной группе не подтянулось ни одной ненулевой величины,
            # то в отчёте оставались бы только “шаблонные” пустые секции.
            print(f"    Пусто по API (шаблонные значения), пропускаем группу")
            data.pop(group_name, None)
            continue

        for firm in FIRM_ORDER:
            data[group_name]["crops"][firm] = {}
            for crop, by_date in agg.get(firm, {}).items():
                fields_set = firm_crop_fields[firm][crop]
                area_ha    = sum(field_area.get(fid, 0) for fid in fields_set)
                if CUSTOM_SHIFT_DATE_MIN is not None and CUSTOM_SHIFT_DATE_MAX is not None:
                    total_ha = sum(
                        v
                        for d, v in by_date.items()
                        if CUSTOM_SHIFT_DATE_MIN <= d <= CUSTOM_SHIFT_DATE_MAX
                    )
                else:
                    total_ha = sum(v for d, v in by_date.items() if d < TODAY)
                data[group_name]["crops"][firm][crop] = {
                    "area_ha":  round(area_ha, 1),
                    "total_ha": round(total_ha, 1),
                    "by_date":  {d: round(v, 1) for d, v in by_date.items()},
                }

    return data


def dates_with_any_activity(work_data: dict) -> list[date]:
    """
    Дни в диапазоне [START_DATE, TODAY], где по любой группе работ есть данные:
    площадь по фирме за день, площадь по культуре за день или задействованная техника.
    """
    active: set[date] = set()
    for gd in work_data.values():
        area_by_firm  = gd.get("area_by_firm_date", {})
        crops_by_firm = gd.get("crops", {})
        machines_firm = gd.get("machines", {})
        for firm in FIRM_ORDER:
            for dt, v in area_by_firm.get(firm, {}).items():
                if isinstance(dt, date) and (v or 0) > 0:
                    active.add(dt)
            for _crop, cd in crops_by_firm.get(firm, {}).items():
                for dt, v in cd.get("by_date", {}).items():
                    if isinstance(dt, date) and (v or 0) > 0:
                        active.add(dt)
            for dt, bag in machines_firm.get(firm, {}).items():
                if isinstance(dt, date) and (bag.get("own") or bag.get("hired")):
                    active.add(dt)
    upper = TODAY - timedelta(days=1)
    return sorted(d for d in active if START_DATE <= d <= upper)


def _parse_int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, str(default)).strip())
        return max(lo, min(hi, v))
    except (ValueError, TypeError):
        return default


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "1" if default else "0").strip().lower()
    if raw in ("1", "true", "yes", "on", "да"):
        return True
    if raw in ("0", "false", "no", "off", "нет"):
        return False
    return default


def dates_with_activity_for_firm(gd: dict, firm: str) -> list[date]:
    """Дни, в которых у данной фирмы в этой группе работ есть данные (для расчёта суточной выработки)."""
    active: set[date] = set()
    for dt, v in gd.get("area_by_firm_date", {}).get(firm, {}).items():
        if isinstance(dt, date) and (v or 0) > 0:
            active.add(dt)
    for _crop, cd in gd.get("crops", {}).get(firm, {}).items():
        for dt, v in cd.get("by_date", {}).items():
            if isinstance(dt, date) and (v or 0) > 0:
                active.add(dt)
    for dt, bag in gd.get("machines", {}).get(firm, {}).items():
        if not isinstance(dt, date):
            continue
        if bag.get("own") or bag.get("hired"):
            active.add(dt)
    upper = TODAY - timedelta(days=1)
    return sorted(d for d in active if START_DATE <= d <= upper)


def compute_display_dates(work_data: dict) -> list[date]:
    """
    Список дат для колонок Excel: не больше max_n последних дней.
    Если only_nonempty: последние max_n дней среди дней с данными.
    Иначе: последние max_n календарных дней до даты отчёта (без неё), не раньше START_DATE.
    Excel за период: все сменные дни от даты начала до даты конца включительно.
    """
    if CUSTOM_SHIFT_DATE_MIN is not None and CUSTOM_SHIFT_DATE_MAX is not None:
        lower = max(CUSTOM_SHIFT_DATE_MIN, START_DATE)
        upper = CUSTOM_SHIFT_DATE_MAX
        out: list[date] = []
        d = lower
        while d <= upper:
            out.append(d)
            d += timedelta(days=1)
        period_cap = _parse_int_env("SVODKI_REPORT2_EXCEL_PERIOD_MAX_DAYS", 31, 1, 62)
        if len(out) > period_cap:
            out = out[-period_cap:]
        return out
    max_n = _parse_int_env("SVODKI_HK_COMPACT_MAX_DAYS", 5, 1, 366)
    only_nonempty = _parse_bool_env("SVODKI_HK_COMPACT_ONLY_NONEMPTY", True)
    if only_nonempty:
        cand = dates_with_any_activity(work_data)
        if len(cand) <= max_n:
            return cand
        return cand[-max_n:]
    out: list[date] = []
    d = TODAY - timedelta(days=1)
    while len(out) < max_n and d >= START_DATE:
        out.append(d)
        d -= timedelta(days=1)
    out.reverse()
    return out


# =============================================================================
# СТИЛИ (как в PDF «Сводка ХК Детальная — Мастер Свод»)
# =============================================================================

_RU_MONTHS_GEN = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _date_header_ru(d: date) -> str:
    return f"{d.day} {_RU_MONTHS_GEN[d.month]}"


def _report_date_label() -> str:
    """Сутки отчёта (последняя колонка): REPORT_DATE = TODAY, смена = TODAY − 1."""
    report_day = TODAY - timedelta(days=1)
    return f"Дата отчёта: {report_day.strftime('%d.%m.%Y')}"


def _solid_fill(hex6: str) -> PatternFill:
    """ARGB для Excel/LibreOffice (необходим префикс FF)."""
    h = hex6.upper().lstrip("#")
    if len(h) == 6:
        h = "FF" + h
    return PatternFill(fill_type="solid", start_color=h, end_color=h)


def make_styles(*, for_excel_period: bool = False) -> dict:
    """Палитра как в отчётах 3, 5–8: шапки 8093B3, итоги BDD7EE, данные белые."""
    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_size = 10 if for_excel_period else 8
    return {
        "border":       border,
        "align_c":      Alignment(horizontal="center", vertical="center", wrap_text=False),
        "align_hdr":    Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=True,
            shrink_to_fit=not for_excel_period,
        ),
        "align_l":      Alignment(vertical="center", wrap_text=False),
        "fill_title":   _solid_fill("FFFFFF"),
        "font_title":   Font(bold=True, color="FF111827", size=14, name="Arial"),
        "fill_grp":     _solid_fill("FFFFFF"),
        "font_grp":     Font(bold=True, size=10, name="Arial", color="FF111827"),
        "fill_season":  _solid_fill("F2F2F2"),
        "font_season":  Font(bold=True, size=10, name="Arial", color="FF000000"),
        "fill_hdr":     _solid_fill("8093B3"),
        "font_hdr":     Font(bold=True, color="FFFFFFFF", size=hdr_size, name="Arial"),
        "fill_firm":    _solid_fill("FFFFFF"),
        "font_firm":    Font(bold=True, size=9, name="Arial", color="FF000000"),
        "fill_data":    _solid_fill("FFFFFF"),
        "font_data":    Font(name="Arial", size=8, color="FF000000"),
        "fill_total":   _solid_fill("BDD7EE"),
        "font_total":   Font(bold=True, name="Arial", size=8, color="FF000000"),
        "fill_subhdr":  _solid_fill("F2F2F2"),
        "font_subhdr":  Font(bold=True, size=9, name="Arial", color="FF000000"),
    }


def _merge_row_bar(
    ws,
    row: int,
    c1: int,
    c2: int,
    text: str,
    s: dict,
    *,
    fill,
    font,
    align: Alignment,
) -> None:
    """
    LibreOffice корректно показывает объединённую шапку, если заливка и границы
    заданы для каждой ячейки диапазона до merge.
    """
    br = s["border"]
    for col in range(c1, c2 + 1):
        c = ws.cell(row=row, column=col)
        c.fill = fill
        c.border = br
    top = ws.cell(row=row, column=c1, value=text)
    top.font = font
    top.alignment = align
    if c2 > c1:
        ws.merge_cells(start_row=row, start_column=c1, end_row=row, end_column=c2)


def sc(cell, s, fill=None, font=None, align=None) -> None:
    cell.border = s["border"]
    if fill:  cell.fill = fill
    if font:  cell.font = font
    if align: cell.alignment = align
    # Единый формат чисел: разделение тысяч пробелом.
    if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
        if not cell.number_format or cell.number_format in {"General", "0"}:
            v = float(cell.value)
            if abs(v - round(v)) < 1e-9:
                cell.number_format = NF_INT
            elif abs(v - round(v, 1)) < 1e-9:
                cell.number_format = NF_DEC1
            else:
                cell.number_format = NF_DEC2

# =============================================================================
# ЗАПИСЬ ЛИСТА (только непустые группы)
# =============================================================================

def _apply_manual_page_breaks(ws, row_indices: list[int]) -> None:
    """Ручные горизонтальные разрывы: новая страница начинается с указанной строки."""
    from openpyxl.worksheet.pagebreak import Break

    seen: set[int] = set()
    for r in sorted(row_indices):
        r = int(r)
        if r <= 1 or r in seen:
            continue
        seen.add(r)
        ws.row_breaks.append(Break(id=r, min=0, max=16383, man=True))


def _make_unique_sheet_title(base: str, used_titles: set[str]) -> str:
    title = str(base or "").strip() or "Отчёт"
    title = title[:31]
    if title not in used_titles:
        return title
    idx = 2
    while True:
        suffix = f" ({idx})"
        candidate = f"{title[: max(1, 31 - len(suffix))]}{suffix}"
        if candidate not in used_titles:
            return candidate
        idx += 1


def _lock_row_heights(ws, max_row: int) -> None:
    """Уменьшаем высоту всех строк примерно на треть (для компактного PDF)."""
    for r in range(1, max_row + 1):
        rd = ws.row_dimensions[r]
        base_height = float(rd.height or 18)
        rd.height = round(base_height * 2 / 3, 1)


def _table_hdr_row_height(kind: str) -> float:
    """Высота строки шапки таблицы: multi — многострочные подписи, tall — блок 4."""
    if _excel_period_export():
        return {"multi": 44.0, "medium": 38.0, "tall": 50.0}[kind]
    return {"multi": 22.0, "medium": 20.0, "tall": 30.0}[kind]


def write_report_sheet(
    wb,
    work_data,          # теперь содержит только группы с данными
    display_dates,      # колонки по датам (последние N дней; не шире этих колонок)
    firm_plan_ha,
    firm_crop_area_ha,
    *,
    sheet_title_prefix: str = "",
):
    excel_period = _excel_period_export()
    s = make_styles(for_excel_period=excel_period)
    n_dates = len(display_dates)
    n_cols  = 6 + n_dates

    # Овощные группы идут после зерновых
    VEG_GROUPS = {"4.1 Посев (овощи)", "5.4 Уборка картофеля, лука",
                  "2.7 Гребнеобразование", "2.6 Фрезерование"}
    group_names = sorted(
        work_data.keys(),
        key=lambda g: (1 if g in VEG_GROUPS else 0, list(work_data.keys()).index(g))
    )

    # Группы, в которых показывается только одна фирма
    GROUP_FIRM_ONLY: dict[str, set[str]] = {
        "2.7 Гребнеобразование": {"АФ Чистопольская"},
        "2.6 Фрезерование":      {"АФ Чистопольская"},
        "4.1 Посев (овощи)":     {"АФ Чистопольская"},
    }
    # Группы, в которых показываются только определённые культуры
    _OVOSHI = {"Картофель", "Морковь", "Свекла столовая", "Капуста", "Лук", "Соя"}
    GROUP_CROP_ONLY: dict[str, set[str]] = {
        "2.7 Гребнеобразование": _OVOSHI,
        "2.6 Фрезерование":      _OVOSHI,
        "4.1 Посев (овощи)":     {"Картофель", "Морковь", "Свекла столовая", "Капуста", "Лук"},
    }

    used_titles: set[str] = set()
    for group_name in group_names:
        gd = work_data[group_name]
        sheet_title = _make_unique_sheet_title(
            f"{sheet_title_prefix}{group_name}".strip(), used_titles
        )
        used_titles.add(sheet_title)
        ws = wb.create_sheet(sheet_title)
        row = 1
        manual_break_rows: list[int] = []

        _merge_row_bar(
            ws, row, 1, n_cols, group_name, s,
            fill=s["fill_grp"],
            font=s["font_grp"],
            align=s["align_c"],
        )
        ws.row_dimensions[row].height = 18
        row += 1

        season_y = str(START_DATE.year)
        _merge_row_bar(
            ws, row, 1, 3, f"Сезон: {season_y}", s,
            fill=s["fill_season"],
            font=s["font_season"],
            align=s["align_l"],
        )
        _merge_row_bar(
            ws, row, 4, n_cols, _report_date_label(), s,
            fill=s["fill_season"],
            font=s["font_season"],
            align=s["align_l"],
        )
        ws.row_dimensions[row].height = 18
        row += 1

        # -------------------------------------------------------------
        # 1. ТАБЛИЦА ПО КУЛЬТУРАМ
        # -------------------------------------------------------------
        headers_crops = [
            "Агрофирма",
            "Культура",
            "Площадь культуры\nучётная, га",
            "План",
            "Выполнено работ\nв Га",
            "в %",
        ]
        for ci, col_name in enumerate(headers_crops, start=1):
            c = ws.cell(row=row, column=ci, value=col_name)
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        for di, d in enumerate(display_dates, start=len(headers_crops) + 1):
            c = ws.cell(row=row, column=di, value=_date_header_ru(d))
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        ws.row_dimensions[row].height = _table_hdr_row_height("multi")
        row += 1

        for firm in FIRM_ORDER:
            # Фильтр: только разрешённые фирмы
            if group_name in GROUP_FIRM_ONLY and firm not in GROUP_FIRM_ONLY[group_name]:
                continue
            crop_data = gd.get("crops", {}).get(firm, {})
            crop_area_ref = firm_crop_area_ha.get(firm, {})
            all_crops = sorted(
                set(list(crop_data.keys()) + list(crop_area_ref.keys())),
                key=lambda c: crop_sort_key_for_group(group_name, c),
            )
            # Фильтр: только разрешённые культуры
            if group_name in GROUP_CROP_ONLY:
                allowed = GROUP_CROP_ONLY[group_name]
                all_crops = [c for c in all_crops if c in allowed]
            all_crops = [c for c in all_crops if not is_crop_omitted(group_name, firm, c)]
            if not all_crops:
                continue

            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
            c = ws.cell(row=row, column=1, value=firm)
            sc(c, s, fill=s["fill_firm"], font=s["font_firm"], align=s["align_l"])
            row += 1

            for crop in all_crops:
                area_ha = round(crop_area_ref.get(crop, 0.0), 1)
                cd = crop_data.get(crop, {"total_ha": 0.0, "by_date": {}})
                total_ha = cd["total_ha"]
                _plan_raw = HARDCODED_PLAN_FLAT.get((group_name, firm, crop))
                plan_ha = float(_plan_raw) if _plan_raw is not None else 0.0
                pct = round(total_ha / plan_ha * 100, 1) if plan_ha else 0.0

                vals = [firm, crop, area_ha, plan_ha, total_ha, pct]
                for ci, val in enumerate(vals, start=1):
                    c = ws.cell(row=row, column=ci, value=val)
                    sc(
                        c,
                        s,
                        fill=s["fill_data"],
                        font=s["font_data"],
                        align=s["align_l"] if ci in (1, 2) else s["align_c"],
                    )
                for di, d in enumerate(display_dates, start=len(headers_crops) + 1):
                    val = cd["by_date"].get(d, 0.0)
                    c = ws.cell(row=row, column=di, value=val)
                    sc(c, s, fill=s["fill_data"], font=s["font_data"], align=s["align_c"])
                row += 1

        # Большая таблица закончилась → разрыв → на новой странице пустая строка, затем сводные.
        manual_break_rows.append(row)
        row += 1

        # -------------------------------------------------------------
        # 2. СОВОКУПНАЯ ПЛОЩАДЬ ПО АГРОФИРМАМ
        # -------------------------------------------------------------
        _merge_row_bar(
            ws, row, 1, n_cols, "СОВОКУПНАЯ ПЛОЩАДЬ ПО АГРОФИРМАМ", s,
            fill=s["fill_subhdr"],
            font=s["font_subhdr"],
            align=s["align_c"],
        )
        ws.row_dimensions[row].height = 18
        row += 1

        headers_svod = ["Агрофирма", "План, га", "Выполнено, га", "%"]
        for ci, col_name in enumerate(headers_svod, start=1):
            c = ws.cell(row=row, column=ci, value=col_name)
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        for di, d in enumerate(display_dates, start=len(headers_svod) + 1):
            c = ws.cell(row=row, column=di, value=_date_header_ru(d))
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        ws.row_dimensions[row].height = _table_hdr_row_height("medium")
        row += 1

        def _firm_plan_from_crops(gn: str, firm: str) -> float | None:
            """Сумма планов по всем культурам из HARDCODED_PLAN для данной группы и агрофирмы."""
            total = sum(
                v for (g, f, _crop), v in HARDCODED_PLAN_FLAT.items()
                if g == gn and f == firm
            )
            return float(total) if total else None

        area_digits = 2 if group_name == PLOW_GROUP_NAME else 1
        for firm in FIRM_ORDER:
            if group_name in GROUP_FIRM_ONLY and firm not in GROUP_FIRM_ONLY[group_name]:
                continue
            plan_ha = _firm_plan_from_crops(group_name, firm) or 0.0
            area_by_firm_date = gd.get("area_by_firm_date", {}).get(firm, {})
            total_ha = round(gd.get("area_by_firm_total", {}).get(firm, 0.0), area_digits)
            pct = round(total_ha / plan_ha * 100, 1) if plan_ha else 0.0

            vals = [firm, plan_ha, total_ha, pct]
            for ci, val in enumerate(vals, start=1):
                c = ws.cell(row=row, column=ci, value=val)
                al = s["align_l"] if ci == 1 else s["align_c"]
                sc(c, s, fill=s["fill_data"], font=s["font_data"], align=al)
            for di, d in enumerate(display_dates, start=len(headers_svod) + 1):
                val = round(area_by_firm_date.get(d, 0.0), area_digits)
                c = ws.cell(row=row, column=di, value=val)
                sc(c, s, fill=s["fill_data"], font=s["font_data"], align=s["align_c"])
            row += 1
        # Итого
        _firms_svod = [f for f in FIRM_ORDER
                       if group_name not in GROUP_FIRM_ONLY or f in GROUP_FIRM_ONLY[group_name]]
        _plans = [(_firm_plan_from_crops(group_name, f) or 0.0) for f in _firms_svod]
        total_plan = round(sum(_plans), 1)
        total_done   = round(sum(gd.get("area_by_firm_total", {}).get(f, 0.0) for f in _firms_svod), area_digits)
        total_pct  = round(total_done / total_plan * 100, 1) if total_plan else 0.0
        itogo_svod   = ["Итого", total_plan, total_done, total_pct]
        for ci, val in enumerate(itogo_svod, start=1):
            c = ws.cell(row=row, column=ci, value=val)
            al = s["align_l"] if ci == 1 else s["align_c"]
            sc(c, s, fill=s["fill_total"], font=s["font_total"], align=al)
        for di, d in enumerate(display_dates, start=len(headers_svod) + 1):
            val = round(sum(gd.get("area_by_firm_date", {}).get(f, {}).get(d, 0.0) for f in _firms_svod), area_digits)
            c = ws.cell(row=row, column=di, value=val)
            sc(c, s, fill=s["fill_total"], font=s["font_total"], align=s["align_c"])
        row += 1
        row += 1

        # Малые сводные таблицы держим вместе, чтобы они не растягивались
        # отдельными почти пустыми страницами в PDF.
        row += 1

        # -------------------------------------------------------------
        # 3. КОЛИЧЕСТВО ЗАДЕЙСТВОВАННЫХ АГРЕГАТОВ
        # -------------------------------------------------------------
        _merge_row_bar(
            ws, row, 1, n_cols, "КОЛИЧЕСТВО ЗАДЕЙСТВОВАННЫХ АГРЕГАТОВ", s,
            fill=s["fill_subhdr"],
            font=s["font_subhdr"],
            align=s["align_c"],
        )
        ws.row_dimensions[row].height = 18
        row += 1

        headers_agg = ["Агрофирма", "Собственных\nагрегатов", "Наёмных\nза тек. день"]
        for ci, col_name in enumerate(headers_agg, start=1):
            c = ws.cell(row=row, column=ci, value=col_name)
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        for di, d in enumerate(display_dates, start=len(headers_agg) + 1):
            c = ws.cell(row=row, column=di, value=_date_header_ru(d))
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        ws.row_dimensions[row].height = _table_hdr_row_height("medium")
        row += 1

        today_date = display_dates[-1] if display_dates else None
        itogo_own   = 0
        itogo_hired = 0
        itogo_daily: dict[date, int] = defaultdict(int)

        for firm in FIRM_ORDER:
            if group_name in GROUP_FIRM_ONLY and firm not in GROUP_FIRM_ONLY[group_name]:
                continue
            machines = gd.get("machines", {}).get(
                firm, defaultdict(lambda: {"own": set(), "hired": set()})
            )
            own_count   = AVAILABLE_OWN.get(group_name, OWN_AGGREGATES).get(firm, 0)
            # Наёмных за текущий день (последняя дата в display_dates)
            hired_today = len(machines[today_date]["hired"]) if today_date else 0
            daily_counts: dict = defaultdict(lambda: {"own": 0, "hired": 0})
            for dt, v in machines.items():
                daily_counts[dt]["own"]   = len(v["own"])
                daily_counts[dt]["hired"] = len(v["hired"])
            itogo_own   += own_count
            itogo_hired += hired_today
            for d in display_dates:
                dc = daily_counts.get(d, {"own": 0, "hired": 0})
                itogo_daily[d] += dc["own"] + dc["hired"]
            vals = [firm, own_count, hired_today]
            for ci, val in enumerate(vals, start=1):
                c = ws.cell(row=row, column=ci, value=val)
                al = s["align_l"] if ci == 1 else s["align_c"]
                sc(c, s, fill=s["fill_data"], font=s["font_data"], align=al)
            for di, d in enumerate(display_dates, start=len(headers_agg) + 1):
                dc = daily_counts.get(d, {"own": 0, "hired": 0})
                total_agg = dc["own"] + dc["hired"]
                c = ws.cell(row=row, column=di, value=total_agg)
                sc(c, s, fill=s["fill_data"], font=s["font_data"], align=s["align_c"])
            row += 1
        # Итого
        for ci, val in enumerate(["Итого", itogo_own, itogo_hired], start=1):
            c = ws.cell(row=row, column=ci, value=val)
            al = s["align_l"] if ci == 1 else s["align_c"]
            sc(c, s, fill=s["fill_total"], font=s["font_total"], align=al)
        for di, d in enumerate(display_dates, start=len(headers_agg) + 1):
            c = ws.cell(row=row, column=di, value=itogo_daily.get(d, 0))
            sc(c, s, fill=s["fill_total"], font=s["font_total"], align=s["align_c"])
        row += 1
        row += 1

        # Следующий малый блок продолжается ниже на той же странице.
        row += 1

        # -------------------------------------------------------------
        # 4. СУТОЧНАЯ ВЫРАБОТКА НА 1 АГРЕГАТ (те же заливки/границы, что у блоков 2–3)
        # -------------------------------------------------------------
        _merge_row_bar(
            ws, row, 1, n_cols, "СУТОЧНАЯ ВЫРАБОТКА НА 1 АГРЕГАТ", s,
            fill=s["fill_subhdr"],
            font=s["font_subhdr"],
            align=s["align_c"],
        )
        ws.row_dimensions[row].height = 18
        row += 1

        headers_rate = [
            "Агрофирма",
            "Плановая\nвыработка, га/сутки",
            "Средняя выработка\nпо сезону, га/сутки",
        ]
        hdr_row = row
        for ci, col_name in enumerate(headers_rate, start=1):
            c = ws.cell(row=row, column=ci, value=col_name)
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        for di, d in enumerate(display_dates, start=len(headers_rate) + 1):
            c = ws.cell(row=row, column=di, value=_date_header_ru(d))
            sc(c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_hdr"])
        ws.row_dimensions[hdr_row].height = _table_hdr_row_height("tall")
        row += 1

        plan_rate = PLAN_RATE_PER_GROUP.get(group_name, 0.0)
        count_threshold = _group_count_threshold(group_name)
        area_threshold = MIN_COUNT_AREA_HA if group_name == PLOW_GROUP_NAME else MIN_DETAIL_AREA_HA
        season_count_threshold = MIN_COUNT_AREA_HA if group_name == PLOW_GROUP_NAME else MIN_DETAIL_AREA_HA
        mda_all = gd.get("machine_day_area", {})
        _firms_rate = [f for f in FIRM_ORDER
                       if group_name not in GROUP_FIRM_ONLY or f in GROUP_FIRM_ONLY[group_name]]
        for firm in _firms_rate:
            mda_firm = mda_all.get(firm, {})
            stats_dates = dates_with_activity_for_firm(gd, firm)
            season_sum, machine_days = _season_stats_for_firm(
                mda_firm,
                stats_dates,
                area_threshold,
                season_count_threshold,
            )
            avg_season = round(season_sum / machine_days, 1) if machine_days else 0.0

            vals = [firm, plan_rate, avg_season]
            for ci, val in enumerate(vals, start=1):
                c = ws.cell(row=row, column=ci, value=val)
                al = s["align_l"] if ci == 1 else s["align_c"]
                sc(c, s, fill=s["fill_data"], font=s["font_data"], align=al)
            for di, dt in enumerate(display_dates, start=len(headers_rate) + 1):
                by_mid = mda_firm.get(dt, {})
                area = _day_rate_area(by_mid, area_threshold)
                agg_count = _day_rate_machines(by_mid, count_threshold)
                prod = round(area / agg_count, 2) if agg_count > 0 else 0
                c = ws.cell(row=row, column=di, value=prod)
                sc(c, s, fill=s["fill_data"], font=s["font_data"], align=s["align_c"])
            row += 1
        # Итого: season_sum всех фирм / machine_days всех фирм (как в сводке 5)
        total_season_sum = 0.0
        total_machine_days = 0
        for f in _firms_rate:
            ss, md = _season_stats_for_firm(
                mda_all.get(f, {}),
                dates_with_activity_for_firm(gd, f),
                area_threshold,
                season_count_threshold,
            )
            total_season_sum += ss
            total_machine_days += md
        avg_season_total = round(total_season_sum / total_machine_days, 1) if total_machine_days else 0.0
        for ci, val in enumerate(["Итого", plan_rate, avg_season_total], start=1):
            c = ws.cell(row=row, column=ci, value=val)
            al = s["align_l"] if ci == 1 else s["align_c"]
            sc(c, s, fill=s["fill_total"], font=s["font_total"], align=al)
        for di, dt in enumerate(display_dates, start=len(headers_rate) + 1):
            total_area = sum(
                _day_rate_area(mda_all.get(f, {}).get(dt, {}), area_threshold)
                for f in _firms_rate
            )
            total_agg = sum(
                _day_rate_machines(mda_all.get(f, {}).get(dt, {}), count_threshold)
                for f in _firms_rate
            )
            prod = round(total_area / total_agg, 2) if total_agg > 0 else 0
            c = ws.cell(row=row, column=di, value=prod)
            sc(c, s, fill=s["fill_total"], font=s["font_total"], align=s["align_c"])
        row += 1
        # Ширина колонок
        # Поджимаем "Агрофирма" для более компактной пропорции шапки.
        # Колонки: Агрофирма, Культура, Площадь культуры, План, Выполнено, %
        widths = [20, 26, 26, 11, 18, 10] if excel_period else [18, 24, 20, 9, 15, 9]
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        date_col_w = 14 if excel_period else 12
        for i in range(7, 7 + n_dates):
            ws.column_dimensions[get_column_letter(i)].width = date_col_w

        if not excel_period:
            _lock_row_heights(ws, row)
        _apply_manual_page_breaks(ws, manual_break_rows)


def build_report() -> None:
    _apply_manual_plan_rates()
    print(f"Отчёт: выполнение работ по культурам")
    if CUSTOM_WINDOW_START is not None and CUSTOM_WINDOW_END is not None:
        print(
            f"Произвольное окно: {CUSTOM_WINDOW_START.strftime('%d.%m.%Y %H:%M')} — "
            f"{CUSTOM_WINDOW_END.strftime('%d.%m.%Y %H:%M')}"
        )
        print(
            f"Сменные дни в отчёте: {CUSTOM_SHIFT_DATE_MIN.strftime('%d.%m.%Y')} — "
            f"{CUSTOM_SHIFT_DATE_MAX.strftime('%d.%m.%Y')}"
        )
    print(f"Период: {START_DATE} — {TODAY}\n")

    headers = {"X-User-Api-Token": API_TOKEN}

    print("Загружаем справочники...")
    (field_to_crop, field_to_firm, field_to_firm_all,
     field_area, machine_group,
     firm_plan_ha, firm_crop_area_ha) = load_reference_data()

    print(f"\nПлан по агрофирмам (tillable_area активных полей):")
    for firm, ha in firm_plan_ha.items():
        print(f"  {firm}: {round(ha):,} га")

    print("\nСобираем данные по работам...")
    work_data = collect_work_data(
        field_to_crop, field_to_firm, field_to_firm_all,
        field_area, machine_group, headers
    )

    if not _excel_period_export():
        try:
            from r23_from_report2 import export_r23_bundle_after_report2

            if os.environ.get("SVODKI_R23_WORK_DATA_BUNDLE_OUT"):
                machines_all = get_all_from_id("machines")
                subtype_by_id = {
                    int(m["id"]): (m.get("machine_subtype") or "—")
                    for m in machines_all
                    if m.get("id") is not None
                }
                export_r23_bundle_after_report2(
                    work_data,
                    report_stamp=TODAY,
                    firm_order=FIRM_ORDER,
                    subtype_by_id=subtype_by_id,
                )
        except Exception as exc:
            print(f"  [r23←2] Не удалось записать снимок для сводки 23: {exc}")

    n_full = (TODAY - START_DATE).days + 1
    max_n = _parse_int_env("SVODKI_HK_COMPACT_MAX_DAYS", 5, 1, 366)
    only_n = _parse_bool_env("SVODKI_HK_COMPACT_ONLY_NONEMPTY", True)
    with_data_days = len(dates_with_any_activity(work_data))
    display_dates = compute_display_dates(work_data)
    print(
        f"\nДней в календарном периоде: {n_full}; дней с данными (всего): {with_data_days}; "
        f"макс. колонок с датами: {max_n}; только непустые дни: {only_n}; "
        f"выведено колонок: {len(display_dates)}"
    )

    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    print("Пишем листы по операциям...")
    write_report_sheet(wb, work_data, display_dates, firm_plan_ha, firm_crop_area_ha)

    wb.save(OUTPUT_FILE)
    print(f"\nСохранено: {OUTPUT_FILE}")

if __name__ == "__main__":
    build_report()