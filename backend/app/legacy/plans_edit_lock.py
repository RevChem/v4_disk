"""Кто сейчас держит редактор планов открытым. Сохранение не блокируется."""
from __future__ import annotations

from app.legacy.json_store_io import atomic_write_text, read_json_dict

import fcntl
import json
import re
import time
from pathlib import Path
from typing import Any

from app.legacy.plans_editor import plans_edits_path

PRESENCE_TTL_SEC = 20
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,80}$")


def token_is_valid(token: str) -> bool:
    return bool(_TOKEN_RE.fullmatch(str(token or "").strip()))


def _paths() -> tuple[Path, Path]:
    folder = plans_edits_path().parent
    return folder / "plans_edit_presence.json", folder / "plans_edit_presence.mutex"


def _with_mutex(fn):
    data_path, mutex_path = _paths()
    mutex_path.parent.mkdir(parents=True, exist_ok=True)
    with mutex_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            return fn(data_path)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _live_sessions(path: Path, now: float | None = None) -> list[dict[str, Any]]:
    raw = _read(path)
    items = raw.get("sessions") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    moment = time.time() if now is None else now
    sessions: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        token = str(item.get("token") or "")
        name = str(item.get("name") or "").strip()
        try:
            user_id = int(item.get("user_id"))
            expires_at = float(item.get("expires_at") or 0)
        except (TypeError, ValueError):
            continue
        if not token_is_valid(token) or expires_at <= moment:
            continue
        sessions.append(
            {
                "user_id": user_id,
                "name": name or f"пользователь #{user_id}",
                "token": token,
                "expires_at": expires_at,
            }
        )
    return sessions


def _write(path: Path, sessions: list[dict[str, Any]]) -> None:
    atomic_write_text(path, json.dumps({"sessions": sessions}, ensure_ascii=False))


def _others(sessions: list[dict[str, Any]], user_id: int) -> dict[str, Any]:
    others: list[dict[str, Any]] = []
    seen: set[int] = set()
    for session in sessions:
        holder_id = int(session["user_id"])
        if holder_id == int(user_id) or holder_id in seen:
            continue
        seen.add(holder_id)
        others.append({"user_id": holder_id, "name": session["name"]})
    return {"others": others}


def heartbeat_plans_presence(user_id: int, holder_name: str, token: str) -> dict[str, Any]:
    def op(path: Path) -> dict[str, Any]:
        now = time.time()
        name = str(holder_name or "").strip() or f"пользователь #{int(user_id)}"
        sessions = [item for item in _live_sessions(path, now) if item["token"] != token]
        sessions.append(
            {
                "user_id": int(user_id),
                "name": name,
                "token": token,
                "expires_at": now + PRESENCE_TTL_SEC,
            }
        )
        _write(path, sessions)
        return _others(sessions, user_id)

    return _with_mutex(op)


def release_plans_presence(user_id: int, token: str) -> dict[str, Any]:
    def op(path: Path) -> dict[str, Any]:
        sessions = [
            item
            for item in _live_sessions(path)
            if not (item["token"] == token and item["user_id"] == int(user_id))
        ]
        if sessions:
            _write(path, sessions)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return _others(sessions, user_id)

    return _with_mutex(op)
