"""Регрессия: прогресс/длительности из параллельных потоков и атомарное сохранение артефактов."""
from __future__ import annotations

import sys
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest


@pytest.fixture()
def rr(tmp_path, monkeypatch):
    # report_generation тянет тяжёлые зависимости; для логики состояния он не нужен
    if "app.legacy.report_generation" not in sys.modules:
        stub = types.ModuleType("app.legacy.report_generation")
        stub.ReportGenerationCancelled = type("C", (Exception,), {})
        stub.ReportGenerationError = type("E", (Exception,), {})
        stub.__getattr__ = lambda name: (lambda *a, **k: None)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "app.legacy.report_generation", stub)
    from app.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    sys.modules.pop("app.legacy.report_runner", None)
    import app.legacy.report_runner as module

    return module


def test_progress_and_durations_survive_parallel_workers(rr):
    d = date(2026, 10, 6)
    ids = [str(i) for i in range(3, 15)]
    for run in range(30):
        jid = f"job{run}"
        rr.init_job_progress(jid)

        def work(rid, jid=jid):
            rr.record_job_report_completed(jid, rid)
            rr.record_report_duration(d, rid, 1.5)

        with ThreadPoolExecutor(3) as ex:
            list(ex.map(work, ids))
        assert set(rr.list_job_completed_report_ids(jid)) == set(ids)
    assert set(rr.load_report_durations(d)) >= set(ids)


def test_save_artifact_is_atomic_and_purges_only_same_report(rr, tmp_path):
    d = date(2026, 10, 6)
    folder = tmp_path / d.isoformat()
    folder.mkdir()
    (folder / "5.Посев 2026 старый.pdf").write_bytes(b"OLD")
    (folder / "50.Другая.pdf").write_bytes(b"KEEP")
    src = tmp_path / "src.pdf"
    src.write_bytes(b"NEW")

    dest = rr._save_artifact(src, d, "5.Посев 2026", ".pdf", report_id="5")
    rr._save_artifact(src, d, "5.Посев 2026", ".pdf", report_id="5")  # повтор того же имени

    assert dest.read_bytes() == b"NEW"
    names = {p.name for p in folder.iterdir()}
    assert "5.Посев 2026 старый.pdf" not in names
    assert "50.Другая.pdf" in names
    assert not any(n.endswith(".part") for n in names)
