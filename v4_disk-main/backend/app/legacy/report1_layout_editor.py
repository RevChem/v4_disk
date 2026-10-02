"""Редактор конфигурации сводки 1: порядок листов, культуры посева, PDF-подстановки."""
from __future__ import annotations

import importlib.util
import json
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.config import settings

_KNOWN_CROPS: tuple[str, ...] = (
    "Рапс яровой",
    "Рапс озимый",
    "Подсолнечник",
    "Соя",
    "Пшеница яровая",
    "Пшеница озимая",
    "Кукуруза на зерно",
    "Кукуруза на силос",
    "Тритикале озимая",
    "Горох",
    "Чечевица",
    "Лён",
    "Картофель",
    "Свекла столовая",
    "Пар",
    "Многолетние травы",
)


def _svodki_plan_dir() -> Path:
    root = Path(settings.svodki_root).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    return root / "plan"


def _load_layout_module():
    path = _svodki_plan_dir() / "report1_layout_config.py"
    if not path.is_file():
        raise FileNotFoundError(f"Не найден {path}")
    spec = importlib.util.spec_from_file_location("svodki_report1_layout_config", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _upload_dir() -> Path:
    upload = Path(settings.upload_dir).expanduser()
    if not upload.is_absolute():
        upload = (Path.cwd() / upload).resolve()
    return upload


def layout_edits_path() -> Path:
    return _upload_dir() / "report1_layout.json"


def load_layout_edits() -> dict[str, Any]:
    path = layout_edits_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_layout_edits(data: dict[str, Any]) -> Path:
    path = layout_edits_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    mod = _load_layout_module()
    existing = load_layout_edits()
    cleaned: dict[str, Any] = dict(existing)
    if "sheet_order" in data:
        normalized = mod._normalize_sheet_slots(data.get("sheet_order"))
        base = mod.default_layout_config()
        if normalized == mod._normalize_sheet_slots(base["sheet_order"]):
            cleaned.pop("sheet_order", None)
        else:
            cleaned["sheet_order"] = normalized
    if "seeding_crop_order" in data:
        normalized = mod._normalize_seeding_crops(data.get("seeding_crop_order"))
        base = mod.default_layout_config()
        if normalized == mod._normalize_seeding_crops(base["seeding_crop_order"]):
            cleaned.pop("seeding_crop_order", None)
        else:
            cleaned["seeding_crop_order"] = normalized
    if "pdf_grafts" in data:
        normalized = mod._normalize_pdf_grafts(data.get("pdf_grafts"))
        base = mod.default_layout_config()
        if normalized == mod._normalize_pdf_grafts(base["pdf_grafts"]):
            cleaned.pop("pdf_grafts", None)
        else:
            cleaned["pdf_grafts"] = normalized
    if not cleaned:
        if path.is_file():
            path.unlink()
    else:
        path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_plans_seeding_base() -> list[str]:
    from .plans_editor import _load_plans_module

    mod = _load_plans_module()
    order = getattr(mod, "SEEDING_CROP_ORDER", ())
    if isinstance(order, (list, tuple)):
        return [str(x) for x in order if str(x).strip()]
    return []


def get_editor_state() -> dict[str, Any]:
    mod = _load_layout_module()
    base = mod.default_layout_config()
    edits = load_layout_edits()
    sheet_order = mod._normalize_sheet_slots(edits.get("sheet_order") or base["sheet_order"])
    effective = {
        "sheet_order": sheet_order,
        "seeding_crop_order": mod._normalize_seeding_crops(
            edits.get("seeding_crop_order") or base["seeding_crop_order"]
        ),
        "pdf_grafts": mod._normalize_pdf_grafts(edits.get("pdf_grafts") or base["pdf_grafts"]),
    }
    page_preview = mod.build_page_preview(sheet_order)
    plans_crops = _load_plans_seeding_base()
    known = sorted(set(_KNOWN_CROPS) | set(plans_crops) | set(effective["seeding_crop_order"]))
    donor_reports = _donor_catalog_for_ui(mod)
    return {
        "base": base,
        "edits": edits,
        "effective": effective,
        "page_preview": page_preview,
        "total_pages": len(page_preview),
        "path": str(layout_edits_path()),
        "has_edits": bool(edits),
        "donor_reports": donor_reports,
        "known_crops": known,
        "plans_seeding_crop_order": plans_crops,
    }


def _donor_catalog_for_ui(mod) -> list[dict[str, Any]]:
    """Каталог сводок-доноров с типичными страницами для подстановки."""
    from app.legacy.report_generation import list_report_catalog_for_ui

    skip = frozenset({"1", "1.18"})
    hints = getattr(mod, "DONOR_PAGE_HINTS", {})
    common = frozenset(getattr(mod, "COMMON_GRAFT_DONOR_IDS", ()))
    out: list[dict[str, Any]] = []
    for row in list_report_catalog_for_ui():
        rid = str(row.get("id") or "").strip()
        if not rid or rid in skip:
            continue
        title = str(row.get("title") or rid).strip()
        pages = hints.get(rid, [{"page": 1, "sheet": "", "desc": "Первая страница"}])
        out.append(
            {
                "id": rid,
                "title": title,
                "label": title,
                "common_donor": rid in common,
                "pages": pages,
            }
        )
    return out


def set_sheet_order(slots: list[dict[str, Any]]) -> dict[str, Any]:
    mod = _load_layout_module()
    normalized = mod._normalize_sheet_slots(slots)
    if not normalized:
        raise ValueError("Порядок листов не может быть пустым.")
    edits = deepcopy(load_layout_edits())
    base = mod.default_layout_config()
    if normalized == mod._normalize_sheet_slots(base["sheet_order"]):
        edits.pop("sheet_order", None)
    else:
        edits["sheet_order"] = normalized
    save_layout_edits(edits)
    return get_editor_state()


def set_seeding_crop_order(crops: list[str]) -> dict[str, Any]:
    mod = _load_layout_module()
    normalized = mod._normalize_seeding_crops(crops)
    if not normalized:
        raise ValueError("Добавьте хотя бы одну культуру.")
    edits = deepcopy(load_layout_edits())
    base = mod.default_layout_config()
    if normalized == mod._normalize_seeding_crops(base["seeding_crop_order"]):
        edits.pop("seeding_crop_order", None)
    else:
        edits["seeding_crop_order"] = normalized
    save_layout_edits(edits)
    return get_editor_state()


def set_pdf_grafts(grafts: list[dict[str, Any]]) -> dict[str, Any]:
    mod = _load_layout_module()
    normalized = mod._normalize_pdf_grafts(grafts)
    edits = deepcopy(load_layout_edits())
    base = mod.default_layout_config()
    if normalized == mod._normalize_pdf_grafts(base["pdf_grafts"]):
        edits.pop("pdf_grafts", None)
    else:
        edits["pdf_grafts"] = normalized
    save_layout_edits(edits)
    return get_editor_state()


def reset_layout_edits(section: str | None = None) -> dict[str, Any]:
    section = (section or "").strip() or None
    allowed = {"sheet_order", "seeding_crop_order", "pdf_grafts"}
    if section and section not in allowed:
        raise ValueError(f"Неизвестная секция: {section}")
    if section:
        edits = load_layout_edits()
        edits.pop(section, None)
        save_layout_edits(edits)
    else:
        path = layout_edits_path()
        if path.is_file():
            path.unlink()
    return get_editor_state()


def write_layout_for_generation(dest: Path) -> Path | None:
    edits = load_layout_edits()
    if not edits:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(edits, ensure_ascii=False), encoding="utf-8")
    return dest


