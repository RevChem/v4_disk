"""Сборка ленты «Логи»: действия по вкладкам и ошибки по сводкам."""
from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import false, func, or_
from sqlalchemy.orm import Session, defer

from app.adapters.outbound.persistence import repositories as repo
from app.application.audit_activity import (
    compact_report_ids,
    event_area,
    format_action_message,
    report_title,
    user_label,
)
from app.models import AuditLog, GenerationJob, User


# Записи аудита, которые не показываем в ленте «Логи».
# Вход в систему остаётся в audit_logs — по нему строится профиль пользователя.
HIDDEN_FEED_ACTIONS: tuple[str, ...] = (
    "reports.generation.requested",
    "reports.generation.completed",
    "reports.generation.failed",
    "reports.generation.cancelled",  # то же самое видно в карточке задания (статус «отменено»)
    "auth.login.success",
)

_FINISHED_JOB_STATUSES = ("completed", "failed", "cancelled")


def as_utc(dt: datetime) -> datetime:
    """SQLite отдаёт naive-время (UTC), PostgreSQL — aware."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def iso_utc(dt: datetime | None) -> str | None:
    return as_utc(dt).isoformat() if dt else None


def job_total_seconds(job: Any, result: dict[str, Any] | None = None) -> float | None:
    """Полное время формирования всех сводок задания, сек.

    Новые задания хранят точное значение в result_json.total_duration_seconds.
    Для старых заданий (до появления этого поля) берём разницу updated_at − created_at.
    """
    raw = (result or {}).get("total_duration_seconds")
    if isinstance(raw, (int, float)) and raw >= 0:
        return float(raw)
    if job.status != "completed" or not job.created_at or not job.updated_at:
        return None
    delta = (as_utc(job.updated_at) - as_utc(job.created_at)).total_seconds()
    return round(delta, 1) if delta >= 0 else None


def _parse_details(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {"raw": raw}


def _log_lines_for_report(logs: list[str], report_id: str) -> list[str]:
    if not logs:
        return []
    rid = str(report_id)
    patterns = (
        re.compile(rf"\b{re.escape(rid)}\b", re.I),
        re.compile(rf"report\s*{re.escape(rid)}\b", re.I),
        re.compile(rf"сводк[aи]?\s*{re.escape(rid)}\b", re.I),
    )
    out: list[str] = []
    for line in logs:
        s = str(line).strip()
        if not s:
            continue
        if any(p.search(s) for p in patterns):
            out.append(s)
    if not out and logs:
        # fallback: последние строки с error по всему job
        out = [
            str(x).strip()
            for x in logs[-40:]
            if re.search(r"error|ошибк|failed|exception|traceback", str(x), re.I)
        ]
    return out[-30:]


# ── Постраничная лента ───────────────────────────────────────────────────────────────────
# Каждая вкладка читается из БД отдельно: свой счётчик, свой offset/limit. Раньше все вкладки
# делили один срез «последние 80 событий из 240 записей аудита», из-за чего старые правки
# (например, в «Редакторе планов») просто не попадали в ответ.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
_CACHE_TTL_SECONDS = 20.0
_MAX_SEASON_EVENTS_PER_JOB = 500

REPORT_FAILED_ACTION = "reports.generation.report_failed"

# (id вкладки, подпись, области event_area()). Порядок = порядок вкладок в интерфейсе.
FIXED_TABS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("users", "Пользователи", ("Пользователи", "Прочее")),
    ("reports", "Сводки", ("Сводки",)),
    ("generation", "Генерация", ("Генерация",)),
    ("seasons", "Сезоны", ()),
    ("plans", "Редактор планов", ("Редактор планов",)),
    ("page_order", "Порядок страниц", ("Порядок страниц",)),
    ("pdf", "Подписи PDF", ("Подписи PDF",)),
    ("cells", "Правки ячеек", ("Правки ячеек",)),
    ("comments", "Комментарии", ("Комментарии",)),
    ("schedule", "Расписание", ("Расписание",)),
    ("proverka", "Проверка", ("Проверка",)),
)
_AREA_TO_TAB: dict[str, str] = {
    area: tab_id for tab_id, _label, areas in FIXED_TABS for area in areas
}
# Вкладки, доступные не только разработчику.
_PUBLIC_TABS = ("reports", "seasons")

_JOB_STATUS_RU = {
    "pending": "в очереди",
    "running": "выполняется…",
    "completed": "готово",
    "cancelled": "отменено",
}

_CACHE: dict[tuple, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()


class _Scope:
    """Кто смотрит ленту и какой фильтр по пользователю выбран."""

    def __init__(self, viewer: User, user_filter: str | None) -> None:
        self.viewer = viewer
        self.see_all = viewer.role in ("admin", "developer")
        self.developer = viewer.role == "developer"
        self.uid = (user_filter or "").strip()

    @property
    def cache_key(self) -> tuple:
        return ("all" if self.see_all else self.viewer.id, self.uid)


def _user_id_filter(column: Any, uid: str) -> Any | None:
    if not uid:
        return None
    if uid == "system":
        return column.is_(None)
    try:
        return column == int(uid)
    except ValueError:
        return false()


def _scope_audit(q: Any, scope: _Scope) -> Any:
    if not scope.see_all:
        q = q.filter(or_(AuditLog.user_id.is_(None), AuditLog.user_id == scope.viewer.id))
    cond = _user_id_filter(AuditLog.user_id, scope.uid)
    return q.filter(cond) if cond is not None else q


def _scope_jobs(q: Any, scope: _Scope) -> Any:
    if not scope.see_all:
        q = q.filter(GenerationJob.user_id == scope.viewer.id)
    cond = _user_id_filter(GenerationJob.user_id, scope.uid)
    return q.filter(cond) if cond is not None else q


def _cached(key: tuple, loader: Any, refresh: bool) -> Any:
    now = time.monotonic()
    if not refresh:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]
    value = loader()
    with _CACHE_LOCK:
        _CACHE[key] = (now, value)
        if len(_CACHE) > 64:
            for old_key, _ in sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:32]:
                _CACHE.pop(old_key, None)
    return value


def _ts(dt: datetime | None) -> float:
    return as_utc(dt).timestamp() if dt else 0.0


class _Users:
    """Кэш пользователей в рамках одного запроса."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._cache: dict[int, User | None] = {}

    def get(self, uid: int | None) -> User | None:
        if uid is None:
            return None
        if uid not in self._cache:
            self._cache[uid] = repo.get_user_by_id(self._db, uid)
        return self._cache[uid]

    def label(self, uid: int | None) -> str:
        return user_label(self.get(uid))


