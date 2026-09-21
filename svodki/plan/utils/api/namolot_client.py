# -*- coding: utf-8 -*-
"""HTTP-клиент Namolot (1С) с fallback на JSON-кэш на сервере."""
from __future__ import annotations

import base64
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import requests

NAMOLOT_BASE_URL = (
    (os.environ.get("NAMOLOT_BASE_URL") or "").strip()
    or "http://10.0.1.5/hpp_elevator_new/hs/ground/report"
)
NAMOLOT_USERNAME = (os.environ.get("NAMOLOT_USERNAME") or "Сводка").strip()
NAMOLOT_PASSWORD = (os.environ.get("NAMOLOT_PASSWORD") or "3VzLqA6S").strip()

TZ_MSK = timezone(timedelta(hours=3))
_SHIFT_HOUR = int(os.environ.get("NAMOLOT_SHIFT_HOUR", "6") or 6)

_DATE_ONLY_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_ISO_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _namolot_auth_header() -> dict:
    user = (os.environ.get("NAMOLOT_USERNAME") or "").strip() or NAMOLOT_USERNAME
    pwd = (os.environ.get("NAMOLOT_PASSWORD") or "").strip() or NAMOLOT_PASSWORD
    if not (user and pwd):
        return {}
    creds = f"{user}:{pwd}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(creds).decode("ascii")}


