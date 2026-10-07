# -*- coding: utf-8 -*-
"""Ежедневный снимок итогов хранилища для аналитики («как менялись цифры за прошлые даты»).

Запуск из svodki/plan после ночной синхронизации хранилища:
    SVODKI_TASK_STORE_DIR=/app/uploads/.task_store python tools/analytics_snapshot.py [--days 120] [--date YYYY-MM-DD]
Пишет в <store_dir>/analytics.sqlite (отдельный файл, не стирается при пересборе store.sqlite).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PLAN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLAN))
import plans  # noqa: F401,E402  (добавляет utils/* в sys.path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120, help="сколько последних суток записывать в снимок")
    ap.add_argument("--date", default="", help="дата снимка (по умолчанию сегодня)")
    a = ap.parse_args()
    import svodki_analytics as an

    res = an.take_snapshot(days_back=a.days, snap_date=a.date or None)
    print(f"[analytics] снимок {res['snap_date']}: {res['rows']} строк (с {res['from']})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
