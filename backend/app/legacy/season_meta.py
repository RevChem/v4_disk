"""Сезон и диапазон дат по группам работ (как в сводке 2 / уборка 10)."""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import settings
from app.legacy.json_store_io import adopt_legacy_json, atomic_write_text, read_json_dict, serialized_rmw
from app.legacy.manual_overrides import combine_season_start_for_date

# Как season_unified.GROUP_SEASON_RULE (year_plus_one)
_SEASON_YEAR_PLUS_ONE_PREFIXES = (
    "2.4 Вспашка",
    "3.1 Опрыскивание",
    "3.2 Опрыскивание (овощи)",
    "4.0 Посев",
    "4.1 Посев",
)

_FERT_LIKE_PREFIXES = (
    "1.1 ",
    "3.1 ",
    "3.2 ",
    "5.4 ",
)

_BORON_PREFIX = "2.1 Боронование"
_HARVEST_GRAIN_PREFIX = "5.0 Уборка"


def _upload_dir() -> Path:
    return Path(settings.upload_dir)


def season_ranges_path() -> Path:
    configured = (getattr(settings, "svodki_season_ranges_path", None) or "").strip()
    if configured:
        return Path(configured)
    return _upload_dir() / "season_ranges.json"


def _normalize_years(raw: Any) -> list[int]:
    """Приводит season_years / season_year к отсортированному списку уникальных лет."""
    years: list[int] = []
    if raw is None:
        return years
    if isinstance(raw, (list, tuple, set)):
        for item in raw:
            try:
                y = int(item)
            except (TypeError, ValueError):
                continue
            if 1990 <= y <= 2100:
                years.append(y)
    else:
        try:
            y = int(raw)
            if 1990 <= y <= 2100:
                years.append(y)
        except (TypeError, ValueError):
            pass
    return sorted(set(years))


def _years_from_override(ov: dict[str, Any]) -> list[int]:
    years = _normalize_years(ov.get("season_years"))
    if years:
        return years
    return _normalize_years(ov.get("season_year"))


