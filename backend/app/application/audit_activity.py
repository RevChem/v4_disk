"""Человекочитаемые записи аудита для UI «Логи»."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.adapters.outbound.persistence import repositories as repo

# catalog report titles cache filled lazily
_REPORT_TITLES: dict[str, str] | None = None


def _report_titles() -> dict[str, str]:
    global _REPORT_TITLES
    if _REPORT_TITLES is not None:
        return _REPORT_TITLES
    try:
        from app.adapters.outbound.proverka.service import list_catalog

        _REPORT_TITLES = {item["id"]: item["title"] for item in list_catalog()}
    except Exception:
        _REPORT_TITLES = {}
    return _REPORT_TITLES


def report_title(report_id: str) -> str:
    return _report_titles().get(str(report_id), f"сводка {report_id}")


def user_label(user) -> str:
    if user is None:
        return "Система"
    return (user.username or user.email or f"user #{user.id}").strip()


def compact_report_ids(rids: list[Any] | None) -> str:
    if not rids:
        return "—"
    items = [str(x).strip() for x in rids if str(x).strip()]
    if not items:
        return "—"
    nums: list[int] = []
    for item in items:
        try:
            nums.append(int(item))
        except ValueError:
            nums = []
            break
    if len(nums) == len(items) and len(nums) >= 3:
        nums.sort()
        if nums == list(range(nums[0], nums[-1] + 1)):
            return f"{nums[0]}–{nums[-1]}"
    return ", ".join(items)


PLAN_SECTION_LABELS = {
    "HARDCODED_PLAN": "площадь план, га",
    "AVAILABLE_OWN": "своя техника",
    "PLAN_RATE_PER_GROUP": "норма плана по группе",
    "CROP_DISPLAY_RANK": "порядок культур",
    "CROP_FIRM_HIDDEN": "скрытие культуры у фирмы",
}


def _num(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else f"{f:g}".replace(".", ",")


def _quoted(text: Any, limit: int = 80) -> str:
    s = " ".join(str(text or "").split())
    return f"«{s[:limit]}{'…' if len(s) > limit else ''}»"


def _ru_date(iso: Any) -> str:
    parts = str(iso or "").split("-")
    return f"{parts[2]}.{parts[1]}.{parts[0]}" if len(parts) == 3 and len(parts[0]) == 4 else str(iso or "")


def _summary_ref(d: dict[str, Any]) -> str:
    rid = d.get("report_id")
    date_s = _ru_date(d.get("summary_date"))
    parts = []
    if rid:
        parts.append(f"сводке {rid}")
    else:
        parts.append("сводке")
    if date_s:
        parts.append(f"за {date_s}")
    return " ".join(parts)


def _change_text(action: str, d: dict[str, Any]) -> str | None:
    """Текст изменения без имени пользователя (для «Логов» и профиля). None — не наше действие."""
    if action == "plans.value.update":
        label = PLAN_SECTION_LABELS.get(str(d.get("section_key")), str(d.get("section_key") or "план"))
        path = " → ".join(str(k) for k in (d.get("keys") or []))
        where = f"{label}: {path}" if path else label
        return f"изменил в редакторе планов ({where}): {_num(d.get('old_value'))} → {_num(d.get('new_value'))}"
    if action == "plans.reset":
        sec = d.get("section_key")
        label = PLAN_SECTION_LABELS.get(str(sec), str(sec)) if sec else "все разделы"
        return f"сбросил правки редактора планов ({label})"
    if action == "plans.work_groups.update":
        added, removed = d.get("added") or [], d.get("removed") or []
        parts = []
        if added:
            parts.append("добавлено: " + ", ".join(str(x) for x in added))
        if removed:
            parts.append("удалено: " + ", ".join(str(x) for x in removed))
        tail = "; ".join(parts) if parts else "порядок/параметры групп"
        return f"изменил группы работ в редакторе планов ({tail})"
    if action == "plans.calendar_seasons.update":
        return "изменил календарные сезоны в редакторе планов"
    if action == "plans.season_range.update":
        group = d.get("group_name") or "—"
        if d.get("clear"):
            return f"сбросил период сезона для {_quoted(group)}"
        start, end = d.get("period_start") or "…", d.get("period_end") or "…"
        return f"изменил период сезона для {_quoted(group)}: {start} — {end}"
    if action == "pdf_header_labels.update":
        return f"изменил подписи «ВИД РАБОТ» в PDF (строк: {d.get('items_count', '?')})"
    if action == "pdf_header_labels.reset":
        return "сбросил подписи «ВИД РАБОТ» в PDF"
    if action == "pdf_header_labels.strip_numbers":
        return f"убрал номера групп в подписях «ВИД РАБОТ» (строк: {d.get('items_count', '?')})"
    if action == "summary.cell_edit.upsert":
        old, new = d.get("old_text"), d.get("new_text")
        change = f": {_quoted(old)} → {_quoted(new)}" if new is not None else ""
        return f"исправил ячейку в {_summary_ref(d)}{change}"
    if action == "summary.cell_edit.delete":
        return f"удалил правку ячейки в {_summary_ref(d)}"
    if action == "summary.comment.upsert":
        comment = f": {_quoted(d.get('comment'), 120)}" if d.get("comment") else ""
        future = " (и на будущие сводки)" if d.get("apply_to_future") else ""
        return f"оставил комментарий к {_summary_ref(d)}{comment}{future}"
    if action == "summary.comment.future.toggle":
        verb = "включил" if d.get("enabled") else "выключил"
        return f"{verb} комментарий для будущих сводок"
    if action == "summary.comment.delete":
        return f"удалил комментарий к {_summary_ref(d)}"
    if action == "summary.delete":
        return f"удалил сводку {_summary_ref(d)}"
    if action == "report1_page_order.update":
        return (
            f"изменил порядок страниц сводки {d.get('report_id') or '1'} "
            f"(страниц: {d.get('page_count', '?')}, скрыто: {d.get('hidden_count', 0)})"
        )
    if action == "report1_page_order.reset":
        return f"сбросил порядок страниц сводки {d.get('report_id') or '1'}"
    return None


# Действия, которые считаются «изменениями» пользователя (вкладка «Изменения» в профиле).
CHANGE_ACTION_AREAS: dict[str, str] = {
    "plans.value.update": "Редактор планов",
    "plans.reset": "Редактор планов",
    "plans.work_groups.update": "Редактор планов",
    "plans.calendar_seasons.update": "Редактор планов",
    "plans.season_range.update": "Редактор планов",
    "pdf_header_labels.update": "Подписи PDF",
    "pdf_header_labels.reset": "Подписи PDF",
    "pdf_header_labels.strip_numbers": "Подписи PDF",
    "summary.cell_edit.upsert": "Правки ячеек",
    "summary.cell_edit.delete": "Правки ячеек",
    "summary.comment.upsert": "Комментарии",
    "summary.comment.future.toggle": "Комментарии",
    "summary.comment.delete": "Комментарии",
    "summary.delete": "Сводки",
    "report1_page_order.update": "Порядок страниц",
    "report1_page_order.reset": "Порядок страниц",
    "schedule.updated": "Расписание",
}


def change_text(action: str, details: dict[str, Any]) -> str:
    """Текст изменения без имени пользователя."""
    text = _change_text(action, details or {})
    if text is None:
        text = format_action_message(action, "", details or {}).strip()
    return text


def format_action_message(action: str, username: str, details: dict[str, Any]) -> str:
    d = details or {}
    text = _change_text(action, d)
    if text is not None:
        return f"{username} {text}"
    date_s = d.get("summary_date") or d.get("report_date") or ""
    date_part = f" за {date_s}" if date_s else ""

    if action == "auth.login.success":
        return f"{username} вошёл в систему"
    if action == "auth.logout":
        return f"{username} вышел из системы"
    if action == "auth.register":
        return f"{username} зарегистрировался"
    if action == "users.created":
        email = d.get("target_email") or "—"
        role = d.get("target_role") or "user"
        return f"{username} создал пользователя {email} (роль: {role})"
    if action == "users.updated":
        uid = d.get("target_user_id")
        fields = ", ".join(d.get("updated_fields") or [])
        return f"{username} изменил пользователя #{uid}: {fields or '—'}"
    if action == "reports.generation.requested":
        rids = d.get("report_ids") or []
        labels = compact_report_ids(rids)
        return f"{username} запустил генерацию сводок {labels}{date_part}"
    if action == "reports.generation.completed":
        created = d.get("created", 0)
        failed = d.get("failed", 0)
        extra = f", ошибок: {failed}" if failed else ""
        return f"{username} — генерация завершена{date_part}: готово {created}{extra}"
    if action == "reports.generation.failed":
        err = str(d.get("error") or "ошибка")[:300]
        return f"{username} — генерация прервана{date_part}: {err}"
    if action == "reports.generation.report_failed":
        rid = d.get("report_id") or "?"
        title = d.get("title") or report_title(str(rid))
        err = str(d.get("error") or "ошибка")[:400]
        return f"Сводка {rid} ({title}){date_part}: {err}"
    if action == "proverka.requested":
        rids = d.get("report_ids") or []
        labels = compact_report_ids(rids) if rids else "все"
        return f"{username} запустил проверку сводок ({labels}){date_part}"
    if action == "proverka.refresh":
        return f"{username} обновил проверку Cropwise{date_part}"
    if action == "schedule.updated":
        changes = d.get("schedule_changes") or []
        if changes:
            summaries = [str(c.get("summary") or "").strip() for c in changes if c.get("summary")]
            if summaries:
                return f"{username} изменил расписание: {'; '.join(summaries)}"
        gen = d.get("generation_count")
        mail = d.get("mail_count")
        parts = []
        if gen is not None:
            parts.append(f"автогенерация: {gen}")
        if mail is not None:
            parts.append(f"рассылка: {mail}")
        extra = f" ({', '.join(parts)})" if parts else ""
        return f"{username} изменил расписание{extra}"
    if action == "schedule.generation.enqueued":
        rids = d.get("report_ids") or []
        labels = compact_report_ids(rids)
        time_s = d.get("time") or ""
        when = f" в {time_s}" if time_s else ""
        return f"Автогенерация по расписанию{when}: сводки {labels}{date_part}"
    if action == "schedule.mail.sent":
        rids = d.get("report_ids") or []
        labels = compact_report_ids(rids)
        emails = d.get("emails") or []
        to = ", ".join(str(x) for x in emails[:3]) if emails else "—"
        if len(emails) > 3:
            to += f" (+{len(emails) - 3})"
        return f"Рассылка по расписанию: сводки {labels}{date_part} → {to}"
    if action == "schedule.mail.failed":
        err = str(d.get("error") or "ошибка")[:300]
        return f"Рассылка по расписанию не удалась{date_part}: {err}"
    if action == "proverka.ai_prompts.updated":
        return f"{username} изменил промпты AI-разбора"
    if action == "proverka.ai_prompts.reset":
        return f"{username} сбросил промпты AI-разбора"
    if action == "proverka.ai_settings.updated":
        model = d.get("model") or "deepseek"
        return f"{username} изменил настройки AI ({model})"
    if action == "proverka.ai_settings.reset":
        return f"{username} сбросил настройки AI"
    if action == "proverka.evening_report":
        return "Автоматическая вечерняя проверка сводок и кол-ва машин (21:00 MSK) и отправка на почту"
    if action.startswith("schedule."):
        return f"Расписание: {action.replace('schedule.', '')}"
    return f"{username}: {action}"


def action_category(action: str) -> str:
    if action == "reports.generation.report_failed":
        return "report"
    if action.startswith("reports.generation."):
        return "user"
    if action.startswith("auth."):
        return "user"
    if action.startswith("proverka."):
        return "user"
    if action.startswith("schedule."):
        return "user"
    return "user"


def record_activity(
    db: Session,
    *,
    action: str,
    user_id: int | None = None,
    ip: str | None = None,
    **details: Any,
) -> None:
    """write_audit + message/category для UI."""
    user = repo.get_user_by_id(db, user_id) if user_id else None
    username = user_label(user)
    payload = dict(details)
    payload.setdefault("username", username)
    payload.setdefault("category", action_category(action))
    if "message" not in payload:
        payload["message"] = format_action_message(action, username, payload)
    repo.write_audit(
        db,
        action=action,
        user_id=user_id,
        ip=ip,
        details=payload,
    )
