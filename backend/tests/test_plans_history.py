"""История редактора планов: постраничность, фильтры, поиск."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SVODKI_ROOT", str(ROOT / "svodki"))
sys.path.insert(0, str(ROOT / "backend"))

from app import models  # noqa: E402
from app.application.plans_history import build_plans_history  # noqa: E402
from app.database import Base  # noqa: E402

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    for uid, name in ((1, "ivan"), (2, "anna")):
        session.add(models.User(id=uid, username=name, email=f"{name}@x.ru", password_hash="x", role="admin"))
    session.commit()
    yield session
    session.close()


def _add(db, n, action, uid, **details):
    db.add(models.AuditLog(user_id=uid, action=action, details=json.dumps(details, ensure_ascii=False),
                           created_at=T0 + timedelta(minutes=n)))


def test_history_only_plans_actions_newest_first_with_values(db):
    _add(db, 1, "plans.value.update", 1, section_key="HARDCODED_PLAN", keys=["Сев", "Пшеница"],
         old_value=100, new_value=120)
    _add(db, 2, "auth.logout", 1)
    _add(db, 3, "summary.delete", 1, report_id="1")
    _add(db, 4, "plans.sync_py", 2, note="ok")
    db.commit()
    out = build_plans_history(db)
    assert out["total"] == 2
    assert [i["action"] for i in out["items"]] == ["plans.sync_py", "plans.value.update"]
    upd = out["items"][1]
    assert (upd["old_value"], upd["new_value"], upd["keys"]) == (100, 120, ["Сев", "Пшеница"])
    assert upd["section_label"] == "площадь план, га" and "100 → 120" in upd["text"]
    assert "_event" not in upd and out["items"][0]["username"] == "anna"
    assert {u["username"] for u in out["users"]} == {"ivan", "anna"}


def test_history_paging_filters_and_search(db):
    for i in range(75):
        _add(db, i, "plans.value.update", 1 if i % 3 else 2, section_key="HARDCODED_PLAN",
             keys=["Озимая", str(i)], old_value=i, new_value=i + 1)
    for i in range(5):
        _add(db, 100 + i, "plans.value.update", 1, section_key="AVAILABLE_OWN", keys=["Трактор", str(i)],
             old_value=1, new_value=2)
    db.commit()

    p1, p3 = build_plans_history(db, page=1), build_plans_history(db, page=2, page_size=50)
    assert (p1["total"], p1["pages"], len(p1["items"]), len(p3["items"])) == (80, 2, 50, 30)

    own = build_plans_history(db, section="AVAILABLE_OWN")
    assert own["total"] == 5
    assert build_plans_history(db, user_id=2)["total"] == 25
    assert build_plans_history(db, query="ТРАКТОР")["total"] == 5
    assert build_plans_history(db, query="озимая 74", section="HARDCODED_PLAN")["total"] == 2  # ключ 74 и новое значение 74
    assert build_plans_history(db, query="озимая", page=99)["page"] == 2  # страница ограничивается
    assert build_plans_history(db, query="нет такого")["items"] == []


def test_document_history_filters_by_summary(db):
    from app.application.plans_history import build_document_history

    _add(db, 1, "summary.cell_edit.upsert", 1, summary_id=5, report_id="3", old_text="1", new_text="2")
    _add(db, 2, "summary.cell_edit.upsert", 1, summary_id=6, report_id="3", old_text="9", new_text="8")
    _add(db, 3, "summary.comment.upsert", 2, summary_id=5, report_id="3", comment="Проверить")
    db.commit()
    out = build_document_history(db, summary_id=5)
    assert out["total"] == 2 and out["items"][0]["username"] == "anna"
    assert build_document_history(db, summary_id=5, query="проверить")["total"] == 1
    assert build_document_history(db, summary_id=7)["items"] == []
