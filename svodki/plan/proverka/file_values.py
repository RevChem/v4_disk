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
_FIRM_HEADERS = ("Агрофирма", "Агрофирмы")
_VALUE_HEADERS = (
    "Общий объем выполненной работы за сутки, га",
    "Обработано за сутки, га",
)
_SHEET_NAME = "Сводка"

# Заголовок блока на листе «Сводка» → операция сверки, если он не совпадает
# с именем группы (отчёт 5: «КУЛЬТУРЫ», отчёт 6: овощи).
_GROUP_BLOCK_TITLES: dict[str, tuple[str, ...]] = {
    "3.1 Опрыскивание": ("3.1 Опрыскивание",),
    "3.2 Опрыскивание (овощи)": ("Опрыскивание овощных культур",),
    "4.0 Посев": ("КУЛЬТУРЫ",),
    "4.1 Посев (овощи)": ("Посев / посадка овощных культур",),
}

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


def _header_indexes(values: list[Any]) -> tuple[int, int] | None:
    firm_idx: int | None = None
    value_idx: int | None = None
    for i, cell in enumerate(values):
        text = str(cell or "").strip()
        if text in _FIRM_HEADERS:
            firm_idx = i
        if text in _VALUE_HEADERS:
            value_idx = i
    if firm_idx is None or value_idx is None:
        return None
    return firm_idx, value_idx


def _section_title(values: list[Any]) -> str | None:
    """Строка-заголовок блока: одна непустая ячейка, не «Итого» и не шапка."""
    texts = [str(cell).strip() for cell in values if cell not in (None, "")]
    if len(texts) != 1:
        return None
    text = texts[0]
    if text == _TOTAL_LABEL or text in _FIRM_HEADERS or text in _VALUE_HEADERS:
        return None
    return text


def read_summary_blocks(ws) -> list[dict[str, Any]]:
    """Блоки листа «Сводка»: заголовок секции и «Итого» по суточной площади.

    У сводок 3, 4, 7, 8 блок один и без заголовка. У 5 и 6 на листе два блока
    со своими шапками — их итоги не складываются, чтобы «В файле» сравнивалось
    с конкретной операцией, а не с суммой соседнего блока.
    """
    blocks: list[dict[str, Any]] = []
    pending_title = ""
    firm_idx: int | None = None
    value_idx: int | None = None
    current_title = ""
    total = 0.0
    found = False

    def flush() -> None:
        nonlocal total, found
        if found:
            blocks.append({"title": current_title, "file_ha": round(total, 2)})
        total = 0.0
        found = False

    for row in ws.iter_rows(values_only=True):
        values = list(row)
        header = _header_indexes(values)
        if header:
            flush()
            firm_idx, value_idx = header
            current_title = pending_title
            pending_title = ""
            continue
        title = _section_title(values)
        if title:
            pending_title = title
            continue
        if firm_idx is None or value_idx is None:
            continue
        if max(firm_idx, value_idx) >= len(values):
            continue
        if str(values[firm_idx] or "").strip() != _TOTAL_LABEL:
            continue
        try:
            total += float(values[value_idx] or 0)
            found = True
        except (TypeError, ValueError):
            continue
    flush()
    return blocks


def file_ha_for_operation(
    file_values: dict[str, Any] | None,
    group_name: str,
    report_id: str,
) -> float | None:
    """Итог Excel тематической сводки для одной строки сверки операций."""
    if not file_values or not file_values.get("found"):
        return None
    blocks = list(file_values.get("blocks") or [])
    if not blocks:
        return None
    aliases = {
        name.casefold()
        for name in _GROUP_BLOCK_TITLES.get(group_name, (group_name,))
    }
    for block in blocks:
        title = str(block.get("title") or "").strip()
        if title and title.casefold() in aliases:
            return float(block.get("file_ha") or 0)
    if len(blocks) == 1:
        from proverka.reports_config import REPORTS

        spec = REPORTS.get(str(report_id))
        groups = tuple(spec.work_groups) if spec else ()
        if len(groups) <= 1:
            return float(blocks[0].get("file_ha") or 0)
    return None


def _canonical_report_id(row: dict[str, Any]) -> str:
    rid = str(row.get("canonical_report") or "").strip()
    if rid:
        return rid
    peers = row.get("active_reports") or row.get("peer_reports") or []
    for peer in peers:
        peer_id = str(peer)
        if peer_id not in ("1", "2"):
            return peer_id
    return ""


def apply_file_ha_to_rows(rows: list[Any], report_date: date) -> None:
    """Дописывает file_ha в уже сохранённые строки сверки (старые задачи проверки)."""
    cache: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or "file_ha" in row:
            continue
        rid = _canonical_report_id(row)
        if rid not in cache:
            try:
                cache[rid] = read_file_total_ha(report_date, rid)
            except Exception:
                cache[rid] = {"found": False}
        row["file_ha"] = file_ha_for_operation(
            cache[rid], str(row.get("group_name") or ""), rid
        )
        row["file_report_id"] = rid or None


def stamp_cross_check_file_ha(all_results: dict[str, Any]) -> None:
    """Кладёт file_ha на строки сверки операций — колонку перед Max diff."""
    reports = all_results.get("reports") or {}
    rows = ((all_results.get("cross_check") or {}).get("operations")) or []
    cache: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        rid = _canonical_report_id(row)
        fv = cache.get(rid)
        if fv is None:
            stored = reports.get(rid) if isinstance(reports.get(rid), dict) else None
            fv = (stored or {}).get("file_values") or {"found": False}
            cache[rid] = fv
        row["file_ha"] = file_ha_for_operation(fv, str(row.get("group_name") or ""), rid)
        row["file_report_id"] = rid or None


def read_file_total_ha(report_date: date, report_id: str) -> dict[str, Any]:
    """{"found": False} либо {"found": True, "file_ha": .., "blocks": [...], "path": ".."}."""
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
        blocks = read_summary_blocks(wb[_SHEET_NAME])
        if not blocks:
            return {"found": False}
        total = round(sum(float(block.get("file_ha") or 0) for block in blocks), 2)
        return {"found": True, "file_ha": total, "blocks": blocks, "path": str(path)}
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
    try:
        stamp_cross_check_file_ha(all_results)
    except Exception:
        return
