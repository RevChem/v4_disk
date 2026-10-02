"""Параллельная загрузка независимых справочников (как сводка 2 _1 / files zip).

По умолчанию конвейер включён (SVODKI_PIPELINE=1).
Отключение: SVODKI_PIPELINE=0 или SVODKI_REPORT2_PIPELINE=0 — последовательно.
SVODKI_REFERENCE_WORKERS / SVODKI_REPORT2_REFERENCE_WORKERS — потоки (по умолчанию 9).
"""
from __future__ import annotations

import os

# Явно включаем конвейер, если переменные ещё не заданы (cron, IDE, subprocess).
os.environ.setdefault("SVODKI_PIPELINE", "1")
os.environ.setdefault("SVODKI_REPORT2_PIPELINE", "1")
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, TypeVar

T = TypeVar("T")


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "1" if default else "0").strip().lower()
    if raw in ("1", "true", "yes", "on", "да"):
        return True
    if raw in ("0", "false", "no", "off", "нет"):
        return False
    return default


def _parse_int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int((os.environ.get(name) or str(default)).strip())
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def pipeline_enabled() -> bool:
    if "SVODKI_REPORT2_PIPELINE" in os.environ:
        return _parse_bool_env("SVODKI_REPORT2_PIPELINE", True)
    if "SVODKI_PIPELINE" in os.environ:
        return _parse_bool_env("SVODKI_PIPELINE", True)
    return True


def reference_workers(default: int = 9) -> int:
    raw = (
        os.environ.get("SVODKI_REFERENCE_WORKERS")
        or os.environ.get("SVODKI_REPORT2_REFERENCE_WORKERS")
    )
    if raw:
        try:
            return max(1, min(16, int(raw.strip())))
        except ValueError:
            pass
    return max(1, min(16, default))


class BackgroundJob:
    """Фоновая задача в daemon-потоке."""

    def __init__(self, fn: Callable[[], T], name: str):
        self._fut: Future[T] = Future()

        def _run() -> None:
            try:
                self._fut.set_result(fn())
            except BaseException as exc:  # noqa: BLE001
                self._fut.set_exception(exc)

        threading.Thread(target=_run, name=name, daemon=True).start()

    def result(self) -> T:
        return self._fut.result()


def _timed_call(label: str, fn: Callable[[], T]) -> T:
    t0 = time.monotonic()
    out = fn()
    n = len(out) if hasattr(out, "__len__") else "-"
    print(f"    [timing] справочник {label}: {time.monotonic() - t0:.1f} с ({n})", flush=True)
    return out


def run_reference_jobs(
    jobs: dict[str, Callable[[], T]],
    *,
    parallel: bool | None = None,
    workers: int | None = None,
    timed: bool = True,
    thread_prefix: str = "svodki-ref",
) -> dict[str, T]:
    """Независимые вызовы get_all_from_id / get_all_page — параллельно по умолчанию."""
    use_parallel = pipeline_enabled() if parallel is None else parallel
    n_workers = reference_workers() if workers is None else workers
    results: dict[str, T] = {}

    if use_parallel and n_workers > 1 and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=n_workers, thread_name_prefix=thread_prefix) as ex:
            futs = {
                name: ex.submit(_timed_call if timed else fn, name, fn)
                for name, fn in jobs.items()
            }
            for name, fut in futs.items():
                results[name] = fut.result()
    else:
        for name, fn in jobs.items():
            results[name] = _timed_call(name, fn) if timed else fn()
    return results