def _audit_event(row: AuditLog, users: _Users) -> dict[str, Any]:
    details = _parse_details(row.details)
    username = details.get("username") or users.label(row.user_id)
    message = details.get("message") or format_action_message(row.action, username, details)
    return {
        "id": f"audit-{row.id}",
        "created_at": iso_utc(row.created_at),
        "action": row.action,
        "message": message,
        "user_id": row.user_id,
        "username": username,
        "ip": row.ip,
        "job_id": details.get("job_id"),
        "summary_date": details.get("summary_date") or details.get("report_date"),
        "report_ids": details.get("report_ids") or [],
        "details": details,
        "area": event_area(row.action),
    }


def _error_event_from_audit(row: AuditLog, users: _Users) -> dict[str, Any]:
    ev = _audit_event(row, users)
    details = ev["details"]
    rid = str(details.get("report_id") or "")
    ev.pop("details", None)
    ev.update({
        "kind": "error",
        "report_id": rid,
        "title": details.get("title") or report_title(rid),
        "status": "error",
        "error": details.get("error") or ev["message"],
        "log_lines": details.get("log_lines") or [],
    })
    return ev


def _audit_rows(db: Session, scope: _Scope, actions: list[str], offset: int, limit: int) -> list[AuditLog]:
    if not actions or limit <= 0:
        return []
    q = _scope_audit(db.query(AuditLog).filter(AuditLog.action.in_(actions)), scope)
    return (
        q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def _audit_action_counts(db: Session, scope: _Scope) -> dict[str, int]:
    q = db.query(AuditLog.action, func.count(AuditLog.id)).filter(
        AuditLog.action.notin_(HIDDEN_FEED_ACTIONS)
    )
    q = _scope_audit(q, scope).group_by(AuditLog.action)
    return {str(action): int(n) for action, n in q.all()}


def _tab_for_action(action: str) -> str | None:
    """Вкладка для действия аудита; None — действие в аудитных вкладках не показываем."""
    if action == REPORT_FAILED_ACTION:
        return None  # ошибки сводок — это вкладка «Сводки», а не «Генерация»
    area = event_area(action)
    if area == "Сезоны":
        return None
    return _AREA_TO_TAB.get(area) or f"area:{area}"


def _actions_by_tab(counts: dict[str, int]) -> dict[str, list[str]]:
    """Какие действия аудита лежат на какой вкладке (неизвестные области — отдельные вкладки)."""
    out: dict[str, list[str]] = {}
    for action in counts:
        tab_id = _tab_for_action(action)
        if tab_id is not None:
            out.setdefault(tab_id, []).append(action)
    return out


def _merge_page(streams: list[list[tuple[float, Any]]], offset: int, size: int) -> list[Any]:
    """Слить уже отсортированные по убыванию потоки и взять страницу. Потоки — первые offset+size."""
    merged = [item for stream in streams for item in stream]
    merged.sort(key=lambda t: t[0], reverse=True)
    return [obj for _, obj in merged[offset: offset + size]]


# ── Ошибки по сводкам ────────────────────────────────────────────────────────────────────
def _load_report_errors(db: Session, scope: _Scope) -> list[tuple[float, dict[str, Any]]]:
    """Все ошибки сводок (новые сверху): из аудита + из заданий, которых в аудите нет."""
    users = _Users(db)
    out: list[tuple[float, dict[str, Any]]] = []
    seen: set[str] = set()
    covered: dict[str, int] = {}

    q = _scope_audit(db.query(AuditLog).filter(AuditLog.action == REPORT_FAILED_ACTION), scope)
    for row in q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).all():
        details = _parse_details(row.details)
        job_id = details.get("job_id")
        key = f"{job_id}:{details.get('report_id') or ''}"
        if key in seen:
            continue
        seen.add(key)
        if job_id:
            covered[str(job_id)] = covered.get(str(job_id), 0) + 1
        out.append((_ts(row.created_at), _error_event_from_audit(row, users)))

    # Старые задания (до появления аудита ошибок) и случаи, когда запись в аудит не удалась.
    jq = db.query(GenerationJob).options(defer(GenerationJob.result_json)).filter(
        GenerationJob.failed_count > 0
    )
    for job in _scope_jobs(jq, scope).order_by(GenerationJob.created_at.desc()).all():
        if covered.get(job.id, 0) >= job.failed_count:
            continue  # все ошибки задания уже есть в аудите — JSON не разбираем
        result = _load_result(job.result_json)
        logs = result.get("logs") or []
        username = users.label(job.user_id)
        when = job.updated_at or job.created_at
        for item in result.get("failed") or []:
            rid = str(item.get("report_id") or "")
            key = f"{job.id}:{rid}"
            if key in seen:
                continue
            seen.add(key)
            error = str(item.get("error") or "ошибка")
            title = item.get("title") or report_title(rid)
            out.append((_ts(when), {
                "id": f"job-{job.id}-{rid}",
                "kind": "error",
                "created_at": iso_utc(when),
                "action": REPORT_FAILED_ACTION,
                "message": f"Сводка {rid} ({title}): {error[:200]}",
                "user_id": job.user_id,
                "username": username,
                "job_id": job.id,
                "summary_date": job.summary_date.isoformat(),
                "report_id": rid,
                "title": title,
                "status": "error",
                "error": error,
                "log_lines": _log_lines_for_report(logs, rid),
                "area": "Сводки",
            }))
    out.sort(key=lambda t: t[0], reverse=True)
    return out


