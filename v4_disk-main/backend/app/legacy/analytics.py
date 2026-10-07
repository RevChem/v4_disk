"""Тонкая обёртка над svodki/plan/utils/shared/svodki_analytics.py (путь к хранилищу — как у генерации сводок)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from app.config import settings

PLAN_DIR = Path(settings.svodki_root) / "plan"


def _store_dir() -> Path:
    raw = (settings.svodki_task_store_dir or "").strip()
    return Path(raw) if raw else Path(settings.upload_dir).resolve() / ".task_store"


def module():
    """Импорт svodki_analytics с настроенными sys.path и SVODKI_TASK_STORE_DIR."""
    if str(PLAN_DIR) not in sys.path:
        sys.path.insert(0, str(PLAN_DIR))
    from plan_paths import ensure_plan_sys_path

    ensure_plan_sys_path()
    os.environ.setdefault("SVODKI_TASK_STORE_DIR", str(_store_dir()))
    import svodki_analytics

    return svodki_analytics
