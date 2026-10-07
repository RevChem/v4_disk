#!/usr/bin/env python3
"""Сформировать все сводки за дату, затем proverka (docker exec)."""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PLAN = BACKEND.parent / "svodki" / "plan"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(PLAN))

REPORT_DATE = (
    date.fromisoformat(os.environ["REPORT_DATE"].split("T")[0])
    if os.environ.get("REPORT_DATE")
    else datetime.now(timezone(timedelta(hours=3))).date()
)


def main() -> int:
    from app.legacy.report_runner import generate_reports_to_disk

    print(f"=== Генерация сводок {REPORT_DATE.isoformat()} ===", flush=True)
    created, failed, logs = generate_reports_to_disk(REPORT_DATE, None)
    print(f"Создано: {len(created)}, ошибок: {len(failed)}", flush=True)
    for item in failed:
        print(f"  FAIL {item.get('report_id')}: {item.get('error', '')[:200]}", flush=True)
    for line in logs[-30:]:
        print(f"  LOG {line}", flush=True)

    upload = Path(os.environ.get("UPLOAD_DIR", "/app/uploads"))
    day_dir = upload / REPORT_DATE.isoformat()
    n_files = len(list(day_dir.glob("*"))) if day_dir.is_dir() else 0
    print(f"Файлов в uploads/{REPORT_DATE}: {n_files}", flush=True)

    from proverka.reconcile import load_env
    from proverka.reports_config import list_report_ids
    from proverka.runner import run_proverka_batch
    from proverka.summary import build_rows, compute_violations

    load_env()
    cookie = os.environ.get("CROPWISE_SESSION_COOKIE", "").strip()
    if not cookie:
        print("ERROR: нет CROPWISE_SESSION_COOKIE", flush=True)
        return 1

    out_dir = upload / "proverka" / f"cli_{REPORT_DATE.isoformat()}_fix"
    out_dir.mkdir(parents=True, exist_ok=True)
    rids = list_report_ids()
    print(f"=== Proverka {REPORT_DATE.isoformat()} ({len(rids)} сводок) ===", flush=True)

    def prog(msg: str) -> None:
        print(f"PROGRESS: {msg}", flush=True)

    result = run_proverka_batch(
        REPORT_DATE,
        rids,
        out_dir,
        cookie=cookie,
        compare_cropwise=True,
        write_excel=True,
        write_json=True,
        progress=prog,
    )
    batch_path = out_dir / f"reconcile_all_{REPORT_DATE.isoformat()}.json"
    batch = json.loads(batch_path.read_text(encoding="utf-8")) if batch_path.is_file() else result
    rows = build_rows(batch)
    cc = batch.get("cross_check") or result.get("cross_check") or {}
    viol = compute_violations(rows, cc.get("operations") or [], sheet_checks=batch.get("sheet_checks") or [])
    summary = {
        "report_date": REPORT_DATE.isoformat(),
        "generation_created": len(created),
        "generation_failed": len(failed),
        "upload_files": n_files,
        "exit_code": result.get("exit_code"),
        "cross_check_summary": cc.get("summary") or {},
        "violation_count": viol.get("violation_count"),
        "excel_path": result.get("excel_path"),
        "out_dir": str(out_dir),
    }
    (out_dir / "cli_summary_after_gen.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("DONE", json.dumps(summary, ensure_ascii=False), flush=True)
    return int(result.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
