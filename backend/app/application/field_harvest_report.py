"""Валовый сбор по действующим полям сезона.

Архив — как в сводках: поле не входит, если на сезон нет inventory_history_items
с available=true (historyable_type=Field).
Площадь — tillable_area (у АР-06-07 это 361,00 га).
Культура — имя crops по history_items.crop_id.
Сорт — history_items.variety (у АР-06-07 это «СИ ротанго»).
Посеяно — сумма completed_area операций «4.0 Посев» и «4.1 Посев (овощи)»
со статусом done или in_progress. Те же виды работ, что в plans.WORK_GROUPS.
Физический вес и урожай — history_items.harvested_weight в тоннах
(у АА-06-04 это 42,66 т).
Урожайность, ц/га — history_items.productivity, как в карточке поля
(у АА-06-04 это 15,80).
Дата сева — sowing_date, дата уборки — harvesting_date.
Зачётный вес (marketable_weight) в отчёт не входит.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from io import BytesIO
from typing import Any

import httpx
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from app.config import settings

PAGE_LIMIT = 1000
PAGE_SIZE = 100
# Один запрос к Cropwise может висеть на длинном сезоне. 30 минут.
CROPWISE_TIMEOUT = httpx.Timeout(1800.0, connect=30.0)

FIELD_HEADERS = (
    "Поле",
    "Площадь, га",
    "Культура",
    "Сорт",
    "Посеяно, га",
    "Дата сева",
    "Дата уборки",
    "Валовый сбор, т",
    "Урожай, т",
    "Урожайность, ц/га",
)
VARIETY_HEADERS = (
    "Сорт",
    "Площадь, га",
    "Посеяно, га",
    "Валовый сбор, т",
    "Урожайность, ц/га",
)
NO_VARIETY = "без сорта"
# plans.WORK_GROUPS: «4.0 Посев» и «4.1 Посев (овощи)».
SOWING_WORK_TYPE_IDS = (
    3, 4, 69, 70, 71, 72, 119, 120, 159, 160, 161, 163,
    68, 148, 172,
)
SOWN_STATUSES = frozenset({"done", "in_progress"})
# field_groups.id → агрофирма. ООО «Лаишевская» (11) входит в АФ Чистопольская.
# «АФЧ Овраги/неудобия» в список не входит.
FIRM_ORDER = (
    "АФ Чистопольская",
    "АгроРаздолье",
    "Аксу-Агро",
    "Актай",
    "Дрожжаное Рассвет Агро",
    "Лениногорская Птицефабрика",
    "НурлатАгро",
)
GROUP_TO_FIRM: dict[int, str] = {
    2: "АФ Чистопольская",
    4: "АФ Чистопольская",
    12: "АФ Чистопольская",
    11: "АФ Чистопольская",
    3: "АгроРаздолье",
    5: "АгроРаздолье",
    6: "Аксу-Агро",
    8: "Аксу-Агро",
    9: "Актай",
    7: "Дрожжаное Рассвет Агро",
    10: "Дрожжаное Рассвет Агро",
    17: "Лениногорская Птицефабрика",
    18: "НурлатАгро",
    30: "НурлатАгро",
    27: "НурлатАгро",
}


class FieldHarvestError(Exception):
    pass


def parse_firms(firms: list[str] | None) -> set[str] | None:
    """None — без фильтра. Иначе только известные агрофирмы, хотя бы одна."""
    if firms is None:
        return None
    chosen: list[str] = []
    for name in firms:
        label = str(name or "").strip()
        if not label:
            continue
        if label not in FIRM_ORDER:
            raise FieldHarvestError(f"Неизвестная агрофирма: {label}.")
        if label not in chosen:
            chosen.append(label)
    if not chosen:
        raise FieldHarvestError("Выберите хотя бы одну агрофирму.")
    return set(chosen)


def field_firm(field: dict) -> str | None:
    return GROUP_TO_FIRM.get(_int(field.get("field_group_id")))


def parse_year(year: int) -> int:
    try:
        value = int(year)
    except (TypeError, ValueError) as exc:
        raise FieldHarvestError("Укажите год сезона.") from exc
    if value < 2000 or value > 2100:
        raise FieldHarvestError("Год сезона вне диапазона 2000–2100.")
    return value


def available_field_ids(items: list[dict], year: int, on: date | None = None) -> set[int]:
    """Поля, доступные в сезоне. Та же проверка, что в сводках 1, 5 и 9."""
    season_start = date(year, 1, 1)
    season_end = on or (date.today() if year == date.today().year else date(year, 12, 31))
    found: set[int] = set()
    for item in items:
        if not item.get("available"):
            continue
        if str(item.get("historyable_type") or "Field") != "Field":
            continue
        fid = _int(item.get("historyable_id"))
        if not fid:
            continue
        started = _day(item.get("event_start_at"))
        if started is None or started > season_end:
            continue
        ended = _day(item.get("event_end_at"))
        if ended is not None and ended < season_start:
            continue
        found.add(fid)
    return found


def sown_hectares(operations: list[dict]) -> dict[int, float]:
    """Фактический посев, га, по полю."""
    totals: dict[int, float] = defaultdict(float)
    for operation in operations:
        if str(operation.get("status") or "") not in SOWN_STATUSES:
            continue
        if _int(operation.get("work_type_id")) not in SOWING_WORK_TYPE_IDS:
            continue
        area = _num(operation.get("completed_area"))
        fid = _int(operation.get("field_id"))
        if fid and area > 0:
            totals[fid] += area
    return totals


def assemble(
    fields: list[dict],
    history: list[dict],
    crops: list[dict] | None = None,
    available_ids: set[int] | None = None,
    sown_by_field: dict[int, float] | None = None,
    firms: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Одна строка на поле сезона с культурой, сортом или весом."""
    crop_names = _crop_names(crops or [])
    sown = sown_by_field or {}
    by_id: dict[int, dict] = {}
    for field in fields:
        fid = _int(field.get("id"))
        if not fid or _archived(field):
            continue
        if available_ids is not None and fid not in available_ids:
            continue
        if firms is not None and field_firm(field) not in firms:
            continue
        by_id[fid] = field

    grouped: dict[int, list[dict]] = defaultdict(list)
    for item in history:
        if not _season_item(item):
            continue
        fid = _int(item.get("field_id"))
        if fid in by_id:
            grouped[fid].append(item)

    rows: list[dict[str, Any]] = []
    for fid, items in grouped.items():
        field = by_id[fid]
        name = str(field.get("name") or "").strip() or str(fid)
        varieties: list[str] = []
        cultures: list[str] = []
        weight = 0.0
        sowing_on: date | None = None
        harvest_on: date | None = None
        productivity: tuple[date | None, float] | None = None
        for item in items:
            variety = str(item.get("variety") or "").strip()
            if variety and variety not in varieties:
                varieties.append(variety)
            culture = crop_names.get(_int(item.get("crop_id")), "")
            if culture and culture not in cultures:
                cultures.append(culture)
            weight += _num(item.get("harvested_weight"))
            sown_day = _day(item.get("sowing_date"))
            if sown_day is not None and (sowing_on is None or sown_day < sowing_on):
                sowing_on = sown_day
            harvested_day = _day(item.get("harvesting_date"))
            if harvested_day is not None and (harvest_on is None or harvested_day > harvest_on):
                harvest_on = harvested_day
            if item.get("productivity") not in (None, ""):
                candidate = (harvested_day, round(_num(item.get("productivity")), 2))
                if productivity is None or (harvested_day or date.min) >= (productivity[0] or date.min):
                    productivity = candidate
        area = round(_num(field.get("tillable_area")), 2)
        weight = round(weight, 3)
        rows.append(
            {
                "field": name,
                "area_ha": area,
                "crop": ", ".join(cultures),
                "variety": ", ".join(varieties),
                "sown_ha": round(sown.get(fid, 0.0), 2),
                "sowing_on": sowing_on,
                "harvest_on": harvest_on,
                "weight_t": weight,
                "yield_c_ha": None if productivity is None else productivity[1],
            }
        )
    rows.sort(key=lambda row: row["field"].casefold())
    return rows


