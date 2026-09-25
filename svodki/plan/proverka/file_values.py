"""Читает фактическое значение «Итого» из уже сохранённого .xlsx отчёта.

Пилотный источник правды «В файле»: то, что реально лежит на листе «Сводка»
сохранённого отчёта в uploads/<дата>/ — в отличие от «Модуль» (наш расчёт,
svodka.total_ha) и «Cropwise» (сверка с внешней системой). Полезно как
дополнительная сверка «расчёт → файл, который видит пользователь» и как
защита от возможных багов в самом экспорте (лист/колонка не совпали,
строку «Итого» не туда дописали и т.п.).

ВАЖНО (см. обсуждение пилота по отчёту 6 «Опрыскивание»): сумма «Итого» по
листу «Сводка» — это сумма «опрыскивание + овощи» из файла, тогда как
svodka.total_ha в некоторых отчётах считает по-другому (например, только
по фирмам основного списка). На части дат числа могут разойтись не из-за
ошибки, а из-за разной методики подсчёта — это НЕ помечается как ошибка,
только выводится рядом как есть, для ручной сверки.

Модуль не бросает исключений наружу: если файла нет, он ещё не был
сохранён (см. report_runner._save_artifact), либо структура листа не
совпала с ожидаемой — просто возвращается {"found": False}, и вызывающий
код (run_proverka_batch) продолжает работу как обычно.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

try:
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover - openpyxl всегда должен быть доступен в этом окружении
    load_workbook = None  # type: ignore[assignment]

from report_platform import upload_dir

_TOTAL_LABEL = "Итого"
_FIRM_COL = "Агрофирма"
_VALUE_COL = "Общий объем выполненной работы за сутки, га"
_SHEET_NAME = "Сводка"

_FILENAME_HASH_PREFIX_RE = re.compile(r"^[0-9a-f]{8}_", re.IGNORECASE)
_REPORT_ID_RE = re.compile(r"^(\d+(?:\.\d+)?)")


def _report_id_from_title(title: str) -> str:
    m = _REPORT_ID_RE.match(str(title or "").strip())
    return m.group(1) if m else ""


def _report_id_from_filename(filename: str) -> str:
    """Копия правила именования из backend/app/legacy/report_runner.py.

    Дублируется намеренно: proverka (svodki/plan) и backend — разные
    процессы/деревья импорта, кросс-импорт между ними не предполагается.
    """
    stem = Path(filename).name
    if _FILENAME_HASH_PREFIX_RE.match(stem):
        stem = stem.split("_", 1)[1]
    stem = Path(stem).stem
    return _report_id_from_title(stem)


def find_report_xlsx(report_date: date, report_id: str) -> Path | None:
    """Ищет уже сохранённый .xlsx для report_id в uploads/<дата>/."""
    folder = upload_dir() / report_date.isoformat()
    if not folder.is_dir():
        return None
    rid = str(report_id)
    candidates = [
        p
        for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() == ".xlsx"
        and _report_id_from_filename(p.name) == rid
    ]
    if not candidates:
        return None
    # На случай дублей — берём самый свежий файл.
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _sum_total_rows(ws) -> tuple[float, bool]:
    """Суммирует значение _VALUE_COL по всем строкам «Итого» листа «Сводка»
    (основной блок «3.1 Опрыскивание» + блок «Опрыскивание овощных культур»,
    у каждого из них — своя шапка колонок и своя строка «Итого»)."""
    firm_col_idx: int | None = None
    value_col_idx: int | None = None
    total = 0.0
    found = False
    for row in ws.iter_rows(values_only=True):
        values = list(row)
        if _FIRM_COL in values and _VALUE_COL in values:
            firm_col_idx = values.index(_FIRM_COL)
            value_col_idx = values.index(_VALUE_COL)
            continue
        if firm_col_idx is None or value_col_idx is None:
            continue
        if max(firm_col_idx, value_col_idx) >= len(values):
            continue
        if str(values[firm_col_idx] or "").strip() != _TOTAL_LABEL:
            continue
        raw = values[value_col_idx]
        try:
            total += float(raw or 0)
            found = True
        except (TypeError, ValueError):
            continue
    return round(total, 2), found


def read_file_total_ha(report_date: date, report_id: str) -> dict[str, Any]:
    """{"found": False} либо {"found": True, "file_ha": .., "path": ".."}."""
    if load_workbook is None:
        return {"found": False}
    path = find_report_xlsx(report_date, report_id)
    if path is None:
        return {"found": False}
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return {"found": False}
    try:
        if _SHEET_NAME not in wb.sheetnames:
            return {"found": False}
        total, found = _sum_total_rows(wb[_SHEET_NAME])
        if not found:
            return {"found": False}
        return {"found": True, "file_ha": total, "path": str(path)}
    except Exception:
        return {"found": False}
    finally:
        wb.close()


def attach_file_values(all_results: dict[str, Any], report_date: date) -> None:
    """Дополняет all_results["reports"][rid]["file_values"] для каждого отчёта.

    Тихо: любая ошибка на отдельном report_id не должна прерывать batch —
    в худшем случае для этого отчёта просто не будет колонки «В файле».
    """
    reports = all_results.get("reports") or {}
    for rid, result in reports.items():
        if not isinstance(result, dict):
            continue
        try:
            result["file_values"] = read_file_total_ha(report_date, rid)
        except Exception:
            result["file_values"] = {"found": False}
