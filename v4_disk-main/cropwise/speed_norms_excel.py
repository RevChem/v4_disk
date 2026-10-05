"""
cropwise/speed_norms_excel.py
================================
Разбор Excel-файла optim.xlsx с нормами скорости оборудования.

С сентября 2026 нормы скорости хранятся в JSON-файле (speed_implements.json) и
редактируются в модальном окне «Жатки» вкладки «Превышение скорости»
(см. speed_implements_dao.py). Проверка скорости (speed_violations_sync.py)
читает нормы ТОЛЬКО из этого справочника.

Этот модуль остался только как парсер файла optim.xlsx для разового /
повторного импорта в справочник (кнопка «Импорт из Excel» в модалке «Жатки» →
POST /api/speed-violations/implements/import-excel). Файл лежит рядом с
модулем — backend/app/cropwise/optim.xlsx.

Колонка «Оптимальная скорость» в файле задаётся диапазоном вида "3-5" /
"6-9". В справочнике хранятся ОБЕ границы (speed_min / speed_max); порогом
превышения считается верхняя граница (speed_max).

ВАЖНО про даты в Excel: ячейки с диапазоном "6-9" Excel сам превращает в
дату (формат ячейки d-m → «6-9» это 6 сентября). Первое число диапазона
оказывается в .day, второе — в .month. Раньше _recover_speed брал .day, то
есть НИЖНЮЮ границу, хотя в документации была заявлена верхняя (для «6-9»
порогом становилось 6, а не 9). Теперь берутся обе границы (min/max из
day и month — не зависит от локали Excel), порогом идёт верхняя.
"""
from __future__ import annotations

import os
import re
from datetime import date
from typing import Optional

import openpyxl

NORMS_DIR = os.path.dirname(os.path.abspath(__file__))  # backend/app/cropwise/
NORMS_PATH = os.path.join(NORMS_DIR, "optim.xlsx")


# =============================================================================
# Разбор Excel
# =============================================================================

_DASH_SPLIT_RE = re.compile(r"\s*[-–—−]\s*")


def recover_speed_range(value) -> tuple[Optional[float], Optional[float]]:
    """Разбирает ячейку «Оптимальная скорость» → (speed_min, speed_max).

    Поддерживаются:
      - дата (Excel превратил "6-9" в 6 сентября): границы — day и month,
        меньшая — нижняя, большая — верхняя (порядок day/month зависит от
        локали Excel, диапазон же всегда возрастающий);
      - строка "3-5" / "3–5" / "3 - 5" → (3, 5); "4,5" → (None, 4.5);
      - одно число (int/float) → (None, число).
    Не распознано → (None, None).
    """
    if value is None or isinstance(value, bool):
        return None, None

    # datetime — подкласс date, поэтому достаточно одной проверки
    if isinstance(value, date):
        a, b = float(value.day), float(value.month)
        return min(a, b), max(a, b)

    if isinstance(value, (int, float)):
        v = float(value)
        return (None, v) if v > 0 else (None, None)

    if isinstance(value, str):
        s = value.strip().replace(",", ".")
        if not s or s[0] in "-–—−":  # пусто или отрицательное число
            return None, None
        try:
            nums = [float(p) for p in _DASH_SPLIT_RE.split(s) if p]
        except ValueError:
            return None, None
        if not nums:
            return None, None
        if len(nums) == 1:
            return None, nums[0]
        lo, hi = nums[0], nums[-1]
        return (hi, lo) if lo > hi else (lo, hi)

    return None, None


def _recover_speed(value) -> Optional[float]:
    """Порог превышения = ВЕРХНЯЯ граница диапазона (см. recover_speed_range)."""
    return recover_speed_range(value)[1]


def load_excel_speed_table(path: str) -> tuple[list[dict], list[tuple[int, str]]]:
    """Парсит лист Excel с колонками ID / Оптимальная скорость (+опционально
    имя, инвентарный номер). Кидает RuntimeError, если обязательные колонки
    не найдены по заголовку — значит это не тот файл/формат."""
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
    header_map: dict[str, int] = {}
    for col_idx, cell_val in enumerate(header_row):
        if cell_val is None:
            continue
        header_map[str(cell_val).strip().lower()] = col_idx

    def _find_col(*needles: str) -> Optional[int]:
        for header_text, col_idx in header_map.items():
            for needle in needles:
                if needle in header_text:
                    return col_idx
        return None

    col_name = _find_col("имя", "название", "наименование")
    col_inv = _find_col("инвентарн")
    col_id = _find_col("id")
    col_speed = _find_col("скорост")

    missing_cols = [label for label, col in
                    (("ID", col_id), ("Оптимальная скорость", col_speed))
                    if col is None]
    if missing_cols:
        raise RuntimeError(
            f"Не удалось найти обязательные колонки по заголовку: {missing_cols}. "
            f"Заголовок первой строки листа: {list(header_row)}."
        )

    rows: list[dict] = []
    broken_rows: list[tuple[int, str]] = []

    for excel_row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if row is None or all(v is None for v in row):
            continue

        def _get(col_idx: Optional[int]):
            if col_idx is None or col_idx >= len(row):
                return None
            return row[col_idx]

        name = _get(col_name)
        inventory_number = _get(col_inv)
        id_raw = _get(col_id)
        speed_raw = _get(col_speed)

        if id_raw is None:
            broken_rows.append((excel_row_idx, "нет ID"))
            continue
        try:
            impl_id = int(id_raw)
        except (TypeError, ValueError):
            broken_rows.append((excel_row_idx, f"ID нечисловой: {id_raw!r}"))
            continue

        speed_min, speed_max = recover_speed_range(speed_raw)
        if speed_max is None:
            broken_rows.append((excel_row_idx, f"не удалось распознать скорость: {speed_raw!r}"))

        rows.append({
            "row": excel_row_idx,
            "name": name,
            "inventory_number": (str(inventory_number).strip() if inventory_number else None),
            "id": impl_id,
            "speed_min": speed_min,
            "speed_max": speed_max,
            "speed_kmh": speed_max,  # порог превышения (верхняя граница)
        })

    return rows, broken_rows
