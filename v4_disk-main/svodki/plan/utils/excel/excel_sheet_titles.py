"""Имена листов Excel = как в PDF (эталон — PDF-верстка)."""
from __future__ import annotations

import re
from typing import Any

from openpyxl.workbook.workbook import Workbook


def firm_tab_title(module: Any, firm_key: str) -> str:
    """Заголовок вкладки: как в PDF (_firm_sheet_title_line), ≤31 символ."""
    fn = getattr(module, "_firm_sheet_title_line", None)
    if callable(fn):
        return _safe_tab(str(fn(firm_key)))
    label_fn = getattr(module, "_firm_table_label", None)
    if callable(label_fn):
        return _safe_tab(str(label_fn(firm_key)))
    return _safe_tab(str(firm_key))


def perf_tab_title(group_name: str) -> str:
    return _safe_tab(f"Выраб. — {group_name}")


def _safe_tab(name: str) -> str:
    s = re.sub(r"[\[\]:*?/\\]", " ", str(name or "").strip())
    return (s or "Лист")[:31]


def normalize_workbook_sheet_titles(wb: Workbook, report_id: str) -> list[str]:
    """
    Переименовать листы Excel под PDF-эталон.
    Возвращает список изменений «старое → новое».
    """
    changes: list[str] = []
    rid = str(report_id or "").strip()

    if rid == "1":
        for ws in wb.worksheets:
            title = str(ws.title or "")
            if title.startswith("Выраб.") or title in {
                "Сводка по культурам",
                "Свод по агрофирмам",
                "Выработка техники",
                "Сводка овощи",
                "Общий намолот",
                "Овощи",
                "Вспашка + Глубокорыхление",
            }:
                continue
            for gn in (
                "5.0 Уборка", "4.0 Посев", "2.11 Культивация", "2.12 Прикатывание",
                "3.1 Опрыскивание", "3.2 Опрыскивание (овощи)",
                "2.1 Боронование", "2.9 Дискование", "2.6 Фрезерование",
                "2.7 Гребнеобразование",
            ):
                if gn in title and not title.startswith("Выраб."):
                    new = perf_tab_title(gn)
                    if new != title:
                        ws.title = _unique_title(wb, new, exclude=ws)
                        changes.append(f"{title!r} → {ws.title!r}")
                    break
        return changes

    if rid == "2":
        return changes

    if rid in {"3", "4", "5", "6", "7", "8", "9", "11"}:
        return changes

    return changes


def _unique_title(wb: Workbook, base: str, *, exclude: Any = None) -> str:
    used = {ws.title for ws in wb.worksheets if ws is not exclude}
    title = _safe_tab(base)
    if title not in used:
        return title
    n = 2
    while True:
        suffix = f" ({n})"
        cand = _safe_tab(base[: max(1, 31 - len(suffix))] + suffix)
        if cand not in used:
            return cand
        n += 1
