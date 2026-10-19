"""Лента «Логи»: постраничная выдача, точные счётчики вкладок, ошибки сводок без дублей."""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SVODKI_ROOT", str(ROOT / "svodki"))
sys.path.insert(0, str(ROOT / "backend"))

from app import models  # noqa: E402
from app.application import activity_log as al  # noqa: E402
from app.database import Base  # noqa: E402

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    al._CACHE.clear()
    yield session
    session.close()
    al._CACHE.clear()


def _user(db, uid: int, name: str, role: str) -> models.User:
    u = models.User(id=uid, username=name, email=f"{name}@x.ru", password_hash="x", role=role)
    db.add(u)
    db.commit()
    return u


def _audit(db, n: int, action: str, user_id: int | None, **details) -> None:
    db.add(models.AuditLog(
        user_id=user_id, action=action, details=json.dumps(details, ensure_ascii=False),
        created_at=T0 + timedelta(minutes=n),
    ))


def _job(db, n: int, user_id, rids, status="completed", result=None, failed=0) -> models.GenerationJob:
    job = models.GenerationJob(
        id=f"job-{n:04d}", user_id=user_id, summary_date=date(2026, 9, 1),
        report_ids=json.dumps(rids), status=status, failed_count=failed,
        result_json=json.dumps(result, ensure_ascii=False) if result is not None else None,
        created_at=T0 + timedelta(minutes=n), updated_at=T0 + timedelta(minutes=n, seconds=30),
    )
    db.add(job)
    return job


@pytest.fixture
def dev(db):
    return _user(db, 1, "dev", "developer")


def test_tab_is_paginated_by_50_and_old_rows_are_reachable(db, dev):
    # 130 правок планов вперемешку с 400 «шумовыми» записями — раньше старые терялись.
    for i in range(130):
        _audit(db, i * 5, "plans.value.update", 1, section_key="HARDCODED_PLAN",
               keys=["k", str(i)], old_value=i, new_value=i + 1)
    for i in range(400):
        _audit(db, i * 5 + 1, "auth.logout", 1)
    db.commit()

    p1 = al.build_activity_page(db, viewer=dev, tab="plans", page=1)
    p2 = al.build_activity_page(db, viewer=dev, tab="plans", page=2)
    p3 = al.build_activity_page(db, viewer=dev, tab="plans", page=3)
    assert (p1["total"], p1["pages"], p1["page_size"]) == (130, 3, 50)
    assert [len(p["items"]) for p in (p1, p2, p3)] == [50, 50, 30]

    ids = [ev["id"] for p in (p1, p2, p3) for ev in p["items"]]
    assert len(ids) == len(set(ids)) == 130
    stamps = [ev["created_at"] for p in (p1, p2, p3) for ev in p["items"]]
    assert stamps == sorted(stamps, reverse=True)
    assert p3["items"][-1]["details"]["keys"] == ["k", "0"]  # самая первая правка достижима

    counts = {t["id"]: t["count"] for t in p1["tabs"]}
    assert counts["plans"] == 130
    assert counts["users"] == 400  # logout → «Пользователи»


def test_page_is_clamped(db, dev):
    for i in range(3):
        _audit(db, i, "plans.reset", 1)
    db.commit()
    page = al.build_activity_page(db, viewer=dev, tab="plans", page=99)
    assert page["page"] == 1 and len(page["items"]) == 3
    empty = al.build_activity_page(db, viewer=dev, tab="pdf", page=5)
    assert empty["total"] == 0 and empty["pages"] == 1 and empty["items"] == []


def test_user_filter_including_system(db, dev):
    _user(db, 2, "anna", "user")
    for i in range(5):
        _audit(db, i, "plans.reset", 1)
    for i in range(3):
        _audit(db, 10 + i, "plans.reset", 2)
    for i in range(2):
        _audit(db, 20 + i, "plans.reset", None)
    db.commit()
    total = lambda f: al.build_activity_page(db, viewer=dev, tab="plans", user_filter=f)["total"]  # noqa: E731
    assert (total(None), total("1"), total("2"), total("system"), total("abc")) == (10, 5, 3, 2, 0)


