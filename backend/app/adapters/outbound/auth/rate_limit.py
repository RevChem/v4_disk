"""Rate limit для auth-эндпоинтов (Redis или in-memory fallback)."""
from __future__ import annotations

import time
from collections import defaultdict

from app.config import settings
from app.infrastructure.redis_client import get_redis

_memory: dict[str, list[float]] = defaultdict(list)


def enforce_rate_limit(key: str, *, limit: int | None = None) -> None:
    limit = limit or settings.auth_rate_limit_per_minute
    now = time.time()
    redis = get_redis()
    bucket = f"rl:{key}"
    if redis is not None:
        count = redis.incr(bucket)
        if count == 1:
            redis.expire(bucket, 60)
        if count > limit:
            raise ValueError("rate_limited")
        return
    window = _memory[key]
    window[:] = [t for t in window if now - t < 60]
    if len(window) >= limit:
        raise ValueError("rate_limited")
    window.append(now)
