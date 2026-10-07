"""Календарные сезоны: диапазоны дат по году сезона и их объединение."""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Сезон Y идёт с 01.07.(Y-1) по 01.07.Y, поэтому с июля календарный год уже
# относится к следующему сезону.
SEASON_START_MONTH = 7


def season_year_for_date(d: date) -> int:
    """Номер сезона, в котором лежит дата: 07.10.2026 -> 2027, 15.03.2027 -> 2027.

    Единственное место, где считается «текущий сезон». Всё, что раньше считало
    ``report_date.year + 1``, должно вызывать эту функцию: такая формула верна
    только с июля по декабрь.
    """
    return d.year + (1 if d.month >= SEASON_START_MONTH else 0)


def default_calendar_entry(season_year: int) -> dict[str, str]:
    """Сезон Y: 01.07.(Y-1) — 01.07.Y (как в редакторе планов)."""
    y = int(season_year)
    return {
        "period_start": f"{y - 1}-07-01",
        "period_end": f"{y}-07-01",
    }


def default_calendar_seasons(report_date: date | None = None) -> dict[str, dict[str, str]]:
    rd = report_date or date.today()
    y = season_year_for_date(rd)
    years = [y - 2, y - 1, y, y + 1]
    return {str(yr): default_calendar_entry(yr) for yr in years}


def _parse_iso(raw: Any) -> date | None:
    text = str(raw or "").strip().split("T")[0]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def normalize_calendar_seasons(raw: Any, report_date: date | None = None) -> dict[str, dict[str, str]]:
    defaults = default_calendar_seasons(report_date)
    if not isinstance(raw, dict):
        return defaults
    out = dict(defaults)
    for key, val in raw.items():
        if not isinstance(val, dict):
            continue
        sk = str(key).strip()
        if not sk:
            continue
        start = _parse_iso(val.get("period_start"))
        end = _parse_iso(val.get("period_end"))
        if start and end and start <= end:
            out[sk] = {
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
            }
    return out


def season_ranges_file_path() -> Path | None:
    configured = (os.environ.get("SVODKI_SEASON_RANGES_PATH") or "").strip()
    if configured:
        return Path(configured)
    upload = (os.environ.get("SVODKI_UPLOAD_DIR") or os.environ.get("UPLOAD_DIR") or "").strip()
    if upload:
        return Path(upload) / "season_ranges.json"
    return None


def _quarantine_corrupt(path: Path) -> None:
    """Битый season_ranges.json -> *.corrupt-<ts> (как json_store_io в бэкенде)."""
    try:
        from datetime import datetime

        target = path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d%H%M%S}")
        path.replace(target)
        logger.error("season_ranges: битый файл перенесён в %s", target)
    except OSError as exc:
        logger.error("season_ranges: не удалось переименовать %s: %s", path, exc)


def load_season_ranges_store(report_date: date | None = None) -> dict[str, Any]:
    path = season_ranges_file_path()
    raw: dict[str, Any] = {}
    if path and path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except json.JSONDecodeError as exc:
            # Битый файл не должен молча подменять пользовательские сезоны
            # дефолтами: уносим его в *.corrupt-<ts> и пишем в лог.
            logger.error("season_ranges: битый JSON в %s: %s", path, exc)
            _quarantine_corrupt(path)
        except OSError as exc:
            # Временный сбой чтения: файл не трогаем.
            logger.error("season_ranges: не удалось прочитать %s: %s", path, exc)
    calendar = normalize_calendar_seasons(raw.get("calendar_seasons"), report_date)
    by_group: dict[str, Any] = {}
    bg = raw.get("by_group")
    if isinstance(bg, dict):
        by_group = {str(k): v for k, v in bg.items() if isinstance(v, dict)}
    elif isinstance(raw, dict):
        by_group = {
            str(k): v
            for k, v in raw.items()
            if isinstance(v, dict) and k not in ("by_group", "calendar_seasons")
        }
    return {"calendar_seasons": calendar, "by_group": by_group}


def load_calendar_seasons(report_date: date | None = None) -> dict[str, dict[str, str]]:
    return load_season_ranges_store(report_date)["calendar_seasons"]


def merged_period_for_years(
    season_years: list[int],
    report_date: date,
    calendar: dict[str, dict[str, str]] | None = None,
) -> tuple[date, date]:
    """
    Объединённый диапазон выбранных сезонов, обрезанный датой отчёта.

    Пример: 2026+2027 при отчёте 2026-09-01 → 2025-07-01 … 2026-09-01.
    Только 2027 → 2026-07-01 … 2026-09-01.
    """
    cal = calendar or load_calendar_seasons(report_date)
    years = sorted({int(y) for y in season_years if 1990 <= int(y) <= 2100})
    if not years:
        return date(report_date.year, 3, 1), report_date

    starts: list[date] = []
    ends: list[date] = []
    for y in years:
        entry = cal.get(str(y)) or default_calendar_entry(y)
        ps = _parse_iso(entry.get("period_start")) or default_calendar_entry(y)["period_start"]
        pe = _parse_iso(entry.get("period_end")) or default_calendar_entry(y)["period_end"]
        if isinstance(ps, str):
            ps = date.fromisoformat(ps)
        if isinstance(pe, str):
            pe = date.fromisoformat(pe)
        starts.append(ps)
        ends.append(pe)

    merged_start = min(starts)
    merged_end = min(max(ends), report_date)
    if merged_start > merged_end:
        # Сезон целиком в будущем относительно даты отчёта (например 2028 на
        # 07.10.2026). Окно с началом позже конца давало пустой факт без
        # объяснения; берём однодневное окно [report_date, report_date].
        logger.warning(
            "merged_period_for_years: сезоны %s начинаются %s, позже даты отчёта %s",
            years, merged_start, report_date,
        )
        merged_start = merged_end = report_date
    return merged_start, merged_end


def format_period_ru(start: date, end: date) -> str:
    return f"{start.strftime('%d.%m.%Y')}–{end.strftime('%d.%m.%Y')}"


def fact_window_end(period_end: date, report_date: date) -> date:
    """Конец окна сезонного факта: не обрезать раньше даты отчёта.

    В редакторе у одного календарного сезона period_end часто 01.07; без
    поднятия до report_date суточный/накопленный факт после этой даты обнуляется.
    """
    if period_end < report_date:
        return report_date
    return period_end
