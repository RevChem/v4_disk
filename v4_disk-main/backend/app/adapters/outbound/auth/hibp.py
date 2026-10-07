"""Have I Been Pwned — k-anonymity check."""
from __future__ import annotations

import hashlib

import httpx

from app.config import settings


def password_is_pwned(password: str) -> bool:
    if not settings.hibp_enabled:
        return False
    sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"https://api.pwnedpasswords.com/range/{prefix}")
            resp.raise_for_status()
        for line in resp.text.splitlines():
            part, _count = line.split(":")
            if part == suffix:
                return True
    except Exception:
        return False
    return False


def assert_password_not_pwned(password: str) -> None:
    if password_is_pwned(password):
        raise ValueError("password_pwned")
