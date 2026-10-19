# -*- coding: utf-8 -*-
"""Заметные предупреждения/ошибки данных при генерации сводки.

Скрипт сводки печатает строку

    REPORT_ALERT|<ERROR|WARN>|<код>|<текст>

Бэкенд (report_generation) собирает такие строки из stdout, сохраняет их по сводке и дате,
а в интерфейсе у сводки загорается красная (ERROR) или оранжевая (WARN) точка с этим текстом.
Расчёт сводки функция не меняет — только сообщает, что данные неполные или неточные.

    from report_alerts import alert
    alert("namolot_plate_unmatched", "Гос.номер «АН 9333 16» не найден в Cropwise", level="ERROR")
"""
from __future__ import annotations

MARK = "REPORT_ALERT"
LEVELS = ("ERROR", "WARN")
_MAX_TEXT = 600

_EMITTED: set[tuple[str, str]] = set()


def format_alert(code: str, message: str, level: str = "ERROR") -> str:
    lvl = level.upper() if level.upper() in LEVELS else "ERROR"
    clean_code = "".join(ch for ch in str(code) if ch.isalnum() or ch in "._-") or "alert"
    text = " ".join(str(message).replace("|", "/").split())
    if len(text) > _MAX_TEXT:
        text = text[: _MAX_TEXT - 1] + "…"
    return f"{MARK}|{lvl}|{clean_code}|{text}"


def alert(code: str, message: str, *, level: str = "ERROR") -> None:
    """Печатает предупреждение (одинаковые код+текст в одном запуске — один раз)."""
    key = (str(code), str(message))
    if key in _EMITTED:
        return
    _EMITTED.add(key)
    print(format_alert(code, message, level), flush=True)


def parse_alert_line(line: str) -> dict[str, str] | None:
    """Строка stdout → {level, code, message} или None. Допускает префикс «[Сводка 14] »."""
    text = str(line or "")
    pos = text.find(MARK + "|")
    if pos < 0:
        return None
    parts = text[pos + len(MARK) + 1:].split("|", 2)
    if len(parts) != 3 or parts[0] not in LEVELS:
        return None
    return {"level": parts[0], "code": parts[1], "message": parts[2].strip()}
