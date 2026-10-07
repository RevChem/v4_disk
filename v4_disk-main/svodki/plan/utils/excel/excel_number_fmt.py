"""
Общие форматы чисел и разбор значений для сводок (svodki/plan).

Подкладывается в PYTHONPATH вместе с каталогом скриптов; без этого файла
отчёты 3–8, 5 и др. падают с ModuleNotFoundError при импорте.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

NF_INT = "# ##0"
NF_DEC1 = "# ##0.0"
NF_DEC2 = "# ##0.00"
NF_DATE = "DD.MM.YYYY"
NF_PCT = "0.0%"


def _coerce_float(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return float(int(raw))
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        s = raw.strip().replace("\u00a0", " ").replace(" ", "")
        s = s.replace(",", ".")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _numeric_format_for_value(v: float) -> tuple[float | int, str]:
    if math.isfinite(v) and abs(v - round(v)) < 1e-9:
        return int(round(v)), NF_INT
    if math.isfinite(v) and abs(v - round(v, 1)) < 1e-9:
        return round(v, 1), NF_DEC1
    if math.isfinite(v) and abs(v - round(v, 2)) < 1e-9:
        return round(v, 2), NF_DEC2
    return v, NF_DEC2


def summary_cell_value_and_format(column_key: str, raw: Any) -> tuple[Any, str | None]:
    """
    column_key — логическое имя колонки (как в SUMMARY_COL_KEYS) или подпись колонки
    из SUMMARY_COLS (с переносами строк).
    """
    k = (column_key or "").strip().replace("\n", " ")
    low = k.lower()

    if raw is None or raw == "":
        return "", None

    if isinstance(raw, str) and raw.strip() == "Итого" and "агро" in low:
        return raw, None

    if "агрофирм" in low or low in {"агрофирма"} or k.startswith("Агрофирм"):
        return raw, None

    if "№" in k and "п/п" in k:
        n = _coerce_float(raw)
        if n is not None:
            if abs(n - round(n)) < 1e-9:
                return int(round(n)), NF_INT
            # Подпункты вроде 6.1 / 6.2 (сводка 10 — кукуруза)
            return round(n, 1), NF_DEC1
        return raw, None

    if "%" in k or "процент" in low or "коэффициент" in low:
        n = _coerce_float(raw)
        if n is not None:
            return n / 100.0 if n > 1.5 else n, NF_PCT
        return raw, None

    # Явные правила для ключевых колонок сводок 3-9 (единообразно для всех отчётов):
    # - объем/выработка за сутки: 2 знака
    # - средние/часовые показатели и время в машино-часах: 1 знак
    n = _coerce_float(raw)
    if n is not None:
        if "общий объем выполненной работы за сутки" in low:
            return round(n, 2), NF_DEC2
        if "выработка за сутки/1 ед агрегата" in low:
            return round(n, 2), NF_DEC2
        if "средняя выработка по сезону" in low:
            return round(n, 1), NF_DEC1
        if "общее время работы на полях" in low:
            return round(n, 1), NF_DEC1
        if "среднее время эффективной работы" in low:
            return round(n, 1), NF_DEC1
        if "часовая выработка" in low:
            return round(n, 1), NF_DEC1

    if n is not None:
        return _numeric_format_for_value(n)
    return raw, None


def hk_detail_cell_value_and_format(hdr: str, raw: Any) -> tuple[Any, str | None]:
    """Форматирование ячеек детальной сводки ХК по тексту заголовка столбца."""
    h = (hdr or "").strip()
    low = h.lower()

    if raw is None:
        return "", None

    if isinstance(raw, datetime):
        return raw, NF_DATE
    if isinstance(raw, date):
        return raw, NF_DATE

    if isinstance(raw, str) and not raw.strip():
        return "", None

    if "агрофирм" in low or "наименован" in low:
        return raw, None

    if "%" in h or "дол" in low and "%" in str(raw):
        n = _coerce_float(raw)
        if n is not None:
            vv = n / 100.0 if n > 1.5 else n
            return vv, NF_PCT
        return raw, None

    n = _coerce_float(raw)
    if n is not None:
        return _numeric_format_for_value(n)
    return raw, None
