# -*- coding: utf-8 -*-
"""Постоянное инкрементальное хранилище machine_tasks и mtfmi между запусками сводок.

Зачем. Каждая сводка (и каждый запуск пачки) заново качает один и тот же сезон:
~70 тыс. заданий и их mtfmi (десятки секунд). Cropwise умеет отдавать «что изменилось»
(фильтр ``updated_at_gt_eq`` у machine_tasks и machine_task_field_mapping_items, см.
документацию API v3), поэтому сезон качается один раз, дальше докачиваются только изменения.

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
import pickle
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

try:  # Linux-сервер; на Windows хранилище работает без межпроцессной блокировки
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

STORE_VERSION = 1
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


class TaskStore:
    def __init__(self, directory: Path, base_url: str) -> None:
        self.dir = directory
        self.base_url = base_url.rstrip("/")
        self.tasks_path = directory / "tasks.pkl"
        self.mtfmi_path = directory / "mtfmi.pkl"
        self.lock_path = directory / "store.lock"
        # tasks.pkl
        self.by_wt: dict[int, dict[int, dict]] = {}
        self.end_ts: dict[int, float] = {}
        self.cov_lo: dict[int, float] = {}
        self.synced_ts = 0.0
        self.full_ts = 0.0
        self.hard_ts = 0.0
        # mtfmi.pkl
        self.mtfmi: dict[int, list[dict]] = {}
        self.history_crop: dict[int, str] = {}
        self._tasks_mtime = -1.0
        self._mtfmi_mtime = -1.0
        self._tasks_dirty = False
        self._mtfmi_dirty = False
        self._fresh_until = 0.0
        self._crops_loaded = False
        self._locks: dict[int, asyncio.Lock] = {}
        self.versions_ok = True
        self.stats = {"live_tasks": 0, "delta_tasks": 0, "delta_mtfmi_tasks": 0}

    # ------------------------------------------------------------------ диск
    def _read(self, path: Path) -> dict | None:
        try:
            with open(path, "rb") as fh:
                data = pickle.load(fh)
        except Exception:
            return None
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            return None
        if data.get("base_url") != self.base_url:
            return None
        return data

    def _reload_if_changed(self) -> None:
        """Подтянуть файлы, если их обновил другой процесс."""
        try:
            tm = self.tasks_path.stat().st_mtime
        except OSError:
            tm = -1.0
        if tm != self._tasks_mtime:
            d = self._read(self.tasks_path) if tm >= 0 else None
            if d:
                self.by_wt, self.end_ts, self.cov_lo = d["by_wt"], d["end_ts"], d["cov_lo"]
                self.synced_ts, self.full_ts = d["synced_ts"], d["full_ts"]
                self.hard_ts = d.get("hard_ts", d["full_ts"])
            else:
                self.by_wt, self.end_ts, self.cov_lo = {}, {}, {}
                self.synced_ts = 0.0
                self.full_ts = 0.0
                self.hard_ts = 0.0
            self._tasks_mtime = tm
            self._tasks_dirty = False
        try:
            mm = self.mtfmi_path.stat().st_mtime
        except OSError:
            mm = -1.0
        if mm != self._mtfmi_mtime:
            d = self._read(self.mtfmi_path) if mm >= 0 else None
            if d:
                self.mtfmi, self.history_crop = d["mtfmi"], d["history_crop"]
            else:
                self.mtfmi, self.history_crop = {}, {}
            self._mtfmi_mtime = mm
            self._mtfmi_dirty = False

    @staticmethod
    def _atomic_dump(path: Path, payload: dict) -> float:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return path.stat().st_mtime

    def _save(self) -> None:
        if self._tasks_dirty:
            self._tasks_mtime = self._atomic_dump(self.tasks_path, {
                "version": STORE_VERSION, "base_url": self.base_url,
                "by_wt": self.by_wt, "end_ts": self.end_ts, "cov_lo": self.cov_lo,
                "synced_ts": self.synced_ts, "full_ts": self.full_ts, "hard_ts": self.hard_ts,
            })
            self._tasks_dirty = False
        if self._mtfmi_dirty:
            self._mtfmi_mtime = self._atomic_dump(self.mtfmi_path, {
                "version": STORE_VERSION, "base_url": self.base_url,
                "mtfmi": self.mtfmi, "history_crop": self.history_crop,
            })
            self._mtfmi_dirty = False

    # ------------------------------------------------------------ блокировки
    def _loop_lock(self) -> asyncio.Lock:
        key = id(asyncio.get_running_loop())
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    @contextlib.asynccontextmanager
    async def _xlock(self):
        async with self._loop_lock():
            self.dir.mkdir(parents=True, exist_ok=True)
            fh = open(self.lock_path, "a+")
            try:
                if fcntl is not None:
                    await asyncio.to_thread(fcntl.flock, fh, fcntl.LOCK_EX)
                self._reload_if_changed()
                yield
            finally:
                try:
                    self._save()
                finally:
                    if fcntl is not None:
                        with contextlib.suppress(OSError):
                            fcntl.flock(fh, fcntl.LOCK_UN)
                    fh.close()

    # ------------------------------------------------------------- мутации
    def _remove_task(self, tid: int) -> None:
        self.end_ts.pop(tid, None)
        for rows in self.by_wt.values():
            if tid in rows:
                del rows[tid]
                self._tasks_dirty = True
                return

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
        # смена work_type: убрать из старого wt
        old_ts = self.end_ts.get(tid)
        if old_ts is not None:
            for w, rows in self.by_wt.items():
                if w != wt and tid in rows:
                    del rows[tid]
        if ets is None or lo is None or ets < lo:
            self._remove_task(tid)
            self._tasks_dirty = True
            return False
        self.by_wt.setdefault(wt, {})[tid] = t
        self.end_ts[tid] = ets
        self._tasks_dirty = True
        return True

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
                self.by_wt.setdefault(wt, {})
            for t in rows:
                self._put_task(t)
            self._tasks_dirty = True
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
            if self.mtfmi.pop(tid, None) is not None:
                self._mtfmi_dirty = True
            self._tasks_dirty = True
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
            if self._put_task(t):
                touched.add(int(t["id"]))
            elif t.get("id") is not None:
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
            for wt, wrows in self.by_wt.items():
                for tid in [i for i in wrows if self.end_ts.get(i, 0) >= r_lo and i not in fresh_ids]:
                    self._remove_task(tid)
                    self.mtfmi.pop(tid, None)
                    self._mtfmi_dirty = True
            for r in rows:
                self._put_task(r)
            touched |= fresh_ids
            n_recent = len(rows)

        mt_ids = {int(r["machine_task_id"]) for r in changed_mtfmi if r.get("machine_task_id") is not None}
        refetch = [tid for tid in (touched | mt_ids) if tid in self.mtfmi]
        if refetch:
            from mtfmi_batch_fetch import fetch_mtfmi_map_live

            live = await fetch_mtfmi_map_live(client, sem, base, refetch, log_prefix=f"  {log}[store] ")
            for tid in refetch:
                self.mtfmi[tid] = list(live.get(tid, []))
            self._mtfmi_dirty = True
            self.stats["delta_mtfmi_tasks"] += len(refetch)

        self.synced_ts = t_start
        self._tasks_dirty = True
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

        store_ids = set(self.end_ts)
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
            self.mtfmi.pop(tid, None)
        if gone:
            self._tasks_dirty = self._mtfmi_dirty = True
        print(f"  {log}[store] сверка по /ids: {len(live)} в API, удалено из хранилища {len(gone)}: "
              f"{time.monotonic() - t0:.1f} с", flush=True)
        return True

    # ------------------------------------------------------------- публично
    async def ensure(self, client, sem, wts: set[int], need_lo: float, log: str = "") -> None:
        now = time.time()
        if (
            now < self._fresh_until
            and all(self.cov_lo.get(w) is not None and self.cov_lo[w] <= need_lo for w in wts)
        ):
            return
        async with self._xlock():
            full_h = _env_float("SVODKI_STORE_FULL_REFRESH_HOURS", 24)
            now = time.time()
            hard_h = _env_float("SVODKI_STORE_HARD_REFRESH_HOURS", 168)
            if self.full_ts and full_h > 0 and now - self.full_ts > full_h * 3600:
                hard_due = hard_h > 0 and now - (self.hard_ts or self.full_ts) > hard_h * 3600
                if not hard_due and await self._reconcile_ids(client, sem, log):
                    self.full_ts = now  # мягкая сверка по /ids, без перекачки сезона
                    self._tasks_dirty = True
                else:
                    print(f"  {log}[store] полный пересбор", flush=True)
                    self.by_wt, self.end_ts, self.cov_lo = {}, {}, {}
                    self.mtfmi = {}
                    self.synced_ts = 0.0
                    self.full_ts = 0.0
                    self._tasks_dirty = self._mtfmi_dirty = True
            if not self.full_ts:
                self.full_ts = self.hard_ts = now
                self._tasks_dirty = True
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
        wts = {int(w) for w in wts}
        if gt is None or lt is None:
            raise ValueError("bad window bounds")
        await self.ensure(client, sem, wts, gt, log)
        season = params.get("season")
        out: list[dict] = []
        for wt in wts:
            for tid, t in self.by_wt.get(wt, {}).items():
                e = self.end_ts.get(tid)
                if e is None or not (gt <= e < lt):
                    continue
                if season is not None and str(t.get("season")) != str(season):
                    continue
                out.append(t)
        out.sort(key=lambda t: int(t["id"]))
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
        if crops is not None and not self._crops_loaded:
            async with self._xlock():
                crops.update({k: v for k, v in self.history_crop.items() if k not in crops})
            self._crops_loaded = True
        if any(i not in self.mtfmi for i in ids):
            async with self._xlock():
                missing = [i for i in ids if i not in self.mtfmi]
                if missing:
                    t0 = time.monotonic()
                    live = await live_fn(client, sem, base_url, missing, log_prefix=log_prefix)
                    for tid in missing:
                        self.mtfmi[tid] = list(live.get(tid, []))
                    self._mtfmi_dirty = True
                    print(
                        f"  {log_prefix}[store] mtfmi докачано {len(missing)} задач "
                        f"(из {len(ids)}): {time.monotonic() - t0:.1f} с",
                        flush=True,
                    )
                    if crops is not None:
                        self.history_crop.update(crops)
        result = {tid: list(self.mtfmi[tid]) for tid in ids if self.mtfmi.get(tid)}
        if remember is not None:
            try:
                n0 = len(crops) if crops is not None else 0
                await remember(client, base_url, result, sem)
                if crops is not None and len(crops) != n0:
                    async with self._xlock():
                        self.history_crop.update(crops)
                        self._mtfmi_dirty = True
            except Exception as exc:  # некритично, как в live-пути
                print(f"  {log_prefix}культура history_item: {exc}", flush=True)
        return result


_STORES: dict[tuple[str, str], TaskStore] = {}


def get_store(base_url: str) -> TaskStore | None:
    d = store_dir()
    if d is None:
        return None
    key = (str(d), str(base_url).rstrip("/"))
    st = _STORES.get(key)
    if st is None:
        st = _STORES[key] = TaskStore(d, key[1])
    return st
