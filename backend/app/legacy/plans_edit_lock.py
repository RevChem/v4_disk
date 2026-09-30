"""Эксклюзивная сессия редактора планов: один открытый редактор на всех пользователей."""
from __future__ import annotations

import fcntl
import json
import re
import time
from pathlib import Path
from typing import Any

from app.legacy.plans_editor import plans_edits_path

LOCK_TTL_SEC = 30
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,80}$")


def token_is_valid(token: str) -> bool:
    return bool(_TOKEN_RE.fullmatch(str(token or "").strip()))


def _paths() -> tuple[Path, Path]:
    folder = plans_edits_path().parent
    return folder / "plans_edit_lock.json", folder / "plans_edit_lock.mutex"


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


def _alive(raw: dict[str, Any] | None, now: float | None = None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        expires_at = float(raw.get("expires_at") or 0)
        user_id = int(raw.get("user_id"))
    except (TypeError, ValueError):
        return None
    token = str(raw.get("token") or "")
    if not token_is_valid(token) or expires_at <= (now if now is not None else time.time()):
        return None
    return {
        "user_id": user_id,
        "holder_name": str(raw.get("holder_name") or "").strip() or f"пользователь #{user_id}",
        "token": token,
        "expires_at": expires_at,
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _clear(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _public(current: dict[str, Any] | None, user_id: int, token: str) -> dict[str, Any]:
    if not current:
        return {
            "locked": False,
            "can_edit": False,
            "held_by_you": False,
            "holder_user_id": None,
            "holder_name": None,
        }
    holder_id = int(current["user_id"])
    return {
        "locked": True,
        "can_edit": current["token"] == token and holder_id == int(user_id),
        "held_by_you": holder_id == int(user_id),
        "holder_user_id": holder_id,
        "holder_name": current["holder_name"],
    }


def acquire_plans_edit_lock(user_id: int, holder_name: str, token: str) -> dict[str, Any]:
    """Забирает свободную блокировку или продлевает свою. Чужая не перехватывается."""

    def op(path: Path) -> dict[str, Any]:
        now = time.time()
        current = _alive(_read(path), now)
        if current and current["token"] != token:
            return _public(current, user_id, token)
        name = str(holder_name or "").strip() or f"пользователь #{int(user_id)}"
        payload = {
            "user_id": int(user_id),
            "holder_name": name,
            "token": token,
            "expires_at": now + LOCK_TTL_SEC,
        }
        _write(path, payload)
        return _public(payload, user_id, token)

    return _with_mutex(op)


def peek_plans_edit_lock(user_id: int, token: str) -> dict[str, Any]:
    def op(path: Path) -> dict[str, Any]:
        return _public(_alive(_read(path)), user_id, token)

    return _with_mutex(op)


def touch_plans_edit_lock(user_id: int, token: str) -> dict[str, Any]:
    """Продлевает TTL, только если эта сессия уже держит живую блокировку."""

    def op(path: Path) -> dict[str, Any]:
        now = time.time()
        current = _alive(_read(path), now)
        if current and current["token"] == token and current["user_id"] == int(user_id):
            current["expires_at"] = now + LOCK_TTL_SEC
            _write(path, current)
        return _public(current, user_id, token)

    return _with_mutex(op)


def release_plans_edit_lock(user_id: int, token: str) -> dict[str, Any]:
    def op(path: Path) -> dict[str, Any]:
        current = _alive(_read(path))
        if current and current["token"] == token and current["user_id"] == int(user_id):
            _clear(path)
            current = None
        return _public(current, user_id, token)

    return _with_mutex(op)
