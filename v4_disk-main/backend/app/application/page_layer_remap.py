"""Перенос комментариев и правок ячеек при смене порядка страниц PDF.

Комментарии/правки хранят ordinal страницы *текущего* (рабочего) PDF и
`cell_key` вида ``{page}:{x}:{y}:{text}``. После перестановки страниц
содержимое уезжает, а ключи без remap остаются на старых номерах.

Идентичность слота:
- own → («own», source_page)
- graft → («graft», donor_report, donor_page)
"""
from __future__ import annotations

from typing import Any

from app.legacy.cell_edits_store import (
    list_summary_cell_edits,
    load_cell_edits_store,
    save_cell_edits_store,
)
from app.legacy.comments_store import (
    list_summary_comments,
    load_comments_store,
    save_comments_store,
)


def slot_identity(slot: dict[str, Any]) -> tuple:
    kind = str(slot.get("kind") or "").strip().lower()
    if kind == "own":
        return ("own", int(slot["page"]))
    if kind == "graft":
        return ("graft", str(slot.get("donor_report") or "").strip(), int(slot["donor_page"]))
    raise ValueError(f"Неизвестный тип слота: {kind!r}")


def identity_page_slots(n_pages: int) -> list[dict[str, Any]]:
    n = max(0, int(n_pages or 0))
    return [{"kind": "own", "page": i} for i in range(1, n + 1)]


def working_page_identities(
    page_slots: list[dict[str, Any]] | None,
    *,
    n_source: int | None = None,
) -> list[tuple]:
    """Идентичности слотов в порядке рабочих страниц (индекс 0 = страница 1)."""
    if page_slots:
        return [slot_identity(s) for s in page_slots]
    n = int(n_source or 0)
    if n < 1:
        return []
    return [("own", i) for i in range(1, n + 1)]


def build_working_page_remap(
    old_slots: list[dict[str, Any]] | None,
    new_slots: list[dict[str, Any]] | None,
    *,
    n_source: int | None = None,
) -> dict[int, int | None]:
    """Старый ordinal рабочей страницы → новый (или None, если страница скрыта/убрана)."""
    old_ids = working_page_identities(old_slots, n_source=n_source)
    new_ids = working_page_identities(new_slots, n_source=n_source)
    new_pos: dict[tuple, int] = {}
    for i, ident in enumerate(new_ids):
        new_pos.setdefault(ident, i + 1)
    return {i + 1: new_pos.get(ident) for i, ident in enumerate(old_ids)}


def remap_is_identity(page_remap: dict[int, int | None]) -> bool:
    if not page_remap:
        return True
    for old, new in page_remap.items():
        if new is None or int(new) != int(old):
            return False
    return True


def remap_cell_key(cell_key: str, old_page: int, new_page: int) -> str:
    key = str(cell_key or "")
    prefix = f"{int(old_page)}:"
    if key.startswith(prefix):
        return f"{int(new_page)}:{key[len(prefix):]}"
    return key


def remap_annotation_rows(
    rows: list[dict[str, Any]],
    page_remap: dict[int, int | None],
) -> tuple[list[dict[str, Any]], int, int]:
    """Переписать page/cell_key. Возвращает (оставленные, перемещено, удалено)."""
    if not rows or not page_remap or remap_is_identity(page_remap):
        return [dict(r) for r in rows if isinstance(r, dict)], 0, 0
    kept: list[dict[str, Any]] = []
    moved = 0
    dropped = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            old_page = int(row.get("page") or 1)
        except (TypeError, ValueError):
            old_page = 1
        if old_page not in page_remap:
            kept.append(dict(row))
            continue
        new_page = page_remap[old_page]
        if new_page is None:
            dropped += 1
            continue
        new_page = int(new_page)
        updated = dict(row)
        if new_page != old_page:
            updated["page"] = new_page
            updated["cell_key"] = remap_cell_key(str(row.get("cell_key") or ""), old_page, new_page)
            moved += 1
        kept.append(updated)
    return kept, moved, dropped


