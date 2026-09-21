"""
Скрипт выгружает данные отчёта "Намолот" из API и сохраняет их в Excel.

Требуемые библиотеки (если не установлены):
    pip install requests openpyxl

Использование:
    python namolot_report_to_excel.py
(параметры запроса задаются ниже в разделе НАСТРОЙКИ)
"""

import base64
import requests
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter
from datetime import datetime

# =========================== НАСТРОЙКИ ===========================

BASE_URL = "http://10.0.1.5/hpp_elevator_new/hs/ground/report"

# Логин и пароль для Basic-авторизации
USERNAME = "Сводка"
PASSWORD = "3VzLqA6S"

PARAMS = {
    "date_begin": "2026-07-01",
    "date_end": "2026-08-01",
    # Необязательные параметры — раскомментируйте и заполните при необходимости:
    # "combine_harvester_guid": "",
    # "crop_guid": "e18cd497-7064-11f1-aed2-000c29b64f55",
    # "machine_operator_guid": "",
    # "spp_guid": "",
}

OUTPUT_FILE = "namolot_report_psheno.xlsx"

# Соответствие полей JSON -> заголовки колонок в Excel (порядок важен)
COLUMNS = [
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
]

# Поле(-я), которые нужно интерпретировать как дату/время
DATE_FIELDS = {"date"}

# ===================================================================


def fetch_data(base_url: str, params: dict, username: str = None, password: str = None) -> list:
    """Делает GET-запрос к API и возвращает список записей (JSON)."""
    print(f"Запрос к {base_url} с параметрами: {params}")

    headers = {}
    if username and password:
        # requests кодирует auth=(user, pass) в latin-1, что ломается на кириллице.
        # Поэтому собираем заголовок Basic Auth вручную в UTF-8.
        credentials = f"{username}:{password}".encode("utf-8")
        encoded = base64.b64encode(credentials).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"

    response = requests.get(base_url, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()

    if not isinstance(data, list):
        raise ValueError(f"Ожидался список записей, получено: {type(data)}")

    print(f"Получено записей: {len(data)}")
    return data


def parse_date_value(value):
    """
    Приводит значение поля даты к datetime, поддерживая разные форматы,
    которые может присылать API:
      - None / "" / 0            -> None (даты нет, а не 1970 год)
      - строка ISO 8601          -> datetime
      - число (unix timestamp,
        в секундах или мс)       -> datetime
      - что-то другое            -> возвращается как есть, без потери данных
    """
    if value is None or value == "" or value == 0:
        return None

    if isinstance(value, str):
        # Иногда API присылает дату строкой, но это тоже может быть
        # число, обёрнутое в кавычки (например "1753606246")
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            return parse_date_value(int(stripped))
        try:
            return datetime.fromisoformat(stripped)
        except ValueError:
            # Не смогли распознать формат — возвращаем исходную строку,
            # чтобы не потерять данные молча
            return value

    if isinstance(value, (int, float)):
        # Если число похоже на миллисекунды (13+ значащих цифр) — делим на 1000
        ts = value / 1000 if abs(value) > 10 ** 12 else value
        try:
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError, OverflowError):
            # Некорректный timestamp — не подставляем "1970 год",
            # а сигнализируем явным None
            return None

    return value


def save_to_excel(data: list, columns: list, output_file: str):
    """Сохраняет список словарей в Excel-файл с базовым форматированием."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Намолот"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Заголовки
    for col_idx, (field, header) in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    # Данные
    date_col_indices = set()
    for row_idx, item in enumerate(data, start=2):
        for col_idx, (field, header) in enumerate(columns, start=1):
            value = item.get(field, "")

            if field in DATE_FIELDS:
                value = parse_date_value(value)
                if isinstance(value, datetime):
                    date_col_indices.add(col_idx)

            ws.cell(row=row_idx, column=col_idx, value=value)

    # Принудительный формат ячеек для колонок с датой,
    # чтобы Excel не "угадывал" формат сам
    for col_idx in date_col_indices:
        col_letter = get_column_letter(col_idx)
        for row in range(2, ws.max_row + 1):
            cell = ws.cell(row=row, column=col_idx)
            if isinstance(cell.value, datetime):
                cell.number_format = "DD.MM.YYYY HH:MM:SS"

    # Автоширина колонок (приблизительная)
    for col_idx, (field, header) in enumerate(columns, start=1):
        max_len = len(header)
        for row in range(2, ws.max_row + 1):
            val = ws.cell(row=row, column=col_idx).value
            if val is not None:
                max_len = max(max_len, len(str(val)))
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 45)

    ws.freeze_panes = "A2"

    wb.save(output_file)
    print(f"Готово! Данные сохранены в файл: {output_file}")


def main():
    try:
        data = fetch_data(BASE_URL, PARAMS, USERNAME, PASSWORD)
    except requests.exceptions.RequestException as e:
        print(f"Ошибка при обращении к API: {e}")
        return
    except ValueError as e:
        print(f"Ошибка обработки данных: {e}")
        return

    if not data:
        print("API вернул пустой список — данных за указанный период нет.")
        return

    save_to_excel(data, COLUMNS, OUTPUT_FILE)


if __name__ == "__main__":
    main()