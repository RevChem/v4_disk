"""Cropwise season year from server env (CROPWISE_SEASON_YEAR / SEASON_YEAR)."""

from __future__ import annotations

import os


def cropwise_season_year(fallback: int | None = None) -> int:
    for key in ("CROPWISE_SEASON_YEAR", "SEASON_YEAR"):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    if fallback is not None:
        return int(fallback)
    from datetime import date

    return date.today().year