def _remap_future_bucket(
    report_bucket: dict[str, Any],
    page_remap: dict[int, int | None],
) -> tuple[dict[str, Any], int, int]:
    """Пересобрать bucket future-комментариев (ключ = cell_key)."""
    if not isinstance(report_bucket, dict) or remap_is_identity(page_remap):
        return report_bucket, 0, 0
    templates = {
        k: v for k, v in report_bucket.items() if k not in {"suppressed_future"} and isinstance(v, dict)
    }
    suppressed = report_bucket.get("suppressed_future")
    new_templates: dict[str, Any] = {}
    moved = 0
    dropped = 0
    for _key, row in templates.items():
        try:
            old_page = int(row.get("page") or 1)
        except (TypeError, ValueError):
            old_page = 1
        if old_page not in page_remap:
            ck = str(row.get("cell_key") or _key)
            new_templates[ck] = dict(row)
            continue
        new_page = page_remap[old_page]
        if new_page is None:
            dropped += 1
            continue
        new_page = int(new_page)
        updated = dict(row)
        old_ck = str(row.get("cell_key") or _key)
        if new_page != old_page:
            updated["page"] = new_page
            updated["cell_key"] = remap_cell_key(old_ck, old_page, new_page)
            moved += 1
        new_templates[str(updated.get("cell_key") or old_ck)] = updated

    new_suppressed: list[str] = []
    if isinstance(suppressed, list):
        for raw in suppressed:
            key = str(raw or "").strip()
            if not key:
                continue
            try:
                old_page = int(key.split(":", 1)[0])
            except (TypeError, ValueError):
                new_suppressed.append(key)
                continue
            if old_page not in page_remap:
                new_suppressed.append(key)
                continue
            new_page = page_remap[old_page]
            if new_page is None:
                continue
            new_suppressed.append(remap_cell_key(key, old_page, int(new_page)))

    out: dict[str, Any] = dict(new_templates)
    if new_suppressed:
        out["suppressed_future"] = new_suppressed
    return out, moved, dropped


def remap_summary_pdf_layer_pages(
    summary_id: int,
    page_remap: dict[int, int | None],
    *,
    report_id: str | None = None,
) -> dict[str, int]:
    """Обновить JSON комментариев/правок (и future) под новый порядок страниц."""
    stats = {
        "comments_moved": 0,
        "comments_dropped": 0,
        "edits_moved": 0,
        "edits_dropped": 0,
        "future_moved": 0,
        "future_dropped": 0,
    }
    if not page_remap or remap_is_identity(page_remap):
        return stats

    comments_store = load_comments_store()
    edits_store = load_cell_edits_store()

    comments = list_summary_comments(comments_store, summary_id)
    if comments:
        remapped, moved, dropped = remap_annotation_rows(comments, page_remap)
        bucket = comments_store.get(str(int(summary_id)))
        if isinstance(bucket, dict):
            bucket["comments"] = remapped
            stats["comments_moved"] = moved
            stats["comments_dropped"] = dropped
            save_comments_store(comments_store)

    edits = list_summary_cell_edits(edits_store, summary_id)
    if edits:
        remapped, moved, dropped = remap_annotation_rows(edits, page_remap)
        bucket = edits_store.get(str(int(summary_id)))
        if isinstance(bucket, dict):
            bucket["edits"] = remapped
            stats["edits_moved"] = moved
            stats["edits_dropped"] = dropped
            save_cell_edits_store(edits_store)

    rid = str(report_id or "").strip()
    if rid:
        future_root = comments_store.get("_future_comments")
        if isinstance(future_root, dict):
            report_bucket = future_root.get(rid)
            if isinstance(report_bucket, dict):
                new_bucket, moved, dropped = _remap_future_bucket(report_bucket, page_remap)
                future_root[rid] = new_bucket
                stats["future_moved"] = moved
                stats["future_dropped"] = dropped
                save_comments_store(comments_store)

    return stats
