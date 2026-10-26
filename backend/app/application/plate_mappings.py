"""Ручные пары госномеров Namolot → Cropwise.

Список по умолчанию лежит рядом со сводкой 14. Сохранение с сайта пишет
отдельный файл в каталог загрузок: следующая генерация читает уже его.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.config import _upload_dir_path, settings
from app.legacy.json_store_io import atomic_write_json

_MAX_ITEMS = 300
_MAX_LEN = 64


def seed_path() -> Path:
    return Path(settings.svodki_root) / "plan" / "plate_mappings.json"


def override_path() -> Path:
    return _upload_dir_path() / "plate_mappings.json"


def active_path() -> Path:
    override = override_path()
    if override.is_file():
        return override
    return seed_path()


def _read_items(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"Файл сопоставлений повреждён: {path.name}")
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        namolot = str(item.get("namolot") or "").strip()
        cropwise = str(item.get("cropwise") or "").strip()
        if namolot and cropwise:
            out.append({"namolot": namolot, "cropwise": cropwise})
    return out


def load_items() -> list[dict[str, str]]:
    return _read_items(active_path())


def _plate_key(raw: str) -> str:
    return " ".join(raw.upper().split())


def save_items(items: list[dict]) -> list[dict[str, str]]:
    if len(items) > _MAX_ITEMS:
        raise ValueError(f"Слишком много пар: {len(items)}. Максимум {_MAX_ITEMS}.")
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        namolot = str((item or {}).get("namolot") or "").strip()
        cropwise = str((item or {}).get("cropwise") or "").strip()
        if not namolot or not cropwise:
            raise ValueError("В каждой строке нужны оба госномера: Namolot и Cropwise.")
        if len(namolot) > _MAX_LEN or len(cropwise) > _MAX_LEN:
            raise ValueError(f"Госномер длиннее {_MAX_LEN} символов.")
        key = _plate_key(namolot)
        if key in seen:
            raise ValueError(f"Номер Namolot повторяется: {namolot}")
        seen.add(key)
        cleaned.append({"namolot": namolot, "cropwise": cropwise})
    atomic_write_json(override_path(), {"items": cleaned}, ensure_ascii=False, indent=2)
    return cleaned
