"""Пользовательские подписи «ВИД РАБОТ» в шапке PDF (uploads/pdf_header_labels.json)."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.config import settings

_FILE_NAME = "pdf_header_labels.json"
_PREFIX = "ВИД РАБОТ: "
_GROUP_NUM_RE = re.compile(r"\d+\.\d+\s+")

# Шаблоны по умолчанию → сводки, где они встречаются.
_KNOWN_WORK_LINES: dict[str, list[str]] = {
    "ВИД РАБОТ: 1.1 Внесение минеральных удобрений": ["1", "1.18", "3"],
    "ВИД РАБОТ: 2.1 Боронование зяби": ["4"],
    "ВИД РАБОТ: 2.9 Дискование": ["7"],
    "ВИД РАБОТ: 2.11 Культивация": ["8"],
    "ВИД РАБОТ: 2.6 Фрезерование": ["8"],
    "ВИД РАБОТ: 2.4 Вспашка, 2.8 Глубокорыхление": ["9"],
    "ВИД РАБОТ: 4.0 Посев": ["5"],
    "ВИД РАБОТ: 4.1 Посев (овощи)": ["5"],
    "ВИД РАБОТ: 3.1 Опрыскивание": ["6"],
    "ВИД РАБОТ: 3.2 Опрыскивание (овощи)": ["6"],
    "ВИД РАБОТ: 5.0 Уборка": ["10", "13", "14"],
    "ВИД РАБОТ: 5.4 Уборка картофеля, лука": ["10", "13"],
    "ВИД РАБОТ: 5.4 Уборка овощей": ["10", "13"],
    "ВИД РАБОТ: 5.2 Уборка зерновых и зернобобовых": ["11"],
    "ВИД РАБОТ: Уборка": ["11"],
    "ВИД РАБОТ: Уборка овощей": ["11"],
    "ВИД РАБОТ: Уборка овощи": ["11", "14"],
    "ВИД РАБОТ: несколько групп (удобрения, обработка почвы, посев, защита, уборка и др.)": ["2"],
}

# Листы «Выраб. — …» сводки 1 (полные имена групп).
for _group in (
    "2.1 Боронование",
    "2.9 Дискование",
    "2.6 Фрезерование",
    "2.11 Культивация",
    "4.0 Посев",
    "4.1 Посев (овощи)",
    "3.1 Опрыскивание",
    "3.2 Опрыскивание (овощи)",
    "2.12 Прикатывание",
    "2.7 Гребнеобразование",
    "5.0 Уборка",
    "5.4 Уборка картофеля, лука",
):
    _line = f"{_PREFIX}{_group}"
    _KNOWN_WORK_LINES.setdefault(_line, []).append("1")
    _KNOWN_WORK_LINES[_line] = sorted(set(_KNOWN_WORK_LINES[_line]))

# Виды работ уборочных сводок 10/13 (report_generation._HARVEST_WT_LABEL_BY_ID / _BY_CROP).
for _group in (
    "5.1 Уборка подсолнечника",
    "5.3 Уборка кукурузы на зерно",
    "5.8 Уборка рапса, горчицы, гречихи и проса",
    "6.3 Скашивание с измельчением кукурузы",
):
    _line = f"{_PREFIX}{_group}"
    _KNOWN_WORK_LINES[_line] = sorted(set(_KNOWN_WORK_LINES.get(_line, [])) | {"10", "13"})


def _upload_dir() -> Path:
    upload = Path(settings.upload_dir).expanduser()
    if not upload.is_absolute():
        upload = (Path.cwd() / upload).resolve()
    upload.mkdir(parents=True, exist_ok=True)
    return upload


def pdf_header_labels_path() -> Path:
    configured = (getattr(settings, "svodki_pdf_header_labels_path", None) or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return _upload_dir() / _FILE_NAME


def load_pdf_header_labels() -> dict[str, Any]:
    path = pdf_header_labels_path()
    if not path.is_file():
        return {"version": 1, "work_line_map": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "work_line_map": {}}
    if not isinstance(raw, dict):
        return {"version": 1, "work_line_map": {}}
    work_line_map = raw.get("work_line_map")
    if not isinstance(work_line_map, dict):
        work_line_map = {}
    cleaned_map: dict[str, str] = {}
    for key, val in work_line_map.items():
        k = str(key or "").strip()
        v = str(val or "").strip()
        if k and v and v != k:
            cleaned_map[k] = v
    return {"version": 1, "work_line_map": cleaned_map}


def save_pdf_header_labels(data: dict[str, Any]) -> Path:
    path = pdf_header_labels_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    work_line_map = data.get("work_line_map")
    if not isinstance(work_line_map, dict):
        work_line_map = {}
    cleaned_map: dict[str, str] = {}
    for key, val in work_line_map.items():
        k = str(key or "").strip()
        v = str(val or "").strip()
        if not k or not v or v == k:
            continue
        if len(v) > 240:
            v = v[:240]
        cleaned_map[k] = v
    payload = {"version": 1, "work_line_map": cleaned_map}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def reset_pdf_header_labels() -> None:
    path = pdf_header_labels_path()
    if path.is_file():
        path.unlink()


def strip_work_line_group_numbers(work_line: str) -> str:
    """«ВИД РАБОТ: 2.11 Культивация» → «ВИД РАБОТ: Культивация»."""
    text = str(work_line or "").strip()
    if not text.startswith(_PREFIX):
        return text
    body = text[len(_PREFIX):]
    body = _GROUP_NUM_RE.sub("", body)
    body = " ".join(body.split())
    return f"{_PREFIX}{body}" if body else text


def _effective_work_line(default_line: str, work_line_map: dict[str, str]) -> str:
    default_line = str(default_line or "").strip()
    if not default_line:
        return default_line
    return str(work_line_map.get(default_line) or default_line).strip() or default_line


def get_editor_state() -> dict[str, Any]:
    saved = load_pdf_header_labels()
    work_line_map: dict[str, str] = saved.get("work_line_map") or {}
    items: list[dict[str, Any]] = []
    for default_line in sorted(_KNOWN_WORK_LINES.keys(), key=lambda s: s.casefold()):
        effective = _effective_work_line(default_line, work_line_map)
        items.append(
            {
                "default_work_line": default_line,
                "work_line": effective,
                "is_custom": effective != default_line,
                "report_ids": list(_KNOWN_WORK_LINES.get(default_line) or []),
            }
        )
    return {
        "path": str(pdf_header_labels_path()),
        "items": items,
        "work_line_map": dict(work_line_map),
    }


def update_work_line_map(updates: dict[str, str | None]) -> dict[str, Any]:
    saved = load_pdf_header_labels()
    work_line_map: dict[str, str] = dict(saved.get("work_line_map") or {})
    for default_line, custom in updates.items():
        key = str(default_line or "").strip()
        if not key:
            continue
        if key not in _KNOWN_WORK_LINES:
            # Раньше такие строки молча пропускались, и правка «сохранялась», но не работала.
            raise ValueError(f"Неизвестная строка «{key}» — её нельзя переименовать.")
        if custom is None or str(custom).strip() == "" or str(custom).strip() == key:
            work_line_map.pop(key, None)
            continue
        val = str(custom).strip()
        if len(val) > 240:
            raise ValueError(f"Слишком длинная подпись для «{key}»")
        work_line_map[key] = val
    save_pdf_header_labels({"work_line_map": work_line_map})
    return get_editor_state()


def apply_pdf_header_label_overrides(
    headers: list[tuple[str, str]],
    report_id: str | None = None,
) -> list[tuple[str, str]]:
    del report_id
    saved = load_pdf_header_labels()
    work_line_map: dict[str, str] = saved.get("work_line_map") or {}
    if not work_line_map:
        return headers
    out: list[tuple[str, str]] = []
    for title, work in headers:
        w = str(work or "").strip()
        if w in work_line_map:
            w = work_line_map[w]
        out.append((title, w))
    return out
