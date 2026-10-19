"""Ссылка на задание Cropwise: та же страница, что в аналитике и расходе топлива."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx

from app.config import _upload_dir_path, get_cropwise_token, settings

TASK_PAGE = "https://operations.cropwise.com/machines/{machine_id}/tasks/{task_id}"
_MAX_IDS = 80


def _store_db() -> Path | None:
    raw = (settings.svodki_task_store_dir or "").strip()
    folder = Path(raw) if raw else _upload_dir_path() / ".task_store"
    path = folder / "store.sqlite"
    return path if path.is_file() else None


def task_page_urls(task_ids: list[int]) -> dict[str, str]:
    """id задания → страница Cropwise. Нет машины в хранилище — ссылки нет."""
    ids = []
    seen: set[int] = set()
    for raw in task_ids:
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            continue
        if tid <= 0 or tid in seen:
            continue
        seen.add(tid)
        ids.append(tid)
        if len(ids) >= _MAX_IDS:
            break
    path = _store_db()
    if not ids or path is None:
        return {}
    marks = ",".join("?" * len(ids))
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return {}
    try:
        rows = con.execute(
            f"SELECT id, machine_id FROM tasks WHERE id IN ({marks}) AND machine_id IS NOT NULL",
            ids,
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    found = {int(tid): int(mid) for tid, mid in rows if mid}
    missing = [tid for tid in ids if tid not in found]
    for tid, mid in _machine_ids_from_api(missing).items():
        found.setdefault(tid, mid)
    return {
        str(tid): TASK_PAGE.format(machine_id=mid, task_id=tid)
        for tid, mid in found.items()
    }


def _machine_ids_from_api(task_ids: list[int]) -> dict[int, int]:
    """Если задания нет в хранилище — один запрос Cropwise на номер. Не больше 15."""
    token = (get_cropwise_token() or "").strip()
    if not token or not task_ids:
        return {}
    base = (settings.cropwise_base_url or "https://operations.cropwise.com/api/v3").rstrip("/")
    out: dict[int, int] = {}
    try:
        with httpx.Client(headers={"X-User-Api-Token": token}, timeout=8) as client:
            for tid in task_ids[:15]:
                try:
                    res = client.get(f"{base}/machine_tasks/{tid}")
                except httpx.HTTPError:
                    continue
                if res.status_code != 200:
                    continue
                payload = res.json() or {}
                if isinstance(payload.get("data"), dict):
                    payload = payload["data"]
                mid = payload.get("machine_id") if isinstance(payload, dict) else None
                if mid:
                    out[tid] = int(mid)
    except (httpx.HTTPError, TypeError, ValueError):
        return out
    return out
