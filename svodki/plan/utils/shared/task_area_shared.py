"""
Единая логика площади по machine_tasks для сводок 1, 2 и 6.

Площадь за смену: MTFMI по полям в окне [window_start, window_end),
затем пропорциональное распределение task.covered_area по агрофирмам.

Кукуруза на силос / на зерно (сводки 10, 14): «Скашивание» (SILAGE_CORN_MOWING_WT_IDS)
кукурузы учитывается как «Кукуруза на силос», даже если в истории написано «на зерно».
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta

CORN_GRAIN_CROP = "Кукуруза на зерно"
CORN_SILAGE_CROP = "Кукуруза на силос"


def history_crop_name(item: dict, fallback: str = "", *, mowing: bool = False) -> str:
    """Культура строки площади. Скашивание кукурузы — всегда силос."""
    from mtfmi_batch_fetch import crop_of_item

    crop = crop_of_item(item, fallback or "")
    if mowing and crop and "кукуруза" in crop.casefold():
        return CORN_SILAGE_CROP
    return crop
SILAGE_CORN_MOWING_WT_IDS: frozenset[int] = frozenset({74, 83, 84, 147, 149})
# Комбайны + кормоуборочные (скашивание кукурузы на силос) — как 11/14.
HARVESTER_GROUP_IDS: frozenset[int] = frozenset({5, 9, 16})

# Буфер end_time при отборе заданий смены. Сводка 2 берёт закрытие до 18:00
# (cutoff + 12 ч) и оставляет только часы самой смены.
CUTOFF_BUFFER_HOURS: int = 12


def _shift_date_from_dt(dt: datetime, day_start_hour: int = 6) -> date:
    """Сменный день: [D day_start_hour, D+1 day_start_hour)."""
    cal = dt.date()
    anchor = datetime(
        cal.year, cal.month, cal.day, day_start_hour, 0, 0, tzinfo=dt.tzinfo,
    )
    if dt < anchor:
        return cal - timedelta(days=1)
    return cal


def area_ha_by_shift_date(
    item: dict,
    *,
    parse_dt: Callable[[object], datetime | None],
    day_start_hour: int = 6,
) -> dict[date, float]:
    """Площадь MTFMI по сменным датам из covered_area_hourly (уборка в сводке 2)."""
    hourly = item.get("covered_area_hourly") or []
    if not hourly:
        return {}
    portions: dict[date, float] = defaultdict(float)
    for entry in hourly:
        h = parse_dt(entry[0])
        if h is None:
            continue
        ha = float(entry[1] or 0.0)
        if ha <= 0:
            continue
        portions[_shift_date_from_dt(h, day_start_hour)] += ha
    return dict(portions)


def harvest_item_area_for_shift(
    item: dict,
    cutoff: datetime,
    *,
    day_start: datetime | None = None,
    work_type_id: int | None = None,
    parse_dt: Callable[[object], datetime | None],
    task_end: datetime | None = None,
) -> float:
    """
    Площадь MTFMI уборки.

    day_start задан — только часы окна [day_start, cutoff), как сводка 2:
    смена 06:00–06:00, без часов предыдущих и следующих суток.
    day_start нет — сумма hourly с меткой < cutoff (сезон до конца окна).
    Без почасовых данных covered_area относится к смене закрытия задания.
    """
    del work_type_id
    hourly = item.get("covered_area_hourly") or []
    if not hourly:
        if (
            task_end is not None
            and day_start is not None
            and not (day_start <= task_end < cutoff)
        ):
            return 0.0
        return float(item.get("covered_area") or 0.0)
    cov = 0.0
    for entry in hourly:
        h = parse_dt(entry[0])
        if h is None or h >= cutoff:
            continue
        if day_start is not None and h < day_start:
            continue
        cov += float(entry[1] or 0.0)
    return cov


def collect_silage_corn_field_ids(
    season: int,
    get_ops: Callable[[int, int], list[dict]],
) -> set[int]:
    """field_id полей с agro_operations «Скашивание» в сезоне."""
    field_ids: set[int] = set()
    for wt_id in SILAGE_CORN_MOWING_WT_IDS:
        for op in get_ops(wt_id, season):
            fid = op.get("field_id")
            if fid is not None:
                field_ids.add(int(fid))
    return field_ids


def resolve_corn_subtype(
    crop_name: str,
    field_id: int | None,
    silage_field_ids: set[int],
) -> str:
    """Кукуруза → «на силос» / «на зерно» по признаку «Скашивание» на поле."""
    if not crop_name or "кукуруза" not in crop_name.casefold():
        return crop_name
    if field_id is None:
        return crop_name
    return (
        CORN_SILAGE_CROP
        if int(field_id) in silage_field_ids
        else CORN_GRAIN_CROP
    )


def crop_key_for_harvest_area(
    wt: int,
    crop: str | None,
    general_crops: set[str],
    other_key: str,
) -> str:
    """
    Ключ культуры для агрегации площади в общем намолоте.

    Задания «Скашивание» учитываются в «Кукуруза на силос» только если
    поле уже классифицировано как кукуруза на силос. Иначе скашивание
    на травах/зерновых не попадает в строку кукурузы.
    """
    if not str(crop or "").strip():
        from mtfmi_batch_fetch import UNDEFINED_CROP

        return UNDEFINED_CROP
    if wt in SILAGE_CORN_MOWING_WT_IDS:
        if crop and "кукуруза" in crop.casefold():
            return CORN_SILAGE_CROP
        return other_key
    if crop in general_crops:
        return crop
    return other_key


def item_area_in_window(
    item: dict,
    window_end: datetime | None,
    window_start: datetime | None = None,
    *,
    parse_dt: Callable[[object], datetime | None],
) -> float:
    """Площадь MTFMI в окне смены [window_start, window_end).

    Если почасовые данные есть (даже нулевые или нечитаемые) — площадь считается
    только по ним, без отката к полной ``covered_area``: иначе фирма получала бы
    площадь, отработанную в другие сутки. К ``covered_area`` откатываемся только
    когда почасовых данных нет вовсе (или окно смены не задано).
    """
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
    row_allowed: Callable[[dict], bool] | None = None,
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
    saw_hourly = False  # были строки с почасовыми данными (окно смены применимо)
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
        if row_allowed is not None:
            if not row_allowed(item):
                continue
        elif item_allowed is not None and not item_allowed(fid_i):
            continue
        firm = firm_for_field(fid_i)
        if not firm:
            continue
        if item.get("covered_area_hourly"):
            saw_hourly = True
        area = item_area_in_window(
            item, cutoff, window_start, parse_dt=parse_dt,
        )
        if area > 0:
            firm_raw[firm] += area

    if not firm_raw:
        if saw_hourly:
            # Почасовые данные есть, но в окне смены площади нет: задание в эту смену не даёт 0 га,
            # а не «полную covered_area поровну между фирмами».
            return {}
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
    row_allowed: Callable[[dict], bool] | None = None,
    crop_for_item: Callable[[dict], str] | None = None,
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
        if row_allowed is not None:
            if not row_allowed(item):
                continue
        elif item_allowed is not None and not item_allowed(fid_i):
            continue
        if firm_for_field(fid_i) != firm:
            continue
        item_cov = item_area_in_window(
            item, cutoff, window_start, parse_dt=parse_dt,
        )
        if item_cov <= 0:
            continue
        crop = crop_for_item(item) if crop_for_item is not None else crop_for_field(fid_i)
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
    row_allowed: Callable[[dict], bool] | None = None,
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
        if row_allowed is not None:
            if not row_allowed(item):
                continue
        elif item_allowed is not None and not item_allowed(fid_i):
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
