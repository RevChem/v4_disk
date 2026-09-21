"""Детализация area_today из блоков detail_own / detail_hired модулей сводок."""
from __future__ import annotations

from typing import Any


def _area_from_row(row: dict) -> float:
    for key in ("Площадь, га", "area", "Площадь"):
        if key in row and row[key] is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                pass
    return 0.0


def _machine_from_row(row: dict) -> str:
    for key in ("Машина", "machine", "Комбайн"):
        v = row.get(key)
        if v:
            return str(v).strip()
    return ""


def flatten_firm_details(
    all_details: dict[str, dict],
    firm_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Строки: агрофирма, машина, площадь, own/hired."""
    rows: list[dict[str, Any]] = []
    firms = firm_names or list(all_details.keys())
    for firm in firms:
        st = all_details.get(firm) or {}
        for block, kind in (("detail_own", "own"), ("detail_hired", "hired")):
            for r in st.get(block) or []:
                area = _area_from_row(r)
                if area <= 0:
                    continue
                machine = _machine_from_row(r)
                rows.append({
                    "firm": firm,
                    "machine": machine,
                    "area_ha": round(area, 2),
                    "kind": kind,
                    "source": "svodka",
                })
    rows.sort(key=lambda x: (-x["area_ha"], x["firm"], x["machine"]))
    return rows
