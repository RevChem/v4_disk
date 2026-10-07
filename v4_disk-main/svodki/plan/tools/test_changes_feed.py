# -*- coding: utf-8 -*-
"""Проверка клиента «Changed objects» (utils/api/changes_feed.py) на мок-API, без сети.
Запуск из svodki/plan: python tools/test_changes_feed.py"""
from __future__ import annotations

import asyncio, os, random, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plans  # noqa: F401,E402
import httpx  # noqa: E402
import changes_feed as cf  # noqa: E402

TZ = timezone(timedelta(hours=3))
BASE = "http://x/api/v3"
failed = False


def ok(name, cond):
    global failed
    print(("  OK   " if cond else "  FAIL ") + name)
    failed |= not cond


def iso(t, frac=False):
    d = datetime.fromtimestamp(t, tz=TZ)
    return d.strftime("%Y-%m-%dT%H:%M:%S") + (f".{int((t % 1) * 1000):03d}" if frac else "") + "+03:00"


def parse(s):
    return datetime.fromisoformat(s).timestamp()


class Mock:
    """/res/changes: окно по updated_at; границы включаются по режиму; сервер режет страницу до cap."""

    def __init__(self, n=900, span=3 * 3600, cap=50, bounds="incl_excl", meta_limit=True, same_second=0,
                 changes_status=200, filter_ignored=False):
        random.seed(5)
        self.now = time.time()
        self.rows = [{"id": i, "updated_at": iso(self.now - random.uniform(0, span), frac=True)} for i in range(1, n + 1)]
        # кластер записей в одну секунду
        t0 = self.now - span / 2
        for k in range(same_second):
            self.rows.append({"id": 100000 + k, "updated_at": iso(t0 + 0.3, frac=True)})
        self.cap, self.bounds, self.meta_limit = cap, bounds, meta_limit
        self.changes_status, self.filter_ignored = changes_status, filter_ignored
        self.calls = {"changes": 0, "filter": 0}

    def truth(self, a):
        return {r["id"] for r in self.rows if parse(r["updated_at"]) >= a}

    def handler(self, req):
        q = req.url.params
        path = req.url.path
        if path.endswith("/changes_ids") and not getattr(self, "ids_ok", False):
            return httpx.Response(404, json={"error": "no"})
        if path.endswith("/changes") or path.endswith("/changes_ids"):
            self.calls["changes"] += 1
            if self.changes_status != 200:
                return httpx.Response(self.changes_status, json={"error": "no"})
            a, b = parse(q["from_time"]), parse(q["to_time"])
            lo_incl = self.bounds in ("incl_excl", "incl_incl")
            hi_incl = self.bounds in ("excl_incl", "incl_incl")
            sel = [r for r in self.rows
                   if (parse(r["updated_at"]) >= a if lo_incl else parse(r["updated_at"]) > a)
                   and (parse(r["updated_at"]) <= b if hi_incl else parse(r["updated_at"]) < b)]
            sel.sort(key=lambda r: r["updated_at"])
            lim = min(int(q.get("limit", 1000)), self.cap)
            page = sel[:lim]
            meta = {"response": {"limit": self.cap}} if self.meta_limit else {"response": {}}
            if path.endswith("/changes_ids"):
                page = [{"id": r["id"], "updated_at": r["updated_at"]} for r in page]
            return httpx.Response(200, json={"data": page, "meta": meta})
        # обычный список с фильтром updated_at_gt_eq и курсором from_id
        self.calls["filter"] += 1
        rows = self.rows
        if "updated_at_gt_eq" in q and not self.filter_ignored:
            a = datetime.strptime(q["updated_at_gt_eq"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=TZ).timestamp()
            rows = [r for r in rows if parse(r["updated_at"]) >= a]
        fid, lim = int(q.get("from_id", 0)), int(q.get("limit", 100))
        rows = sorted((r for r in rows if r["id"] >= fid), key=lambda r: r["id"])
        pg = rows[:lim]
        return httpx.Response(200, json={"data": pg, "meta": {"response": {
            "last_record_id": pg[-1]["id"] if pg else None, "obtained_records": len(pg)}}})


async def run(m: Mock, since, mode="auto", caps=None):
    async with httpx.AsyncClient(transport=httpx.MockTransport(m.handler)) as c:
        return await cf.fetch_changed(c, BASE, "things", since, asyncio.Semaphore(8), caps=caps, mode=mode)


print("1) деление окна: любой вариант включения границ даёт полный набор")
for bounds in ("incl_excl", "excl_incl", "incl_incl", "excl_excl"):
    m = Mock(bounds=bounds)
    since = m.now - 2 * 3600
    rows, how = asyncio.run(run(m, since))
    got = {r["id"] for r in rows}
    ok(f"{bounds}: {len(got)} из {len(m.truth(since))}, источник {how}, запросов /changes {m.calls['changes']}",
       how == "changes" and got == m.truth(since))
    ok(f"{bounds}: без дублей", len(rows) == len(got))

print("2) сервер не сообщает limit в meta — упор в страницу всё равно определяется по запрошенному limit")
os.environ["SVODKI_CHANGES_LIMIT"] = "50"
m = Mock(meta_limit=False, cap=50)
since = m.now - 2 * 3600
rows, how = asyncio.run(run(m, since))
ok("полный набор при limit=50 без meta.limit", how == "changes" and {r["id"] for r in rows} == m.truth(since))
os.environ.pop("SVODKI_CHANGES_LIMIT")

print("3) малая дельта — один запрос")
m = Mock(n=20, cap=1000)
rows, how = asyncio.run(run(m, m.now - 3600))
ok("1 запрос /changes", m.calls["changes"] == 1 and how == "changes")

print("4) /changes не поддерживается (404) → запасной путь, возможность запоминается")
m = Mock(changes_status=404)
caps: dict = {}
since = m.now - 3600
rows, how = asyncio.run(run(m, since, caps=caps))
ok("фильтр updated_at дал полный набор", how == "filter" and {r["id"] for r in rows} == m.truth(since))
ok("возможность помечена как неработающая", caps.get("things", {}).get("ok") is False)
n0 = m.calls["changes"]
rows, how = asyncio.run(run(m, since, caps=caps))
ok("повторно /changes уже не дёргается", m.calls["changes"] == n0 and how == "filter")
caps["things"]["ts"] = time.time() - 2 * 86400
rows, how = asyncio.run(run(m, since, caps=caps))
ok("через сутки пробуем снова", m.calls["changes"] == n0 + 1)

print("5) на одной секунде больше записей, чем лимит → окно не делится → запасной путь, ничего не теряем")
m = Mock(n=100, cap=50, same_second=120)
since = m.now - 3 * 3600
rows, how = asyncio.run(run(m, since))
ok("источник — фильтр, набор полный", how == "filter" and {r["id"] for r in rows} == m.truth(since))

print("6) ресурс игнорирует updated_at_gt_eq и отдаёт всё — лишнее отбрасывается на клиенте")
m = Mock(changes_status=404, filter_ignored=True)
since = m.now - 1800
rows, how = asyncio.run(run(m, since))
got = {r["id"] for r in rows}
ok("нет записей старше since-120с, все нужные на месте",
   how == "filter" and m.truth(since) <= got and all(parse(r["updated_at"]) >= since - 121 for r in rows)
   and len(got) < len(m.rows))

print("7) режимы off / force / verify")
m = Mock()
since = m.now - 3600
rows, how = asyncio.run(run(m, since, mode="off"))
ok("off: /changes не вызывается", m.calls["changes"] == 0 and how == "filter")
m = Mock(changes_status=404)
try:
    asyncio.run(run(m, m.now - 3600, mode="force"))
    ok("force: ошибка при недоступном /changes", False)
except cf.ChangesUnsupported:
    ok("force: ошибка при недоступном /changes", True)
m = Mock()
since = m.now - 3600
rows, how = asyncio.run(run(m, since, mode="verify"))
ok("verify: оба пути, объединение равно истине", how == "verify" and {r["id"] for r in rows} == m.truth(since))

print("8) /changes_ids")
async def ids_case():
    m = Mock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(m.handler)) as c:
        return m, await cf.fetch_changed_ids(c, BASE, "things", m.now - 3600, asyncio.Semaphore(4))
m, res = asyncio.run(ids_case())
ok("/changes_ids недоступен (404) → None, без исключения", res is None)
async def ids_case2():
    m = Mock(); m.ids_ok = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(m.handler)) as c:
        return m, await cf.fetch_changed_ids(c, BASE, "things", m.now - 3600, asyncio.Semaphore(4))
m, res = asyncio.run(ids_case2())
ok("/changes_ids отдаёт полный набор id", res == m.truth(m.now - 3600))

print("9) формат времени с поясом")
ok("+03:00 в формате документации", cf.fmt_offset(1_700_000_000).endswith("+03:00") and "T" in cf.fmt_offset(1_700_000_000))

print("\nИТОГ:", "ЕСТЬ ОШИБКИ" if failed else "всё прошло")
sys.exit(1 if failed else 0)
