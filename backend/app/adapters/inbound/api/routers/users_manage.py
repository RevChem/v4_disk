"""Управление пользователями (только разработчик)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.adapters.inbound.api.deps import get_client_ip, require_manage_users_access
from app.adapters.outbound.auth import hibp
from app.adapters.outbound.auth.security import hash_password, verify_password
from app.adapters.outbound.persistence import repositories as repo
from app.application.audit_activity import record_activity
from app.application.user_profile import build_user_profile
from app.database import get_db
from app.models import User
from app.schemas_auth import UserAdminCreateIn, UserAdminUpdateIn, UserListOut, UserRead

router = APIRouter(prefix="/users/manage", tags=["users"])


def _user_read(user: User) -> UserRead:
    return UserRead.model_validate(user)


@router.get("", response_model=UserListOut)
def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_manage_users_access),
):
    return UserListOut(items=[_user_read(u) for u in repo.list_users(db)])


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserAdminCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(require_manage_users_access),
):
    try:
        hibp.assert_password_not_pwned(body.password)
    except ValueError as exc:
        if str(exc) == "password_pwned":
            raise HTTPException(status_code=400, detail="password_pwned") from exc
        raise
    if repo.get_user_by_email(db, body.email):
        raise HTTPException(status_code=400, detail="email_taken")
    username = (body.username or body.email.split("@")[0]).strip()[:64]
    if not username:
        raise HTTPException(status_code=400, detail="invalid_username")
    base = username
    n = 1
    while repo.get_user_by_username(db, username):
        username = f"{base}{n}"[:64]
        n += 1
    user = repo.create_user(
        db,
        username=username,
        email=body.email,
        password_hash=hash_password(body.password),
        role=body.role,
        email_verified=True,
        full_name=(body.full_name or "").strip() or None,
    )
    record_activity(
        db,
        action="users.created",
        user_id=actor.id,
        ip=get_client_ip(request),
        target_user_id=user.id,
        target_email=user.email,
        target_role=user.role,
    )
    return _user_read(user)


@router.patch("/{user_id}", response_model=UserRead)
def update_user(
    user_id: int,
    body: UserAdminUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(require_manage_users_access),
):
    user = repo.get_user_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="empty_update")
    audit_fields = sorted(fields.keys())

    if user.id == actor.id:
        if fields.get("is_active") is False:
            raise HTTPException(status_code=400, detail="cannot_deactivate_self")
        if "role" in fields and fields["role"] != "developer":
            raise HTTPException(status_code=400, detail="cannot_change_own_role")

    if "full_name" in fields:
        user.full_name = (fields["full_name"] or "").strip() or None
    if "role" in fields:
        user.role = fields["role"]
    if "is_active" in fields:
        user.is_active = bool(fields["is_active"])

    if "password" in fields:
        try:
            hibp.assert_password_not_pwned(fields["password"])
        except ValueError as exc:
            if str(exc) == "password_pwned":
                raise HTTPException(status_code=400, detail="password_pwned") from exc
            raise
        user = repo.set_password(db, user.id, hash_password(fields["password"]))
        if not verify_password(fields["password"], user.password_hash):
            raise HTTPException(status_code=500, detail="password_not_saved")
    else:
        db.commit()
        db.refresh(user)
    record_activity(
        db,
        action="users.updated",
        user_id=actor.id,
        ip=get_client_ip(request),
        target_user_id=user.id,
        updated_fields=audit_fields,
    )
    return _user_read(user)


@router.get("/{user_id}/profile")
def get_user_profile(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_manage_users_access),
):
    user = repo.get_user_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    return build_user_profile(db, user)
