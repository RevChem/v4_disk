# -*- coding: utf-8 -*-
"""Клиент «Changed objects» Cropwise API v3: что изменилось с момента X.

Документация API (для любого ресурса):
    GET /{resource}/changes      ?from_time=&to_time=&limit=   — полные объекты, изменённые между датами
    GET /{resource}/changes_ids  ?from_time=&to_time=&limit=   — только id (+ updated_at) изменённых

Пагинации у этих методов в документации нет (только ``limit``), поэтому страницы НЕ курсором, а делением
окна времени пополам: если ответ упёрся в лимит — окно делится на две половины (с нахлёстом в 1 с,
чтобы граничные записи не терялись при любом варианте включения границ), пока каждая часть не станет
меньше лимита. Дубли по id убираются (побеждает запись с большим ``updated_at``).

Запасной путь — обычный список с фильтром ``updated_at_gt_eq`` и курсором ``from_id`` (работает на
проверенных эндпоинтах, ``tools/probe_changes_api.py``). Он включается, когда:
  * ``/changes`` не поддерживается ресурсом (4xx) — запоминается на сутки в ``caps``;
  * окно не делится (на одной секунде больше записей, чем лимит);
  * режим ``SVODKI_CHANGES_API=off``.

Режим ``SVODKI_CHANGES_API``:
  auto    (по умолчанию) — /changes, при невозможности — фильтр updated_at;
  verify  — оба пути, результат = объединение; расхождение пишется в лог (для проверки на живых данных);
  force   — только /changes, ошибка — исключение;
  off     — только фильтр updated_at.

Часовой пояс: время без пояса трактуется как МСК (``SVODKI_STORE_TZ_HOURS``, 3) — как во всём хранилище.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from async_http import fetch_pages, request_json

_ISO = "%Y-%m-%dT%H:%M:%S"
CAPS_BAD_TTL_SEC = 24 * 3600
_OVERLAP_SEC = 1.0
# Деление с нахлёстом сходится к окну 2 с (w → w/2 + 1), поэтому порог выше этой точки: окно ≤ 3 с,
# всё ещё упирающееся в лимит, делить дальше нельзя.
_MIN_WINDOW_SEC = 3.0


class ChangesUnsupported(Exception):
    """Ресурс не отдаёт /changes (или ответ другого формата)."""


class ChangesUnpaginable(Exception):
    """Окно в 2 секунды всё ещё не помещается в лимит: разделить его нельзя."""


def changes_mode() -> str:
    m = (os.environ.get("SVODKI_CHANGES_API") or "auto").strip().lower()
    return m if m in {"auto", "verify", "force", "off"} else "auto"


def changes_limit() -> int:
    try:
        return max(2, int((os.environ.get("SVODKI_CHANGES_LIMIT") or "").strip() or 1000))
    except ValueError:
        return 1000


def _tz() -> timezone:
    try:
        h = float((os.environ.get("SVODKI_STORE_TZ_HOURS") or "").strip() or 3)
    except ValueError:
        h = 3.0
    return timezone(timedelta(hours=h))


def fmt_offset(ts: float) -> str:
    """epoch → ``2015-02-03T22:25:32+03:00`` (формат из документации API)."""
    dt = datetime.fromtimestamp(ts, tz=_tz())
    s = dt.strftime(_ISO)
    off = dt.strftime("%z")
    return f"{s}{off[:3]}:{off[3:]}"


def fmt_naive(ts: float) -> str:
    """epoch → время без пояса (МСК) — формат фильтров updated_at_gt_eq в хранилище."""
    return datetime.fromtimestamp(ts, tz=_tz()).strftime(_ISO)


def _row_ts(row: dict) -> float:
    raw = row.get("updated_at")
    if not raw:
        return 0.0
    s = str(raw).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz())
    return dt.timestamp()


def dedupe_latest(rows: list[dict]) -> list[dict]:
    """Одна запись на id (у кого updated_at больше). Записи без id отбрасываются."""
    best: dict[int, dict] = {}
    for r in rows:
        try:
            i = int(r["id"] if isinstance(r, dict) else r)
        except (KeyError, TypeError, ValueError):
            continue
        cur = best.get(i)
        if cur is None or _row_ts(r) >= _row_ts(cur):
            best[i] = r
    return [best[i] for i in sorted(best)]


# --------------------------------------------------------------------------- возможности (caps)
def caps_usable(caps: dict | None, resource: str, now: float | None = None) -> bool:
    """False — /changes этого ресурса недавно признан неработающим."""
    if not caps:
        return True
    c = caps.get(resource)
    if not c or c.get("ok", True):
        return True
    return (now or time.time()) - float(c.get("ts") or 0) > CAPS_BAD_TTL_SEC


def caps_mark(caps: dict | None, resource: str, ok: bool, why: str = "") -> None:
    if caps is None:
        return
    caps[resource] = {"ok": bool(ok), "ts": time.time(), "why": why[:200]}


# --------------------------------------------------------------------------- /changes
def _rows_and_limit(body: Any, requested: int) -> tuple[list, int]:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise ChangesUnsupported("ответ без списка data")
    meta = ((body.get("meta") or {}).get("response") or {}) if isinstance(body, dict) else {}
    eff = requested
    try:
        srv = int(meta.get("limit") or meta.get("per_page") or 0)
        if 0 < srv < eff:
            eff = srv  # сервер режет страницу до своего максимума
    except (TypeError, ValueError):
        pass
    return data, eff


async def _window(client, url: str, a: float, b: float, sem, limit: int, stats: dict) -> list:
    body = await request_json(
        client, "GET", url, sem,
        params={"from_time": fmt_offset(a), "to_time": fmt_offset(b), "limit": limit},
    )
    stats["requests"] += 1
    rows, eff = _rows_and_limit(body, limit)
    if len(rows) < eff:
        return rows
    if b - a <= _MIN_WINDOW_SEC:
        raise ChangesUnpaginable(f"{url}: {len(rows)} записей в окне {b - a:.1f} с")
    mid = (a + b) / 2
    stats["splits"] += 1
    tasks = [
        asyncio.ensure_future(_window(client, url, a, mid + _OVERLAP_SEC, sem, limit, stats)),
        asyncio.ensure_future(_window(client, url, mid - _OVERLAP_SEC, b, sem, limit, stats)),
    ]
    try:
        left, right = await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:  # не оставляем вторую половину работать в фоне после отказа первой
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return left + right


async def _via_changes(client, base: str, resource: str, a: float, b: float, sem, *,
                       ids_only: bool, stats: dict) -> list:
    suffix = "changes_ids" if ids_only else "changes"
    url = f"{base.rstrip('/')}/{resource}/{suffix}"
    try:
        rows = await _window(client, url, a, b, sem, changes_limit(), stats)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if 400 <= code < 500 and code != 429:
            raise ChangesUnsupported(f"HTTP {code}") from exc
        raise
    if ids_only:
        return rows
    if rows and not all(isinstance(r, dict) and "id" in r for r in rows):
        raise ChangesUnsupported("записи без id")
    return dedupe_latest(rows)


def post_filter(rows: list[dict], since: float, *, tolerance: float = 120.0) -> tuple[list[dict], int]:
    """Отсечь записи старше since (на случай, если ресурс молча игнорирует фильтр updated_at_gt_eq и
    отдаёт всё). Записи без updated_at остаются. -> (оставшиеся, сколько отброшено)."""
    keep = [r for r in rows if not r.get("updated_at") or _row_ts(r) >= since - tolerance]
    return keep, len(rows) - len(keep)


async def _via_filter(client, base: str, resource: str, a: float, filter_params: dict | None, sem,
                      log: str = "") -> list[dict]:
    rows = await fetch_pages(
        client, f"{base.rstrip('/')}/{resource}",
        {**(filter_params or {}), "updated_at_gt_eq": fmt_naive(a)}, sem,
    )
    rows, dropped = post_filter(rows, a)
    if dropped:
        print(f"  {log}[changes] {resource}: фильтр updated_at_gt_eq не применяется ресурсом — "
              f"отброшено {dropped} старых записей на клиенте", flush=True)
    return rows


async def fetch_changed(
    client,
    base: str,
    resource: str,
    since: float,
    sem,
    *,
    until: float | None = None,
    filter_params: dict | None = None,
    caps: dict | None = None,
    log: str = "",
    mode: str | None = None,
) -> tuple[list[dict], str]:
    """Объекты ресурса, изменённые в [since, until]. -> (rows, источник: "changes" | "filter" | "verify").

    since/until — epoch; mode — переопределяет SVODKI_CHANGES_API для одного вызова.
    filter_params идут только в запасной путь (/changes фильтров ресурса не знает,
    поэтому вызывающий дофильтровывает результат сам).
    """
    mode = mode or changes_mode()
    until = until if until is not None else time.time() + 60
    stats = {"requests": 0, "splits": 0}
    t0 = time.monotonic()
    res_changes: list[dict] | None = None
    if mode != "off" and caps_usable(caps, resource):
        try:
            res_changes = await _via_changes(client, base, resource, since, until, sem, ids_only=False, stats=stats)
            caps_mark(caps, resource, True)
        except (ChangesUnsupported, ChangesUnpaginable) as exc:
            caps_mark(caps, resource, False, repr(exc))
            if mode == "force":
                raise
            print(f"  {log}[changes] {resource}: /changes недоступен ({exc}) — фильтр updated_at", flush=True)
        except Exception as exc:  # 5xx/сеть: в этот раз запасной путь, возможности не трогаем
            if mode == "force":
                raise
            print(f"  {log}[changes] {resource}: ошибка /changes ({exc!r}) — фильтр updated_at", flush=True)
    if mode == "force" and res_changes is None:
        raise ChangesUnsupported(resource)

    if res_changes is not None and mode != "verify":
        print(f"  {log}[changes] {resource}: {len(res_changes)} изменённых, запросов {stats['requests']}, "
              f"делений окна {stats['splits']}: {time.monotonic() - t0:.1f} с", flush=True)
        return res_changes, "changes"

    rows = await _via_filter(client, base, resource, since, filter_params, sem, log)
    if res_changes is None:
        return rows, "filter"

    # verify: сравнение двух путей на одном окне, результат — объединение
    mine = {int(r["id"]) for r in res_changes}
    flt = {int(r["id"]) for r in rows if r.get("id") is not None}
    only_c, only_f = mine - flt, flt - mine
    tag = "совпадают" if not only_c and not only_f else f"РАСХОЖДЕНИЕ: только /changes {len(only_c)}, только фильтр {len(only_f)}"
    print(f"  {log}[changes verify] {resource}: /changes {len(mine)} и фильтр {len(flt)} — {tag}", flush=True)
    return dedupe_latest(res_changes + rows), "verify"


async def fetch_changed_ids(
    client, base: str, resource: str, since: float, sem, *, until: float | None = None,
    caps: dict | None = None, log: str = "",
) -> set[int] | None:
    """id изменённых записей через /changes_ids; None — метод недоступен (вызывающий берёт fetch_changed)."""
    if changes_mode() == "off" or not caps_usable(caps, resource + "#ids"):
        return None
    stats = {"requests": 0, "splits": 0}
    try:
        rows = await _via_changes(client, base, resource, since, until if until is not None else time.time() + 60,
                                  sem, ids_only=True, stats=stats)
    except (ChangesUnsupported, ChangesUnpaginable) as exc:
        caps_mark(caps, resource + "#ids", False, repr(exc))
        return None
    except Exception as exc:
        print(f"  {log}[changes] {resource}: ошибка /changes_ids ({exc!r})", flush=True)
        return None
    caps_mark(caps, resource + "#ids", True)
    out: set[int] = set()
    for r in rows:
        try:
            out.add(int(r["id"] if isinstance(r, dict) else r))
        except (KeyError, TypeError, ValueError):
            continue
    return out
