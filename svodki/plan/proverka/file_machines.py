"""Число задействованной техники из уже сохранённых .xlsx сводок.

Так же, как сверка гектаров с Cropwise (file_values.py): сводки заново не
считаются, читается файл в uploads/<дата>/. Результат — операция → агрофирма →
число машин, ровно то, что видит пользователь в Excel.

Откуда берётся число:
- сводки 3–9, 11, 14: лист «Сводка», колонка «Количество задействованной
  техники…» / «Количество комбайнов на полях»;
- сводка 1: листы «Выраб. — <операция>» и «Выработка техники», та же колонка;
- сводка 2: на листе каждой операции таблица «КОЛИЧЕСТВО ЗАДЕЙСТВОВАННЫХ АГРЕГАТОВ»,
  берётся колонка даты отчёта («8 октября»), а без даты — крайняя правая.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover
    load_workbook = None  # type: ignore[assignment]

from proverka.file_values import (
    _FIRM_HEADERS,
    _SHEET_NAME,
    _TOTAL_LABEL,
    _canonical_firm,
    _flat_header,
    _group_for_sheet_section,
    _header_index,
    _read_failure,
    _section_title,
    find_report_xlsx,
)

# Сводки, в Excel которых числа машин нет. 10 и 13 — не «операционные»: у 10 листы по культурам
# («Общий намолот», «Рапс озимый», …), у 13 — обзор холдинга по культурам и фирмам; листа «Сводка»
# с колонкой техники там нет. Раньше они давали «файл не прочитан: нет листа «Сводка»».
REPORTS_WITHOUT_MACHINES_IN_FILE: frozenset[str] = frozenset({"10", "13"})

REPORT2_MACHINES_TITLE = "КОЛИЧЕСТВО ЗАДЕЙСТВОВАННЫХ АГРЕГАТОВ"
_RU_MONTHS_GEN = (
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

_MACHINE_HEADER_PREFIXES = (
    "количество задействованной техники",
    "кол-во задействованной техники",
    "количество комбайнов на полях",
    "количество собственной техники на полях",
)


class MachineFileMissing(RuntimeError):
    """Нет сохранённого .xlsx этой сводки за выбранную дату."""


def _is_machine_header(cell: Any) -> bool:
    text = _flat_header(cell).casefold()
    return any(text.startswith(prefix) for prefix in _MACHINE_HEADER_PREFIXES)


def _header_columns(values: list[Any]) -> tuple[int, int] | None:
    """(колонка агрофирмы, колонка числа машин). Берётся первая колонка машин."""
    firm_names = {_flat_header(name) for name in _FIRM_HEADERS}
    firm_idx: int | None = None
    machine_idx: int | None = None
    for i, cell in enumerate(values):
        if firm_idx is None and _flat_header(cell) in firm_names:
            firm_idx = i
        if machine_idx is None and _is_machine_header(cell):
            machine_idx = i
    if firm_idx is None or machine_idx is None:
        return None
    return firm_idx, machine_idx


def _count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value)))
    try:
        return int(round(float(str(value).replace(" ", "").replace(",", "."))))
    except ValueError:
        return None


def _summary_sheet_machines(ws: Any, report_id: str) -> dict[str, dict[str, int]]:
    """Лист «Сводка»: операция → агрофирма → число машин."""
    out: dict[str, dict[str, int]] = {}
    pending = ""
    current = ""
    cols: tuple[int, int] | None = None
    bucket: dict[str, int] = {}

    def flush() -> None:
        nonlocal bucket
        group = _group_for_sheet_section(current, _SHEET_NAME, report_id)
        if group and bucket:
            dest = out.setdefault(group, {})
            for firm, cnt in bucket.items():
                dest[firm] = dest.get(firm, 0) + cnt
        bucket = {}

    for row in ws.iter_rows(values_only=True):
        values = list(row)
        header = _header_columns(values)
        if header:
            flush()
            cols = header
            current = pending
            pending = ""
            continue
        # Блок культур на том же листе («Культура» / «Остаток, га») стоит в тех же колонках, что
        # агрофирма и техника. Без сброса «Свёкла — 21083 га остатка» читалась как 21083 машин
        # (так же устроено чтение гектаров в file_values._xlsx_summary_firm_areas).
        if _header_index(values, ("Культура",)) is not None:
            flush()
            cols = None
            current = ""
            pending = ""
            continue
        title = _section_title(values)
        if title:
            pending = title
            continue
        if cols is None or max(cols) >= len(values):
            continue
        label = str(values[cols[0]] or "").strip()
        if not label or label == _TOTAL_LABEL:
            continue
        cnt = _count(values[cols[1]])
        if cnt is None:
            continue
        firm = _canonical_firm(label)
        bucket[firm] = bucket.get(firm, 0) + cnt
    flush()
    return out


def _perf_sheet_machines(ws: Any) -> dict[str, int]:
    """Лист «Выраб. — …»: агрофирма → число машин (до строки «Итого»)."""
    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    cols: tuple[int, int] | None = None
    start = 0
    for i, row in enumerate(rows[:5]):
        cols = _header_columns(list(row))
        if cols:
            start = i + 1
            break
    if cols is None:
        return {}
    by_firm: dict[str, int] = {}
    for row in rows[start:]:
        if max(cols) >= len(row):
            continue
        label = str(row[cols[0]] or "").strip()
        if not label:
            continue
        if label == _TOTAL_LABEL:
            break
        cnt = _count(row[cols[1]])
        if cnt is None:
            continue
        firm = _canonical_firm(label)
        by_firm[firm] = by_firm.get(firm, 0) + cnt
    return by_firm


def _grouped_perf_machines(ws: Any) -> dict[str, dict[str, int]]:
    """Один лист с несколькими блоками (вспашка + глубокорыхление)."""
    from proverka.file_consistency import group_for_sheet

    out: dict[str, dict[str, int]] = {}
    current: str | None = None
    cols: tuple[int, int] | None = None
    bucket: dict[str, int] = {}
    for raw in ws.iter_rows(values_only=True):
        values = list(raw)
        header = _header_columns(values)
        if header:
            cols = header
            bucket = {}
            continue
        label = str(values[0] or "").strip() if values else ""
        named = group_for_sheet(label) if label and label != _TOTAL_LABEL else None
        if named:
            current = named
            cols = None
            continue
        if current is None or cols is None or not label or max(cols) >= len(values):
            continue
        if label == _TOTAL_LABEL:
            out[current] = bucket
            current = None
            continue
        cnt = _count(values[cols[1]])
        if cnt is None:
            continue
        firm = _canonical_firm(label)
        bucket[firm] = bucket.get(firm, 0) + cnt
    return out


def _report1_machines(wb: Any) -> dict[str, dict[str, int]]:
    from proverka.file_consistency import _perf_group

    out: dict[str, dict[str, int]] = {}
    for ws in wb.worksheets:
        title = str(ws.title or "").strip()
        if "Вспашка" in title and "Глубокорыхление" in title:
            for group, by_firm in _grouped_perf_machines(ws).items():
                if by_firm:
                    out.setdefault(group, {}).update(by_firm)
            continue
        group = _perf_group(title)
        if not group:
            continue
        by_firm = _perf_sheet_machines(ws)
        if by_firm:
            out.setdefault(group, {}).update(by_firm)
    return out


def _date_header(report_date: Any) -> str:
    return f"{report_date.day} {_RU_MONTHS_GEN[report_date.month]}"


def _report2_sheet_machines(ws: Any, report_date: Any = None) -> dict[str, int]:
    """Лист операции сводки 2: агрофирма → число машин за день отчёта."""
    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    title_idx = next(
        (
            i for i, r in enumerate(rows)
            if any(_flat_header(c).upper() == REPORT2_MACHINES_TITLE for c in r)
        ),
        None,
    )
    if title_idx is None:
        return {}
    hdr_idx = None
    for i in range(title_idx + 1, min(title_idx + 4, len(rows))):
        if any(_flat_header(c) in {_flat_header(n) for n in _FIRM_HEADERS} for c in rows[i]):
            hdr_idx = i
            break
    if hdr_idx is None:
        return {}
    header = rows[hdr_idx]
    firm_col = next(
        i for i, c in enumerate(header) if _flat_header(c) in {_flat_header(n) for n in _FIRM_HEADERS}
    )
    day_col = None
    if report_date is not None:
        wanted = _date_header(report_date)
        for i, c in enumerate(header):
            if _flat_header(c).casefold() == wanted:
                day_col = i
                break
    if day_col is None:
        filled = [i for i, c in enumerate(header) if c not in (None, "")]
        day_col = filled[-1] if filled else None
    if day_col is None or day_col <= firm_col:
        return {}
    out: dict[str, int] = {}
    for row in rows[hdr_idx + 1:]:
        if firm_col >= len(row):
            continue
        label = str(row[firm_col] or "").strip()
        if not label:
            break
        if label == _TOTAL_LABEL:
            break
        cnt = _count(row[day_col]) if day_col < len(row) else None
        if cnt is None:
            continue
        firm = _canonical_firm(label)
        out[firm] = out.get(firm, 0) + cnt
    return out


def _report2_machines(wb: Any, report_date: Any = None) -> dict[str, dict[str, int]]:
    from proverka.file_consistency import group_for_sheet

    out: dict[str, dict[str, int]] = {}
    for ws in wb.worksheets:
        group = group_for_sheet(ws.title)
        if not group:
            continue
        by_firm = _report2_sheet_machines(ws, report_date)
        if by_firm:
            out[group] = by_firm
    return out


def machines_from_path(
    path: Path, report_id: str, report_date: Any = None,
) -> dict[str, dict[str, int]]:
    """Операция → агрофирма → число машин из одного .xlsx.

    report_date нужен сводке 2: в таблице несколько колонок-дат."""
    rid = str(report_id)
    if rid in REPORTS_WITHOUT_MACHINES_IN_FILE or path.suffix.lower() != ".xlsx":
        return {}
    if load_workbook is None:
        raise RuntimeError(f"{path.name}: ImportError: нет openpyxl")
    try:
        wb = load_workbook(path, read_only=False, data_only=True)
    except Exception as exc:
        raise _read_failure(path, exc) from exc
    try:
        if rid == "1":
            return _report1_machines(wb)
        if rid == "2":
            return _report2_machines(wb, report_date)
        if _SHEET_NAME not in wb.sheetnames:
            raise ValueError(
                f"нет листа «{_SHEET_NAME}», листы: {', '.join(map(str, wb.sheetnames))}"
            )
        return _summary_sheet_machines(wb[_SHEET_NAME], rid)
    except Exception as exc:
        raise _read_failure(path, exc) from exc
    finally:
        wb.close()


def read_report_machines(report_date, report_id: str) -> dict[str, dict[str, int]]:
    """Находит .xlsx сводки за дату и читает из него число машин."""
    path = find_report_xlsx(report_date, str(report_id))
    if path is None:
        raise MachineFileMissing(f"сводка {report_id}: нет .xlsx за {report_date.isoformat()}")
    return machines_from_path(path, str(report_id), report_date)