def _cache_path() -> Path | None:
    raw = (os.environ.get("NAMOLOT_CACHE_FILE") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_file() else None
    upload = (os.environ.get("UPLOAD_DIR") or "uploads").strip()
    p = Path(upload) / ".namolot_cache" / "records.json"
    return p if p.is_file() else None


def _prefer_cache() -> bool:
    """На сервере с залитым кэшем не блокируем генерацию ожиданием live API."""
    flag = (os.environ.get("NAMOLOT_PREFER_CACHE") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return _cache_path() is not None


def _connect_timeout_sec() -> float:
    raw = (os.environ.get("NAMOLOT_CONNECT_TIMEOUT_SEC") or "10").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 10.0


def _read_timeout_sec(*, supplement: bool = False) -> float:
    if supplement:
        raw = (
            os.environ.get("NAMOLOT_SUPPLEMENT_READ_TIMEOUT_SEC")
            or os.environ.get("NAMOLOT_SUPPLEMENT_TIMEOUT_SEC")
            or os.environ.get("NAMOLOT_READ_TIMEOUT_SEC")
            or "20"
        ).strip()
    else:
        raw = (
            os.environ.get("NAMOLOT_READ_TIMEOUT_SEC")
            or os.environ.get("NAMOLOT_LIVE_TIMEOUT_SEC")
            or "30"
        ).strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 20.0 if supplement else 30.0


def _request_timeout(*, supplement: bool = False) -> tuple[float, float]:
    """(connect, read) — быстрый fail если 10.0.1.5 недоступен, read длиннее для живого 1С."""
    return _connect_timeout_sec(), _read_timeout_sec(supplement=supplement)


def _parse_record_datetime(value) -> datetime | None:
    """Разбор поля date записи Namolot (с TZ_MSK), совместим с 10_main_harvest."""
    if value is None or value == "" or value == 0:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.lstrip("-").isdigit():
            return _parse_record_datetime(int(stripped))
        s = stripped
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt: datetime | None = None
        date_only = False
        try:
            dt = datetime.fromisoformat(s)
            if "T" not in s and " " not in s.strip() and len(s.strip()) <= 10:
                date_only = True
        except ValueError:
            for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
                try:
                    dt = datetime.strptime(s, fmt)
                    date_only = fmt == "%d.%m.%Y"
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
        if date_only:
            dt = dt.replace(hour=12, minute=0, second=0, microsecond=0)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_MSK)
        else:
            dt = dt.astimezone(TZ_MSK)
        return dt
    if isinstance(value, (int, float)):
        ts = value / 1000 if abs(value) > 10 ** 12 else value
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except (ValueError, OSError, OverflowError):
            return None
    return None


def _shift_cutoff(date_end: date) -> datetime:
    """Конец смены для REPORT_DATE: 06:00 date_end по MSK."""
    return datetime.combine(date_end, time(_SHIFT_HOUR, 0), tzinfo=TZ_MSK)


def _load_cache_payload() -> tuple[list[dict], dict, Path | None]:
    path = _cache_path()
    if path is None:
        return [], {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  [namolot] ошибка чтения кэша {path}: {e}", flush=True)
        return [], {}, path
    if isinstance(payload, dict):
        records = payload.get("records")
        meta = payload
    else:
        records = payload
        meta = {}
    if not isinstance(records, list):
        print(f"  [namolot] неверный формат кэша {path}", flush=True)
        return [], meta if isinstance(meta, dict) else {}, path
    out = [rec for rec in records if isinstance(rec, dict)]
    fetched = meta.get("fetched_at") if isinstance(meta, dict) else None
    print(
        f"  [namolot] кэш {path.name}: {len(out)} записей"
        + (f" (обновлён {fetched})" if fetched else ""),
        flush=True,
    )
    return out, meta if isinstance(meta, dict) else {}, path


def _load_cache() -> list[dict]:
    records, _meta, _path = _load_cache_payload()
    return records


def _max_record_datetime(records: list[dict]) -> datetime | None:
    max_dt: datetime | None = None
    for rec in records:
        rec_dt = _parse_record_datetime(rec.get("date"))
        if rec_dt is None:
            continue
        if max_dt is None or rec_dt > max_dt:
            max_dt = rec_dt
    return max_dt


def _cache_covers_shift(records: list[dict], meta: dict, date_end: date) -> bool:
    """Кэш достаточен для смены, заканчивающейся date_end 06:00 (без live API).

    Смотрим только на фактическое время последней записи. stamped fetched_at
    больше не считается достаточным — иначе утренний срез «на дату» считался
    полным днём и сайт показывал 114+367 вместо ~9500 т.
    """
    if not records:
        return False
    cutoff = _shift_cutoff(date_end)
    max_dt = _max_record_datetime(records)
    if max_dt is None:
        return False
    return max_dt >= cutoff - timedelta(hours=2)


def _record_calendar_date(value) -> date | None:
    """Календарная дата записи Namolot (для фильтра кэша по периоду запроса)."""
    rec_dt = _parse_record_datetime(value)
    if rec_dt is not None:
        return rec_dt.date()
    if value is None or value == "" or value == 0:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if _DATE_ONLY_RE.match(s):
            try:
                return datetime.strptime(s, "%d.%m.%Y").date()
            except ValueError:
                return None
        if _ISO_DATE_ONLY_RE.match(s):
            try:
                return date.fromisoformat(s)
            except ValueError:
                return None
    return None


def _filter_records_by_period(
    records: list[dict],
    date_begin: date,
    date_end: date,
) -> list[dict]:
    out: list[dict] = []
    for rec in records:
        rec_date = _record_calendar_date(rec.get("date"))
        if rec_date is None or date_begin <= rec_date <= date_end:
            out.append(rec)
    if len(out) != len(records):
        print(
            f"  [namolot] кэш отфильтрован по периоду "
            f"{date_begin}..{date_end}: {len(out)} из {len(records)}",
            flush=True,
        )
    return out


def _record_dedupe_key(rec: dict) -> tuple[str, ...]:
    return (
        str(rec.get("spp_guid") or rec.get("spp") or ""),
        str(rec.get("date") or ""),
        str(rec.get("quantity") or ""),
        str(rec.get("combine_harvester_guid") or rec.get("combine_harvester_id") or ""),
    )


def _merge_records(existing: list[dict], extra: list[dict]) -> list[dict]:
    seen = {_record_dedupe_key(rec) for rec in existing}
    merged = list(existing)
    added = 0
    for rec in extra:
        if not isinstance(rec, dict):
            continue
        key = _record_dedupe_key(rec)
        if key in seen:
            continue
        seen.add(key)
        merged.append(rec)
        added += 1
    if added:
        print(
            f"  [namolot] кэш дополнен: +{added} записей (всего {len(merged)})",
            flush=True,
        )
    return merged


def _persist_cache(
    path: Path,
    records: list[dict],
    *,
    date_begin: date,
    date_end: date,
    meta: dict | None = None,
) -> None:
    if (os.environ.get("NAMOLOT_CACHE_READONLY") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return
    payload = dict(meta or {})
    payload.update(
        {
            "fetched_at": date_end.isoformat(),
            "date_begin": date_begin.isoformat(),
            "date_end": date_end.isoformat(),
            "records": records,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        print(
            f"  [namolot] кэш обновлён на диске: {path} ({len(records)} записей)",
            flush=True,
        )
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"  [namolot] не удалось записать кэш {path}: {exc}", flush=True)


def _fetch_live(
    date_begin: date,
    date_end: date,
    *,
    supplement: bool = False,
) -> list[dict]:
    base = (os.environ.get("NAMOLOT_BASE_URL") or "").strip() or NAMOLOT_BASE_URL
    params = {"date_begin": date_begin.isoformat(), "date_end": date_end.isoformat()}
    timeout = _request_timeout(supplement=supplement)
    try:
        resp = requests.get(
            base,
            params=params,
            headers=_namolot_auth_header(),
            timeout=timeout,
            proxies={"http": None, "https": None},
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            print(
                f"  [namolot] live API: {len(data)} записей "
                f"{date_begin}..{date_end} (connect {timeout[0]:g}s, read {timeout[1]:g}s)",
                flush=True,
            )
            return data
        if not isinstance(data, list):
            print(f"  [namolot] неожиданный формат ответа: {type(data)}", flush=True)
    except Exception as e:
        print(
            f"  [namolot] live API недоступен {date_begin}..{date_end} "
            f"(connect {timeout[0]:g}s, read {timeout[1]:g}s): {e}",
            flush=True,
        )
    return []


def _ensure_fresh_records(
    cached: list[dict],
    meta: dict,
    cache_path: Path | None,
    date_begin: date,
    date_end: date,
) -> list[dict]:
    """Дополняет устаревший кэш коротким live-запросом; свежий кэш не трогает."""
    if _cache_covers_shift(cached, meta, date_end):
        return cached

    max_dt = _max_record_datetime(cached)
    cutoff = _shift_cutoff(date_end)
    print(
        f"  [namolot] кэш устарел для смены до {cutoff.strftime('%d.%m %H:%M')}"
        + (f" (последняя запись {max_dt.strftime('%d.%m %H:%M')})" if max_dt else "")
        + " — пробуем live API…",
        flush=True,
    )

    if max_dt is not None:
        inc_begin = max(max_dt.date() - timedelta(days=1), date_begin)
    else:
        inc_begin = date_begin

    live = _fetch_live(inc_begin, date_end, supplement=True)
    if not live and not cached:
        live = _fetch_live(date_begin, date_end, supplement=False)

    if live:
        merged = _merge_records(cached, live)
        if cache_path is not None:
            old_begin = meta.get("date_begin")
            try:
                stored_begin = date.fromisoformat(str(old_begin)[:10]) if old_begin else date_begin
            except ValueError:
                stored_begin = date_begin
            _persist_cache(
                cache_path,
                merged,
                date_begin=min(stored_begin, date_begin),
                date_end=date_end,
                meta=meta,
            )
        return merged

    if cached:
        allow = (os.environ.get("NAMOLOT_ALLOW_STALE") or "0").strip().lower() in (
            "1", "true", "yes", "on",
        )
        if allow:
            print(
                "  [namolot] ВНИМАНИЕ: live 1С недоступен — ALLOW_STALE=1, "
                "берём устаревший кэш (тонны могут быть занижены)",
                flush=True,
            )
            return cached
        print(
            "  [namolot] live 1С недоступен и кэш не покрывает смену — "
            "не отдаём устаревший кэш (как локальный скрипт: нужен прямой "
            "запрос к 10.0.1.5 или sync_namolot_cache_to_server.sh с ПК)",
            flush=True,
        )
        return []
    return []



def refresh_namolot_cache(
    date_begin: date | None = None,
    date_end: date | None = None,
    *,
    force: bool = False,
) -> dict:
    """Тянет live Namolot и пишет кэш (для cron / pre-generate).

    Env: NAMOLOT_AUTO_REFRESH=0 — не вызывать извне; force=True игнорирует covers.
    Возвращает dict: ok, records, added, error, cache_path.
    """
    today = date.today()
    if date_end is None:
        date_end = today
    if date_begin is None:
        # сезон уборки с марта
        date_begin = date(date_end.year, 3, 1)
        if date_end < date_begin:
            date_begin = date(date_end.year - 1, 3, 1)

    cached, meta, cache_path = _load_cache_payload()
    if not force and cached and _cache_covers_shift(cached, meta, date_end):
        return {
            "ok": True,
            "skipped": True,
            "records": len(cached),
            "added": 0,
            "cache_path": str(cache_path) if cache_path else None,
            "error": None,
        }

    live = _fetch_live(date_begin, date_end, supplement=True)
    if not live:
        live = _fetch_live(date_begin, date_end, supplement=False)
    if not live:
        return {
            "ok": False,
            "skipped": False,
            "records": len(cached),
            "added": 0,
            "cache_path": str(cache_path) if cache_path else None,
            "error": "live API unavailable",
        }

    before = len(cached)
    merged = _merge_records(cached, live) if cached else list(live)
    if cache_path is not None:
        _persist_cache(
            cache_path,
            merged,
            date_begin=date_begin,
            date_end=date_end,
            meta=meta,
        )
    print(
        f"  [namolot] AUTO_REFRESH: кэш обновлён +{len(merged) - before} "
        f"(всего {len(merged)}) → {cache_path}",
        flush=True,
    )
    return {
        "ok": True,
        "skipped": False,
        "records": len(merged),
        "added": max(0, len(merged) - before),
        "cache_path": str(cache_path) if cache_path else None,
        "error": None,
    }


def fetch_namolot_records(date_begin: date, date_end: date) -> list[dict]:
    """Сырые записи «Намolot»: свежий кэш (быстро) или догрузка через live API."""
    cached, meta, cache_path = _load_cache_payload()

    if cached and (_prefer_cache() or cache_path is not None):
        records = _ensure_fresh_records(cached, meta, cache_path, date_begin, date_end)
        if records:
            return _filter_records_by_period(records, date_begin, date_end)

    live = _fetch_live(date_begin, date_end)
    if live:
        if cache_path is not None:
            merged = _merge_records(cached, live)
            _persist_cache(cache_path, merged, date_begin=date_begin, date_end=date_end, meta=meta)
        return live

    # Кэш без покрытия смены: всё равно отдаём (нужен сезон), но не притворяемся
    # что сутки полные — это ловит 10_main_harvest (incomplete_day).
    if cached:
        if not _cache_covers_shift(cached, meta, date_end):
            print(
                "  [namolot] отдаём кэш без покрытия смены (сезон OK, сутки неполные)",
                flush=True,
            )
        return _filter_records_by_period(cached, date_begin, date_end)

    print(
        "  [namolot] нет данных — залейте кэш: "
        "REMOTE_BASES='/home/razrab1/disk /home/razrab1/work disk' "
        "bash deploy/sync_namolot_cache_to_server.sh",
        flush=True,
    )
    return []
