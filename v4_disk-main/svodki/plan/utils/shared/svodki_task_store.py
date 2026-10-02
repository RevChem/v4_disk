# -*- coding: utf-8 -*-
"""Постоянное инкрементальное хранилище machine_tasks и mtfmi между запусками сводок.

Зачем. Каждая сводка (и каждый запуск пачки) заново качает один и тот же сезон:
~70 тыс. заданий и их mtfmi (десятки секунд). Cropwise умеет отдавать «что изменилось»
(фильтр ``updated_at_gt_eq`` у machine_tasks и machine_task_field_mapping_items, см.
документацию API v3), поэтому сезон качается один раз, дальше докачиваются только изменения.

Хранилище — одна база SQLite (``store.sqlite``, режим WAL) вместо двух pickle-файлов:
процесс сводки читает только свой тип работ и своё окно дат и не держит сезон в памяти.
Хранятся только нужные поля (``svodki_task_store_fields.py``, генерируется
``tools/gen_store_fields.py``; ``SVODKI_STORE_FIELDS=all`` — хранить всё), полный исходный
JSON лежит отдельной сжатой колонкой ``raw`` у заданий и у строк площади (читатели её не грузят; из него база
пересобирается командой ``python svodki_task_store.py rebuild`` без обращения к API).
Суточные факты (v4): при записи mtfmi считаются таблицы ``mtfmi_fact`` (строка площади) и
``mtfmi_daily`` (площадь по сменным дням [D 06:00, D+1 06:00) из covered_area_hourly) — те же
числа, что даёт ``task_area_shared.area_ha_by_shift_date``. ``TaskStore.daily_facts()`` отдаёт
агрегаты по (тип работ, машина, поле, день) одним SQL-запросом без загрузки заданий и строк;
``tools/verify_daily_facts.py`` сверяет таблицу с прямым расчётом из raw на реальных данных.
``SVODKI_STORE_READONLY=1`` — режим сводок: если покрытие есть, в API не ходим, обновляет
хранилище бэкенд (``tools/sync_task_store.py``) один раз перед пачкой.

Включение: ``SVODKI_TASK_STORE_DIR=/путь`` (без переменной — всё работает по-старому).
Подходит и для пачки, и для одиночных запусков: хранилище общее для процессов
(межпроцессная блокировка flock; второй процесс ждёт первого и берёт свежее).

Что гарантируется и что нет.
  * Правки заданий и строк mtfmi (updated_at) подхватываются при каждой синхронизации.
  * Задания последних ``SVODKI_STORE_RECENT_DAYS`` (по умолчанию 1) суток и их mtfmi
    перечитываются целиком при каждой синхронизации: ловит позднюю пересчитанную площадь,
    о которой ``updated_at`` не говорит (1 сутки при доступном /versions, иначе 3).
  * Удаления заданий видны в журнале /versions (event=destroy) при каждой синхронизации;
    раз в ``SVODKI_STORE_FULL_REFRESH_HOURS`` (24) выполняется сверка со списком /ids
    (один запрос, без перекачки сезона), раз в ``SVODKI_STORE_HARD_REFRESH_HOURS`` (168) или
    если /ids недоступен — полный пересбор.
  * Данные не старше ``SVODKI_STORE_MAX_AGE_SEC`` (по умолчанию 90 с) считаются свежими
    без обращения к API: подряд идущие сводки пачки синхронизируют хранилище один раз.

Окно времени без часового пояса трактуется как МСК (``SVODKI_STORE_TZ_HOURS``, по
умолчанию 3) — так же, как формируются границы во всех сводках.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import threading
import zlib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

try:  # Linux-сервер; на Windows хранилище работает без межпроцессной блокировки
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

STORE_VERSION = 4  # 4 = + суточные факты (mtfmi_fact/mtfmi_daily); 3 = SQLite + raw; 1 = pickle
SHIFT_START_HOUR = 6  # сменный день [D 06:00, D+1 06:00) — как task_area_shared._shift_date_from_dt
_ISO = "%Y-%m-%dT%H:%M:%S"
ELIGIBLE_KEYS = {"end_time_gt_eq", "end_time_lt", "season"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def store_dir() -> Path | None:
    raw = (os.environ.get("SVODKI_TASK_STORE_DIR") or "").strip()
    if not raw or (os.environ.get("SVODKI_TASK_STORE_DISABLE") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return None
    return Path(raw)


def _tz() -> timezone:
    return timezone(timedelta(hours=_env_float("SVODKI_STORE_TZ_HOURS", 3)))


def parse_ts(raw: Any) -> float | None:
    """ISO-строка → epoch; время без пояса — в поясе аккаунта (МСК)."""
    if raw is None:
        return None
    s = str(raw).strip().replace(" ", "T")
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz())
    return dt.timestamp()


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=_tz()).strftime(_ISO)


def eligible(params: dict | None) -> bool:
    """Запрос целиком выражается окном end_time (+season) — его можно отдать из хранилища."""
    p = params or {}
    return (
        bool(p.get("end_time_gt_eq"))
        and bool(p.get("end_time_lt"))
        and set(p) <= ELIGIBLE_KEYS
    )


def _truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


_ALWAYS = {"id", "work_type_id", "end_time", "season", "updated_at", "machine_task_id"}


def _load_keep() -> frozenset[str] | None:
    """Белый список полей. None — хранить всё (нет сгенерированного списка или SVODKI_STORE_FIELDS=all)."""
    if (os.environ.get("SVODKI_STORE_FIELDS") or "").strip().lower() == "all":
        return None
    try:
        from svodki_task_store_fields import FIELDS
    except ImportError:
        return None
    return frozenset(FIELDS) | _ALWAYS


_KEEP = _load_keep()


def _trim(row: dict) -> dict:
    if _KEEP is None:
        return row
    return {k: v for k, v in row.items() if k in _KEEP or k.startswith("x_custom")}


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _raw(obj: Any) -> bytes | None:
    """Полный исходный JSON, сжатый (None — SVODKI_STORE_NO_RAW=1)."""
    if _truthy("SVODKI_STORE_NO_RAW"):
        return None
    return zlib.compress(_dumps(obj).encode("utf-8"), 3)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS cov (wt INTEGER PRIMARY KEY, lo REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, wt INTEGER NOT NULL, end_ts REAL NOT NULL,
    season TEXT, data TEXT NOT NULL, raw BLOB, machine_id INTEGER);
CREATE INDEX IF NOT EXISTS tasks_wt_end ON tasks (wt, end_ts);
CREATE INDEX IF NOT EXISTS tasks_end ON tasks (end_ts);
CREATE TABLE IF NOT EXISTS mtfmi_tasks (task_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS mtfmi (
    rid INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, data TEXT NOT NULL, raw BLOB);
CREATE INDEX IF NOT EXISTS mtfmi_task ON mtfmi (task_id);
CREATE TABLE IF NOT EXISTS history_crop (hid INTEGER PRIMARY KEY, crop TEXT);
-- Суточные факты: одна строка на строку площади (mtfmi_fact) и на её сменный день (mtfmi_daily).
-- Считаются при записи mtfmi из ПОЛНОГО JSON (до обрезки полей) и всегда совпадают с
-- task_area_shared.area_ha_by_shift_date / covered_area.
CREATE TABLE IF NOT EXISTS mtfmi_fact (
    rid INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, field_id INTEGER,
    covered_area REAL NOT NULL DEFAULT 0, hourly_n INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS mtfmi_fact_task ON mtfmi_fact (task_id);
CREATE TABLE IF NOT EXISTS mtfmi_daily (
    rid INTEGER NOT NULL, shift_date TEXT NOT NULL, ha REAL NOT NULL,
    PRIMARY KEY (rid, shift_date)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS mtfmi_daily_date ON mtfmi_daily (shift_date);
"""
def _hour_dt(raw: Any) -> datetime | None:
    """Метка hourly → datetime в поясе аккаунта (как parse_dt сводок: без пояса = МСК, число = epoch)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        t = float(raw)
        if t > 1e12:
            t /= 1000.0
        try:
            return datetime.fromtimestamp(t, tz=_tz())
        except (OSError, ValueError, OverflowError):
            return None
    ts = parse_ts(raw)
    return None if ts is None else datetime.fromtimestamp(ts, tz=_tz())


def shift_date_of(dt: datetime) -> str:
    """Сменный день ISO-строкой: до 06:00 — предыдущая календарная дата."""
    d = dt.date() if dt.hour >= SHIFT_START_HOUR else (dt - timedelta(days=1)).date()
    return d.isoformat()


def daily_portions(item: dict) -> dict[str, float]:
    """Площадь строки mtfmi по сменным датам из covered_area_hourly (ha<=0 пропускаются)."""
    out: dict[str, float] = {}
    for entry in item.get("covered_area_hourly") or []:
        try:
            h = _hour_dt(entry[0])
            ha = float(entry[1] or 0.0)
        except (TypeError, ValueError, IndexError):
            continue
        if h is None or ha <= 0:
            continue
        k = shift_date_of(h)
        out[k] = out.get(k, 0.0) + ha
    return out


_CHUNK = 500  # лимит переменных SQLite — 999 в старых сборках


def _chunks(items: list, n: int = _CHUNK):
    for i in range(0, len(items), n):
        yield items[i : i + n]


class TaskStore:
    def __init__(self, directory: Path, base_url: str) -> None:
        self.dir = directory
        self.base_url = base_url.rstrip("/")
        self.db_path = directory / "store.sqlite"
        self.lock_path = directory / "store.lock"
        # малое состояние в памяти (сами задания и mtfmi — только в базе)
        self.cov_lo: dict[int, float] = {}
        self.synced_ts = 0.0
        self.full_ts = 0.0
        self.hard_ts = 0.0
        self._fresh_until = 0.0
        self._crops_loaded = False
        self._tl = threading.local()
        self._locks: dict[int, asyncio.Lock] = {}
        self.versions_ok = True
        self.stats = {"live_tasks": 0, "delta_tasks": 0, "delta_mtfmi_tasks": 0}

    # ------------------------------------------------------------------ диск
    def _db(self) -> sqlite3.Connection:
        con = getattr(self._tl, "con", None)
        if con is not None:
            return con
        self.dir.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.db_path), timeout=120, isolation_level=None)
        con.execute("PRAGMA busy_timeout=120000")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA cache_size=-16384")  # 16 МБ на процесс
        con.executescript(_SCHEMA)
        row = con.execute("SELECT v FROM meta WHERE k='version'").fetchone()
        base = con.execute("SELECT v FROM meta WHERE k='base_url'").fetchone()
        if row is not None and int(row[0]) == 3 and not (base and base[0] != self.base_url):
            self._migrate_3_to_4(con)
            row = (str(STORE_VERSION),)
        if row is None or int(row[0]) != STORE_VERSION or (self.base_url and base and base[0] != self.base_url):
            con.execute("BEGIN IMMEDIATE")
            for tbl in ("meta", "cov", "tasks", "mtfmi_tasks", "mtfmi", "history_crop", "mtfmi_fact", "mtfmi_daily"):
                con.execute(f"DELETE FROM {tbl}")
            con.execute("INSERT INTO meta VALUES ('version', ?)", (str(STORE_VERSION),))
            con.execute("INSERT INTO meta VALUES ('base_url', ?)", (self.base_url,))
            con.execute("COMMIT")
        self._tl.con = con
        return con

    def _migrate_3_to_4(self, con: sqlite3.Connection) -> None:
        """v3 → v4 без перекачки: колонка tasks.machine_id и суточные факты из raw/data."""
        con.execute("BEGIN IMMEDIATE")
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
            if "machine_id" not in cols:
                con.execute("ALTER TABLE tasks ADD COLUMN machine_id INTEGER")
            self._tl.con = con
            self._rebuild_facts_tx(con)
            con.execute("UPDATE meta SET v=? WHERE k='version'", (str(STORE_VERSION),))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            self._tl.con = None

    def _rebuild_facts_tx(self, con: sqlite3.Connection) -> int:
        """Пересчитать machine_id заданий и суточные факты из raw (если нет raw — из data). В транзакции."""
        for tbl in ("mtfmi_fact", "mtfmi_daily"):
            con.execute(f"DELETE FROM {tbl}")
        for tid, raw, data in con.execute("SELECT id, raw, data FROM tasks").fetchall():
            t = json.loads(zlib.decompress(raw).decode("utf-8")) if raw else json.loads(data)
            con.execute("UPDATE tasks SET machine_id=? WHERE id=?", (_int_or_none(t.get("machine_id")), tid))
        n = 0
        for rid, tid, raw, data in con.execute("SELECT rid, task_id, raw, data FROM mtfmi").fetchall():
            r = json.loads(zlib.decompress(raw).decode("utf-8")) if raw else json.loads(data)
            self._put_fact(int(rid), int(tid), r)
            n += 1
        return n

    def rebuild_facts(self) -> int:
        """Пересчитать суточные факты из raw без обращения к API."""
        con = self._db()
        con.execute("BEGIN IMMEDIATE")
        try:
            n = self._rebuild_facts_tx(con)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        return n

    def daily_facts(self, wts, date_from: str, date_to: str, *, season: str | None = None,
                    by: str = "machine_field_day") -> list[tuple]:
        """Площадь по сменным дням [date_from, date_to) — агрегатом в SQL, без загрузки заданий/строк.

        by="machine_field_day": (wt, machine_id, field_id, shift_date, ha)
        by="machine_day":       (wt, machine_id, shift_date, ha)
        by="day":               (wt, shift_date, ha)
        Строки без hourly в daily не попадают — их площадь covered_area отдаёт ``flat_facts``.
        """
        wts = sorted({int(w) for w in wts})
        q = ",".join("?" * len(wts))
        cols = {
            "machine_field_day": ("t.wt, t.machine_id, f.field_id, d.shift_date", "t.wt, t.machine_id, f.field_id, d.shift_date"),
            "machine_day": ("t.wt, t.machine_id, d.shift_date", "t.wt, t.machine_id, d.shift_date"),
            "day": ("t.wt, d.shift_date", "t.wt, d.shift_date"),
        }[by]
        sql = (f"SELECT {cols[0]}, SUM(d.ha) FROM mtfmi_daily d JOIN mtfmi_fact f ON f.rid=d.rid "
               f"JOIN tasks t ON t.id=f.task_id WHERE t.wt IN ({q}) AND d.shift_date >= ? AND d.shift_date < ?")
        args: list[Any] = [*wts, date_from, date_to]
        if season is not None:
            sql += " AND t.season=?"
            args.append(str(season))
        sql += f" GROUP BY {cols[1]} ORDER BY {cols[1]}"
        return [tuple(r) for r in self._db().execute(sql, args)]

    def machine_day_area(self, wts, date_from: str, date_to: str, key_for_field, *,
                         season: str | None = None) -> dict[Any, dict[str, dict[int | None, float]]]:
        """Аналог machine_day_area сводки 2 для hourly-площади (уборочный путь area_ha_by_shift_date):
        {ключ: {сменная_дата: {machine_id: га}}}. key_for_field(field_id) → ключ (например, фирма) или
        None, чтобы пропустить поле. Даты — [date_from, date_to), ISO. Строки без hourly сюда не входят
        (их даёт flat_facts), как и в сводке: там они идут по дате задания.
        """
        out: dict[Any, dict[str, dict[int | None, float]]] = {}
        cache: dict[Any, Any] = {}
        for wt, mid, fid, day, ha in self.daily_facts(wts, date_from, date_to, season=season):
            if fid not in cache:
                cache[fid] = key_for_field(fid) if fid is not None else None
            k = cache[fid]
            if k is None or ha <= 0:
                continue
            d = out.setdefault(k, {}).setdefault(day, {})
            d[mid] = d.get(mid, 0.0) + ha
        return out

    def flat_facts(self, wts, ts_from: float, ts_to: float) -> list[tuple]:
        """Строки площади без hourly: (wt, machine_id, field_id, task_end_date, covered_area) по end_time задания."""
        wts = sorted({int(w) for w in wts})
        q = ",".join("?" * len(wts))
        out = []
        for wt, mid, fid, end_ts, cov in self._db().execute(
            f"SELECT t.wt, t.machine_id, f.field_id, t.end_ts, f.covered_area FROM mtfmi_fact f "
            f"JOIN tasks t ON t.id=f.task_id WHERE f.hourly_n=0 AND f.covered_area>0 AND t.wt IN ({q}) "
            f"AND t.end_ts>=? AND t.end_ts<?", [*wts, ts_from, ts_to]):
            out.append((wt, mid, fid, shift_date_of(datetime.fromtimestamp(end_ts, tz=_tz())), cov))
        return out

    def _load_state(self) -> None:
        con = self._db()
        self.cov_lo = {int(w): float(lo) for w, lo in con.execute("SELECT wt, lo FROM cov")}
        m = dict(con.execute("SELECT k, v FROM meta"))
        self.synced_ts = float(m.get("synced_ts", 0) or 0)
        self.full_ts = float(m.get("full_ts", 0) or 0)
        self.hard_ts = float(m.get("hard_ts", 0) or self.full_ts)

    def _save_state(self) -> None:
        con = self._db()
        con.executemany(
            "INSERT INTO meta VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            [("synced_ts", repr(self.synced_ts)), ("full_ts", repr(self.full_ts)),
             ("hard_ts", repr(self.hard_ts))],
        )
        con.execute("DELETE FROM cov")
        con.executemany("INSERT INTO cov VALUES (?, ?)", list(self.cov_lo.items()))

    # ------------------------------------------------------------ блокировки
    def _loop_lock(self) -> asyncio.Lock:
        key = id(asyncio.get_running_loop())
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    @contextlib.asynccontextmanager
    async def _xlock(self):
        """Единственный писатель: asyncio-замок (в процессе) + flock (между процессами) + транзакция."""
        async with self._loop_lock():
            self.dir.mkdir(parents=True, exist_ok=True)
            fh = open(self.lock_path, "a+")
            try:
                if fcntl is not None:
                    await asyncio.to_thread(fcntl.flock, fh, fcntl.LOCK_EX)
                con = self._db()
                con.execute("BEGIN IMMEDIATE")
                try:
                    self._load_state()
                    yield
                    self._save_state()
                except BaseException:
                    with contextlib.suppress(Exception):
                        con.execute("ROLLBACK")
                    self._load_state_quiet()
                    raise
                else:
                    con.execute("COMMIT")
            finally:
                if fcntl is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()

    def _load_state_quiet(self) -> None:
        """После отката вернуть состояние в памяти к тому, что в базе."""
        with contextlib.suppress(Exception):
            self._load_state()
            self._fresh_until = 0.0

    # ------------------------------------------------------------- мутации
    def _remove_task(self, tid: int) -> None:
        self._db().execute("DELETE FROM tasks WHERE id=?", (tid,))

    def _drop_mtfmi(self, tid: int) -> bool:
        con = self._db()
        had = con.execute("DELETE FROM mtfmi_tasks WHERE task_id=?", (tid,)).rowcount > 0
        self._drop_facts(tid)
        con.execute("DELETE FROM mtfmi WHERE task_id=?", (tid,))
        return had

    def _set_mtfmi(self, tid: int, rows: list[dict]) -> None:
        con = self._db()
        self._drop_facts(tid)
        con.execute("DELETE FROM mtfmi WHERE task_id=?", (tid,))
        for r in rows:
            cur = con.execute(
                "INSERT INTO mtfmi (task_id, data, raw) VALUES (?, ?, ?)",
                (tid, _dumps(_trim(r)), _raw(r)),
            )
            self._put_fact(int(cur.lastrowid), tid, r)
        con.execute("INSERT OR IGNORE INTO mtfmi_tasks VALUES (?)", (tid,))

    def _put_fact(self, rid: int, tid: int, r: dict) -> None:
        con = self._db()
        try:
            fid = int(r["field_id"]) if r.get("field_id") else None
        except (TypeError, ValueError):
            fid = None
        try:
            cov = float(r.get("covered_area") or 0.0)
        except (TypeError, ValueError):
            cov = 0.0
        por = daily_portions(r)
        con.execute(
            "INSERT OR REPLACE INTO mtfmi_fact VALUES (?,?,?,?,?)",
            (rid, tid, fid, cov, len(r.get("covered_area_hourly") or [])),
        )
        con.executemany("INSERT OR REPLACE INTO mtfmi_daily VALUES (?,?,?)",
                        [(rid, d, ha) for d, ha in por.items()])

    def _drop_facts(self, tid: int) -> None:
        con = self._db()
        con.execute("DELETE FROM mtfmi_daily WHERE rid IN (SELECT rid FROM mtfmi WHERE task_id=?)", (tid,))
        con.execute("DELETE FROM mtfmi_fact WHERE task_id=?", (tid,))

    def _has_mtfmi(self, ids) -> set[int]:
        out: set[int] = set()
        con = self._db()
        ids = list(ids)
        for ch in _chunks(ids):
            q = ",".join("?" * len(ch))
            out.update(r[0] for r in con.execute(f"SELECT task_id FROM mtfmi_tasks WHERE task_id IN ({q})", ch))
        return out

    def _put_task(self, t: dict) -> bool:
        """Положить задание, если оно попадает в покрытие; иначе убрать. True — лежит в хранилище."""
        tid = t.get("id")
        if tid is None:
            return False
        tid = int(tid)
        try:
            wt = int(t.get("work_type_id"))
        except (TypeError, ValueError):
            self._remove_task(tid)
            return False
        ets = parse_ts(t.get("end_time"))
        lo = self.cov_lo.get(wt)
        if ets is None or lo is None or ets < lo:
            self._remove_task(tid)
            return False
        raw = _raw(t)
        self._db().execute(
            "INSERT OR REPLACE INTO tasks (id, wt, end_ts, season, data, raw, machine_id) VALUES (?,?,?,?,?,?,?)",
            (tid, wt, ets, str(t.get("season")), _dumps(_trim(t)), raw, _int_or_none(t.get("machine_id"))),
        )
        return True

    def _wipe_all(self) -> None:
        con = self._db()
        for tbl in ("tasks", "mtfmi_tasks", "mtfmi", "cov", "mtfmi_fact", "mtfmi_daily"):
            con.execute(f"DELETE FROM {tbl}")
        self.cov_lo = {}
        self.synced_ts = 0.0
        self.full_ts = 0.0
        self.hard_ts = 0.0

    # --------------------------------------------------------------- сеть
    async def _live_tasks(self, client, sem, wts, a_ts: float, b_ts: float) -> list[dict]:
        from live_machine_tasks_fetch import windowed_live_impl

        self.stats["live_tasks"] += 1
        return await windowed_live_impl(
            client, self.base_url, sorted(wts),
            {"end_time_gt_eq": fmt_ts(a_ts), "end_time_lt": fmt_ts(b_ts)}, sem,
        )

    async def _extend_coverage(self, client, sem, wts: set[int], need_lo: float, log: str) -> None:
        now = time.time()
        hi = now + 2 * 86400
        groups: dict[float | None, list[int]] = {}
        for wt in wts:
            cur = self.cov_lo.get(wt)
            if cur is None or cur > need_lo:
                groups.setdefault(cur, []).append(wt)
        for cur, group in groups.items():
            b = hi if cur is None else cur
            t0 = time.monotonic()
            rows = await self._live_tasks(client, sem, group, need_lo, b)
            for wt in group:
                self.cov_lo[wt] = need_lo
            for t in rows:
                self._put_task(t)
            if self.synced_ts == 0.0:
                self.synced_ts = now
            print(
                f"  {log}[store] докачано {len(rows)} заданий, wt={len(group)}, "
                f"с {fmt_ts(need_lo)}: {time.monotonic() - t0:.1f} с",
                flush=True,
            )

    async def _delta(self, client, sem, log: str) -> None:
        from async_http import fetch_pages

        t_start = time.time()
        margin = _env_float("SVODKI_STORE_MARGIN_SEC", 3600)
        since = fmt_ts(max(0.0, self.synced_ts - margin))
        t0 = time.monotonic()
        base = self.base_url

        async def _destroyed(item_type: str) -> list[dict] | None:
            """Журнал удалений (/versions, event=destroy). None — ресурс недоступен."""
            try:
                return await fetch_pages(
                    client, f"{base}/versions",
                    {"item_type": item_type, "event": "destroy", "created_at_gt_eq": since}, sem,
                )
            except Exception as exc:  # 403/4xx и т.п. — работаем без журнала (см. полный пересбор)
                print(f"  {log}[store] versions({item_type}) недоступны: {exc}", flush=True)
                return None

        changed_tasks, changed_mtfmi, del_tasks, del_mtfmi = await asyncio.gather(
            fetch_pages(client, f"{base}/machine_tasks", {"updated_at_gt_eq": since}, sem),
            fetch_pages(client, f"{base}/machine_task_field_mapping_items",
                        {"updated_at_gt_eq": since}, sem),
            _destroyed("MachineTask"),
            _destroyed("MachineTaskFieldMappingItem"),
        )
        touched: set[int] = set()
        n_destroyed = 0
        for v in del_tasks or []:
            if v.get("item_id") is None:
                continue
            tid = int(v["item_id"])
            self._remove_task(tid)
            self._drop_mtfmi(tid)
            n_destroyed += 1
        for v in del_mtfmi or []:  # удалена строка площади — перечитать mtfmi её задания
            snap = v.get("snapshot_before_change")
            if isinstance(snap, str):
                try:
                    snap = json.loads(snap)
                except ValueError:
                    snap = None
            if isinstance(snap, dict) and snap.get("machine_task_id") is not None:
                touched.add(int(snap["machine_task_id"]))
        if del_mtfmi is None or del_tasks is None:
            self.versions_ok = False
        for t in changed_tasks:
            self._put_task(t)
            if t.get("id") is not None:
                touched.add(int(t["id"]))
        self.stats["delta_tasks"] += len(changed_tasks)

        # Недавние сутки — целиком (поздняя пересчитанная площадь, о которой updated_at не говорит).
        # Удаления ловит журнал /versions, поэтому при его наличии хватает 1 суток; без него — 3.
        recent_days = _env_float("SVODKI_STORE_RECENT_DAYS", 1 if self.versions_ok else 3)
        covered = set(self.cov_lo)
        n_recent = 0
        if covered and recent_days > 0:
            r_lo = t_start - recent_days * 86400
            rows = await self._live_tasks(client, sem, covered, r_lo, t_start + 2 * 86400)
            fresh_ids = {int(r["id"]) for r in rows if r.get("id") is not None}
            stale_ids = [
                r[0] for r in self._db().execute("SELECT id FROM tasks WHERE end_ts >= ?", (r_lo,))
                if r[0] not in fresh_ids
            ]
            for tid in stale_ids:
                self._remove_task(tid)
                self._drop_mtfmi(tid)
            for r in rows:
                self._put_task(r)
            touched |= fresh_ids
            n_recent = len(rows)

        mt_ids = {int(r["machine_task_id"]) for r in changed_mtfmi if r.get("machine_task_id") is not None}
        refetch = sorted(self._has_mtfmi(touched | mt_ids))
        if refetch:
            from mtfmi_batch_fetch import fetch_mtfmi_map_live

            live = await fetch_mtfmi_map_live(client, sem, base, refetch, log_prefix=f"  {log}[store] ")
            for tid in refetch:
                self._set_mtfmi(tid, list(live.get(tid, [])))
            self.stats["delta_mtfmi_tasks"] += len(refetch)

        self.synced_ts = t_start
        print(
            f"  {log}[store] дельта: заданий {len(changed_tasks)}, mtfmi-строк {len(changed_mtfmi)}, "
            f"удалено {n_destroyed}, недавние сутки {n_recent}, перечитано mtfmi {len(refetch)}: "
            f"{time.monotonic() - t0:.1f} с",
            flush=True,
        )

    async def _reconcile_ids(self, client, sem, log: str) -> bool:
        """Убрать из хранилища задания, которых больше нет в API (GET machine_tasks/ids).

        /ids отдаёт ВСЕ id одним ответом без пагинации. Удаляем только то, чего нет в ответе,
        поэтому игнорирование фильтров (ответ шире) безвредно. Защита от обрезанного ответа:
        слишком много «удалённых» — не верим, возвращаем False (вызывающий делает пересбор).
        """
        from async_http import request_json

        store_ids = {r[0] for r in self._db().execute("SELECT id FROM tasks")}
        if not store_ids:
            return True
        lo = min(self.cov_lo.values()) if self.cov_lo else None
        params = {"end_time_gt_eq": fmt_ts(lo)} if lo else {}
        t0 = time.monotonic()
        try:
            body = await request_json(
                client, "GET", f"{self.base_url}/machine_tasks/ids", sem, params=params, timeout=300,
            )
        except Exception as exc:
            print(f"  {log}[store] /ids недоступен: {exc}", flush=True)
            return False
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            return False
        meta = (body.get("meta") or {}).get("response") or {}
        if meta.get("obtained_records") not in (None, len(data)):
            return False  # ответ обрезан
        live = {int(x["id"] if isinstance(x, dict) else x) for x in data}
        gone = store_ids - live
        if len(gone) > max(50, 0.05 * len(store_ids)):
            print(f"  {log}[store] /ids: «удалённых» {len(gone)} из {len(store_ids)} — подозрительно, пересбор", flush=True)
            return False
        for tid in gone:
            self._remove_task(tid)
            self._drop_mtfmi(tid)
        print(f"  {log}[store] сверка по /ids: {len(live)} в API, удалено из хранилища {len(gone)}: "
              f"{time.monotonic() - t0:.1f} с", flush=True)
        return True

    # ------------------------------------------------------------- публично
    def _covered(self, wts, need_lo: float) -> bool:
        return all(self.cov_lo.get(w) is not None and self.cov_lo[w] <= need_lo for w in wts)

    async def ensure(self, client, sem, wts: set[int], need_lo: float, log: str = "",
                     *, force: bool = False) -> None:
        now = time.time()
        if not force:
            if now < self._fresh_until and self._covered(wts, need_lo):
                return
            if _truthy("SVODKI_STORE_READONLY"):
                # Режим сводок: свежесть обеспечил бэкенд перед пачкой — только читаем.
                self._load_state()
                if self.synced_ts > 0 and self._covered(wts, need_lo):
                    self._fresh_until = now + _env_float("SVODKI_STORE_MAX_AGE_SEC", 90)
                    return
        async with self._xlock():
            full_h = _env_float("SVODKI_STORE_FULL_REFRESH_HOURS", 24)
            now = time.time()
            hard_h = _env_float("SVODKI_STORE_HARD_REFRESH_HOURS", 168)
            if self.full_ts and full_h > 0 and now - self.full_ts > full_h * 3600:
                hard_due = hard_h > 0 and now - (self.hard_ts or self.full_ts) > hard_h * 3600
                if not hard_due and await self._reconcile_ids(client, sem, log):
                    self.full_ts = now  # мягкая сверка по /ids, без перекачки сезона
                else:
                    print(f"  {log}[store] полный пересбор", flush=True)
                    self._wipe_all()
            if not self.full_ts:
                self.full_ts = self.hard_ts = now
            max_age = _env_float("SVODKI_STORE_MAX_AGE_SEC", 90)
            stale = self.synced_ts == 0.0 or now - self.synced_ts > max_age
            had_cov = bool(self.cov_lo)
            await self._extend_coverage(client, sem, wts, need_lo, log)
            if stale and had_cov:
                await self._delta(client, sem, log)
            self._fresh_until = time.time() + max_age

    async def query_tasks(self, client, sem, wts, params: dict, log: str = "") -> list[dict]:
        gt = parse_ts(params["end_time_gt_eq"])
        lt = parse_ts(params["end_time_lt"])
        wts = sorted({int(w) for w in wts})
        if gt is None or lt is None:
            raise ValueError("bad window bounds")
        await self.ensure(client, sem, set(wts), gt, log)
        season = params.get("season")
        q = ",".join("?" * len(wts))
        sql = f"SELECT data FROM tasks WHERE wt IN ({q}) AND end_ts >= ? AND end_ts < ?"
        args: list[Any] = [*wts, gt, lt]
        if season is not None:
            sql += " AND season = ?"
            args.append(str(season))
        sql += " ORDER BY id"
        out = [json.loads(r[0]) for r in self._db().execute(sql, args)]
        if log:
            print(f"  {log}[store] machine_tasks: {len(out)} (wt={len(wts)})", flush=True)
        return out

    async def get_mtfmi(
        self, client, sem, base_url: str, task_ids, live_fn: Callable[..., Awaitable[dict]],
        *, log_prefix: str = "", crops: dict | None = None,
        remember: Callable[..., Awaitable[None]] | None = None,
    ) -> dict[int, list[dict]]:
        ids = sorted({int(t) for t in task_ids if t is not None})
        if not ids:
            return {}
        con = self._db()
        if crops is not None and not self._crops_loaded:
            for hid, crop in con.execute("SELECT hid, crop FROM history_crop"):
                crops.setdefault(int(hid), crop)
            self._crops_loaded = True
        if len(self._has_mtfmi(ids)) < len(ids):
            async with self._xlock():
                have = self._has_mtfmi(ids)
                missing = [i for i in ids if i not in have]
                if missing:
                    t0 = time.monotonic()
                    live = await live_fn(client, sem, base_url, missing, log_prefix=log_prefix)
                    for tid in missing:
                        self._set_mtfmi(tid, list(live.get(tid, [])))
                    print(
                        f"  {log_prefix}[store] mtfmi докачано {len(missing)} задач "
                        f"(из {len(ids)}): {time.monotonic() - t0:.1f} с",
                        flush=True,
                    )
                    if crops is not None:
                        self._save_crops(crops)
        result: dict[int, list[dict]] = {}
        for ch in _chunks(ids):
            q = ",".join("?" * len(ch))
            for tid, data in con.execute(
                f"SELECT task_id, data FROM mtfmi WHERE task_id IN ({q}) ORDER BY rid", ch
            ):
                result.setdefault(int(tid), []).append(json.loads(data))
        if remember is not None:
            try:
                n0 = len(crops) if crops is not None else 0
                await remember(client, base_url, result, sem)
                if crops is not None and len(crops) != n0:
                    async with self._xlock():
                        self._save_crops(crops)
            except Exception as exc:  # некритично, как в live-пути
                print(f"  {log_prefix}культура history_item: {exc}", flush=True)
        return result

    def _save_crops(self, crops: dict) -> None:
        self._db().executemany(
            "INSERT OR REPLACE INTO history_crop VALUES (?, ?)",
            [(int(k), v) for k, v in crops.items()],
        )

    async def fill_mtfmi(self, client, sem, log: str = "", *, crops: dict | None = None,
                         remember: Callable[..., Awaitable[None]] | None = None,
                         chunk: int = 3000) -> int:
        """Догрузить строки площади всем заданиям хранилища, у которых их ещё нет.

        Вызывается синхронизацией перед пачкой, чтобы сводки (READONLY) не ходили в API за mtfmi.
        Пишет порциями: ошибка на одной порции не теряет предыдущие.
        """
        from mtfmi_batch_fetch import fetch_mtfmi_map_live

        con = self._db()
        todo = [r[0] for r in con.execute(
            "SELECT id FROM tasks WHERE id NOT IN (SELECT task_id FROM mtfmi_tasks) ORDER BY id")]
        done = 0
        t0 = time.monotonic()
        for ids in _chunks(todo, chunk):
            async with self._xlock():
                have = self._has_mtfmi(ids)
                part = [i for i in ids if i not in have]
                if not part:
                    continue
                live = await fetch_mtfmi_map_live(client, sem, self.base_url, part, log_prefix=f"  {log}[store] ")
                for tid in part:
                    self._set_mtfmi(tid, list(live.get(tid, [])))
                if remember is not None:
                    try:
                        await remember(client, self.base_url, {t: v for t, v in live.items() if v}, sem)
                    except Exception as exc:  # некритично
                        print(f"  {log}[store] культура history_item: {exc}", flush=True)
                if crops is not None:
                    self._save_crops(crops)
                done += len(part)
        print(f"  {log}[store] mtfmi догружено {done} из {len(todo)} задач: {time.monotonic() - t0:.1f} с", flush=True)
        return done

    # ------------------------------------------------------------ сервис
    def rebuild_from_raw(self) -> int:
        """Пересобрать колонку data из raw (после смены списка полей) — без обращения к API."""
        con = self._db()
        con.execute("BEGIN IMMEDIATE")
        n = 0
        rows = con.execute("SELECT id, raw FROM tasks WHERE raw IS NOT NULL").fetchall()
        for tid, raw in rows:
            t = json.loads(zlib.decompress(raw).decode("utf-8"))
            con.execute("UPDATE tasks SET data=? WHERE id=?", (_dumps(_trim(t)), tid))
            n += 1
        for rid, raw in con.execute("SELECT rid, raw FROM mtfmi WHERE raw IS NOT NULL").fetchall():
            r = json.loads(zlib.decompress(raw).decode("utf-8"))
            con.execute("UPDATE mtfmi SET data=? WHERE rid=?", (_dumps(_trim(r)), rid))
            n += 1
        con.execute("COMMIT")
        return n


_STORES: dict[tuple[str, str], TaskStore] = {}


def get_store(base_url: str) -> TaskStore | None:
    if (os.environ.get("SVODKI_TASK_STORE_BACKEND") or "").strip().lower() == "pickle":
        # Аварийный возврат на прежнее pickle-хранилище (svodki_task_store_pickle.py).
        import svodki_task_store_pickle as _legacy

        return _legacy.get_store(base_url)  # type: ignore[return-value]
    d = store_dir()
    if d is None:
        return None
    key = (str(d), str(base_url).rstrip("/"))
    st = _STORES.get(key)
    if st is None:
        st = _STORES[key] = TaskStore(d, key[1])
    return st


if __name__ == "__main__":  # python svodki_task_store.py rebuild
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "rebuild":
        _d = store_dir()
        _base = os.environ.get("CROPWISE_BASE_URL", "")
        if _d is None:
            raise SystemExit("SVODKI_TASK_STORE_DIR не задан")
        print("пересобрано заданий:", TaskStore(_d, _base).rebuild_from_raw())
    elif len(sys.argv) > 1 and sys.argv[1] == "rebuild-facts":
        _d = store_dir()
        if _d is None:
            raise SystemExit("SVODKI_TASK_STORE_DIR не задан")
        print("пересчитано строк площади:", TaskStore(_d, os.environ.get("CROPWISE_BASE_URL", "")).rebuild_facts())
    else:
        raise SystemExit("команды: rebuild | rebuild-facts")
