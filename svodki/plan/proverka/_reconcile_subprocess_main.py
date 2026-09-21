#!/usr/bin/env python3
"""Worker entry: одна сводка reconcile_one в отдельном OS-процессе (Celery-safe)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

PLAN_DIR = Path(__file__).resolve().parent.parent
if str(PLAN_DIR) not in sys.path:
    sys.path.insert(0, str(PLAN_DIR))

from plan_paths import ensure_plan_sys_path  # noqa: E402

ensure_plan_sys_path()

from proverka.runner import _reconcile_worker  # noqa: E402


def main() -> None:
    payload = tuple(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    rid, result = _reconcile_worker(payload)
    Path(sys.argv[2]).write_text(
        json.dumps([rid, result], ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
