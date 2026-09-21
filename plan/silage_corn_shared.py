"""Re-export для обратной совместимости — логика в task_area_shared."""

from task_area_shared import (
    CORN_GRAIN_CROP,
    CORN_SILAGE_CROP,
    HARVEST_SILAGE_CORN_REPORT_IDS,
    SILAGE_CORN_MOWING_WT_IDS,
    collect_silage_corn_field_ids,
    crop_key_for_harvest_area,
    resolve_corn_subtype,
)

__all__ = [
    "CORN_GRAIN_CROP",
    "CORN_SILAGE_CROP",
    "HARVEST_SILAGE_CORN_REPORT_IDS",
    "SILAGE_CORN_MOWING_WT_IDS",
    "collect_silage_corn_field_ids",
    "crop_key_for_harvest_area",
    "resolve_corn_subtype",
]
