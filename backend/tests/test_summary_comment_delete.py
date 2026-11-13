"""Удаление комментария не должно отменяться следующим открытием редактора."""
from __future__ import annotations

from app.legacy.comments_store import (
    apply_future_comments_to_summary,
    delete_summary_comment,
    list_summary_comments,
    upsert_future_comment,
    upsert_summary_comment,
)


def _put(store, summary_id, *, cell_key, comment, apply_to_future=False, page=1):
    upsert_summary_comment(
        store,
        summary_id,
        report_id="13",
        base_pdf_path="/tmp/13.pdf",
        page=page,
        x=0.4,
        y=0.2,
        comment_anchor_y=0.8,
        cell_key=cell_key,
        cell_text="0",
        comment=comment,
    )
    row = next(item for item in list_summary_comments(store, summary_id) if item["cell_key"] == cell_key)
    row["apply_to_future"] = apply_to_future
    if apply_to_future:
        upsert_future_comment(store, "13", row)
    return row


def test_delete_is_not_restored_when_future_flag_is_off():
    store: dict = {}
    row = _put(store, 24, cell_key="1:10:20:0", comment="дождь", apply_to_future=False)
    upsert_future_comment(store, "13", {**row, "apply_to_future": True})

    kept, deleted = delete_summary_comment(store, 24, row["id"])
    assert deleted
    assert kept == []

    comments, changed = apply_future_comments_to_summary(
        store, 24, report_id="13", base_pdf_path="/tmp/13.pdf"
    )
    assert comments == []
    assert changed is False


def test_delete_suppresses_same_note_saved_under_another_cell_key():
    store: dict = {}
    current = _put(store, 24, cell_key="3:40:80:56 372", comment="дождь", apply_to_future=True)
    stale = {
        **current,
        "cell_key": "3:20:80:56 367",
        "x": 0.2,
    }
    upsert_future_comment(store, "13", stale)

    delete_summary_comment(store, 24, current["id"])
    comments, _changed = apply_future_comments_to_summary(
        store, 24, report_id="13", base_pdf_path="/tmp/13.pdf"
    )
    assert comments == []

    # Другая дата тот же шаблон по-прежнему получает.
    other, changed = apply_future_comments_to_summary(
        store, 25, report_id="13", base_pdf_path="/tmp/13-next.pdf"
    )
    assert changed is True
    assert {row["comment"] for row in other} == {"дождь"}


def test_delete_removes_duplicate_copy_on_the_same_page():
    store: dict = {}
    first = _put(store, 24, cell_key="1:10:20:0", comment="дождь")
    second = _put(store, 24, cell_key="1:30:20:0", comment="дождь")
    other = _put(store, 24, cell_key="2:10:20:1", comment="дождь", page=2)

    kept, deleted = delete_summary_comment(store, 24, first["id"])
    assert deleted
    assert second["id"] not in {row["id"] for row in kept}
    assert [row["id"] for row in kept] == [other["id"]]
