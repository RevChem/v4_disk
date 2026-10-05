#!/usr/bin/env python3
"""
Одноразовая сверка для check12: оконная параллельная выгрузка против полной
последовательной. Нужна, чтобы убедиться, что новая схема загрузки отдаёт
ровно те же данные, что и старая.

Что сверяется:
  1. machine_tasks:  оконная (created_at) выгрузка vs один курсор по диапазону сезонов
  2. mapping_items:  оконная выгрузка vs полный курсор
  3. (--transit) транзитные пары: выгрузка mtfmi по пачкам machine_task_id vs полный
     курсор по всей таблице mtfmi (долго!), на выборке задач

Как исключается дрейф данных: фиксируется момент T0 (сейчас минус --lag-hours),
обе выгрузки ограничиваются created_at_lt=T0. Записи, созданные во время
долгой полной выгрузки, в сравнении не участвуют. Остаточные расхождения
возможны только из-за правок/удалений старых записей за время прогона
(единицы, а не сотни).

Запуск (только стандартная библиотека, Python 3.9+):
    export CROPWISE_TOKEN="ваш_токен"
    python verify_check12_windows.py                    # tasks + mapping_items
    python verify_check12_windows.py --transit          # + проверка транзита (долго)
    python verify_check12_windows.py --skip-tasks       # только mapping_items

Токен не хранится в скрипте. Скрипт только читает данные (GET).
Время: полная выгрузка задач ~11 минут, mapping_items сопоставимо; их полные
курсоры идут одновременно. --transit добавляет полный проход по mtfmi.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

FMT = "%Y-%m-%dT%H:%M:%S"
PAGE = 1000


class Api:
    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/"), token

    def get(self, endpoint: str, params: dict, retries: int = 6) -> dict:
        url = f"{self.base}/{endpoint}?{urllib.parse.urlencode(params)}"
        last = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers={"X-User-Api-Token": self.token})
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(min(2 ** attempt, 32))
                    last = e
                    continue
                raise SystemExit(f"HTTP {e.code} для {url}\n{e.read().decode('utf-8', 'replace')[:300]}")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last = e
                time.sleep(min(2 ** attempt, 32))
        raise SystemExit(f"Не удалось выполнить {url}: {last}")

    def scan(self, endpoint: str, params: dict, project, label: str = "", progress: int = 0) -> list:
        """Курсор from_id; project(records) -> list применяется к каждой странице."""
        out, from_id, pages = [], 0, 0
        while True:
            body = self.get(endpoint, {**params, "from_id": from_id, "limit": PAGE})
            chunk = body.get("data", [])
            if not chunk:
                break
            pages += 1
            out.extend(project(chunk))
            meta = body.get("meta", {}).get("response", {})
            if progress and pages % progress == 0:
                print(f"    [{label}] страниц {pages}, записей {len(out)}", flush=True)
            if meta.get("obtained_records", len(chunk)) < (meta.get("limit") or PAGE) or not meta.get("last_record_id"):
                break
            from_id = meta["last_record_id"] + 1
        return out


def make_windows(start: datetime, step_days: int, t0: datetime) -> list[dict]:
    """Точное разбиение (-inf, T0): первое окно открыто слева, стыки делят одну строку границы."""
    bounds, cur = [], start
    while cur < t0:
        bounds.append(cur.strftime(FMT))
        cur += timedelta(days=step_days)
    t0s = t0.strftime(FMT)
    wins = [{"lt": bounds[0]}]
    wins += [{"gt_eq": a, "lt": b} for a, b in zip(bounds, bounds[1:])]
    wins.append({"gt_eq": bounds[-1], "lt": t0s})
    return wins


def windowed(api: Api, endpoint: str, wins: list[dict], params: dict, project, workers: int, label: str):
    def one(w):
        p = dict(params)
        if "gt_eq" in w:
            p["created_at_gt_eq"] = w["gt_eq"]
        if "lt" in w:
            p["created_at_lt"] = w["lt"]
        return api.scan(endpoint, p, project)
    t = time.time()
    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, part in enumerate(ex.map(one, wins), 1):
            out.extend(part)
            if i % 20 == 0:
                print(f"    [{label}] окон {i}/{len(wins)}", flush=True)
    return out, time.time() - t


def compare(name: str, full: dict, win: dict) -> bool:
    only_full = set(full) - set(win)
    only_win = set(win) - set(full)
    diff_val = [k for k in set(full) & set(win) if full[k] != win[k]]
    ok = not only_full and not only_win and not diff_val
    print(f"\n--- {name} ---")
    print(f"  полная выгрузка: {len(full)} | оконная: {len(win)}")
    print(f"  только в полной: {len(only_full)} | только в оконной: {len(only_win)} | разные значения: {len(diff_val)}")
    for title, ids in (("только в полной", only_full), ("только в оконной", only_win), ("разные значения", diff_val)):
        if ids:
            print(f"    примеры ({title}): {sorted(ids)[:10]}")
    print(f"  РЕЗУЛЬТАТ: {'СОВПАДАЕТ' if ok else 'ЕСТЬ РАСХОЖДЕНИЯ'}")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token", default=os.environ.get("CROPWISE_TOKEN"))
    ap.add_argument("--base-url", default=os.environ.get("CROPWISE_BASE_URL", "https://operations.cropwise.com/api/v3"))
    ap.add_argument("--seasons", default="2025,2026,2027")
    ap.add_argument("--window-start", default="2023-09-01")
    ap.add_argument("--step-days", type=int, default=14)
    ap.add_argument("--workers", type=int, default=12, help="параллельных окон")
    ap.add_argument("--lag-hours", type=float, default=6.0, help="T0 = сейчас минус N часов")
    ap.add_argument("--skip-tasks", action="store_true")
    ap.add_argument("--skip-mapping", action="store_true")
    ap.add_argument("--transit", action="store_true", help="проверить и транзитные пары (долго)")
    ap.add_argument("--transit-sample", type=int, default=3000)
    args = ap.parse_args()
    if not args.token:
        sys.exit("Нужен токен: переменная CROPWISE_TOKEN или --token")

    api = Api(args.base_url, args.token)
    seasons = sorted({int(s) for s in args.seasons.split(",") if s.strip()})
    smin, smax = seasons[0], seasons[-1]
    t0 = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=args.lag_hours)
    t0s = t0.strftime(FMT)
    start = datetime.fromisoformat(args.window_start)
    wins = make_windows(start, args.step_days, t0)
    print(f"T0 = {t0s}; окон: {len(wins)}; сезоны {seasons}; параллельных окон: {args.workers}\n")

    def proj_tasks(recs):
        return [(int(t["id"]), 1) for t in recs
                if t.get("id") is not None and str(t.get("season", "")).strip().isdigit()
                and int(str(t["season"]).strip()) in set(seasons)]

    def proj_map(recs):
        return [(int(i["id"]), (int(i["machine_task_id"]), int(i["agro_operation_id"]))) for i in recs
                if i.get("id") is not None and i.get("machine_task_id") is not None
                and i.get("agro_operation_id") is not None]

    results: list[bool] = []
    task_ids_full: set[int] = set()
    jobs = {}

    with ThreadPoolExecutor(max_workers=2) as full_pool:
        # Полные последовательные курсоры запускаем в фоне, пока считаем оконные
        if not args.skip_tasks:
            jobs["tasks"] = full_pool.submit(
                api.scan, "machine_tasks",
                {"season_gt_eq": smin, "season_lt_eq": smax, "created_at_lt": t0s},
                proj_tasks, "tasks-full", 20)
        if not args.skip_mapping:
            jobs["mapping"] = full_pool.submit(
                api.scan, "machine_task_agro_operation_mapping_items",
                {"created_at_lt": t0s}, proj_map, "mapping-full", 20)

        win_res = {}
        if not args.skip_tasks:
            print("Оконная выгрузка задач...")
            r, dt = windowed(api, "machine_tasks", wins, {"season_gt_eq": smin, "season_lt_eq": smax},
                             proj_tasks, args.workers, "tasks-win")
            win_res["tasks"] = (dict(r), dt)
            print(f"  готово: {len(r)} задач за {dt:.0f} c")
        if not args.skip_mapping:
            print("Оконная выгрузка mapping_items...")
            r, dt = windowed(api, "machine_task_agro_operation_mapping_items", wins, {},
                             proj_map, args.workers, "mapping-win")
            win_res["mapping"] = (dict(r), dt)
            print(f"  готово: {len(r)} записей за {dt:.0f} c")

        print("\nЖдём полные последовательные выгрузки (это самая долгая часть)...")
        for name, fut in jobs.items():
            t = time.time()
            full = dict(fut.result())
            print(f"  полная выгрузка {name}: {len(full)} за {time.time() - t:.0f} c (после оконной)")
            results.append(compare(
                "machine_tasks" if name == "tasks" else "mapping_items", full, win_res[name][0]))
            if name == "tasks":
                task_ids_full = set(full)

    # ------------------------------------------------------------------ транзит
    if args.transit:
        print("\n--- транзитные пары (mtfmi) ---")
        if not task_ids_full:
            print("  нужны задачи: не используйте --skip-tasks вместе с --transit")
        else:
            print("  Полный проход по machine_task_field_mapping_items (долго)...")
            t = time.time()
            full_transit = set(api.scan(
                "machine_task_field_mapping_items", {},
                lambda recs: [(int(i["machine_task_id"]), int(i["field_id"])) for i in recs
                              if i.get("in_transit") and i.get("machine_task_id") is not None
                              and i.get("field_id") is not None],
                "mtfmi-full", 50))
            print(f"  готово за {time.time() - t:.0f} c, транзитных пар в таблице: {len(full_transit)}")
            rnd = random.Random(42)
            transit_tasks = sorted({tid for tid, _ in full_transit} & task_ids_full)
            others = sorted(task_ids_full - set(transit_tasks))
            half = args.transit_sample // 2
            sample = rnd.sample(transit_tasks, min(half, len(transit_tasks))) + \
                rnd.sample(others, min(args.transit_sample - half, len(others)))
            print(f"  Выборка задач: {len(sample)} (из них с транзитом: {min(half, len(transit_tasks))})")

            batches = [sample[i:i + 300] for i in range(0, len(sample), 300)]

            def fetch_batch(b):
                return api.scan(
                    "machine_task_field_mapping_items", {"machine_task_id": ",".join(map(str, b))},
                    lambda recs: [(int(i["machine_task_id"]), int(i["field_id"])) for i in recs
                                  if i.get("in_transit") and i.get("machine_task_id") is not None
                                  and i.get("field_id") is not None])
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                batched = {p for part in ex.map(fetch_batch, batches) for p in part}
            sample_set = set(sample)
            expected = {p for p in full_transit if p[0] in sample_set}
            results.append(compare("транзитные пары (на выборке)",
                                   {p: 1 for p in expected}, {p: 1 for p in batched}))

    print("\n=== ИТОГ ===")
    if results and all(results):
        print("Все сверки СОВПАДАЮТ: оконная загрузка и фильтр транзита по пачкам не меняют результат.")
    elif results:
        print("ЕСТЬ РАСХОЖДЕНИЯ. Если их единицы - вероятно, правки/удаления за время прогона: "
              "повторите запуск. Если сотни - не включайте новую схему и пришлите вывод.")
    for name, (_, dt) in win_res.items():
        print(f"Оконная выгрузка {name}: {dt:.0f} c")


if __name__ == "__main__":
    main()
