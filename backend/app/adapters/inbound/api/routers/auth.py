"""Auth endpoints."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.adapters.inbound.api.deps import get_client_ip, get_current_active_user, oauth2_scheme
from app.adapters.outbound.auth import hibp
from app.adapters.outbound.auth.rate_limit import enforce_rate_limit
from app.adapters.outbound.auth.security import (
    get_dummy_hash,
    consume_backup_code,
    create_access_token,
    create_mfa_pending_token,
    create_refresh_token,
    decode_token,
    decrypt_mfa_secret,
    encrypt_mfa_secret,
    generate_backup_codes,
    generate_opaque_token,
    generate_totp_secret,
    hash_backup_codes,
    hash_password,
    totp_provisioning_uri,
    verify_backup_code,
    verify_password,
    verify_totp_code,
)
from app.adapters.outbound.auth.token_blacklist import is_revoked, revoke_jti
from app.adapters.outbound.mail.smtp import send_email
from app.adapters.outbound.persistence import repositories as repo
from app.config import settings
from app.database import get_db
from app.infrastructure.logging import get_logger
from app.models import User
from app.schemas_auth import (
    EmailVerifyRequest,
    LoginRequest,
    LoginResponse,
    MfaDisableRequest,
    MfaEnableRequest,
    MfaSetupResponse,
    MfaVerifyLoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResendVerificationRequest,
    TokenPair,
    UserRead,
)

router = APIRouter(tags=["auth"])
users_router = APIRouter(prefix="/users", tags=["users"])
auth_router = APIRouter(prefix="/auth", tags=["auth"])


def _normalize_time(started: float) -> None:
    elapsed = time.monotonic() - started
    if elapsed < 0.3:
        time.sleep(0.3 - elapsed)


log = get_logger(__name__)


@users_router.get("/me/profile")
def my_profile(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Свой профиль: входы, запуски сводок и внесённые изменения."""
    from app.application.user_profile import build_user_profile

    return build_user_profile(db, current_user)


@users_router.get("/me", response_model=UserRead)
def get_me(current_user: User = Depends(get_current_active_user)):
    return current_user


@users_router.post("/register", status_code=status.HTTP_200_OK)
def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    started = time.monotonic()
    try:
        enforce_rate_limit(f"register:{get_client_ip(request)}")
        hibp.assert_password_not_pwned(body.password)
        if repo.get_user_by_email(db, body.email):
            return {"detail": "Если email свободен, аккаунт создан. Проверьте почту."}
        username = body.username or body.email.split("@")[0][:64]
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
            role="user",
            email_verified=not settings.require_email_verification,
        )
        if settings.require_email_verification:
            token = generate_opaque_token()
            repo.create_email_verification_token(db, user.id, token)
            link = f"{settings.public_base_url}/verify?token={token}"
            send_email(body.email, "Подтверждение email", f"Перейдите по ссылке: {link}")
        from app.application.audit_activity import record_activity

        record_activity(db, action="auth.register", user_id=user.id, ip=get_client_ip(request))
        return {"detail": "Если email свободен, аккаунт создан. Проверьте почту."}
    except ValueError as exc:
        if str(exc) == "rate_limited":
            raise HTTPException(status_code=429, detail="rate_limited") from exc
        if str(exc) == "password_pwned":
            raise HTTPException(status_code=400, detail="password_pwned") from exc
        raise
    finally:
        _normalize_time(started)


