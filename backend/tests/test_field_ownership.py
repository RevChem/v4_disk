"""Смена принадлежности поля: пометка Namolot во времени и против папки Cropwise."""
from datetime import datetime

from app.application.field_ownership import changed_ownership
from app.application.namolot_view import _NUMBER_TO_FIRM, field_base_code, msk_wall

INDEX = {
    "РВ-01-61": "АФ Чистопольская",
    "РВ-01-02": "АгроРаздолье",
}


def rec(spp, when, kg=1000):
    return {"spp": spp, "date": when, "quantity": kg}


def run(records, since="2025-06-01", known=None):
    return changed_ownership(
        records,
        INDEX,
        datetime.fromisoformat(since).date(),
        field_code=field_base_code,
        record_time=msk_wall,
        number_to_firm=_NUMBER_TO_FIRM,
        known_codes=known,
    )


def test_tag_change_and_folder_mismatch_are_listed():
    items = run([
        rec("РВ-01-61(АФ3) Пшеница", "27.08.2026 19:35:57", 4170),
        rec("РВ-01-61(АФ4) Пшеница", "03.09.2026 10:00:00", 1000),
        rec("РВ-01-02(АФ3) Пшеница", "27.08.2026 10:00:00", 7000),
        rec("РВ-01-02(АФ3) Пшеница", "01.05.2025 10:00:00", 9000),
    ], known={"РВ-01-61"})
    by_code = {row["code"]: row for row in items}
    assert set(by_code) == {"РВ-01-61"}
    row = by_code["РВ-01-61"]
    assert row["tag_changed"] is True
    assert row["folder_differs"] is True
    assert row["known_transfer"] is True
    assert row["cropwise_firm"] == "АФ Чистопольская"
    assert [period["firm"] for period in row["periods"]] == ["АгроРаздолье", "Аксу-Агро"]
    assert row["periods"][0]["tonnes"] == 4.17


def test_single_matching_tag_is_not_a_change():
    items = run([rec("РВ-01-02(АФ3) Пшеница", "27.08.2026 10:00:00")])
    assert items == []
