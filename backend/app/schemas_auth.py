"""Схемы auth API."""
from __future__ import annotations

from datetime import datetime

from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    mfa_required: bool = False
    mfa_pending_token: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None


class MfaVerifyLoginRequest(BaseModel):
    mfa_pending_token: str
    code: str = Field(min_length=6, max_length=16)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str


class RefreshRequest(BaseModel):
    refresh_token: str


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    username: str | None = None


class UserRead(BaseModel):
    id: int
    username: str
    full_name: str | None = None
    email: str | None
    role: str
    is_active: bool
    email_verified: bool
    mfa_enabled: bool
    last_login_at: datetime | None = None

    model_config = {"from_attributes": True}


class UserListOut(BaseModel):
    items: list[UserRead] = Field(default_factory=list)


class UserAdminCreateIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    username: str | None = Field(default=None, max_length=64)
    full_name: str | None = Field(default=None, max_length=255)
    role: Literal["developer", "admin", "user"] = "user"


class UserAdminUpdateIn(BaseModel):
    password: str | None = Field(default=None, min_length=8)
    full_name: str | None = Field(default=None, max_length=255)
    role: Literal["developer", "admin", "user"] | None = None
    is_active: bool | None = None


class ChangeMyPasswordIn(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


class MfaSetupResponse(BaseModel):
    secret: str
    provisioning_uri: str
    backup_codes: list[str]


class MfaEnableRequest(BaseModel):
    code: str


class MfaDisableRequest(BaseModel):
    code: str


class EmailVerifyRequest(BaseModel):
    token: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10)
    new_password: str = Field(min_length=8)


class JobStatusOut(BaseModel):
    id: str
    status: str
    summary_date: str
    created_count: int
    failed_count: int
    result: dict | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class AuditLogOut(BaseModel):
    id: int
    user_id: int | None
    action: str
    details: dict
    ip: str | None
    created_at: datetime


class ScheduleConfigOut(BaseModel):
    config: dict


class ScheduleConfigIn(BaseModel):
    config: dict
