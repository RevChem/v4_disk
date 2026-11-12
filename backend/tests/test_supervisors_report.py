"""Сборка отчёта по заданиям: госномер, пути, листы agri/transport."""
from datetime import datetime, timedelta, timezone

from openpyxl import load_workbook
from io import BytesIO

from app.application.supervisors_report import (
    HEADERS,
    assemble_rows,
    parse_period,
    SupervisorsReportError,
    _workbook,
)

TZ = timezone(timedelta(hours=3))


def _task(tid, machine_id, work_type_id, total_m):
    return {
        "id": tid,
        "machine_id": machine_id,
        "work_type_id": work_type_id,
        "total_distance": total_m,
        "start_time": "2026-10-31T18:00:00.000+03:00",
        "end_time": "2026-11-01T05:59:00.000+03:00",
    }


def test_rows_match_checked_sample_and_split_by_machine_type():
    tasks = [
        _task(480479, 1, 121, 75810),
        _task(481381, 2, 121, 76470),
        _task(451674, 3, 143, 324390),
    ]
    machines = {
        1: {
            "name": "Трактор  JOHN DEERE 8340R 7661АК АА0304",
            "registration_number": "7661АК",
            "inventory_number": "00-010373",
            "machine_type": "agri",
        },
        2: {
            "name": "Трактор  CLAAS AXION 940 7449АЕ АФЧ0304",
            "registration_number": "7449АЕ",
            "inventory_number": "00-005078 АР",
            "machine_type": "agri",
        },
        3: {
            "name": "КАМАЗ АТЗ Р628ВЕ АФЧ0401",
            "registration_number": "Р628ВЕ",
            "inventory_number": "00-005796",
            "machine_type": "transport",
        },
    }
    fields = [
        {"id": 1, "machine_task_id": 480479, "field_id": 10, "work_distance": 75530},
        {"id": 2, "machine_task_id": 481381, "field_id": 20, "work_distance": 18760},
        {"id": 3, "machine_task_id": 481381, "field_id": 21, "work_distance": 11330},
        {"id": 4, "machine_task_id": 481381, "field_id": 22, "work_distance": 28830},
        {"id": 5, "machine_task_id": 481381, "field_id": 23, "work_distance": 0},
        {"id": 6, "machine_task_id": 481381, "field_id": 24, "work_distance": 14460},
    ]
    rows = assemble_rows(
        tasks,
        machines,
        {121: 3, 143: 4},
        {3: "Обработка почвы", 4: "Транспорт"},
        fields,
    )
    agri = [row for row in rows if row["sheet"] == "agri"]
    transport = [row for row in rows if row["sheet"] == "transport"]
    assert [row["values"][4] for row in agri if row["task_id"] == 480479] == [75.53]
    john = next(row for row in agri if row["task_id"] == 480479)
    assert john["values"][1].endswith("7661АК / 00-010373")
    assert john["values"][2] == "7661АК"
    assert john["values"][3] == "Обработка почвы"
    assert john["values"][5] == 75.81
    assert john["values"][6] == 0
    assert HEADERS[-1] == "Общий расход топлива, л"
    assert "Внесение" not in " ".join(HEADERS)
    assert "Бункера" not in HEADERS
    paths = [row["values"][4] for row in agri if row["task_id"] == 481381]
    assert paths == [18.76, 11.33, 28.83, 0.0, 14.46]
    assert all(row["values"][6] == 0 for row in rows)
    assert transport[0]["values"][2] == "Р628ВЕ"
    assert transport[0]["values"][3] == "Транспорт"
    assert transport[0]["values"][5] == 324.39

    book = load_workbook(BytesIO(_workbook(rows)))
    assert book.sheetnames[:2] == ["agri", "transport"]
    assert book["agri"]["C2"].value == "7449АЕ" or book["agri"]["C2"].value == "7661АК"


def test_period_rejects_inverted_and_too_long():
    start = datetime(2026, 11, 1, 6, 0, tzinfo=TZ)
    try:
        parse_period(start, start)
    except SupervisorsReportError as exc:
        assert "позже" in str(exc)
    else:
        raise AssertionError("ожидался отказ")
    try:
        parse_period(start, start + timedelta(days=94))
    except SupervisorsReportError as exc:
        assert "93" in str(exc)
    else:
        raise AssertionError("ожидался отказ")
    parsed_start, parsed_end = parse_period(
        datetime(2026, 11, 1, 6, 0),
        datetime(2026, 11, 30, 5, 59),
    )
    assert parsed_start.tzinfo == TZ
    assert parsed_end.hour == 5 and parsed_end.minute == 59
