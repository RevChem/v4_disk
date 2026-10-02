"""Настройки интерфейса (тема и фон): хранятся отдельно для каждого пользователя."""
from __future__ import annotations

from typing import Any

THEME_IDS: tuple[str, ...] = (
    "dark",
    "light",
    "green",
    "blue",
    "amber",
    "rose",
    "teal",
    "violet",
)

DEFAULT_INTERFACE_SETTINGS: dict[str, Any] = {
    "theme": "dark",
    "background_version": 0,
}


def normalize_interface_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULT_INTERFACE_SETTINGS)
    if not isinstance(raw, dict):
        return out
    theme = str(raw.get("theme") or "").strip()
    if theme in THEME_IDS:
        out["theme"] = theme
    try:
        out["background_version"] = max(0, int(raw.get("background_version") or 0))
    except (TypeError, ValueError):
        pass
    return out
