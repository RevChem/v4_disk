"""Ссылка на задание Cropwise строится по machine_id из хранилища, расчёт сводок не трогает."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SVODKI_ROOT", str(ROOT / "svodki"))
sys.path.insert(0, str(ROOT / "backend"))

from app.application import task_links  # noqa: E402


def test_task_page_url_uses_machine_from_store(tmp_path, monkeypatch):
    monkeypatch.setattr(task_links, "_machine_ids_from_api", lambda ids: {})
    db = tmp_path / "store.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, machine_id INTEGER)")
    con.execute("INSERT INTO tasks (id, machine_id) VALUES (395525, 2185), (395526, NULL)")
    con.commit()
    con.close()
    monkeypatch.setattr(task_links, "_store_db", lambda: db)
    urls = task_links.task_page_urls([395525, 395526, "нет", 395525])
    assert urls == {
        "395525": "https://operations.cropwise.com/machines/2185/tasks/395525",
    }


def test_missing_store_returns_no_links(tmp_path, monkeypatch):
    monkeypatch.setattr(task_links, "_store_db", lambda: tmp_path / "missing.sqlite")
    assert task_links.task_page_urls([395525]) == {}
