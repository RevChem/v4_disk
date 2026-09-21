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

    plan_dir = Path(__file__).resolve().parent.parent
    script_path = plan_dir / "1_main_ammonium__1_.py"
    if not script_path.is_file():
        raise FileNotFoundError(f"Эталон сводки 1 не найден: {script_path}")

    mod_name = "report1_legacy_ammonium"
    spec = importlib.util.spec_from_file_location(mod_name, script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить {script_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    _LEGACY = module
    return module