def test_generation_tab_merges_jobs_and_cancel_requests_and_shows_all_reports_jobs(db, dev):
    ok = {"created": [{"report_id": "1", "title": "A"}, {"report_id": "2", "title": "B"}],
          "failed": [], "logs": []}
    for i in range(60):
        _job(db, i * 2, 1, ["1", "2"], result=ok)
    _job(db, 1000, 1, [], result=ok)  # пустой список = «все сводки»; раньше не показывался
    _audit(db, 1001, "reports.generation.cancel_requested", 1, job_id="job-1000", report_ids=[])
    _audit(db, 1002, "reports.generation.cancelled", 1)  # дубль карточки — скрыт
    _audit(db, 1003, "reports.generation.completed", 1)  # скрыт
    db.commit()

    p1 = al.build_activity_page(db, viewer=dev, tab="generation", page=1)
    assert p1["total"] == 62  # 61 задание + 1 запрос отмены
    assert p1["items"][0]["action"] == "reports.generation.cancel_requested"
    all_job = p1["items"][1]
    assert all_job["job_id"] == "job-1000"
    assert "все сводки" in all_job["message"] and [r["report_id"] for r in all_job["reports"]] == ["1", "2"]
    p2 = al.build_activity_page(db, viewer=dev, tab="generation", page=2)
    assert len(p1["items"]) == 50 and len(p2["items"]) == 12
    assert len({e["id"] for e in p1["items"] + p2["items"]}) == 62


def test_report_errors_are_deduped_between_audit_and_jobs(db, dev):
    failed = {"created": [], "failed": [{"report_id": "7", "title": "Семь", "error": "boom"}], "logs": ["сводка 7 error"]}
    # задание 1: ошибка есть и в аудите, и в задании → одна запись
    _job(db, 1, 1, ["7"], status="completed", result=failed, failed=1)
    _audit(db, 2, "reports.generation.report_failed", 1, job_id="job-0001", report_id="7",
           title="Семь", error="boom", category="report", log_lines=["x"])
    # задание 2: аудита нет (старое) → запись берётся из задания, даже при пустом report_ids
    _job(db, 3, 1, [], status="completed", result=failed, failed=1)
    # изменение сводки — на той же вкладке, но kind=change
    _audit(db, 4, "summary.delete", 1, report_id="7", summary_date="2026-09-01")
    db.commit()

    page = al.build_activity_page(db, viewer=dev, tab="reports")
    assert page["total"] == 3
    assert [(e["kind"], e.get("job_id")) for e in page["items"]] == [
        ("change", None), ("error", "job-0003"), ("error", "job-0001"),
    ]


def test_seasons_tab_and_visibility_for_regular_users(db, dev):
    anna = _user(db, 2, "anna", "user")
    res = {"created": [], "failed": [], "logs": ["SEASON_MISMATCH сводка=2 сезон 2026 vs 2027", "обычная строка"]}
    _job(db, 1, 1, ["2"], result=res)
    _job(db, 2, 2, ["2"], result=res)
    _audit(db, 3, "plans.reset", 1)
    db.commit()

    assert al.build_activity_page(db, viewer=dev, tab="seasons")["total"] == 2
    own = al.build_activity_page(db, viewer=anna, tab="plans")  # чужая вкладка → вкладка по умолчанию
    assert [t["id"] for t in own["tabs"]] == ["reports", "seasons"]
    assert own["tab"] == "reports"
    assert al.build_activity_page(db, viewer=anna, tab="seasons")["total"] == 1  # только свои


def test_unknown_audit_areas_get_their_own_tab(db, dev):
    _audit(db, 1, "interface.settings.update", 1)
    db.commit()
    page = al.build_activity_page(db, viewer=dev, tab="area:Интерфейс")
    assert page["total"] == 1
    assert "area:Интерфейс" in [t["id"] for t in page["tabs"]]


def test_messages_are_human_readable():
    from app.application.audit_activity import format_action_message as fmt

    assert fmt("summary.delete", "U", {"report_id": "5", "summary_date": "2026-09-01"}) == (
        "U удалил сводку 5 за 01.09.2026"
    )
    # report_ids из GenerationJob — JSON-строка; раньше разбиралась по символам
    msg = fmt("reports.generation.cancel_requested", "U", {"report_ids": '["1","2","3"]'})
    assert "1–3" in msg and "[" not in msg
    for action in (
        "plans.sync_py", "interface.updated", "works_summary.generate", "field_season.generate",
        "proverka.cancel_requested", "proverka_machines.requested", "proverka_machines.cancel_requested",
    ):
        assert fmt(action, "U", {}) != f"U: {action}", action


