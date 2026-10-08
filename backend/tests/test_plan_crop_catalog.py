"""Каждая агрооперация в редакторе планов получает полный каталог культур."""
from __future__ import annotations

from app.legacy.plans_editor import get_editor_state
from app.schemas import PlansEditorOut


def test_every_work_group_lists_the_full_crop_catalog():
    state = get_editor_state()
    payload = PlansEditorOut.model_validate(state).model_dump()
    catalog = set(payload["crops"])
    assert len(catalog) >= 30
    assert "Введение в оборот 2027 год" in catalog
    plan = payload["effective"]["HARDCODED_PLAN"]
    mode = payload["effective"]["CROP_REPORT_MODE"]
    assert payload["work_groups"]
    for group in payload["work_groups"]:
        present = set()
        for crops in (plan.get(group) or {}).values():
            if isinstance(crops, dict):
                present.update(crops)
        present.discard("Иные культуры")
        flagged = set((mode.get(group) or {}))
        assert catalog <= present | flagged, group
        for crop in catalog - present:
            assert (mode.get(group) or {}).get(crop) == 2, (group, crop)