@lru_cache(maxsize=1)
def _season_calendar_module():
    plan_path = Path(settings.svodki_root).expanduser()
    if not plan_path.is_absolute():
        plan_path = (Path.cwd() / plan_path).resolve()
    mod_path = plan_path / "plan" / "utils" / "season" / "season_calendar.py"
    if not mod_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("svodki_season_calendar", mod_path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def current_season_year(report_date: date) -> int:
    """Номер сезона даты (07.10.2026 и 15.03.2027 -> 2027); единый хелпер season_calendar."""
    cal_mod = _season_calendar_module()
    if cal_mod and hasattr(cal_mod, "season_year_for_date"):
        return cal_mod.season_year_for_date(report_date)
    return report_date.year + (1 if report_date.month >= 7 else 0)


def load_full_season_store(report_date: date | None = None) -> dict[str, Any]:
    path = season_ranges_path()
    adopt_legacy_json(path, _upload_dir() / "season_ranges.json")
    # Битый файл уходит в *.corrupt-<ts> с записью в лог; сбой чтения — исключение (не {}).
    raw: dict[str, Any] = read_json_dict(path, label="season_ranges")
    cal_mod = _season_calendar_module()
    if cal_mod:
        calendar = cal_mod.normalize_calendar_seasons(raw.get("calendar_seasons"), report_date)
    else:
        calendar = {}
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


def save_full_season_store(store: dict[str, Any], report_date: date | None = None) -> Path:
    path = season_ranges_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cal_mod = _season_calendar_module()
    calendar = store.get("calendar_seasons") or {}
    if cal_mod:
        calendar = cal_mod.normalize_calendar_seasons(calendar, report_date)
    by_group = store.get("by_group") if isinstance(store.get("by_group"), dict) else {}
    payload = {"calendar_seasons": calendar, "by_group": by_group}
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return path


def load_calendar_seasons(report_date: date | None = None) -> dict[str, dict[str, str]]:
    return load_full_season_store(report_date)["calendar_seasons"]


def merged_period_for_group_years(
    season_years: list[int],
    report_date: date,
    calendar: dict[str, dict[str, str]] | None = None,
) -> tuple[date, date]:
    cal_mod = _season_calendar_module()
    if cal_mod:
        return cal_mod.merged_period_for_years(season_years, report_date, calendar)
    y = report_date.year
    return date(y, 3, 1), report_date


def load_season_range_overrides() -> dict[str, Any]:
    return load_full_season_store()["by_group"]


@serialized_rmw(name="season_ranges")
def save_season_range_overrides(by_group: dict[str, Any]) -> Path:
    store = load_full_season_store()
    cleaned: dict[str, Any] = {}
    for name, meta in (by_group or {}).items():
        key = str(name or "").strip()
        if not key or not isinstance(meta, dict):
            continue
        item: dict[str, Any] = {}
        years = _years_from_override(meta)
        if years:
            item["season_years"] = years
            item["season_year"] = years[-1]
        if item:
            cleaned[key] = item
    store["by_group"] = cleaned
    return save_full_season_store(store)


def _validate_calendar_seasons(calendar: dict[str, Any]) -> None:
    """Раньше некорректные строки молча отбрасывались, а UI писал «сохранено»."""
    for key, val in (calendar or {}).items():
        label = str(key).strip() or "?"
        if not isinstance(val, dict):
            raise ValueError(f"Сезон {label}: некорректные данные.")
        start = _parse_iso_date(val.get("period_start"))
        end = _parse_iso_date(val.get("period_end"))
        if start is None or end is None:
            raise ValueError(f"Сезон {label}: укажите начало и конец периода (дата ГГГГ-ММ-ДД).")
        if start > end:
            raise ValueError(
                f"Сезон {label}: начало ({start.isoformat()}) позже конца ({end.isoformat()})."
            )


@serialized_rmw(name="season_ranges")
def set_calendar_seasons(calendar: dict[str, Any], report_date: date | None = None) -> dict[str, dict[str, str]]:
    _validate_calendar_seasons(calendar)
    store = load_full_season_store(report_date)
    cal_mod = _season_calendar_module()
    if cal_mod:
        store["calendar_seasons"] = cal_mod.normalize_calendar_seasons(calendar, report_date)
    else:
        store["calendar_seasons"] = {}
    save_full_season_store(store, report_date)
    return store["calendar_seasons"]


def _parse_iso_date(raw: Any) -> date | None:
    text = str(raw or "").strip().split("T")[0]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


@serialized_rmw(name="season_ranges")
def set_season_range_override(
    group_name: str,
    *,
    period_start: str | None = None,
    period_end: str | None = None,
    season_year: int | None = None,
    season_years: list[int] | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    name = str(group_name or "").strip()
    if not name:
        raise ValueError("Укажите группу работ.")
    # Границы группы задаёт календарь сезонов. Молчаливый 200 оставлял бы
    # клиента с датами, которые не сохранились.
    if str(period_start or "").strip() or str(period_end or "").strip():
        raise ValueError(
            "period_start и period_end для группы не сохраняются. "
            "Даты сезона задаются в календаре сезонов."
        )
    store = load_season_range_overrides()
    if clear:
        store.pop(name, None)
        save_season_range_overrides(store)
        return store

    years: list[int] | None = None
    if season_years is not None:
        years = _normalize_years(season_years)
        if not years:
            raise ValueError("Выберите хотя бы один сезон (год).")
    elif season_year is not None:
        years = _normalize_years([season_year])

    item: dict[str, Any] = {}
    if years is not None:
        item["season_years"] = years
        item["season_year"] = years[-1]

    has_any = bool(_years_from_override(item))
    if not has_any:
        store.pop(name, None)
    else:
        store[name] = item
    save_season_range_overrides(store)
    return store


def _default_work_season_start(report_date: date) -> date:
    raw = (settings.svodki_season_start_date or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw.split("T")[0])
        except ValueError:
            pass
    return date(report_date.year, 3, 14)


def _fert_period_start(report_date: date) -> date:
    return date(report_date.year, 3, 1)


def _harvest_period_start(report_date: date) -> date:
    return combine_season_start_for_date(report_date)


def _matches_prefix(group_name: str, prefix: str) -> bool:
    return group_name.strip().startswith(prefix) or group_name.strip() == prefix.rstrip()


def season_year_for_group(group_name: str, report_date: date) -> int:
    """Основной (последний) год сезона — для совместимости."""
    years = season_years_for_group(group_name, report_date)
    return years[-1] if years else report_date.year


@lru_cache(maxsize=1)
def _season_unified_module():
    plan_path = Path(settings.svodki_root).expanduser()
    if not plan_path.is_absolute():
        plan_path = (Path.cwd() / plan_path).resolve()
    mod_path = plan_path / "plan" / "utils" / "season" / "season_unified.py"
    if not mod_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("season_unified", mod_path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def season_years_for_group(group_name: str, report_date: date) -> list[int]:
    """
    Годы Cropwise-сезона по умолчанию (season_unified — как proverka).
    """
    name = str(group_name or "").strip()
    su = _season_unified_module()
    if su is not None:
        hist = su.default_history_years_for_group(name, report_date)
        if hist:
            return hist
    for prefix in _SEASON_YEAR_PLUS_ONE_PREFIXES:
        if _matches_prefix(name, prefix):
            cur = current_season_year(report_date)
            return [cur - 1, cur]
    return [report_date.year]


def period_start_for_group(group_name: str, report_date: date) -> date:
    name = str(group_name or "").strip()
    if _matches_prefix(name, _HARVEST_GRAIN_PREFIX) or name == "5.0 Уборка":
        return _harvest_period_start(report_date)
    if _matches_prefix(name, _BORON_PREFIX):
        return date(report_date.year, 1, 1)
    for prefix in _FERT_LIKE_PREFIXES:
        if name.startswith(prefix):
            return _fert_period_start(report_date)
    if "Уборка картофеля" in name or ("овощ" in name.lower() and name.startswith("5.")):
        return _fert_period_start(report_date)
    return _default_work_season_start(report_date)


def format_ru_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def _format_years_label(years: list[int]) -> str:
    if not years:
        return "—"
    if len(years) == 1:
        return str(years[0])
    return ", ".join(str(y) for y in years)


def _build_group_meta(
    name: str,
    report_date: date,
    override: dict[str, Any] | None = None,
    calendar: dict[str, dict[str, str]] | None = None,
    base_store: dict[str, Any] | None = None,
) -> dict[str, Any]:
    default_start = period_start_for_group(name, report_date)
    default_end = report_date
    default_years = season_years_for_group(name, report_date)
    su = _season_unified_module()
    if su is not None:
        store = base_store if base_store is not None else load_full_season_store(report_date)
        if calendar is not None:
            store = {**store, "calendar_seasons": calendar}
        if override:
            bg = dict(store.get("by_group") or {})
            bg[name] = {**(bg.get(name) or {}), **override}
            store = {**store, "by_group": bg}
        cfg = su.resolve_group_season_config(name, report_date, store=store)
        years = list(cfg.get("history_years") or default_years)
        start = date.fromisoformat(str(cfg["period_start"]))
        end = date.fromisoformat(str(cfg["period_end"]))
        has_override = bool(cfg.get("has_file_override"))
        year_label = _format_years_label(years)
        return {
            "group_name": name,
            "season_year": years[-1] if years else report_date.year,
            "season_years": years,
            "task_season_years": cfg.get("task_season_years") or [],
            "season_rule": cfg.get("season_rule"),
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "period_start_ru": format_ru_date(start),
            "period_end_ru": format_ru_date(end),
            "default_period_start": default_start.isoformat(),
            "default_period_end": default_end.isoformat(),
            "default_season_year": default_years[-1] if default_years else report_date.year,
            "default_season_years": default_years,
            "season_year_options": sorted(
                {
                    current_season_year(report_date) - 2,
                    current_season_year(report_date) - 1,
                    current_season_year(report_date),
                    current_season_year(report_date) + 1,
                    *default_years,
                    *years,
                }
            ),
            "has_override": has_override,
            "label": (
                f"Операция: {name} · Сезон{'ы' if len(years) > 1 else ''}: {year_label} · "
                f"Диапазон: {format_ru_date(start)}–{format_ru_date(end)}"
            ),
        }
    ov = override or {}
    years = _years_from_override(ov) or list(default_years)
    calendar = calendar if calendar is not None else load_calendar_seasons(report_date)
    start, end = merged_period_for_group_years(years, report_date, calendar)
    has_override = bool(_years_from_override(ov))
    year_label = _format_years_label(years)
    return {
        "group_name": name,
        "season_year": years[-1] if years else report_date.year,
        "season_years": years,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "period_start_ru": format_ru_date(start),
        "period_end_ru": format_ru_date(end),
        "default_period_start": default_start.isoformat(),
        "default_period_end": default_end.isoformat(),
        "default_season_year": default_years[-1] if default_years else report_date.year,
        "default_season_years": default_years,
        "season_year_options": sorted(
            {
                current_season_year(report_date) - 2,
                current_season_year(report_date) - 1,
                current_season_year(report_date),
                current_season_year(report_date) + 1,
                *default_years,
                *years,
            }
        ),
        "has_override": has_override,
        "label": (
            f"Операция: {name} · Сезон{'ы' if len(years) > 1 else ''}: {year_label} · "
            f"Диапазон: {format_ru_date(start)}–{format_ru_date(end)}"
        ),
    }


def season_meta_for_groups(group_names: list[str], report_date: date | None = None) -> dict[str, Any]:
    """
    Метаданные сезона по группам WORK_GROUPS для даты платформы.
    Учитывает ручные правки из uploads/season_ranges.json.
    """
    end = report_date or date.today()
    store = load_full_season_store(end)
    overrides = store["by_group"]
    calendar = store["calendar_seasons"]
    by_group: dict[str, dict[str, Any]] = {}
    for raw_name in group_names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        by_group[name] = _build_group_meta(name, end, overrides.get(name), calendar, store)
    return {
        "report_date": end.isoformat(),
        "report_date_ru": format_ru_date(end),
        "path": str(season_ranges_path()),
        "calendar_seasons": calendar,
        "by_group": by_group,
    }


def inject_season_range_env(env: dict[str, str], report_date: date | None = None) -> None:
    """
    Подставляет сезоны всех групп через season_unified (единый резолвер).
    """
    rd = report_date or date.today()
    path = season_ranges_path()
    if path.is_file():
        env.setdefault("SVODKI_SEASON_RANGES_PATH", str(path.resolve()))
        env.setdefault("SVODKI_UPLOAD_DIR", str(_upload_dir().resolve()))

    su = _season_unified_module()
    if su is not None:
        su.inject_season_range_env(env, rd)
        return

    overrides = load_season_range_overrides()
    if not overrides:
        return
    _ = rd
