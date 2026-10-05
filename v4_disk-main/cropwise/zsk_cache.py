# cropwise/zsk_cache.py
# Общие константы и helpers Redis для простоев ЗСК (веб + bot API).
from __future__ import annotations

import json
from datetime import date, datetime

import redis

from app.config import get_redis_url
from app.logging_config import get_logger

logger = get_logger("cropwise.zsk_cache")

REDIS_KEY = "zsk_downtime_cache"
REDIS_CONFIG_KEY = "zsk_downtime_config"
REDIS_CLASSIFICATION_KEY = "zsk_downtime_classification"

_redis = redis.from_url(get_redis_url(), decode_responses=True)


def get_redis_client():
    return _redis


def get_zsk_date_range() -> tuple[date, date]:
    from app.cropwise.zsk_downtime_sync import default_date_range

    raw = _redis.get(REDIS_CONFIG_KEY)
    if raw:
        try:
            cfg = json.loads(raw)
            return date.fromisoformat(cfg["date_from"]), date.fromisoformat(cfg["date_to"])
        except Exception as e:
            logger.error(f"get_zsk_date_range: parse error, использую дефолт: {e}")

    now_server = datetime.now().replace(microsecond=0)
    return default_date_range(now_server)


def set_zsk_date_range(date_from: date, date_to: date) -> None:
    _redis.set(REDIS_CONFIG_KEY, json.dumps({
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
    }))
