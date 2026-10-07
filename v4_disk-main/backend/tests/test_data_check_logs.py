"""DATA_CHECK из всего stdout попадает в лог генерации и в файл для «Проверки сводок»."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SVODKI_ROOT", str(ROOT / "svodki"))
sys.path.insert(0, str(ROOT / "backend"))

from app.legacy import report_generation as rg  # noqa: E402


STDOUT = """
загрузка полей
DATA_CHECK_CALC_TOTAL=10.00
SEASON_MISMATCH сводка=2 сезон_задания=2026 год_культуры=2027
ещё одна служебная строка
DATA_CHECK [WARN] сводка=2 WT_UNMAPPED: 1 шт, 30.0 га — вид работ
DATA_CHECK [INFO] сводка=2 AREA_MANUAL: 1 шт, 1.0 га — вручную
DATA_CHECK итог сводка=2: проверено строк=1 заданий=1; CRIT=0 WARN=1
хвост 1
хвост 2
"""


def test_data_check_lines_skip_calc_total_and_keep_warnings():
    lines = rg._data_check_log_lines(STDOUT, "Сводка 2")
    assert len(lines) == 3
    assert all(line.startswith("[Сводка 2] DATA_CHECK") for line in lines)
    assert all("DATA_CHECK_CALC_TOTAL" not in line for line in lines)
    assert "WT_UNMAPPED" in lines[0]


def test_stdout_log_keeps_findings_outside_the_tail():
    logs: list[str] = []
    checks = rg._append_script_stdout_logs(logs, STDOUT, "Сводка 2", tail=2)
    joined = "\n".join(logs)
    assert "WT_UNMAPPED" in joined
    assert "SEASON_MISMATCH" in joined
    assert "хвост 2" in joined
    assert "DATA_CHECK_CALC_TOTAL" not in joined
    assert joined.count("WT_UNMAPPED") == 1
    assert any("WT_UNMAPPED" in line for line in checks)


def test_save_keeps_warn_and_drops_info(tmp_path, monkeypatch):
    monkeypatch.setattr(rg.settings, "upload_dir", str(tmp_path))
    day = date(2026, 10, 5)
    rg.save_generation_data_checks(
        day,
        "2",
        rg._data_check_log_lines(STDOUT, "Сводка 2"),
    )
    stored = rg.load_generation_data_checks(day)
    assert len(stored) == 1
    assert "WT_UNMAPPED" in stored[0]
    assert "AREA_MANUAL" not in stored[0]

    rg.save_generation_data_checks(day, "2", [])
    assert rg.load_generation_data_checks(day) == []


def test_atexit_print_is_in_captured_stdout(tmp_path):
    """Как _run_logged_subprocess_interruptible: stdout в файл, чтение после wait."""
    import subprocess
    import textwrap

    script = tmp_path / "child.py"
    script.write_text(
        textwrap.dedent(
            """
            import atexit
            def report():
                print("DATA_CHECK [WARN] сводка=2 WT_UNMAPPED: 1 шт, 30.0 га", flush=True)
            atexit.register(report)
            print("script body", flush=True)
            """
        ),
        encoding="utf-8",
    )
    out_path = tmp_path / "stdout.log"
    with open(out_path, "wb") as stdout_f:
        proc = subprocess.Popen([sys.executable, str(script)], stdout=stdout_f, stderr=subprocess.DEVNULL)
    rc = proc.wait(timeout=15)
    text = out_path.read_text(encoding="utf-8")
    assert rc == 0
    assert "script body" in text
    assert "WT_UNMAPPED" in text
    assert rg._data_check_log_lines(text, "Сводка 2")