def variety_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, float]] = {}
    for row in rows:
        key = str(row.get("variety") or "").strip() or NO_VARIETY
        bucket = buckets.setdefault(
            key,
            {"area_ha": 0.0, "sown_ha": 0.0, "weight_t": 0.0, "yield_area": 0.0, "yield_product": 0.0},
        )
        bucket["area_ha"] += _num(row.get("area_ha"))
        bucket["sown_ha"] += _num(row.get("sown_ha"))
        bucket["weight_t"] += _num(row.get("weight_t"))
        if row.get("yield_c_ha") is not None:
            area_for_yield = _num(row.get("area_ha"))
            bucket["yield_area"] += area_for_yield
            bucket["yield_product"] += _num(row.get("yield_c_ha")) * area_for_yield
    summary = []
    for name, values in buckets.items():
        area = round(values["area_ha"], 2)
        sown = round(values["sown_ha"], 2)
        weight = round(values["weight_t"], 3)
        summary.append(
            {
                "variety": name,
                "area_ha": area,
                "sown_ha": sown,
                "weight_t": weight,
                "yield_c_ha": _weighted_yield(values["yield_product"], values["yield_area"]),
            }
        )
    summary.sort(key=lambda row: (row["variety"] == NO_VARIETY, row["variety"].casefold()))
    return summary


