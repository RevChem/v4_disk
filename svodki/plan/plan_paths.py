"""Пути svodki/plan для PYTHONPATH (генерация, proverka, subprocess)."""
from __future__ import annotations

from pathlib import Path

PLAN_ROOT = Path(__file__).resolve().parent

# Подкаталоги utils — каждый в PYTHONPATH для import excel_style, machine_tasks_batch_fetch, …
UTILS_SUBDIRS: tuple[str, ...] = (
    "utils/api",
    "utils/excel",
    "utils/season",
    "utils/report1",
    "utils/pdf",
    "utils/shared",
)


def plan_pythonpath_entries() -> list[Path]:
    """Корень plan + utils/* — порядок: plan, затем utils."""
    out: list[Path] = [PLAN_ROOT]
    for sub in UTILS_SUBDIRS:
        p = PLAN_ROOT / sub
        if p.is_dir():
            out.append(p)
    return out


def ensure_plan_sys_path() -> None:
    """Добавляет plan + utils/* в sys.path (proverka, редактор планов, in-process)."""
    import sys

    for entry in plan_pythonpath_entries():
        s = str(entry)
        if s not in sys.path:
            sys.path.insert(0, s)


def find_support_file(filename: str) -> Path | None:
    """Ищет вспомогательный модуль по имени файла в plan и utils/*."""
    candidates = [PLAN_ROOT / filename]
    for sub in UTILS_SUBDIRS:
        candidates.append(PLAN_ROOT / sub / filename)
    for path in candidates:
        if path.is_file():
            return path
    return None
