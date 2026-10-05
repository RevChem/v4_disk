# -*- coding: utf-8 -*-
"""Однократная синхронизация хранилища заданий (SQLite) — выполняется бэкендом перед пачкой сводок.

Делает ту же работу, что раньше делал каждый процесс сводки под блокировкой: докачка покрытия,
дельта по updated_at, журнал /versions, недавние сутки, сверка по /ids. После неё сводки с
SVODKI_STORE_READONLY=1 в API за заданиями не ходят.

Запуск из svodki/plan (env: SVODKI_TASK_STORE_DIR, CROPWISE_API_TOKEN, CROPWISE_BASE_URL,
REPORT_DATE, SVODKI_SEASON_START_DATE):
    python tools/sync_task_store.py [--from YYYY-MM-DD] [--wts 1,2,3]
Код возврата 0 — хранилище свежее; иначе бэкенд оставляет прежнее поведение (сводки синхронизируют сами).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import date
from pathlib import Path

PLAN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLAN))
import plans  # noqa: F401,E402  (добавляет utils/* в sys.path)

import httpx  # noqa: E402


async def _run(wts: set[int], since: float, base: str, token: str) -> int:
    from datetime import date  # noqa: F811
    import svodki_task_store as ts

    st = ts.get_store(base)
    if st is None:
        print("[sync] SVODKI_TASK_STORE_DIR не задан — нечего синхронизировать", flush=True)
        return 2
    headers = {"X-User-Api-Token": token} if token else {}
    sem = asyncio.Semaphore(int(os.environ.get("SVODKI_PREFETCH_CONCURRENCY", "20") or 20))
    t0 = time.monotonic()
    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        # Сначала каждый wt получает покрытие, затем один общий проход дельты (force).
        await st.ensure(client, sem, wts, since, "sync ", force=True)
        # Строки площади для новых заданий — здесь, чтобы сводки (READONLY) не брали замок записи.
        import mtfmi_batch_fetch as mb

        await st.fill_mtfmi(client, sem, "sync ", crops=mb._HISTORY_CROP, remember=mb.remember_history_crops)
        # Связи «задание → агрооперация» (mtaomi) и их привязки: дельта через «Changed objects» + перечитывание
        # изменённых заданий; сводка 2 берёт их из хранилища вместо выгрузки всего сезона.
        try:
            await st.sync_links(client, sem, "sync ")
        except Exception as exc:  # некритично: сводка 2 докачает связи сама
            print(f"[sync] связи агроопераций не обновлены: {exc}", flush=True)
        # Справочники (fields, history_items, ...) — один раз на пачку; сводки читают их из refs.sqlite.
        if os.environ.get("SVODKI_REF_CACHE_DISABLE", "").strip().lower() not in {"1", "true", "yes", "on"}:
            import svodki_ref_store as rs

            ry = date.fromisoformat((os.environ.get("REPORT_DATE") or date.today().isoformat())[:10]).year
            try:
                await rs.refresh_standard(client, sem, base, [ry - 1, ry, ry + 1], "sync ")
            except Exception as exc:  # некритично: сводки выгрузят справочники сами
                print(f"[sync] справочники не обновлены: {exc}", flush=True)
    print(f"[sync] готово: wt={len(wts)}, с {ts.fmt_ts(since)}, {time.monotonic() - t0:.1f} с", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="")
    ap.add_argument("--wts", default="")
    a = ap.parse_args()

    import svodki_task_store as ts

    rd_raw = (os.environ.get("REPORT_DATE") or "").strip()[:10]
    rd = date.fromisoformat(rd_raw) if rd_raw else date.today()
    raw_from = (a.date_from or os.environ.get("SVODKI_SEASON_START_DATE") or "").strip()[:10]
    if not raw_from:
        raw_from = f"{rd.year}-01-01"
    # Сводки 11, 12, 26 (комбайны): сезон с 10 июня; до 10 июня — прошлого года.
    # Берём более раннюю границу, чтобы покрыть окно всех сводок.
    combine = date(rd.year if (rd.month, rd.day) >= (6, 10) else rd.year - 1, 6, 10)
    raw_from = min(date.fromisoformat(raw_from), combine).isoformat()
    since = ts.parse_ts(raw_from + "T00:00:00")

    if a.wts.strip():
        wts = {int(x) for x in a.wts.split(",") if x.strip()}
    else:
        import svodki_prefetch as sp

        wts = {int(x) for x in sp._all_work_type_ids()}
    token = os.environ.get("CROPWISE_API_TOKEN") or os.environ.get("API_TOKEN") or ""
    base = (os.environ.get("CROPWISE_BASE_URL") or "https://operations.cropwise.com/api/v3").rstrip("/")
    return asyncio.run(_run(wts, since, base, token))


if __name__ == "__main__":
    raise SystemExit(main())
