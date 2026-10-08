"""Нормализация и валидация расписания автогенерации и рассылки."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


DEFAULT_SCHEDULE: dict[str, Any] = {
    "generation": [
        {
            "id": "morning",
            "label": "Утренняя сводка",
            "description": "Ежедневная выгрузка утренних сводок ХК (1–4) перед началом смены.",
            "enabled": True,
            "hour": 6,
            "minute": 30,
            "report_ids": ["1", "2", "3", "4"],
        },
        {
            "id": "report5",
            "label": "Обеденная сводка",
            "description": "Ежедневная выгрузка обеденной сводки по посеву.",
            "enabled": True,
            "hour": 13,
            "minute": 0,
            "report_ids": ["5"],
        },
        {
            "id": "report22-am",
            "label": "Аналитика (утро)",
            "description": "Утренний отчёт аналитики (сводка 22).",
            "enabled": True,
            "hour": 8,
            "minute": 0,
            "report_ids": ["22"],
        },
        {
            "id": "report22-pm",
            "label": "Аналитика (вечер)",
            "description": "Вечерний отчёт аналитики (сводка 22).",
            "enabled": True,
            "hour": 16,
            "minute": 0,
            "report_ids": ["22"],
        },
    ],
    "mail": [],
}


def _new_entry_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _normalize_time(entry: dict[str, Any]) -> tuple[int, int]:
    hour = int(entry.get("hour", 0))
    minute = int(entry.get("minute", 0))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Некорректное время: {hour:02d}:{minute:02d}")
    return hour, minute


def strip_developer_only_report_ids(config: dict[str, Any], hidden: frozenset[str]) -> dict[str, Any]:
    """Убирает сводки роли «Разработчик» из ответа расписания."""
    out = json.loads(json.dumps(config))
    for section in ("generation", "mail"):
        for entry in out.get(section) or []:
            if isinstance(entry, dict):
                entry["report_ids"] = [
                    str(rid) for rid in (entry.get("report_ids") or []) if str(rid) not in hidden
                ]
    return out


def restore_developer_only_report_ids(
    before: dict[str, Any],
    incoming: dict[str, Any],
    hidden: frozenset[str],
) -> dict[str, Any]:
    """Возвращает уже сохранённые скрытые сводки, если правку делает не разработчик."""
    out = json.loads(json.dumps(incoming))
    kept: dict[tuple[str, str], list[str]] = {}
    for section in ("generation", "mail"):
        for entry in before.get(section) or []:
            if not isinstance(entry, dict):
                continue
            kept[(section, str(entry.get("id") or ""))] = [
                str(rid) for rid in (entry.get("report_ids") or []) if str(rid) in hidden
            ]
    for section in ("generation", "mail"):
        for entry in out.get(section) or []:
            if not isinstance(entry, dict):
                continue
            ids = [str(rid) for rid in (entry.get("report_ids") or []) if str(rid) not in hidden]
            for rid in kept.get((section, str(entry.get("id") or "")), []):
                if rid not in ids:
                    ids.append(rid)
            entry["report_ids"] = ids
    return out


def _normalize_report_ids(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",") if x.strip()]
    out: list[str] = []
    for item in raw:
        rid = str(item or "").strip()
        if rid and rid not in out:
            out.append(rid)
    return out


def _normalize_emails(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [x.strip() for x in re.split(r"[,;\s]+", raw) if x.strip()]
    out: list[str] = []
    for item in raw:
        email = str(item or "").strip().lower()
        if not email:
            continue
        if not _EMAIL_RE.match(email):
            raise ValueError(f"Некорректный email: {email}")
        if email not in out:
            out.append(email)
    return out


def _normalize_generation_entry(raw: dict[str, Any], *, fallback_id: str) -> dict[str, Any]:
    hour, minute = _normalize_time(raw)
    report_ids = _normalize_report_ids(raw.get("report_ids"))
    if not report_ids:
        raise ValueError("Укажите хотя бы одну сводку для автогенерации")
    entry_id = str(raw.get("id") or fallback_id).strip() or fallback_id
    label = str(raw.get("label") or "").strip() or None
    description = str(raw.get("description") or "").strip() or None
    return {
        "id": entry_id[:32],
        "label": label,
        "description": description,
        "enabled": bool(raw.get("enabled", True)),
        "hour": hour,
        "minute": minute,
        "report_ids": report_ids,
    }


def _normalize_mail_entry(raw: dict[str, Any], *, fallback_id: str) -> dict[str, Any]:
    hour, minute = _normalize_time(raw)
    report_ids = _normalize_report_ids(raw.get("report_ids"))
    emails = _normalize_emails(raw.get("emails"))
    if not report_ids:
        raise ValueError("Укажите сводку для автоматической отправки")
    if not emails:
        raise ValueError("Укажите хотя бы один email для рассылки")
    entry_id = str(raw.get("id") or fallback_id).strip() or fallback_id
    label = str(raw.get("label") or "").strip() or None
    description = str(raw.get("description") or "").strip() or None
    subject = str(raw.get("subject") or "").strip() or None
    return {
        "id": entry_id[:32],
        "label": label,
        "description": description,
        "enabled": bool(raw.get("enabled", True)),
        "hour": hour,
        "minute": minute,
        "report_ids": report_ids,
        "emails": emails,
        "subject": subject,
    }


def migrate_legacy_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not raw:
        return DEFAULT_SCHEDULE.copy()
    if "generation" in raw or "mail" in raw:
        return raw

    generation: list[dict[str, Any]] = []
    morning = raw.get("morning")
    if isinstance(morning, dict):
        generation.append(
            {
                "id": "morning",
                "label": "Утро",
                "enabled": morning.get("enabled", True),
                "hour": morning.get("hour", 6),
                "minute": morning.get("minute", 30),
                "report_ids": morning.get("report_ids") or [],
            }
        )
    report5 = raw.get("report5")
    if isinstance(report5, dict):
        generation.append(
            {
                "id": "report5",
                "label": "Посев",
                "enabled": report5.get("enabled", True),
                "hour": report5.get("hour", 13),
                "minute": report5.get("minute", 0),
                "report_ids": report5.get("report_ids") or ["5"],
            }
        )
    report22 = raw.get("report22")
    if isinstance(report22, dict):
        slots = report22.get("slots") or [{"hour": 8, "minute": 0}, {"hour": 16, "minute": 0}]
        for idx, slot in enumerate(slots):
            if not isinstance(slot, dict):
                continue
            generation.append(
                {
                    "id": f"report22-{idx}",
                    "label": f"Аналитика ({idx + 1})",
                    "enabled": report22.get("enabled", True),
                    "hour": slot.get("hour", 8),
                    "minute": slot.get("minute", 0),
                    "report_ids": report22.get("report_ids") or ["22"],
                }
            )
    mail = raw.get("mail") if isinstance(raw.get("mail"), list) else []
    return {"generation": generation, "mail": mail}


def normalize_schedule_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    migrated = migrate_legacy_config(raw)
    generation: list[dict[str, Any]] = []
    for idx, item in enumerate(migrated.get("generation") or []):
        if not isinstance(item, dict):
            continue
        generation.append(
            _normalize_generation_entry(item, fallback_id=_new_entry_id("gen"))
        )
    mail: list[dict[str, Any]] = []
    for idx, item in enumerate(migrated.get("mail") or []):
        if not isinstance(item, dict):
            continue
        mail.append(_normalize_mail_entry(item, fallback_id=_new_entry_id("mail")))
    return {"generation": generation, "mail": mail}


def run_state_key(kind: str, entry_id: str, hour: int, minute: int) -> str:
    """Ключ dedup: отдельно для каждого слота времени."""
    base = f"{kind}_{entry_id}_{hour:02d}{minute:02d}"
    return base[:32]


def format_time(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"
