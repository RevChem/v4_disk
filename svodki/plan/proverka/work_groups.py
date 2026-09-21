"""Единый mapping work_type_id по группам операций для proverka."""
from __future__ import annotations

from plans import WORK_GROUPS

try:
    from task_area_shared import SILAGE_CORN_MOWING_WT_IDS
except ImportError:
    SILAGE_CORN_MOWING_WT_IDS = frozenset({74, 83, 84, 147, 149})

_VEG_54_WT: list[int] = next(
    (list(ids) for name, ids in WORK_GROUPS if name == "5.4 Уборка картофеля, лука"),
    [75, 76, 77],
)

# plans.WORK_GROUPS «5.0 Уборка» включает wt 75–77 (овощи, 5.4). В Cropwise UI
# зерновая уборка — это 5.2 + 5.8 + «Скашивание с измельчением кукурузы»;
# wt 75–77 в work.json давали ~41 га овощей вместо силоса (~59 га).
_HARVEST_GRAIN_WT: list[int] = sorted(
    {6, 7, 111, 118, 125, 126, 145} | set(int(x) for x in SILAGE_CORN_MOWING_WT_IDS)
)

WORK_GROUP_WT_OVERRIDES: dict[str, list[int]] = {
    "5.4 Уборка картофеля, лука": _VEG_54_WT,
    "5.0 Уборка": _HARVEST_GRAIN_WT,
}


def work_group_wt_map() -> dict[str, list[int]]:
    m = {name: list(ids) for name, ids in WORK_GROUPS}
    m.update(WORK_GROUP_WT_OVERRIDES)
    return m


def work_type_ids_for_group(group_name: str) -> list[int]:
    if group_name in WORK_GROUP_WT_OVERRIDES:
        return list(WORK_GROUP_WT_OVERRIDES[group_name])
    return list(dict(WORK_GROUPS).get(group_name) or [])
