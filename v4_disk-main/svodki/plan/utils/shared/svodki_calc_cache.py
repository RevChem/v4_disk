# -*- coding: utf-8 -*-
"""Кэш вкладов заданий в сезонные суммы: пересчитываются только изменившиеся задания.

Идея. Сезонная сводка — это сумма вкладов отдельных заданий (площадь по фирме/дню/машине/культуре). Вклад
задания зависит от самого задания, его строк площади, его связей с агрооперациями и справочников (культура и
фирма поля, группа машины). Хранилище ведёт версию каждого задания (``task_rev``): она растёт при любой
правке задания, строк площади, связей или привязанной агрооперации. Вклад, посчитанный при версии R,
остаётся верным, пока версия R — поэтому на следующий день пересчитывается только то, что изменилось.

Что НЕ ловит версия — зависимости от справочников и даты отчёта — вызывающий кладёт в ключ расчёта
(``calc_key``: сводка, группа, период, отпечаток справочников) и в ``stamp`` задания (окно смены).
Любое изменение ключа = полный пересчёт, как раньше.

Порядок работы вызывающего (см. 2_main_ideal_compact.py):
    snap  = store.calc_snapshot()                     # ДО чтения заданий/строк площади
    ...загрузка заданий и mtfmi...
    cached = store.calc_get(ckey, ids, stamps)        # {id: payload}
    ...пересчёт только остальных...
    store.calc_put(ckey, [(id, snap.get(id, 0), stamp, payload), ...])   # запишется лишь если версия не ушла вперёд

Запись — короткая транзакция; при занятой базе (идёт синхронизация) кэш не пишется — это только оптимизация.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import zlib
from typing import Any

_CHUNK = 400


def calc_key(*parts: Any) -> str:
    """Стабильный ключ расчёта из произвольных JSON-совместимых частей."""
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def fingerprint(obj: Any) -> str:
    """Отпечаток справочника (dict/set/list вложенных JSON-значений): равные данные → равный отпечаток."""
    def norm(o: Any) -> Any:
        if isinstance(o, dict):
            return sorted(([str(k), norm(v)] for k, v in o.items()), key=lambda kv: kv[0])
        if isinstance(o, (set, frozenset)):
            return sorted(norm(x) for x in o)
        if isinstance(o, (list, tuple)):
            return [norm(x) for x in o]
        return o
    return hashlib.sha1(json.dumps(norm(obj), ensure_ascii=False, default=str,
                                   separators=(",", ":")).encode("utf-8")).hexdigest()


def _chunks(items: list, n: int = _CHUNK):
    for i in range(0, len(items), n):
        yield items[i : i + n]


class CalcCacheMixin:
    """Подмешивается в TaskStore: использует self._db()."""

    def calc_snapshot(self) -> dict[int, int]:
        """Версии всех заданий (task_id → rev). Снимать ДО чтения данных, из которых считается вклад."""
        return {int(t): int(r) for t, r in self._db().execute("SELECT task_id, rev FROM task_rev")}

    def calc_get(self, ckey: str, task_ids, stamps: dict[int, str] | None = None) -> dict[int, Any]:
        """Верные вклады: версия задания не изменилась с момента записи и stamp совпал."""
        ids = sorted({int(t) for t in task_ids})
        out: dict[int, Any] = {}
        con = self._db()
        for ch in _chunks(ids):
            q = ",".join("?" * len(ch))
            for tid, stamp, payload in con.execute(
                f"SELECT c.task_id, c.stamp, c.payload FROM calc_cache c "
                f"LEFT JOIN task_rev r ON r.task_id = c.task_id "
                f"WHERE c.ckey=? AND c.task_id IN ({q}) AND c.rev = COALESCE(r.rev, 0)", [ckey, *ch]):
                if stamps is not None and stamps.get(int(tid)) != stamp:
                    continue
                out[int(tid)] = json.loads(zlib.decompress(payload).decode("utf-8"))
        return out

    def calc_put(self, ckey: str, items, *, label: str = "") -> int:
        """Сохранить вклады: items = [(task_id, rev_при_чтении, stamp, payload)]. Пишется только то, у чего
        версия в базе всё ещё равна rev_при_чтении (задание не менялось, пока шёл расчёт).
        -> сколько записано; при занятой базе 0 (кэш — оптимизация)."""
        rows = [(ckey, int(t), int(r), str(st),
                 zlib.compress(json.dumps(p, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"), 1),
                 int(t), int(r)) for t, r, st, p in items]
        con = self._db()
        try:
            con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return 0
        try:
            n0 = con.total_changes
            con.executemany(
                "INSERT OR REPLACE INTO calc_cache (ckey, task_id, rev, stamp, payload) "
                "SELECT ?, ?, ?, ?, ? WHERE COALESCE((SELECT rev FROM task_rev WHERE task_id=?), 0) = ?", rows)
            n = con.total_changes - n0
            self._touch_key(ckey, label)
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return n

    def _touch_key(self, ckey: str, label: str = "") -> None:
        self._db().execute(
            "INSERT INTO calc_keys VALUES (?, ?, ?) ON CONFLICT(ckey) DO UPDATE SET used=excluded.used, "
            "label=CASE WHEN excluded.label='' THEN label ELSE excluded.label END", (ckey, label, time.time()))

    def calc_touch(self, ckey: str, label: str = "") -> None:
        """Отметить ключ использованным (чтение без записи вкладов), чтобы gc его не удалил."""
        con = self._db()
        try:
            con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return
        try:
            self._touch_key(ckey, label)
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def calc_gc(self, max_age_days: float = 14.0) -> int:
        """Удалить вклады ключей, которыми не пользовались max_age_days суток, и устаревшие версии."""
        con = self._db()
        try:
            con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return 0
        try:
            cutoff = time.time() - max_age_days * 86400
            n0 = con.total_changes
            con.execute("DELETE FROM calc_cache WHERE ckey IN (SELECT ckey FROM calc_keys WHERE used < ?)", (cutoff,))
            con.execute("DELETE FROM calc_keys WHERE used < ?", (cutoff,))
            con.execute("DELETE FROM calc_cache WHERE task_id NOT IN (SELECT id FROM tasks)")
            con.execute("DELETE FROM calc_cache WHERE rev != COALESCE((SELECT rev FROM task_rev r WHERE r.task_id=calc_cache.task_id), 0)")
            n = con.total_changes - n0
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return n
