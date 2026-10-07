"""Единый mapping work_type_id по группам операций для proverka."""
from __future__ import annotations

from plans import WORK_GROUPS

_VEG_54_WT: list[int] = next(
    (list(ids) for name, ids in WORK_GROUPS if name == "5.4 Уборка картофеля, лука"),
    [75, 76, 77],
)

# 5.0 — список из plans.WORK_GROUPS плюс 6.3 «Скашивание с измельчением кукурузы».
# В work.json это колонка «Кормозаготовка», work_type_id 84. Остальные виды
# скашивания (ботва, валок, сено) в уборку не входят.
_CORN_SILAGE_CHOP_WT = 84
_HARVEST_GRAIN_WT: list[int] = sorted(
    (
        set(next(ids for name, ids in WORK_GROUPS if name == "5.0 Уборка"))
        - set(_VEG_54_WT)
    )
    | {_CORN_SILAGE_CHOP_WT}
)

# Культивация: единый набор work_type_id для plans.WORK_GROUPS, сводки 8
# (8_main_fixed.WORK_TYPES) и всех проверок — 10, 50, 63, 96.
_CULTIVATION_WT: list[int] = [10, 50, 63, 96]

WORK_GROUP_WT_OVERRIDES: dict[str, list[int]] = {
    "5.4 Уборка картофеля, лука": _VEG_54_WT,
    "5.0 Уборка": _HARVEST_GRAIN_WT,
    "2.11 Культивация": _CULTIVATION_WT,
}


def work_group_wt_map() -> dict[str, list[int]]:
    m = {name: list(ids) for name, ids in WORK_GROUPS}
    m.update(WORK_GROUP_WT_OVERRIDES)
    return m


def work_type_ids_for_group(group_name: str) -> list[int]:
    if group_name in WORK_GROUP_WT_OVERRIDES:
        return list(WORK_GROUP_WT_OVERRIDES[group_name])
    return list(dict(WORK_GROUPS).get(group_name) or [])
