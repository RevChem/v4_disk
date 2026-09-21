#!/usr/bin/env python3
"""Benchmark report 10 without Namolot/NetWeight HTTP (stub ~0s)."""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path


def _load_module(script_path: Path, label: str):
    # Stub external 1C APIs before importing report module.
    import requests as _requests

    def _empty_namolot(*_a, **_k):
        return []

    def _empty_net(*_a, **_k):
        return []

    os.environ.setdefault("REPORT_DATE", "2026-09-16")
    os.environ["SVODKI_DISABLE_API_CACHE"] = "1"
    os.environ.pop("SVODKI_PREFETCH_DATA_PATH", None)
    os.environ.pop("SVODKI_API_CACHE_PATH", None)

    plan_dir = script_path.parent.resolve()
    for sub in (
        "utils/api",
        "utils/excel",
        "utils/season",
        "utils/shared",
    ):
        p = plan_dir / sub
        if p.is_dir():
            sys.path.insert(0, str(p))
    sys.path.insert(0, str(plan_dir))

    spec = importlib.util.spec_from_file_location(f"r10_{label}", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {script_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod

    t0 = time.perf_counter()
    spec.loader.exec_module(mod)
    load_s = time.perf_counter() - t0

    mod.fetch_namolot_records = _empty_namolot  # type: ignore[attr-defined]
    if hasattr(mod, "fetch_net_weight_records"):
        mod.fetch_net_weight_records = _empty_net  # type: ignore[attr-defined]

    return mod, load_s


def _run(mod, label: str) -> float:
    from datetime import date

    report_date = date.fromisoformat(os.environ["REPORT_DATE"])
    t0 = time.perf_counter()

    day_start, cutoff = mod.compute_time_range(report_date)
    cache = mod.prepare_harvest_cache(report_date)

    preload_wt_ids: set[int] = set(mod.GRAIN_HARVEST_WT_IDS) | set(mod.SILAGE_CORN_MOWING_WT_IDS)
    for _crop, (wt, _firms) in mod.CROP_CONFIGS.items():
        preload_wt_ids.add(int(wt))
    mod.preload_agro_operations(preload_wt_ids, mod.SEASON_YEAR)

    mod.build_general_harvest(report_date, cache)
    mod.build_vegetables_report(report_date)
    for crop_name in mod._REPORT10_CROP_SHEET_ORDER:
        cfg = mod.CROP_CONFIGS.get(crop_name)
        if not cfg:
            continue
        work_type_id, agrofirms = cfg
        mod.build_crop_report(crop_name, work_type_id, agrofirms, report_date, cache)

    elapsed = time.perf_counter() - t0
    print(
        f"[{label}] cropwise-only (no Namolot/NetWeight): {elapsed:.1f}s "
        f"(import {load_s:.1f}s, day_start={day_start}, cutoff={cutoff})",
        flush=True,
    )
    return elapsed


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    scripts = [
        ("plan", root / "plan" / "10_main_harvest.py"),
        ("svodki/plan", root / "svodki" / "plan" / "10_main_harvest.py"),
    ]
    results: list[tuple[str, float]] = []
    load_s = 0.0
    for label, path in scripts:
        if not path.is_file():
            print(f"SKIP {label}: {path} missing", flush=True)
            continue
        print(f"\n=== {label} ===", flush=True)
        mod, load_s = _load_module(path, label.replace("/", "_"))
        results.append((label, _run(mod, label)))

    print("\n--- summary (Namolot/NetWeight stubbed, DISABLE_API_CACHE=1) ---", flush=True)
    for label, sec in results:
        print(f"  {label}: {sec:.1f}s", flush=True)
