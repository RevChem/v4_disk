"""История изменений редактора планов (кнопка «История»): записи аудита plans.*."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.application.activity_log import (
    _Users,
    _audit_event,
    _matches,
    iso_utc,
    search_terms,
)
from app.application.audit_activity import PLAN_SECTION_LABELS, change_text
from app.models import AuditLog, User

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# Подписи для действий, не связанных с одним разделом.
_ACTION_LABELS = {
    "plans.value.update": "Значения планов",
    "plans.work_groups.update": "Группы работ",
    "plans.calendar_seasons.update": "Календарные сезоны",
    "plans.season_range.update": "Периоды сезонов",
    "plans.sync_py": "Синхронизация plans.py",
    "plans.import_json": "Загрузка json",
}


def _entry(row: AuditLog, users: _Users) -> dict[str, Any]:
    ev = _audit_event(row, users)
    d = ev["details"]
    section = d.get("section_key")
    return {
        "id": row.id,
        "created_at": iso_utc(row.created_at),
        "user_id": row.user_id,
        "username": ev["username"],
        "action": row.action,
        "kind": _ACTION_LABELS.get(row.action, row.action),
        "section_key": section,
        "section_label": PLAN_SECTION_LABELS.get(str(section)) if section else None,
        "keys": d.get("keys") if isinstance(d.get("keys"), list) else None,
        "old_value": d.get("old_value"),
        "new_value": d.get("new_value"),
        "has_values": "old_value" in d or "new_value" in d,
        "summary_date": d.get("summary_date"),
        "text": change_text(row.action, d),
        # для поиска (в ответ не отдаём)
        "_event": ev,
    }


def build_plans_history(
    db: Session,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    query: str | None = None,
    section: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Страница истории (новые сверху). section — section_key или имя действия (например plans.reset)."""
    size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    terms = search_terms(query)
    section = (section or "").strip()
    users = _Users(db)

    base = db.query(AuditLog).filter(AuditLog.action.like("plans.%"))
    if user_id is not None:
        base = base.filter(AuditLog.user_id == user_id)
    ordered = base.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())

    def keep(item: dict[str, Any]) -> bool:
        if section and section not in (item["section_key"], item["action"]):
            return False
        return not terms or _matches(item["_event"], terms)

    page = max(1, int(page or 1))
    if not terms and not section:
        total = int(base.with_entities(func.count(AuditLog.id)).scalar() or 0)
        pages = max(1, -(-total // size))
        page = min(page, pages)
        rows = ordered.offset((page - 1) * size).limit(size).all()
        items = [_entry(r, users) for r in rows]
    else:
        # Фильтр по тексту/разделу лежит внутри JSON деталей — проходим выборку потоком.
        matched: list[dict[str, Any]] = []
        total = 0
        for row in ordered.yield_per(500):
            item = _entry(row, users)
            if not keep(item):
                continue
            total += 1
            matched.append(item)
        pages = max(1, -(-total // size))
        page = min(page, pages)
        items = matched[(page - 1) * size: page * size]

    for item in items:
        item.pop("_event", None)

    participants = (
        db.query(AuditLog.user_id).filter(AuditLog.action.like("plans.%"), AuditLog.user_id.isnot(None))
        .distinct().all()
    )
    ids = [r[0] for r in participants]
    people = (
        [{"id": u.id, "username": (u.username or u.email or f"#{u.id}")}
         for u in db.query(User).filter(User.id.in_(ids)).order_by(User.username.asc()).all()]
        if ids else []
    )
    sections = [{"value": k, "label": v} for k, v in PLAN_SECTION_LABELS.items()]
    sections += [
        {"value": a, "label": lbl}
        for a, lbl in _ACTION_LABELS.items() if a != "plans.value.update"
    ]
    return {
        "items": items,
        "page": page,
        "pages": pages,
        "page_size": size,
        "total": total,
        "terms": terms,
        "users": people,
        "sections": sections,
    }


DOCUMENT_ACTIONS = (
    "summary.cell_edit.upsert",
    "summary.cell_edit.delete",
    "summary.comment.upsert",
    "summary.comment.delete",
    "summary.comment.future.toggle",
)


def build_document_history(
    db: Session,
    *,
    summary_id: int,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    query: str | None = None,
) -> dict[str, Any]:
    """История правок ячеек и комментариев одного документа (новые сверху)."""
    size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    terms = search_terms(query)
    users = _Users(db)
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.action.in_(DOCUMENT_ACTIONS))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    )
    matched: list[dict[str, Any]] = []
    for row in rows.yield_per(500):
        ev = _audit_event(row, users)
        if ev["details"].get("summary_id") != summary_id:
            continue
        if terms and not _matches(ev, terms):
            continue
        matched.append({
            "id": row.id,
            "created_at": iso_utc(row.created_at),
            "username": ev["username"],
            "action": row.action,
            "text": change_text(row.action, ev["details"]),
        })
    total = len(matched)
    pages = max(1, -(-total // size))
    page = max(1, min(int(page or 1), pages))
    return {
        "items": matched[(page - 1) * size: page * size],
        "page": page, "pages": pages, "page_size": size, "total": total, "terms": terms,
    }
