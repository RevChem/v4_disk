"""
Единая логика площади по machine_tasks для сводок 1, 2 и 6.

Площадь за смену: MTFMI по полям в окне [window_start, window_end),
затем пропорциональное распределение task.covered_area по агрофирмам.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime


def item_area_in_window(
    item: dict,
    window_end: datetime | None,
    window_start: datetime | None = None,
    *,
    parse_dt: Callable[[object], datetime | None],
) -> float:
    """Площадь MTFMI в окне смены [window_start, window_end)."""
    hourly = item.get("covered_area_hourly") or []
    if hourly and window_end is not None:
        total = 0.0
        for entry in hourly:
            h = parse_dt(entry[0])
            if h is None:
                continue
            if window_start is not None and h < window_start:
                continue
            if h < window_end:
                total += float(entry[1] or 0)
        if total > 0:
            return total
    return float(item.get("covered_area") or 0)


def firms_from_task_items(
    items: list[dict],
    *,
    field_ok: Callable[[int], bool],
    firm_for_field: Callable[[int], str],
) -> set[str]:
    firms: set[str] = set()
    for item in items:
        fid = item.get("field_id")
        if not fid:
            continue
        try:
            fid_i = int(fid)
        except (TypeError, ValueError):
            continue
        if not field_ok(fid_i):
            continue
        firm = firm_for_field(fid_i)
        if firm:
            firms.add(firm)
    return firms


def task_cov_by_firm(
    task: dict,
    items: list[dict],
    *,
    cutoff: datetime | None,
    window_start: datetime | None,
    field_ok: Callable[[int], bool],
    firm_for_field: Callable[[int], str],
    parse_dt: Callable[[object], datetime | None],
    item_allowed: Callable[[int], bool] | None = None,
) -> dict[str, float]:
    """
    Площадь задания по агрофирмам.

    1. Суммируем MTFMI по полям в окне смены.
    2. Масштабируем к task.covered_area (официальная цифра Cropwise).
    3. Если MTFMI пуст — равное деление covered_area по фирмам из items.
    """
    cov = float(task.get("covered_area") or 0)
    if cov <= 0:
        return {}

    firm_raw: dict[str, float] = defaultdict(float)
    for item in items:
        fid = item.get("field_id")
        if not fid:
            continue
        try:
            fid_i = int(fid)
        except (TypeError, ValueError):
            continue
        if not field_ok(fid_i):
            continue
        if item_allowed is not None and not item_allowed(fid_i):
            continue
        firm = firm_for_field(fid_i)
        if not firm:
            continue
        area = item_area_in_window(
            item, cutoff, window_start, parse_dt=parse_dt,
        )
        if area > 0:
            firm_raw[firm] += area

    if not firm_raw:
        firms = firms_from_task_items(
            items, field_ok=field_ok, firm_for_field=firm_for_field,
        )
        if not firms:
            return {}
        share = cov / len(firms)
        return {firm: share for firm in firms}

    total_raw = sum(firm_raw.values())
    if total_raw <= 0:
        share = cov / len(firm_raw)
        return {firm: share for firm in firm_raw}

    return {firm: cov * (raw / total_raw) for firm, raw in firm_raw.items()}


def task_crop_cov_for_firm(
    task: dict,
    items: list[dict],
    firm: str,
    cov_for_firm: float,
    *,
    cutoff: datetime | None,
    window_start: datetime | None,
    field_ok: Callable[[int], bool],
    firm_for_field: Callable[[int], str],
    crop_for_field: Callable[[int], str],
    parse_dt: Callable[[object], datetime | None],
    item_allowed: Callable[[int], bool] | None = None,
) -> dict[str, float]:
    """Распределение cov_for_firm по культурам пропорционально MTFMI в окне."""
    raw_crop: dict[str, float] = defaultdict(float)
    for item in items:
        fid = item.get("field_id")
        if not fid:
            continue
        try:
            fid_i = int(fid)
        except (TypeError, ValueError):
            continue
        if not field_ok(fid_i):
            continue
        if item_allowed is not None and not item_allowed(fid_i):
            continue
        if firm_for_field(fid_i) != firm:
            continue
        item_cov = item_area_in_window(
            item, cutoff, window_start, parse_dt=parse_dt,
        )
        if item_cov <= 0:
            continue
        crop = crop_for_field(fid_i)
        if crop:
            raw_crop[crop] += item_cov

    raw_total = sum(raw_crop.values())
    if raw_total <= 0 or cov_for_firm <= 0:
        return {}
    scale = cov_for_firm / raw_total
    return {crop: raw * scale for crop, raw in raw_crop.items()}


def task_field_cov_for_firm(
    task: dict,
    items: list[dict],
    firm: str,
    cov_for_firm: float,
    *,
    cutoff: datetime | None,
    window_start: datetime | None,
    field_ok: Callable[[int], bool],
    firm_for_field: Callable[[int], str],
    parse_dt: Callable[[object], datetime | None],
    item_allowed: Callable[[int], bool] | None = None,
) -> dict[int, float]:
    """Распределение cov_for_firm по полям пропорционально MTFMI в окне."""
    raw_field: dict[int, float] = defaultdict(float)
    for item in items:
        fid = item.get("field_id")
        if not fid:
            continue
        try:
            fid_i = int(fid)
        except (TypeError, ValueError):
            continue
        if not field_ok(fid_i):
            continue
        if item_allowed is not None and not item_allowed(fid_i):
            continue
        if firm_for_field(fid_i) != firm:
            continue
        item_cov = item_area_in_window(
            item, cutoff, window_start, parse_dt=parse_dt,
        )
        if item_cov > 0:
            raw_field[fid_i] += item_cov

    raw_total = sum(raw_field.values())
    if raw_total <= 0 or cov_for_firm <= 0:
        return {}
    scale = cov_for_firm / raw_total
    return {fid: raw * scale for fid, raw in raw_field.items()}


# =============================================================================
# Кукуруза на силос / на зерно (операции «Скашивание») — сводки 2, 10, 11, 14
# =============================================================================

#   74  — 5.6 Скашивание ботвы
#   83  — 6.1 Скашивание с измельчением трав
#   84  — 6.3 Скашивание с измельчением кукурузы
#  147  — 6.8 Скашивание в валок
#  149  — 6.9 Скашивание на сено
SILAGE_CORN_MOWING_WT_IDS: frozenset[int] = frozenset({74, 83, 84, 147, 149})

# Cropwise machine_group_id для «комбайнов на полях» в сводках уборки:
#   5 — Комбайны
#   9 — Зерноуборочные комбайны
#  16 — Комбайн кормоуборочный (скашивание кукурузы на силос, напр. wt=84)
HARVESTER_GROUP_IDS: frozenset[int] = frozenset({5, 9, 16})

CORN_GRAIN_CROP = "Кукуруза на зерно"
CORN_SILAGE_CROP = "Кукуруза на силос"

HARVEST_SILAGE_CORN_REPORT_IDS: frozenset[str] = frozenset({"10", "11", "12", "14"})

# Буфер end_time при отборе заданий смены (как 11_main_harvest: +2 ч).
CUTOFF_BUFFER_HOURS: int = 2


def harvest_item_area_for_shift(
    item: dict,
    cutoff: datetime,
    *,
    day_start: datetime | None = None,
    work_type_id: int | None = None,
    parse_dt: Callable[[object], datetime | None],
) -> float:
    """
    Площадь MTFMI за смену уборки (10/11/13/14 → эталон 772.44 га).

    Зерновые wt: сумма hourly с h < cutoff, без нижней границы (как сводка 2).
    Скашивание (SILAGE_CORN_MOWING_WT_IDS) при переданном day_start: окно
    [day_start, cutoff) — без spillover за 06:00 и без «хвостов» соседней смены.
    """
    is_mowing = (
        work_type_id is not None
        and int(work_type_id) in SILAGE_CORN_MOWING_WT_IDS
        and day_start is not None
    )
    hourly = item.get("covered_area_hourly") or []
    if not hourly:
        return float(item.get("covered_area") or 0.0)
    cov = 0.0
    if is_mowing:
        for entry in hourly:
            h = parse_dt(entry[0])
            if h and day_start <= h < cutoff:
                cov += float(entry[1] or 0.0)
    else:
        for entry in hourly:
            h = parse_dt(entry[0])
            if h and h < cutoff:
                cov += float(entry[1] or 0.0)
    return cov


def resolve_corn_subtype(
    crop_name: str,
    field_id: int | None,
    silage_field_ids: set[int],
) -> str:
    if not crop_name or "кукуруза" not in crop_name.casefold():
        return crop_name
    if field_id is None:
        return crop_name
    return CORN_SILAGE_CROP if int(field_id) in silage_field_ids else CORN_GRAIN_CROP


def collect_silage_corn_agro_operations(
    fetch_ops_by_wt: Callable[[int], list[dict]],
) -> list[dict]:
    """
    Данные «Кукуруза на силос» из agro_operations — как в 10_main_harvest._get_ops_for_crop.

    Берутся все операции «Скашивание» (SILAGE_CORN_MOWING_WT_IDS). Дальше обычно
    оставляют только поля с field_to_crop == «Кукуруза на силос»
    (см. filter_ops_for_crop / get_field_to_crop).
    """
    ops: list[dict] = []
    for wt_id in sorted(SILAGE_CORN_MOWING_WT_IDS):
        batch = fetch_ops_by_wt(int(wt_id)) or []
        if batch:
            ops.extend(batch)
    return ops


def filter_ops_for_crop(
    ops: list[dict],
    field_to_crop: dict[int, str],
    crop_name: str,
) -> list[dict]:
    """Оставить agro_operations, у которых поле имеет культуру crop_name."""
    out: list[dict] = []
    for op in ops:
        fid = op.get("field_id")
        if fid is None:
            continue
        if field_to_crop.get(int(fid)) == crop_name:
            out.append(op)
    return out


def harvest_crop_key_for_area(
    wt: int | None,
    field_crop: str | None,
    *,
    general_crops: set[str] | frozenset[str] | None = None,
    other_key: str | None = None,
    default_crop: str | None = None,
) -> str | None:
    """
    Культура для площади уборки (mtfmi) — общая логика сводок 2/10.

    Скашивание (SILAGE_CORN_MOWING_WT_IDS) идёт в «Кукуруза на силос» только если
    поле уже помечено как силос. Иначе None — запись пропускается (сено/травы
    не должны давать ~8500 га в кукурузу на силос).
    """
    fc = (field_crop or "").strip()
    if wt is not None and int(wt) in SILAGE_CORN_MOWING_WT_IDS:
        return CORN_SILAGE_CROP if fc == CORN_SILAGE_CROP else None
    if general_crops is not None:
        if fc in general_crops:
            return fc
        return other_key
    return fc or default_crop


def crop_key_for_harvest_area(
    wt: int,
    field_crop: str | None,
    general_crops: set[str],
    other_key: str,
) -> str | None:
    """Совместимая обёртка → harvest_crop_key_for_area (фильтр силоса)."""
    return harvest_crop_key_for_area(
        wt,
        field_crop,
        general_crops=general_crops,
        other_key=other_key,
    )


def collect_silage_corn_field_ids(
    season_year: int,
    fetch_ops: Callable[[int, int], list[dict]],
) -> set[int]:
    field_ids: set[int] = set()
    for wt_id in SILAGE_CORN_MOWING_WT_IDS:
        for op in fetch_ops(wt_id, season_year):
            fid = op.get("field_id")
            if fid is not None:
                field_ids.add(int(fid))
    return field_ids