def build_excel(year: int, firms: list[str] | None = None) -> tuple[bytes, str]:
    season = parse_year(year)
    chosen = parse_firms(firms)
    with _client() as client:
        fields = _pages(client, "fields")
        history = _pages(client, "history_items", {"year": season})
        crops = _pages(client, "crops")
        inventory = _pages(client, "inventory_history_items", {"historyable_type": "Field"})
        operations = _sowing_operations(client, season)
    rows = assemble(
        fields,
        history,
        crops,
        available_field_ids(inventory, season),
        sown_hectares(operations),
        chosen,
    )
    payload = workbook(rows, variety_summary(rows))
    return payload, f"Валовый_сбор_{season}.xlsx"


def workbook(rows: list[dict[str, Any]], summary: list[dict[str, Any]]) -> bytes:
    book = Workbook()
    fields_sheet = book.active
    fields_sheet.title = "Поля"
    _write_sheet(
        fields_sheet,
        FIELD_HEADERS,
        [
            (
                row["field"],
                row["area_ha"],
                row["crop"],
                row["variety"],
                row["sown_ha"],
                row["sowing_on"],
                row["harvest_on"],
                row["weight_t"],
                row["weight_t"],
                row["yield_c_ha"],
            )
            for row in rows
        ],
        (28, 16, 28, 36, 16, 16, 16, 22, 14, 20),
        area_col=2,
        sown_col=5,
        weight_col=8,
        yield_col=10,
        date_cols=(6, 7),
        harvest_col=9,
    )
    variety_sheet = book.create_sheet("По сортам")
    _write_sheet(
        variety_sheet,
        VARIETY_HEADERS,
        [
            (row["variety"], row["area_ha"], row["sown_ha"], row["weight_t"], row["yield_c_ha"])
            for row in summary
        ],
        (36, 16, 16, 22, 20),
        area_col=2,
        sown_col=3,
        weight_col=4,
        yield_col=5,
    )
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _write_sheet(
    sheet,
    headers: tuple[str, ...],
    values: list[tuple],
    widths: tuple[int, ...],
    area_col: int,
    sown_col: int,
    weight_col: int,
    yield_col: int,
    date_cols: tuple[int, ...] = (),
    harvest_col: int | None = None,
) -> None:
    header_font = Font(bold=True)
    sheet.append(list(headers))
    for cell in sheet[1]:
        cell.font = header_font
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for row in values:
        sheet.append(list(row))
    area_sum = sum(_num(row[area_col - 1]) for row in values)
    sown_sum = sum(_num(row[sown_col - 1]) for row in values)
    weight_sum = sum(_num(row[weight_col - 1]) for row in values)
    total = [""] * len(headers)
    total[0] = "Итого"
    total[area_col - 1] = round(area_sum, 2)
    total[sown_col - 1] = round(sown_sum, 2)
    total[weight_col - 1] = round(weight_sum, 3)
    if harvest_col:
        total[harvest_col - 1] = round(weight_sum, 2)
    total[yield_col - 1] = _sheet_yield(values, area_col, yield_col)
    sheet.append(total)
    total_row = sheet.max_row
    for cell in sheet[total_row]:
        cell.font = Font(bold=True)
    for index in range(2, total_row + 1):
        sheet.cell(index, area_col).number_format = "0.00"
        sheet.cell(index, sown_col).number_format = "0.00"
        sheet.cell(index, weight_col).number_format = "0.000"
        if harvest_col and index < total_row:
            sheet.cell(index, harvest_col).number_format = "0.00"
        if harvest_col and index == total_row:
            sheet.cell(index, harvest_col).number_format = "0.00"
        sheet.cell(index, yield_col).number_format = "0.00"
        if index < total_row:
            for column in date_cols:
                sheet.cell(index, column).number_format = "DD.MM.YYYY"
    last_data = max(total_row - 1, 1)
    last_col = get_column_letter(len(headers))
    sheet.auto_filter.ref = f"A1:{last_col}{last_data}"
    sheet.freeze_panes = "A2"
    sheet.row_dimensions[1].height = 32
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width


