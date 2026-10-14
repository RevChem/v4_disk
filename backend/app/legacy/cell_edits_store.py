"""Хранилище правок ячеек PDF (замена текста поверх выбранных фрагментов)."""

from __future__ import annotations

from app.legacy.json_store_io import atomic_write_text, read_json_dict

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings


def cell_edits_store_path() -> Path:
    root = Path(settings.upload_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / "_summary_cell_edits.json"


def load_cell_edits_store() -> dict[str, Any]:
    return read_json_dict(cell_edits_store_path(), label="cell_edits")


def save_cell_edits_store(store: dict[str, Any]) -> Path:
    path = cell_edits_store_path()
    atomic_write_text(path, json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True))
    return path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _float_or_default(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _int_or_default(value: Any, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ensure_summary_bucket(
    store: dict[str, Any],
    summary_id: int,
    *,
    report_id: str,
    base_pdf_path: str,
) -> dict[str, Any]:
    sid = str(int(summary_id))
    bucket = store.get(sid)
    if not isinstance(bucket, dict):
        bucket = {}
        store[sid] = bucket
    bucket["report_id"] = str(report_id or "")
    bucket["base_pdf_path"] = str(base_pdf_path)
    edits = bucket.get("edits")
    if not isinstance(edits, list):
        edits = []
        bucket["edits"] = edits
    return bucket


def get_cell_edit_pdf_base_path(
    store: dict[str, Any],
    summary_id: int,
    working_pdf: Path,
    *,
    report_id: str,
    preferred_base: Path | None = None,
    refresh: bool = False,
) -> Path:
    """PDF без слоя правок ячеек: один раз копируется, дальше всегда merge с этой копии."""
    working_pdf = working_pdf.resolve()
    ensure_summary_bucket(
        store,
        summary_id,
        report_id=report_id,
        base_pdf_path=str(working_pdf),
    )
    sid = str(int(summary_id))
    bucket = store.get(sid)
    if not isinstance(bucket, dict):
        raise RuntimeError("internal: summary cell edits bucket missing")
    raw = bucket.get("cell_edit_pdf_base_path")
    if not refresh and isinstance(raw, str) and raw.strip():
        p = Path(raw.strip()).resolve()
        if p.is_file():
            return p
    root = Path(settings.upload_dir).resolve() / "_cell_edit_pdf_bases"
    root.mkdir(parents=True, exist_ok=True)
    dest = root / f"{sid}.pdf"
    source = preferred_base if preferred_base is not None and preferred_base.is_file() else working_pdf
    shutil.copy2(source, dest)
    bucket["cell_edit_pdf_base_path"] = str(dest)
    return dest


def find_cell_edits_for_report(store: dict[str, Any], report_id: str) -> tuple[int | None, list[dict[str, Any]]]:
    """Найти последние правки для report_id (при смене summary_id после генерации)."""
    rid = str(report_id or "").strip()
    if not rid:
        return None, []
    best_sid: int | None = None
    best_edits: list[dict[str, Any]] = []
    for key, bucket in store.items():
        if not isinstance(bucket, dict):
            continue
        try:
            sid = int(key)
        except (TypeError, ValueError):
            continue
        if str(bucket.get("report_id") or "").strip() != rid:
            continue
        edits = bucket.get("edits")
        if not isinstance(edits, list) or not edits:
            continue
        if best_sid is None or sid > best_sid:
            best_sid = sid
            best_edits = [dict(row) for row in edits if isinstance(row, dict)]
    return best_sid, best_edits


def adopt_cell_edits_for_summary(
    store: dict[str, Any],
    summary_id: int,
    *,
    report_id: str,
    base_pdf_path: str,
) -> list[dict[str, Any]]:
    """Перенести правки report_id на новый summary_id (после перегенерации)."""
    existing = list_summary_cell_edits(store, summary_id)
    if existing:
        return existing
    donor_sid, edits = find_cell_edits_for_report(store, report_id)
    if not edits:
        return []
    bucket = ensure_summary_bucket(
        store,
        summary_id,
        report_id=report_id,
        base_pdf_path=base_pdf_path,
    )
    bucket["edits"] = [dict(row) for row in edits]
    bucket.pop("cell_edit_pdf_base_path", None)
    if donor_sid is not None and int(donor_sid) != int(summary_id):
        # Старый bucket больше не нужен как источник для этой сводки.
        store.pop(str(int(donor_sid)), None)
    return list_summary_cell_edits(store, summary_id)


def list_summary_cell_edits(store: dict[str, Any], summary_id: int) -> list[dict[str, Any]]:
    bucket = store.get(str(int(summary_id)))
    if not isinstance(bucket, dict):
        return []
    edits = bucket.get("edits")
    if not isinstance(edits, list):
        return []
    return [row for row in edits if isinstance(row, dict)]


def _find_overlapping_edit(
    edits: list[dict[str, Any]],
    *,
    page: int,
    left: float,
    top: float,
    width: float,
    height: float,
) -> dict[str, Any] | None:
    """Правка на той же странице, внутрь которой попадает центр новой ячейки."""
    cx = left + width / 2.0
    cy = top + height / 2.0
    best: dict[str, Any] | None = None
    best_dist = None
    for row in edits:
        if _int_or_default(row.get("page"), 1) != page:
            continue
        rl = _float_or_default(row.get("left"), 0.0)
        rt = _float_or_default(row.get("top"), 0.0)
        rw = _float_or_default(row.get("width"), 0.01)
        rh = _float_or_default(row.get("height"), 0.02)
        tol_x = max(0.003, rw * 0.5)
        tol_y = max(0.002, rh * 0.5)
        if not (rl - tol_x <= cx <= rl + rw + tol_x and rt - tol_y <= cy <= rt + rh + tol_y):
            continue
        dist = abs(cx - (rl + rw / 2.0)) + abs(cy - (rt + rh / 2.0))
        if best is None or dist < best_dist:
            best, best_dist = row, dist
    return best


def upsert_summary_cell_edit(
    store: dict[str, Any],
    summary_id: int,
    *,
    report_id: str,
    base_pdf_path: str,
    page: int,
    left: float,
    top: float,
    width: float,
    height: float,
    cell_key: str,
    cell_text: str,
    replacement: str,
    fill_color: str = "",
    text_color: str = "",
) -> list[dict[str, Any]]:
    bucket = ensure_summary_bucket(
        store,
        summary_id,
        report_id=report_id,
        base_pdf_path=base_pdf_path,
    )
    edits = bucket["edits"]
    key = str(cell_key or "").strip()
    if not key:
        raise ValueError("cell_key обязателен")
    now = _now_iso()
    existing = None
    for row in edits:
        if str(row.get("cell_key", "")) == key:
            existing = row
            break
    if existing is None:
        # Повторная правка по уже заменённому значению: PDF в редакторе показывает новый текст,
        # у него другой cell_key. Это та же ячейка — перезаписываем старую правку, а не
        # добавляем вторую поверх. Исходный текст и координаты берём из первой правки.
        existing = _find_overlapping_edit(
            edits,
            page=max(1, _int_or_default(page, 1)),
            left=_float_or_default(left, 0.0),
            top=_float_or_default(top, 0.0),
            width=_float_or_default(width, 0.01),
            height=_float_or_default(height, 0.02),
        )
        if existing is not None:
            key = str(existing.get("cell_key") or key)
            page = existing.get("page", page)
            left = existing.get("left", left)
            top = existing.get("top", top)
            width = existing.get("width", width)
            height = existing.get("height", height)
            cell_text = str(existing.get("cell_text") or cell_text)
    fill = str(fill_color or "").strip() or str((existing or {}).get("fill_color") or "").strip()
    tcolor = str(text_color or "").strip() or str((existing or {}).get("text_color") or "").strip()
    payload = {
        "id": str((existing or {}).get("id") or uuid4().hex[:12]),
        "page": max(1, _int_or_default(page, 1)),
        "left": _clamp01(_float_or_default(left, 0.0)),
        "top": _clamp01(_float_or_default(top, 0.0)),
        "width": _clamp01(max(0.001, _float_or_default(width, 0.01))),
        "height": _clamp01(max(0.001, _float_or_default(height, 0.02))),
        "cell_key": key,
        "cell_text": str(cell_text or "").strip(),
        "replacement": str(replacement or "").strip(),
        "fill_color": fill,
        "text_color": tcolor,
        "created_at": str((existing or {}).get("created_at") or now),
        "updated_at": now,
    }
    if not payload["replacement"]:
        raise ValueError("Текст замены не может быть пустым")
    if existing is None:
        edits.append(payload)
    else:
        existing.clear()
        existing.update(payload)
    edits.sort(key=lambda row: (int(row.get("page", 1)), str(row.get("cell_key", ""))))
    return edits


def delete_summary_cell_edit(
    store: dict[str, Any], summary_id: int, edit_id: str
) -> tuple[list[dict[str, Any]], bool]:
    bucket = store.get(str(int(summary_id)))
    if not isinstance(bucket, dict):
        return [], False
    edits = list_summary_cell_edits(store, summary_id)
    kept = [row for row in edits if str(row.get("id", "")) != str(edit_id)]
    deleted = len(kept) != len(edits)
    bucket["edits"] = kept
    return kept, deleted


def clear_all_cell_edits(store: dict[str, Any]) -> list[dict[str, Any]]:
    """Обнулить все правки ячеек. Возвращает затронутые сводки (до очистки)."""
    cleared: list[dict[str, Any]] = []
    for sid, bucket in list(store.items()):
        if not isinstance(bucket, dict):
            continue
        if not str(sid).isdigit():
            continue
        edits = bucket.get("edits")
        if not isinstance(edits, list) or not edits:
            continue
        cleared.append(
            {
                "summary_id": int(sid),
                "report_id": str(bucket.get("report_id") or ""),
                "edits_count": len(edits),
                "cell_edit_pdf_base_path": str(bucket.get("cell_edit_pdf_base_path") or ""),
            }
        )
        bucket["edits"] = []
    return cleared
