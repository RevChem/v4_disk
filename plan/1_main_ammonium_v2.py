# Сводка 1 v2 — быстрая сборка (те же таблицы, единый кэш mtfmi + prefetch).
# Включение на сервере: SVODKI_REPORT1_SCRIPT=1_main_ammonium_v2.py
from __future__ import annotations

from report1_fast.runner import run_report1_v2

if __name__ == "__main__":
    run_report1_v2()
