"""POST /summaries/{id}/cell-edits/batch: черновик правок сохраняется одним запросом."""
from __future__ import annotations

import copy
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SVODKI_ROOT", str(ROOT / "svodki"))
sys.path.insert(0, str(ROOT / "backend"))

from app import models  # noqa: E402
from app.adapters.inbound.api import deps  # noqa: E402
from app.adapters.inbound.api.routers import summaries as route  # noqa: E402
from app.database import Base, get_db  # noqa: E402


def _edit(key: str, old: str, new: str, top: float = 0.1, left: float = 0.1) -> dict:
    return {
        "page": 1,
        "left": left,
        "top": top,
        "width": 0.08,
        "height": 0.012,
        "cell_key": key,
        "cell_text": old,
        "replacement": new,
        "fill_color": "#ffffff",
        "text_color": "",
    }


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    session = Session()
    user = models.User(id=1, username="admin", email="a@x.ru", password_hash="x", role="admin")
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    xlsx = tmp_path / "s.xlsx"
    xlsx.write_bytes(b"x")
    session.add_all(
        [
            user,
            models.Summary(id=7, title="Сводка", summary_date=date(2026, 10, 15), file_path=str(pdf), visible=True),
            models.Summary(id=8, title="Excel", summary_date=date(2026, 10, 15), file_path=str(xlsx), visible=True),
        ]
    )
    session.commit()

    disk: dict = {}
    rebuilds: list[int] = []

    # «Диск»: чтение отдаёт копию, запись заменяет — как реальный JSON-файл.
    monkeypatch.setattr(route, "load_cell_edits_store", lambda: copy.deepcopy(disk))
    monkeypatch.setattr(route, "save_cell_edits_store", lambda store: (disk.clear(), disk.update(copy.deepcopy(store))))
    monkeypatch.setattr(route, "rebuild_summary_pdf_layers", lambda item: rebuilds.append(item.id))

    app = FastAPI()
    app.include_router(route.router)

    def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[deps.require_cell_edit_access] = lambda: user
    client = TestClient(app)
    yield client, disk, rebuilds, session
    session.close()


def _post(client, sid, edits=(), delete_ids=()):
    return client.post(f"/summaries/{sid}/cell-edits/batch", json={"edits": list(edits), "delete_ids": list(delete_ids)})


def test_batch_saves_all_edits_with_single_rebuild(env):
    client, disk, rebuilds, _ = env
    resp = _post(client, 7, [_edit("1:a", "10", "11", top=0.1), _edit("1:b", "20", "22", top=0.2), _edit("1:c", "30", "33", top=0.3)])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(e["replacement"] for e in body["edits"]) == ["11", "22", "33"]
    assert len(disk["7"]["edits"]) == 3
    assert rebuilds == [7]  # одна пересборка PDF на весь пакет


def test_batch_can_update_and_delete_in_one_request(env):
    client, disk, rebuilds, _ = env
    first = _post(client, 7, [_edit("1:a", "10", "11", top=0.1), _edit("1:b", "20", "22", top=0.2)]).json()
    ids = {e["cell_key"]: e["id"] for e in first["edits"]}
    rebuilds.clear()
    resp = _post(client, 7, [_edit("1:a", "10", "99", top=0.1)], delete_ids=[ids["1:b"]])
    assert resp.status_code == 200, resp.text
    edits = resp.json()["edits"]
    assert [(e["cell_key"], e["replacement"]) for e in edits] == [("1:a", "99")]
    assert edits[0]["id"] == ids["1:a"]  # правка обновилась, а не задвоилась
    assert rebuilds == [7]


def test_batch_is_atomic_when_one_edit_is_invalid(env):
    client, disk, rebuilds, _ = env
    _post(client, 7, [_edit("1:a", "10", "11")])
    before = copy.deepcopy(disk)
    rebuilds.clear()
    # Пустая замена после trim: схема пропускает «   », а хранилище отвергает.
    resp = _post(client, 7, [_edit("1:b", "20", "22", top=0.2), _edit("1:c", "30", "   ", top=0.3)])
    assert resp.status_code == 400
    assert "Правка №2" in resp.json()["detail"]
    assert disk == before  # первая правка пакета не попала на диск
    assert rebuilds == []


def test_batch_ignores_unknown_delete_ids_and_requires_changes(env):
    client, _, rebuilds, _ = env
    assert _post(client, 7).status_code == 400
    ok = _post(client, 7, [_edit("1:a", "10", "11")], delete_ids=["nope"])
    assert ok.status_code == 200
    assert len(ok.json()["edits"]) == 1


def test_batch_rejects_non_pdf_and_missing_summary(env):
    client, _, rebuilds, _ = env
    assert _post(client, 8, [_edit("1:a", "1", "2")]).status_code == 400
    assert _post(client, 999, [_edit("1:a", "1", "2")]).status_code == 404
    assert rebuilds == []


def test_batch_reedit_with_new_cell_key_updates_same_row(env):
    """После замены в PDF у ячейки другой ключ. Пакет должен обновить ту же правку, а не добавить вторую."""
    client, _, _, session = env
    first = _post(client, 7, [_edit("1:0:0:10", "10", "11", top=0.1, left=0.1)]).json()
    old_id = first["edits"][0]["id"]
    resp = _post(client, 7, [_edit("1:0:0:11", "10", "99", top=0.1, left=0.1)])
    assert resp.status_code == 200, resp.text
    edits = resp.json()["edits"]
    assert len(edits) == 1
    assert edits[0]["id"] == old_id
    assert edits[0]["cell_key"] == "1:0:0:10"
    assert edits[0]["cell_text"] == "10"
    assert edits[0]["replacement"] == "99"
    last = json.loads(session.query(models.AuditLog).order_by(models.AuditLog.id.desc()).first().details)
    assert last["edit_id"] == old_id
    assert (last["old_text"], last["new_text"]) == ("10", "99")


def test_batch_writes_one_audit_record_per_change(env):
    client, _, _, session = env
    first = _post(client, 7, [_edit("1:a", "10", "11", top=0.1), _edit("1:b", "20", "22", top=0.2)]).json()
    victim = first["edits"][0]["id"]
    _post(client, 7, [_edit("1:c", "30", "33", top=0.3)], delete_ids=[victim])
    rows = session.query(models.AuditLog).order_by(models.AuditLog.id).all()
    actions = [r.action for r in rows]
    assert actions.count("summary.cell_edit.upsert") == 3
    assert actions.count("summary.cell_edit.delete") == 1
    upserts = [json.loads(r.details) for r in rows if r.action == "summary.cell_edit.upsert"]
    assert {(d["old_text"], d["new_text"]) for d in upserts} == {("10", "11"), ("20", "22"), ("30", "33")}
