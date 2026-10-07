"""Пороги площади (га) для сверки proverka."""
from __future__ import annotations

# Расхождения меньше этого значения не считаются DIFF и не попадают в нарушения.
DIFF_MIN_HA = 0.5


def diff_is_significant(diff: float) -> bool:
    return abs(float(diff)) >= DIFF_MIN_HA


def compare_ha_status(diff: float) -> str:
    return "OK" if not diff_is_significant(diff) else "DIFF"
