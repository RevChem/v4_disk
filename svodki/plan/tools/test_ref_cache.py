# -*- coding: utf-8 -*-
"""Кэш справочников: страницы из полного списка, промах = одна выгрузка, TTL, requests и httpx. Из svodki/plan."""
from __future__ import annotations

import asyncio, os, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
tmp = tempfile.mkdtemp(prefix="refc_")
os.environ["SVODKI_TASK_STORE_DIR"] = tmp
os.environ["CROPWISE_BASE_URL"] = "http://x/api/v3"
import plans  # noqa: F401,E402
import httpx, requests  # noqa: E402
import importlib.util  # noqa: E402
_sp = importlib.util.spec_from_file_location('svodki_sitecustomize', Path(__file__).resolve().parents[1] / 'sitecustomize.py')
_m = importlib.util.module_from_spec(_sp); _sp.loader.exec_module(_m)  # как при PYTHONPATH=plan/ в боевых процессах
import svodki_ref_store as rs  # noqa: E402
from cropwise_paging import from_id_should_continue  # noqa: E402

DB = {"fields": [{"id": i * 3, "n": i} for i in range(1, 2501)], "machines": [{"id": i} for i in range(1, 150)],
      "history_items": [{"id": i, "year": 2026} for i in range(1, 450)]}
calls = []
failed = False


def ok(n, c):
    global failed
    print(("  OK   " if c else "  FAIL ") + n); failed |= not c


def handler(req):
    q = req.url.params; ep = req.url.path.rsplit("/", 1)[-1]
    calls.append((ep, dict(q)))
    rows = [r for r in DB.get(ep, []) if r["id"] >= int(q.get("from_id", 0))]
    pg = rows[:100]  # реальный размер страницы ~100
    return httpx.Response(200, json={"data": pg, "meta": {"response": {
        "last_record_id": pg[-1]["id"] if pg else None, "obtained_records": len(pg)}}})


# requests-путь: подменяем транспорт на уровне адаптера
class Adapter(requests.adapters.BaseAdapter):
    def send(self, request, **kw):
        r = handler(httpx.Request("GET", request.url))
        resp = requests.Response(); resp.status_code = r.status_code; resp._content = r.content
        resp.url = request.url; resp.headers["content-type"] = "application/json"; return resp
    def close(self): pass


def get_all_requests(ep, extra=None):
    s = requests.Session(); s.mount("http://", Adapter())
    out, fid = [], 0
    while True:
        b = s.get(f"http://x/api/v3/{ep}", params={**(extra or {}), "from_id": fid, "limit": 1000}).json()
        ch = b["data"]
        if not ch: break
        out += ch
        if not from_id_should_continue(b["meta"]["response"], ch): break
        fid = b["meta"]["response"]["last_record_id"] + 1
    return out


async def get_all_httpx(ep, extra=None):
    from async_http import fetch_pages
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        return await fetch_pages(c, f"http://x/api/v3/{ep}", dict(extra or {}), asyncio.Semaphore(5))


print("1) requests: промах → одна полная выгрузка, повтор из кэша")
calls.clear(); a = get_all_requests("fields"); n1 = len(calls)
ok("данные == эталон", a == DB["fields"])
ok(f"выгрузка по {n1} запросам (25 страниц + хвост), не больше", n1 <= 27)
calls.clear(); b = get_all_requests("fields")
ok("повтор: 0 запросов к API, те же данные", not calls and b == a)

print("2) httpx (fetch_pages) читает тот же кэш")
calls.clear(); c = asyncio.run(get_all_httpx("fields"))
ok("0 запросов, данные == эталон", not calls and c == DB["fields"])

print("3) разные параметры — разные ключи; не-справочники не трогаем")
calls.clear(); h = get_all_requests("history_items", {"year": 2026})
ok("history_items?year=2026 == эталон", h == DB["history_items"])
calls.clear(); h2 = get_all_requests("history_items", {"year": 2025}); ok("другой year — отдельная выгрузка", len(calls) > 0)
calls.clear(); get_all_requests("machine_tasks_x"); ok("прочие endpoints идут в API как раньше", len(calls) > 0)

print("4) TTL")
os.environ["SVODKI_REF_CACHE_TTL_SEC"] = "0.001"; rs._MEMO.clear(); time.sleep(0.01)
calls.clear(); m = get_all_requests("machines"); calls.clear(); time.sleep(0.01); rs._MEMO.clear(); m = get_all_requests("machines")
ok("просроченный кэш → перезагрузка", len(calls) > 0 and m == DB["machines"])
os.environ["SVODKI_REF_CACHE_TTL_SEC"] = "3600"

print("5) границы страниц")
req = rs.parse_request("GET", "http://x/api/v3/fields", {"from_id": 7, "limit": 5})
ids, rows = rs.load(rs.parse_request("GET", "http://x/api/v3/fields", {"from_id": 0}))
import json as _j
body = _j.loads(rs.page_entry(req, ids, rows)["text"])
ok("from_id=7,limit=5 → id 9,12,15,18,21", [r["id"] for r in body["data"]] == [9, 12, 15, 18, 21])
ok("пустая страница после конца", _j.loads(rs.page_entry({**req, "from_id": 10**9}, ids, rows)["text"])["data"] == [])

print("6) выключатель")
os.environ["SVODKI_REF_CACHE_DISABLE"] = "1"; calls.clear(); get_all_requests("fields")
ok("SVODKI_REF_CACHE_DISABLE=1 → прямой API", len(calls) > 0)
print("7) refresh_standard прогревает кэш; сводки потом не ходят в API")
os.environ.pop("SVODKI_REF_CACHE_DISABLE", None); rs._MEMO.clear()
import sqlite3; c0 = sqlite3.connect(os.path.join(tmp, "refs.sqlite")); c0.execute("DELETE FROM ref"); c0.commit(); c0.close()
async def warm():
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        return await rs.refresh_standard(c, asyncio.Semaphore(5), "http://x/api/v3", [2026])
calls.clear(); nw = asyncio.run(warm()); ok(f"прогрето наборов: {nw}", nw == 7)
calls.clear(); a = get_all_requests("fields"); b = get_all_requests("history_items", {"year": 2026})
ok("после прогрева 0 запросов, данные верны", not calls and a == DB["fields"] and b == DB["history_items"])
print("\nИТОГ:", "ЕСТЬ ОШИБКИ" if failed else "всё прошло"); sys.exit(1 if failed else 0)
