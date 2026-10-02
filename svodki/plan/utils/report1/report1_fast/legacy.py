"""Загрузка эталонного скрипта 1_main_ammonium__1_.py как модуля."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_LEGACY: ModuleType | None = None


def load_legacy() -> ModuleType:
    global _LEGACY
    if _LEGACY is not None:
        return _LEGACY

    script_name = "1_main_ammonium__1_.py"
    here = Path(__file__).resolve()
    # Скрипт лежит в svodki/plan/, а этот файл — в svodki/plan/utils/report1/report1_fast/:
    # поднимаемся по родителям, пока не найдём (раньше брался только parent.parent).
    script_path = None
    for parent in here.parents:
        cand = parent / script_name
        if cand.is_file():
            script_path = cand
            break
    if script_path is None:
        raise FileNotFoundError(
            f"Эталон сводки 1 не найден ({script_name}) выше {here.parent}"
        )

    mod_name = "report1_legacy_ammonium"
    spec = importlib.util.spec_from_file_location(mod_name, script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить {script_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    _LEGACY = module
    return module
