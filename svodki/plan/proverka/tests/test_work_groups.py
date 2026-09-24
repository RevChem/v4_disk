"""Mapping work_type_id для proverka ↔ Cropwise."""
from __future__ import annotations

from proverka.work_groups import work_type_ids_for_group


def test_harvest_wt_includes_plans_list_and_corn_chop() -> None:
    wt = set(work_type_ids_for_group("5.0 Уборка"))
    assert wt == {6, 7, 75, 76, 77, 84, 111, 118, 125, 126, 145}
    assert 74 not in wt
    assert 147 not in wt
    assert 149 not in wt


def test_veg_harvest_wt_unchanged() -> None:
    wt = work_type_ids_for_group("5.4 Уборка картофеля, лука")
    assert set(wt) == {75, 76, 77}


def test_disk_crop_uses_task_season_and_skips_zyab() -> None:
    from plans import crop_for_disk_field

    crops = {2026: "Зябь", 2027: "Пшеница озимая"}
    assert crop_for_disk_field(crops, 2027) == "Пшеница озимая"
    assert crop_for_disk_field(crops, 2026) == "Пшеница озимая"
    assert crop_for_disk_field({2026: "Рапс яровой", 2027: "Пшеница озимая"}, 2027) == "Пшеница озимая"
    assert crop_for_disk_field({2026: "Зябь"}, 2026) == ""