def _weighted_yield(product: float, area: float) -> float | None:
    if area <= 0:
        return None
    return round(product / area, 2)


def _sheet_yield(values: list[tuple], area_col: int, yield_col: int) -> float | None:
    product = 0.0
    area = 0.0
    for row in values:
        value = row[yield_col - 1]
        if value is None or value == "":
            continue
        row_area = _num(row[area_col - 1])
        product += _num(value) * row_area
        area += row_area
    return _weighted_yield(product, area)


def _crop_names(crops: list[dict]) -> dict[int, str]:
    names: dict[int, str] = {}
    for crop in crops:
        cid = _int(crop.get("id"))
        name = str(crop.get("name") or "").strip()
        if cid and name:
            names[cid] = name
    return names


def _archived(field: dict) -> bool:
    return bool(field.get("archived"))


def _day(value: Any) -> date | None:
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _sowing_operations(client: httpx.Client, year: int) -> list[dict]:
    ids = ",".join(str(item) for item in SOWING_WORK_TYPE_IDS)
    return _pages(client, "agro_operations", {"season": year, "work_type_id": ids})


def _season_item(item: dict) -> bool:
    if item.get("crop_id"):
        return True
    if str(item.get("variety") or "").strip():
        return True
    return _num(item.get("harvested_weight")) > 0


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _client() -> httpx.Client:
    from app.config import get_cropwise_token

    token = (get_cropwise_token() or "").strip()
    if not token:
        raise FieldHarvestError("Не задан токен Cropwise.")
    base = (settings.cropwise_base_url or "https://operations.cropwise.com/api/v3").rstrip("/")
    return httpx.Client(
        base_url=base,
        headers={"X-User-Api-Token": token},
        timeout=CROPWISE_TIMEOUT,
    )


def _pages(client: httpx.Client, endpoint: str, params: dict | None = None) -> list[dict]:
    out: list[dict] = []
    from_id = 0
    base = dict(params or {})
    while True:
        query = {**base, "from_id": from_id, "limit": PAGE_LIMIT}
        response = client.get(f"/{endpoint}", params=query)
        response.raise_for_status()
        body = response.json()
        chunk = body.get("data") or []
        if not isinstance(chunk, list) or not chunk:
            break
        out.extend(item for item in chunk if isinstance(item, dict))
        meta = ((body.get("meta") or {}).get("response") or {})
        last = meta.get("last_record_id")
        obtained = int(meta.get("obtained_records") or len(chunk))
        if not last or obtained < PAGE_SIZE:
            break
        from_id = int(last) + 1
    return out
