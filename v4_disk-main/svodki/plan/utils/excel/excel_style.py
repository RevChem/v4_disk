# -*- coding: utf-8 -*-
"""
Утилита стилизации таблиц Excel для сводок.
Границы, ширина колонок, выделение заголовков, строка «Итого».
"""

from __future__ import annotations

from openpyxl.styles import Border, Side, Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

HARVEST_HEADER_ROW_HEIGHT = 52.0
HARVEST_TITLE_ROW_HEIGHT = 24.0


def _cell_value_is_numeric(val) -> bool:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return True
    if val is None:
        return False
    s = str(val).strip().replace("\u00a0", "").replace(" ", "")
    if not s or s in {"-", "—"}:
        return False
    s = s.replace(",", ".")
    try:
        float(s)
        return True
    except ValueError:
        return False


def _write_harvest_sheet_banner(worksheet, title: str, n_cols: int) -> None:
    """Заголовок листа только в штампе PDF — в Excel не пишем (иначе дубль в PDF)."""
    return


def add_report_footnotes(worksheet, after_row: int, n_cols: int) -> None:
    """Сноски под таблицами уборки отключены (оставлено для совместимости вызовов)."""
    return


def add_standard_footnotes(worksheet, after_row: int, n_cols: int = 11) -> None:
    """Совместимость: add_report_footnotes."""
    add_report_footnotes(worksheet, after_row, n_cols)


def _autofit_harvest_columns(
    worksheet,
    start_row: int,
    n_rows: int,
    n_cols: int,
    *,
    start_col: int = 1,
    min_width: float = 10.0,
    max_width: float = 24.0,
) -> None:
    for i in range(n_cols):
        col_idx = start_col + i
        col_letter = get_column_letter(col_idx)
        col_max = min_width
        for r in range(n_rows):
            cell = worksheet.cell(row=start_row + r, column=col_idx)
            val = cell.value
            if val is None:
                continue
            s = str(val)
            if r == 0:
                est_lines = max(1, (len(s) + 11) // 12)
                col_max = max(col_max, min(max_width, len(s) / est_lines + 3))
            else:
                col_max = max(col_max, min(max_width, len(s) + 2))
        prev = float(worksheet.column_dimensions[col_letter].width or 0)
        worksheet.column_dimensions[col_letter].width = max(prev, col_max, min_width)


def finish_harvest_table(
    worksheet,
    start_row: int,
    n_data_rows: int,
    n_cols: int,
    *,
    has_total_row: bool = True,
) -> None:
    """Стили таблицы уборки + высота шапки + ширина колонок."""
    # n_data_rows — строк данных в Excel включая «Итого», если она уже в DataFrame
    n_table_rows = 1 + n_data_rows
    style_report_range(
        worksheet,
        start_row=start_row,
        n_rows=n_table_rows,
        n_cols=n_cols,
        has_total_row=has_total_row,
        center_numeric=True,
    )
    worksheet.row_dimensions[start_row].height = HARVEST_HEADER_ROW_HEIGHT
    _autofit_harvest_columns(worksheet, start_row, n_table_rows, n_cols)


def apply_styles_to_sheet(writer, sheet_name: str, df, has_total_row: bool = True) -> None:
    """
    Применить стили к листу после записи DataFrame.
    writer — pd.ExcelWriter с engine="openpyxl".
    Стили таблицы без сносок под таблицей.
    """
    ws = writer.book[sheet_name]
    if df.empty:
        return
    n_cols = len(df.columns)
    finish_harvest_table(
        ws,
        start_row=1,
        n_data_rows=len(df),
        n_cols=n_cols,
        has_total_row=has_total_row,
    )


def apply_vegetables_sheet(writer, sheet_name: str, df, report_date) -> None:
    """Лист «Овощи»: таблица с первой строки, шапка — только в PDF."""
    ws = writer.book[sheet_name]
    if not df.empty:
        finish_harvest_table(
            ws,
            start_row=1,
            n_data_rows=len(df),
            n_cols=len(df.columns),
            has_total_row=True,
        )


def apply_holding_summary_sheet(writer, sheet_name: str, section_title: str, df, report_date) -> None:
    """Лист зерновые/овощные: таблица с первой строки, шапка — только в PDF."""
    ws = writer.book[sheet_name]
    if not df.empty:
        finish_harvest_table(
            ws,
            start_row=1,
            n_data_rows=len(df),
            n_cols=len(df.columns),
            has_total_row=True,
        )


def style_report_sheet(worksheet, df, has_total_row: bool = True) -> None:
    """
    Применяет оформление к листу Excel:
    - границы всех ячеек
    - автоподбор ширины колонок (без выхода за края)
    - выделение заголовка (жирный, фон)
    - выделение строки «Итого» (жирный, фон)
    """
    if df.empty:
        return

    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )
    header_fill = PatternFill(start_color="8093B3", end_color="8093B3", fill_type="solid")
    total_fill = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    total_font = Font(bold=True)

    n_rows, n_cols = len(df) + 1, len(df.columns)  # +1 для заголовка
    max_col_width = 50  # чтобы не выезжали за края

    for col_idx in range(1, n_cols + 1):
        col_letter = get_column_letter(col_idx)
        max_len = 10
        for row_idx in range(1, n_rows + 1):
            cell = worksheet[f"{col_letter}{row_idx}"]
            val = cell.value
            if val is not None:
                s = str(val)
                max_len = min(max(max_len, len(s) + 1), max_col_width)
        worksheet.column_dimensions[col_letter].width = max_len

    for row_idx in range(1, n_rows + 1):
        is_header = row_idx == 1
        is_total = has_total_row and row_idx == n_rows
        for col_idx in range(1, n_cols + 1):
            cell = worksheet.cell(row=row_idx, column=col_idx)
            cell.border = thin_border
            cell.alignment = Alignment(wrap_text=True, vertical="center")
            if is_header:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            elif is_total:
                cell.fill = total_fill
                cell.font = total_font