def merge_seeding_into_plans_edits(plans_edits: dict[str, Any]) -> dict[str, Any]:
    """Добавляет SEEDING_CROP_ORDER из layout в patch для plans."""
    mod = _load_layout_module()
    edits = load_layout_edits()
    crops = edits.get("seeding_crop_order")
    if not isinstance(crops, list) or not crops:
        return plans_edits
    normalized = mod._normalize_seeding_crops(crops)
    base_crops = mod._normalize_seeding_crops(mod.default_layout_config()["seeding_crop_order"])
    plans_base = _load_plans_seeding_base()
    if normalized == base_crops and normalized == mod._normalize_seeding_crops(plans_base):
        return plans_edits
    out = dict(plans_edits)
    out["SEEDING_CROP_ORDER"] = normalized
    return out


def inject_report1_layout_env(env: dict[str, str], output_dir: Path) -> None:
    upload = _upload_dir()
    env["SVODKI_UPLOAD_DIR"] = str(upload)
    eff = output_dir / f".report1_layout_{uuid.uuid4().hex}.json"
    if write_layout_for_generation(eff) is not None:
        env["SVODKI_REPORT1_LAYOUT_PATH"] = str(eff)
    else:
        env.pop("SVODKI_REPORT1_LAYOUT_PATH", None)


def effective_pdf_graft_spec() -> tuple[tuple[int, str, int], ...] | None:
    edits = load_layout_edits()
    if not edits or "pdf_grafts" not in edits:
        return None
    mod = _load_layout_module()
    grafts = mod._normalize_pdf_grafts(edits.get("pdf_grafts"))
    if grafts == mod._normalize_pdf_grafts(mod.default_layout_config()["pdf_grafts"]):
        return None
    return mod.pdf_grafts_to_spec(grafts)
