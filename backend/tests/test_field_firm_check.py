"""Проверка «пометка фирмы в поле Namolot ≠ папка Cropwise». Сеть не нужна."""
from datetime import datetime

from app.application import field_firm_check as ffc
from app.application import namolot_view as nv

INDEX = {
    "РВ-01-61": "АФ Чистопольская",   # поле передали, Cropwise знает только нового владельца
    "РВ-01-02": "АгроРаздолье",
    "АИ-01-13": "АФ Чистопольская",   # пометка АФ4, папка другая — новая ошибка
    "АИ-02-116": "Аксу-Агро",         # пометка (АА) неизвестна — не тревога
}


def rec(spp, when, kg=1000, crop="Пшеница озимая 26г."):
    return {"spp": spp, "date": when, "quantity": kg, "crop": crop}


def run(records, transfer=None, ignored=None):
    return ffc.find_mismatches(
        records, INDEX,
        field_code=nv.field_base_code,
        record_time=nv.msk_wall,
        number_to_firm=nv._NUMBER_TO_FIRM,
        transfer_firm=transfer,
        ignored=ignored,
    )


RECORDS = [
    rec("РВ-01-61(АФ3) Пшеница озимая 26г.", "27.08.2026 19:35:57", 4170),
    rec("РВ-01-61(АФ3) Пшеница озимая 26г.", "28.08.2026 15:42:01", 5833),
    rec("РВ-01-02(АФ3) Пшеница озимая 26г.", "27.08.2026 10:00:00", 7000),
    rec("АИ-02-116(АА) Пшеница озимая 26г.", "01.08.2026 10:00:00", 2000),
    rec("АИ-01-13(АФ4) Рапс озимый 26г.", "01.08.2026 10:00:00", 3000, "Рапс озимый 26г."),
    rec("ЗЗ-99-99(АФ3) Пшеница озимая 26г.", "01.08.2026 10:00:00", 1000),   # нет в Cropwise
]


def test_finds_transferred_and_new_field_but_not_unknown_tags():
    found = run(RECORDS)
    assert {i["code"] for i in found} == {"РВ-01-61", "АИ-01-13"}
    moved = next(i for i in found if i["code"] == "РВ-01-61")
    assert moved["tag_firm"] == "АгроРаздолье" and moved["cropwise_firm"] == "АФ Чистопольская"
    assert moved["tonnes"] == 10.003 and moved["records"] == 2
    assert moved["first_at"].startswith("2026-08-27") and moved["last_at"].startswith("2026-08-28")


def test_known_transfer_is_not_an_error():
    def transfer(code, when):
        if code != "РВ-01-61":
            return None
        return "АгроРаздолье" if when < datetime(2026, 9, 2) else "АФ Чистопольская"

    found = run(RECORDS, transfer=transfer)
    assert [i["code"] for i in found] == ["АИ-01-13"]


def test_transfer_table_does_not_hide_records_after_transfer_date():
    """После даты передачи пометка АФ3 при папке Чистопольской — снова ошибка."""
    def transfer(code, when):
        if code != "РВ-01-61":
            return None
        return "АгроРаздолье" if when < datetime(2026, 9, 2) else "АФ Чистопольская"

    late = [rec("РВ-01-61(АФ3) Пшеница озимая 26г.", "05.09.2026 10:00:00", 9000)]
    assert [i["code"] for i in run(late, transfer=transfer)] == ["РВ-01-61"]


def test_ignored_key_is_skipped():
    key = ffc.item_key("АИ-01-13", "Аксу-Агро", "АФ Чистопольская")
    assert "АИ-01-13" not in {i["code"] for i in run(RECORDS, ignored={key})}


def test_latin_lookalikes_in_tag_and_code():
    assert ffc.firm_from_tags("AИ-01-13(AФ4) Рапс", nv._NUMBER_TO_FIRM) == "Аксу-Агро"
    assert ffc.firm_from_tags("X (АФ3) (АФ4)", nv._NUMBER_TO_FIRM) is None


def test_state_roundtrip_and_ignore_applies_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(ffc.settings, "upload_dir", str(tmp_path))
    item = {"key": "АИ-01-13|Аксу-Агро|АФ Чистопольская", "code": "АИ-01-13", "tonnes": 3.0}
    ffc._save_state({"run_id": "r1", "checked_at": "t", "status": "ok", "items": [item]})
    assert [i["code"] for i in ffc.read_state()["items"]] == ["АИ-01-13"]
    ffc.add_ignored(item["key"], "admin@x", "намеренно")
    assert ffc.read_state()["items"] == []


def test_source_failure_keeps_previous_items_and_run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(ffc.settings, "upload_dir", str(tmp_path))
    item = {"key": "k|a|b", "code": "k"}
    ffc._save_state({"run_id": "r1", "checked_at": "t", "status": "ok", "items": [item]})

    def boom():
        raise RuntimeError("нет сети")

    monkeypatch.setattr(nv, "_load_field_codes", boom)
    monkeypatch.setattr(nv, "field_code_index", lambda: {})
    state = ffc.run_check()
    assert state["status"] == "error" and state["run_id"] == "r1"
    assert state["items"] == [item]


def test_firm_tag_in_code_prefix_and_in_brackets():
    n2f = nv._NUMBER_TO_FIRM
    assert ffc.firm_from_tags("АФ5-02-27 Пшеница озимая 26г.", n2f) == "Дрожжаное Рассвет Агро"
    assert ffc.firm_from_tags("АФ2-01-02 Пшеница озимая 26г.", n2f) == "АФ Чистопольская"
    assert ffc.firm_from_tags("АИ-01-09(АФ4) Пшеница озимая 26г.", n2f) == "Аксу-Агро"
    assert ffc.firm_from_tags("АФ3-01-01(АФ3) Пшеница", n2f) == "АгроРаздолье"
    assert ffc.firm_from_tags("АФ3-01-01(АФ4) Пшеница", n2f) is None   # противоречие
    assert ffc.firm_from_tags("РВ-01-61 Пшеница озимая 26г.", n2f) is None
    assert ffc.firm_from_tags("АФ-01-01 Пшеница", n2f) is None


def test_prefix_tag_mismatch_with_cropwise_is_found():
    index = {"АФ5-02-27": "АФ Чистопольская", "АФ2-01-02": "АФ Чистопольская"}
    recs = [rec("АФ5-02-27 Пшеница озимая 26г.", "01.08.2026 10:00:00", 3000),
            rec("АФ2-01-02 Пшеница озимая 26г.", "01.08.2026 10:00:00", 3000)]
    found = ffc.find_mismatches(recs, index, field_code=nv.field_base_code,
                                record_time=nv.msk_wall, number_to_firm=nv._NUMBER_TO_FIRM)
    assert [i["code"] for i in found] == ["АФ5-02-27"]
