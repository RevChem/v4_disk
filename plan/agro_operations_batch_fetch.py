# -*- coding: utf-8 -*-
"""Пакетная загрузка agro_operations (фильтр work_type_id через запятую).

Сначала batch work_type_id=1,2,3,...; при пустом ответе — по одному id на запрос.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

AO_WT_ID_BATCH_SIZE = max(
    1,
    int(os.environ.get("SVODKI_AGRO_OPS_WT_BATCH_SIZE", "25") or 25),
)

GetAllSync = Callable[[str, dict[str, Any] | None], list[dict]]


def _chunked(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _dedupe_ops(rows: list[dict]) -> list[dict]:
    by_id: dict[int, dict] = {}
    for row in rows:
        oid = row.get("id")
        if oid is not None:
            by_id[int(oid)] = row
    return list(by_id.values())


def fetch_agro_operations_by_wt_sync(
    get_all_fn: GetAllSync,
    wt_ids: list[int] | set[int],
    base_params: dict[str, Any],
    *,
    endpoint: str = "agro_operations",
    log_prefix: str = "",
) -> list[dict]:
    """agro_operations с фильтром work_type_id на стороне API (sync)."""
    if not wt_ids:
        return []

    wt_list = sorted({int(w) for w in wt_ids})
    prefix = log_prefix or ""
    all_ops: list[dict] = []

    def _fetch_one(wt_id: int) -> list[dict]:
        return get_all_fn(endpoint, {**base_params, "work_type_id": wt_id})

    def _fetch_batch(batch: list[int]) -> list[dict]:
        return get_all_fn(
            endpoint,
            {**base_params, "work_type_id": ",".join(str(w) for w in batch)},
        )

    if len(wt_list) == 1:
        return _dedupe_ops(_fetch_one(wt_list[0]))

    batches = _chunked(wt_list, AO_WT_ID_BATCH_SIZE)
    print(
        f"  {prefix}agro_operations: {len(wt_list)} work types "
        f"({len(batches)} batch)...",
        flush=True,
    )

    failed_batches: list[list[int]] = []
    for batch in batches:
        try:
            all_ops.extend(_fetch_batch(batch))
        except Exception as exc:
            print(
                f"  {prefix}agro_operations batch wt (n={len(batch)}) failed: {exc}",
                flush=True,
            )
            failed_batches.append(batch)

    if not all_ops:
        print(
            f"  {prefix}agro_operations: batch work_type_id filter returned 0, "
            "fallback to one request per work_type_id",
            flush=True,
        )
        for wid in wt_list:
            all_ops.extend(_fetch_one(wid))
        return _dedupe_ops(all_ops)

    for batch in failed_batches:
        for wid in batch:
            all_ops.extend(_fetch_one(wid))

    return _dedupe_ops(all_ops)
