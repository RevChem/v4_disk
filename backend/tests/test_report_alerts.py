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


def test_places_attach_to_alert_and_stay_out_of_the_paragraph(tmp_path, monkeypatch):
    monkeypatch.setattr(rg.settings, "upload_dir", str(tmp_path))
    day = date(2026, 10, 19)
    places = ["задание=187080 поле=1328 covered=66.00 поле=57.00", "задание=188074 поле=1237", "задание=1", "задание=2"]
    import json
    lines = [
        "DATA_CHECK [CRIT] сводка=2 CROP_UNRESOLVED: 4 шт, 10.0 га — history_item не найден или без культуры | "
        + "; ".join(places[:3]),
        "DATA_CHECK_PLACES сводка=2 code=CROP_UNRESOLVED " + json.dumps(places, ensure_ascii=False),
        "DATA_CHECK [CRIT] сводка=2 AREA_GT_FIELD: 309 шт, 10960.2 га — площадь строки больше площади поля | задание=187080",
    ]
    logs: list[str] = []
    saved = rg._append_script_stdout_logs(logs, "\n".join(lines), "Сводка 2", tail=5)
    assert not any("DATA_CHECK_PLACES" in line for line in logs)
    rg.save_generation_data_checks(day, "2", saved)
    alerts = rg.load_generation_alerts(day)["2"]
    assert alerts[0]["places"] == places
    assert alerts[0]["message"].count("задание=") == 3
    assert rg.load_generation_data_checks(day)[0].count("задание=") == 3
    assert "DATA_CHECK_PLACES" not in rg.load_generation_data_checks(day)[0]
    assert rg.load_generation_data_check_places(day)["2:CROP_UNRESOLVED"] == places
    assert all("AREA_GT_FIELD" not in a["message"] for a in alerts)
    assert all("AREA_GT_FIELD" not in line for line in rg.load_generation_data_checks(day))


def test_saved_area_gt_field_is_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(rg.settings, "upload_dir", str(tmp_path))
    day = date(2026, 10, 19)
    folder = tmp_path / day.isoformat() / ".data_check" / "alerts"
    folder.mkdir(parents=True)
    (folder / "2.json").write_text(
        '{"report_id": "2", "alerts": ['
        '{"level": "ERROR", "code": "data_check_crit.0", "message": "сводка=2 AREA_GT_FIELD: 316 шт"},'
        '{"level": "ERROR", "code": "data_check_crit.1", "message": "сводка=2 CROP_UNRESOLVED: 1 шт"}'
        "]}",
        encoding="utf-8",
    )
    alerts = rg.load_generation_alerts(day)["2"]
    assert [a["message"] for a in alerts] == ["сводка=2 CROP_UNRESOLVED: 1 шт"]


def test_audit_message_and_area():
    assert event_area("reports.generation.report_alert") == "Сводки"
    msg = format_action_message(
        "reports.generation.report_alert", "U",
        {"report_id": "14", "title": "Уборка", "summary_date": "2026-10-15",
         "alerts": [{"message": "Гос.номер не найден"}, {"message": "ещё"}]},
    )
    assert "Сводка 14" in msg and "не найден" in msg and "ещё 1" in msg
