"""Сборка отчёта по заданиям: госномер, пути, листы agri/transport."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from openpyxl import load_workbook
from io import BytesIO

from app.application.supervisors_report import (
    FMI_BATCH_SIZE,
    HEADERS,
    assemble_rows,
    parse_period,
    SupervisorsReportError,
    _fetch_fields,
    _workbook,
)

TZ = timezone(timedelta(hours=3))


def _task(tid, machine_id, work_type_id, total_m, fuel=0):
    return {
        "id": tid,
        "machine_id": machine_id,
        "work_type_id": work_type_id,
        "total_distance": total_m,
        "fuel_consumption": fuel,
        "start_time": "2026-10-31T18:00:00.000+03:00",
        "end_time": "2026-11-01T05:59:00.000+03:00",
    }


def test_rows_match_checked_sample_and_split_by_machine_type():
    tasks = [
        _task(480479, 1, 121, 75810, 1386.22),
        _task(481381, 2, 121, 76470, 10.5),
        _task(451674, 3, 143, 324390, 249.22),
        _task(482999, 7, 121, 50000, 99),
    ]
    machines = {
        1: {
            "name": "Трактор  JOHN DEERE 8340R 7661АК АА0304",
            "registration_number": "7661АК",
            "inventory_number": "00-010373",
            "machine_type": "agri",
            "machine_subtype": "tractor",
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
            "machine_subtype": "fuel_bowser",
        },
        7: {
            "name": "Наёмник",
            "registration_number": "Н007НН",
            "inventory_number": "7",
            "machine_type": "agri",
            "machine_group_id": 7,
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
    assert [row["values"][5] for row in agri if row["task_id"] == 480479] == [75.53]
    john = next(row for row in agri if row["task_id"] == 480479)
    assert john["values"][1].endswith("7661АК / 00-010373")
    assert john["values"][2] == "7661АК"
    assert john["values"][3] == "Трактор"
    assert john["values"][4] == "Обработка почвы"
    assert john["values"][6] == 75.81
    assert john["values"][7] == 1386.22
    assert john["values"][8] == "https://operations.cropwise.com/machines/1/tasks/480479"
    assert "Общий расход топлива, л" in HEADERS
    assert HEADERS[-1] == "Задание"
    assert "Внесение" not in " ".join(HEADERS)
    assert "Бункера" not in HEADERS
    paths = [row["values"][5] for row in agri if row["task_id"] == 481381]
    assert paths == [18.76, 11.33, 28.83, 0.0, 14.46]
    assert {row["values"][7] for row in agri if row["task_id"] == 481381} == {10.5}
    assert transport[0]["values"][2] == "Р628ВЕ"
    assert transport[0]["values"][3] == "Топливозаправщик"
    assert transport[0]["values"][7] == 249.22
    assert transport[0]["values"][4] == "Транспорт"
    assert transport[0]["values"][6] == 324.39
    assert all(row["sheet"] != "наемники" for row in rows)
    assert all(row["task_id"] != 482999 for row in rows)

    book = load_workbook(BytesIO(_workbook(rows)))
    assert book.sheetnames[:2] == ["agri", "transport"]
    assert book["agri"]["C2"].value == "7449АЕ" or book["agri"]["C2"].value == "7661АК"
    linked = {
        cell.hyperlink.target
        for row in book["agri"].iter_rows(min_row=2, min_col=9, max_col=9)
        for cell in row
        if cell.hyperlink is not None
    }
    assert "https://operations.cropwise.com/machines/1/tasks/480479" in linked


def test_fields_are_fetched_in_batches_of_300():
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids = request.url.params["machine_task_id"].split(",")
        if int(request.url.params.get("from_id") or 0) > 0:
            return httpx.Response(200, json={"data": [], "meta": {"response": {}}})
        seen.append(ids)
        data = [{"id": int(item), "machine_task_id": int(item), "work_distance": 1000} for item in ids]
        return httpx.Response(
            200,
            json={"data": data, "meta": {"response": {"last_record_id": int(ids[-1]), "obtained_records": len(data)}}},
        )

    task_ids = list(range(1, FMI_BATCH_SIZE + 2))

    async def run() -> list[dict]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as client:
            return await _fetch_fields(client, task_ids)

    rows = asyncio.run(run())
    assert [len(batch) for batch in seen] == [FMI_BATCH_SIZE, 1]
    assert len(rows) == FMI_BATCH_SIZE + 1
    assert {row["machine_task_id"] for row in rows} == set(task_ids)


def test_failed_field_batch_is_retried_one_task_at_a_time():
    def handler(request: httpx.Request) -> httpx.Response:
        ids = request.url.params["machine_task_id"].split(",")
        if len(ids) > 1:
            return httpx.Response(500, json={"error": "batch"})
        item = ids[0]
        return httpx.Response(
            200,
            json={
                "data": [{"id": int(item), "machine_task_id": int(item), "work_distance": 500}],
                "meta": {"response": {"obtained_records": 1}},
            },
        )

    async def run() -> list[dict]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as client:
            return await _fetch_fields(client, [11, 12])

    rows = asyncio.run(run())
    assert sorted(row["machine_task_id"] for row in rows) == [11, 12]


def test_period_rejects_inverted_and_accepts_any_length():
    start = datetime(2026, 11, 1, 6, 0, tzinfo=TZ)
    try:
        parse_period(start, start)
    except SupervisorsReportError as exc:
        assert "позже" in str(exc)
    else:
        raise AssertionError("ожидался отказ")
    parsed_start, parsed_end = parse_period(start, start + timedelta(days=400))
    assert parsed_start == start
    assert parsed_end - parsed_start == timedelta(days=400)
