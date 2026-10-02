"""JWT, пароли, MFA — без FastAPI."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
import jwt
import pyotp
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field

from app.config import settings

TokenType = Literal["access", "refresh", "mfa"]

_BCRYPT_ROUNDS = 12
_dummy_hash_cache: str | None = None


def get_dummy_hash() -> str:
    """Hash для constant-time сравнения, если email не найден."""
    global _dummy_hash_cache
    if _dummy_hash_cache is None:
        _dummy_hash_cache = bcrypt.hashpw(
            b"dummy-password-for-timing",
            bcrypt.gensalt(rounds=_BCRYPT_ROUNDS),
        ).decode("utf-8")
    return _dummy_hash_cache


def _password_bytes(password: str) -> bytes:
    raw = password.encode("utf-8")
    # bcrypt принимает не более 72 байт
    return raw[:72] if len(raw) > 72 else raw


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_password_bytes(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


class TokenPayload(BaseModel):
    sub: str
    type: TokenType
    jti: str
    iss: str
    aud: str
    iat: datetime
    exp: datetime
    tv: int = 1
    role: str | None = None
    family: str | None = None


def hash_token(token: str) -> str:
    return hmac.new(settings.jwt_secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def generate_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _build_payload(
    user_id: int,
    token_type: TokenType,
    role: str | None,
    ttl: timedelta,
    token_version: int,
    family_id: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": token_type,
        "jti": uuid.uuid4().hex,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + ttl,
        "tv": token_version,
    }
    if role is not None:
        payload["role"] = role
    if family_id is not None:
        payload["family"] = family_id
    return payload


def create_access_token(user_id: int, role: str, token_version: int) -> str:
    ttl = timedelta(minutes=settings.access_token_expire_minutes)
    return _encode(_build_payload(user_id, "access", role, ttl, token_version))


def create_refresh_token(
    user_id: int, role: str, token_version: int, family_id: str | None = None
) -> tuple[str, str, str, datetime]:
    family_id = family_id or str(uuid.uuid4())
    ttl = timedelta(days=settings.refresh_token_expire_days)
    payload = _build_payload(user_id, "refresh", role, ttl, token_version, family_id)
    jti = payload["jti"]
    expires_at = payload["exp"]
    return _encode(payload), jti, family_id, expires_at


def create_mfa_pending_token(user_id: int, role: str, token_version: int) -> str:
    return _encode(_build_payload(user_id, "mfa", role, timedelta(minutes=5), token_version))


def decode_token(token: str, expected_type: TokenType) -> TokenPayload:
    try:
        raw = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            leeway=30,
        )
        payload = TokenPayload(**raw)
    except Exception as exc:
        raise ValueError("invalid_token") from exc
    if payload.type != expected_type:
        raise ValueError("invalid_token_type")
    return payload


def _fernet() -> Fernet:
    import base64

    raw = (settings.mfa_encryption_key or settings.jwt_secret).encode()
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)


def encrypt_mfa_secret(secret: str) -> str:
    f = _fernet()
    assert f is not None
    return f.encrypt(secret.encode()).decode()


def decrypt_mfa_secret(value: str) -> str:
    f = _fernet()
    assert f is not None
    try:
        return f.decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("invalid_mfa_config") from exc


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="Svodki PDF")


def verify_totp_code(secret: str, code: str) -> bool:
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


def generate_backup_codes(count: int = 8) -> list[str]:
    return [secrets.token_hex(4) for _ in range(count)]


def hash_backup_codes(codes: list[str]) -> str:
    return json.dumps([hash_token(c) for c in codes])


def verify_backup_code(stored_json: str | None, code: str) -> bool:
    if not stored_json:
        return False
    try:
        hashes = json.loads(stored_json)
    except json.JSONDecodeError:
        return False
    h = hash_token(code.strip())
    return h in hashes


def consume_backup_code(stored_json: str | None, code: str) -> str | None:
    if not stored_json:
        return None
    try:
        hashes = json.loads(stored_json)
    except json.JSONDecodeError:
        return None
    h = hash_token(code.strip())
    if h not in hashes:
        return None
    hashes.remove(h)
    return json.dumps(hashes)