def _load_result(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


# ── Несовпадения сезонов ─────────────────────────────────────────────────────────────────
def _load_season_events(db: Session, scope: _Scope) -> list[tuple[float, dict[str, Any]]]:
    users = _Users(db)
    id_q = db.query(GenerationJob.id).filter(GenerationJob.result_json.like("%SEASON_MISMATCH%"))
    job_ids = [
        row[0]
        for row in _scope_jobs(id_q, scope).order_by(GenerationJob.created_at.desc()).all()
    ]
    out: list[tuple[float, dict[str, Any]]] = []
    # result_json с логами бывает тяжёлым — читаем пачками, не всё сразу.
    for start in range(0, len(job_ids), 20):
        chunk = job_ids[start: start + 20]
        rows = {
            job.id: job
            for job in db.query(
                GenerationJob.id,
                GenerationJob.user_id,
                GenerationJob.summary_date,
                GenerationJob.created_at,
                GenerationJob.updated_at,
                GenerationJob.result_json,
            ).filter(GenerationJob.id.in_(chunk))
        }
        for job_id in chunk:
            job = rows.get(job_id)
            if job is None:
                continue
            logs = _load_result(job.result_json).get("logs") or []
            ts = _ts(job.updated_at or job.created_at)
            for ev in _season_events_from_logs(job, logs, users.label(job.user_id)):
                out.append((ts, ev))
    out.sort(key=lambda t: t[0], reverse=True)  # сортировка устойчивая: порядок строк лога сохраняется
    return out


# ── Карточки заданий (вкладка «Генерация») ───────────────────────────────────────────────
def _job_card(job: GenerationJob, username: str) -> dict[str, Any]:
    result = _load_result(job.result_json)
    failed_map = {str(x.get("report_id")): x for x in (result.get("failed") or [])}
    created_map = {str(x.get("report_id")): x for x in (result.get("created") or [])}
    try:
        rids = [str(x) for x in json.loads(job.report_ids or "[]")]
    except json.JSONDecodeError:
        rids = []
    # Пустой список = «все сводки»: показываем то, что реально было сформировано.
    all_reports = not rids
    if all_reports:
        rids = list(dict.fromkeys([*created_map, *failed_map]))

    reports_summary: list[dict[str, Any]] = []
    for rs in rids:
        if rs in failed_map:
            item = failed_map[rs]
            reports_summary.append({
                "report_id": rs,
                "title": item.get("title") or report_title(rs),
                "status": "error",
                "error": str(item.get("error") or "ошибка"),
            })
        else:
            reports_summary.append({
                "report_id": rs,
                "title": (created_map.get(rs) or {}).get("title") or report_title(rs),
                "status": "ok",
                "error": None,
            })

    has_error = bool(failed_map) or job.status == "failed"
    what = "все сводки" if all_reports and not rids else f"сводки {compact_report_ids(rids)}"
    if all_reports and rids:
        what = f"все сводки ({compact_report_ids(rids)})"
    msg = f"{username} — {what} за {job.summary_date.isoformat()}"
    if has_error:
        msg += f" (ошибок: {len(failed_map) or 1})"
    else:
        msg += f" — {_JOB_STATUS_RU.get(job.status, job.status)}"

    return {
        "id": f"job-{job.id}",
        "created_at": iso_utc(job.created_at),
        "action": "reports.generation.job",
        "message": msg,
        "user_id": job.user_id,
        "username": username,
        "job_id": job.id,
        "summary_date": job.summary_date.isoformat(),
        "report_ids": rids,
        "reports": reports_summary,
        "has_error": has_error,
        "job_status": job.status,
        "duration_seconds": job_total_seconds(job, result),
        "area": event_area("reports.generation.job"),
    }


# ── Поиск ────────────────────────────────────────────────────────────────────────────────
MAX_QUERY_TERMS = 8
# Ключи события, которые не должны участвовать в поиске: технические идентификаторы и флаги,
# иначе запрос «error» или «audit» находил бы вообще всё.
_SEARCH_SKIP_KEYS = frozenset({"id", "kind", "has_error", "user_id", "status", "details", "reports"})
_SEARCH_SKIP_DETAILS = frozenset({"category"})


def _norm(text: Any) -> str:
    return str(text).casefold().replace("ё", "е")


def search_terms(query: str | None) -> list[str]:
    """Слова запроса (нижний регистр, ё=е). Событие подходит, если в нём есть ВСЕ слова."""
    words = [_norm(w) for w in (query or "").split()]
    return list(dict.fromkeys(w for w in words if w))[:MAX_QUERY_TERMS]


def _flatten(value: Any, out: list[str]) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, dict):
        for v in value.values():
            _flatten(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _flatten(v, out)
    else:
        out.append(str(value))


def _ru_date_of(iso: Any) -> str:
    parts = str(iso or "")[:10].split("-")
    return f"{parts[2]}.{parts[1]}.{parts[0]}" if len(parts) == 3 and len(parts[0]) == 4 else ""


def _haystack(ev: dict[str, Any]) -> str:
    """Весь видимый и полезный для поиска текст события, в нормализованном виде."""
    parts: list[str] = []
    for key, value in ev.items():
        if key not in _SEARCH_SKIP_KEYS:
            _flatten(value, parts)
    details = ev.get("details")
    if isinstance(details, dict):
        _flatten({k: v for k, v in details.items() if k not in _SEARCH_SKIP_DETAILS}, parts)
    for item in ev.get("reports") or []:  # карточка задания: сводки и их ошибки
        if isinstance(item, dict):
            _flatten([item.get("report_id"), item.get("title"), item.get("error")], parts)
    # даты в привычном виде (дд.мм.гггг), чтобы можно было искать «15.10.2026»
    for key in ("created_at", "summary_date"):
        ru = _ru_date_of(ev.get(key))
        if ru:
            parts.append(ru)
    return _norm("\n".join(parts))


def _matches(ev: dict[str, Any], terms: list[str]) -> bool:
    text = _haystack(ev)
    return all(t in text for t in terms)


def _load_search_index(
    db: Session,
    scope: _Scope,
    terms: list[str],
    errors: list[tuple[float, dict[str, Any]]],
    seasons: list[tuple[float, dict[str, Any]]],
) -> dict[str, list[tuple[float, dict[str, Any]]]]:
    """Все совпавшие события по вкладкам (новые сверху). Читаем таблицы целиком один раз."""
    users = _Users(db)
    buckets: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)

    if scope.developer:
        q = _scope_audit(
            db.query(AuditLog).filter(AuditLog.action.notin_(HIDDEN_FEED_ACTIONS)), scope
        ).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        for row in q.yield_per(500):
            tab_id = _tab_for_action(row.action)
            if tab_id is None:
                continue
            ev = _audit_event(row, users)
            if tab_id == "reports":
                ev["kind"] = "change"
            if _matches(ev, terms):
                buckets[tab_id].append((_ts(row.created_at), ev))

        jq = _scope_jobs(db.query(GenerationJob), scope).order_by(
            GenerationJob.created_at.desc(), GenerationJob.id.desc()
        )
        for job in jq.yield_per(50):
            card = _job_card(job, users.label(job.user_id))
            if _matches(card, terms):
                buckets["generation"].append((_ts(job.created_at), card))

    buckets["reports"].extend((ts, ev) for ts, ev in errors if _matches(ev, terms))
    buckets["seasons"].extend((ts, ev) for ts, ev in seasons if _matches(ev, terms))
    for items in buckets.values():
        items.sort(key=lambda t: t[0], reverse=True)  # сортировка устойчивая
    return dict(buckets)


