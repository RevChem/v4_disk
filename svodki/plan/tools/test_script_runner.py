"""Запуск скриптов-проверок tools/test_*.py как отдельных процессов (код выхода 0 = успех)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import SCRIPT_STYLE

PLAN = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("script", SCRIPT_STYLE)
def test_script_passes(script: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-I", str(Path(__file__).with_name(script))],
        cwd=PLAN,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