@users_router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    started = time.monotonic()
    ip = get_client_ip(request)
    try:
        enforce_rate_limit(f"login:{ip}")
        user = repo.get_user_by_email(db, body.email)
        if user is None:
            verify_password(body.password, get_dummy_hash())
            log.info("auth.login.failed", email=body.email, reason="bad_credentials", ip=ip)
            raise HTTPException(status_code=401, detail="login_failed")
        if repo.is_locked(user):
            log.info("auth.login.failed", email=body.email, reason="locked", ip=ip)
            raise HTTPException(status_code=401, detail="login_failed")
        if not verify_password(body.password, user.password_hash):
            repo.record_failed_login(db, user)
            log.info("auth.login.failed", email=body.email, reason="bad_credentials", ip=ip)
            raise HTTPException(status_code=401, detail="login_failed")
        repo.reset_failed_login(db, user)
        if not user.is_active:
            raise HTTPException(status_code=401, detail="inactive_user")
        if settings.require_email_verification and not user.email_verified:
            raise HTTPException(status_code=401, detail="email_not_verified")
        if user.mfa_enabled:
            return LoginResponse(
                mfa_required=True,
                mfa_pending_token=create_mfa_pending_token(user.id, user.role, user.token_version),
            )
        access = create_access_token(user.id, user.role, user.token_version)
        refresh, jti, family_id, expires = create_refresh_token(user.id, user.role, user.token_version)
        repo.save_refresh_token(db, user.id, jti, family_id, expires)
        log.info("auth.login.success", user_id=user.id, email=body.email, ip=ip)
        from app.application.audit_activity import record_activity

        record_activity(db, action="auth.login.success", user_id=user.id, ip=ip)
        return LoginResponse(access_token=access, refresh_token=refresh)
    except ValueError as exc:
        if str(exc) == "rate_limited":
            raise HTTPException(status_code=429, detail="rate_limited") from exc
        raise
    finally:
        _normalize_time(started)


@auth_router.post("/mfa/verify-login", response_model=TokenPair)
def verify_mfa_login(body: MfaVerifyLoginRequest, request: Request, db: Session = Depends(get_db)):
    ip = get_client_ip(request)
    try:
        payload = decode_token(body.mfa_pending_token, "mfa")
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid_mfa_session") from None
    user = repo.get_user_by_id(db, int(payload.sub))
    if user is None or not user.mfa_enabled or not user.mfa_secret_encrypted:
        raise HTTPException(status_code=401, detail="invalid_mfa_session")
    secret = decrypt_mfa_secret(user.mfa_secret_encrypted)
    ok = verify_totp_code(secret, body.code)
    if not ok and user.mfa_backup_codes_hash:
        ok = verify_backup_code(user.mfa_backup_codes_hash, body.code)
        if ok:
            updated = consume_backup_code(user.mfa_backup_codes_hash, body.code)
            user.mfa_backup_codes_hash = updated
            db.commit()
    if not ok:
        log.info("auth.mfa.failed", user_id=user.id, ip=ip)
        raise HTTPException(status_code=401, detail="invalid_mfa_code")
    access = create_access_token(user.id, user.role, user.token_version)
    refresh, jti, family_id, expires = create_refresh_token(user.id, user.role, user.token_version)
    repo.save_refresh_token(db, user.id, jti, family_id, expires)
    log.info("auth.mfa.success", user_id=user.id, ip=ip)
    from app.application.audit_activity import record_activity

    record_activity(db, action="auth.login.success", user_id=user.id, ip=ip)
    return TokenPair(access_token=access, refresh_token=refresh)


