"""Съём фактов заданий не пишет ничего, пока расчёт его не включил."""
from __future__ import annotations

import os
from datetime import date

from proverka import task_facts as tf


def test_note_is_noop_when_capture_off() -> None:
    assert not tf.capture_on()
    tf.note_fact(group="1.1", firm="А", crop="Соя", ha=5, task_id=1)
    tf.note_pending(("1.1", "А", 1), group="1.1", firm="А", ha=5, task_id=1)
    assert tf.bind_capture() is None


def test_commit_keeps_only_qualifying_machines() -> None:
    tokens = tf.begin_task_facts()
    try:
        tf.note_pending(
            ("1.1 Внесение минеральных удобрений", "А", 1),
            group="1.1 Внесение минеральных удобрений",
            firm="А", crop="Соя", ha=12.5, task_id=111,
        )
        tf.note_pending(
            ("1.1 Внесение минеральных удобрений", "А", 2),
            group="1.1 Внесение минеральных удобрений",
            firm="А", crop="Соя", ha=0.2, task_id=112,
        )
        tf.note_pending(
            ("2.1 Боронование", "А", 3),
            group="2.1 Боронование", firm="А", crop="Соя", ha=4, task_id=113,
        )
        tf.commit_group("1.1 Внесение минеральных удобрений", {("А", 1)})
        rows = [r for r in (tf._SINK.get() or []) if r["group"].startswith("1.1")]
        assert [r["task_id"] for r in rows] == [111]
        assert tf._PENDING.get() and tf._PENDING.get()[0][1]["task_id"] == 113
    finally:
        tf.discard_task_facts(tokens)
    assert not tf.capture_on()


def test_store_writes_tsv_without_touching_operations() -> None:
    operations = {
        "1.1 Внесение минеральных удобрений": {"area_today_by_firm": {"А": 12.5}},
    }
    tokens = tf.begin_task_facts()
    try:
        tf.note_fact(
            group="1.1 Внесение минеральных удобрений",
            firm="А", crop="Соя", ha=12.5, task_id=111, field_id=7,
        )
        rows = list(tf._SINK.get() or [])
    finally:
        tf.take_task_facts(tokens)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        os.environ["PROVERKA_TASK_FACTS_DIR"] = td
        try:
            meta = tf.safe_store_task_facts(
                rows, report_id="3", report_date=date(2026, 9, 23), operations=operations,
            )
            assert meta and meta["rows"] == 1
            assert (Path(td) / "task_facts_2026-09-23__r3.tsv").is_file()
        finally:
            os.environ.pop("PROVERKA_TASK_FACTS_DIR", None)
    assert meta["groups"]["1.1 Внесение минеральных удобрений"]["consistent"]
    assert operations["1.1 Внесение минеральных удобрений"]["area_today_by_firm"]["А"] == 12.5
