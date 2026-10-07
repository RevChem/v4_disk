#!/usr/bin/env python3
"""Замер RSS при генерации сводок (пример: python3 scripts/measure_report_memory.py 2026-09-11 1 2)."""
from __future__ import annotations

import gc
import json
import os
import resource
import sys
import threading
import time
from datetime import date


def rss_mb() -> float:
    with open("/proc/self/status", encoding="utf-8") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: measure_report_memory.py YYYY-MM-DD report_id ...", file=sys.stderr)
        sys.exit(2)

    report_date = date.fromisoformat(sys.argv[1])
    report_ids = sys.argv[2:]
    isolated = os.environ.get("MEASURE_ISOLATED", "").strip() in {"1", "true", "yes"}

    samples: list[float] = []
    stop = False

    def poller() -> None:
        while not stop:
            samples.append(rss_mb())
            time.sleep(0.25)

    gc.collect()
    baseline = rss_mb()
    t0 = time.perf_counter()
    thread = threading.Thread(target=poller, daemon=True)
    thread.start()

    if isolated and len(report_ids) == 1:
        import subprocess

        env = os.environ.copy()
        env["MEASURE_ISOLATED"] = "0"
        env["PYTHONPATH"] = "/app"
        cmd = [sys.executable, __file__, report_date.isoformat(), report_ids[0]]
        proc = subprocess.Popen(cmd, cwd="/app", env=env)
        while proc.poll() is None:
            try:
                with open(f"/proc/{proc.pid}/status", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            samples.append(int(line.split()[1]) / 1024)
                            break
            except OSError:
                pass
            time.sleep(0.25)
        rc = proc.returncode
        if rc != 0:
            sys.exit(rc)
        stop = True
        thread.join(timeout=2)
        peak = max(samples) if samples else baseline
        out = {
            "report_date": report_date.isoformat(),
            "report_ids_requested": report_ids,
            "mode": "isolated_subprocess",
            "memory_mb": {
                "peak_rss_sampled": round(peak, 1),
            },
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    from app.legacy.report_runner import generate_reports_to_disk

    created, failed, logs = generate_reports_to_disk(report_date, report_ids)

    stop = True
    thread.join(timeout=2)
    elapsed = time.perf_counter() - t0
    final = rss_mb()
    peak = max(samples) if samples else final
    ru = resource.getrusage(resource.RUSAGE_SELF)

    out = {
        "report_date": report_date.isoformat(),
        "report_ids_requested": report_ids,
        "created": [{"report_id": c.report_id, "title": c.title} for c in created],
        "failed": failed,
        "elapsed_sec": round(elapsed, 1),
        "memory_mb": {
            "baseline_rss": round(baseline, 1),
            "peak_rss_sampled": round(peak, 1),
            "final_rss": round(final, 1),
            "delta_peak_vs_baseline": round(peak - baseline, 1),
            "ru_maxrss_mb": round(ru.ru_maxrss / 1024, 1),
        },
        "log_tail": logs[-12:],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
