"""REPORT_ALERT: заметные ошибки данных сводки → файл по дате → «Логи». Расчёт сводок не трогает."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SVODKI_ROOT", str(ROOT / "svodki"))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "svodki" / "plan" / "utils" / "shared"))

from app.legacy import report_generation as rg  # noqa: E402
from app.application.audit_activity import event_area, format_action_message  # noqa: E402
import report_alerts as ra  # noqa: E402

STDOUT = """
загрузка
REPORT_ALERT|ERROR|namolot_plate_unmatched|Гос.номер из Namolot 'АН 9333 16' (АгроРаздолье, 12.40 т) не найден
REPORT_ALERT|WARN|namolot_undated|Namolot: 2 записи без даты
REPORT_ALERT|ERROR|namolot_plate_unmatched|Гос.номер из Namolot 'АН 9333 16' (АгроРаздолье, 12.40 т) не найден
DATA_CHECK [CRIT] сводка=14 TOTAL_MISMATCH: 5 шт
хвост скрипта
"""


def test_script_and_backend_parsers_agree():
    line = ra.format_alert("namolot_plate_unmatched", "текст | с чертой\nи переносом", "ERROR")
    assert line.count("|") == 3 and "\n" not in line
    parsed_script = ra.parse_alert_line("[Сводка 14] " + line)
    assert parsed_script == rg.parse_report_alert("[Сводка 14] " + line)
    assert parsed_script["level"] == "ERROR" and "с чертой" in parsed_script["message"]
    assert rg.parse_report_alert("REPORT_ALERT|FATAL|x|y") is None
    assert rg.parse_report_alert("обычная строка") is None


def test_alert_is_emitted_once_per_run(capsys):
    ra._EMITTED.clear()
    ra.alert("c", "m")
    ra.alert("c", "m")
    assert capsys.readouterr().out.count("REPORT_ALERT|") == 1


def test_logs_keep_alerts_and_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(rg.settings, "upload_dir", str(tmp_path))
    logs: list[str] = []
    lines = rg._append_script_stdout_logs(logs, STDOUT, "Сводка 14", tail=3)
    assert sum("REPORT_ALERT" in x for x in logs) == 3
    day = date(2026, 10, 15)
    rg.save_generation_data_checks(day, "14", lines)
    alerts = rg.load_generation_alerts(day)["14"]
    assert [a["level"] for a in alerts] == ["ERROR", "ERROR", "WARN"]
    assert {a["code"] for a in alerts} >= {"namolot_plate_unmatched", "namolot_undated"}
    rg.save_generation_data_checks(day, "14", [])
    assert rg.load_generation_alerts(day) == {}


def test_audit_message_and_area():
    assert event_area("reports.generation.report_alert") == "Сводки"
    msg = format_action_message(
        "reports.generation.report_alert", "U",
        {"report_id": "14", "title": "Уборка", "summary_date": "2026-10-15",
         "alerts": [{"message": "Гос.номер не найден"}, {"message": "ещё"}]},
    )
    assert "Сводка 14" in msg and "не найден" in msg and "ещё 1" in msg
