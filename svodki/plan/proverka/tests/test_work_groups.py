"""Mapping work_type_id для proverka ↔ Cropwise."""
from __future__ import annotations

from proverka.work_groups import work_type_ids_for_group


def test_harvest_wt_excludes_veg_includes_silage_mowing() -> None:
    wt = work_type_ids_for_group("5.0 Уборка")
    assert 75 not in wt
    assert 76 not in wt
    assert 77 not in wt
    assert 6 in wt
    assert 74 in wt
    assert 149 in wt


def test_veg_harvest_wt_unchanged() -> None:
    wt = work_type_ids_for_group("5.4 Уборка картофеля, лука")
    assert set(wt) == {75, 76, 77}
