# -*- coding: utf-8 -*-
"""Сводка 2: machine_tasks/mtfmi из общего prefetch JSON (обёртка над svodki_prefetch_io)."""

from __future__ import annotations

from svodki_prefetch_io import build_mtfmi_map, prefetch_tasks_by_wt

__all__ = ["prefetch_tasks_by_wt", "build_mtfmi_map"]
