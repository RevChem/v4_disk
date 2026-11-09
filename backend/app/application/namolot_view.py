"""Срез «Намолот» для кнопки Namolot: живой API 1С и группировка по агрофирме."""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.config import get_namolot_config

# Тонны в отчётах = quantity / 1000 (как в 10_main_harvest).
QTY_TO_TONNES = 1.0 / 1000.0
MAX_PERIOD_DAYS = 200
BRIEF_MAX_DAYS = 400
SHIFT_HOUR = 6
TZ_MSK = timezone(timedelta(hours=3))
_CACHE_TTL_SEC = 15 * 60
_YEAR_SUFFIX_RE = re.compile(r"\s*\d{2,4}\s*г?\.?\s*$")
_FIELD_CODE_RE = re.compile(r"^[^\s(]+")
# Латиница, внешне неотличимая от кириллицы: в 1С и Cropwise код поля набирают по-разному.
_FIELD_CODE_TRANSLATE = str.maketrans(
    {
        "A": "А",
        "B": "В",
        "C": "С",
        "E": "Е",
        "H": "Н",
        "K": "К",
        "M": "М",
        "O": "О",
        "P": "Р",
        "T": "Т",
        "X": "Х",
        "Y": "У",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)
_FIELD_INDEX_TTL_SEC = 6 * 60 * 60

# Организация из Namolot → агрофирма холдинга.
# Номер «Агрофирма N» важнее названия: в 1С строка часто без кавычек и в другом порядке слов.
_NUMBER_TO_FIRM: dict[int, str] = {
    2: "АФ Чистопольская",
    3: "АгроРаздолье",
    4: "Аксу-Агро",
    5: "Дрожжаное Рассвет Агро",
    14: "НурлатАгро",
    15: "НурлатАгро",
}
FIRM_ORDER: tuple[str, ...] = (
    "АФ Чистопольская",
    "АгроРаздолье",
    "Аксу-Агро",
    "Актай",
    "Дрожжаное Рассвет Агро",
    "Лениногорская Птицефабрика",
    "НурлатАгро",
)
OTHER_FIRM = "Прочие"

_ORG_NUM_RE = re.compile(r"агрофирм\w*\s+(\d+)(?:\s*/\s*(\d+))?")
_WORD_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"чистополь"), "АФ Чистопольская"),
    (re.compile(r"(^|\s)полив($|\s)"), "АФ Чистопольская"),
    (re.compile(r"раздолье"), "АгроРаздолье"),
    (re.compile(r"(^|\s)аксу($|\s)"), "Аксу-Агро"),
    (re.compile(r"(^|\s)актай($|\s)"), "Актай"),
    (re.compile(r"дрожжан"), "Дрожжаное Рассвет Агро"),
    (re.compile(r"лениногор"), "Лениногорская Птицефабрика"),
)

COLUMNS: tuple[tuple[str, str], ...] = (
    ("spp", "Поле"),
    ("spp_guid", "ID поля"),
    ("combine_harvester", "Комбайн/Трактор"),
    ("combine_harvester_guid", "ID комбайна"),
    ("combine_harvester_id", "Госзнак"),
    ("machine_operator", "Механизатор"),
    ("machine_operator_guid", "ID механизатора"),
    ("quantity", "Намолот"),
    ("date", "Дата"),
    ("organization", "Организация"),
    ("organization_guid", "ID организации"),
    ("crop", "Культура"),
    ("crop_guid", "ID культуры"),
)
DATE_FIELDS = {"date"}

_cache_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


