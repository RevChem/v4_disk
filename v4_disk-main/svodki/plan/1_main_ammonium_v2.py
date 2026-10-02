# -*- coding: utf-8 -*-
"""Сводка 1 v2: те же листы, что у 1_main_ammonium__1_.py, но данные грузятся
одним широким проходом (report1_fast.preload_all) с таймерами этапов.

Запуск: SVODKI_REPORT1_SCRIPT=1_main_ammonium_v2.py (бэкенд) или напрямую
``python 1_main_ammonium_v2.py``.
"""
from __future__ import annotations

import plans  # noqa: F401  # bootstrap utils/* в sys.path

from report1_fast.runner import run_report1_v2

if __name__ == "__main__":
    run_report1_v2()
