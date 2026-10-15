"""Кукуруза на силос не переписывается в зерно, если в названии уже есть «силос»."""
from __future__ import annotations

from plan_paths import ensure_plan_sys_path

ensure_plan_sys_path()

from task_area_shared import (  # noqa: E402
    CORN_GRAIN_CROP,
    CORN_SILAGE_CROP,
    named_corn_subtype,
    resolve_corn_subtype,
)


def test_explicit_silage_name_survives_without_mowing_op():
    assert resolve_corn_subtype("Кукуруза на силос", 10, set()) == CORN_SILAGE_CROP
    assert resolve_corn_subtype("Кукуруза на силос", None, set()) == CORN_SILAGE_CROP


def test_explicit_grain_name_stays_grain_even_on_mowing_field():
    assert resolve_corn_subtype("Кукуруза на зерно", 10, {10}) == CORN_GRAIN_CROP


def test_unnamed_corn_uses_mowing_field():
    assert resolve_corn_subtype("Кукуруза", 10, {10}) == CORN_SILAGE_CROP
    assert resolve_corn_subtype("Кукуруза", 11, {10}) == CORN_GRAIN_CROP
    assert resolve_corn_subtype("Кукуруза", None, {10}) == "Кукуруза"


def test_named_corn_subtype_from_namolot_text():
    assert named_corn_subtype("Кукуруза на силос 26г.") == CORN_SILAGE_CROP
    assert named_corn_subtype("кукуруза зерно") == CORN_GRAIN_CROP
    assert named_corn_subtype("Кукуруза") is None
    assert named_corn_subtype("Пшеница озимая") is None
