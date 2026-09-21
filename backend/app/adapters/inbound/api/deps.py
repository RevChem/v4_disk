"""FastAPI dependencies."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.adapters.outbound.auth.security import decode_token
from app.adapters.outbound.auth.token_blacklist import is_revoked
from app.adapters.outbound.persistence import repositories as repo
from app.database import get_db
from app.models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/users/login")
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="/users/login", auto_error=False)


def get_client_ip(request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return ""


def _auth_error(code: str, status_code: int = status.HTTP_401_UNAUTHORIZED) -> HTTPException:
    return HTTPException(status_code=status_code, detail=code)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    try:
        payload = decode_token(token, "access")
    except ValueError:
        raise _auth_error("invalid_token") from None
    if is_revoked(payload.jti):
        raise _auth_error("token_revoked")
    user = repo.get_user_by_id(db, int(payload.sub))
    if user is None:
        raise _auth_error("user_not_found")
    if payload.tv != user.token_version:
        raise _auth_error("token_revoked")
    return user


def get_current_active_user(user: User = Depends(get_current_user)) -> User:
    if not user.is_active:
        raise _auth_error("inactive_user")
    return user


def require_admin(user: User = Depends(get_current_active_user)) -> User:
    if not user.is_admin():
        raise _auth_error("admin_required", status.HTTP_403_FORBIDDEN)
    return user


def require_plans_access(user: User = Depends(get_current_active_user)) -> User:
    if not user.can_edit_plans():
        raise _auth_error("forbidden", status.HTTP_403_FORBIDDEN)
    return user


def require_generate_access(user: User = Depends(get_current_active_user)) -> User:
    if not user.can_generate():
        raise _auth_error("forbidden", status.HTTP_403_FORBIDDEN)
    return user


def _user_from_raw_token(raw_token: str | None, db: Session) -> User:
    if not raw_token:
        raise _auth_error("not_authenticated")
    try:
        payload = decode_token(raw_token, "access")
    except ValueError:
        raise _auth_error("invalid_token") from None
    if is_revoked(payload.jti):
        raise _auth_error("token_revoked")
    user = repo.get_user_by_id(db, int(payload.sub))
    if user is None:
        raise _auth_error("user_not_found")
    if payload.tv != user.token_version:
        raise _auth_error("token_revoked")
    if not user.is_active:
        raise _auth_error("inactive_user")
    return user


def require_generate_access_flexible(
    token: str | None = Depends(oauth2_scheme_optional),
    access_token: str | None = Query(None),
    db: Session = Depends(get_db),
) -> User:
    """Bearer или ?access_token= для pdf.js и прямых ссылок на файлы."""
    user = _user_from_raw_token(token or access_token, db)
    if not user.can_generate():
        raise _auth_error("forbidden", status.HTTP_403_FORBIDDEN)
    return user


def require_schedule_access(user: User = Depends(get_current_active_user)) -> User:
    if not user.can_manage_schedule():
        raise _auth_error("forbidden", status.HTTP_403_FORBIDDEN)
    return user


def require_developer_access(user: User = Depends(get_current_active_user)) -> User:
    if not user.can_use_proverka():
        raise _auth_error("forbidden", status.HTTP_403_FORBIDDEN)
    return user


def require_manage_users_access(user: User = Depends(get_current_active_user)) -> User:
    if not user.can_manage_users():
        raise _auth_error("forbidden", status.HTTP_403_FORBIDDEN)
    return user


def require_cell_edit_access(user: User = Depends(get_current_active_user)) -> User:
    if not user.can_edit_cells():
        raise _auth_error("forbidden", status.HTTP_403_FORBIDDEN)
    return user


def require_comment_access(user: User = Depends(get_current_active_user)) -> User:
    if user.role not in ("admin", "developer", "user"):
        raise _auth_error("forbidden", status.HTTP_403_FORBIDDEN)
    return user


def require_page_order_access(user: User = Depends(get_current_active_user)) -> User:
    if not user.can_generate():
        raise _auth_error("forbidden", status.HTTP_403_FORBIDDEN)
    return user
