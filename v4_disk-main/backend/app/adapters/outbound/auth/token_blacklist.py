"""JWT blacklist в Redis."""
from __future__ import annotations

import time

from app.infrastructure.redis_client import get_redis


def revoke_jti(jti: str, exp_ts: int) -> None:
    redis = get_redis()
    if redis is None:
        return
    ttl = max(1, exp_ts - int(time.time()))
    redis.setex(f"bl:{jti}", ttl, "1")


def is_revoked(jti: str) -> bool:
    redis = get_redis()
    if redis is None:
        return False
    return bool(redis.get(f"bl:{jti}"))
