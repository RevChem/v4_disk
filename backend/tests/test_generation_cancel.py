"""Частичная отмена не закрывает всю задачу и не помечает её worker_lost."""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SVODKI_ROOT", str(ROOT / "svodki"))
sys.path.insert(0, str(ROOT / "backend"))

from app.application import generation_cancel as gc  # noqa: E402
from app.legacy import report_generation as rg  # noqa: E402


class _Job:
    def __init__(self, requested: bool, ids: str) -> None:
        self.cancel_requested = requested
        self.cancel_report_ids = ids


class _DB:
    def close(self) -> None:
        return None


def test_stale_job_cancelled_only_on_full_cancel():
    assert (
        gc.stale_running_job_outcome(cancel_all=True, celery_task_id="t", active_task_ids={"t"})
        == "cancelled"
    )
    assert (
        gc.stale_running_job_outcome(cancel_all=False, celery_task_id="t", active_task_ids={"t"})
        is None
    )


def test_stale_job_not_worker_lost_when_celery_inspect_is_empty():
    assert (
        gc.stale_running_job_outcome(cancel_all=False, celery_task_id="t", active_task_ids=None)
        is None
    )
    assert (
        gc.stale_running_job_outcome(cancel_all=False, celery_task_id="t", active_task_ids=set())
        is None
    )
    assert (
        gc.stale_running_job_outcome(cancel_all=False, celery_task_id="t", active_task_ids={"other"})
        == "worker_lost"
    )


def test_partial_cancel_does_not_look_like_whole_job(monkeypatch):
    job = _Job(True, json.dumps(["10"]))
    monkeypatch.setattr(gc.repo, "get_generation_job", lambda db, job_id: job)
    monkeypatch.setattr("app.database.SessionLocal", lambda: _DB())
    check = gc.make_cancel_checker("job", refresh_sec=0)
    assert check(None) is False
    assert check("10") is True
    assert check("11") is False


def test_full_cancel_answers_none_report_id(monkeypatch):
    job = _Job(True, "[]")
    monkeypatch.setattr(gc.repo, "get_generation_job", lambda db, job_id: job)
    monkeypatch.setattr("app.database.SessionLocal", lambda: _DB())
    check = gc.make_cancel_checker("job", refresh_sec=0)
    assert check(None) is True
    assert check("10") is True


def test_partial_kill_touches_only_named_reports(monkeypatch):
    killed: list[object] = []
    monkeypatch.setattr(rg, "_terminate_report_subprocess_tree", lambda proc: killed.append(proc))
    first, second, prefetch = object(), object(), object()
    rg._job_active_subprocesses["job-partial"] = [
        ("10", first),
        ("11", second),
        (None, prefetch),
    ]
    try:
        rg.kill_generation_job_subprocesses("job-partial", report_ids={"10"})
        assert killed == [first]
    finally:
        rg._job_active_subprocesses.pop("job-partial", None)


def test_data_checks_written_atomically(monkeypatch, tmp_path):
    monkeypatch.setattr(rg.settings, "upload_dir", str(tmp_path))
    rg.save_generation_data_checks(date(2026, 10, 7), "10", ["DATA_CHECK [WARN] сводка=10 X"])
    folder = tmp_path / "2026-10-07" / ".data_check" / "sanity"
    files = list(folder.iterdir())
    assert [p.name for p in files] == ["10.json"]
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["report_id"] == "10"
    assert payload["lines"] == ["DATA_CHECK [WARN] сводка=10 X"]


class _PendingJob:
    def __init__(self) -> None:
        self.status = "pending"
        self.cancel_requested = False
        self.cancel_report_ids = "[]"


def test_dead_db_connection_marks_job_failed_instead_of_pending(monkeypatch):
    from app.adapters.inbound.celery import tasks as tasks_mod

    job = _PendingJob()
    saved: dict = {}

    def update(db, obj, **fields):
        saved.update(fields)
        for key, value in fields.items():
            setattr(obj, key, value)
        return obj

    monkeypatch.setattr("app.database.engine", type("E", (), {"dispose": staticmethod(lambda: None)})())
    monkeypatch.setattr("app.database.SessionLocal", lambda: _DB())
    monkeypatch.setattr(tasks_mod.repo, "get_generation_job", lambda db, job_id: job)
    monkeypatch.setattr(tasks_mod.repo, "update_generation_job", update)

    status = tasks_mod._mark_generation_job_crashed("job", RuntimeError("closed"))
    assert status == "failed"
    assert saved["status"] == "failed"
    assert "RuntimeError" in saved["error_message"]


def test_generation_task_survives_system_exit(monkeypatch):
    from app.adapters.inbound.celery import tasks as tasks_mod

    def boom():
        raise SystemExit(1)

    monkeypatch.setattr(tasks_mod, "SessionLocal", boom)
    monkeypatch.setattr(tasks_mod, "_mark_generation_job_crashed", lambda job_id, exc: "cancelled")
    result = tasks_mod.run_generation_job_task("job-1")
    assert result == {"job_id": "job-1", "status": "cancelled", "error": "SystemExit"}