@auth_router.post("/refresh", response_model=TokenPair)
def refresh(body: RefreshRequest, request: Request, db: Session = Depends(get_db)):
    enforce_rate_limit(f"refresh:{get_client_ip(request)}")
    try:
        payload = decode_token(body.refresh_token, "refresh")
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid_token") from None
    if is_revoked(payload.jti):
        raise HTTPException(status_code=401, detail="token_revoked")
    record = repo.consume_refresh_token(db, payload.jti)
    if record is None:
        existing = repo.get_refresh_by_jti(db, payload.jti)
        if existing and existing.revoked:
            repo.revoke_refresh_family(db, existing.family_id)
            raise HTTPException(status_code=401, detail="refresh_reuse")
        raise HTTPException(status_code=401, detail="invalid_token")
    user = repo.get_user_by_id(db, int(payload.sub))
    if user is None or not user.is_active or record.user_id != user.id:
        raise HTTPException(status_code=401, detail="invalid_refresh")
    if payload.tv != user.token_version:
        raise HTTPException(status_code=401, detail="token_revoked")
    family_id = payload.family or record.family_id
    access = create_access_token(user.id, user.role, user.token_version)
    refresh_token, jti, _, expires = create_refresh_token(
        user.id, user.role, user.token_version, family_id=family_id
    )
    repo.save_refresh_token(db, user.id, jti, family_id, expires)
    return TokenPair(access_token=access, refresh_token=refresh_token)


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    token: str = Depends(oauth2_scheme),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    try:
        payload = decode_token(token, "access")
        revoke_jti(payload.jti, int(payload.exp.timestamp()))
    except ValueError:
        pass
    from app.application.audit_activity import record_activity

    record_activity(
        db,
        action="auth.logout",
        user_id=current_user.id,
        ip=get_client_ip(request),
    )
    log.info("auth.logout", user_id=current_user.id)


@auth_router.post("/verify-email")
def verify_email(body: EmailVerifyRequest, db: Session = Depends(get_db)):
    record = repo.get_valid_email_verification_token(db, body.token)
    if record is None:
        raise HTTPException(status_code=400, detail="invalid_token")
    user = repo.get_user_by_id(db, record.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="invalid_token")
    repo.mark_email_verified(db, user, record)
    return {"detail": "Email подтверждён"}


@auth_router.post("/resend-verification")
def resend_verification(body: ResendVerificationRequest, db: Session = Depends(get_db)):
    user = repo.get_user_by_email(db, body.email)
    if user and not user.email_verified:
        token = generate_opaque_token()
        repo.create_email_verification_token(db, user.id, token)
        link = f"{settings.public_base_url}/verify?token={token}"
        send_email(body.email, "Подтверждение email", f"Перейдите по ссылке: {link}")
    return {"detail": "Если email существует и не подтверждён, письмо отправлено"}


@auth_router.post("/mfa/setup", response_model=MfaSetupResponse)
def mfa_setup(current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    if current_user.mfa_enabled:
        raise HTTPException(status_code=400, detail="mfa_already_enabled")
    secret = generate_totp_secret()
    current_user.mfa_secret_encrypted = encrypt_mfa_secret(secret)
    codes = generate_backup_codes()
    current_user.mfa_backup_codes_hash = hash_backup_codes(codes)
    db.commit()
    email = current_user.email or current_user.username
    return MfaSetupResponse(
        secret=secret,
        provisioning_uri=totp_provisioning_uri(secret, email),
        backup_codes=codes,
    )


@auth_router.post("/mfa/enable")
def mfa_enable(
    body: MfaEnableRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    if not current_user.mfa_secret_encrypted:
        raise HTTPException(status_code=400, detail="mfa_not_setup")
    secret = decrypt_mfa_secret(current_user.mfa_secret_encrypted)
    if not verify_totp_code(secret, body.code):
        raise HTTPException(status_code=400, detail="invalid_mfa_code")
    current_user.mfa_enabled = True
    db.commit()
    return {"detail": "MFA включена"}


@auth_router.post("/mfa/disable")
def mfa_disable(
    body: MfaDisableRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    if not current_user.mfa_enabled or not current_user.mfa_secret_encrypted:
        raise HTTPException(status_code=400, detail="mfa_not_enabled")
    secret = decrypt_mfa_secret(current_user.mfa_secret_encrypted)
    if not verify_totp_code(secret, body.code):
        raise HTTPException(status_code=400, detail="invalid_mfa_code")
    current_user.mfa_enabled = False
    current_user.mfa_secret_encrypted = None
    current_user.mfa_backup_codes_hash = None
    repo.bump_token_version(db, current_user)
    return {"detail": "MFA отключена"}


router.include_router(users_router)
router.include_router(auth_router)
