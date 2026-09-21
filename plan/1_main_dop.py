"""
Cropwise Operations — отчёт "Яровой сев (сводка по культурам)"
===============================================================
Формирует Excel с отдельным листом на каждую агрофирму.
Колонки: №, Культура, План га, Факт общий га, Остаток, Доля %, Факт сутки, Прогнозная дата.
Данные: covered_area из machine_tasks (виды посева ALL_WT_IDS).
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
import requests
from plans_1 import (
    API_TOKEN,
    BASE_URL,
    TZ_MSK,
    FIRM_TO_FOLDER,
    FOLDER_TO_FIRM,
    FIRM_TO_REGION,
    FIELD_NAME_FORCE_RULES,
    WORK_GROUPS,
    SEEDING_CROP_ORDER,
    PLAN_SEEDING_HA,
    VEG_CROP_ROWS,
    VEG_WT_FALLBACK_CROP,
    HARDCODED_PLAN,
    SKIP_CROPS as _SKIP_CROPS,
    build_field_to_crop,
)
from season_config import cropwise_season_year
try:
    from excel_number_fmt import NF_DEC2, NF_INT
except ImportError:
    NF_DEC2 = "# ##0.00"
    NF_INT  = "# ##0"

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

REPORT_DATE = date.today()
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])
SEASON_YEAR = cropwise_season_year(fallback=REPORT_DATE.year)

DAY_START_HOUR: int = 6
DAY_END_HOUR:   int = 6
# TZ_MSK импортируется из plans

SEMAPHORE_LIMIT = 40

# название → region_id (используется только для порядка и как ключ)
AGROFIRMS: dict[str, int] = FIRM_TO_REGION

# FIRM_TO_FOLDER, FOLDER_TO_FIRM — импортируются из plans
# FIELD_NAME_FORCE_RULES — импортируется из plans

# Порядок культур на листе (ранжирование) — импортируется из plans как SEEDING_CROP_ORDER
CROP_ORDER: tuple[str, ...] = SEEDING_CROP_ORDER

# Полный перечень культур посева (как в сводке 5 / plans.py).
# В «Факт общий, га» учитываются только эти культуры; многолетние травы, пар и пр.
# из добора history_items не попадают в таблицу посева.
FULL_SEEDING_CROP_ORDER: tuple[str, ...] = (
    "Рапс яровой",
    "Рапс озимый",
    "Подсолнечник",
    "Соя",
    "Пшеница яровая",
    "Пшеница озимая",
    "Тритикале озимая",
    "Кукуруза на зерно",
    "Кукуруза на силос",
)
# Отображение посева — SEEDING_CROP_ORDER из plans_1 (озимый сев + тритикале).
_FULL_SEEDING_CROP_SET: frozenset[str] = frozenset(FULL_SEEDING_CROP_ORDER)
# Культуры, которые реально выводятся в «Итого (все АФ)» / ЯрСев.
_DISPLAY_SEEDING_CROP_SET: frozenset[str] = frozenset(CROP_ORDER)
# Fallback для суточного факта без культуры в sowing-карте (озимый сев).
_DEFAULT_SEEDING_CROP = "Рапс озимый"
# Тритикале только у НурлатАгро — не дублировать пустую строку в таблицах остальных АФ.
_TRITICALE_CROP = "Тритикале озимая"
_TRITICALE_FIRMS: frozenset[str] = frozenset({"НурлатАгро"})

# Овощные культуры — отдельная таблица (только АФ Чистопольская)
# VEG_CROP_ROWS, VEG_CROP_PLAN_HA — импортируются из plans

# Культуры для каждой агрофирмы
FIRM_CROPS: dict[str, tuple[str, ...]] = {
    firm: (
        CROP_ORDER
        if firm in _TRITICALE_FIRMS
        else tuple(c for c in CROP_ORDER if c != _TRITICALE_CROP)
    )
    for firm in AGROFIRMS
}

# Статичный план, га — импортируется из plans как PLAN_SEEDING_HA
PLAN_HA: dict[str, dict[str, float | None]] = PLAN_SEEDING_HA

_SEED_KEY = "4.0 Посев"
_VEG_KEY  = "4.1 Посев (овощи)"

_VEG_CROP_NAME_MAP: dict[str, str] = {"Свекла столовая": "Свекла"}
VEG_CROP_PLAN_HA: dict[str, float] = {}
for _firm_crops in HARDCODED_PLAN.get(_VEG_KEY, {}).values():
    for _crop, _val in _firm_crops.items():
        _key = _VEG_CROP_NAME_MAP.get(_crop, _crop)
        VEG_CROP_PLAN_HA[_key] = VEG_CROP_PLAN_HA.get(_key, 0.0) + float(_val or 0.0)

ALL_WT_IDS: set[int] = {
    wt_id
    for group_name, wt_ids in WORK_GROUPS
    if group_name == _SEED_KEY
    for wt_id in wt_ids
}
VEG_WT_IDS: set[int] = {
    wt_id
    for group_name, wt_ids in WORK_GROUPS
    if group_name == _VEG_KEY
    for wt_id in wt_ids
}
# =============================================================================
# НОРМАЛИЗАЦИЯ НАЗВАНИЙ КУЛЬТУР
# =============================================================================

def _normalize(s: str) -> str:
    return str(s or "").strip().lower().replace("ё", "е")


_CROP_MAP: dict[str, str] = {
    "рапс яровой":           "Рапс яровой",
    "рапс яр":               "Рапс яровой",
    "рапс озимый":           "Рапс озимый",
    "рапс оз":               "Рапс озимый",
    "подсолнечник":          "Подсолнечник",
    "соя":                   "Соя",
    "пшеница яровая":        "Пшеница яровая",
    "пшеница яр":            "Пшеница яровая",
    "пшеница озимая":        "Пшеница озимая",
    "пшеница оз":            "Пшеница озимая",
    "тритикале озимая":      "Тритикале озимая",
    "тритикале оз":          "Тритикале озимая",
    "тритикале":             "Тритикале озимая",
    "кукуруза на зерно":     "Кукуруза на зерно",
    "кукуруза зерно":        "Кукуруза на зерно",
    "кукуруза на силос":     "Кукуруза на силос",
    "кукуруза силос":        "Кукуруза на силос",
}

def _map_crop(raw: str) -> str | None:
    n = _normalize(raw)
    if not n:
        return None
    # Точное совпадение
    if n in _CROP_MAP:
        return _CROP_MAP[n]
    # Частичное совпадение
    for key, val in _CROP_MAP.items():
        if key in n or n in key:
            return val
    return None


def _crop_label_for_stats(raw: str) -> str:
    """Нормализованное имя культуры для накопления факта (как в сводке 5)."""
    return _map_crop(raw) or str(raw or "").strip()


def _is_main_seeding_crop(crop: str) -> bool:
    """Культура из основного перечня ярового/озимого посева."""
    label = _crop_label_for_stats(crop)
    return bool(label) and label in _FULL_SEEDING_CROP_SET


def _crop_for_seeding_fact(raw: str, *, for_today: bool) -> str | None:
    """
    Культура для строк посева.

    Сезон / сутки: культуры из FULL_SEEDING_CROP_ORDER (в т.ч. яровой рапс).
    Сутки без культуры в sowing-карте — fallback на озимый рапс (как сводка 2).
    """
    label = _crop_label_for_stats(raw)
    if label in _FULL_SEEDING_CROP_SET:
        return label
    if for_today:
        return _DEFAULT_SEEDING_CROP
    return None


def _apply_seeding_today_from_perf(
    firm_stats: dict[str, dict],
    seeding_day_stats: dict[str, dict] | None,
) -> None:
    """
    Подставляет «Факт за сутки» из листа «Выраб. — 4.0 Посев»,
    чтобы итоги совпадали байт-в-байт (одна и та же суточная площадь).

    Важно (озимый сев): area_by_crop в сводке 1 строится из общего field_to_crop
    (agro_operations / year_now) и часто даёт «Рапс яровой» на тех же полях, где
    озимый оверлей уже дал «Пшеница озимая». Если затереть today такой разбивкой,
    строки озимых в таблице показывают 0 (яровые в CROP_ORDER не выводятся).
    Поэтому берём только культуры из _DISPLAY_SEEDING_CROP_SET; если их нет —
    оставляем today из mtfmi (как сводка 5 без area_by_crop).
    """
    if not seeding_day_stats:
        return
    for firm in AGROFIRMS:
        st = seeding_day_stats.get(firm) or {}
        by_crop_raw = st.get("area_by_crop") or {}
        # Без разбивки по культурам не затираем today_by_crop из mtfmi —
        # иначе весь суточный факт уходит в «Рапс озимый» (см. 5_main_fixed).
        if not by_crop_raw:
            continue
        display: dict[str, float] = defaultdict(float)
        for crop, val in by_crop_raw.items():
            v = float(val or 0.0)
            if v <= 0:
                continue
            label = _crop_label_for_stats(crop)
            if label in _DISPLAY_SEEDING_CROP_SET:
                display[label] += v
        # Яровые/прочие культуры из Выраб. не должны обнулять озимый today (mtfmi).
        if not display:
            continue
        bucket = firm_stats.setdefault(firm, {"fact_by_crop": {}, "today_by_crop": {}})
        bucket["today_by_crop"] = {k: round(v, 2) for k, v in display.items()}


def _sum_crop_from_map(crop_map: dict[str, float], crop: str) -> float:
    target = _crop_label_for_stats(crop)
    return sum(
        float(val or 0.0)
        for name, val in (crop_map or {}).items()
        if _crop_label_for_stats(name) == target
    )


def _sum_crop_across_firms(
    firm_stats: dict[str, dict],
    crop: str,
    key: str = "fact_by_crop",
) -> float:
    return sum(
        _sum_crop_from_map(firm_stats.get(firm, {}).get(key, {}), crop)
        for firm in AGROFIRMS
    )

# =============================================================================
# ВЫЧИСЛЕНИЕ ВРЕМЕННОГО ДИАПАЗОНА
# =============================================================================

def compute_time_range(report_date: date) -> tuple[datetime, datetime]:
    if DAY_START_HOUR == DAY_END_HOUR:
        day_start = datetime(
            report_date.year, report_date.month, report_date.day,
            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
        ) - timedelta(days=1)
        cutoff = datetime(
            report_date.year, report_date.month, report_date.day,
            DAY_END_HOUR, 0, 0, tzinfo=TZ_MSK,
        )
    else:
        day_start = datetime(
            report_date.year, report_date.month, report_date.day,
            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
        )
        cutoff = datetime(
            report_date.year, report_date.month, report_date.day,
            DAY_END_HOUR, 0, 0, tzinfo=TZ_MSK,
        )
    return day_start, cutoff

# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def parse_dt(s) -> "datetime | None":
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
# HTTP-ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


def get_all_from_id(endpoint: str, extra_params: dict | None = None) -> list[dict]:
    params  = dict(extra_params or {})
    results = []
    from_id = 0
    while True:
        r = session.get(
            f"{BASE_URL}/{endpoint}",
            params={**params, "from_id": from_id, "limit": 1000},
        )
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


async def fetch_all_pages(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    items   = []
    from_id = 0
    while True:
        async with semaphore:
            r     = await client.get(url, params={**params, "from_id": from_id, "limit": 1000})
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


# =============================================================================
# СПРАВОЧНИКИ: поля → культура, поля → агрофирма
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
        pid = (
            gf.get("parent_id")
            or gf.get("parent_group_folder_id")
            or gf.get("group_folder_parent_id")
        )
        parent[int(fid)] = int(pid) if pid is not None else None
    return parent


def _group_folder_name_firm_hints(group_folders: list[dict]) -> dict[int, str]:
    hints: dict[int, str] = {}
    for gf in group_folders:
        fid = gf.get("id")
        if fid is None:
            continue
        blob = _norm_field_label(
            f"{gf.get('name') or ''} {gf.get('title') or ''}"
        )
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


def _folder_id_chain(
    folder_id: int | None, parent_map: dict[int, int | None]
) -> list[int]:
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


def load_field_references(
    report_date: date = REPORT_DATE,
) -> tuple[dict[int, str], dict[int, str], set[int], dict[int, str]]:
    """
    Возвращает:
      field_to_crop:      field_id → название культуры (только из history_items, active=True,
                          без fallback на crop_id из поля — как в 2_main_ideal_compact)
      field_to_firm:      field_id → название агрофирмы (через иерархию group_folders)
      available_field_ids: field_id — поля с available=True хотя бы в один день периода
    """
    print("  Загружаем fields / field_groups / group_folders...")
    fields        = get_all_from_id("fields")
    fgs_all       = get_all_from_id("field_groups")
    gfs_all       = get_all_from_id("group_folders")
    print("  Загружаем crops...")
    crops         = get_all_from_id("crops")
    # Озимый сев: маппинг полей как в сводке 5 (history 2026 + 2027).
    years_to_load: list[int] = []
    for yr in (SEASON_YEAR + 1, SEASON_YEAR):
        if yr not in years_to_load:
            years_to_load.append(yr)

    crop_map: dict[int, str] = {
        int(c["id"]): str(c.get("name") or c.get("title") or "").strip()
        for c in crops if c.get("id")
    }

    all_history_items: list[dict] = []
    for crop_year in years_to_load:
        print(f"  Загружаем history_items year={crop_year}...")
        chunk = get_all_from_id("history_items", {"year": str(crop_year)})
        all_history_items.extend(chunk)
        print(f"    history_items year={crop_year}: {len(chunk)} записей")

    field_to_crop = build_field_to_crop(
        crop_map=crop_map,
        history_items=all_history_items,
        report_date=report_date,
        period_start=date(SEASON_YEAR, 1, 1),
        skip_crops=_SKIP_CROPS,
        season_year=SEASON_YEAR + 1,
    )
    print(f"    {len(field_to_crop)} полей с культурой (sowing_date + оверлей рапса)")

    # Fallback на crop_id/crop_name напрямую из поля намеренно убран:
    # он включал бы поля без активной записи в сезоне и давал расхождение с эталоном.

    # ── available_field_ids: поля active=True в периоде [1.01, REPORT_DATE] ──
    print("  Загружаем inventory_history_items (фильтр available)...")
    ihi_all = get_all_from_id("inventory_history_items", {"historyable_type": "Field"})
    period_start = date(SEASON_YEAR, 1, 1)
    period_end   = report_date
    available_field_ids: set[int] = set()
    for r in ihi_all:
        if not r.get("available"):
            continue
        fid_r = r.get("historyable_id")
        if fid_r is None:
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
            available_field_ids.add(int(fid_r))
    print(f"  Полей available=True в периоде: {len(available_field_ids)}")

    # ── агрофирмы: через иерархию group_folders ───────────────────────────────
    fgs_map    = {fg["id"]: fg for fg in fgs_all}
    parent_map = _folder_parent_map(gfs_all)
    root_ids   = set(FIRM_TO_FOLDER.values())
    gf_hints   = _group_folder_name_firm_hints(gfs_all)

    field_to_firm: dict[int, str] = {}

    for f in fields:
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
    for f in fields:
        fid = f.get("id")
        if fid is None:
            continue
        fk = _firm_from_field_name(f)
        if fk:
            field_to_firm[int(fid)] = fk
            forced += 1

    field_to_firm_all = dict(field_to_firm)
    field_to_firm = {
        fid: firm for fid, firm in field_to_firm.items()
        if fid in field_to_crop and fid in available_field_ids
    }
    print(
        f"  Полей с культурой: {len(field_to_crop)}, "
        f"с агрофирмой (после фильтра): {len(field_to_firm)}, "
        f"всего с привязкой к АФ: {len(field_to_firm_all)} "
        f"(принудительно по имени: {forced})"
    )
    return field_to_crop, field_to_firm, available_field_ids, field_to_firm_all


# =============================================================================
# АСИНХРОННАЯ ЗАГРУЗКА MTFMI (как в 2_main_ideal_compact.py)
# =============================================================================

MTFMI_BATCH_SIZE = 300  # сколько machine_task_id класть в один запрос mtfmi


async def fetch_mtfmi_for_tasks(
    task_ids: list[int],
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
) -> dict[int, list[dict]]:
    """
    Забирает mtfmi для списка task_ids пачками по MTFMI_BATCH_SIZE
    (endpoint поддерживает фильтр по списку значений machine_task_id),
    вместо одного запроса на каждую задачу. При сбое батча — повтор поштучно.
    """
    url = f"{BASE_URL}/machine_task_field_mapping_items"
    result: dict[int, list[dict]] = {tid: [] for tid in task_ids}
    if not task_ids:
        return result

    async def _batch(batch: list[int]) -> list[dict]:
        return await fetch_all_pages(
            client, url,
            {"machine_task_id": ",".join(str(tid) for tid in batch)},
            semaphore,
        )

    async def _one(tid: int) -> list[dict]:
        return await fetch_all_pages(client, url, {"machine_task_id": tid}, semaphore)

    batches = [task_ids[i:i + MTFMI_BATCH_SIZE] for i in range(0, len(task_ids), MTFMI_BATCH_SIZE)]
    batch_results = await asyncio.gather(*[_batch(b) for b in batches], return_exceptions=True)

    failed_ids: list[int] = []
    for batch, res in zip(batches, batch_results):
        if isinstance(res, BaseException):
            failed_ids.extend(batch)
            continue
        for item in res:
            tid = item.get("machine_task_id")
            if tid is not None and int(tid) in result:
                result[int(tid)].append(item)

    if failed_ids:
        print(f"    Повторяем поштучно {len(failed_ids)} задач (батч mtfmi не удался)...")
        retry_results = await asyncio.gather(*[_one(tid) for tid in failed_ids], return_exceptions=True)
        for tid, res in zip(failed_ids, retry_results):
            if not isinstance(res, BaseException):
                result[tid] = res

    return result


# =============================================================================
# ПОДСЧЁТ ФАКТА: machine_tasks → mtfmi → field_to_crop/firm
# Логика "Факт общий" совпадает с area_by_firm_total из 2_main_ideal_compact.py:
#   - covered_area_hourly суммируется до cutoff смены; порог cov > 0.5
#   - fallback на covered_area только если hourly список пуст (не if cov <= 0)
#   - задания без end_time (no_shift) обрабатываются отдельно через covered_area
# =============================================================================

async def fetch_all_crop_stats(
    client:    httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    day_start: datetime,
    cutoff:    datetime,
    season_start: datetime,
    field_to_crop: dict[int, str],
    field_to_firm: dict[int, str],
    available_field_ids: set[int] | None = None,
    report_date: date = REPORT_DATE,
    field_to_firm_all: dict[int, str] | None = None,
) -> dict[str, dict]:
    """
    Загружает все machine_tasks по ALL_WT_IDS за сезон,
    затем через mtfmi получает covered_area по полю,
    определяет культуру (field_to_crop) и агрофирму.

    Фирма — как FACT_BY_ALL_FIELDS в сводке 1/2: из полной карты полей
    (field_to_firm_all), без фильтра available/обязательной культуры.
    Иначе суточный факт расходится с «Выраб. — 4.0 Посев».

    Возвращает dict: firm_name → {fact_by_crop, today_by_crop}
    """
    firm_by_field = field_to_firm_all if field_to_firm_all is not None else field_to_firm
    url              = f"{BASE_URL}/machine_tasks"
    season_start_str = season_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str       = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    print("  Загружаем machine_tasks за сезон по всем work_type_id...")
    chunks = await asyncio.gather(*[
        fetch_all_pages(client, url, {
            "work_type_id":   wt,
            "end_time_gt_eq": season_start_str,
            "end_time_lt":    cutoff_str,
        }, semaphore)
        for wt in sorted(ALL_WT_IDS)
    ])

    # Дедупликация по id
    all_tasks: list[dict] = []
    seen_ids: set[int] = set()
    for chunk in chunks:
        for t in chunk:
            tid = t.get("id")
            if tid and int(tid) not in seen_ids:
                seen_ids.add(int(tid))
                all_tasks.append(t)
    print(f"  Всего уникальных заданий: {len(all_tasks)}")

    season_task_year_str = str(SEASON_YEAR + 1)
    from collections import Counter
    season_counts = Counter(
        str(t.get("season") or t.get("year") or "<пусто>").strip() or "<пусто>"
        for t in all_tasks
    )
    print(
        f"  Сезон заданий (нужен {season_task_year_str}): {dict(season_counts)}"
    )
    all_tasks = [
        t for t in all_tasks
        if str(t.get("season") or t.get("year") or "").strip() == season_task_year_str
    ]
    print(f"  После фильтра season=={season_task_year_str}: {len(all_tasks)} заданий")

    if not all_tasks:
        empty = {"fact_by_crop": {}, "today_by_crop": {}}
        return {firm: dict(empty) for firm in AGROFIRMS}

    # Разбиваем задания на:
    #   filtered_tasks  — с известной сменой (есть end_time и дата в пределах сезона)
    #   no_shift_tasks  — без end_time; площадь берётся через covered_area напрямую
    task_shift: dict[int, tuple[datetime, datetime]] = {}  # tid → (shift_start, shift_cutoff)
    filtered_tasks: list[dict] = []
    no_shift_tasks: list[dict] = []
    today_task_ids: set[int]   = set()

    for t in all_tasks:
        tid    = int(t["id"])
        end_dt = parse_dt(t.get("end_time"))

        if end_dt is None:
            no_shift_tasks.append(t)
            continue

        # Граница смены: смена дня D = [D 06:00, D+1 06:00)
        cal_date     = end_dt.date()
        shift_cutoff = datetime(
            cal_date.year, cal_date.month, cal_date.day,
            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
        )
        if end_dt < shift_cutoff:
            # end_time до 06:00 → смена предыдущего календарного дня
            cal_date     = cal_date - timedelta(days=1)
            shift_cutoff = datetime(
                cal_date.year, cal_date.month, cal_date.day,
                DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
            ) + timedelta(days=1)
        else:
            shift_cutoff = shift_cutoff + timedelta(days=1)
        shift_start = shift_cutoff - timedelta(days=1)

        # Фильтр по периоду сезона
        if not (date(SEASON_YEAR, 1, 1) <= cal_date <= report_date):
            continue

        task_shift[tid] = (shift_start, shift_cutoff)
        filtered_tasks.append(t)

        # Суточное окно для "Факт за сутки"
        if day_start <= end_dt < cutoff:
            today_task_ids.add(tid)

    print(
        f"  Заданий с известной сменой: {len(filtered_tasks)}, "
        f"без end_time: {len(no_shift_tasks)}, за сутки: {len(today_task_ids)}"
    )

    # Загружаем mtfmi для всех заданий
    all_task_ids = [int(t["id"]) for t in filtered_tasks + no_shift_tasks]
    print("  Загружаем mtfmi для всех заданий...")
    mtfmi_map = await fetch_mtfmi_for_tasks(all_task_ids, semaphore, client)

    # Накапливаем covered_area: firm → crop → (season, today)
    fact_acc:  dict[str, dict[str, float]] = {f: defaultdict(float) for f in AGROFIRMS}
    today_acc: dict[str, dict[str, float]] = {f: defaultdict(float) for f in AGROFIRMS}

    # ── Задания с известной сменой ────────────────────────────────────────────
    for t in filtered_tasks:
        tid      = int(t["id"])
        is_today = tid in today_task_ids
        _, shift_cutoff = task_shift[tid]

        for item in mtfmi_map.get(tid, []):
            fid = item.get("field_id")
            if not fid:
                continue
            fid_i = int(fid)

            # Как «Выраб. — 4.0 Посев» / FACT_BY_ALL_FIELDS: без available-фильтра.
            firm = firm_by_field.get(fid_i)
            if not firm:
                continue

            raw_crop = field_to_crop.get(fid_i, "")
            crop_season = _crop_for_seeding_fact(raw_crop, for_today=False)
            crop_today = _crop_for_seeding_fact(raw_crop, for_today=True)

            # Суммируем hourly до cutoff смены
            hourly = item.get("covered_area_hourly") or []
            cov = 0.0
            for entry in hourly:
                h = parse_dt(entry[0])
                if h and h < shift_cutoff:
                    cov += float(entry[1] or 0.0)
            # Fallback: только если hourly список пуст
            if not hourly:
                cov = float(item.get("covered_area") or 0.0)

            if cov <= 0.5:
                continue

            if crop_season:
                fact_acc[firm][crop_season] += cov
            if is_today and crop_today:
                today_acc[firm][crop_today] += cov

    # ── Задания без end_time: covered_area напрямую ───────────────────────────
    for t in no_shift_tasks:
        tid = int(t["id"])
        for item in mtfmi_map.get(tid, []):
            fid = item.get("field_id")
            if not fid:
                continue
            fid_i = int(fid)

            firm = firm_by_field.get(fid_i)
            if not firm:
                continue

            raw_crop = field_to_crop.get(fid_i, "")
            crop_season = _crop_for_seeding_fact(raw_crop, for_today=False)
            crop_today = _crop_for_seeding_fact(raw_crop, for_today=True)

            cov = float(item.get("covered_area") or 0.0)
            if cov <= 0.5:
                continue

            if crop_season:
                fact_acc[firm][crop_season] += cov
            if crop_today:
                # нет end_time → считаем суточными (как в 5_main)
                today_acc[firm][crop_today] += cov

    # Формируем результат
    result: dict[str, dict] = {}
    for firm in AGROFIRMS:
        result[firm] = {
            "fact_by_crop":  {k: round(v, 2) for k, v in fact_acc[firm].items()},
            "today_by_crop": {k: round(v, 2) for k, v in today_acc[firm].items()},
        }
    return result



# =============================================================================
# ПОДСЧЁТ ФАКТА ПО ОВОЩНЫМ (через region → machines → tasks)
# Логика аналогична 5_main: фильтр по VEG_WT_IDS, fallback культуры по work_type_id
# =============================================================================

VEG_FIRM        = "АФ Чистопольская"
VEG_FIRM_REGION = AGROFIRMS[VEG_FIRM]   # machine_region_id (FIRM_TO_REGION)

def _task_work_type_id(task: dict) -> int | None:
    raw = task.get("work_type_id")
    if raw is None:
        wt_obj = task.get("work_type")
        if isinstance(wt_obj, dict):
            raw = wt_obj.get("id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _merge_veg_into_firm_stats(firm_stats: dict[str, dict], veg_stats: dict) -> None:
    """Подмешивает овощи в таблицу посева АФ Чистопольская (листы ЯрСев)."""
    bucket = firm_stats.setdefault(VEG_FIRM, {"fact_by_crop": {}, "today_by_crop": {}})
    for key in ("fact_by_crop", "today_by_crop"):
        for crop, val in (veg_stats.get(key) or {}).items():
            if float(val or 0.0) != 0.0:
                bucket[key][crop] = round(float(val), 2)


def _veg_task_in_firm(
    field_id: int | None,
    machine_id: object,
    field_to_firm: dict[int, str],
    region_machine_ids: set[int],
) -> bool:
    """Поле Чистопольской АФ или машина региона на дату отчёта."""
    if field_id is not None and field_to_firm.get(int(field_id)) == VEG_FIRM:
        return True
    if machine_id is None:
        return False
    try:
        return int(machine_id) in region_machine_ids
    except (TypeError, ValueError):
        return False


def _veg_cov_from_item(
    item: dict,
    day_start: datetime,
    cutoff: datetime,
    *,
    for_today: bool,
) -> float:
    hourly = item.get("covered_area_hourly") or []
    if for_today:
        cov = 0.0
        for entry in hourly:
            h = parse_dt(entry[0])
            if h and day_start <= h < cutoff:
                cov += float(entry[1] or 0.0)
        return cov
    return float(item.get("covered_area") or 0.0)


def _map_veg_crop_label(crop_name: str) -> str | None:
    """Нормализует название культуры к строке из VEG_CROP_ROWS."""
    raw = str(crop_name or "").strip().lower().replace("ё", "е")
    if not raw:
        return None
    if "карто" in raw:
        return "Картофель"
    if "лук" in raw:
        return "Лук"
    if "морков" in raw:
        return "Морковь"
    if "свек" in raw:
        return "Свекла"
    mapped = _VEG_CROP_NAME_MAP.get(str(crop_name or "").strip())
    if mapped in VEG_CROP_ROWS:
        return mapped
    return None


def _veg_label_for_item(
    task: dict,
    item: dict,
    field_to_crop: dict[int, str] | None,
    crop_names_by_id: dict[int, str] | None = None,
) -> str | None:
    """Культура по полю MTFMI (приоритет), иначе по заданию."""
    fid = item.get("field_id")
    if field_to_crop is not None and fid is not None:
        try:
            by_field = _map_veg_crop_label(field_to_crop.get(int(fid), ""))
            if by_field:
                return by_field
        except (TypeError, ValueError):
            pass
    return _veg_crop_label(task, crop_names_by_id)


def _veg_crop_label(task: dict, crop_names_by_id: dict[int, str] | None = None) -> str | None:
    """Как в 5_main: crop_id → имя, затем текстовые поля задания, затем fallback по work_type_id."""
    cid = task.get("crop_id")
    if crop_names_by_id and cid is not None:
        try:
            mapped = crop_names_by_id.get(int(cid), "")
            by_crop = _map_veg_crop_label(mapped)
            if by_crop:
                return by_crop
        except (TypeError, ValueError):
            pass
    for key in ("crop_name", "crop_title", "culture_name", "culture_title",
                "field_crop_name", "field_crop_title"):
        by_crop = _map_veg_crop_label(str(task.get(key) or ""))
        if by_crop:
            return by_crop
    wt = task.get("work_type_id")
    try:
        if wt is not None:
            return VEG_WT_FALLBACK_CROP.get(int(wt))
    except (TypeError, ValueError):
        pass
    return None


async def _fetch_region_machine_ids(
    client: httpx.AsyncClient,
    region_id: int,
    target_date: str,
    semaphore: asyncio.Semaphore,
) -> set[int]:
    url = f"{BASE_URL}/machine_region_mapping_items"
    with_end, no_end = await asyncio.gather(
        fetch_all_pages(client, url, {
            "machine_region_id": region_id,
            "date_start_lt_eq":  target_date,
            "date_end_gt_eq":    target_date,
        }, semaphore),
        fetch_all_pages(client, url, {
            "machine_region_id": region_id,
            "date_start_lt_eq":  target_date,
            "no_date_end":       "true",
        }, semaphore),
    )
    return {m["machine_id"] for m in with_end + no_end if m.get("machine_id")}


async def fetch_veg_stats(
    client:               httpx.AsyncClient,
    semaphore:            asyncio.Semaphore,
    day_start:            datetime,
    cutoff:               datetime,
    season_start:         datetime,
    field_to_firm:        dict[int, str],
    available_field_ids:  set[int] | None = None,
    crop_names_by_id:     dict[int, str] | None = None,
    season_cutoff:        datetime | None = None,
    field_to_crop:        dict[int, str] | None = None,
) -> dict:
    """
    Факт посева овощей по АФ Чистопольская — та же логика, что в 5_main:
    задания по VEG_WT_IDS, площадь из MTFMI по полям (не covered_area задания).
    """
    if season_cutoff is None:
        season_cutoff = cutoff
    empty = {"fact_by_crop": {}, "today_by_crop": {}}
    firm_map = field_to_firm
    target_date = day_start.strftime("%Y-%m-%d")
    region_machine_ids = await _fetch_region_machine_ids(
        client, VEG_FIRM_REGION, target_date, semaphore
    )
    print(f"  Овощи: машин региона {VEG_FIRM_REGION} на {target_date}: {len(region_machine_ids)}")

    url = f"{BASE_URL}/machine_tasks"
    day_start_str    = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str       = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    season_start_str = season_start.strftime("%Y-%m-%dT%H:%M:%S")
    season_cutoff_str = season_cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    print("  Овощи: загружаем machine_tasks по work_type_id...")
    today_chunks, season_chunks = await asyncio.gather(
        asyncio.gather(*[
            fetch_all_pages(client, url, {
                "work_type_id":   wt,
                "end_time_gt_eq": day_start_str,
                "end_time_lt":    cutoff_str,
            }, semaphore)
            for wt in sorted(VEG_WT_IDS)
        ]),
        asyncio.gather(*[
            fetch_all_pages(client, url, {
                "work_type_id":   wt,
                "end_time_gt_eq": season_start_str,
                "end_time_lt":    season_cutoff_str,
            }, semaphore)
            for wt in sorted(VEG_WT_IDS)
        ]),
    )

    def _dedupe_tasks(chunks: list) -> list[dict]:
        out: list[dict] = []
        seen: set[int] = set()
        for chunk in chunks:
            for t in chunk:
                tid = t.get("id")
                if not tid:
                    continue
                tid_i = int(tid)
                if tid_i in seen:
                    continue
                seen.add(tid_i)
                if _task_work_type_id(t) not in VEG_WT_IDS:
                    continue
                out.append(t)
        return out

    tasks_today  = _dedupe_tasks(today_chunks)
    tasks_season = _dedupe_tasks(season_chunks)
    print(f"  Овощи: заданий за сезон={len(tasks_season)}, за сутки={len(tasks_today)}")

    all_task_ids = list({int(t["id"]) for t in tasks_today + tasks_season})
    if not all_task_ids:
        return empty

    print(f"  Овощи: загружаем mtfmi для {len(all_task_ids)} заданий...")
    mtfmi_map = await fetch_mtfmi_for_tasks(all_task_ids, semaphore, client)

    fact_acc:  dict[str, float] = defaultdict(float)
    today_acc: dict[str, float] = defaultdict(float)
    skipped_no_label = 0

    for t in tasks_season:
        tid = int(t["id"])
        items = mtfmi_map.get(tid, [])
        if items:
            for item in items:
                label = _veg_label_for_item(t, item, field_to_crop, crop_names_by_id)
                if not label:
                    skipped_no_label += 1
                    continue
                cov = _veg_cov_from_item(item, day_start, cutoff, for_today=False)
                if cov > 0:
                    fact_acc[label] += cov
        else:
            label = _veg_crop_label(t, crop_names_by_id)
            if not label:
                skipped_no_label += 1
                continue
            cov = float(t.get("covered_area") or 0.0)
            if cov > 0:
                fact_acc[label] += cov

    today_task_ids = {int(t["id"]) for t in tasks_today}
    for t in tasks_season:
        tid = int(t["id"])
        items = mtfmi_map.get(tid, [])
        if items:
            for item in items:
                label = _veg_label_for_item(t, item, field_to_crop, crop_names_by_id)
                if not label:
                    continue
                cov = _veg_cov_from_item(item, day_start, cutoff, for_today=True)
                if cov > 0:
                    today_acc[label] += cov
        elif tid in today_task_ids:
            label = _veg_crop_label(t, crop_names_by_id)
            if not label:
                continue
            cov = float(t.get("covered_area") or 0.0)
            if cov > 0:
                today_acc[label] += cov

    if not fact_acc and not today_acc:
        print(
            f"  Овощи: площадь 0 при {len(all_task_ids)} заданиях "
            f"(без культуры: {skipped_no_label})"
        )
    else:
        print(f"  Овощи итого: факт={dict(fact_acc)}, сутки={dict(today_acc)}", flush=True)

    return {
        "fact_by_crop":  {k: round(v, 2) for k, v in fact_acc.items()},
        "today_by_crop": {k: round(v, 2) for k, v in today_acc.items()},
    }


# =============================================================================
# СТИЛИ
# =============================================================================

_COLOR_HDR   = "7B8EB1"   # как в остальных таблицах первой сводки
_COLOR_TOTAL = "BDD7EE"   # голубой итог
_COLOR_DATA  = "FFFFFF"   # белые строки данных
_COLOR_TITLE = "7B8EB1"   # как в остальных таблицах первой сводки


def _border() -> Border:
    t = Side(style="thin")
    return Border(left=t, right=t, top=t, bottom=t)


def _cell(
    ws,
    row: int,
    col: int,
    value,
    bold: bool = False,
    bg: str | None = None,
    color: str = "000000",
    halign: str = "center",
    number_format: str | None = None,
    font_size: int = 10,
    wrap: bool = True,
) -> None:
    c = ws.cell(row=row, column=col, value=value)
    c.font      = Font(bold=bold, color=color, size=font_size, name="Calibri")
    c.alignment = Alignment(horizontal=halign, vertical="center", wrap_text=wrap)
    c.border    = _border()
    if bg:
        c.fill = PatternFill("solid", fgColor=bg)
    if number_format:
        c.number_format = number_format


# =============================================================================
# ЗАПИСЬ ЛИСТА ДЛЯ ОДНОЙ АГРОФИРМЫ
# =============================================================================

HEADERS = [
    "№",
    "Культура",
    "План, га",
    "Факт общий, га",
    "Остаток к выполнению, га",
    "Доля факта от плана, %",
    "Факт за сутки, га",
    "Прогнозная дата завершения\nпо фактической выработке",
]

COL_WIDTHS = [9, 13, 13, 15, 20, 16, 15, 36]


def _forecast_date(report_date: date, plan: float, fact: float, today: float) -> str:
    if plan <= 0:
        return ""
    # По требованию: если факта нет, прогнозную дату не показываем.
    if fact <= 0:
        return ""
    rem = max(plan - fact, 0.0)
    if rem <= 0:
        return report_date.strftime("%d.%m.%Y")
    # Если за сутки нет выработки, используем усреднённый темп по факту на дату отчёта.
    if today <= 0:
        days_elapsed = max((report_date - date(report_date.year, 1, 1)).days + 1, 1)
        today = fact / days_elapsed
    if today <= 0:
        return ""
    days = math.ceil(rem / today)
    return (report_date + timedelta(days=max(1, days))).strftime("%d.%m.%Y")


def _write_crop_rows(
    ws,
    crops: tuple,
    plan_map: dict,
    fact_map: dict,
    today_map: dict,
    report_date: date,
    start_row: int,
) -> tuple[int, float, float, float, float]:
    """
    Записывает строки культур начиная с start_row.
    Возвращает (следующий_row, totals_plan, totals_fact, totals_today, totals_rem_pos).
    Пустые значения по плану приводятся к 0, чтобы не было пустых ячеек.
    """
    row = start_row
    totals_plan  = 0.0
    totals_fact  = 0.0
    totals_today = 0.0
    totals_rem_pos = 0.0

    for i, crop in enumerate(crops, start=1):
        raw_plan = plan_map.get(crop)
        plan     = float(raw_plan) if raw_plan is not None else 0.0
        fact     = _sum_crop_from_map(fact_map, crop)
        today    = _sum_crop_from_map(today_map, crop)

        rem   = plan - fact
        pct   = fact / plan if plan > 0 else 0.0
        fdate = _forecast_date(report_date, plan, fact, today) if plan > 0 else ""
        totals_plan += plan

        totals_fact  += fact
        totals_today += today
        totals_rem_pos += rem

        vals = [i, crop, plan, fact, rem, pct, today, fdate]
        for ci, v in enumerate(vals, start=1):
            fmt = None
            if ci in (3, 4, 5, 7):
                fmt = NF_DEC2
            elif ci == 6:
                fmt = "0.0%"
            _cell(ws, row, ci, v,
                  bg=_COLOR_DATA,
                  halign="left" if ci == 2 else "center",
                  number_format=fmt,
                  font_size=10)
        ws.row_dimensions[row].height = 18
        row += 1

    return row, totals_plan, totals_fact, totals_today, totals_rem_pos


def write_firm_sheet(
    wb: openpyxl.Workbook,
    firm_name: str,
    report_date: date,
    stats: dict,
) -> None:
    crops      = FIRM_CROPS.get(firm_name, CROP_ORDER)
    plan_map   = PLAN_HA.get(firm_name, {})
    fact_map   = stats.get("fact_by_crop", {})
    today_map  = stats.get("today_by_crop", {})

    sheet_title = firm_name[:31]
    ws = wb.create_sheet(title=sheet_title)

    row = 1

    # Заголовок агрофирмы
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(HEADERS))
    c = ws.cell(row=row, column=1, value=f"Посев — {firm_name}")
    c.font      = Font(bold=True, size=13, name="Calibri", color="FFFFFF")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill      = PatternFill("solid", fgColor=_COLOR_TITLE)
    c.border    = _border()
    ws.row_dimensions[row].height = 22
    row += 1

    # Шапка
    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, row, ci, h, bold=True, bg=_COLOR_HDR, color="FFFFFF",
              halign="center", font_size=10, wrap=True)
    ws.row_dimensions[row].height = 42
    row += 1

    row, totals_plan, totals_fact, totals_today, totals_rem_pos = _write_crop_rows(
        ws, crops, plan_map, fact_map, today_map, report_date, row
    )

    # Итоговая строка
    total_rem = totals_rem_pos
    total_pct = totals_fact / totals_plan if totals_plan > 0 else 0.0
    total_vals = ["", "Итого", totals_plan, totals_fact, total_rem, total_pct, totals_today, ""]
    for ci, v in enumerate(total_vals, start=1):
        fmt = None
        if ci in (3, 4, 5, 7):
            fmt = NF_DEC2
        elif ci == 6:
            fmt = "0.0%"
        _cell(ws, row, ci, v, bold=True, bg=_COLOR_TOTAL,
              halign="left" if ci == 2 else "center",
              number_format=fmt, font_size=10, wrap=False)
    ws.row_dimensions[row].height = 18

    # Ширины колонок
    for ci, w in enumerate(COL_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    ws.freeze_panes = "A3"


def write_summary_sheet(
    wb: openpyxl.Workbook,
    report_date: date,
    firm_stats: dict[str, dict],
) -> None:
    """Сводный лист — сумма по всем агрофирмам."""
    ws = wb.create_sheet(title="Итого (все АФ)", index=0)
    row = 1

    # Заголовок
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(HEADERS))
    c = ws.cell(row=row, column=1, value="Посев — Сводно по всем агрофирмам")
    c.font      = Font(bold=True, size=10, name="Calibri", color="FFFFFF")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill      = PatternFill("solid", fgColor=_COLOR_TITLE)
    c.border    = _border()
    ws.row_dimensions[row].height = 22
    row += 1

    # Шапка
    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, row, ci, h, bold=True, bg=_COLOR_HDR, color="FFFFFF",
              halign="center", font_size=8, wrap=True)
    ws.row_dimensions[row].height = 42
    row += 1

    # Суммируем план и факт по каждой культуре
    total_plan_all  = 0.0
    total_fact_all  = 0.0
    total_today_all = 0.0
    total_rem_pos_all = 0.0

    for i, crop in enumerate(CROP_ORDER, start=1):
        # Пустые планы приводим к 0, чтобы в таблице не было пустых ячеек.
        crop_plan_values = [
            PLAN_HA.get(firm, {}).get(crop) or 0.0
            for firm in AGROFIRMS
        ]
        plan  = float(sum(crop_plan_values))
        fact  = _sum_crop_across_firms(firm_stats, crop, "fact_by_crop")
        today = _sum_crop_across_firms(firm_stats, crop, "today_by_crop")

        rem   = plan - fact
        pct   = fact / plan if plan > 0 else 0.0
        fdate = _forecast_date(report_date, plan, fact, today) if plan > 0 else ""
        total_plan_all += plan

        total_fact_all  += fact
        total_today_all += today
        total_rem_pos_all += rem

        vals = [i, crop, plan, fact, rem, pct, today, fdate]
        for ci, v in enumerate(vals, start=1):
            fmt = None
            if ci in (3, 4, 5, 7):
                fmt = NF_DEC2
            elif ci == 6:
                fmt = "0.0%"
            _cell(ws, row, ci, v,
                  bg=_COLOR_DATA,
                  halign="left" if ci == 2 else "center",
                  number_format=fmt,
                  font_size=8)
        ws.row_dimensions[row].height = 18
        row += 1

    # Итоговая строка сводного листа
    total_rem = total_rem_pos_all
    total_pct = total_fact_all / total_plan_all if total_plan_all > 0 else 0.0
    total_vals = ["", "Итого", total_plan_all, total_fact_all, total_rem, total_pct, total_today_all, ""]
    for ci, v in enumerate(total_vals, start=1):
        fmt = None
        if ci in (3, 4, 5, 7):
            fmt = NF_DEC2
        elif ci == 6:
            fmt = "0.0%"
        _cell(ws, row, ci, v, bold=True, bg=_COLOR_TOTAL,
              halign="left" if ci == 2 else "center",
              number_format=fmt, font_size=8, wrap=False)
    ws.row_dimensions[row].height = 18
    row += 1

    for ci, w in enumerate(COL_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    ws.freeze_panes = "A3"



async def build_report_async(report_date: date = REPORT_DATE) -> None:
    day_start, cutoff = compute_time_range(report_date)
    season_start = datetime(SEASON_YEAR, 1, 1, tzinfo=TZ_MSK)

    print(f"Отчёт 'Посев'  |  Дата: {report_date}")
    print(f"  Суточный диапазон: {day_start} — {cutoff}\n")

    # Загружаем справочники синхронно (один раз)
    field_to_crop, field_to_firm, available_field_ids, field_to_firm_all = load_field_references(report_date)

    headers   = {"X-User-Api-Token": API_TOKEN}
    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    pool      = httpx.Limits(max_connections=60, max_keepalive_connections=30)

    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        firm_stats = await fetch_all_crop_stats(
            client, semaphore,
            day_start, cutoff, season_start,
            field_to_crop, field_to_firm,
            available_field_ids=available_field_ids,
            report_date=report_date,
            field_to_firm_all=field_to_firm_all,
        )
    for firm_name, stats in firm_stats.items():
        print(f"\n  {firm_name}:")
        for crop in FIRM_CROPS.get(firm_name, CROP_ORDER):
            fact  = stats["fact_by_crop"].get(crop, 0.0)
            today = stats["today_by_crop"].get(crop, 0.0)
            print(f"    {crop:30s}  факт={fact:8.2f}  сутки={today:8.2f}")

    # Формируем Excel
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    for firm_name in AGROFIRMS:
        write_firm_sheet(wb, firm_name, report_date, firm_stats[firm_name])

    # Сводный лист создаётся последним, но вставляется на позицию 0
    write_summary_sheet(wb, report_date, firm_stats)

    output_file = f"spring_seeding_{report_date}.xlsx"
    wb.save(output_file)
    print(f"\nСохранено: {output_file}")


def build_report(report_date: date = REPORT_DATE) -> None:
    asyncio.run(build_report_async(report_date))


def _write_firm_block_compact(ws, firm_name: str, report_date: date, stats: dict, start_row: int) -> int:
    crops = FIRM_CROPS.get(firm_name, CROP_ORDER)
    plan_map = PLAN_HA.get(firm_name, {})
    fact_map = stats.get("fact_by_crop", {})
    today_map = stats.get("today_by_crop", {})

    row = start_row
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(HEADERS))
    c = ws.cell(row=row, column=1, value=f"Посев — {firm_name}")
    c.font = Font(bold=True, size=10, name="Calibri", color="FFFFFF")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill = PatternFill("solid", fgColor=_COLOR_TITLE)
    c.border = _border()
    ws.row_dimensions[row].height = 18
    row += 1

    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, row, ci, h, bold=True, bg=_COLOR_HDR, color="FFFFFF",
              halign="center", font_size=8, wrap=True)
    ws.row_dimensions[row].height = 34
    row += 1

    row, totals_plan, totals_fact, totals_today, totals_rem_pos = _write_crop_rows(
        ws, crops, plan_map, fact_map, today_map, report_date, row
    )

    for r in range(start_row + 2, row):
        ws.row_dimensions[r].height = 15
        for ci in range(1, len(HEADERS) + 1):
            ws.cell(row=r, column=ci).font = Font(name="Calibri", size=8, bold=False)

    total_rem = totals_rem_pos
    total_pct = totals_fact / totals_plan if totals_plan > 0 else 0.0
    total_vals = ["", "Итого", totals_plan, totals_fact, total_rem, total_pct, totals_today, ""]
    for ci, v in enumerate(total_vals, start=1):
        fmt = None
        if ci in (3, 4, 5, 7):
            fmt = NF_DEC2
        elif ci == 6:
            fmt = "0.0%"
        _cell(ws, row, ci, v, bold=True, bg=_COLOR_TOTAL,
              halign="left" if ci == 2 else "center", number_format=fmt, font_size=8, wrap=False)
    ws.row_dimensions[row].height = 16
    return row + 2


def write_veg_summary_sheet(
    wb: openpyxl.Workbook,
    report_date: date,
    veg_stats: dict,
) -> None:
    """Отдельный лист — посев/посадка овощных культур (АФ Чистопольская)."""
    ws = wb.create_sheet(title="Овощи — посев", index=1)

    row = 1

    # Заголовок
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(HEADERS))
    c = ws.cell(row=row, column=1, value="Посев / посадка овощных культур — АФ Чистопольская")
    c.font      = Font(bold=True, size=12, name="Calibri", color="FFFFFF")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill      = PatternFill("solid", fgColor=_COLOR_TITLE)
    c.border    = _border()
    ws.row_dimensions[row].height = 22
    row += 1

    # Шапка
    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, row, ci, h, bold=True, bg=_COLOR_HDR, color="FFFFFF",
              halign="center", font_size=10, wrap=True)
    ws.row_dimensions[row].height = 42
    row += 1

    fact_map  = veg_stats.get("fact_by_crop", {})
    today_map = veg_stats.get("today_by_crop", {})

    total_plan = total_fact = total_today = total_rem_pos = 0.0
    for i, crop in enumerate(VEG_CROP_ROWS, start=1):
        plan  = float(VEG_CROP_PLAN_HA.get(crop, 0.0) or 0.0)
        fact  = float(fact_map.get(crop, 0.0) or 0.0)
        today = float(today_map.get(crop, 0.0) or 0.0)
        rem   = plan - fact
        pct   = fact / plan if plan > 0 else 0.0
        fdate = _forecast_date(report_date, plan, fact, today) if plan > 0 else ""

        total_plan  += plan
        total_fact  += fact
        total_today += today
        total_rem_pos += rem

        vals = [i, crop, plan, fact, rem, pct, today, fdate]
        for ci, v in enumerate(vals, start=1):
            fmt = None
            if ci in (3, 4, 5, 7):
                fmt = NF_DEC2
            elif ci == 6:
                fmt = "0.0%"
            _cell(ws, row, ci, v,
                  bg=_COLOR_DATA,
                  halign="left" if ci == 2 else "center",
                  number_format=fmt,
                  font_size=10)
        ws.row_dimensions[row].height = 18
        row += 1

    # Итоговая строка
    total_rem = total_rem_pos
    total_pct = total_fact / total_plan if total_plan > 0 else 0.0
    total_vals = ["", "Итого", total_plan, total_fact, total_rem, total_pct, total_today, ""]
    for ci, v in enumerate(total_vals, start=1):
        fmt = None
        if ci in (3, 4, 5, 7):
            fmt = NF_DEC2
        elif ci == 6:
            fmt = "0.0%"
        _cell(ws, row, ci, v, bold=True, bg=_COLOR_TOTAL,
              halign="left" if ci == 2 else "center",
              number_format=fmt, font_size=10, wrap=False)
    ws.row_dimensions[row].height = 18

    for ci, w in enumerate(COL_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    ws.freeze_panes = "A3"


async def append_tables_to_workbook_async(
    wb: openpyxl.Workbook,
    report_date: date = REPORT_DATE,
    seeding_day_stats: dict[str, dict] | None = None,
    firm_stats: dict[str, dict] | None = None,
) -> None:
    """
    Встраивает доп.таблицы в начало существующей книги:
    минимум 3 таблицы на лист.

    seeding_day_stats — опционально статистика «Выраб. — 4.0 Посев»
    (area_today / area_by_crop); если передана, «Факт за сутки» берётся из неё.
    firm_stats — готовый fact_by_crop из 5_main (один источник с листами рапса/пшеницы).
    """
    if firm_stats is None:
        day_start, cutoff = compute_time_range(report_date)
        season_start = datetime(SEASON_YEAR, 1, 1, tzinfo=TZ_MSK)
        field_to_crop, field_to_firm, available_field_ids, field_to_firm_all = (
            load_field_references(report_date)
        )

        headers = {"X-User-Api-Token": API_TOKEN}
        semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
        pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
        async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
            firm_stats = await fetch_all_crop_stats(
                client, semaphore, day_start, cutoff, season_start, field_to_crop, field_to_firm,
                available_field_ids=available_field_ids,
                report_date=report_date,
                field_to_firm_all=field_to_firm_all,
            )

    _apply_seeding_today_from_perf(firm_stats, seeding_day_stats)

    # Добавляем сводную таблицу "общая информация".
    write_summary_sheet(wb, report_date, firm_stats)

    firm_names = list(AGROFIRMS.keys())
    chunks = [firm_names[i:i + 2] for i in range(0, len(firm_names), 2)]
    for sheet_idx, chunk in enumerate(chunks):
        ws = wb.create_sheet(title=f"ЯрСев блок {sheet_idx + 1}"[:31], index=sheet_idx + 1)
        for ci, w in enumerate(COL_WIDTHS, start=1):
            ws.column_dimensions[get_column_letter(ci)].width = w
        row = 1
        for firm_name in chunk:
            row = _write_firm_block_compact(ws, firm_name, report_date, firm_stats[firm_name], row)


if __name__ == "__main__":
    build_report()