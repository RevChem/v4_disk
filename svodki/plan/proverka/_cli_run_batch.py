#!/usr/bin/env python3
"""One-off CLI proverka run (docker exec)."""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

PLAN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLAN))
os.chdir(PLAN)

from proverka.reconcile import load_env  # noqa: E402
from proverka.reports_config import list_report_ids  # noqa: E402
from proverka.runner import run_proverka_batch  # noqa: E402
from proverka.summary import build_rows, compute_violations  # noqa: E402


def main() -> int:
    load_env()
    rd = os.environ.get("REPORT_DATE", date.today().isoformat())
    report_date = date.fromisoformat(rd.split("T")[0])
    cookie = os.environ.get("CROPWISE_SESSION_COOKIE", "").strip()
    upload = Path(os.environ.get("UPLOAD_DIR", "/app/uploads"))
    out_dir = upload / "proverka" / f"cli_{report_date.isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    rids = list_report_ids()

    def prog(msg: str) -> None:
        print(f"PROGRESS: {msg}", flush=True)

    print(f"report_date={report_date} reports={rids} out={out_dir}", flush=True)
    result = run_proverka_batch(
        report_date,
        rids,
        out_dir,
        cookie=cookie,
        compare_cropwise=True,
        write_excel=True,
        write_json=True,
        progress=prog,
    )
    batch_path = out_dir / f"reconcile_all_{report_date.isoformat()}.json"
    batch = json.loads(batch_path.read_text(encoding="utf-8")) if batch_path.is_file() else result
    rows = build_rows(batch)
    cc = batch.get("cross_check") or result.get("cross_check") or {}
    viol = compute_violations(
        rows,
        cc.get("operations") or [],
        sheet_checks=batch.get("sheet_checks") or [],
    )
    summary = {
        "report_date": report_date.isoformat(),
        "exit_code": result.get("exit_code"),
        "cross_check_summary": cc.get("summary") or {},
        "violation_count": viol.get("violation_count"),
        "cropwise_violation_count": viol.get("cropwise_violation_count"),
        "cross_check_violation_count": viol.get("cross_check_violation_count"),
        "excel_path": result.get("excel_path"),
        "json_path": str(batch_path),
        "violations": viol.get("violations") or [],
    }
    (out_dir / "cli_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("DONE", json.dumps({k: summary[k] for k in summary if k != "violations"}, ensure_ascii=False), flush=True)
    return int(result.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
