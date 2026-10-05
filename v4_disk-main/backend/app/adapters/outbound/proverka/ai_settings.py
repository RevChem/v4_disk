"""Настройки DeepSeek для AI-разбора (uploads/proverka/ai_settings.yaml)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.config import settings

ALLOWED_MODELS = (
    "deepseek-flash",
    "deepseek-chat",
    "deepseek-reasoner",
)


def settings_path() -> Path:
    p = Path(settings.upload_dir) / "proverka" / "ai_settings.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_file() -> dict[str, Any]:
    path = settings_path()
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _mask_key(key: str) -> str:
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 8:
        return "••••"
    return f"{k[:4]}…{k[-4:]}"


def load_ai_runtime_settings() -> dict[str, str]:
    """Для analyst: переопределения model/api_key поверх .env."""
    import os

    data = _read_file()
    model = str(data.get("model") or os.environ.get("PROVERKA_AI_MODEL") or "deepseek-flash").strip()
    api_key = str(data.get("api_key") or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    base_url = str(data.get("base_url") or os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").strip()
    if model not in ALLOWED_MODELS:
        model = "deepseek-flash"
    return {"model": model, "api_key": api_key, "base_url": base_url}


def settings_out() -> dict[str, Any]:
    import os

    data = _read_file()
    custom_key = str(data.get("api_key") or "").strip()
    env_key = str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    model = str(data.get("model") or os.environ.get("PROVERKA_AI_MODEL") or "deepseek-flash").strip()
    if model not in ALLOWED_MODELS:
        model = "deepseek-flash"
    effective_key = custom_key or env_key
    return {
        "model": model,
        "models": list(ALLOWED_MODELS),
        "api_key_set": bool(effective_key),
        "api_key_masked": _mask_key(effective_key),
        "api_key_source": "custom" if custom_key else ("env" if env_key else "none"),
        "is_custom": bool(data),
        "updated_at": data.get("updated_at"),
    }


def save_settings(*, model: str, api_key: str | None = None) -> dict[str, Any]:
    model = str(model or "deepseek-flash").strip()
    if model not in ALLOWED_MODELS:
        raise ValueError(f"Недопустимая модель: {model}")
    prev = _read_file()
    payload: dict[str, Any] = {"model": model, "updated_at": datetime.now(timezone.utc).isoformat()}
    if api_key is not None:
        key = str(api_key or "").strip()
        if key:
            payload["api_key"] = key
        else:
            payload.pop("api_key", None)
    elif prev.get("api_key"):
        payload["api_key"] = prev["api_key"]
    settings_path().write_text(
        yaml.dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return settings_out()


def reset_settings() -> dict[str, Any]:
    settings_path().unlink(missing_ok=True)
    return settings_out()
