"""Вопросы по заданиям (задание × поле) из уже выполненной проверки — без повторного прогона.

Источник — файлы task_facts_<дата>__r<id>.tsv в каталоге job проверки
(uploads/proverka/<job_id>/), которые пишет расчёт сводки (proverka/task_facts.py).
Без job_id берётся самый свежий job за дату, где такие файлы есть.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from app.adapters.outbound.proverka.service import (
    _ensure_plan_path,
    job_dir,
    proverka_jobs_root,
)

_JOB_ID_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")  # uuid4 из create_job; защита от ../
_FILTER_KEYS = ("group", "crop", "firm", "machine", "field")


def _tf():
    _ensure_plan_path()
    from proverka import task_facts

    return task_facts


def resolve_facts_dir(report_date: date, job_id: str | None = None) -> tuple[Path, str, float] | None:
    """(каталог job, job_id, mtime самого свежего файла фактов) или None."""
    tf = _tf()
    if job_id:
        if not _JOB_ID_RE.match(job_id):
            raise ValueError("Некорректный job_id")
        d = job_dir(job_id)
        files = tf.facts_files(d, report_date)
        if not files:
            return None
        return d, job_id, max(f.stat().st_mtime for f in files)

    root = proverka_jobs_root()
    if not root.is_dir():
        return None
    best: tuple[Path, float] | None = None
    for child in root.iterdir():
        if not child.is_dir():
            continue
        files = tf.facts_files(child, report_date)
        if not files:
            continue
        mtime = max(f.stat().st_mtime for f in files)
        if best is None or mtime > best[1]:
            best = (child, mtime)
    if best is None:
        return None
    return best[0], best[0].name, best[1]


def _iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _not_found(report_date: date, job_id: str | None) -> dict[str, Any]:
    return {
        "available": False,
        "report_date": report_date.isoformat(),
        "job_id": job_id,
        "detail": (
            "Для этой даты нет сохранённых фактов по заданиям. Запустите проверку сводки 3 "
            "(факты пишутся при расчёте; нужен PROVERKA_TASK_FACTS≠0)."
        ),
    }


def _load(report_date: date, job_id: str | None, report_ids: Sequence[str] | None):
    found = resolve_facts_dir(report_date, job_id)
    if not found:
        return None
    d, jid, mtime = found
    rows = _tf().load_task_facts(d, report_date, report_ids=report_ids or None)
    return jid, mtime, rows


def _apply_filter(rows: list[dict[str, Any]], flt: dict[str, Any]) -> list[dict[str, Any]]:
    kw = {k: flt.get(k) for k in _FILTER_KEYS}
    return _tf().filter_facts(
        rows,
        **kw,
        task_id=flt.get("task_id"),
        min_ha=flt.get("min_ha"),
        exact=bool(flt.get("exact")),
    )


def facts_options(report_date: date, job_id: str | None = None) -> dict[str, Any]:
    """Что доступно для фильтров (выпадающие списки) и насколько свежий снимок."""
    tf = _tf()
    loaded = _load(report_date, job_id, None)
    if loaded is None:
        return _not_found(report_date, job_id)
    jid, mtime, rows = loaded
    return {
        "available": True,
        "report_date": report_date.isoformat(),
        "job_id": jid,
        "generated_at": _iso(mtime),
        "rows": len(rows),
        "ha_total": round(sum(r["ha"] for r in rows), 2),
        "values": tf.distinct_values(rows),
    }


def facts_query(
    report_date: date,
    job_id: str | None,
    flt: dict[str, Any],
    *,
    report_ids: Sequence[str] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Отфильтрованные строки + итоги — без модели, 0 токенов."""
    tf = _tf()
    loaded = _load(report_date, job_id, report_ids)
    if loaded is None:
        return _not_found(report_date, job_id)
    jid, mtime, rows = loaded
    picked = _apply_filter(rows, flt)
    picked.sort(key=lambda r: (-r["ha"], r["firm"], r["task_id"] or 0))
    shown = []
    for r in picked[:limit]:
        shown.append({**r, "task_url": tf.task_url(r.get("machine_id"), r.get("task_id"))})
    return {
        "available": True,
        "report_date": report_date.isoformat(),
        "job_id": jid,
        "generated_at": _iso(mtime),
        "rows_total": len(picked),
        "ha_total": round(sum(r["ha"] for r in picked), 2),
        "by_firm": tf.aggregate(picked, ("firm",)),
        "by_crop": tf.aggregate(picked, ("crop",)),
        "rows": shown,
        "truncated": len(picked) > len(shown),
    }


def facts_ask(
    report_date: date,
    job_id: str | None,
    flt: dict[str, Any],
    question: str,
    *,
    report_ids: Sequence[str] | None = None,
    columns: Sequence[str] | None = None,
    max_rows: int | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Ответ модели по срезу. Срез фильтруется здесь — модель не видит остальное."""
    _ensure_plan_path()
    from proverka.ai.facts_qa import ask_facts

    loaded = _load(report_date, job_id, report_ids)
    if loaded is None:
        return _not_found(report_date, job_id)
    jid, mtime, rows = loaded
    picked = _apply_filter(rows, flt)
    res = ask_facts(question, picked, columns=columns, max_rows=max_rows, use_llm=use_llm)
    res.update(
        available=True,
        report_date=report_date.isoformat(),
        job_id=jid,
        generated_at=_iso(mtime),
    )
    return res
