"""Единый mapping work_type_id по группам операций для proverka."""
from __future__ import annotations

from plans import WORK_GROUPS

# 6.3 «Скашивание с измельчением кукурузы» / кормозаготовка.
# В plans.WORK_GROUPS этого ID нет: он добавляется к «5.0 Уборка», а не заменяет список.
_CORN_SILAGE_CHOP_WT = 84
_HARVEST_GROUP = "5.0 Уборка"
_VEG_HARVEST_GROUP = "5.4 Уборка картофеля, лука"

WORK_GROUP_WT_EXTRAS: dict[str, tuple[int, ...]] = {
    _HARVEST_GROUP: (_CORN_SILAGE_CHOP_WT,),
}


def work_group_wt_map() -> dict[str, list[int]]:
    """Группы из WORK_GROUPS. К «5.0» добавляется 84, овощные ID остаются в «5.4»."""
    groups = {name: [int(x) for x in ids] for name, ids in WORK_GROUPS}
    harvest = groups.get(_HARVEST_GROUP)
    if harvest is not None:
        veg = set(groups.get(_VEG_HARVEST_GROUP) or [])
        ids = [wt for wt in harvest if wt not in veg]
        for extra in WORK_GROUP_WT_EXTRAS.get(_HARVEST_GROUP, ()):
            if extra not in ids:
                ids.append(extra)
        groups[_HARVEST_GROUP] = ids
    return groups


def work_type_ids_for_group(group_name: str) -> list[int]:
    return list(work_group_wt_map().get(group_name) or [])
