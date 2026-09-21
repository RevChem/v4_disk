# Сводка 1 standalone — все листы из живого API, без доноров 3–8 и PDF-graft.
# На сервере: SVODKI_REPORT1_SCRIPT=1_main_ammonium_standalone.py
from __future__ import annotations

from report1_standalone.runner import run_report1_standalone

if __name__ == "__main__":
    run_report1_standalone()