def _norm_org(text: object) -> str:
    raw = str(text or "").casefold().replace("ё", "е")
    raw = raw.replace("«", " ").replace("»", " ").replace('"', " ").replace("'", " ")
    raw = raw.replace("/", " / ")
    raw = re.sub(r"[^0-9a-zа-я/\s]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def field_base_code(name: object) -> str:
    """Код поля: всё до пробела или «(», как в сводках 10 и 14.

    «АР-03-16 Соя 26г.» и «АР-03-16» — одно поле. Звёздочка в начале и
    латинские двойники кириллицы не мешают сравнению с Cropwise.
    """
    raw = str(name or "").strip().lstrip("*").strip()
    if not raw:
        return ""
    match = _FIELD_CODE_RE.match(raw)
    if not match:
        return ""
    code = match.group(0).upper().lstrip("*").replace("Ё", "Е")
    return code.translate(_FIELD_CODE_TRANSLATE)


def _transfer_firm(code: str, rec: dict) -> str | None:
    """Фирма поля на дату записи, если поле передавали между фирмами (plans.FIELD_FIRM_TRANSFERS)."""
    if not code:
        return None
    try:
        from plans import FIELD_FIRM_TRANSFERS, firm_override_for_field
    except Exception:
        return None
    if code not in FIELD_FIRM_TRANSFERS:
        return None
    return firm_override_for_field(code, msk_wall(rec.get("date")))


def record_firm(rec: dict, index: dict[str, str] | None = None) -> str | None:
    """Агрофирма записи: сначала код поля spp, затем узнаваемая организация.

    Неузнанное не складывается в «Прочие»: в сводках такие тонны тоже не
    попадают ни в одну агрофирму.
    """
    code = field_base_code(rec.get("spp"))
    catalog = index or {}
    moved = _transfer_firm(code, rec)
    if moved:
        return moved
    if code and code in catalog:
        return catalog[code]
    firm = holding_firm(rec.get("organization"))
    if firm == OTHER_FIRM:
        return None
    return firm


def holding_firm(organization: object) -> str:
    """Агрофирма холдинга по строке organization. Неузнанное — «Прочие»."""
    raw = _norm_org(organization)
    if not raw:
        return OTHER_FIRM
    match = _ORG_NUM_RE.search(raw)
    if match:
        number = int(match.group(1))
        firm = _NUMBER_TO_FIRM.get(number)
        if firm:
            return firm
    for pattern, firm in _WORD_RULES:
        if pattern.search(raw):
            return firm
    return OTHER_FIRM


def _round_t(kg: float) -> float:
    return round(float(kg) * QTY_TO_TONNES, 3)


def summarize(records: list[dict]) -> dict:
    """Тонны и число записей: агрофирма → организация → культура."""
    bucket: dict[str, dict[str, dict[str, list]]] = {}
    total_kg = 0.0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        try:
            qty = float(rec.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        total_kg += qty
        firm = holding_firm(rec.get("organization"))
        org = str(rec.get("organization") or "").strip() or "без организации"
        crop = str(rec.get("crop") or "").strip() or "—"
        org_map = bucket.setdefault(firm, {})
        crop_map = org_map.setdefault(org, {})
        cell = crop_map.setdefault(crop, [0.0, 0])
        cell[0] += qty
        cell[1] += 1

    def crops_out(crop_map: dict[str, list]) -> list[dict]:
        rows = [
            {"crop": name, "tonnes": _round_t(kg), "records": count}
            for name, (kg, count) in crop_map.items()
        ]
        rows.sort(key=lambda row: (-row["tonnes"], row["crop"]))
        return rows

    order = list(FIRM_ORDER)
    if OTHER_FIRM in bucket:
        order.append(OTHER_FIRM)
    firms = []
    for firm in order:
        orgs = bucket.get(firm)
        if not orgs:
            continue
        org_rows = []
        firm_kg = 0.0
        firm_records = 0
        for org, crop_map in orgs.items():
            org_kg = sum(cell[0] for cell in crop_map.values())
            org_records = sum(cell[1] for cell in crop_map.values())
            firm_kg += org_kg
            firm_records += org_records
            org_rows.append(
                {
                    "organization": org,
                    "tonnes": _round_t(org_kg),
                    "records": org_records,
                    "crops": crops_out(crop_map),
                }
            )
        org_rows.sort(key=lambda row: (-row["tonnes"], row["organization"]))
        firms.append(
            {
                "firm": firm,
                "tonnes": _round_t(firm_kg),
                "records": firm_records,
                "organizations": org_rows,
            }
        )
    return {
        "records": sum(firm["records"] for firm in firms),
        "tonnes": _round_t(total_kg),
        "firms": firms,
    }


def msk_wall(value) -> datetime | None:
    """Время записи как настенные часы MSK. Наивные строки 1С уже в MSK."""
    if value is None or value == "" or value == 0:
        return None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return msk_wall(int(value.strip()))
    if isinstance(value, (int, float)):
        ts = value / 1000 if abs(value) > 10**12 else float(value)
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK).replace(tzinfo=None)
        except (ValueError, OSError, OverflowError):
            return None
    parsed = parse_date_value(value)
    if not isinstance(parsed, datetime):
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(TZ_MSK).replace(tzinfo=None)


def _surname(operator: str) -> str:
    parts = operator.split()
    return parts[0] if parts else ""


def record_rows(records: list[dict], index: dict[str, str] | None = None) -> list[dict]:
    """Компактные строки для фильтра: агрофирма, культура, госномер, фамилия, тонны."""
    rows: list[dict] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        firm = record_firm(rec, index)
        if not firm:
            continue
        try:
            qty = float(rec.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        operator = str(rec.get("machine_operator") or "").strip()
        wall = msk_wall(rec.get("date"))
        rows.append(
            {
                "at": wall.strftime("%Y-%m-%dT%H:%M:%S") if wall else None,
                "firm": firm,
                "crop": canonical_crop(rec.get("crop")) or "—",
                "plate": str(rec.get("combine_harvester_id") or "").strip(),
                "operator": operator,
                "surname": _surname(operator),
                "tonnes": _round_t(qty),
                "field": str(rec.get("spp") or "").strip(),
            }
        )
    return rows


def _norm_plate(value: object) -> str:
    """Как normPlate в frontend/src/utils/namolotFilter.js."""
    return re.sub(r"[\s-]+", "", str(value or "").lower())


def _norm_text(value: object) -> str:
    """Как normText: нижний регистр и «ё» → «е»."""
    return str(value or "").casefold().replace("ё", "е").strip()


def _range_stamp(day: str, clock: str, empty_clock: str) -> str:
    day = str(day or "").strip()
    if not day:
        return ""
    clock = str(clock or "").strip() or empty_clock
    return f"{day}T{clock}"[:16]


def _id_set(value: object) -> set[int] | None:
    if value is None:
        return None
    found: set[int] = set()
    for item in value:  # type: ignore[union-attr]
        try:
            found.add(int(item))
        except (TypeError, ValueError):
            continue
    return found


def _label_set(value: object) -> set[str] | None:
    if not value:
        return None
    found = {_norm_text(item) for item in value if _norm_text(item)}  # type: ignore[union-attr]
    return found or None


def filter_record_rows(
    rows: list[dict],
    *,
    firm: str = "",
    crop: str = "",
    date_from: str = "",
    time_from: str = "",
    date_to: str = "",
    time_to: str = "",
    plate: str = "",
    surname: str = "",
    operation: str = "",
    firms: list[str] | None = None,
    crew: str = "",
    group_ids: set[int] | list[int] | None = None,
    group_names: list[str] | None = None,
    detail_ids: set[int] | list[int] | None = None,
    detail_labels: list[str] | None = None,
) -> list[dict]:
    """Те же необязательные условия, что у кнопки Namolot. Пустые поля срез не сужают.

    firms — несколько агрофирм. crew: hired (группа машин 7), own или пусто (все).

    operation — точное агрозадание из списка, например «3.1 Опрыскивание». Пустое значение не отсекает строки.
    group_ids / group_names — группы из plans.WORK_GROUPS: строка подходит, если её work_type_id
    входит в любую выбранную группу или подпись совпадает с названием группы.
    detail_ids / detail_labels — отдельные виды работ. Если заданы и группы, и виды работ,
    остаются строки, которые подходят под оба списка.
    """
    firm_name = str(firm or "").strip()
    firm_names: set[str] = set()
    for raw in firms or []:
        text = str(raw or "").strip()
        if text:
            firm_names.add(text)
    if firm_name:
        firm_names.add(firm_name)
    crew_name = str(crew or "").strip().lower()
    if crew_name not in ("hired", "own"):
        crew_name = ""
    crop_name = str(crop or "").strip()
    plate_q = _norm_plate(plate)
    name_q = _norm_text(surname)
    operation_q = _norm_text(operation)
    group_id_set = _id_set(group_ids)
    group_name_set = _label_set(group_names)
    detail_id_set = _id_set(detail_ids)
    detail_label_set = _label_set(detail_labels)
    group_on = group_id_set is not None or group_name_set is not None
    detail_on = detail_id_set is not None or detail_label_set is not None
    start = _range_stamp(date_from, time_from, "00:00")
    end = _range_stamp(date_to, time_to, "23:59")
    picked: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if firm_names and str(row.get("firm") or "") not in firm_names:
            continue
        if crew_name:
            kind = "hired" if row.get("crew") == "hired" else "own"
            if kind != crew_name:
                continue
        if crop_name and row.get("crop") != crop_name:
            continue
        stamp = str(row.get("at") or "")[:16]
        if (start or end) and not stamp:
            continue
        if start and stamp < start:
            continue
        if end and stamp > end:
            continue
        if plate_q and plate_q not in _norm_plate(row.get("plate")):
            continue
        if name_q and name_q not in _norm_text(f"{row.get('surname') or ''} {row.get('operator') or ''}"):
            continue
        if operation_q and _norm_text(row.get("operation")) != operation_q:
            continue
        work_type_id = row.get("work_type_id")
        work_type_ok = isinstance(work_type_id, int)
        operation_name = _norm_text(row.get("operation"))
        if group_on:
            in_group = bool(group_id_set is not None and work_type_ok and work_type_id in group_id_set)
            named = bool(group_name_set and operation_name in group_name_set)
            if not (in_group or named):
                continue
        if detail_on:
            in_detail = bool(detail_id_set is not None and work_type_ok and work_type_id in detail_id_set)
            labeled = bool(detail_label_set and operation_name in detail_label_set)
            if not (in_detail or labeled):
                continue
        picked.append(row)
    return picked


def _ask_max_rows() -> int:
    try:
        return max(1, int(os.environ.get("NAMOLOT_ASK_MAX_ROWS", "200")))
    except ValueError:
        return 200


def _tonnes(row: dict) -> float:
    try:
        return float(row.get("tonnes") or 0)
    except (TypeError, ValueError):
        return 0.0


def _top_tonnes(rows: list[dict], key: str, limit: int = 12) -> str:
    buckets: dict[str, float] = {}
    for row in rows:
        name = str(row.get(key) or "").strip() or "—"
        buckets[name] = buckets.get(name, 0.0) + _tonnes(row)
    ranked = sorted(buckets.items(), key=lambda item: item[1], reverse=True)
    parts = [f"{name}={value:.3f}" for name, value in ranked[:limit]]
    if len(ranked) > limit:
        rest = sum(value for _name, value in ranked[limit:])
        parts.append(f"…ещё {len(ranked) - limit}={rest:.3f}")
    return "; ".join(parts)


def format_ask_table(rows: list[dict], *, max_rows: int | None = None) -> tuple[str, dict]:
    """TSV для вопроса. Итоги считаются по всем строкам среза, даже если таблица усечена."""
    limit = _ask_max_rows() if max_rows is None else max(1, int(max_rows))
    clean = [row for row in rows if isinstance(row, dict)]
    total = round(sum(_tonnes(row) for row in clean), 3)
    ordered = sorted(clean, key=lambda row: (_tonnes(row), str(row.get("at") or "")), reverse=True)
    shown = ordered[:limit]
    lines = ["дата\tагрофирма\tкультура\tгосномер\tмеханизатор\tполе\tтонны"]
    for row in shown:
        lines.append(
            "\t".join(
                [
                    str(row.get("at") or "—"),
                    str(row.get("firm") or "—"),
                    str(row.get("crop") or "—"),
                    str(row.get("plate") or "—"),
                    str(row.get("operator") or "—"),
                    str(row.get("field") or "—"),
                    f"{_tonnes(row):.3f}",
                ]
            )
        )
    truncated = len(clean) > len(shown)
    if truncated:
        hidden = ordered[len(shown):]
        hidden_t = round(sum(_tonnes(row) for row in hidden), 3)
        lines.append(
            f"… не показано {len(hidden)} записей на {hidden_t:.3f} т (мельче показанных)"
        )
    lines.append(f"Итого: {len(clean)} записей, {total:.3f} т")
    if clean:
        lines.append("По агрофирмам: " + _top_tonnes(clean, "firm"))
        lines.append("По культурам: " + _top_tonnes(clean, "crop"))
    return "\n".join(lines), {
        "rows_total": len(clean),
        "rows_sent": len(shown),
        "tonnes_total": total,
        "truncated": truncated,
    }


ASK_SYSTEM = (
    "Ты помощник агронома. Отвечай только по таблице намолота Namolot: "
    "каждая строка — одно взвешивание (дата, агрофирма, культура, госномер, механизатор, поле, тонны). "
    "Не выдумывай числа, госномера и фамилии. Если таблица усечена, суммы бери из строк «Итого» и «По …» — "
    "они посчитаны по всем строкам среза. Если данных недостаточно — так и скажи. Отвечай кратко, по-русски."
)


def ask_records(rows: list[dict], question: str, *, use_llm: bool = True) -> dict:
    """Ответ по уже отфильтрованным строкам. Пустой срез и use_llm=False модель не вызывают."""
    table, meta = format_ask_table(rows)
    question = str(question or "").strip()
    out = {
        "ok": True,
        "available": True,
        "llm_used": False,
        "question": question,
        "answer": table if meta["rows_total"] else "По заданному фильтру данных нет.",
        "usage": None,
        "model": None,
        "error": None,
        **meta,
    }
    if not use_llm or not question or not meta["rows_total"]:
        return out
    from app.adapters.outbound.proverka.service import _ensure_plan_path

    _ensure_plan_path()
    from proverka.ai.facts_qa import answer_tsv

    answered = answer_tsv(question, table, system=ASK_SYSTEM)
    out.update(
        ok=answered.get("ok", True),
        llm_used=bool(answered.get("llm_used")),
        answer=answered.get("answer") or out["answer"],
        usage=answered.get("usage"),
        model=answered.get("model"),
        error=answered.get("error"),
    )
    return out


def _auth_header(username: str, password: str) -> dict[str, str]:
    if not (username and password):
        return {}
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _check_period(date_begin: date, date_end: date, *, max_days: int = MAX_PERIOD_DAYS) -> None:
    if date_end < date_begin:
        raise ValueError("Дата окончания раньше даты начала")
    if (date_end - date_begin).days > max_days:
        raise ValueError(f"Период длиннее {max_days} дней")


def fetch_live(date_begin: date, date_end: date, *, max_days: int = MAX_PERIOD_DAYS) -> list[dict]:
    """Прямой GET в Namolot. Кэш сводок не используется."""
    _check_period(date_begin, date_end, max_days=max_days)
    key = (date_begin.isoformat(), date_end.isoformat())
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL_SEC:
            return hit[1]

    base, user, password = get_namolot_config()
    if not password:
        raise ValueError("Не задан NAMOLOT_PASSWORD")
    response = requests.get(
        base,
        params={"date_begin": key[0], "date_end": key[1]},
        headers=_auth_header(user, password),
        timeout=(10, 180),
        proxies={"http": None, "https": None},
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("Namolot API вернул не список записей")
    records = [row for row in data if isinstance(row, dict)]
    with _cache_lock:
        _cache[key] = (time.monotonic(), records)
    return records


def build_view(date_begin: date, date_end: date) -> dict:
    records = fetch_live(date_begin, date_end)
    rows = record_rows(records, field_code_index())
    return {
        "date_begin": date_begin.isoformat(),
        "date_end": date_end.isoformat(),
        "records": len(rows),
        "tonnes": round(sum(row["tonnes"] for row in rows), 3),
        "rows": rows,
    }


def parse_date_value(value):
    """Дата записи Namolot для Excel. Пустое и 0 — не дата."""
    if value is None or value == "" or value == 0:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.lstrip("-").isdigit():
            return parse_date_value(int(stripped))
        try:
            return datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
                try:
                    return datetime.strptime(stripped, fmt)
                except ValueError:
                    continue
            return value
    if isinstance(value, (int, float)):
        ts = value / 1000 if abs(value) > 10**12 else value
        try:
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError, OverflowError):
            return None
    return value


def workbook_bytes(records: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Намолот"
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for col_idx, (_field, header) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    date_cols: set[int] = set()
    for row_idx, item in enumerate(records, start=2):
        for col_idx, (field, _header) in enumerate(COLUMNS, start=1):
            value = item.get(field, "")
            if field in DATE_FIELDS:
                value = parse_date_value(value)
                if isinstance(value, datetime):
                    date_cols.add(col_idx)
            ws.cell(row=row_idx, column=col_idx, value=value)

    for col_idx in date_cols:
        for row in range(2, ws.max_row + 1):
            cell = ws.cell(row=row, column=col_idx)
            if isinstance(cell.value, datetime):
                cell.number_format = "DD.MM.YYYY HH:MM:SS"

    for col_idx, (_field, header) in enumerate(COLUMNS, start=1):
        max_len = len(header)
        limit = min(ws.max_row, 200)
        for row in range(2, limit + 1):
            val = ws.cell(row=row, column=col_idx).value
            if val is not None:
                max_len = max(max_len, len(str(val)))
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 45)
    ws.freeze_panes = "A2"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _crop_key(text: object) -> str:
    raw = re.sub(r"\s+", " ", str(text or "").strip())
    raw = _YEAR_SUFFIX_RE.sub("", raw).strip()
    return raw.casefold().replace("ё", "е")


# Те же расхождения, что в сводке 10: род («озимый»/«озимая») и «Трикитакле».
_CROP_ALIASES = {
    "тритикале озимый": "Тритикале озимая",
    "тритикале яровой": "Тритикале яровая",
    "трикитакле озимая": "Тритикале озимая",
    "трикитакле озимый": "Тритикале озимая",
    "свекла": "Свекла столовая",
    "свекла столовая": "Свекла столовая",
}
_CANONICAL_CROPS = (
    "Рапс озимый",
    "Рапс яровой",
    "Пшеница озимая",
    "Пшеница яровая",
    "Тритикале озимая",
    "Тритикале яровая",
    "Соя",
    "Подсолнечник",
    "Кукуруза на зерно",
    "Картофель",
    "Морковь",
    "Свекла столовая",
)


def canonical_crop(text: object) -> str | None:
    """Имя культуры как в сводке 10. Незнакомое имя остаётся как в 1С."""
    raw = re.sub(r"\s+", " ", str(text or "").strip())
    key = _crop_key(raw)
    if not key:
        return None
    if "кукуруз" in key:
        return "Кукуруза на зерно"
    aliased = _CROP_ALIASES.get(key)
    if aliased:
        return aliased
    if "тритикал" in key or "трикитакл" in key:
        return "Тритикале озимая"
    for name in _CANONICAL_CROPS:
        if _crop_key(name) == key:
            return name
    for name in _CANONICAL_CROPS:
        low = _crop_key(name)
        if low in key or key in low:
            return name
    return raw


def same_crop(record_crop: object, wanted: str) -> bool:
    got = canonical_crop(record_crop)
    need = canonical_crop(wanted)
    if not got or not need:
        return False
    return _crop_key(got) == _crop_key(need)


def shift_cutoff(report_date: date) -> datetime:
    """06:00 даты сводки: записи этой смены идут до 05:59."""
    return datetime(report_date.year, report_date.month, report_date.day, SHIFT_HOUR)


def harvest_season_start(report_date: date) -> date:
    from app.legacy.manual_overrides import combine_season_start_for_date

    return combine_season_start_for_date(report_date)


def _shown_end(cutoff: datetime) -> str:
    return (cutoff - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")


def _field_codes_path():
    from pathlib import Path

    from app.config import settings

    return Path(settings.upload_dir) / ".namolot_field_codes.json"


def _read_field_codes() -> dict[str, str] | None:
    path = _field_codes_path()
    try:
        if not path.is_file():
            return None
        if time.time() - path.stat().st_mtime > _FIELD_INDEX_TTL_SEC:
            return None
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    return {str(code): str(firm) for code, firm in data.items() if code and firm}


def _write_field_codes(index: dict[str, str]) -> None:
    path = _field_codes_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return


def _load_field_codes() -> dict[str, str]:
    """Код поля Cropwise → агрофирма. Та же привязка, что в сводках 10 и 14."""
    from app.config import get_cropwise_token, settings
    from app.legacy.works_summary import _build_field_firm, _get_all

    token = get_cropwise_token()
    if not token:
        return {}
    base = (settings.cropwise_base_url or "").strip() or "https://operations.cropwise.com/api/v3"
    session = requests.Session()
    session.headers.update({"X-User-Api-Token": token})
    fields = _get_all(session, base, "fields")
    groups = _get_all(session, base, "field_groups")
    folders = _get_all(session, base, "group_folders")
    by_id = _build_field_firm(fields, groups, folders)
    index: dict[str, str] = {}
    for field in fields:
        fid = field.get("id")
        if fid is None:
            continue
        try:
            firm = by_id.get(int(fid))
        except (TypeError, ValueError):
            continue
        if not firm:
            continue
        code = field_base_code(field.get("name") or field.get("title"))
        if code and code not in index:
            index[code] = firm
    return index


def field_code_index() -> dict[str, str]:
    """Справочник кодов полей. Сеть Cropwise — только если файла нет или он старше 6 часов."""
    cached = _read_field_codes()
    if cached:
        return cached
    try:
        loaded = _load_field_codes()
    except Exception:
        return {}
    if loaded:
        _write_field_codes(loaded)
    return loaded


def _select_brief(
    records: list[dict],
    report_date: date,
    firm: str,
    crop: str,
    index: dict[str, str] | None = None,
) -> tuple[dict, list[dict]]:
    """Тонны одной агрофирмы и культуры и сырые записи этого среза."""
    cutoff = shift_cutoff(report_date)
    day0 = cutoff - timedelta(days=1)
    season_day = harvest_season_start(report_date)
    season0 = datetime(season_day.year, season_day.month, season_day.day, SHIFT_HOUR)
    day_kg = season_kg = 0.0
    day_n = season_n = 0
    kept: list[dict] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if record_firm(rec, index) != firm:
            continue
        if not same_crop(rec.get("crop"), crop):
            continue
        wall = msk_wall(rec.get("date"))
        if wall is None or wall < season0 or wall >= cutoff:
            continue
        try:
            qty = float(rec.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        season_kg += qty
        season_n += 1
        if day0 <= wall < cutoff:
            day_kg += qty
            day_n += 1
        kept.append(rec)
    summary = {
        "firm": firm,
        "crop": crop,
        "report_date": report_date.isoformat(),
        "day": {
            "from": day0.strftime("%Y-%m-%dT%H:%M:%S"),
            "to": _shown_end(cutoff),
            "tonnes": _round_t(day_kg),
            "records": day_n,
        },
        "season": {
            "from": season0.strftime("%Y-%m-%dT%H:%M:%S"),
            "to": _shown_end(cutoff),
            "tonnes": _round_t(season_kg),
            "records": season_n,
        },
    }
    return summary, kept


def brief_from_records(
    records: list[dict],
    report_date: date,
    firm: str,
    crop: str,
    index: dict[str, str] | None = None,
) -> dict:
    summary, kept = _select_brief(records, report_date, firm, crop, index)
    summary["rows"] = record_rows(kept, index)
    return summary


def _require_firm_crop(firm: str, crop: str) -> tuple[str, str]:
    firm = str(firm or "").strip()
    crop = str(crop or "").strip()
    if not firm or not crop:
        raise ValueError("Нужны агрофирма и культура")
    return firm, crop


def build_brief(report_date: date, firm: str, crop: str) -> dict:
    firm, crop = _require_firm_crop(firm, crop)
    index = field_code_index()
    start = harvest_season_start(report_date)
    records = fetch_live(start, report_date, max_days=BRIEF_MAX_DAYS)
    return brief_from_records(records, report_date, firm, crop, index)


def season_from_records(
    records: list[dict],
    report_date: date,
    index: dict[str, str] | None = None,
) -> dict:
    """Все записи сезона до 05:59, без отбора по агрофирме и культуре."""
    cutoff = shift_cutoff(report_date)
    day0 = cutoff - timedelta(days=1)
    season_day = harvest_season_start(report_date)
    season0 = datetime(season_day.year, season_day.month, season_day.day, SHIFT_HOUR)
    kept: list[dict] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        wall = msk_wall(rec.get("date"))
        if wall is None or wall < season0 or wall >= cutoff:
            continue
        kept.append(rec)
    rows = record_rows(kept, index)
    return {
        "report_date": report_date.isoformat(),
        "day": {
            "from": day0.strftime("%Y-%m-%dT%H:%M:%S"),
            "to": _shown_end(cutoff),
            "until": cutoff.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "season": {
            "from": season0.strftime("%Y-%m-%dT%H:%M:%S"),
            "to": _shown_end(cutoff),
            "until": cutoff.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "rows": rows,
        "records": len(rows),
    }


def build_season(report_date: date) -> dict:
    index = field_code_index()
    start = harvest_season_start(report_date)
    records = fetch_live(start, report_date, max_days=BRIEF_MAX_DAYS)
    return season_from_records(records, report_date, index)


def _fmt_table_at(value: object) -> str:
    text = str(value or "")
    if "T" not in text:
        return text or "—"
    day, clock = text.split("T", 1)
    parts = day.split("-")
    if len(parts) != 3:
        return text
    year, month, date = parts
    return f"{date}.{month}.{year} {clock[:5]}".strip()


def _join_cell(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "—"
    text = str(value or "").strip()
    return text or "—"


def table_excel(mode: str, rows: list[dict]) -> bytes:
    """Лист в том виде, в каком таблица уже отфильтрована на экране."""
    wb = Workbook()
    ws = wb.active
    if mode == "plates":
        ws.title = "Госномера"
        headers = ["№", "Госномер", "Намолот, т", "Записей", "Фамилии", "Культуры", "Агрофирмы"]
    elif mode == "surnames":
        ws.title = "Фамилии"
        headers = ["№", "Фамилия", "Намолот, т", "Записей", "Госномера", "Культуры", "Агрофирмы"]
    else:
        ws.title = "Записи"
        headers = ["№", "Дата", "Агрофирма", "Культура", "Госномер", "Механизатор", "Поле", "Намолот, т"]
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for row_idx, row in enumerate(rows, start=1):
        if mode in {"plates", "surnames"}:
            values = [
                row_idx,
                row.get("key") or "—",
                row.get("tonnes"),
                row.get("records"),
                _join_cell(row.get("names") if mode == "plates" else row.get("plates")),
                _join_cell(row.get("crops")),
                _join_cell(row.get("firms")),
            ]
        else:
            values = [
                row_idx,
                _fmt_table_at(row.get("at")),
                row.get("firm") or "—",
                row.get("crop") or "—",
                row.get("plate") or "—",
                row.get("operator") or "—",
                row.get("field") or "—",
                row.get("tonnes"),
            ]
        for col_idx, value in enumerate(values, start=1):
            ws.cell(row=row_idx + 1, column=col_idx, value=value)
    for col_idx, header in enumerate(headers, start=1):
        width = len(header)
        for row in range(2, min(ws.max_row, 200) + 1):
            val = ws.cell(row=row, column=col_idx).value
            if val is not None:
                width = max(width, len(str(val)))
        ws.column_dimensions[get_column_letter(col_idx)].width = min(width + 2, 45)
    ws.freeze_panes = "A2"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_brief_excel(report_date: date, firm: str, crop: str) -> bytes:
    firm, crop = _require_firm_crop(firm, crop)
    start = harvest_season_start(report_date)
    records = fetch_live(start, report_date, max_days=BRIEF_MAX_DAYS)
    _summary, kept = _select_brief(records, report_date, firm, crop)
    return workbook_bytes(kept)


def build_excel(date_begin: date, date_end: date) -> bytes:
    return workbook_bytes(fetch_live(date_begin, date_end))


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
