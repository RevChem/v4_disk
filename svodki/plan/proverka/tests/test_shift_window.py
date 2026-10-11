"""Смена 06:00–05:59: граница без пояса — московское время, не UTC."""
from __future__ import annotations

from svodki_prefetch_io import filter_tasks_end_time_window

START = "2026-10-10T06:00:00"
END = "2026-10-11T06:00:00"


def _task(tid: int, end_time: str) -> dict:
    return {"id": tid, "end_time": end_time}


def test_shift_keeps_end_before_nine_msk_and_drops_outside():
    tasks = [
        _task(1, "2026-10-10T05:59:00.000+03:00"),
        _task(2, "2026-10-10T06:00:00.000+03:00"),
        _task(3, "2026-10-10T08:59:00.000+03:00"),
        _task(4, "2026-10-11T05:59:00.000+03:00"),
        _task(5, "2026-10-11T06:00:00.000+03:00"),
    ]
    kept = {row["id"] for row in filter_tasks_end_time_window(tasks, START, END)}
    assert kept == {2, 3, 4}
