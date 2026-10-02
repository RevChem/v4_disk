# -*- coding: utf-8 -*-
"""Кэш справочников Cropwise (fields, history_items, inventory_history_items, machines, agro_operations,
crops, field_groups, group_folders) рядом с хранилищем заданий: ``<SVODKI_TASK_STORE_DIR>/refs.sqlite``.

Отдельный файл, а не таблица в store.sqlite: запись справочников не должна ждать долгих транзакций
синхронизации заданий. Хранится ПОЛНЫЙ список строк под ключом «endpoint + параметры без from_id/limit».

Как работает. Перехватчик в sitecustomize.py (requests и httpx) видит страничный GET справочника;
если полный список есть в кэше и моложе ``SVODKI_REF_CACHE_TTL_SEC`` (3600), страница собирается из него
(from_id/limit/meta как у API). Промах — перехватчик один раз (замок flock на ключ) выгружает весь
список курсором, кладёт в кэш и отдаёт страницу. Любая ошибка кэша — запрос идёт в API как раньше.
Синхронизация перед пачкой (tools/sync_task_store.py) обновляет типовые наборы принудительно.

Выключить: ``SVODKI_REF_CACHE_DISABLE=1``; кэш работает только при заданном SVODKI_TASK_STORE_DIR.
"""
from __future__ import annotations

import bisect
import contextlib
import hashlib
import json
import os
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

ENDPOINTS = frozenset({
    "fields", "history_items", "inventory_history_items", "machines", "agro_operations",
    "crops", "field_groups", "group_folders",
})
PAGE_MAX = 1000
_MEMO: dict[str, tuple[float, list[int], list[dict]]] = {}


def _truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def ttl_sec() -> float:
    try:
        return float(os.environ.get("SVODKI_REF_CACHE_TTL_SEC", "").strip() or 3600)
    except ValueError:
        return 3600.0


def cache_dir() -> Path | None:
    raw = (os.environ.get("SVODKI_TASK_STORE_DIR") or "").strip()
    if not raw or _truthy("SVODKI_REF_CACHE_DISABLE") or _truthy("SVODKI_TASK_STORE_DISABLE"):
        return None
    return Path(raw)


def _pairs(params: Any) -> list[tuple[str, str]]:
    if params is None:
        return []
    items = params.items() if isinstance(params, dict) else params
    out: list[tuple[str, str]] = []
    for k, v in items:
        if isinstance(v, (list, tuple)):
            out.extend((str(k), str(x)) for x in v)
        elif v is not None:
            out.append((str(k), str(v)))
    return out


def parse_request(method: str, url: Any, params: Any) -> dict | None:
    """None — запрос не про справочник из белого списка. Иначе endpoint, ключ, from_id, limit."""
    if str(method).upper() != "GET" or _truthy("SVODKI_REF_CACHE_BYPASS"):
        return None
    base = (os.environ.get("CROPWISE_BASE_URL") or "").strip().rstrip("/")
    u = urlsplit(str(url))
    path = u.path
    if base:
        b = urlsplit(base)
        prefix = b.path.rstrip("/") + "/"
        if u.netloc != b.netloc or not path.startswith(prefix):
            return None
        endpoint = path[len(prefix):].strip("/")
    elif "/api/v3/" in path:
        endpoint = path.split("/api/v3/", 1)[1].strip("/")
    else:
        return None
    if endpoint not in ENDPOINTS:
        return None
    pairs = parse_qsl(u.query, keep_blank_values=True) + _pairs(params)
    from_id, limit, extra = 0, PAGE_MAX, []
    for k, v in pairs:
        if k == "from_id":
            try:
                from_id = int(v)
            except ValueError:
                return None
        elif k == "limit":
            try:
                limit = max(1, min(PAGE_MAX, int(v)))
            except ValueError:
                return None
        else:
            extra.append((k, v))
    extra.sort()
    key = hashlib.sha1(json.dumps([base, endpoint, extra], ensure_ascii=False).encode()).hexdigest()
    return {"endpoint": endpoint, "key": key, "extra": extra, "from_id": from_id, "limit": limit,
            "base": base, "url": f"{base}/{endpoint}" if base else str(url).split("?")[0]}


def _db(d: Path) -> sqlite3.Connection:
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(d / "refs.sqlite"), timeout=60, isolation_level=None)
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("CREATE TABLE IF NOT EXISTS ref (key TEXT PRIMARY KEY, endpoint TEXT, params TEXT, "
                "fetched_at REAL, n INTEGER, rows BLOB)")
    return con


