"""
Сводка 27.1 — рейтинг механизаторов по общему времени остановок.

Тот же расчёт и период, что у сводки 27, но вместо количества остановок
в колонках — суммарное время (чат-бот и Cropwise > 10 мин).

Запускается вместе со сводкой 27 (автодобавление в очереди генерации).
"""
from __future__ import annotations

import os
import runpy
from pathlib import Path

os.environ["REPORT27_METRIC"] = "duration"

_SCRIPT = Path(__file__).resolve().parent / "27_mechanizator_rating.py"

if __name__ == "__main__":
    runpy.run_path(str(_SCRIPT), run_name="__main__")
