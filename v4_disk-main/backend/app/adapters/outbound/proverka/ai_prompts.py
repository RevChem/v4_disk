"""Хранение промптов DeepSeek (uploads/proverka/ai_rules.yaml)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.config import settings


def _bundled_rules_path() -> Path:
    return Path(settings.svodki_root) / "plan" / "proverka" / "ai" / "rules.yaml"


def custom_rules_path() -> Path:
    p = Path(settings.upload_dir) / "proverka" / "ai_rules.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def default_prompts() -> dict[str, Any]:
    return _read_yaml(_bundled_rules_path())


def load_prompts() -> dict[str, Any]:
    custom = custom_rules_path()
    if custom.is_file():
        return _read_yaml(custom)
    return default_prompts()


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    rules_in = data.get("rules") or []
    rules: list[dict[str, str]] = []
    if isinstance(rules_in, list):
        for item in rules_in:
            if not isinstance(item, dict):
                continue
            rules.append({
                "id": str(item.get("id") or "").strip(),
                "title": str(item.get("title") or "").strip(),
                "description": str(item.get("description") or "").strip(),
            })
    return {
        "prompt_context": str(data.get("prompt_context") or "").strip(),
        "rules": [r for r in rules if r.get("id") or r.get("title")],
    }


def save_prompts(data: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize(data)
    path = custom_rules_path()
    payload = {
        **normalized,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(
        yaml.dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return prompts_out()


def reset_prompts() -> dict[str, Any]:
    custom_rules_path().unlink(missing_ok=True)
    return prompts_out()


def prompts_out() -> dict[str, Any]:
    custom = custom_rules_path()
    data = load_prompts()
    normalized = _normalize(data)
    updated_at = None
    is_custom = custom.is_file()
    if is_custom:
        updated_at = data.get("updated_at")
        if not updated_at:
            try:
                updated_at = datetime.fromtimestamp(
                    custom.stat().st_mtime, tz=timezone.utc,
                ).isoformat()
            except OSError:
                updated_at = None
    return {
        **normalized,
        "is_custom": is_custom,
        "updated_at": updated_at,
    }
