#!/usr/bin/env python3
"""Последовательный прогон сводок с замером wall-time и строк [timing]/[pipeline].

Примеры::

    python tools/benchmark_svodki_timing.py                 # базовый набор (2–9)
    python tools/benchmark_svodki_timing.py --all           # + 0, 0_1, 1_dop, 10, 12, 14
    python tools/benchmark_svodki_timing.py 4_main 6_main   # по подстроке имени файла
    python tools/benchmark_svodki_timing.py --json before.json
    python tools/benchmark_svodki_timing.py --json after.json --compare before.json

Строки ``[timing] этап: N с`` печатает ``utils/shared/svodki_timing.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PLAN = Path(__file__).resolve().parents[1]
REPORT_DATE = os.environ.get("REPORT_DATE", "2026-10-01")

# Сначала 2, затем типовые async-отчёты
SCRIPTS: tuple[str, ...] = (
    "2_main_ideal_compact.py",
    "3_main_real.py",
    "4_main_fixed.py",
    "5_main_fixed.py",
    "6_main_3.py",
    "7_main__4_.py",
    "8_main_fixed.py",
    "9_main_fixed.py",
)

# Остальные сводки (подключаются флагом --all)
EXTRA_SCRIPTS: tuple[str, ...] = (
    "0_main_status_vpr.py",
    "0_1_main_status_vpr_morning.py",
    "1_main_ammonium__1_.py",
    "1_main_ammonium_v2.py",
    "1_main_dop.py",
    "10_main_harvest.py",
    "12_main_harvest_idle.py",
    "14.main.py",
)

TOTAL_RE = re.compile(r"\[timing\]\s*ИТОГО:\s*([0-9.]+)", re.IGNORECASE)

TIMING_RE = re.compile(
    r"\[(?:timing|pipeline|r2 live|prefetch)[^\]]*\][^\n]*",
    re.IGNORECASE,
)


def _select_scripts(args: argparse.Namespace) -> list[str]:
    pool = list(SCRIPTS) + (list(EXTRA_SCRIPTS) if args.all or args.names else [])
    if not args.names:
        return pool
    picked = [s for s in pool if any(n in s for n in args.names)]
    return picked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="подстроки имён скриптов (по умолчанию — базовый набор)")
    ap.add_argument("--all", action="store_true", help="добавить сводки 0, 0_1, 1 (legacy и v2), 1_dop, 10, 12, 14")
    ap.add_argument("--json", metavar="PATH", help="сохранить результаты прогона в JSON")
    ap.add_argument("--compare", metavar="PATH", help="сравнить wall-time с прошлым JSON")
    args = ap.parse_args()

    env = os.environ.copy()
    env.setdefault("REPORT_DATE", REPORT_DATE)
    env.setdefault("SVODKI_PIPELINE", "1")
    env.setdefault("PYTHONUNBUFFERED", "1")
    plan_s = str(PLAN)
    env["PYTHONPATH"] = plan_s + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

    rows: list[tuple[str, float, int, list[str]]] = []
    for script in _select_scripts(args):
        path = PLAN / script
        if not path.is_file():
            print(f"SKIP (нет файла): {script}", flush=True)
            continue
        print(f"\n{'=' * 60}\n>>> {script}\n{'=' * 60}", flush=True)
        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(PLAN),
            env=env,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("SVODKI_BENCH_TIMEOUT", "900")),
        )
        wall = time.monotonic() - t0
        out = (proc.stdout or "") + (proc.stderr or "")
        marks = TIMING_RE.findall(out)
        rows.append((script, wall, proc.returncode, marks))
        if proc.returncode != 0:
            tail = out.strip().splitlines()[-8:]
            print("FAILED rc=%s" % proc.returncode, flush=True)
            for line in tail:
                print(line, flush=True)
        else:
            print(f"OK wall={wall:.1f}s", flush=True)
        for m in marks[:25]:
            print(f"  {m}", flush=True)
        if len(marks) > 25:
            print(f"  ... ещё {len(marks) - 25} строк timing", flush=True)

    print(f"\n{'=' * 60}\nСводка (REPORT_DATE={REPORT_DATE})\n{'=' * 60}", flush=True)
    prev: dict[str, float] = {}
    if args.compare:
        try:
            prev = {r["script"]: r["wall"] for r in json.loads(Path(args.compare).read_text(encoding="utf-8"))}
        except (OSError, ValueError, KeyError) as exc:
            print(f"  (не удалось прочитать {args.compare}: {exc})", flush=True)
    result_rows = []
    for script, wall, rc, marks in rows:
        status = "OK" if rc == 0 else f"ERR({rc})"
        cmp_txt = ""
        if script in prev and prev[script] > 0 and rc == 0:
            delta = wall - prev[script]
            cmp_txt = f"  [было {prev[script]:.1f}s, {delta:+.1f}s, {delta / prev[script] * 100:+.0f}%]"
        print(f"  {wall:7.1f}s  {status:8s}  {script}  ({len(marks)} timing lines){cmp_txt}", flush=True)
        total = next((float(m.group(1)) for m in map(TOTAL_RE.search, reversed(marks)) if m), None)
        result_rows.append({"script": script, "wall": round(wall, 2), "rc": rc, "script_total": total})
    if args.json:
        Path(args.json).write_text(json.dumps(result_rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  результаты: {args.json}", flush=True)
    return 0 if all(r[2] == 0 for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
