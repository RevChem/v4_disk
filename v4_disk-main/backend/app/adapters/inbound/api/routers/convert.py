"""Excel → PDF conversion."""
from __future__ import annotations

import shutil
import tempfile
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.adapters.inbound.api.deps import require_generate_access
from app.adapters.outbound.reports.service import convert_xlsx_file_to_pdf
from app.config import settings
from app.infrastructure.logging import get_logger
from app.models import User

router = APIRouter(prefix="/convert", tags=["convert"])
log = get_logger(__name__)


@router.post("/xlsx-to-pdf")
async def convert_uploaded_xlsx_to_pdf(
    file: UploadFile = File(...),
    summary_date: date | None = Form(default=None),
    _: User = Depends(require_generate_access),
):
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Ожидается файл .xlsx")

    tmp_dir = Path(tempfile.mkdtemp(prefix="xlsx2pdf_"))
    try:
        src = tmp_dir / f"upload_{uuid.uuid4().hex}.xlsx"
        with src.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        pdf_path = convert_xlsx_file_to_pdf(src, tmp_dir, summary_date=summary_date)
        if summary_date is not None:
            dest_folder = Path(settings.upload_dir) / summary_date.isoformat()
            dest_folder.mkdir(parents=True, exist_ok=True)
            dest = dest_folder / pdf_path.name
            shutil.copy2(pdf_path, dest)
            return FileResponse(dest, media_type="application/pdf", filename=dest.name)
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=Path(file.filename).stem + ".pdf",
        )
    except Exception as exc:
        log.exception("convert.xlsx_to_pdf.failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
