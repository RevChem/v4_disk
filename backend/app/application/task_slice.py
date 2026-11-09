"""Срез заданий всех операций и вопрос по нему.

Фильтры те же, что у кнопки Namolot: пустые поля не отсекают записи.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import date
from typing import Any

from app.application.namolot_view import filter_record_rows

_TTL_SEC = 15 * 60
_lock = threading.Lock()
_cache: tuple[str, float, dict[str, Any]] | None = None

ASK_SYSTEM = (
    "Ты помощник агронома. Отвечай только по таблице заданий Cropwise. "
    "Каждая строка — одно задание на поле: дата закрытия, агрофирма, культура, операция "
    "(внесение удобрений, посев, опрыскивание, уборка и любые другие), госномер, механизатор, поле, га. "
    "Не выдумывай числа, операции и фамилии. Если таблица усечена, суммы бери из строк «Итого» и «По …» — "
    "они посчитаны по всем строкам среза. Если данных недостаточно — так и скажи. Отвечай кратко, по-русски."
)


def _store_dir():
    from app.adapters.outbound.proverka.driver_work_service import _store_dir as store_dir

    return store_dir()


def _apply_names(rows: list[dict[str, Any]]) -> str:
    from app.adapters.outbound.proverka.driver_work_service import _resolve_people

    ids = [row["driver_id"] for row in rows if row.get("driver_id")]
    names, note = _resolve_people(ids)
    for row in rows:
        driver_id = row.get("driver_id")
        name = names.get(driver_id) if driver_id else ""
        if name:
            row["operator"] = name
            row["surname"] = name.split()[0]
        elif driver_id:
            row["operator"] = f"id {driver_id}"
            row["surname"] = ""
    return note


def work_type_label(work_type_id: int, title: str) -> str:
    """Как чип в «Группах работ»: номер справочника и его название."""
    return f"{work_type_id}: {title}"


def name_unmapped_operations(rows: list[dict[str, Any]], catalog: dict[int, str]) -> None:
    """Подпись операции берётся из справочника work_types, а не «вид работ 147»."""
    for row in rows:
        work_type_id = row.get("work_type_id")
        if not isinstance(work_type_id, int):
            continue
        title = str(catalog.get(work_type_id) or "").strip()
        if title and not title.startswith("work_type_"):
            row["operation"] = work_type_label(work_type_id, title)


def _apply_work_titles(rows: list[dict[str, Any]]) -> None:
    if not any(isinstance(row.get("work_type_id"), int) for row in rows):
        return
    from app.legacy.plans_editor import WorkTypesUnavailableError, fetch_work_types_catalog

    try:
        catalog = {int(item["id"]): str(item.get("name") or "") for item in fetch_work_types_catalog()}
    except WorkTypesUnavailableError:
        return
    name_unmapped_operations(rows, catalog)


def build_slice(report_date: date) -> dict[str, Any]:
    """Все операции с 1 января до 06:00 даты сводки. Повтор в пределах 15 минут берёт память."""
    global _cache
    key = report_date.isoformat() + ":crew-track"
    now = time.monotonic()
    with _lock:
        cached = _cache
        if cached and cached[0] == key and now - cached[1] < _TTL_SEC:
            return cached[2]

    from app.adapters.outbound.proverka.service import _ensure_plan_path

    _ensure_plan_path()
    import task_questions

    payload = task_questions.load_rows(_store_dir(), report_date)
    if payload.get("available"):
        rows = payload.get("rows") or []
        _apply_work_titles(rows)
        payload["names_note"] = _fill_names(rows)
    else:
        payload["names_note"] = ""
    # Без ФИО фамилия в фильтре бесполезна: не запоминаем сбой справочника.
    if payload.get("names_note"):
        return payload
    with _lock:
        _cache = (key, time.monotonic(), payload)
    return payload


def _fill_names(rows: list[dict[str, Any]]) -> str:
    note = _apply_names(rows)
    if note and "не ответил" in note:
        time.sleep(1)
        note = _apply_names(rows)
    return note


def clear_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def _ha(row: dict[str, Any]) -> float:
    try:
        return float(row.get("ha") or 0)
    except (TypeError, ValueError):
        return 0.0


def _max_rows() -> int:
    try:
        return max(1, int(os.environ.get("TASK_ASK_MAX_ROWS", "200")))
    except ValueError:
        return 200


def _top(rows: list[dict[str, Any]], key: str, limit: int = 12) -> str:
    buckets: dict[str, float] = {}
    for row in rows:
        name = str(row.get(key) or "").strip() or "—"
        buckets[name] = buckets.get(name, 0.0) + _ha(row)
    ranked = sorted(buckets.items(), key=lambda item: item[1], reverse=True)
    parts = [f"{name}={value:.2f}" for name, value in ranked[:limit]]
    if len(ranked) > limit:
        rest = sum(value for _name, value in ranked[limit:])
        parts.append(f"…ещё {len(ranked) - limit}={rest:.2f}")
    return "; ".join(parts)


def format_ask_table(rows: list[dict[str, Any]], *, max_rows: int | None = None) -> tuple[str, dict[str, Any]]:
    limit = _max_rows() if max_rows is None else max(1, int(max_rows))
    clean = [row for row in rows if isinstance(row, dict)]
    total = round(sum(_ha(row) for row in clean), 2)
    ordered = sorted(clean, key=lambda row: (_ha(row), str(row.get("at") or "")), reverse=True)
    shown = ordered[:limit]
    lines = ["дата\tагрофирма\tкультура\tоперация\tгосномер\tмеханизатор\tполе\tга"]
    for row in shown:
        lines.append(
            "\t".join(
                [
                    str(row.get("at") or "—"),
                    str(row.get("firm") or "—"),
                    str(row.get("crop") or "—"),
                    str(row.get("operation") or "—"),
                    str(row.get("plate") or "—"),
                    str(row.get("operator") or "—"),
                    str(row.get("field") or "—"),
                    f"{_ha(row):.2f}",
                ]
            )
        )
    truncated = len(clean) > len(shown)
    if truncated:
        hidden = ordered[len(shown) :]
        hidden_ha = round(sum(_ha(row) for row in hidden), 2)
        lines.append(f"… не показано {len(hidden)} строк на {hidden_ha:.2f} га (мельче показанных)")
    lines.append(f"Итого: {len(clean)} строк, {total:.2f} га")
    if clean:
        lines.append("По операциям: " + _top(clean, "operation"))
        lines.append("По агрофирмам: " + _top(clean, "firm"))
        lines.append("По культурам: " + _top(clean, "crop"))
    return "\n".join(lines), {
        "rows_total": len(clean),
        "rows_sent": len(shown),
        "ha_total": total,
        "truncated": truncated,
    }


def ask_rows(rows: list[dict[str, Any]], question: str, *, use_llm: bool = True) -> dict[str, Any]:
    table, meta = format_ask_table(rows)
    question = str(question or "").strip()
    out: dict[str, Any] = {
        "ok": True,
        "available": True,
        "llm_used": False,
        "question": question,
        "answer": table if meta["rows_total"] else "По заданному фильтру заданий нет.",
        "usage": None,
        "model": None,
        "error": None,
        **meta,
    }
    if not use_llm or not question or not meta["rows_total"]:
        return out
    from app.adapters.outbound.proverka.service import _ensure_plan_path

    _ensure_plan_path()
    from proverka.ai.facts_qa import answer_tsv

    answered = answer_tsv(question, table, system=ASK_SYSTEM)
    out.update(
        ok=answered.get("ok", True),
        llm_used=bool(answered.get("llm_used")),
        answer=answered.get("answer") or out["answer"],
        usage=answered.get("usage"),
        model=answered.get("model"),
        error=answered.get("error"),
    )
    return out


def list_task_work_groups() -> list[dict[str, Any]]:
    """Группы работ в том же составе и порядке, что WORK_GROUPS в plans.py."""
    from app.adapters.outbound.proverka.service import _ensure_plan_path

    _ensure_plan_path()
    from plans import WORK_GROUPS

    groups: list[dict[str, Any]] = []
    for name, ids in WORK_GROUPS:
        title = str(name or "").strip()
        if not title:
            continue
        parsed: list[int] = []
        for raw in ids:
            try:
                parsed.append(int(raw))
            except (TypeError, ValueError):
                continue
        groups.append({"name": title, "ids": parsed})
    return groups


def _clean_texts(values: object, *, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    found: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip()
        if not text or len(text) > 160 or text in seen:
            continue
        seen.add(text)
        found.append(text)
        if len(found) >= limit:
            break
    return found


def _clean_ids(values: object, *, limit: int) -> list[int]:
    if not isinstance(values, list):
        return []
    found: list[int] = []
    seen: set[int] = set()
    for raw in values:
        try:
            number = int(raw)
        except (TypeError, ValueError):
            continue
        if number <= 0 or number in seen:
            continue
        seen.add(number)
        found.append(number)
        if len(found) >= limit:
            break
    return found


def operation_bounds(
    operations: object = None,
    work_type_ids: object = None,
    operation_labels: object = None,
) -> dict[str, Any]:
    """Границы среза: группы plans.WORK_GROUPS и, отдельно, конкретные виды работ."""
    names = _clean_texts(operations, limit=40)
    labels = _clean_texts(operation_labels, limit=80)
    ids = _clean_ids(work_type_ids, limit=300)
    bounds: dict[str, Any] = {}
    if names:
        known = {item["name"]: item["ids"] for item in list_task_work_groups()}
        resolved: set[int] = set()
        for name in names:
            resolved.update(known.get(name) or [])
        bounds["group_ids"] = resolved
        bounds["group_names"] = names
    if ids:
        bounds["detail_ids"] = ids
    if labels:
        bounds["detail_labels"] = labels
    return bounds


def ask_slice(report_date: date, question: str, *, use_llm: bool = True, **filters: Any) -> dict[str, Any]:
    payload = build_slice(report_date)
    if not payload.get("available"):
        return payload
    bounds = operation_bounds(
        filters.pop("operations", None),
        filters.pop("work_type_ids", None),
        filters.pop("operation_labels", None),
    )
    picked = filter_record_rows(payload.get("rows") or [], **filters, **bounds)
    answer = ask_rows(picked, question, use_llm=use_llm)
    answer["report_date"] = report_date.isoformat()
    return answer
