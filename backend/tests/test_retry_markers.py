"""Повтор скрипта сводки — только при настоящем временном сбое, а не из-за чисел в выводе."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SVODKI_ROOT", str(ROOT / "svodki"))
sys.path.insert(0, str(ROOT / "backend"))

from app.legacy import report_generation as rg  # noqa: E402


@pytest.mark.parametrize(
    "out",
    [
        "Обработано 1429.5 га\nKeyError: 'crop'",
        "task 50431 ok\nValueError: bad season",
        "итого 2504,3\nAssertionError",
        "ids: 5029, 15030, 4290\nTypeError",
    ],
)
def test_digits_in_output_do_not_trigger_retry(out):
    assert rg._subprocess_should_retry(1, out, "", 1, 5) is False


@pytest.mark.parametrize(
    "out",
    [
        "HTTP 429 Too Many Requests",
        "requests.exceptions.HTTPError: 502 Server Error: Bad Gateway for url: https://x",
        "httpx.HTTPStatusError: Server error '503 Service Unavailable' for url 'https://x'",
        "429 Client Error: Too Many Requests for url",
        "504 Gateway Timeout",
        "RuntimeError: status_code=429",
        "httpx.ReadTimeout: timed out",
        "json.decoder.JSONDecodeError: Expecting value",
    ],
)
def test_real_transient_failures_still_retry(out):
    assert rg._subprocess_should_retry(1, out, "", 1, 5) is True


def test_success_and_last_attempt_never_retry():
    assert rg._subprocess_should_retry(0, "HTTP 429", "", 1, 5) is False
    assert rg._subprocess_should_retry(1, "HTTP 429 Too Many Requests", "", 5, 5) is False


def test_oom_kill_is_still_transient():
    assert rg._subprocess_should_retry(-9, "", "", 1, 5) is True
    assert rg._subprocess_should_retry(137, "", "", 1, 5) is True
