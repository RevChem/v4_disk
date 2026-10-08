"""Расписание: окно догона вместо точного совпадения минуты, повтор рассылки при сбое."""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.adapters.inbound.celery import tasks
from app.config import settings

TZ = ZoneInfo("Europe/Moscow")


def at(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 10, 8, h, m, s, tzinfo=TZ)


ENTRY = {"id": "e1", "hour": 6, "minute": 30}


# ── чистая логика окна ────────────────────────────────────────────────────────────────────
def test_exact_minute_runs():
    assert tasks._should_run_now(ENTRY, at(6, 30, 0), 15)
    assert tasks._should_run_now(ENTRY, at(6, 30, 59), 15)


def test_before_slot_does_not_run():
    assert not tasks._should_run_now(ENTRY, at(6, 29, 59), 15)


def test_missed_tick_is_caught_up():
    assert tasks._should_run_now(ENTRY, at(6, 31, 5), 15)
    assert tasks._should_run_now(ENTRY, at(6, 44, 59), 15)


def test_window_expires():
    assert not tasks._should_run_now(ENTRY, at(6, 45, 0), 15)


def test_window_of_one_minute_is_legacy_behaviour():
    assert tasks._should_run_now(ENTRY, at(6, 30, 30), 1)
    assert not tasks._should_run_now(ENTRY, at(6, 31, 0), 1)


def test_window_does_not_wrap_past_midnight():
    late = {"id": "late", "hour": 23, "minute": 55}
    next_day = datetime(2026, 10, 9, 0, 5, tzinfo=TZ)
    assert not tasks._should_run_now(late, next_day, 15)


def test_last_attempt_flag():
    assert not tasks._is_last_attempt(ENTRY, at(6, 30), 15)
    assert not tasks._is_last_attempt(ENTRY, at(6, 43, 59), 15)
    assert tasks._is_last_attempt(ENTRY, at(6, 44), 15)
    assert tasks._is_last_attempt(ENTRY, at(6, 30), 1)


# ── check_schedules на подмене БД ─────────────────────────────────────────────────────────
@pytest.fixture
def sched(monkeypatch):
    st = SimpleNamespace(
        now=at(6, 30, 10),
        ran=set(),
        enqueued=[],
        activities=[],
        mail_calls=0,
        mail_result={"ok": True, "sent": ["a@b.c"]},
        fail_enqueue_for=set(),
        config={
            "generation": [
                {"id": "g1", "enabled": True, "hour": 6, "minute": 30, "report_ids": ["1"]},
                {"id": "g2", "enabled": True, "hour": 6, "minute": 30, "report_ids": ["2"]},
            ],
            "mail": [],
        },
    )

    class FakeDB:
        def rollback(self):
            pass

        def close(self):
            pass

    def fake_enqueue(db, *, report_ids, **kw):
        if report_ids[0] in st.fail_enqueue_for:
            raise RuntimeError("boom")
        st.enqueued.append(report_ids[0])
        return SimpleNamespace(id=len(st.enqueued)), None

    def fake_mail(**kw):
        st.mail_calls += 1
        return st.mail_result

    monkeypatch.setattr(settings, "schedule_catchup_minutes", 15)
    monkeypatch.setattr(tasks, "SessionLocal", lambda: FakeDB())
    monkeypatch.setattr(tasks, "_now_msk", lambda: st.now)
    monkeypatch.setattr(tasks.repo, "get_schedule_config", lambda db: st.config)
    monkeypatch.setattr(
        tasks.repo,
        "get_schedule_run_state",
        lambda db, key: SimpleNamespace(last_enqueued_on=st.now.date()) if key in st.ran else None,
    )
    monkeypatch.setattr(tasks.repo, "mark_schedule_enqueued", lambda db, key, d: st.ran.add(key))
    monkeypatch.setattr(tasks, "record_activity", lambda db, **kw: st.activities.append(kw["action"]))
    monkeypatch.setattr(tasks, "enqueue_or_run", fake_enqueue)
    monkeypatch.setattr(tasks, "send_scheduled_reports", fake_mail)
    return st


def test_generation_runs_late_and_only_once(sched):
    sched.now = at(6, 32, 4)  # тик на минуту «опоздал»
    tasks.check_schedules()
    assert sched.enqueued == ["1", "2"]
    sched.now = at(6, 33, 4)
    tasks.check_schedules()
    assert sched.enqueued == ["1", "2"]  # дубликатов нет


def test_generation_not_started_after_window(sched):
    sched.now = at(7, 0, 0)
    tasks.check_schedules()
    assert sched.enqueued == []


def test_one_failing_entry_does_not_block_others_and_is_retried(sched):
    sched.fail_enqueue_for = {"1"}
    tasks.check_schedules()
    assert sched.enqueued == ["2"]  # g2 отработал, несмотря на сбой g1

    sched.fail_enqueue_for = set()
    sched.now = at(6, 31, 10)
    tasks.check_schedules()
    assert sched.enqueued == ["2", "1"]  # g1 повторён на следующем тике, g2 — не дублируется


def _mail_config(sched):
    sched.config = {
        "generation": [],
        "mail": [
            {
                "id": "m1",
                "enabled": True,
                "hour": 6,
                "minute": 30,
                "report_ids": ["1"],
                "emails": ["a@b.c"],
            }
        ],
    }


def test_failed_mail_is_retried_and_succeeds(sched):
    _mail_config(sched)
    sched.mail_result = {"ok": False, "error": "PDF не найдены: 1"}
    tasks.check_schedules()
    assert sched.mail_calls == 1 and sched.ran == set()
    assert "schedule.mail.failed" not in sched.activities  # промежуточный сбой в журнал не пишем

    sched.now = at(6, 35)
    sched.mail_result = {"ok": True, "sent": ["a@b.c"]}
    tasks.check_schedules()
    assert sched.mail_calls == 2 and len(sched.ran) == 1
    assert sched.activities == ["schedule.mail.sent"]

    sched.now = at(6, 36)
    tasks.check_schedules()
    assert sched.mail_calls == 2  # после успеха больше не отправляем


def test_failed_mail_logged_once_on_last_attempt(sched):
    _mail_config(sched)
    sched.mail_result = {"ok": False, "error": "SMTP"}
    for minute in range(30, 50):  # 06:30 … 06:49, окно 15 минут
        sched.now = at(6, minute, 5)
        tasks.check_schedules()
    assert sched.mail_calls == 15  # 06:30 … 06:44
    assert sched.activities == ["schedule.mail.failed"]
    assert sched.ran == set()
