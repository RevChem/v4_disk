"""Подхват .env проекта в os.environ. Уже заданные переменные не перезаписываются."""
from __future__ import annotations

import os
from pathlib import Path


def _apply_env_file(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def load_project_env() -> None:
    candidates: list[Path] = [Path("/app/.env")]
    seen: set[str] = set()
    starts = [Path.cwd()]
    try:
        starts.append(Path(__file__).resolve().parent)
    except OSError:
        pass
    for start in starts:
        for folder in (start, *start.parents):
            candidates.append(folder / ".env")
            if (folder / "docker-compose.yml").is_file():
                break
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            _apply_env_file(path)
            return
