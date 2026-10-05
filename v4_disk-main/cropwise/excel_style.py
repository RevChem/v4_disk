# -*- coding: utf-8 -*-
"""
Утилита стилизации таблиц Excel для сводок.
Границы, ширина колонок, выделение заголовков, строка «Итого», примечания * и **.
"""

from openpyxl.styles import Border, Side, Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

# Стандартные примечания для сводок по уборке (как в макете CropWise)
FOOTNOTE_STAR = "* — с учетом закрытых/подтянутых агроопераций на 7.30 в Cropwise"
FOOTNOTE_DOUBLE = "** — ждем согласования на перенос плана кукурузы силоса на зерно и зерно на силос на основании СЗ от исполнительного директора"


def add_standard_footnotes(worksheet, after_row: int) -> None:
    """
    Добавляет стандартные примечания * и ** внизу листа.
    after_row — номер последней строки таблицы (1-based), примечания добавляются ниже.
    """
    row = after_row + 1
    worksheet.cell(row=row, column=1, value=FOOTNOTE_STAR)
    worksheet.cell(row=row + 1, column=1, value=FOOTNOTE_DOUBLE)
    for r in range(2):
        cell = worksheet.cell(row=row + r, column=1)
        cell.font = Font(size=9, italic=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")


def apply_styles_to_sheet(writer, sheet_name: str, df, has_total_row: bool = True) -> None:
    """
    Применить стили к листу после записи DataFrame.
    writer — pd.ExcelWriter с engine="openpyxl".
    Добавляет стандартные примечания * и ** под таблицей.
    """
    ws = writer.book[sheet_name]
    style_report_sheet(ws, df, has_total_row)
    if not df.empty:
        table_end_row = len(df) + 1  # заголовок + строки данных
        add_standard_footnotes(ws, table_end_row)


def apply_vegetables_sheet(writer, sheet_name: str, df, report_date) -> None:
    """
    Лист «Овощи»: заголовок, дата, стили таблицы по макету.
    Таблица записывается с startrow=4 (см. main).
    """
    from openpyxl.styles import Alignment, Font, PatternFill

    ws = writer.book[sheet_name]
    ws["A1"] = "ОБЩАЯ УБОРКА ОВОЩНЫХ КУЛЬТУР ПО ХОЛДИНГУ"
    ws["A1"].font = Font(bold=True)
    date_str = report_date.strftime("%d.%m.%Y") if hasattr(report_date, "strftime") else str(report_date)
    ws["I1"] = f"Сводка за {date_str}"
    ws["I1"].alignment = Alignment(horizontal="right")
    ws["I1"].fill = PatternFill(start_color="E0E0E0", end_color="E0E0E0", fill_type="solid")

    if not df.empty:
        n_rows, n_cols = len(df) + 1, len(df.columns)
        style_report_range(ws, start_row=5, n_rows=n_rows, n_cols=n_cols, has_total_row=True)
        table_end_row = 5 + n_rows - 1  # последняя строка таблицы (startrow=5 + строки - 1)
        add_standard_footnotes(ws, table_end_row)


def apply_holding_summary_sheet(writer, sheet_name: str, section_title: str, df, report_date) -> None:
    """
    Лист «СВОДНАЯ ИНФОРМАЦИЯ ПО УБОРКЕ»: заголовок секции (ЗЕРНОВЫЕ/ОВОЩНЫЕ КУЛЬТУРЫ), дата, стили.
    Таблица записывается с startrow=4.
    """
    ws = writer.book[sheet_name]
    ws["A1"] = section_title
    ws["A1"].font = Font(bold=True)
    date_str = report_date.strftime("%d.%m.%Y") if hasattr(report_date, "strftime") else str(report_date)
    ws["K1"] = f"Сводка за {date_str}"
    ws["K1"].alignment = Alignment(horizontal="right")
    ws["K1"].fill = PatternFill(start_color="E0E0E0", end_color="E0E0E0", fill_type="solid")

    if not df.empty:
        n_rows, n_cols = len(df) + 1, len(df.columns)
        style_report_range(ws, start_row=5, n_rows=n_rows, n_cols=n_cols, has_total_row=True)
        table_end_row = 5 + n_rows - 1
        add_standard_footnotes(ws, table_end_row)


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
            cell.alignment = Alignment(wrap_text=True, vertical="center")
            if is_header:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            elif is_total:
                cell.fill = total_fill
                cell.font = total_font


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
