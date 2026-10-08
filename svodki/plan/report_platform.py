"""Единая платформа: сезоны, культуры (plans_edits), env для всех сводок и proverka."""
from __future__ import annotations

import importlib
import json
import os
import uuid
from datetime import date
from pathlib import Path
from typing import Any


def upload_dir() -> Path:
    for key in ("SVODKI_UPLOAD_DIR", "UPLOAD_DIR"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            p = Path(raw)
            return p if p.is_absolute() else (Path.cwd() / p).resolve()
    root = Path(__file__).resolve().parent.parent.parent
    return (root / "uploads").resolve()


def season_ranges_path() -> Path:
    configured = (os.environ.get("SVODKI_SEASON_RANGES_PATH") or "").strip()
    if configured:
        return Path(configured)
    return upload_dir() / "season_ranges.json"


def plans_edits_path() -> Path:
    configured = (os.environ.get("SVODKI_PLANS_EDITS_PATH") or "").strip()
    if configured:
        return Path(configured)
    return upload_dir() / "plans_edits.json"


def load_plans_edits_store() -> dict[str, Any]:
    path = plans_edits_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_season_ranges_store() -> dict[str, Any]:
    path = season_ranges_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def reload_plans_module() -> Any:
    from plan_paths import ensure_plan_sys_path

    ensure_plan_sys_path()
    import plans

    return importlib.reload(plans)


def reload_editor_modules() -> None:
    """Подхватить правки редактора в уже запущенном процессе.

    Celery-worker импортирует plans и proverka.work_groups один раз.
    Без перезагрузки смена групп работ в проверку не попадает.
    """
    edits = plans_edits_path()
    if edits.is_file():
        os.environ["SVODKI_PLANS_EDITS_PATH"] = str(edits.resolve())
    reload_plans_module()
    import proverka.work_groups as work_groups

    importlib.reload(work_groups)


def bootstrap_platform(
    report_date: date,
    *,
    temp_dir: Path | None = None,
    do_reload_plans: bool = True,
) -> dict[str, str]:
    """
    Подготовить env как при генерации с сайта:
    season_ranges.json, plans_edits.json, REPORT_DATE.
    """
    from plan_paths import ensure_plan_sys_path

    ensure_plan_sys_path()
    ud = upload_dir()
    ud.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {}
    os.environ["REPORT_DATE"] = report_date.isoformat()
    env["REPORT_DATE"] = report_date.isoformat()

    os.environ["SVODKI_UPLOAD_DIR"] = str(ud)
    os.environ["UPLOAD_DIR"] = str(ud)
    env["SVODKI_UPLOAD_DIR"] = str(ud)

    sr = season_ranges_path()
    if sr.is_file():
        os.environ["SVODKI_SEASON_RANGES_PATH"] = str(sr.resolve())
        env["SVODKI_SEASON_RANGES_PATH"] = str(sr.resolve())

    edits = load_plans_edits_store()
    if edits:
        if temp_dir is not None:
            eff = temp_dir / f".plans_edits_{uuid.uuid4().hex}.json"
            eff.write_text(json.dumps(edits, ensure_ascii=False), encoding="utf-8")
            os.environ["SVODKI_PLANS_EDITS_PATH"] = str(eff)
            env["SVODKI_PLANS_EDITS_PATH"] = str(eff)
        else:
            pe = plans_edits_path()
            os.environ["SVODKI_PLANS_EDITS_PATH"] = str(pe.resolve())
            env["SVODKI_PLANS_EDITS_PATH"] = str(pe.resolve())
    else:
        os.environ.pop("SVODKI_PLANS_EDITS_PATH", None)

    if do_reload_plans:
        reload_plans_module()

    return env


def platform_summary() -> dict[str, Any]:
    """Краткий снимок активных настроек (для логов / proverka)."""
    edits = load_plans_edits_store()
    seasons = load_season_ranges_store()
    by_group = seasons.get("by_group") if isinstance(seasons.get("by_group"), dict) else seasons
    crop_hidden = sum(
        1
        for g, firms in (edits.get("CROP_FIRM_HIDDEN") or {}).items()
        if isinstance(firms, dict)
        for f, crops in firms.items()
        if isinstance(crops, dict)
        for c, v in crops.items()
        if int(v or 0) > 0
    )
    crop_rank = sum(
        1
        for g, crops in (edits.get("CROP_DISPLAY_RANK") or {}).items()
        if isinstance(crops, dict)
        for c, v in crops.items()
        if v is not None
    )
    return {
        "upload_dir": str(upload_dir()),
        "season_ranges_path": str(season_ranges_path()),
        "plans_edits_path": str(plans_edits_path()),
        "season_groups": len(by_group) if isinstance(by_group, dict) else 0,
        "crop_firm_hidden_pairs": crop_hidden,
        "crop_rank_entries": crop_rank,
    }
