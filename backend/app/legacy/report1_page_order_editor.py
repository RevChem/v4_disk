"""Пользовательский порядок страниц PDF сводок (перестановка, скрытие, подстановки).

Конфиг хранится отдельно на каждую сводку. Для сводки «1» путь совместим
с прежним `report1_page_order.json`; для остальных — `page_order_{id}.json`.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings

_REPORT_ID_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
# Сводка 1.18 без собственного конфига наследует порядок сводки 1 (как раньше).
_LEGACY_SHARED_FROM = {"1.18": "1"}


def normalize_report_id(report_id: str | None) -> str:
    rid = str(report_id or "1").strip()
    if not rid or not _REPORT_ID_RE.match(rid):
        raise ValueError(f"Некорректный id сводки: {report_id!r}")
    return rid


def _upload_dir() -> Path:
    root = Path(settings.upload_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def store_path(report_id: str | None = "1") -> Path:
    rid = normalize_report_id(report_id)
    # Обратная совместимость: прежний глобальный файл сводки 1.
    if rid == "1":
        return _upload_dir() / "report1_page_order.json"
    safe = rid.replace(".", "_")
    return _upload_dir() / f"page_order_{safe}.json"


def _default_config() -> dict:
    return {
        "page_slots": None,
        "page_order": None,
        "hidden_pages": None,
        "source_page_count": None,
        "reference_summary_id": None,
        "updated_at": None,
    }


def load_config(report_id: str | None = "1") -> dict:
    path = store_path(report_id)
    if not path.is_file():
        return _default_config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_config()
    if not isinstance(raw, dict):
        return _default_config()
    page_slots = raw.get("page_slots")
    if page_slots is not None and not isinstance(page_slots, list):
        page_slots = None
    page_order = raw.get("page_order")
    if page_order is not None and not isinstance(page_order, list):
        page_order = None
    hidden_pages = raw.get("hidden_pages")
    if hidden_pages is not None and not isinstance(hidden_pages, list):
        hidden_pages = None
    return {
        "page_slots": page_slots,
        "page_order": page_order,
        "hidden_pages": hidden_pages,
        "source_page_count": raw.get("source_page_count"),
        "reference_summary_id": raw.get("reference_summary_id"),
        "updated_at": raw.get("updated_at"),
    }


def resolve_config_report_id(report_id: str | None) -> str:
    """Id конфига для применения: 1.18 без своего файла → конфиг «1»."""
    rid = normalize_report_id(report_id)
    if has_custom_order(rid):
        return rid
    shared_from = _LEGACY_SHARED_FROM.get(rid)
    if shared_from and has_custom_order(shared_from):
        return shared_from
    return rid


def _normalize_int_list(values: list[int] | None) -> list[int]:
    normalized: list[int] = []
    for item in values or []:
        try:
            normalized.append(int(item))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Некорректный номер страницы: {item!r}") from exc
    return normalized


def _normalize_page_slots(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("page_slots не может быть пустым.")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind == "own":
            try:
                page = int(item["page"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Некорректная own-страница: {item!r}") from exc
            if page < 1:
                raise ValueError(f"Номер страницы должен быть ≥ 1: {page}")
            out.append({"kind": "own", "page": page})
        elif kind == "graft":
            donor_report = str(item.get("donor_report") or "").strip()
            try:
                donor_page = int(item.get("donor_page"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Некорректная graft-страница: {item!r}") from exc
            if not donor_report or donor_page < 1:
                raise ValueError(f"Некорректная подстановка: {item!r}")
            slot: dict[str, Any] = {
                "kind": "graft",
                "donor_report": donor_report,
                "donor_page": donor_page,
            }
            label = str(item.get("label") or "").strip()
            if label:
                slot["label"] = label
            out.append(slot)
        else:
            raise ValueError(f"Неизвестный тип слота: {kind!r}")
    if not out:
        raise ValueError("page_slots не может быть пустым.")
    return out


def _page_order_from_slots(page_slots: list[dict[str, Any]]) -> list[int]:
    return [int(s["page"]) for s in page_slots if s.get("kind") == "own"]


def _page_slots_from_legacy(page_order: list[int] | None) -> list[dict[str, Any]] | None:
    if not page_order:
        return None
    return [{"kind": "own", "page": int(p)} for p in page_order]


def effective_page_slots(cfg: dict) -> list[dict[str, Any]] | None:
    slots = cfg.get("page_slots")
    if isinstance(slots, list) and slots:
        return _normalize_page_slots(slots)
    legacy = cfg.get("page_order")
    if isinstance(legacy, list) and legacy:
        return _page_slots_from_legacy(legacy)
    return None


def _validate_page_layout(
    page_slots: list[dict[str, Any]],
    *,
    n_pages: int | None = None,
    hidden_pages: list[int] | None = None,
    report_label: str = "1",
) -> tuple[list[dict[str, Any]], list[int]]:
    slots = _normalize_page_slots(page_slots)
    hidden = _normalize_int_list(hidden_pages)
    own_pages = _page_order_from_slots(slots)
    if not own_pages and not any(s.get("kind") == "graft" for s in slots):
        raise ValueError("Должна остаться хотя бы одна страница в итоговой сводке.")
    if len(set(own_pages)) != len(own_pages):
        raise ValueError(
            f"Одна исходная страница сводки {report_label} не может встречаться дважды."
        )
    if len(set(hidden)) != len(hidden):
        raise ValueError("hidden_pages содержит повторяющиеся страницы.")
    overlap = set(own_pages) & set(hidden)
    if overlap:
        raise ValueError(
            f"Страница не может быть одновременно видимой и скрытой: {sorted(overlap)}."
        )
    if n_pages is not None:
        all_pages = set(range(1, n_pages + 1))
        chosen = set(own_pages) | set(hidden)
        out_of_range = [page for page in chosen if page < 1 or page > n_pages]
        if out_of_range:
            raise ValueError(f"Номера страниц вне диапазона 1–{n_pages}: {sorted(out_of_range)}.")
        if chosen != all_pages:
            missing = sorted(all_pages - chosen)
            if missing:
                raise ValueError(f"Не все страницы PDF учтены. Не указаны: {missing}.")
    return slots, hidden


def is_identity_order(page_order: list[int] | None, n_pages: int) -> bool:
    if not page_order or n_pages < 1:
        return True
    if len(page_order) != n_pages:
        return False
    return page_order == list(range(1, n_pages + 1))


def is_default_layout(
    page_slots: list[dict[str, Any]] | None,
    hidden_pages: list[int] | None,
    n_pages: int,
) -> bool:
    hidden = hidden_pages or []
    if hidden:
        return False
    if not page_slots:
        return True
    if any(s.get("kind") == "graft" for s in page_slots):
        return False
    own = _page_order_from_slots(page_slots)
    return is_identity_order(own, n_pages)


def has_custom_order(report_id: str | None = "1") -> bool:
    cfg = load_config(report_id)
    page_slots = effective_page_slots(cfg)
    if not page_slots:
        return False
    hidden_pages = cfg.get("hidden_pages") or []
    source_page_count = cfg.get("source_page_count")
    page_order = _page_order_from_slots(page_slots)
    if source_page_count is None and page_order is not None:
        source_page_count = len(page_order) + len(hidden_pages)
    n_pages = int(source_page_count or 0)
    return not is_default_layout(page_slots, hidden_pages, n_pages)


def save_page_order(
    page_slots: list[dict[str, Any]],
    *,
    report_id: str | None = "1",
    reference_summary_id: int | None = None,
    n_pages: int | None = None,
    hidden_pages: list[int] | None = None,
) -> dict:
    rid = normalize_report_id(report_id)
    slots, hidden = _validate_page_layout(
        page_slots,
        n_pages=n_pages,
        hidden_pages=hidden_pages,
        report_label=rid,
    )
    if n_pages is not None and is_default_layout(slots, hidden, n_pages):
        reset_page_order(rid)
        return get_editor_state(rid)

    payload = {
        "report_id": rid,
        "page_slots": slots,
        "page_order": _page_order_from_slots(slots) or None,
        "hidden_pages": hidden or None,
        "source_page_count": n_pages,
        "reference_summary_id": reference_summary_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path = store_path(rid)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return get_editor_state(rid)


def reset_page_order(report_id: str | None = "1") -> dict:
    rid = normalize_report_id(report_id)
    path = store_path(rid)
    if path.is_file():
        path.unlink()
    return get_editor_state(rid)


_donor_hints_warned = False


def _donor_layout_hints() -> tuple[dict[str, Any], frozenset[str]]:
    """Подсказки по страницам доноров из report1_layout_config.py (необязательный файл).

    Если файла нет или он не загрузился — работаем без подсказок (у каждого донора
    предлагается первая страница), но пишем предупреждение, а не скрываем доноров.
    """
    global _donor_hints_warned
    try:
        from .report1_layout_editor import _load_layout_module

        mod = _load_layout_module()
        return dict(getattr(mod, "DONOR_PAGE_HINTS", {}) or {}), frozenset(
            getattr(mod, "COMMON_GRAFT_DONOR_IDS", ()) or ()
        )
    except Exception as exc:
        if not _donor_hints_warned:
            _donor_hints_warned = True
            import logging

            logging.getLogger(__name__).warning(
                "page_order: подсказки доноров недоступны (%s); используется первая страница каждой сводки.",
                exc,
            )
        return {}, frozenset()


def _donor_catalog(master_report_id: str = "1") -> list[dict[str, Any]]:
    from .report_generation import list_report_catalog_for_ui

    hints, common = _donor_layout_hints()
    master = normalize_report_id(master_report_id)
    skip = {master}
    if master == "1":
        skip.add("1.18")
    out: list[dict[str, Any]] = []
    for row in list_report_catalog_for_ui():
        rid = str(row.get("id") or "").strip()
        if not rid or rid in skip:
            continue
        title = str(row.get("title") or rid).strip()
        pages = hints.get(rid) or [{"page": 1, "sheet": "", "desc": "Первая страница"}]
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


def get_editor_state(report_id: str | None = "1") -> dict:
    rid = normalize_report_id(report_id)
    cfg = load_config(rid)
    page_slots = effective_page_slots(cfg)
    hidden_pages = cfg.get("hidden_pages") or []
    source_page_count = cfg.get("source_page_count")
    page_order = _page_order_from_slots(page_slots) if page_slots else cfg.get("page_order")
    if source_page_count is None and page_order is not None:
        source_page_count = len(page_order) + len(hidden_pages)
    n_pages = int(source_page_count or 0)
    has_custom = bool(page_slots) and not is_default_layout(page_slots, hidden_pages, n_pages)
    graft_count = sum(1 for s in (page_slots or []) if s.get("kind") == "graft")
    return {
        "report_id": rid,
        "page_slots": page_slots,
        "page_order": page_order,
        "hidden_pages": hidden_pages or None,
        "source_page_count": source_page_count,
        "reference_summary_id": cfg.get("reference_summary_id"),
        "updated_at": cfg.get("updated_at"),
        "has_custom_order": has_custom,
        "graft_count": graft_count,
        "donor_catalog": _donor_catalog(rid),
        "path": str(store_path(rid)),
    }


def _read_pdf_page_count(pdf_path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(pdf_path)).pages)


def apply_page_layout_to_pdf(
    src_pdf: Path,
    page_slots: list[dict[str, Any]],
    *,
    dest_pdf: Path | None = None,
    hidden_pages: list[int] | None = None,
    donor_pdfs: dict[str, Path] | None = None,
    report_label: str = "1",
) -> Path:
    from pypdf import PdfReader, PdfWriter

    if not src_pdf.is_file():
        raise FileNotFoundError(f"PDF не найден: {src_pdf}")
    reader = PdfReader(str(src_pdf))
    n_pages = len(reader.pages)
    slots, _hidden = _validate_page_layout(
        page_slots,
        n_pages=n_pages,
        hidden_pages=hidden_pages,
        report_label=report_label,
    )
    donors = donor_pdfs or {}
    donor_readers: dict[str, PdfReader] = {}

    target = (dest_pdf or src_pdf).resolve()
    tmp = target.with_suffix(".page_order_tmp.pdf")
    writer = PdfWriter()
    for slot in slots:
        if slot["kind"] == "own":
            writer.add_page(reader.pages[int(slot["page"]) - 1])
            continue
        donor_id = str(slot["donor_report"])
        donor_page = int(slot["donor_page"])
        donor_path = donors.get(donor_id)
        if donor_path is None or not donor_path.is_file():
            raise ValueError(
                f"Нет PDF сводки {donor_id} для подстановки стр. {donor_page}. "
                f"Сгенерируйте сводку {donor_id} в той же пачке."
            )
        if donor_id not in donor_readers:
            donor_readers[donor_id] = PdfReader(str(donor_path))
        d_reader = donor_readers[donor_id]
        if donor_page < 1 or donor_page > len(d_reader.pages):
            raise ValueError(
                f"Сводка {donor_id}: страница {donor_page} вне диапазона "
                f"1–{len(d_reader.pages)}."
            )
        writer.add_page(d_reader.pages[donor_page - 1])

    with tmp.open("wb") as fh:
        writer.write(fh)
    if dest_pdf is None:
        tmp.replace(target)
        return target
    shutil.copy2(tmp, target)
    tmp.unlink(missing_ok=True)
    return target


def apply_page_order_permutation(
    src_pdf: Path,
    page_order: list[int],
    *,
    dest_pdf: Path | None = None,
    hidden_pages: list[int] | None = None,
    donor_pdfs: dict[str, Path] | None = None,
    page_slots: list[dict[str, Any]] | None = None,
    report_label: str = "1",
) -> Path:
    slots = page_slots if page_slots is not None else _page_slots_from_legacy(page_order)
    if not slots:
        raise ValueError("page_slots/page_order не может быть пустым.")
    return apply_page_layout_to_pdf(
        src_pdf,
        slots,
        dest_pdf=dest_pdf,
        hidden_pages=hidden_pages,
        donor_pdfs=donor_pdfs,
        report_label=report_label,
    )


def page_order_base_path(report_id: str, summary_id: int) -> Path:
    rid = normalize_report_id(report_id)
    safe = rid.replace(".", "_")
    root = _upload_dir() / "_page_order_pdf_bases"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{safe}__{int(summary_id)}.pdf"


def ensure_page_order_base_pdf(
    report_id: str,
    summary_id: int,
    working_pdf: Path,
    *,
    refresh: bool = False,
) -> Path:
    """Исходный PDF до пользовательской перестановки (для повторного применения)."""
    working_pdf = working_pdf.resolve()
    if not working_pdf.is_file():
        raise FileNotFoundError(f"PDF не найден: {working_pdf}")
    base = page_order_base_path(report_id, summary_id)
    if refresh or not base.is_file():
        shutil.copy2(working_pdf, base)
    return base


def restore_page_order_base_pdf(
    report_id: str,
    summary_id: int,
    working_pdf: Path,
) -> bool:
    """Вернуть рабочий PDF из базы и удалить снимок базы."""
    base = page_order_base_path(report_id, summary_id)
    if not base.is_file():
        return False
    working_pdf = working_pdf.resolve()
    shutil.copy2(base, working_pdf)
    base.unlink(missing_ok=True)
    return True


def apply_saved_page_order_to_pdf(
    pdf_path: Path,
    logs: list[str],
    *,
    report_id: str | None = None,
    report_label: str | None = None,
    donor_pdfs: dict[str, Path] | None = None,
    summary_id: int | None = None,
    refresh_base: bool = False,
    base_seed: Path | None = None,
) -> bool:
    """base_seed — «чистый» PDF (без комментариев/правок ячеек) для первого снимка базы."""
    label = str(report_label or report_id or "1").strip() or "1"
    config_id = resolve_config_report_id(report_id or label)
    cfg = load_config(config_id)
    page_slots = effective_page_slots(cfg)
    if not page_slots:
        return False
    hidden_pages = cfg.get("hidden_pages") or []
    if not pdf_path.is_file():
        logs.append(f"Сводка {label}: нет PDF для пользовательского порядка страниц.")
        return False
    source_pdf = pdf_path
    try:
        if summary_id is not None:
            source_pdf = ensure_page_order_base_pdf(
                config_id,
                int(summary_id),
                base_seed if base_seed is not None else pdf_path,
                refresh=refresh_base,
            )
        n_pages = _read_pdf_page_count(source_pdf)
    except Exception as exc:
        logs.append(f"Сводка {label}: не прочитать PDF — {exc}")
        return False
    if is_default_layout(page_slots, hidden_pages, n_pages):
        return False
    own_pages = _page_order_from_slots(page_slots)
    expected_total = len(own_pages) + len(hidden_pages)
    if expected_total != n_pages:
        logs.append(
            f"Сводка {label}: пользовательский порядок страниц не применён — "
            f"ожидалось {expected_total} исходных стр. "
            f"(видимых {len(own_pages)}, скрытых {len(hidden_pages)}), "
            f"в PDF {n_pages} стр."
        )
        return False
    graft_slots = [s for s in page_slots if s.get("kind") == "graft"]
    missing_donors = sorted(
        {
            str(s["donor_report"])
            for s in graft_slots
            if not donor_pdfs or str(s["donor_report"]) not in donor_pdfs
        }
    )
    if graft_slots and missing_donors:
        logs.append(
            f"Сводка {label}: порядок с подстановками не применён — "
            f"нет PDF доноров: {', '.join(missing_donors)}. "
            f"Включите эти сводки в пачку генерации."
        )
        return False
    try:
        apply_page_layout_to_pdf(
            source_pdf,
            page_slots,
            dest_pdf=pdf_path,
            hidden_pages=hidden_pages,
            donor_pdfs=donor_pdfs,
            report_label=label,
        )
    except Exception as exc:
        logs.append(f"Сводка {label}: ошибка пользовательского порядка страниц — {exc}")
        return False
    hidden_note = f", скрыто {len(hidden_pages)} стр." if hidden_pages else ""
    graft_note = f", подстановок {len(graft_slots)}" if graft_slots else ""
    inherit_note = (
        f" (конфиг сводки {config_id})" if config_id != label else ""
    )
    logs.append(
        f"Сводка {label}: применён пользовательский порядок страниц PDF"
        f"{inherit_note} "
        f"({len(page_slots)} стр. итого{hidden_note}{graft_note})."
    )
    return True