# ── Сборка страницы ──────────────────────────────────────────────────────────────────────
def build_activity_page(
    db: Session,
    *,
    viewer: User,
    tab: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    user_filter: str | None = None,
    refresh: bool = False,
    query: str | None = None,
) -> dict[str, Any]:
    """Одна страница выбранной вкладки + счётчики всех вкладок.

    tab — id вкладки (см. FIXED_TABS или «area:<название>»); page — с 1; user_filter —
    id пользователя, «system» (действия без пользователя) или пусто (все);
    query — поиск по тексту событий: счётчики вкладок показывают число совпадений.
    """
    scope = _Scope(viewer, user_filter)
    terms = search_terms(query)
    size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    users = _Users(db)

    counts = _audit_action_counts(db, scope) if scope.developer else {}
    by_tab = _actions_by_tab(counts)

    def audit_total(tab_id: str) -> int:
        return sum(counts.get(a, 0) for a in by_tab.get(tab_id, []))

    errors = _cached(("errors", scope.cache_key), lambda: _load_report_errors(db, scope), refresh)
    seasons = _cached(("seasons", scope.cache_key), lambda: _load_season_events(db, scope), refresh)

    jobs_q = _scope_jobs(db.query(func.count(GenerationJob.id)), scope)
    jobs_total = int(jobs_q.scalar() or 0) if scope.developer else 0

    found: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    if terms:
        found = _cached(
            ("search", scope.cache_key, " ".join(terms)),
            lambda: _load_search_index(db, scope, terms, errors, seasons),
            refresh,
        )

    def tab_total(tab_id: str) -> int:
        if terms:
            return len(found.get(tab_id, ()))
        if tab_id == "reports":
            return len(errors) + audit_total("reports")
        if tab_id == "seasons":
            return len(seasons)
        if tab_id == "generation":
            return audit_total(tab_id) + jobs_total
        return audit_total(tab_id)

    totals: dict[str, int] = {}
    tabs: list[dict[str, Any]] = []
    for tab_id, label, _areas in FIXED_TABS:
        if not scope.developer and tab_id not in _PUBLIC_TABS:
            continue
        totals[tab_id] = tab_total(tab_id)
        tabs.append({"id": tab_id, "label": label, "count": totals[tab_id]})
    if scope.developer:
        extra = sorted(
            (t for t in by_tab if t.startswith("area:")),
            key=lambda t: t.removeprefix("area:").casefold(),
        )
        for tab_id in extra:
            totals[tab_id] = tab_total(tab_id)
            tabs.append({"id": tab_id, "label": tab_id.removeprefix("area:"), "count": totals[tab_id]})

    visible = [t["id"] for t in tabs]
    default_tab = "users" if scope.developer else "reports"
    active = tab if tab in visible else default_tab
    total = totals[active]
    pages = max(1, -(-total // size))
    page = max(1, min(int(page or 1), pages))
    offset = (page - 1) * size
    need = offset + size

    items: list[dict[str, Any]]
    if terms:
        items = [ev for _, ev in found.get(active, [])[offset: offset + size]]
    elif active == "seasons":
        items = [ev for _, ev in seasons[offset: offset + size]]
    elif active == "reports":
        change_rows = _audit_rows(db, scope, by_tab.get("reports", []), 0, need)
        change = [(_ts(r.created_at), {**_audit_event(r, users), "kind": "change"}) for r in change_rows]
        items = _merge_page([change, errors[:need]], offset, size)
    elif active == "generation":
        audit_part = [
            (_ts(r.created_at), ("audit", r))
            for r in _audit_rows(db, scope, by_tab.get("generation", []), 0, need)
        ]
        jq = _scope_jobs(
            db.query(GenerationJob).options(defer(GenerationJob.result_json)), scope
        )
        job_part = [
            (_ts(j.created_at), ("job", j))
            for j in jq.order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc()).limit(need).all()
        ]
        items = []
        for kind, obj in _merge_page([audit_part, job_part], offset, size):
            items.append(_audit_event(obj, users) if kind == "audit" else _job_card(obj, users.label(obj.user_id)))
    else:
        rows = _audit_rows(db, scope, by_tab.get(active, []), offset, size)
        items = [_audit_event(r, users) for r in rows]

    if scope.see_all:
        user_rows = (
            db.query(User).filter(User.is_active.is_(True)).order_by(User.username.asc()).all()
        )
        users_list = [{"id": u.id, "username": user_label(u)} for u in user_rows]
    else:
        users_list = [{"id": viewer.id, "username": user_label(viewer)}]

    return {
        "tab": active,
        "tabs": tabs,
        "items": items,
        "page": page,
        "pages": pages,
        "page_size": size,
        "total": total,
        "users": users_list,
        "viewer_can_see_all": scope.see_all,
        "query": " ".join(terms),
        "terms": terms,
    }


def _season_events_from_logs(job: Any, logs: list, username: str) -> list[dict[str, Any]]:
    """Строки SEASON_MISMATCH из лога генерации — вкладка «Сезоны»."""
    events: list[dict[str, Any]] = []
    when = (job.updated_at or job.created_at)
    created = iso_utc(when)  # naive-время SQLite — это UTC; без суффикса браузер прочёл бы его как локальное
    date_s = job.summary_date.isoformat() if job.summary_date else ""
    n = 0
    for line in logs or []:
        text = str(line).strip()
        if "SEASON_MISMATCH" not in text:
            continue
        message = text.replace("SEASON_MISMATCH", "").strip(" -")
        if date_s and date_s not in message:
            message = f"{date_s} · {message}"
        events.append({
            "id": f"season-{job.id}-{n}",
            "created_at": created,
            "action": "seasons.mismatch",
            "message": message,
            "user_id": job.user_id,
            "username": username,
            "job_id": job.id,
            "summary_date": date_s,
            "area": "Сезоны",
        })
        n += 1
        if n >= _MAX_SEASON_EVENTS_PER_JOB:
            break
    return events


def job_detail(db: Session, job_id: str, *, viewer: User) -> dict[str, Any] | None:
    job = repo.get_generation_job(db, job_id)
    if job is None:
        return None
    if viewer.role not in ("admin", "developer") and job.user_id != viewer.id:
        return None
    user = repo.get_user_by_id(db, job.user_id) if job.user_id else None
    result = {}
    if job.result_json:
        try:
            result = json.loads(job.result_json)
        except json.JSONDecodeError:
            result = {}
    logs = result.get("logs") or []
    reports: list[dict[str, Any]] = []
    try:
        rids = json.loads(job.report_ids or "[]")
    except json.JSONDecodeError:
        rids = []
    failed_map = {str(x.get("report_id")): x for x in (result.get("failed") or [])}
    if not rids:
        # Пустой список = «все сводки»: показываем то, что реально было сформировано (как в карточке задания).
        created_ids = [str(x.get("report_id")) for x in (result.get("created") or []) if isinstance(x, dict)]
        rids = list(dict.fromkeys([*created_ids, *failed_map]))
    for rid in rids:
        rs = str(rid)
        failed = failed_map.get(rs)
        reports.append({
            "report_id": rs,
            "title": (failed or {}).get("title") or report_title(rs),
            "status": "error" if failed else ("ok" if job.status == "completed" else job.status),
            "error": failed.get("error") if failed else job.error_message,
            "log_lines": _log_lines_for_report(logs, rs) if failed else [],
        })
    return {
        "job_id": job.id,
        "status": job.status,
        "summary_date": job.summary_date.isoformat(),
        "username": user_label(user),
        "user_id": job.user_id,
        "reports": reports,
        "logs": logs[-200:],
        "error_message": job.error_message,
    }
