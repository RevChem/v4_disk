"""Сравнение конфигурации расписания для audit-логов."""
from __future__ import annotations

from typing import Any

from app.application.audit_activity import compact_report_ids


def _entry_label(entry: dict[str, Any]) -> str:
    label = str(entry.get("label") or "").strip()
    if label:
        return label
    return str(entry.get("id") or "задача")


def _entry_time(entry: dict[str, Any]) -> str:
    return f"{int(entry.get('hour', 0)):02d}:{int(entry.get('minute', 0)):02d}"


def _enabled_label(enabled: bool) -> str:
    return "вкл" if enabled else "выкл"


def _diff_fields(before: dict[str, Any], after: dict[str, Any], *, is_mail: bool) -> list[str]:
    label = _entry_label(after)
    parts: list[str] = []

    if before.get("enabled") != after.get("enabled"):
        parts.append(f"{_enabled_label(bool(before.get('enabled')))} → {_enabled_label(bool(after.get('enabled')))}")

    if (before.get("hour"), before.get("minute")) != (after.get("hour"), after.get("minute")):
        parts.append(f"время {_entry_time(before)} → {_entry_time(after)}")

    if before.get("report_ids") != after.get("report_ids"):
        parts.append(
            f"сводки {compact_report_ids(before.get('report_ids') or [])} → "
            f"{compact_report_ids(after.get('report_ids') or [])}"
        )

    if str(before.get("label") or "") != str(after.get("label") or ""):
        parts.append(f"название «{_entry_label(before)}» → «{label}»")

    if is_mail:
        if before.get("emails") != after.get("emails"):
            b_em = ", ".join(before.get("emails") or []) or "—"
            a_em = ", ".join(after.get("emails") or []) or "—"
            parts.append(f"email {b_em} → {a_em}")
        if str(before.get("subject") or "") != str(after.get("subject") or ""):
            b_sub = str(before.get("subject") or "—")
            a_sub = str(after.get("subject") or "—")
            parts.append(f"тема «{b_sub}» → «{a_sub}»")

    return parts


def _summarize_added(entry: dict[str, Any], *, is_mail: bool) -> str:
    label = _entry_label(entry)
    bits = [_entry_time(entry), _enabled_label(bool(entry.get("enabled", True)))]
    bits.append(f"сводки {compact_report_ids(entry.get('report_ids') or [])}")
    if is_mail:
        emails = ", ".join(entry.get("emails") or [])
        if emails:
            bits.append(f"→ {emails}")
    return f"добавлена «{label}» ({', '.join(bits)})"


def _summarize_removed(entry: dict[str, Any]) -> str:
    return f"удалена «{_entry_label(entry)}»"


def diff_schedule_config(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Список изменений для record_activity (schedule_changes)."""
    changes: list[dict[str, Any]] = []

    for section, is_mail in (("generation", False), ("mail", True)):
        before_map = {str(e.get("id")): e for e in (before.get(section) or []) if e.get("id")}
        after_map = {str(e.get("id")): e for e in (after.get(section) or []) if e.get("id")}

        for entry_id, entry in after_map.items():
            if entry_id not in before_map:
                changes.append({
                    "kind": "added",
                    "section": section,
                    "entry_id": entry_id,
                    "label": _entry_label(entry),
                    "summary": _summarize_added(entry, is_mail=is_mail),
                })

        for entry_id, entry in before_map.items():
            if entry_id not in after_map:
                changes.append({
                    "kind": "removed",
                    "section": section,
                    "entry_id": entry_id,
                    "label": _entry_label(entry),
                    "summary": _summarize_removed(entry),
                })

        for entry_id in before_map:
            if entry_id not in after_map:
                continue
            b_entry = before_map[entry_id]
            a_entry = after_map[entry_id]
            field_parts = _diff_fields(b_entry, a_entry, is_mail=is_mail)
            if not field_parts:
                continue
            changes.append({
                "kind": "updated",
                "section": section,
                "entry_id": entry_id,
                "label": _entry_label(a_entry),
                "summary": f"«{_entry_label(a_entry)}»: {'; '.join(field_parts)}",
            })

    return changes
