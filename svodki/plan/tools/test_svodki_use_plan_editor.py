"""Защита от повторения: сводка не должна читать план мимо «Редактора планов».

Скрипт сводки, который обращается к HARDCODED_PLAN / HARDCODED_PLAN_FLAT, обязан
пользоваться и функциями редактора (plan_ha_for_report, firm_group_plan_ha,
is_crop_user_excluded, is_crop_excluded_from_totals, crop_code_for_report,
is_crop_omitted, collapse_crop_areas). Иначе «Не считать», «Иные» и скрытие
культур на такую сводку не действуют.

Новая сводка с прямым чтением плана роняет тест. Когда сводку из KNOWN_UNFIXED
починят, тест потребует убрать её из списка.
"""
from __future__ import annotations

import ast
from pathlib import Path

PLAN_DIR = Path(__file__).resolve().parents[1]

RAW_PLAN_NAMES = {"HARDCODED_PLAN", "HARDCODED_PLAN_FLAT"}
EDITOR_AWARE_NAMES = {
    "plan_ha_for_report",
    "firm_group_plan_ha",
    "is_crop_user_excluded",
    "is_crop_excluded_from_totals",
    "crop_code_for_report",
    "is_crop_omitted",
    "collapse_crop_areas",
}

# Известный долг: читают план напрямую и фильтры редактора не применяют.
KNOWN_UNFIXED = {
    "0_main_status_vpr.py",
    "0_1_main_status_vpr_morning.py",
}


def _names_used(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            out.update(a.name for a in node.names)
            out.update(a.asname for a in node.names if a.asname)
    return out


def _svodka_scripts() -> list[Path]:
    return sorted(
        p for p in PLAN_DIR.glob("*.py")
        if p.name[0].isdigit() and p.name != "plans.py"
    )


def _violations() -> set[str]:
    bad: set[str] = set()
    for path in _svodka_scripts():
        used = _names_used(path)
        if used & RAW_PLAN_NAMES and not (used & EDITOR_AWARE_NAMES):
            bad.add(path.name)
    return bad


def test_new_svodka_does_not_read_plan_bypassing_editor():
    new = _violations() - KNOWN_UNFIXED
    assert not new, (
        f"Сводки читают HARDCODED_PLAN мимо редактора планов: {sorted(new)}. "
        "Берите план через plans.plan_ha_for_report / firm_group_plan_ha и "
        "исключайте культуры через is_crop_excluded_from_totals."
    )


def test_known_unfixed_list_is_not_stale():
    fixed = KNOWN_UNFIXED - _violations()
    assert not fixed, f"Убрать из KNOWN_UNFIXED (уже исправлены): {sorted(fixed)}"
