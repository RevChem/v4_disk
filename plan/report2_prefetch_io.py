# -*- coding: utf-8 -*-
"""Сводка 2: machine_tasks/mtfmi из общего prefetch JSON (без повторных HTTP)."""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from typing import Any

_TASKS_BY_WT: dict[int, list[dict[str, Any]]] | None = None
_MTFMI_ROWS: list[dict[str, Any]] | None = None
_PREFETCH_PATH: str = ""


def _use_prefetch_tasks() -> bool:
    explicit = os.environ.get("SVODKI_REPORT2_USE_PREFETCH_TASKS")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip().lower() not in {"0", "false", "no"}
    # По умолчанию: если backend передал prefetch JSON — используем его.
    return bool((os.environ.get("SVODKI_PREFETCH_DATA_PATH") or "").strip())


def _load_prefetch() -> bool:
    global _TASKS_BY_WT, _MTFMI_ROWS, _PREFETCH_PATH
    if _TASKS_BY_WT is not None:
        return bool(_TASKS_BY_WT)
    _TASKS_BY_WT = {}
    _MTFMI_ROWS = []
    if not _use_prefetch_tasks():
        return False
    path = (os.environ.get("SVODKI_PREFETCH_DATA_PATH") or "").strip()
    if not path or not os.path.isfile(path):
        return False
    started = time.monotonic()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        print(f"  [r2 prefetch] не удалось прочитать {path}: {exc}", flush=True)
        return False
    endpoints = data.get("endpoints") or {}
    tasks = endpoints.get("machine_tasks") or []
    mtfmi = endpoints.get("machine_task_field_mapping_items") or []
    by_wt: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in tasks:
        wt = row.get("work_type_id")
        if wt is None:
            continue
        by_wt[int(wt)].append(row)
    _TASKS_BY_WT = dict(by_wt)
    _MTFMI_ROWS = list(mtfmi)
    _PREFETCH_PATH = path
    print(
        f"  [r2 prefetch] загружено за {time.monotonic() - started:.1f} с: "
        f"tasks={len(tasks)} mtfmi={len(mtfmi)} work_types={len(_TASKS_BY_WT)}",
        flush=True,
    )
    return True


def prefetch_tasks_by_wt() -> dict[int, list[dict[str, Any]]] | None:
    if not _load_prefetch() or not _TASKS_BY_WT:
        return None
    return _TASKS_BY_WT


def build_mtfmi_map(task_ids: list[int] | set[int]) -> dict[int, list[dict[str, Any]]] | None:
    """Один проход по mtfmi из prefetch (без HTTP). None — prefetch недоступен."""
    if not _load_prefetch() or _MTFMI_ROWS is None:
        return None
    need = {int(t) for t in task_ids if t is not None}
    if not need:
        return {}
    started = time.monotonic()
    out: dict[int, list[dict[str, Any]]] = {tid: [] for tid in need}
    for row in _MTFMI_ROWS:
        tid = row.get("machine_task_id")
        if tid is None:
            continue
        tid_i = int(tid)
        if tid_i in need:
            out[tid_i].append(row)
    print(
        f"  [r2 prefetch] mtfmi index: {len(need)} задач за {time.monotonic() - started:.1f} с",
        flush=True,
    )
    return out