def style_report_range(
    worksheet,
    start_row: int,
    n_rows: int,
    n_cols: int,
    has_total_row: bool = False,
    start_col: int = 1,
    center_numeric: bool = False,
) -> None:
    """
    Применяет оформление к диапазону на листе (для таблиц СОБСТВЕННЫЕ/НАЕМНЫЕ).
    start_row — 1-based Excel row, n_rows — всего строк, n_cols — колонок.
    start_col — 1-based Excel column (по умолчанию 1).
    """
    if n_rows <= 0 or n_cols <= 0:
        return

    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )
    header_fill = PatternFill(start_color="8093B3", end_color="8093B3", fill_type="solid")
    total_fill = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    total_font = Font(bold=True)
    max_col_width = 50

    for i in range(n_cols):
        col_idx = start_col + i
        col_letter = get_column_letter(col_idx)
        max_len = 10
        for r in range(n_rows):
            cell = worksheet.cell(row=start_row + r, column=col_idx)
            val = cell.value
            if val is not None:
                max_len = min(max(max_len, len(str(val)) + 1), max_col_width)
        w = worksheet.column_dimensions[col_letter].width or 10
        worksheet.column_dimensions[col_letter].width = min(max(w, max_len), max_col_width)

    for r in range(n_rows):
        is_header = r == 0
        is_total = has_total_row and r == n_rows - 1
        for i in range(n_cols):
            col_idx = start_col + i
            cell = worksheet.cell(row=start_row + r, column=col_idx)
            cell.border = thin_border
            if is_header:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            elif is_total:
                cell.fill = total_fill
                cell.font = total_font
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            elif center_numeric and _cell_value_is_numeric(cell.value):
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            else:
                cell.alignment = Alignment(
                    horizontal="left", vertical="center", wrap_text=True,
                )


def pattern_fill_low_output_vs_plan() -> PatternFill:
    """
    Заливка ячеек «выработка на 1 агрегат за сутки» ниже плановой (га/сутки).
    Тот же оттенок, что _COLOR_ALERT_PROD в 1_main_ammonium__1_.py.
    """
    return PatternFill("solid", fgColor="FCE4EC")


def summary_rate_below_plan_fill(
    column_key: str,
    row_data: dict,
    base_fill: PatternFill,
    alert_fill: PatternFill,
    *,
    plan_key: str,
    machines_key: str,
    rate_key: str,
) -> PatternFill:
    """Для строки сводки: если столбец — суточная выработка на агрегат и она < плана."""
    if column_key != rate_key:
        return base_fill
    try:
        plan = float(row_data.get(plan_key) or 0)
        nm = int(float(row_data.get(machines_key) or 0))
        rate = float(row_data.get(rate_key) or 0)
    except (TypeError, ValueError):
        return base_fill
    if nm > 0 and rate < plan:
        return alert_fill
    return base_fill


# Примечание на листах детализации (собственный / наёмный транспорт)
DETAIL_MACHINES_MIN_AREA_NOTE = (
    "В сводных данных отображается техника, отработавшая больше 10 га."
)
DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT = 34.0
# Сводки 8 и 9: компактная строка (auto_fit иначе раздувает merged-ячейку).
DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT_REPORT_8_9 = 22.0


def write_detail_machines_min_area_note(
    worksheet,
    row: int,
    n_cols: int,
    *,
    border: Border | None = None,
    height_pt: float | None = None,
) -> int:
    """Строка-пояснение перед таблицей техники; возвращает следующий номер строки."""
    worksheet.merge_cells(
        start_row=row, start_column=1, end_row=row, end_column=n_cols,
    )
    cell = worksheet.cell(row=row, column=1, value=DETAIL_MACHINES_MIN_AREA_NOTE)
    cell.font = Font(bold=False, size=11, color="000000")
    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    if border is not None:
        cell.border = border
    worksheet.row_dimensions[row].height = (
        DETAIL_MACHINES_MIN_AREA_NOTE_HEIGHT_PT
        if height_pt is None
        else height_pt
    )
    return row + 1
