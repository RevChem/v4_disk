"""Связь PDF на диске с записями Summary в БД (для правок ячеек и порядка страниц)."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.crud import create_summary
from app.models import Summary


def report_id_from_title(title: str) -> str:
    m = re.match(r"\s*(\d+(?:\.\d+)?)", str(title or ""))
    return m.group(1) if m else ""


def ensure_summary_for_file(
    db: Session,
    *,
    summary_date: date,
    filename: str,
    title: str,
    file_path: str | Path,
) -> Summary:
    path = Path(file_path).resolve()
    path_str = str(path)

    by_path = db.scalar(
        select(Summary)
        .where(Summary.file_path == path_str, Summary.visible.is_(True))
        .order_by(Summary.id.desc())
        .limit(1)
    )
    if by_path is not None:
        if by_path.summary_date != summary_date or by_path.title != title:
            by_path.summary_date = summary_date
            by_path.title = title
            db.commit()
            db.refresh(by_path)
        return by_path

    by_title = db.scalar(
        select(Summary)
        .where(
            Summary.summary_date == summary_date,
            Summary.title == title,
            Summary.visible.is_(True),
        )
        .order_by(Summary.id.desc())
        .limit(1)
    )
    if by_title is not None:
        by_title.file_path = path_str
        db.commit()
        db.refresh(by_title)
        return by_title

    return create_summary(db, title=title, summary_date=summary_date, file_path=path_str)


def enrich_generated_files(db: Session, summary_date: date, files: list[dict[str, str]]) -> list[dict[str, str]]:
    folder = Path(settings.upload_dir) / summary_date.isoformat()
    out: list[dict[str, str]] = []
    for item in files:
        filename = str(item.get("filename") or "")
        title = str(item.get("title") or filename)
        path = folder / filename
        if not path.is_file():
            row = dict(item)
            row.setdefault("summary_id", "")
            out.append(row)
            continue
        summary = ensure_summary_for_file(
            db,
            summary_date=summary_date,
            filename=filename,
            title=title,
            file_path=path,
        )
        row = dict(item)
        row["summary_id"] = str(summary.id)
        out.append(row)
    return out
