"""Настройки интерфейса текущего пользователя (тема и фон)."""
from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from app.adapters.inbound.api.deps import get_client_ip, get_current_active_user
from app.application.audit_activity import record_activity
from app.application.interface_settings import THEME_IDS
from app.database import get_db
from app.legacy.interface_settings_store import (
    load_user_settings,
    update_user_interface_settings,
    user_background_path,
)
from app.models import User
from app.schemas import InterfaceSettingsIn, InterfaceSettingsOut
from sqlalchemy.orm import Session

router = APIRouter(prefix="/interface-settings", tags=["interface"])


def _settings_out(user_id: int) -> InterfaceSettingsOut:
    cfg = load_user_settings(user_id)
    bg = user_background_path(user_id)
    version = int(cfg.get("background_version") or 0)
    return InterfaceSettingsOut(
        theme=str(cfg.get("theme") or "dark"),
        has_custom_background=bg is not None,
        background_version=version if bg is not None else 0,
    )


@router.get("", response_model=InterfaceSettingsOut)
def get_my_interface_settings(user: User = Depends(get_current_active_user)) -> InterfaceSettingsOut:
    return _settings_out(user.id)


@router.get("/background")
def get_my_interface_background(
    user: User = Depends(get_current_active_user),
    v: int = 0,
) -> Response:
    path = user_background_path(user.id)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="background_not_found")
    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media,
        # Фон персональный: URL одинаков у разных пользователей одного браузера,
        # поэтому HTTP-кэш использовать нельзя (иначе возможна подмена чужим фоном).
        headers={
            "Cache-Control": "private, no-store",
            "Vary": "Authorization",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.put("", response_model=InterfaceSettingsOut)
def update_my_interface_settings(
    body: InterfaceSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    theme = str(body.theme or "").strip()
    if theme not in THEME_IDS:
        raise HTTPException(status_code=400, detail="invalid_theme")
    fields = body.model_dump(exclude_unset=True)
    try:
        if "custom_background" in fields:
            update_user_interface_settings(
                user.id,
                theme=theme,
                custom_background=fields["custom_background"],
            )
        else:
            update_user_interface_settings(user.id, theme=theme)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_activity(
        db,
        action="interface.updated",
        user_id=user.id,
        ip=get_client_ip(request),
        theme=theme,
        background_changed="custom_background" in fields,
    )
    return _settings_out(user.id)
