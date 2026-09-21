"""Настройки интерфейса пользователя (тема и фон) — uploads/_interface/{user_id}/."""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

from app.application.interface_settings import DEFAULT_INTERFACE_SETTINGS, normalize_interface_settings
from app.config import settings

_DATA_URL_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.DOTALL)
_MAX_BG_BYTES = 4 * 1024 * 1024
_BG_NAME = "background"


def _interface_root() -> Path:
    root = Path(settings.upload_dir) / "_interface"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _user_dir(user_id: int) -> Path:
    path = _interface_root() / str(int(user_id))
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path(user_id: int) -> Path:
    return _user_dir(user_id) / "settings.json"


def load_user_settings(user_id: int) -> dict[str, Any]:
    path = settings_path(user_id)
    if not path.is_file():
        return dict(DEFAULT_INTERFACE_SETTINGS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_INTERFACE_SETTINGS)
    return normalize_interface_settings(raw if isinstance(raw, dict) else None)


def _save_user_settings(user_id: int, data: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_interface_settings(data)
    path = settings_path(user_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return normalized


def user_background_path(user_id: int) -> Path | None:
    base = _user_dir(user_id)
    for ext in (".webp", ".jpg", ".jpeg", ".png", ".gif"):
        candidate = base / f"{_BG_NAME}{ext}"
        if candidate.is_file():
            return candidate
    return None


def _ext_for_mime(mime: str) -> str:
    mapping = {
        "image/webp": ".webp",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
    }
    return mapping.get(mime.lower(), ".jpg")


def _clear_user_background(user_id: int) -> None:
    base = _user_dir(user_id)
    for ext in (".webp", ".jpg", ".jpeg", ".png", ".gif"):
        path = base / f"{_BG_NAME}{ext}"
        if path.is_file():
            path.unlink(missing_ok=True)


def _write_background_from_data_url(user_id: int, data_url: str) -> None:
    match = _DATA_URL_RE.match(str(data_url or "").strip())
    if not match:
        raise ValueError("Некорректный формат изображения (ожидается data URL).")
    mime = match.group(1)
    try:
        payload = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:
        raise ValueError("Не удалось декодировать изображение.") from exc
    if len(payload) > _MAX_BG_BYTES:
        raise ValueError("Файл слишком большой (макс. 4 МБ).")
    if not mime.startswith("image/"):
        raise ValueError("Допустимы только изображения.")
    _clear_user_background(user_id)
    dest = _user_dir(user_id) / f"{_BG_NAME}{_ext_for_mime(mime)}"
    dest.write_bytes(payload)


def update_user_interface_settings(
    user_id: int,
    *,
    theme: str,
    custom_background: str | None = ...,
) -> dict[str, Any]:
    current = load_user_settings(user_id)
    current["theme"] = theme
    if custom_background is not ...:
        text = str(custom_background or "").strip()
        if not text:
            _clear_user_background(user_id)
        else:
            _write_background_from_data_url(user_id, text)
        current["background_version"] = time.time_ns() // 1_000_000
    return _save_user_settings(user_id, current)
