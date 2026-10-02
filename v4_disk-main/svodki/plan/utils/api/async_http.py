# -*- coding: utf-8 -*-
"""Общий async HTTP-слой для пакетных загрузчиков (mtfmi / mtaomi / machine_tasks).

Зачем:
  * retry с экспоненциальным backoff на 429 / 5xx / сетевые сбои (раньше повторов не было);
  * семафор держится только на время самого запроса, пауза перед повтором — вне семафора,
    иначе «спящие» повторы занимали бы слоты и тормозили остальные запросы;
  * СТРОГИЙ режим: если данные не удалось получить, это исключение, а не молчаливая
    пустая выборка. SVODKI_FETCH_STRICT=0 возвращает прежнее поведение (глотать ошибки).

Переменные окружения:
  SVODKI_HTTP_RETRIES (5), SVODKI_HTTP_BACKOFF_BASE (1.0 с), SVODKI_FETCH_STRICT (1).
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
from typing import Any

import httpx


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int((os.environ.get(name) or str(default)).strip()))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float((os.environ.get(name) or str(default)).strip()))
    except ValueError:
        return default


HTTP_RETRIES = _env_int("SVODKI_HTTP_RETRIES", 5)
BACKOFF_BASE = _env_float("SVODKI_HTTP_BACKOFF_BASE", 1.0)
BACKOFF_MAX = 30.0
RETRY_AFTER_MAX = 60.0
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
STRICT_FETCH = (os.environ.get("SVODKI_FETCH_STRICT", "1").strip().lower()
                not in {"0", "false", "no", "off"})

from cropwise_paging import (
    CROPWISE_PAGE_LIMIT,
    CROPWISE_PAGE_SIZE,
    from_id_should_continue,
)

_STATS = {"429": 0, "5xx": 0, "network": 0}
_STATS_LOCK = threading.Lock()


def _count(kind: str) -> None:
    with _STATS_LOCK:
        _STATS[kind] += 1


def retry_summary() -> str:
    """Итог по повторам за процесс — для строки в конце отчёта."""
    with _STATS_LOCK:
        total = sum(_STATS.values())
        if not total:
            return "HTTP: повторов не потребовалось"
        return (f"HTTP: повторов {total} (429: {_STATS['429']}, "
                f"5xx: {_STATS['5xx']}, сеть: {_STATS['network']})")


def is_client_error(exc: BaseException) -> bool:
    """4xx, кроме 429: запрос отвергнут как некорректный (работает и для requests, и для httpx)."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(code, int) and 400 <= code < 500 and code != 429


def should_fallback_single(exc: BaseException) -> bool:
    """Нужно ли после ошибки пакетного запроса пробовать поштучно.

    Строгий режим: только если пакет отвергнут как некорректный (4xx). Исчерпанные retry
    на 429/5xx/сеть поштучными запросами не лечатся, они лишь умножили бы нагрузку на
    упавший сервер, поэтому исключение пробрасывается. Нестрогий режим: как раньше, всегда.
    """
    return (not STRICT_FETCH) or is_client_error(exc)


def reraise_if_strict(exc: BaseException) -> None:
    """Вызывать там, где раньше ошибка глоталась (continue / пустой список)."""
    if STRICT_FETCH:
        raise exc


def _backoff(attempt: int, retry_after: str | None) -> float:
    delay = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** attempt)) * (0.5 + random.random() / 2)
    if retry_after:
        try:
            delay = max(delay, min(float(retry_after), RETRY_AFTER_MAX))
        except ValueError:
            pass
    return delay


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    sem: asyncio.Semaphore,
    *,
    params: dict | None = None,
    json_body: Any = None,
    timeout: float | None = None,
) -> Any:
    """Один запрос с повторами. После исчерпания попыток бросает HTTPStatusError / TransportError."""
    kwargs: dict[str, Any] = {}
    if params is not None:
        kwargs["params"] = params
    if json_body is not None:
        kwargs["json"] = json_body
    if timeout is not None:
        kwargs["timeout"] = timeout

    attempt = 0
    while True:
        try:
            async with sem:
                r = await client.request(method, url, **kwargs)
        except httpx.TransportError:
            if attempt >= HTTP_RETRIES:
                raise
            _count("network")
            delay = _backoff(attempt, None)
        else:
            if r.status_code in RETRY_STATUS and attempt < HTTP_RETRIES:
                _count("429" if r.status_code == 429 else "5xx")
                delay = _backoff(attempt, r.headers.get("Retry-After"))
            else:
                r.raise_for_status()
                return r.json()
        attempt += 1
        await asyncio.sleep(delay)  # вне семафора


# Эндпоинты, у которых проверено на живом API (tools/probe_changes_api.py, проба 1: limit=1000 и 5000
# дают ровно 1000 строк, meta.response.limit=1000): страница = ровно CROPWISE_PAGE_LIMIT строк,
# неполная страница — последняя. Для остальных прежнее правило (лишний запрос, зато без риска).
VERIFIED_FULL_PAGE = frozenset(
    x.strip() for x in os.environ.get(
        "SVODKI_VERIFIED_FULL_PAGE_ENDPOINTS",
        "machine_tasks,machine_task_field_mapping_items,versions",
    ).split(",") if x.strip()
)


def _short_page_is_last(url: str, n: int) -> bool:
    return n < CROPWISE_PAGE_LIMIT and url.rstrip("/").rsplit("/", 1)[-1] in VERIFIED_FULL_PAGE


async def fetch_pages(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    sem: asyncio.Semaphore,
) -> list[dict]:
    """Курсорная выгрузка (from_id). Условия остановки те же, что были в модулях."""
    items: list[dict] = []
    from_id = 0
    while True:
        body = await request_json(
            client, "GET", url, sem,
            params={**params, "from_id": from_id, "limit": CROPWISE_PAGE_LIMIT},
        )
        chunk = body.get("data", [])
        if not chunk:
            break
        items.extend(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not from_id_should_continue(meta, chunk):
            break
        if _short_page_is_last(url, len(chunk)):
            break  # неполная страница на эндпоинте с проверенным размером — следующая будет пустой
        from_id = int(last_id) + 1
    return items
