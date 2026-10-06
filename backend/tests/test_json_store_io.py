"""Регрессия: JSON-хранилища не теряют правки (атомарная запись, lock RMW, бэкап битого файла)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.config import settings
from app.legacy.json_store_io import atomic_write_json, read_json_dict, serialized_rmw


@pytest.fixture(autouse=True)
def _upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    return tmp_path


def test_rmw_lock_does_not_lose_updates(tmp_path):
    path = tmp_path / "store.json"
    atomic_write_json(path, {"n": 0})

    @serialized_rmw
    def inc():
        data = read_json_dict(path)
        data["n"] += 1
        atomic_write_json(path, data)

    with ThreadPoolExecutor(16) as ex:
        list(ex.map(lambda _: inc(), range(200)))
    assert read_json_dict(path)["n"] == 200


def test_concurrent_atomic_writes_never_corrupt(tmp_path):
    path = tmp_path / "w.json"
    with ThreadPoolExecutor(16) as ex:
        list(ex.map(lambda i: atomic_write_json(path, {"i": i, "pad": "x" * 5000}), range(200)))
    assert "i" in read_json_dict(path)
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_file_is_backed_up_not_silently_overwritten(tmp_path):
    path = tmp_path / "plans.json"
    path.write_text('{"a": 1, "broken": ', encoding="utf-8")
    assert read_json_dict(path) == {}
    backups = list(tmp_path.glob("plans.json.corrupt-*"))
    assert len(backups) == 1 and "broken" in backups[0].read_text(encoding="utf-8")


def test_non_dict_json_is_backed_up(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_json_dict(path) == {}
    backups = list(tmp_path.glob("list.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "[1, 2, 3]"
    assert not path.exists()


def test_os_error_on_read_does_not_quarantine_healthy_file(tmp_path, monkeypatch):
    from pathlib import Path

    from app.legacy import json_store_io as jio

    path = tmp_path / "ok.json"
    path.write_text('{"keep": 1}', encoding="utf-8")
    monkeypatch.setattr(jio, "_READ_RETRY_DELAY_SEC", 0)

    real = Path.read_text

    def boom(self, *a, **k):
        if self == path:
            raise PermissionError("denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(jio.JsonStoreReadError):
        read_json_dict(path)
    monkeypatch.setattr(Path, "read_text", real)
    # файл на месте, не переименован и не затёрт
    assert path.exists() and not list(tmp_path.glob("ok.json.corrupt-*"))
    assert read_json_dict(path) == {"keep": 1}


def test_transient_os_error_is_retried(tmp_path, monkeypatch):
    from pathlib import Path

    from app.legacy import json_store_io as jio

    path = tmp_path / "flaky.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    monkeypatch.setattr(jio, "_READ_RETRY_DELAY_SEC", 0)
    real, calls = Path.read_text, {"n": 0}

    def flaky(self, *a, **k):
        if self == path and calls["n"] < 2:
            calls["n"] += 1
            raise OSError("temporary")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert read_json_dict(path) == {"a": 1}


def test_locks_are_per_store_and_reentrant(tmp_path):
    from app.legacy.json_store_io import store_rmw_lock

    with store_rmw_lock("a"):
        with store_rmw_lock("a"):  # реентерабельность
            with store_rmw_lock("b"):  # независимое хранилище
                pass
    assert (tmp_path / ".json_store.a.lock").exists() or not hasattr(__import__("fcntl"), "flock")
    assert (tmp_path / ".json_store.b.lock").exists() or not hasattr(__import__("fcntl"), "flock")


def test_serialized_rmw_named_decorator(tmp_path):
    path = tmp_path / "n.json"
    atomic_write_json(path, {"n": 0})

    @serialized_rmw(name="named")
    def inc():
        d = read_json_dict(path)
        d["n"] += 1
        atomic_write_json(path, d)

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda _: inc(), range(100)))
    assert read_json_dict(path)["n"] == 100
