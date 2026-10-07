"""Курсорная пагинация Cropwise API (from_id + limit)."""
from __future__ import annotations

# В query limit до 1000; фактическая страница ~100 (см. proverka/season_cropwise).
CROPWISE_PAGE_LIMIT = 1000
CROPWISE_PAGE_SIZE = 100


def from_id_should_continue(meta: dict, chunk: list) -> bool:
    """Нужна ли следующая страница (last_id есть и страница «полная»)."""
    last_id = meta.get("last_record_id")
    if not last_id:
        return False
    obtained = int(meta.get("obtained_records") or len(chunk) or 0)
    return obtained >= CROPWISE_PAGE_SIZE
