"""Shared Cropwise API JSON snapshot for server report subprocesses.

Python imports ``sitecustomize`` automatically when this directory is on
``PYTHONPATH``. Перехват ``requests`` / ``httpx`` включается, если задано
``SVODKI_API_CACHE_PATH`` **или** ``SVODKI_PREFETCH_DATA_PATH`` (один JSON от
``svodki_prefetch.py`` — тот же снимок, что и для сводки 2, независимо от
момента запуска генератора). Только ``SVODKI_API_CACHE_PATH`` без prefetch по-прежнему
писал бы снимок при первом проходе; только prefetch — чтение из готового файла.

Из скрипта без ``PYTHONPATH`` можно вызвать ``install_svodki_api_hooks()`` из этого модуля
**до** ``import requests`` (см. ``23_analytics_svodka.py``).

При ``SVODKI_DISABLE_API_CACHE=1`` перехват отключён полностью.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _bootstrap_plan_import_paths() -> None:
    """plan/ в PYTHONPATH — добавить utils/* для import excel_style, season_unified, …"""
    try:
        import sys
        from pathlib import Path

        plan_root = Path(__file__).resolve().parent
        entries = [plan_root]
        for sub in (
            "utils/api",
            "utils/excel",
            "utils/season",
            "utils/report1",
            "utils/pdf",
            "utils/shared",
        ):
            p = plan_root / sub
            if p.is_dir():
                entries.append(p)
        for p in entries:
            s = str(p)
            if s not in sys.path:
                sys.path.insert(0, s)
    except Exception:
        pass


_bootstrap_plan_import_paths()


CACHE_PATH_ENV = "SVODKI_API_CACHE_PATH"
PREFETCH_DATA_ENV = "SVODKI_PREFETCH_DATA_PATH"
PREFETCH_STRICT_ENV = "SVODKI_PREFETCH_STRICT"
READ_ONLY_ENV = "SVODKI_API_CACHE_READ_ONLY"
PREFETCH_ONLY_ENV = "SVODKI_PREFETCH_ONLY"
LOCK_TIMEOUT_SECONDS = 300
LOCK_POLL_SECONDS = 0.05
CROPWISE_API_MARKER = "operations.cropwise.com/api/v3"


DISABLE_API_CACHE_ENV = "SVODKI_DISABLE_API_CACHE"
REPORT1_LIVE_MTFMI_ENV = "SVODKI_REPORT1_LIVE_MTFMI"
REPORT2_LIVE_API_ENV = "SVODKI_REPORT2_LIVE_API"

_RETRYABLE_HTTP_STATUS = frozenset({429, 502, 503, 504})


def _max_http_retries() -> int:
    try:
        return max(1, int(os.environ.get("SVODKI_PREFETCH_HTTP_RETRIES", "5")))
    except (TypeError, ValueError):
        return 5


def _retry_delay_seconds(attempt: int, response: Any = None) -> float:
    retry_after: float | None = None
    if response is not None:
        headers = getattr(response, "headers", None) or {}
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw is not None:
            try:
                retry_after = float(raw)
            except (TypeError, ValueError):
                retry_after = None
    if retry_after is not None and retry_after > 0:
        return min(60.0, retry_after)
    return min(30.0, 1.5 * (2 ** attempt))


def _fetch_requests_with_retry(fetch: Callable[[], Any]) -> Any:
    max_attempts = _max_http_retries()
    last: Any = None
    for attempt in range(max_attempts):
        last = fetch()
        status = int(getattr(last, "status_code", 200))
        if status not in _RETRYABLE_HTTP_STATUS:
            return last
        if attempt >= max_attempts - 1:
            return last
        time.sleep(_retry_delay_seconds(attempt, last))
    return last


async def _fetch_httpx_with_retry(fetch: Callable[[], Any]) -> Any:
    import asyncio

    max_attempts = _max_http_retries()
    last: Any = None
    for attempt in range(max_attempts):
        last = await fetch()
        status = int(getattr(last, "status_code", 200))
        if status not in _RETRYABLE_HTTP_STATUS:
            return last
        if attempt >= max_attempts - 1:
            return last
        await asyncio.sleep(_retry_delay_seconds(attempt, last))
    return last


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cache_path() -> Path | None:
    if os.environ.get(DISABLE_API_CACHE_ENV, "").strip().lower() in {"1", "true", "yes", "y", "on"}:
        return None
    raw = os.environ.get(CACHE_PATH_ENV, "").strip()
    if not raw:
        return None
    return Path(raw)


# (identity_token, parsed_json_or_none): сброс при другом файле или после его перезаписи.
_PREFETCH_CACHE: tuple[Any, dict[str, Any] | None] | None = None
_PREFETCH_INDEXES: dict[str, dict[str, dict[Any, list[dict[str, Any]]]]] = {}


def _prefetch_data_path() -> Path | None:
    raw = os.environ.get(PREFETCH_DATA_ENV, "").strip()
    if not raw:
        return None
    return Path(raw)


def _prefetch_identity_token() -> Any:
    """Путь prefetch + mtime + размер: при изменении файл перечитывается."""
    path = _prefetch_data_path()
    if path is None or not path.exists():
        return None
    try:
        st = path.stat()
        return (str(path.resolve()), int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return None


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


class CacheMissError(RuntimeError):
    pass


def _load_prefetch_data() -> dict[str, Any] | None:
    global _PREFETCH_CACHE
    token = _prefetch_identity_token()
    if (
        _PREFETCH_CACHE is not None
        and _PREFETCH_CACHE[0] == token
        and _PREFETCH_CACHE[1] is not None
    ):
        return _PREFETCH_CACHE[1]

    _PREFETCH_INDEXES.clear()
    if token is None:
        _PREFETCH_CACHE = (None, None)
        return None

    path = _prefetch_data_path()
    assert path is not None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _PREFETCH_CACHE = (token, None)
        if _env_flag(PREFETCH_STRICT_ENV):
            raise CacheMissError(f"Cannot read prefetch data: {path}: {exc}") from exc
        return None
    if not isinstance(data, dict) or not isinstance(data.get("endpoints"), dict):
        _PREFETCH_CACHE = (token, None)
        if _env_flag(PREFETCH_STRICT_ENV):
            raise CacheMissError(f"Invalid prefetch data: {path}")
        return None
    _PREFETCH_CACHE = (token, data)
    return data


def _endpoint_from_url(url: Any) -> str | None:
    parsed = urlsplit(str(url))
    marker = "/api/v3/"
    if marker in parsed.path:
        return parsed.path.split(marker, 1)[1].strip("/")
    base_url = os.environ.get("CROPWISE_BASE_URL", "").strip()
    if base_url:
        base = urlsplit(base_url)
        prefix = base.path.rstrip("/") + "/"
        if parsed.netloc == base.netloc and parsed.path.startswith(prefix):
            return parsed.path[len(prefix):].strip("/")
    return None


def _parse_params(url: Any, params: Any) -> dict[str, list[str]]:
    parsed = urlsplit(str(url))
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    pairs.extend(_normalize_pairs(params))
    out: dict[str, list[str]] = {}
    for k, v in pairs:
        out.setdefault(str(k), []).append(str(v))
    return out


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _index_for(endpoint: str, field: str, rows: list[dict[str, Any]]) -> dict[Any, list[dict[str, Any]]]:
    endpoint_indexes = _PREFETCH_INDEXES.setdefault(endpoint, {})
    cached = endpoint_indexes.get(field)
    if cached is not None:
        return cached
    idx: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        value = row.get(field)
        if value is None:
            continue
        idx.setdefault(value, []).append(row)
        int_value = _as_int(value)
        if int_value is not None and int_value != value:
            idx.setdefault(int_value, []).append(row)
    endpoint_indexes[field] = idx
    return idx


def _time_value(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if value is None and field == "end_time":
        value = row.get("finished_at") or row.get("updated_at")
    if value is None and field == "date_start":
        value = row.get("start_date")
    if value is None and field == "date_end":
        value = row.get("end_date")
    return str(value or "")


def _time_field_for_key(row: dict[str, Any], field: str) -> bool:
    if field in row:
        return True
    if field == "end_time":
        return "finished_at" in row or "updated_at" in row
    if field == "date_start":
        return "start_date" in row
    if field == "date_end":
        return "end_date" in row
    return False


def _matches(row: dict[str, Any], filters: dict[str, list[str]]) -> bool:
    for key, values in filters.items():
        if key in {"from_id", "limit", "page", "per_page", "_"}:
            continue
        value = values[-1] if values else ""
        if key == "no_date_end":
            want_no_end = str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
            has_end = bool(row.get("date_end") or row.get("end_date"))
            if want_no_end == has_end:
                return False
            continue
        if key == "only_active":
            want_active = str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
            if want_active and row.get("is_active") is False:
                return False
            continue
        if key.endswith("_gt_eq"):
            field = key[:-6]
            if _time_field_for_key(row, field) and _time_value(row, field) < value:
                return False
            continue
        if key.endswith("_lt_eq"):
            field = key[:-6]
            if _time_field_for_key(row, field) and _time_value(row, field) > value:
                return False
            continue
        if key.endswith("_gte"):
            field = key[:-4]
            if _time_field_for_key(row, field) and _time_value(row, field) < value:
                return False
            continue
        if key.endswith("_lt"):
            field = key[:-3]
            if _time_field_for_key(row, field) and _time_value(row, field) >= value:
                return False
            continue
        if key.endswith("_lte"):
            field = key[:-4]
            if _time_field_for_key(row, field) and _time_value(row, field) > value:
                return False
            continue
        # Strict equality only if the row has this field. Other filters
        # (например `season`, `only_active` для users и т.п.) применяются
        # на стороне сервера во время prefetch и не дублируются здесь.
        if key not in row:
            continue
        current = row[key]
        if current is None:
            return False
        if str(current) != value:
            cur_i = _as_int(current)
            val_i = _as_int(value)
            if cur_i is None or val_i is None or cur_i != val_i:
                return False
    return True


def _filtered_rows(endpoint: str, rows: list[dict[str, Any]], filters: dict[str, list[str]]) -> list[dict[str, Any]]:
    candidates = rows
    for key in (
        "machine_task_id",
        "work_type_id",
        "machine_id",
        "machine_region_id",
        "field_id",
        "crop_id",
        "historyable_type",
        "year",
    ):
        if key in filters and filters[key]:
            value = filters[key][-1]
            value_i = _as_int(value)
            idx = _index_for(endpoint, key, rows)
            if not idx:
                # Endpoint rows don't have this field at all; fall back to scan.
                continue
            candidates = idx.get(value_i if value_i is not None else value, [])
            break
    return [row for row in candidates if _matches(row, filters)]


def _mtfmi_prefetch_rows_missing_hourly(rows: list[dict[str, Any]]) -> bool:
    """Prefetch mtfmi без covered_area_hourly даёт нули/~55k в 1.1 МУ — догружаем с Cropwise."""
    if not rows:
        return False
    has_hourly = any(isinstance(r.get("covered_area_hourly"), list) and r["covered_area_hourly"] for r in rows)
    if has_hourly:
        return False
    return any(float(r.get("covered_area") or 0.0) > 0.5 for r in rows)


def _prefetch_entry_for_request(method: str, url: Any, params: Any) -> dict[str, Any] | None:
    if method.upper() != "GET":
        return None
    data = _load_prefetch_data()
    if data is None:
        return None
    endpoint = _endpoint_from_url(url)
    if not endpoint:
        return None
    if _env_flag(REPORT1_LIVE_MTFMI_ENV) and endpoint == "machine_task_field_mapping_items":
        return None
    # Сводка 2: не отдавать machine_tasks/mtfmi из prefetch — фильтры окна дают
    # неполный non-empty набор и обнуляют факт посева относительно локального прогона.
    if _env_flag(REPORT2_LIVE_API_ENV) and endpoint in {
        "machine_task_field_mapping_items",
        "machine_tasks",
    }:
        return None
    endpoints = data.get("endpoints") or {}
    rows = endpoints.get(endpoint)
    if rows is None:
        if _env_flag(PREFETCH_STRICT_ENV):
            raise CacheMissError(f"Missing endpoint in prefetch data: {endpoint}")
        return None
    if not isinstance(rows, list):
        return None
    filters = _parse_params(url, params)
    matched = _filtered_rows(endpoint, rows, filters)
    # Пустой prefetch «200 data:[]» блокирует догрузку с Cropwise → нули в посеве и МУ.
    # mtaomi: пустой снимок ломает культуру в сводке 14 (fallback на «Зябь» и т.п.).
    if not matched and endpoint in {
        "machine_task_field_mapping_items",
        "machine_tasks",
        "agro_operations",
        "history_items",
        "machine_task_agro_operation_mapping_items",
    }:
        return None
    if endpoint == "machine_task_field_mapping_items" and _mtfmi_prefetch_rows_missing_hourly(matched):
        return None
    from_id = _as_int((filters.get("from_id") or ["0"])[-1]) or 0
    page = max(1, _as_int((filters.get("page") or ["1"])[-1]) or 1)
    limit = _as_int((filters.get("limit") or filters.get("per_page") or ["1000"])[-1]) or 1000
    matched = sorted(matched, key=lambda row: _as_int(row.get("id")) or 0)
    if "page" in filters:
        start = (page - 1) * limit
        chunk = matched[start:start + limit]
    else:
        chunk = [row for row in matched if (_as_int(row.get("id")) or 0) >= from_id][:limit]
    last_id = max((_as_int(row.get("id")) or 0 for row in chunk), default=0)
    body = {
        "data": chunk,
        "meta": {
            "response": {
                "obtained_records": len(chunk),
                "last_record_id": last_id,
            }
        },
    }
    return {
        "status_code": 200,
        "headers": {"content-type": "application/json"},
        "text": json.dumps(body, ensure_ascii=False),
        "url": str(url),
        "fetched_at": data.get("created_at") or _utc_now(),
    }


def _normalize_pairs(value: Any) -> list[tuple[str, str]]:
    if value is None:
        return []
    if isinstance(value, dict):
        items = value.items()
    else:
        items = value
    pairs: list[tuple[str, str]] = []
    try:
        for key, val in items:
            if val is None:
                continue
            if isinstance(val, (list, tuple, set)):
                for one in val:
                    pairs.append((str(key), str(one)))
            else:
                pairs.append((str(key), str(val)))
    except Exception:
        pairs.append(("_raw_params", str(value)))
    return sorted(pairs)


def _normalized_url_and_params(url: Any, params: Any) -> tuple[str, list[tuple[str, str]]]:
    url_str = str(url)
    parsed = urlsplit(url_str)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    param_pairs = _normalize_pairs(params)
    all_pairs = sorted((str(k), str(v)) for k, v in [*query_pairs, *param_pairs])
    normalized_query = urlencode(all_pairs, doseq=True)
    normalized_url = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, normalized_query, "")
    )
    return normalized_url, all_pairs


def _cache_key(method: str, url: Any, params: Any) -> tuple[str, dict[str, Any]]:
    normalized_url, normalized_params = _normalized_url_and_params(url, params)
    key_obj = {
        "method": method.upper(),
        "url": normalized_url,
        "params": normalized_params,
    }
    raw = json.dumps(key_obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), key_obj


class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self.fd, str(os.getpid()).encode("ascii", errors="ignore"))
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > LOCK_TIMEOUT_SECONDS:
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timeout waiting for cache lock: {self.path}")
                time.sleep(LOCK_POLL_SECONDS)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _empty_store() -> dict[str, Any]:
    return {
        "version": 1,
        "created_at": _utc_now(),
        "storage": "entry-files-v1",
        "entries": {},
    }


def _entry_root(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".entries")


def _entry_path(path: Path, key_hash: str) -> Path:
    return _entry_root(path) / key_hash[:2] / f"{key_hash}.json"


def _read_store_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = _empty_store()
    if not isinstance(data, dict):
        data = _empty_store()
    if not isinstance(data.get("entries"), dict):
        data["entries"] = {}
    data.setdefault("version", 1)
    data.setdefault("created_at", _utc_now())
    return data


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _ensure_manifest(path: Path) -> None:
    if path.exists():
        return
    try:
        _write_json_atomic(path, _empty_store())
    except FileExistsError:
        pass


def _load_entry(path: Path, key_hash: str) -> dict[str, Any] | None:
    entry_file = _entry_path(path, key_hash)
    if entry_file.exists():
        try:
            data = json.loads(entry_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    # Backward compatibility with older single-file snapshots.
    with _FileLock(path.with_suffix(path.suffix + ".store.lock")):
        return _read_store_unlocked(path)["entries"].get(key_hash)


def _save_entry(path: Path, key_hash: str, key_obj: dict[str, Any], entry: dict[str, Any]) -> None:
    _ensure_manifest(path)
    entry_file = _entry_path(path, key_hash)
    payload = {
        **entry,
        "key": key_obj,
        "cached_at": _utc_now(),
    }
    _write_json_atomic(entry_file, payload)


def _live_api_on_prefetch_miss(url: Any) -> bool:
    """При промахе prefetch — догрузка с Cropwise (посев, МУ, agro_operations)."""
    endpoint = _endpoint_from_url(url)
    return endpoint in {
        "machine_task_field_mapping_items",
        "machine_tasks",
        "agro_operations",
        "history_items",
        "machine_task_agro_operation_mapping_items",
    }


def _is_cacheable_get(method: str, url: Any, kwargs: dict[str, Any]) -> bool:
    if method.upper() != "GET":
        return False
    if kwargs.get("stream"):
        return False
    url_str = str(url)
    if CROPWISE_API_MARKER in url_str:
        return True
    base_url = os.environ.get("CROPWISE_BASE_URL", "").strip()
    if not base_url:
        return False
    try:
        base = urlsplit(base_url)
        current = urlsplit(url_str)
    except Exception:
        return False
    return bool(
        base.netloc
        and current.netloc == base.netloc
        and current.path.startswith(base.path.rstrip("/") + "/")
    )


def _entry_from_response(response: Any) -> dict[str, Any]:
    text = getattr(response, "text", "")
    headers = dict(getattr(response, "headers", {}) or {})
    return {
        "status_code": int(getattr(response, "status_code", 200)),
        "headers": headers,
        "text": text,
        "url": str(getattr(response, "url", "")),
        "fetched_at": _utc_now(),
    }


def _should_cache_response(response: Any) -> bool:
    try:
        return int(getattr(response, "status_code", 0)) < 400
    except Exception:
        return False


class _CachedRequestsResponse:
    def __init__(self, entry: dict[str, Any]):
        self.status_code = int(entry.get("status_code", 200))
        self.headers = entry.get("headers") or {}
        self.text = entry.get("text") or ""
        self.content = self.text.encode("utf-8")
        self.url = entry.get("url") or ""
        self.encoding = "utf-8"
        self.reason = "OK" if self.status_code < 400 else "Cached HTTP error"

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self, **kwargs: Any) -> Any:
        return json.loads(self.text, **kwargs)

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        import requests

        raise requests.HTTPError(
            f"{self.status_code} Error for cached url: {self.url}",
            response=self,
        )


class _CachedHttpxResponse:
    def __init__(self, entry: dict[str, Any]):
        import httpx

        self.status_code = int(entry.get("status_code", 200))
        self.headers = entry.get("headers") or {}
        self.text = entry.get("text") or ""
        self.content = self.text.encode("utf-8")
        self.url = httpx.URL(entry.get("url") or "https://operations.cropwise.com")
        self.request = httpx.Request("GET", self.url)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self, **kwargs: Any) -> Any:
        return json.loads(self.text, **kwargs)

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        import httpx

        raise httpx.HTTPStatusError(
            f"{self.status_code} Error for cached url: {self.url}",
            request=self.request,
            response=self,
        )


def _cached_or_fetch(
    method: str,
    url: Any,
    params: Any,
    fetch: Callable[[], Any],
    wrap_cached: Callable[[dict[str, Any]], Any],
) -> Any:
    path = _cache_path()
    if path is None:
        return fetch()
    key_hash, key_obj = _cache_key(method, url, params)
    cached = _load_entry(path, key_hash)
    if cached is not None:
        return wrap_cached(cached)
    if _env_flag(READ_ONLY_ENV):
        raise CacheMissError(f"Missing Cropwise API snapshot entry: {key_obj['url']}")

    with _FileLock(path.with_suffix(path.suffix + f".{key_hash}.lock")):
        cached = _load_entry(path, key_hash)
        if cached is not None:
            return wrap_cached(cached)
        if _env_flag(READ_ONLY_ENV):
            raise CacheMissError(f"Missing Cropwise API snapshot entry: {key_obj['url']}")
        response = _fetch_requests_with_retry(fetch)
        if _should_cache_response(response):
            _save_entry(path, key_hash, key_obj, _entry_from_response(response))
        return response


def _install_requests_cache() -> None:
    try:
        import requests
    except Exception:
        return

    original_request = requests.sessions.Session.request

    def cached_request(self, method: str, url: Any, **kwargs: Any) -> Any:
        prefetched = _prefetch_entry_for_request(method, url, kwargs.get("params"))
        if prefetched is not None:
            return _CachedRequestsResponse(prefetched)
        if _live_api_on_prefetch_miss(url):
            return _fetch_requests_with_retry(
                lambda: original_request(self, method, url, **kwargs)
            )
        if not _is_cacheable_get(method, url, kwargs):
            return original_request(self, method, url, **kwargs)
        return _cached_or_fetch(
            method,
            url,
            kwargs.get("params"),
            lambda: original_request(self, method, url, **kwargs),
            _CachedRequestsResponse,
        )

    if getattr(requests.sessions.Session.request, "_svodki_cache_installed", False):
        return
    cached_request._svodki_cache_installed = True  # type: ignore[attr-defined]
    requests.sessions.Session.request = cached_request


def _install_httpx_cache() -> None:
    try:
        import httpx
    except Exception:
        return

    original_request = httpx.AsyncClient.request

    async def cached_request(self, method: str, url: Any, **kwargs: Any) -> Any:
        prefetched = _prefetch_entry_for_request(method, url, kwargs.get("params"))
        if prefetched is not None:
            return _CachedHttpxResponse(prefetched)
        if _live_api_on_prefetch_miss(url):
            return await _fetch_httpx_with_retry(
                lambda: original_request(self, method, url, **kwargs)
            )
        if not _is_cacheable_get(method, url, kwargs):
            return await original_request(self, method, url, **kwargs)
        path = _cache_path()
        if path is None:
            return await original_request(self, method, url, **kwargs)
        key_hash, key_obj = _cache_key(method, url, kwargs.get("params"))
        cached = _load_entry(path, key_hash)
        if cached is not None:
            return _CachedHttpxResponse(cached)
        if _env_flag(READ_ONLY_ENV):
            raise CacheMissError(f"Missing Cropwise API snapshot entry: {key_obj['url']}")
        with _FileLock(path.with_suffix(path.suffix + f".{key_hash}.lock")):
            cached = _load_entry(path, key_hash)
            if cached is not None:
                return _CachedHttpxResponse(cached)
            if _env_flag(READ_ONLY_ENV):
                raise CacheMissError(f"Missing Cropwise API snapshot entry: {key_obj['url']}")
            response = await _fetch_httpx_with_retry(
                lambda: original_request(self, method, url, **kwargs)
            )
            if _should_cache_response(response):
                _save_entry(path, key_hash, key_obj, _entry_from_response(response))
            return response

    if getattr(httpx.AsyncClient.request, "_svodki_cache_installed", False):
        return
    cached_request._svodki_cache_installed = True  # type: ignore[attr-defined]
    httpx.AsyncClient.request = cached_request


def _install_prefetch_only_mode() -> None:
    if not _env_flag(PREFETCH_ONLY_ENV):
        return
    try:
        from openpyxl.workbook.workbook import Workbook
    except Exception:
        return

    if getattr(Workbook.save, "_svodki_prefetch_installed", False):
        return

    def save_noop(self, filename: Any) -> None:
        print(f"[prefetch] Excel save skipped: {filename}", flush=True)

    save_noop._svodki_prefetch_installed = True  # type: ignore[attr-defined]
    Workbook.save = save_noop


def want_svodki_api_hooks() -> bool:
    """Нужны ли перехватчики (кэш или prefetch), с учётом отключения."""
    if _env_flag(DISABLE_API_CACHE_ENV):
        return False
    if _cache_path() is not None:
        return True
    return bool(os.environ.get(PREFETCH_DATA_ENV, "").strip())


def install_svodki_api_hooks() -> None:
    """Подключить ответы из prefetch/API-кэша к requests и httpx.

    Безопасно вызывать повторно. Имеет смысл перед импортом ranis / plan-скриптов,
    если при старте Python каталог со ``sitecustomize`` не был в ``PYTHONPATH``.
    """
    if not want_svodki_api_hooks():
        return
    _install_requests_cache()
    _install_httpx_cache()
    _install_prefetch_only_mode()


# --- Кэш справочников в хранилище (utils/shared/svodki_ref_store.py) --------------------------
def _ref_full_fetch_sync(call: Callable[[dict], Any], req: dict) -> list[dict] | None:
    """Выгрузить полный список курсором. call(params) -> response. None — ответ не как у API."""
    from svodki_ref_store import PAGE_MAX, extract_rows  # type: ignore[import-not-found]

    rows: list[dict] = []
    from_id = 0
    for _ in range(2000):
        resp = _fetch_requests_with_retry(lambda: call({**dict(req["extra"]), "from_id": from_id, "limit": PAGE_MAX}))
        if int(getattr(resp, "status_code", 0)) >= 400:
            return None
        body = resp.json()
        chunk = extract_rows(body)
        if not chunk:
            return rows
        rows.extend(chunk)
        meta = (body.get("meta") or {}).get("response") or {}
        last = meta.get("last_record_id")
        if not last or int(meta.get("obtained_records") or len(chunk)) < 100:
            return rows
        from_id = int(last) + 1
    return None


async def _ref_full_fetch_async(call: Callable[[dict], Any], req: dict) -> list[dict] | None:
    from svodki_ref_store import PAGE_MAX, extract_rows  # type: ignore[import-not-found]

    rows: list[dict] = []
    from_id = 0
    for _ in range(2000):
        resp = await _fetch_httpx_with_retry(lambda: call({**dict(req["extra"]), "from_id": from_id, "limit": PAGE_MAX}))
        if int(getattr(resp, "status_code", 0)) >= 400:
            return None
        body = resp.json()
        chunk = extract_rows(body)
        if not chunk:
            return rows
        rows.extend(chunk)
        meta = (body.get("meta") or {}).get("response") or {}
        last = meta.get("last_record_id")
        if not last or int(meta.get("obtained_records") or len(chunk)) < 100:
            return rows
        from_id = int(last) + 1
    return None


def _install_ref_cache() -> None:
    """Самый внутренний слой: prefetch/API-кэш выше по стеку имеют приоритет."""
    if not os.environ.get("SVODKI_TASK_STORE_DIR", "").strip():
        return
    if os.environ.get("SVODKI_REF_CACHE_DISABLE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    try:
        import requests
        import svodki_ref_store as rs
    except Exception:
        return

    orig_sync = requests.sessions.Session.request

    def ref_request(self, method: str, url: Any, **kwargs: Any) -> Any:
        try:
            req = rs.parse_request(method, url, kwargs.get("params")) if not kwargs.get("stream") else None
            if req is not None:
                got = rs.load(req)
                if got is None:
                    with rs.key_lock(req):
                        got = rs.load(req)
                        if got is None:
                            base_kw = {k: v for k, v in kwargs.items() if k != "params"}
                            rows = _ref_full_fetch_sync(
                                lambda p: orig_sync(self, "GET", req["url"], params=p, **base_kw), req)
                            if rows is not None:
                                rs.store(req, rows)
                                got = rs.load(req)
                if got is not None:
                    return _CachedRequestsResponse(rs.page_entry(req, got[0], got[1]))
        except Exception as exc:  # кэш — оптимизация: при любой ошибке идём в API
            print(f"[ref-cache] пропуск: {exc}", flush=True)
        return orig_sync(self, method, url, **kwargs)

    if not getattr(requests.sessions.Session.request, "_svodki_ref_installed", False):
        ref_request._svodki_ref_installed = True  # type: ignore[attr-defined]
        requests.sessions.Session.request = ref_request

    try:
        import httpx
    except Exception:
        return
    orig_async = httpx.AsyncClient.request

    async def ref_arequest(self, method: str, url: Any, **kwargs: Any) -> Any:
        try:
            req = rs.parse_request(method, url, kwargs.get("params")) if not kwargs.get("stream") else None
            if req is not None:
                got = rs.load(req)
                if got is None:
                    # flock блокирующий: выгрузку делаем в потоке-обёртке нельзя (нужен event loop),
                    # поэтому замок берём неблокирующе в отдельном потоке.
                    import asyncio

                    def _lock_enter():
                        cm = rs.key_lock(req); cm.__enter__(); return cm

                    cm = await asyncio.to_thread(_lock_enter)
                    try:
                        got = rs.load(req)
                        if got is None:
                            base_kw = {k: v for k, v in kwargs.items() if k != "params"}
                            rows = await _ref_full_fetch_async(
                                lambda p: orig_async(self, "GET", req["url"], params=p, **base_kw), req)
                            if rows is not None:
                                rs.store(req, rows)
                                got = rs.load(req)
                    finally:
                        cm.__exit__(None, None, None)
                if got is not None:
                    return _CachedHttpxResponse(rs.page_entry(req, got[0], got[1]))
        except Exception as exc:
            print(f"[ref-cache] пропуск: {exc}", flush=True)
        return await orig_async(self, method, url, **kwargs)

    if not getattr(httpx.AsyncClient.request, "_svodki_ref_installed", False):
        ref_arequest._svodki_ref_installed = True  # type: ignore[attr-defined]
        httpx.AsyncClient.request = ref_arequest


try:
    _install_ref_cache()
except Exception:  # pragma: no cover
    pass


if want_svodki_api_hooks():
    install_svodki_api_hooks()
