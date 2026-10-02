"""Сводка 1 standalone: прямой вызов 1_main_ammonium__1_.py (без v2 preload)."""

from __future__ import annotations

import os
import time

from report1_fast.legacy import load_legacy


def _enable_standalone_env() -> None:
    os.environ["REPORT1_STANDALONE"] = "1"
    os.environ.pop("REPORT1_SKIP_PERF_GROUPS", None)
    # Оркестратор задаёт skip harvest + prefetch при 10/11 в той же пачке — не сбрасываем.
    if not (os.environ.get("REPORT1_SKIP_HARVEST_FETCH") or "").strip():
        os.environ.pop("REPORT1_SKIP_HARVEST_FETCH", None)
    if not (os.environ.get("SVODKI_PREFETCH_DATA_PATH") or "").strip():
        os.environ.pop("SVODKI_PREFETCH_DATA_PATH", None)
        os.environ.pop("SVODKI_PREFETCH_STRICT", None)


async def build_report_async_standalone() -> None:
    _enable_standalone_env()
    legacy = load_legacy()
    build_fn = getattr(legacy, "build_report_async", None)
    if not callable(build_fn):
        raise RuntimeError("1_main_ammonium__1_.py: build_report_async не найден")
    t0 = time.monotonic()
    await build_fn()
    print(f"[standalone] готово за {time.monotonic() - t0:.1f} с")


def run_report1_standalone() -> None:
    import asyncio

    _enable_standalone_env()
    asyncio.run(build_report_async_standalone())
