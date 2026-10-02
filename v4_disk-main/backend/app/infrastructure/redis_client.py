"""Redis-клиент с graceful fallback."""
from __future__ import annotations

from functools import lru_cache

import redis

from app.config import settings


@lru_cache
def get_redis() -> redis.Redis | None:
    try:
        client = redis.from_url(settings.redis_url, decode_responses=True)
        client.ping()
        return client
    except Exception:
        return None