def load(req: dict, max_age: float | None = None) -> tuple[list[int], list[dict]] | None:
    d = cache_dir()
    if d is None:
        return None
    age = ttl_sec() if max_age is None else max_age
    m = _MEMO.get(req["key"])
    if m and time.time() - m[0] <= age:
        return m[1], m[2]
    con = _db(d)
    try:
        row = con.execute("SELECT fetched_at, rows FROM ref WHERE key=?", (req["key"],)).fetchone()
    finally:
        con.close()
    if row is None or time.time() - row[0] > age:
        return None
    rows = json.loads(zlib.decompress(row[1]).decode("utf-8"))
    rows.sort(key=lambda r: int(r.get("id", 0)))
    ids = [int(r.get("id", 0)) for r in rows]
    _MEMO[req["key"]] = (row[0], ids, rows)
    return ids, rows


def store(req: dict, rows: list[dict]) -> None:
    d = cache_dir()
    if d is None:
        return
    blob = zlib.compress(json.dumps(rows, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"), 3)
    now = time.time()
    con = _db(d)
    try:
        con.execute("INSERT OR REPLACE INTO ref VALUES (?,?,?,?,?,?)",
                    (req["key"], req["endpoint"], json.dumps(req["extra"], ensure_ascii=False), now, len(rows), blob))
    finally:
        con.close()
    _MEMO.pop(req["key"], None)


@contextlib.contextmanager
def key_lock(req: dict):
    """Один процесс выгружает ключ, остальные ждут и берут готовое."""
    d = cache_dir()
    fh = None
    try:
        if d is not None and fcntl is not None:
            d.mkdir(parents=True, exist_ok=True)
            fh = open(d / f"ref_{req['key'][:16]}.lock", "a+")
            fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        if fh is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


def page_entry(req: dict, ids: list[int], rows: list[dict]) -> dict:
    """Страница в формате записи sitecustomize (status_code/headers/text/url)."""
    i = bisect.bisect_left(ids, req["from_id"])
    chunk = rows[i : i + req["limit"]]
    last = int(chunk[-1]["id"]) if chunk else None
    body = {"data": chunk, "meta": {"response": {
        "last_record_id": last, "obtained_records": len(chunk), "total_count": len(rows),
        "has_more": i + len(chunk) < len(rows), "per_page": req["limit"]}}}
    return {"status_code": 200, "headers": {"content-type": "application/json", "x-svodki-ref-cache": "1"},
            "text": json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str), "url": req["url"]}


def extract_rows(body: Any) -> list[dict]:
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, list) else []


def standard_sets(years: list[int]) -> list[tuple[str, dict]]:
    """Типовые выгрузки справочников сводок (их параметры — как в 2_main_ideal_compact._fetch_reference_raw)."""
    sets: list[tuple[str, dict]] = [
        ("fields", {}), ("machines", {}), ("crops", {}), ("field_groups", {}), ("group_folders", {}),
        ("inventory_history_items", {"historyable_type": "Field"}),
    ]
    sets += [("history_items", {"year": y}) for y in years]
    return sets


async def refresh_standard(client, sem, base_url: str, years: list[int], log: str = "") -> int:
    """Принудительно перезагрузить типовые справочники в кэш (вызывает синхронизация перед пачкой)."""
    from async_http import fetch_pages

    os.environ["SVODKI_REF_CACHE_BYPASS"] = "1"
    n = 0
    try:
        async def one(ep: str, params: dict) -> None:
            nonlocal n
            t0 = time.monotonic()
            rows = await fetch_pages(client, f"{base_url.rstrip('/')}/{ep}", params, sem)
            os.environ.pop("SVODKI_REF_CACHE_BYPASS", None)
            try:
                req = parse_request("GET", f"{base_url.rstrip('/')}/{ep}", params)
            finally:
                os.environ["SVODKI_REF_CACHE_BYPASS"] = "1"
            if req is not None:
                with key_lock(req):
                    store(req, rows)
                n += 1
                print(f"  {log}[ref] {ep} {params or ''}: {len(rows)} строк, {time.monotonic() - t0:.1f} с", flush=True)

        import asyncio

        await asyncio.gather(*(one(ep, pr) for ep, pr in standard_sets(years)))
    finally:
        os.environ.pop("SVODKI_REF_CACHE_BYPASS", None)
    return n
