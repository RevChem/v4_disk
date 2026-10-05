"""Запуск AI-разбора для job proverka."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

ProgressFn = Callable[[str], None] | None


def run_ai_for_job(job_dir: Path, prog: ProgressFn = None) -> dict[str, Any]:
    from proverka.ai.digest import load_batch_from_job_dir
    from proverka.ai.analyst import run_ai_analysis

    _prog = prog or (lambda _: None)
    _prog("AI: загрузка batch JSON…")
    batch = load_batch_from_job_dir(job_dir)
    if not batch:
        return {
            "ai_status": "failed",
            "ai_error": "reconcile_all_*.json not found",
            "ai_violation_count": 0,
        }

    _prog("AI: digest + domain rules + DeepSeek…")
    analysis = run_ai_analysis(batch)

    ai_report_path = job_dir / "ai_report.json"
    ai_report_path.write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    vc = int(analysis.get("violation_count") or 0)
    status = "completed" if analysis.get("ok") else "failed"
    return {
        "ai_status": status,
        "ai_violation_count": vc,
        "ai_report": {
            "executive_summary": (analysis.get("report") or {}).get("executive_summary"),
            "violation_count": vc,
            "findings": (analysis.get("report") or {}).get("findings") or [],
            "markdown": analysis.get("markdown"),
            "llm_used": analysis.get("llm_used"),
            "model": analysis.get("model"),
            "error": analysis.get("error"),
        },
        "ai_error": analysis.get("error"),
    }
