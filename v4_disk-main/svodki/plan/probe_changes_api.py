# -*- coding: utf-8 -*-
"""Зонд Cropwise API v3: что реально умеют /changes, /changes_ids, /ids, /versions.

ТОЛЬКО ЧТЕНИЕ (GET и POST mass_request, который тоже читает). Токен не печатается и
не сохраняется. Зависимостей нет (stdlib).

Запуск (из svodki/plan):

    export CROPWISE_API_TOKEN=...            # токен из окружения, не из кода
    python tools/probe_changes_api.py                 # обычная проверка, ~60-150 запросов
    python tools/probe_changes_api.py --burst 60      # + проба лимитов: 60 одновременных запросов
    python tools/probe_changes_api.py --hours 6       # окно изменений (по умолчанию 24 ч)

Результат: текст в консоли + probe_report.json рядом со скриптом. Пришлите оба
(или хотя бы консольный вывод целиком).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = os.environ.get("CROPWISE_BASE_URL", "https://operations.cropwise.com/api/v3").rstrip("/")
TOKEN = (os.environ.get("CROPWISE_API_TOKEN") or os.environ.get("API_TOKEN") or "").strip()
TZ = timezone(timedelta(hours=3))
REPORT: dict = {"base": BASE, "started": datetime.now(TZ).isoformat(), "tests": {}}
RATE_HEADERS: dict[str, str] = {}
_REQ = {"n": 0}


def ts(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%Y-%m-%dT%H:%M:%S%z")[:-2] + ":" + dt.astimezone(TZ).strftime("%z")[-2:]


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def call(method: str, path: str, params: dict | None = None, body=None, *, retries: int = 4):
    """-> (status, json|None, seconds). 429/5xx повторяются с паузой."""
    url = f"{BASE}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-User-Api-Token": TOKEN, "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {}),
        })
        t0 = time.monotonic()
        _REQ["n"] += 1
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                for k, v in r.headers.items():
                    if k.lower().startswith(("x-ratelimit", "ratelimit", "retry-after", "x-rate")):
                        RATE_HEADERS[k] = v
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None), time.monotonic() - t0
        except urllib.error.HTTPError as e:
            for k, v in e.headers.items():
                if k.lower().startswith(("x-ratelimit", "ratelimit", "retry-after", "x-rate")):
                    RATE_HEADERS[k] = v
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))
                continue
            try:
                payload = json.loads(e.read() or b"null")
            except Exception:
                payload = None
            return e.code, payload, time.monotonic() - t0
        except Exception as e:  # сеть
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            return -1, {"error": repr(e)}, time.monotonic() - t0
    return -1, None, 0.0


def meta_of(body) -> dict:
    return (body or {}).get("meta", {}) if isinstance(body, dict) else {}


def rows_of(body) -> list:
    d = body.get("data") if isinstance(body, dict) else None
    return d if isinstance(d, list) else []


def cursor_all(path: str, params: dict, *, limit: int = 1000, cap_pages: int = 400) -> tuple[list[dict], int, int]:
    """Выгрузка по from_id до пустой страницы. -> (rows, pages, max_page_size)."""
    out: list[dict] = []
    from_id, pages, biggest = 0, 0, 0
    while pages < cap_pages:
        st, body, _ = call("GET", path, {**params, "from_id": from_id, "limit": limit})
        if st != 200:
            raise RuntimeError(f"{path}: HTTP {st}")
        chunk = rows_of(body)
        pages += 1
        if not chunk:
            break
        biggest = max(biggest, len(chunk))
        key = lambda r: r.get("id") if isinstance(r, dict) else r  # noqa: E731
        if out and key(chunk[0]) == key(out[-len(chunk)] if len(out) >= len(chunk) else out[0]) and pages > 1 and key(chunk[-1]) == key(out[-1]):
            raise RuntimeError(f"{path}: from_id игнорируется (страница повторилась)")
        out.extend(chunk)
        last = meta_of(body).get("response", {}).get("last_record_id") or chunk[-1].get("id")
        from_id = int(last) + 1
    return out, pages, biggest


class Section:
    def __init__(self, key: str, title: str):
        self.key, self.title, self.data = key, title, {}
        print(f"\n=== {title} ===", flush=True)

    def info(self, msg: str, **kw):
        print(f"  • {msg}", flush=True)
        self.data.update(kw)

    def ok(self, msg: str, **kw):
        print(f"  ✔ {msg}", flush=True)
        self.data.update(kw)

    def bad(self, msg: str, **kw):
        print(f"  ✘ {msg}", flush=True)
        self.data.update(kw)

    def done(self):
        REPORT["tests"][self.key] = self.data


def guarded(fn):
    def wrap(*a, **k):
        try:
            fn(*a, **k)
        except Exception as e:  # один сбой не должен ронять остальные пробы
            print(f"  ✘ тест упал: {e!r}", flush=True)
            REPORT["tests"][fn.__name__] = {"error": repr(e)}
    return wrap


# --------------------------------------------------------------------------- тесты
@guarded
def t0_connect():
    s = Section("connect", "0. Доступ")
    st, body, dt = call("GET", "machine_tasks", {"limit": 1})
    s.data["status"] = st
    if st != 200:
        s.bad(f"machine_tasks → HTTP {st}: {json.dumps(body, ensure_ascii=False)[:200]}")
        s.done()
        print("\nБез доступа дальше проверять нечего.")
        write_report()
        sys.exit(1)
    s.ok(f"machine_tasks?limit=1 → 200 за {dt:.2f} с; ключи meta: {sorted(meta_of(body))}, "
         f"meta.response: {sorted(meta_of(body).get('response', {}))}")
    s.done()


@guarded
def t1_page_size():
    s = Section("page_size", "1. Реальный размер страницы (limit)")
    since = ts(datetime.now(TZ) - timedelta(days=60))
    for lim in (100, 500, 1000, 5000):
        st, body, dt = call("GET", "machine_tasks", {"limit": lim, "updated_at_gt_eq": since})
        n = len(rows_of(body)) if st == 200 else None
        s.info(f"limit={lim}: HTTP {st}, получено {n} за {dt:.2f} с", **{f"limit_{lim}": n})
    s.info("Если для limit=1000 пришло ~100, страница реально 100: 68 тыс. заданий = ~680 запросов.")
    s.done()


WINDOW: dict = {}


@guarded
def t2_changes_ids(hours: float):
    s = Section("changes_ids", f"2. machine_tasks/changes_ids за последние {hours:g} ч")
    now = datetime.now(TZ)
    frm = now - timedelta(hours=hours)
    st, body, dt = call("GET", "machine_tasks/changes_ids", {"from_time": ts(frm), "to_time": ts(now)})
    s.data["status"] = st
    if st != 200:
        s.bad(f"HTTP {st}: {json.dumps(body, ensure_ascii=False)[:300]}")
        return s.done()
    rows = rows_of(body)
    m = meta_of(body)
    ups = [parse(r.get("updated_at")) for r in rows]
    asc = all(a <= b for a, b in zip(ups, ups[1:]) if a and b)
    desc = all(a >= b for a, b in zip(ups, ups[1:]) if a and b)
    order = "asc" if asc and not desc else "desc" if desc and not asc else ("все равны/1 запись" if asc and desc else "не отсортировано")
    ids = [int(r["id"]) for r in rows]
    s.info(f"HTTP 200 за {dt:.2f} с; записей {len(rows)}, уникальных id {len(set(ids))}; порядок по updated_at: {order}",
           rows=len(rows), unique=len(set(ids)), order=order)
    s.info(f"meta: {json.dumps(m, ensure_ascii=False)[:420]}", meta=m)
    s.info(f"пример записи: {json.dumps(rows[0], ensure_ascii=False) if rows else '—'}")
    lim = m.get("response", {}).get("limit")
    s.info(f"максимальный limit по умолчанию: {lim}" + ("  ← окно упёрлось в лимит, пагинация нужна" if lim and len(rows) >= int(lim) else ""))
    WINDOW.update(frm=frm, to=now, ids=set(ids), order=order, rows=rows, limit=lim)

    # сверка с обычным списком по updated_at_gt_eq
    try:
        lst, pages, big = cursor_all("machine_tasks", {"updated_at_gt_eq": ts(frm), "updated_at_lt": ts(now)})
        a = {int(r["id"]) for r in lst}
        diff = a ^ set(ids)
        s.info(f"список machine_tasks?updated_at_gt_eq/lt: {len(a)} заданий ({pages} стр., макс. страница {big}); "
               f"расхождение с changes_ids: {len(diff)}", list_count=len(a), diff=len(diff))
        (s.ok if not diff else s.bad)(
            "changes_ids и фильтр updated_at дают один набор" if not diff
            else f"наборы различаются: только в списке {len(a - set(ids))}, только в changes_ids {len(set(ids) - a)} "
                 f"(правки между запросами тоже дают небольшой разрыв)")
    except Exception as e:
        s.bad(f"сверка со списком не удалась: {e!r}")
    s.done()


@guarded
def t3_pagination():
    s = Section("changes_pagination", "3. Постраничность changes_ids (limit=5): нет ли потерь на стыках")
    if not WINDOW.get("ids"):
        s.info("в окне нет изменений — расширьте окно: --hours 72")
        return s.done()
    ref = WINDOW["ids"]
    frm, to, order = WINDOW["frm"], WINDOW["to"], WINDOW["order"]
    if len(ref) < 12:
        s.info(f"в окне всего {len(ref)} записей — мало для проверки стыков; расширьте --hours")
    got: list[int] = []
    pages = 0
    stuck = False
    cur_from, cur_to = frm, to
    # стратегия по порядку: asc → сдвигаем from_time вверх, desc → to_time вниз
    while pages < 400:
        st, body, _ = call("GET", "machine_tasks/changes_ids",
                           {"from_time": ts(cur_from), "to_time": ts(cur_to), "limit": 5})
        if st != 200:
            s.bad(f"страница {pages + 1}: HTTP {st}")
            break
        rows = rows_of(body)
        pages += 1
        if not rows:
            break
        got.extend(int(r["id"]) for r in rows)
        last = parse(rows[-1].get("updated_at"))
        if last is None or len(rows) < 5:
            break
        if order == "desc":
            if last >= cur_to:
                stuck = True
                s.bad("курсор не продвигается (to_time=last_record_time): на одной секунде записей больше размера страницы")
                break
            cur_to = last
        else:
            if last <= cur_from:
                stuck = True
                s.bad("курсор не продвигается (from_time=last_record_time): на одной секунде записей больше размера страницы")
                break
            cur_from = last
    uniq = set(got)
    missing, dups = ref - uniq, len(got) - len(uniq)
    s.info(f"страниц {pages}; получено {len(got)}, уникальных {len(uniq)}, дублей {dups}; эталон {len(ref)}; "
           f"потеряно {len(missing)}", pages=pages, got=len(got), dups=dups, missing=len(missing))
    if not missing:
        s.ok(f"пагинация сдвигом времени ({'to_time' if order == 'desc' else 'from_time'} = updated_at последней записи) "
             f"потерь не дала")
    else:
        s.bad(f"ПОТЕРЯНО {len(missing)} id (стык страниц на одной секунде{", курсор застрял" if stuck else ""}). "
              f"Сдвиг времени как курсор НЕ безопасен — оставляем from_id + updated_at_gt_eq.", lost=sorted(missing)[:20])

    # поддерживает ли changes_ids курсор from_id?
    ids_sorted = sorted(ref)
    if len(ids_sorted) > 4:
        mid = ids_sorted[len(ids_sorted) // 2]
        st, body, _ = call("GET", "machine_tasks/changes_ids",
                           {"from_time": ts(frm), "to_time": ts(to), "from_id": mid})
        n = len(rows_of(body)) if st == 200 else None
        s.info(f"from_id={mid} у changes_ids: HTTP {st}, записей {n} из {len(ref)} — "
               + ("курсор ИГНОРИРУЕТСЯ" if n == len(ref) else "курсор работает" if n is not None else "ошибка"),
               from_id_supported=(n is not None and n != len(ref)))
    s.done()


@guarded
def t4_changes_full():
    s = Section("changes_full", "4. machine_tasks/changes (полные объекты)")
    now = datetime.now(TZ)
    st, body, dt = call("GET", "machine_tasks/changes",
                        {"from_time": ts(now - timedelta(hours=24)), "to_time": ts(now), "limit": 3})
    rows = rows_of(body) if st == 200 else []
    s.info(f"HTTP {st} за {dt:.2f} с, записей {len(rows)}; полей в объекте: {len(rows[0]) if rows else '—'}")
    if rows:
        s.info(f"есть updated_at/work_type_id/end_time/season: "
               f"{all(k in rows[0] for k in ('updated_at', 'work_type_id', 'end_time', 'season'))}")
    s.done()


@guarded
def t5_ids_endpoint():
    s = Section("ids", "5. machine_tasks/ids — список существующих id (для поиска удалений)")
    now = datetime.now(TZ)
    params = {"end_time_gt_eq": ts(now - timedelta(days=3)), "end_time_lt": ts(now + timedelta(days=1))}
    st, body, dt = call("GET", "machine_tasks/ids", {**params, "limit": 1000})
    s.data["status"] = st
    if st != 200:
        s.bad(f"HTTP {st}: {json.dumps(body, ensure_ascii=False)[:300]}")
        return s.done()
    rows = rows_of(body)
    shape = type(rows[0]).__name__ if rows else "—"
    s.info(f"HTTP 200 за {dt:.2f} с; записей {len(rows)}; элемент: {shape} "
           f"{json.dumps(rows[0], ensure_ascii=False) if rows else ''}; meta: {json.dumps(meta_of(body), ensure_ascii=False)[:300]}")
    try:
        got_rows, ipages, ibig = cursor_all("machine_tasks/ids", params)
    except RuntimeError as e:
        s.bad(f"полная выгрузка /ids по from_id невозможна: {e}")
        return s.done()
    ids_all = {int(r["id"] if isinstance(r, dict) else r) for r in got_rows}
    lst, pages, big = cursor_all("machine_tasks", params)
    ref = {int(r["id"]) for r in lst}
    s.info(f"/ids: {len(ids_all)} id за {ipages} стр. (макс. страница {ibig}); список: {len(ref)} заданий за {pages} стр.",
           ids_pages=ipages, list_pages=pages)
    (s.ok if ids_all == ref else s.bad)(
        "/ids учитывает фильтры (end_time), пагинируется по from_id и совпадает со списком" if ids_all == ref
        else f"/ids расходится со списком на {len(ids_all ^ ref)} id (фильтры игнорируются или иная пагинация)",
        filters_respected=(ids_all == ref))
    s.done()


@guarded
def t6_versions():
    s = Section("versions", "6. /versions — журнал удалений (event=destroy)")
    since = ts(datetime.now(TZ) - timedelta(days=30))
    for item in ("MachineTask", "MachineTaskFieldMappingItem"):
        st, body, dt = call("GET", "versions", {"item_type": item, "event": "destroy",
                                                "created_at_gt_eq": since, "limit": 1000})
        if st != 200:
            s.bad(f"{item}: HTTP {st}: {json.dumps(body, ensure_ascii=False)[:200]}", **{f"{item}_status": st})
            continue
        rows = rows_of(body)
        s.info(f"{item}: HTTP 200, destroy-записей за 30 сут: {len(rows)}"
               f"{' (страница полная — их больше)' if len(rows) >= 100 else ''}", **{f"{item}_destroy_30d": len(rows)})
        if rows:
            r = rows[0]
            snap = r.get("snapshot_before_change")
            s.info(f"  ключи: {sorted(r)}; snapshot_before_change: {type(snap).__name__}"
                   + (f", machine_task_id в снимке: {snap.get('machine_task_id') if isinstance(snap, dict) else '—'}"
                      if item.endswith("Item") else ""))
        # проверка, что фильтр по created_at действительно работает
        st2, body2, _ = call("GET", "versions", {"item_type": item, "event": "destroy", "limit": 1000})
        if st2 == 200:
            n_all = len(rows_of(body2))
            s.info(f"  без фильтра по дате (одна страница): {n_all} — "
                   + ("фильтр сужает выборку" if n_all > len(rows) else "фильтр ничего не сузил/записей мало"))
    s.done()


@guarded
def t7_mtfmi(sample: int):
    s = Section("mtfmi", "7. machine_task_field_mapping_items: changes_ids, лимиты фильтра, mass_request")
    now = datetime.now(TZ)
    st, body, dt = call("GET", "machine_task_field_mapping_items/changes_ids",
                        {"from_time": ts(now - timedelta(hours=24)), "to_time": ts(now), "limit": 1000})
    s.info(f"changes_ids за 24 ч: HTTP {st}, записей {len(rows_of(body)) if st == 200 else '—'}, за {dt:.2f} с")

    # реальные id заданий
    tasks, _, _ = cursor_all("machine_tasks", {"end_time_gt_eq": ts(now - timedelta(days=20)),
                                                "end_time_lt": ts(now)}, limit=1000, cap_pages=12)
    tids = [int(t["id"]) for t in tasks][:2000]
    s.info(f"взято {len(tids)} реальных id заданий для проб")
    if len(tids) < 20:
        return s.done()
    best_ok = 0
    for n in (50, 300, 600, 1000, 1500, 2000):
        if n > len(tids):
            break
        t0 = time.monotonic()
        st, body, dt = call("GET", "machine_task_field_mapping_items",
                            {"machine_task_id": ",".join(map(str, tids[:n])), "limit": 1000})
        got = len(rows_of(body)) if st == 200 else None
        s.info(f"machine_task_id × {n}: HTTP {st}, строк на 1-й странице {got}, {dt:.2f} с")
        if st == 200:
            best_ok = n
        else:
            break
    s.data["max_task_ids_per_filter_ok"] = best_ok
    s.info(f"максимум значений в фильтре, прошедший без ошибки: {best_ok} (сейчас в коде 300)")

    # mass_request по id строк mtfmi
    st, body, _ = call("GET", "machine_task_field_mapping_items", {"machine_task_id": ",".join(map(str, tids[:30])), "limit": 20})
    mt_ids = [int(r["id"]) for r in rows_of(body)][:15]
    if mt_ids:
        st, mb, dt = call("POST", "machine_task_field_mapping_items/mass_request", body={"data": mt_ids})
        got = len(rows_of(mb)) if st == 200 else None
        s.info(f"mass_request по {len(mt_ids)} id строк mtfmi: HTTP {st}, вернул {got} за {dt:.2f} с "
               "(mass_request берёт id самих строк, НЕ machine_task_id)")
    s.done()


@guarded
def t8_burst(n: int):
    s = Section("burst", f"8. Лимиты запросов: {n} одновременных GET")
    codes: dict[int, int] = {}
    lock = threading.Lock()

    def one(_):
        st, _b, dt = call("GET", "machine_tasks", {"limit": 1}, retries=0)
        with lock:
            codes[st] = codes.get(st, 0) + 1
        return dt

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=n) as ex:
        times = list(ex.map(one, range(n)))
    wall = time.monotonic() - t0
    s.info(f"коды ответов: {codes}; wall {wall:.1f} с; медиана {sorted(times)[len(times) // 2]:.2f} с",
           codes=codes, wall=round(wall, 2))
    if codes.get(429):
        s.bad(f"429 уже при {n} параллельных — потолок параллелизма где-то ниже")
    else:
        s.ok(f"{n} параллельных без 429 — запас для увеличения семафоров есть (проверьте и на пачке реальных страниц)")
    s.done()


def write_report():
    REPORT["finished"] = datetime.now(TZ).isoformat()
    REPORT["requests_total"] = _REQ["n"]
    REPORT["rate_limit_headers"] = RATE_HEADERS
    path = Path(__file__).with_name("probe_report.json")
    path.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nОтчёт: {path}  (запросов выполнено: {_REQ['n']})")
    print("Заголовки лимитов в ответах:", RATE_HEADERS or "не обнаружены")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24, help="окно изменений для changes_ids")
    ap.add_argument("--burst", type=int, default=0, help="сколько одновременных запросов для пробы лимитов (0 = пропустить)")
    args = ap.parse_args()
    if not TOKEN:
        sys.exit("Нужна переменная окружения CROPWISE_API_TOKEN")
    print(f"База: {BASE}; окно {args.hours:g} ч. Токен не выводится.")
    t0_connect()
    t1_page_size()
    t2_changes_ids(args.hours)
    t3_pagination()
    t4_changes_full()
    t5_ids_endpoint()
    t6_versions()
    t7_mtfmi(2000)
    if args.burst > 0:
        t8_burst(args.burst)
    write_report()


if __name__ == "__main__":
    main()
