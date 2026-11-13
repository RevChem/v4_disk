import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings
from app.legacy.json_store_io import atomic_write_text, read_json_dict

_FUTURE_COMMENTS_KEY = "_future_comments"


def comments_store_path() -> Path:
    root = Path(settings.upload_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / "_summary_comments.json"


def load_comments_store() -> dict[str, Any]:
    return read_json_dict(comments_store_path(), label="comments")


def save_comments_store(store: dict[str, Any]) -> Path:
    path = comments_store_path()
    atomic_write_text(path, json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True))
    return path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future_comments_bucket(store: dict[str, Any]) -> dict[str, Any]:
    bucket = store.get(_FUTURE_COMMENTS_KEY)
    if not isinstance(bucket, dict):
        bucket = {}
        store[_FUTURE_COMMENTS_KEY] = bucket
    return bucket


def _normalize_report_id(report_id: str) -> str:
    return str(report_id or "").strip()


def _normalize_cell_key(cell_key: str) -> str:
    return str(cell_key or "").strip()


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float_or_default(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_or_default(value: Any, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _comment_template_from_row(row: dict[str, Any], *, report_id: str) -> dict[str, Any]:
    now = _now_iso()
    return {
        "id": str(row.get("future_id") or row.get("id") or uuid4().hex[:12]),
        "report_id": _normalize_report_id(report_id),
        "page": _int_or_default(row.get("page", 1), 1),
        "x": _float_or_default(row.get("x", 0.0), 0.0),
        "y": _float_or_default(row.get("y", 0.0), 0.0),
        "comment_anchor_y": _float_or_none(row.get("comment_anchor_y")),
        "cell_key": _normalize_cell_key(str(row.get("cell_key", ""))),
        "cell_text": str(row.get("cell_text") or "").strip(),
        "comment": str(row.get("comment") or "").strip(),
        "starred": True,
        "apply_to_future": True,
        "created_at": str(row.get("created_at") or now),
        "updated_at": now,
    }


def _suppress_future(bucket: dict[str, Any], cell_key: str) -> None:
    key = _normalize_cell_key(cell_key)
    if not key:
        return
    lst = bucket.get("suppressed_future")
    if not isinstance(lst, list):
        lst = []
    if key not in lst:
        lst.append(key)
    bucket["suppressed_future"] = lst


def _unsuppress_future(bucket: dict[str, Any], cell_key: str) -> None:
    key = _normalize_cell_key(cell_key)
    lst = bucket.get("suppressed_future")
    if isinstance(lst, list) and key in lst:
        bucket["suppressed_future"] = [k for k in lst if k != key]


def ensure_summary_bucket(
    store: dict[str, Any],
    summary_id: int,
    *,
    report_id: str,
    base_pdf_path: str,
) -> dict[str, Any]:
    sid = str(summary_id)
    bucket = store.get(sid)
    if not isinstance(bucket, dict):
        bucket = {}
        store[sid] = bucket
    bucket["report_id"] = str(report_id)
    bucket["base_pdf_path"] = str(base_pdf_path)
    comments = bucket.get("comments")
    if not isinstance(comments, list):
        comments = []
        bucket["comments"] = comments
    return bucket


def get_comment_pdf_base_path(
    store: dict[str, Any],
    summary_id: int,
    working_pdf: Path,
    *,
    report_id: str,
    refresh: bool = False,
) -> Path:
    """PDF без слоя комментариев: один раз копируется с рабочего файла, merge всегда с этой копии.

    Иначе при каждом сохранении читается уже «нарисованный» PDF и оверлей дублируется.
    refresh=True — снимок устарел (новая генерация сводки): перезаписать его текущим PDF.
    """
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
        raise RuntimeError("internal: summary comments bucket missing")
    raw = bucket.get("comment_pdf_base_path")
    if not refresh and isinstance(raw, str) and raw.strip():
        p = Path(raw.strip()).resolve()
        if p.is_file():
            return p
    root = Path(settings.upload_dir).resolve() / "_comment_pdf_bases"
    root.mkdir(parents=True, exist_ok=True)
    base = root / f"{int(summary_id)}.pdf"
    if refresh or not base.exists():
        shutil.copy2(working_pdf, base)
    bucket["comment_pdf_base_path"] = str(base.resolve())
    return base.resolve()


def list_summary_comments(store: dict[str, Any], summary_id: int) -> list[dict[str, Any]]:
    bucket = store.get(str(summary_id))
    if not isinstance(bucket, dict):
        return []
    comments = bucket.get("comments")
    if not isinstance(comments, list):
        return []
    out: list[dict[str, Any]] = []
    for row in comments:
        if isinstance(row, dict):
            out.append(row)
    return out


def list_future_comments(store: dict[str, Any], report_id: str) -> list[dict[str, Any]]:
    report_bucket = _future_comments_bucket(store).get(_normalize_report_id(report_id))
    if not isinstance(report_bucket, dict):
        return []
    out: list[dict[str, Any]] = []
    for row in report_bucket.values():
        if isinstance(row, dict):
            out.append(row)
    out.sort(key=lambda row: (int(row.get("page", 1)), str(row.get("cell_key", ""))))
    return out


def upsert_future_comment(
    store: dict[str, Any], report_id: str, row: dict[str, Any]
) -> dict[str, Any] | None:
    rid = _normalize_report_id(report_id)
    cell_key = _normalize_cell_key(str(row.get("cell_key", "")))
    if not rid or not cell_key:
        return None
    future_root = _future_comments_bucket(store)
    report_bucket = future_root.get(rid)
    if not isinstance(report_bucket, dict):
        report_bucket = {}
        future_root[rid] = report_bucket
    template = _comment_template_from_row(row, report_id=rid)
    previous = report_bucket.get(cell_key)
    if isinstance(previous, dict) and previous.get("created_at"):
        template["created_at"] = previous["created_at"]
    report_bucket[cell_key] = template
    return template


def delete_future_comment(store: dict[str, Any], report_id: str, cell_key: str) -> bool:
    report_bucket = _future_comments_bucket(store).get(_normalize_report_id(report_id))
    if not isinstance(report_bucket, dict):
        return False
    key = _normalize_cell_key(cell_key)
    existed = key in report_bucket
    report_bucket.pop(key, None)
    return existed


def apply_future_comments_to_summary(
    store: dict[str, Any],
    summary_id: int,
    *,
    report_id: str,
    base_pdf_path: str,
) -> tuple[list[dict[str, Any]], bool]:
    bucket = ensure_summary_bucket(
        store,
        summary_id,
        report_id=report_id,
        base_pdf_path=base_pdf_path,
    )
    comments = bucket["comments"]
    existing_keys = {
        _normalize_cell_key(str(row.get("cell_key", "")))
        for row in comments
        if isinstance(row, dict)
    }
    changed = False
    future_keys = {
        _normalize_cell_key(str(row.get("cell_key", "")))
        for row in list_future_comments(store, report_id)
    }
    for row in comments:
        if not isinstance(row, dict) or not row.get("apply_to_future"):
            continue
        cell_key = _normalize_cell_key(str(row.get("cell_key", "")))
        if cell_key and cell_key not in future_keys:
            upsert_future_comment(store, report_id, row)
            future_keys.add(cell_key)
    now = _now_iso()
    suppressed = {
        _normalize_cell_key(str(k)) for k in (bucket.get("suppressed_future") or [])
    }
    for future_row in list_future_comments(store, report_id):
        cell_key = _normalize_cell_key(str(future_row.get("cell_key", "")))
        if not cell_key or cell_key in existing_keys or cell_key in suppressed:
            continue
        copied = dict(future_row)
        copied["id"] = uuid4().hex[:12]
        copied["created_at"] = now
        copied["updated_at"] = now
        copied["apply_to_future"] = True
        comments.append(copied)
        existing_keys.add(cell_key)
        changed = True
    comments.sort(key=lambda row: (int(row.get("page", 1)), str(row.get("cell_key", ""))))
    return comments, changed


def upsert_summary_comment(
    store: dict[str, Any],
    summary_id: int,
    *,
    report_id: str,
    base_pdf_path: str,
    page: int,
    x: float,
    y: float,
    comment_anchor_y: float | None,
    cell_key: str,
    cell_text: str,
    comment: str,
) -> list[dict[str, Any]]:
    bucket = ensure_summary_bucket(
        store,
        summary_id,
        report_id=report_id,
        base_pdf_path=base_pdf_path,
    )
    comments = bucket["comments"]
    now = _now_iso()
    normalized_key = str(cell_key).strip()
    normalized_text = str(cell_text or "").strip()
    normalized_comment = str(comment or "").strip()

    _unsuppress_future(bucket, normalized_key)
    existing = None
    for row in comments:
        if str(row.get("cell_key", "")).strip() == normalized_key:
            existing = row
            break
    if existing is None:
        existing = {
            "id": uuid4().hex[:12],
            "created_at": now,
        }
        comments.append(existing)

    existing.update(
        {
            "page": int(page),
            "x": float(x),
            "y": float(y),
            "comment_anchor_y": float(comment_anchor_y) if comment_anchor_y is not None else None,
            "cell_key": normalized_key,
            "cell_text": normalized_text,
            "comment": normalized_comment,
            "starred": True,
            "updated_at": now,
        }
    )
    comments.sort(key=lambda row: (int(row.get("page", 1)), str(row.get("cell_key", ""))))
    return comments


def delete_summary_comment(
    store: dict[str, Any], summary_id: int, comment_id: str
) -> tuple[list[dict[str, Any]], bool]:
    sid = str(summary_id)
    bucket = store.get(sid)
    if not isinstance(bucket, dict):
        return ([], False)
    comments = list_summary_comments(store, summary_id)
    removed_ids = {str(comment_id)}
    seeds = [row for row in comments if str(row.get("id", "")) == str(comment_id)]
    # Та же приписка, привязанная к другому ключу ячейки (цифра сдвинулась): на листе
    # это один комментарий. Иначе после удаления на странице остаётся вторая копия.
    for seed in seeds:
        text = str(seed.get("comment") or "").strip()
        page = _int_or_default(seed.get("page"), 1)
        if not text:
            continue
        for row in comments:
            if (
                str(row.get("comment") or "").strip() == text
                and _int_or_default(row.get("page"), 1) == page
            ):
                removed_ids.add(str(row.get("id", "")))
    kept = [row for row in comments if str(row.get("id", "")) not in removed_ids]
    removed = [row for row in comments if str(row.get("id", "")) in removed_ids]
    deleted = len(kept) != len(comments)
    bucket["comments"] = kept
    # Шаблон «на будущее» остаётся для других дат, но в эту сводку больше не возвращается.
    # Иначе следующее открытие редактора сразу рисует комментарий заново.
    report_id = str(bucket.get("report_id") or "")
    future_rows = list_future_comments(store, report_id) if report_id else []
    for row in removed:
        _suppress_future(bucket, str(row.get("cell_key", "")))
        text = str(row.get("comment") or "").strip()
        page = _int_or_default(row.get("page"), 1)
        if not text:
            continue
        for future_row in future_rows:
            if (
                str(future_row.get("comment") or "").strip() == text
                and _int_or_default(future_row.get("page"), 1) == page
            ):
                _suppress_future(bucket, str(future_row.get("cell_key", "")))
    return (kept, deleted)

