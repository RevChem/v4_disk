"""Общие замеры этапов сводок: строки ``[timing] …`` и итог в конце прогона.

Использование в скрипте сводки::

    from svodki_timing import stage, timing_start, timing_finish

    timing_start()
    with stage("справочники"):
        ...
    timing_finish()          # печатает «[timing] ИТОГО: …» и «[http] …»

Формат строк совпадает с тем, что ловит ``tools/benchmark_svodki_timing.py``
(``TIMING_RE``), поэтому бенчмарк видит этапы всех сводок одинаково.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

_STAGE_TIMES: dict[str, float] = {}
_T_START: float | None = None


def timing_start() -> None:
    """Сбрасывает накопленные этапы и запускает общий таймер."""
    global _T_START
    _STAGE_TIMES.clear()
    _T_START = time.monotonic()


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Замер этапа: печатает длительность и копит итог для сводки в конце."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        dt = time.monotonic() - t0
        _STAGE_TIMES[name] = _STAGE_TIMES.get(name, 0.0) + dt
        print(f"  [timing] {name}: {dt:.1f} с", flush=True)


def stage_add(name: str, seconds: float) -> None:
    """Записывает этап, измеренный вручную (например, wall фоновой задачи)."""
    _STAGE_TIMES[name] = _STAGE_TIMES.get(name, 0.0) + seconds
    print(f"  [timing] {name}: {seconds:.1f} с", flush=True)


def stage_times() -> dict[str, float]:
    """Копия накопленных этапов (для тестов и сравнения прогонов)."""
    return dict(_STAGE_TIMES)


def timing_finish() -> None:
    """Печатает итоговую строку ``[timing] ИТОГО`` и сводку по повторам HTTP."""
    total = (time.monotonic() - _T_START) if _T_START is not None else sum(_STAGE_TIMES.values())
    print(
        f"\n[timing] ИТОГО: {total:.1f} с; этапы: "
        + ", ".join(f"{k}={v:.0f}с" for k, v in _STAGE_TIMES.items()),
        flush=True,
    )
    try:
        from async_http import retry_summary

        print(f"[http] {retry_summary()}", flush=True)
    except ImportError:
        pass
