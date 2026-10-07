# -*- coding: utf-8 -*-
"""Связи «задание → агрооперация» (mtaomi) и привязка агроопераций в sqlite-хранилище заданий.

Зачем. Сводка 2 определяет культуру задания через AgroOperation: для КАЖДОГО задания сезона качались его связи
(machine_task_agro_operation_mapping_items, пачки по 300 → сотни запросов) и затем сами агрооперации
(mass_request по 500 id). Это повторялось на каждом запуске. Теперь связи лежат в sqlite рядом с заданиями и
обновляются только изменённым.

Что хранится.
  mtaomi_tasks(task_id)   — задания, чьи связи уже загружены (в том числе с нулём связей);
  mtaomi(id, task_id, agro_operation_id) — сами связи;
  agro_ops(id, history_item_id, field_id) — то, что сводке нужно от агрооперации.

Как поддерживается свежесть (каждый пункт — отдельный независимый предохранитель).
  1. Изменённые связи и агрооперации — через «Changed objects» (``changes_feed``: /changes, запасной путь
     updated_at_gt_eq); у изменённой связи перечитываются связи её задания, у изменённой агрооперации
     обновляется строка и поднимается версия всех заданий, которые на неё ссылаются.
  2. Удалённые связи — журнал /versions (event=destroy, MachineTaskAgroOperationMappingItem).
  3. Задания, которые изменила дельта заданий (links_dirty), и задания последних
     ``SVODKI_LINKS_RECENT_DAYS`` (7) суток перечитываются целиком — ловит правки, о которых updated_at не говорит.
  4. Раз в ``SVODKI_LINKS_HARD_REFRESH_HOURS`` (72) все связи перечитываются заново (как полный пересбор заданий).

Версия задания (task_rev) поднимается только если набор связей или привязка агрооперации реально изменились.

Выключить: ``SVODKI_TASK_LINKS=0`` — сводка 2 качает связи как раньше.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Any

_CHUNK = 500


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def links_enabled() -> bool:
    return (os.environ.get("SVODKI_TASK_LINKS") or "1").strip().lower() not in {"0", "false", "no", "off"}


def _chunks(items: list, n: int = _CHUNK):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class LinksMixin:
    """Подмешивается в TaskStore: использует self._db(), self._xlock(), self._bump(), self.base_url."""

    # ----------------------------------------------------------------- запись
    def _mark_links_dirty(self, ids) -> None:
        if not links_enabled():
            return
        ids = [int(i) for i in ids if i is not None]
        if ids:
            self._db().executemany("INSERT OR IGNORE INTO links_dirty VALUES (?)", [(i,) for i in ids])

    def _set_links(self, tid: int, rows: list[dict]) -> bool:
        """Заменить связи задания. True — набор связей изменился (версия задания поднята)."""
        con = self._db()
        new = sorted((int(r["id"]), _int_or_none(r.get("agro_operation_id")))
                     for r in rows if r.get("id") is not None)
        known = con.execute("SELECT 1 FROM mtaomi_tasks WHERE task_id=?", (tid,)).fetchone() is not None
        if known:
            old = sorted(con.execute("SELECT id, agro_operation_id FROM mtaomi WHERE task_id=?", (tid,)).fetchall())
            old = [(int(a), b) for a, b in old]
            if old == new:
                con.execute("UPDATE mtaomi_tasks SET synced=? WHERE task_id=?", (time.time(), tid))
                return False
        con.execute("DELETE FROM mtaomi WHERE task_id=?", (tid,))
        con.executemany("INSERT OR REPLACE INTO mtaomi (id, task_id, agro_operation_id) VALUES (?,?,?)",
                        [(i, tid, a) for i, a in new])
        con.execute("INSERT OR REPLACE INTO mtaomi_tasks VALUES (?, ?)", (tid, time.time()))
        self._bump(tid)
        return True

    def _put_ops(self, ops: list[dict]) -> int:
        """Положить/обновить агрооперации. Для изменившихся поднимается версия ссылающихся заданий."""
        con = self._db()
        changed = 0
        for op in ops:
            oid = _int_or_none(op.get("id"))
            if oid is None:
                continue
            new = (_int_or_none(op.get("history_item_id")), _int_or_none(op.get("field_id")))
            old = con.execute("SELECT history_item_id, field_id FROM agro_ops WHERE id=?", (oid,)).fetchone()
            if old is not None and tuple(old) == new:
                continue
            con.execute("INSERT OR REPLACE INTO agro_ops VALUES (?,?,?)", (oid, new[0], new[1]))
            if old is not None:  # привязка ИЗМЕНИЛАСЬ (новая строка заданий ещё не касалась)
                changed += 1
                for (tid,) in con.execute("SELECT DISTINCT task_id FROM mtaomi WHERE agro_operation_id=?",
                                          (oid,)).fetchall():
                    self._bump(tid)
        return changed

    def _has_links(self, ids) -> set[int]:
        out: set[int] = set()
        con = self._db()
        for ch in _chunks(list(ids)):
            q = ",".join("?" * len(ch))
            out.update(r[0] for r in con.execute(f"SELECT task_id FROM mtaomi_tasks WHERE task_id IN ({q})", ch))
        return out

    # ------------------------------------------------------------------ сеть
    async def _fetch_links_live(self, client, sem, ids: list[int], log: str) -> dict[int, list[dict]]:
        from mtaomi_batch_fetch import fetch_mtaomi_by_machine_task_ids

        return await fetch_mtaomi_by_machine_task_ids(client, self.base_url, ids, sem, log_prefix=f"  {log}[links] ")

    async def _load_missing_ops(self, client, sem, log: str) -> int:
        """Догрузить агрооперации, на которые ссылаются связи, но которых нет в agro_ops."""
        from mtaomi_batch_fetch import mass_request_async

        con = self._db()
        missing = sorted(r[0] for r in con.execute(
            "SELECT DISTINCT m.agro_operation_id FROM mtaomi m LEFT JOIN agro_ops a ON a.id=m.agro_operation_id "
            "WHERE m.agro_operation_id IS NOT NULL AND a.id IS NULL"))
        if not missing:
            return 0
        t0 = time.monotonic()
        ops = await mass_request_async(client, self.base_url, "agro_operations", missing, sem)
        got = {int(o["id"]) for o in ops if o.get("id") is not None}
        self._put_ops(ops)
        # удалённые на стороне API агрооперации: помечаем пустой привязкой, чтобы не запрашивать каждый раз
        for oid in missing:
            if oid not in got:
                con.execute("INSERT OR IGNORE INTO agro_ops VALUES (?, NULL, NULL)", (oid,))
        print(f"  {log}[links] агрооперации догружены: {len(got)} из {len(missing)}: {time.monotonic() - t0:.1f} с",
              flush=True)
        return len(got)

    async def get_task_agro_ops_raw(self, client, sem, task_ids, log: str = ""):
        """То же, что ``_fetch_task_agro_ops_raw`` сводки 2:
        (task_id → {agro_operation_id}, agro_operation_id → history_item_id, agro_operation_id → field_id).
        Недостающие задания докачиваются и сохраняются; готовые берутся из базы без обращения к API."""
        ids = sorted({int(t) for t in task_ids if t is not None})
        task_to_ops: dict[int, set[int]] = defaultdict(set)
        op_to_hist: dict[int, int] = {}
        op_to_field: dict[int, int] = {}
        if not ids:
            return task_to_ops, op_to_hist, op_to_field
        if len(self._has_links(ids)) < len(ids):
            async with self._xlock():
                have = self._has_links(ids)
                missing = [i for i in ids if i not in have]
                if missing:
                    t0 = time.monotonic()
                    live = await self._fetch_links_live(client, sem, missing, log)
                    for tid in missing:
                        self._set_links(tid, list(live.get(tid, [])))
                    await self._load_missing_ops(client, sem, log)
                    now = time.time()  # отсчёт полной сверки и дельты начинается с первого наполнения
                    self._db().executemany("INSERT OR IGNORE INTO meta VALUES (?, ?)",
                                           [("links_synced", repr(now)), ("links_hard", repr(now))])
                    print(f"  {log}[links] связи докачаны для {len(missing)} заданий (из {len(ids)}): "
                          f"{time.monotonic() - t0:.1f} с", flush=True)
        con = self._db()
        for ch in _chunks(ids):
            q = ",".join("?" * len(ch))
            for tid, oid in con.execute(
                    f"SELECT task_id, agro_operation_id FROM mtaomi WHERE task_id IN ({q}) "
                    "AND agro_operation_id IS NOT NULL", ch):
                task_to_ops[int(tid)].add(int(oid))
        op_ids = sorted({o for s in task_to_ops.values() for o in s})
        for ch in _chunks(op_ids):
            q = ",".join("?" * len(ch))
            for oid, hid, fid in con.execute(f"SELECT id, history_item_id, field_id FROM agro_ops WHERE id IN ({q})", ch):
                if hid is not None:
                    op_to_hist[int(oid)] = int(hid)
                if fid is not None:
                    op_to_field[int(oid)] = int(fid)
        return task_to_ops, op_to_hist, op_to_field

    async def sync_links(self, client, sem, log: str = "", *, now: float | None = None) -> dict:
        """Обновить связи и агрооперации (вызывает синхронизация перед пачкой). Возвращает статистику.

        Трогает только уже отслеживаемые задания и виды работ: если сводки ни разу не просили связи
        (пустые таблицы), ничего не делает — первое наполнение происходит лениво, из сводки 2.
        """
        stats = {"refetched": 0, "new": 0, "ops_changed": 0, "hard": False, "skipped": False}
        if not links_enabled():
            stats["skipped"] = True
            return stats
        now = now or time.time()
        async with self._xlock():
            con = self._db()
            if con.execute("SELECT 1 FROM mtaomi_tasks LIMIT 1").fetchone() is None:
                stats["skipped"] = True
                return stats
            meta = dict(con.execute("SELECT k, v FROM meta WHERE k IN ('links_synced','links_hard')"))
            last = float(meta.get("links_synced", 0) or 0)
            hard_ts = float(meta.get("links_hard", 0) or 0)
            hard_h = _env_float("SVODKI_LINKS_HARD_REFRESH_HOURS", 72)
            t0 = time.monotonic()

            # осиротевшие связи (задание удалено/вышло из покрытия)
            con.execute("DELETE FROM mtaomi_tasks WHERE task_id NOT IN (SELECT id FROM tasks)")
            con.execute("DELETE FROM mtaomi WHERE task_id NOT IN (SELECT id FROM tasks)")

            refetch: set[int] = set()
            if hard_h > 0 and hard_ts and now - hard_ts > hard_h * 3600:
                refetch = {r[0] for r in con.execute("SELECT task_id FROM mtaomi_tasks")}
                stats["hard"] = True
            else:
                margin = _env_float("SVODKI_LINKS_MARGIN_SEC", 3600)
                since = max(0.0, (last or hard_ts or now - 86400) - margin)
                # Только виды работ, по которым сводки уже просили связи: остальные не наполняем.
                tracked = [r[0] for r in con.execute(
                    "SELECT DISTINCT t.wt FROM tasks t JOIN mtaomi_tasks m ON m.task_id=t.id")]
                q = ",".join("?" * len(tracked)) or "NULL"
                cand = await self._links_changed_tasks(client, sem, since, log)
                cand |= {r[0] for r in con.execute("SELECT task_id FROM links_dirty")}
                for ch in _chunks(sorted(cand)):
                    qq = ",".join("?" * len(ch))
                    refetch |= {r[0] for r in con.execute(
                        f"SELECT id FROM tasks WHERE id IN ({qq}) AND wt IN ({q})", [*ch, *tracked])}
                recent_days = _env_float("SVODKI_LINKS_RECENT_DAYS", 7)
                if recent_days > 0:
                    refetch |= {r[0] for r in con.execute(
                        "SELECT t.id FROM tasks t JOIN mtaomi_tasks m ON m.task_id=t.id WHERE t.end_ts >= ?",
                        (now - recent_days * 86400,))}
                # новые задания отслеживаемых видов работ (их связи ещё не загружены)
                new_ids = {r[0] for r in con.execute(
                    f"SELECT id FROM tasks WHERE wt IN ({q}) AND id NOT IN (SELECT task_id FROM mtaomi_tasks)",
                    tracked)}
                stats["new"] = len(new_ids)
                refetch |= new_ids
            ids = sorted(refetch)
            for ch in _chunks(ids, 3000):
                live = await self._fetch_links_live(client, sem, ch, log)
                for tid in ch:
                    self._set_links(tid, list(live.get(tid, [])))
            stats["refetched"] = len(ids)
            stats["ops_changed"] = await self._sync_ops(client, sem, last, now, log)
            await self._load_missing_ops(client, sem, log)
            con.execute("DELETE FROM links_dirty")
            con.executemany("INSERT INTO meta VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                            [("links_synced", repr(now)), ("links_hard", repr(now if stats["hard"] or not hard_ts else hard_ts))])
            print(f"  {log}[links] синхронизация: перечитано {stats['refetched']} заданий"
                  f"{' (полное)' if stats['hard'] else ''}, новых {stats['new']}, "
                  f"изменённых агроопераций {stats['ops_changed']}: {time.monotonic() - t0:.1f} с", flush=True)
        return stats

    async def _links_changed_tasks(self, client, sem, since: float, log: str) -> set[int]:
        """Задания, у которых связи менялись/удалялись с момента since."""
        from async_http import fetch_pages

        out: set[int] = set()
        rows = await self._changed(client, sem, "machine_task_agro_operation_mapping_items", since, log, source="auto")
        out |= {int(r["machine_task_id"]) for r in rows if r.get("machine_task_id") is not None}
        try:
            destroyed = await fetch_pages(client, f"{self.base_url}/versions", {
                "item_type": "MachineTaskAgroOperationMappingItem", "event": "destroy",
                "created_at_gt_eq": _fmt_naive(since)}, sem)
        except Exception as exc:  # журнал недоступен — полагаемся на остальные предохранители
            print(f"  {log}[links] versions(MachineTaskAgroOperationMappingItem) недоступны: {exc}", flush=True)
            destroyed = []
        import json as _json

        for v in destroyed:
            snap = v.get("snapshot_before_change")
            if isinstance(snap, str):
                try:
                    snap = _json.loads(snap)
                except ValueError:
                    snap = None
            if isinstance(snap, dict) and snap.get("machine_task_id") is not None:
                out.add(int(snap["machine_task_id"]))
        return out

    async def _sync_ops(self, client, sem, last: float, now: float, log: str) -> int:
        """Изменённые агрооперации: обновить строки, которые мы храним."""
        con = self._db()
        if con.execute("SELECT 1 FROM agro_ops LIMIT 1").fetchone() is None:
            return 0
        margin = _env_float("SVODKI_LINKS_MARGIN_SEC", 3600)
        since = max(0.0, (last or now - 86400) - margin)
        rows = await self._changed(client, sem, "agro_operations", since, log, source="auto")
        tracked = {r[0] for r in con.execute("SELECT id FROM agro_ops")}
        mine = [r for r in rows if r.get("id") is not None and int(r["id"]) in tracked]
        return self._put_ops(mine)


def _fmt_naive(ts: float) -> str:
    try:
        from svodki_task_store import fmt_ts

        return fmt_ts(ts)
    except Exception:
        from changes_feed import fmt_naive

        return fmt_naive(ts)
