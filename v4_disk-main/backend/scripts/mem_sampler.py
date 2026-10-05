#!/usr/bin/env python3
"""Пик RAM и ответы 429 за пачку сводок -> mem.tsv (для подбора SVODKI_GENERATION_MAX_WORKERS).

Запуск параллельно с генерацией (на хосте или внутри контейнера worker-heavy):
    python3 backend/scripts/mem_sampler.py --out /tmp/mem.tsv --interval 2 [--log /path/to/worker.log]
Остановка: Ctrl+C — печатает итог (пик RSS, пик числа процессов, число 429 в логе).

Колонки mem.tsv: ts_iso, rss_mb (сумма по python/soffice/libreoffice), n_proc, n_python, n_soffice, new_429.
Правило подбора: поднимать 2 -> 3 -> 4, пока пик < ~75% WORKER_HEAVY_MEM_LIMIT и new_429 ~ 0.
"""
from __future__ import annotations

import argparse
import os
import re
import time
from datetime import datetime
from pathlib import Path

_PAT_429 = re.compile(r"\b429\b")


def _scan() -> tuple[float, int, int, int]:
    rss = 0.0
    n = n_py = n_so = 0
    page_kb = os.sysconf("SC_PAGE_SIZE") / 1024.0
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip().lower()
            if not (comm.startswith("python") or "soffice" in comm or "libreoffice" in comm):
                continue
            fields = Path(f"/proc/{pid}/statm").read_text().split()
            rss += int(fields[1]) * page_kb / 1024.0
        except (OSError, IndexError, ValueError):
            continue
        n += 1
        if comm.startswith("python"):
            n_py += 1
        else:
            n_so += 1
    return rss, n, n_py, n_so


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="mem.tsv")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--log", default="", help="лог воркера: считаем строки с «429»")
    a = ap.parse_args()
    log_pos = 0
    total_429 = 0
    peak = 0.0
    peak_n = 0
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write("ts\trss_mb\tn_proc\tn_python\tn_soffice\tnew_429\n")
        try:
            while True:
                rss, n, n_py, n_so = _scan()
                new429 = 0
                if a.log and os.path.isfile(a.log):
                    with open(a.log, "r", encoding="utf-8", errors="replace") as lf:
                        lf.seek(log_pos)
                        chunk = lf.read()
                        log_pos = lf.tell()
                    new429 = len(_PAT_429.findall(chunk))
                    total_429 += new429
                peak = max(peak, rss)
                peak_n = max(peak_n, n)
                fh.write(f"{datetime.now().isoformat(timespec='seconds')}\t{rss:.0f}\t{n}\t{n_py}\t{n_so}\t{new429}\n")
                fh.flush()
                time.sleep(a.interval)
        except KeyboardInterrupt:
            print(f"peak_rss_mb={peak:.0f} peak_procs={peak_n} http_429={total_429} -> {a.out}")


if __name__ == "__main__":
    main()