# ── Поиск ────────────────────────────────────────────────────────────────────────────────
def test_search_counts_per_tab_and_pagination(db, dev):
    for i in range(70):
        _audit(db, i, "plans.value.update", 1, section_key="HARDCODED_PLAN",
               keys=["Озимая пшеница", str(i)], old_value=1, new_value=2)
    for i in range(10):
        _audit(db, 100 + i, "summary.cell_edit.upsert", 1, report_id="3", new_text="пшеница")
    _audit(db, 200, "plans.reset", 1)
    db.commit()

    page = al.build_activity_page(db, viewer=dev, tab="plans", query="ПШЕНИЦА")  # регистр не важен
    counts = {t["id"]: t["count"] for t in page["tabs"]}
    assert (page["total"], counts["plans"], counts["cells"]) == (70, 70, 10)
    assert page["terms"] == ["пшеница"] and len(page["items"]) == 50
    page2 = al.build_activity_page(db, viewer=dev, tab="plans", query="пшеница", page=2)
    assert len(page2["items"]) == 20

    # все слова должны встретиться; «ё» = «е»
    both = al.build_activity_page(db, viewer=dev, tab="plans", query="пшеница 69")
    assert both["total"] == 1
    assert al.build_activity_page(db, viewer=dev, tab="plans", query="пшеница zzz")["total"] == 0


def test_search_by_user_date_and_does_not_match_technical_fields(db, dev):
    anna = _user(db, 2, "Анна", "user")
    _audit(db, 1, "plans.reset", 2)
    _audit(db, 2, "plans.reset", 1)
    db.commit()
    by_user = al.build_activity_page(db, viewer=dev, tab="plans", query="анна")
    assert by_user["total"] == 1 and by_user["items"][0]["username"] == "Анна"
    assert al.build_activity_page(db, viewer=dev, tab="plans", query="01.09.2026")["total"] == 2
    # служебные поля (category, kind, id) в поиске не участвуют
    assert al.build_activity_page(db, viewer=dev, tab="plans", query="audit")["total"] == 0
    assert al.build_activity_page(db, viewer=dev, tab="plans", query="category")["total"] == 0
    assert anna.id == 2


def test_search_covers_errors_jobs_seasons_and_respects_visibility(db, dev):
    anna = _user(db, 2, "anna", "user")
    failed = {"created": [], "failed": [{"report_id": "7", "title": "Семь", "error": "Таймаут Cropwise"}],
              "logs": ["SEASON_MISMATCH поле Берёзовое"]}
    _job(db, 1, 1, ["7"], result=failed, failed=1)
    _job(db, 2, 2, ["7"], result=failed, failed=1)
    db.commit()

    reports = al.build_activity_page(db, viewer=dev, tab="reports", query="таймаут")
    assert reports["total"] == 2 and all(e["kind"] == "error" for e in reports["items"])
    assert al.build_activity_page(db, viewer=dev, tab="seasons", query="березовое")["total"] == 2
    gen = al.build_activity_page(db, viewer=dev, tab="generation", query="таймаут")
    assert gen["total"] == 2  # карточки заданий ищутся по тексту ошибки
    own = al.build_activity_page(db, viewer=anna, tab="reports", query="таймаут")
    assert own["total"] == 1  # обычный пользователь видит только своё


def test_search_empty_query_is_ignored_and_results_follow_refresh(db, dev):
    _audit(db, 1, "plans.reset", 1)
    db.commit()
    assert al.build_activity_page(db, viewer=dev, tab="plans", query="   ")["total"] == 1
    assert al.build_activity_page(db, viewer=dev, tab="plans", query="сбросил")["total"] == 1
    _audit(db, 2, "plans.reset", 1)
    db.commit()
    assert al.build_activity_page(db, viewer=dev, tab="plans", query="сбросил", refresh=True)["total"] == 2


def test_job_detail_for_all_reports_job_lists_created_and_failed(db, dev):
    res = {"created": [{"report_id": "1", "title": "A"}],
           "failed": [{"report_id": "2", "title": "B", "error": "boom"}], "logs": ["сводка 2 error"]}
    _job(db, 1, 1, [], result=res, failed=1)
    db.commit()
    detail = al.job_detail(db, "job-0001", viewer=dev)
    assert [(r["report_id"], r["status"]) for r in detail["reports"]] == [("1", "ok"), ("2", "error")]


def test_season_events_have_utc_timestamp(db, dev):
    _job(db, 1, 1, ["2"], result={"created": [], "failed": [], "logs": ["SEASON_MISMATCH x"]})
    db.commit()
    item = al.build_activity_page(db, viewer=dev, tab="seasons")["items"][0]
    assert item["created_at"].endswith("+00:00")
