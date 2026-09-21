#!/usr/bin/env python3
"""Полная проверка MU и cross-check через run_proverka_batch."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

PLAN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLAN))

from proverka.runner import run_proverka_batch  # noqa: E402
from proverka.summary import build_rows  # noqa: E402


def main() -> int:
    cookie = (os.environ.get("CROPWISE_SESSION_COOKIE") or "").strip()
    if not cookie:
        print("ERROR: CROPWISE_SESSION_COOKIE", file=sys.stderr)
        return 1
    rd = date.fromisoformat(os.environ.get("REPORT_DATE", "2026-09-15"))
    report_ids = os.environ.get("REPORT_IDS", "1,2,3,4,5,6,7,8,9").split(",")
    report_ids = [x.strip() for x in report_ids if x.strip()]

    def prog(msg: str) -> None:
        print(msg, flush=True)

    with tempfile.TemporaryDirectory(prefix="verify_proverka_") as td:
        out = Path(td)
        result = run_proverka_batch(
            rd,
            report_ids,
            out,
            cookie=cookie,
            write_excel=False,
            write_json=False,
            progress=prog,
        )
        rows = result.get("summary_rows") or build_rows(result)
        print("\n=== Сводка vs Cropwise ===")
        mu_bad = False
        for r in rows:
            title = str(r.get("title") or "")
            rid = str(r.get("id") or "")
            is_mu = (
                "1.1" in title
                or "МУ" in title
                or "минераль" in title.lower()
                or rid in ("3",)
            )
            if not is_mu and rid not in ("1", "2"):
                continue
            sv = float(r.get("svodka_ha") or 0)
            cw = float(r.get("cropwise_ha") or 0)
            diff = float(r.get("diff_ha") or 0)
            st = r.get("status")
            print(f"  [{rid}] {title}: svodka={sv:.2f} cropwise={cw:.2f} diff={diff:+.2f} {st}")
            if is_mu and abs(diff) > 5.0:
                mu_bad = True
        cc = result.get("cross_check") or {}
        ops = cc.get("operations") if isinstance(cc, dict) else cc
        if ops:
            print("\n=== Cross-check (MU) ===")
            for row in ops:
                if not isinstance(row, dict):
                    continue
                op = str(row.get("operation") or row.get("group_name") or "")
                if "1.1" not in op:
                    continue
                print(f"  {row}")
                nums: list[float] = []
                for i in range(1, 10):
                    v = row.get(f"svodka_{i}") or row.get(f"r{i}")
                    if v in (None, "—", ""):
                        continue
                    try:
                        nums.append(float(v))
                    except (TypeError, ValueError):
                        pass
                if nums and max(nums) - min(nums) > 0.05:
                    mu_bad = True
        return 2 if mu_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
