"""
Единый источник сезонов по группам работ (WORK_GROUPS).

Данные пользователя: uploads/season_ranges.json (редактор планов).
Правила (код): GROUP_SEASON_RULE ниже.

Все потребители (PDF, сводки 1/2, proverka) вызывают resolve_group_season_config().

Модель сезонов: сезон Y = 01.07.(Y-1) … 01.07.Y. Номер текущего сезона даёт
season_year_for_date(report_date) (07.10.2026 -> 2027, 15.03.2027 -> 2027);
``report_date.year + 1`` без учёта месяца использовать нельзя.

Что значат правила (по умолчанию, без правок в редакторе планов):
- history_years — сезоны, по которым берётся факт и история культур. Для
  year_plus_one / plow_year_plus_one / both_years это [текущий-1, текущий],
  как чипы «Сезоны» в редакторе и пример в merged_period_for_years.
- task_season_years — единственный «основной» сезон (текущий) для определения
  культуры (посев, вспашка, озимые в опрыскивании). Фильтром machine_task.season
  для факта он по умолчанию не служит: факт группы режет fact_season_years_for_group,
  то есть history_years, а при сохранённом выборе в редакторе — он.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

try:
    from season_calendar import season_year_for_date
except ImportError:  # season_calendar не в sys.path (отдельная загрузка модуля)
    def season_year_for_date(d: date) -> int:
        return d.year + (1 if d.month >= 7 else 0)

# Группа работ → season_rule
GROUP_SEASON_RULE: dict[str, str] = {
    "1.1 Внесение минеральных удобрений": "both_years",
    "2.1 Боронование": "none",
    "2.4 Вспашка": "plow_year_plus_one",
    "2.8 Глубокорыхление": "plow_year_plus_one",
    "2.9 Дискование": "both_years",
    "2.11 Культивация": "none",
    "2.12 Прикатывание": "none",
    "2.6 Фрезерование": "none",
    "2.7 Гребнеобразование": "none",
    "3.1 Опрыскивание": "year_plus_one",
    "3.2 Опрыскивание (овощи)": "year_plus_one",
    "4.0 Посев": "year_plus_one",
    "4.1 Посев (овощи)": "year_plus_one",
    "5.0 Уборка": "none",
    "5.4 Уборка картофеля, лука": "none",
}

# season_rule тематической сводки (report_id) — proverka ReportSpec
REPORT_SEASON_RULE: dict[str, str] = {
    "3": "both_years",
    "4": "none",
    "5": "year_plus_one",
    "6": "year_plus_one",
    "7": "both_years",
    "8": "none",
    "9": "plow_year_plus_one",
    "11": "none",
}

_STORE_CACHE: dict[str, Any] | None = None
_STORE_CACHE_DATE: date | None = None
_STORE_CACHE_SIG: tuple | None = None


def clear_season_cache() -> None:
    global _STORE_CACHE, _STORE_CACHE_DATE, _STORE_CACHE_SIG
    _STORE_CACHE = None
    _STORE_CACHE_DATE = None
    _STORE_CACHE_SIG = None


def _store_file_signature() -> tuple:
    """Путь, mtime и размер season_ranges.json. Смена файла сбрасывает кэш."""
    try:
        from season_calendar import season_ranges_file_path

        path = season_ranges_file_path()
    except ImportError:
        path = _season_ranges_path()
    if not path:
        return (None, None, None)
    try:
        st = Path(path).stat()
    except OSError:
        return (str(path), None, None)
    return (str(path), st.st_mtime_ns, st.st_size)


# Общие legacy-ключи (SVODKI_SPRAY_*, SVODKI_SEEDING_*) перезаписывались последней
# группой в пачке — в inject больше не пишем, только per-group ключи ниже.
_COLLIDING_SHARED_ENV_KEYS = frozenset({
    "SVODKI_SPRAY_SEASON_YEARS",
    "SVODKI_SPRAY_TASK_SEASON_YEARS",
    "SVODKI_SPRAY_PERIOD_START",
    "SVODKI_SPRAY_PERIOD_END",
    "SVODKI_SEEDING_SEASON_YEARS",
})


def _group_env_slug(group_name: str) -> str:
    slug = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "_", str(group_name or "").strip())
    return slug.strip("_")[:96]


def group_env_key(group_name: str, suffix: str) -> str:
    """Per-group env: SVODKI_GRP_<группа>_HISTORY_YEARS и т.д."""
    return f"SVODKI_GRP_{_group_env_slug(group_name)}_{suffix}"


def _years_from_env_var(key: str) -> list[int]:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return []
    return _normalize_years([part.strip() for part in raw.split(",") if part.strip()])


def _apply_per_group_env(name: str, ov: dict[str, Any]) -> None:
    hist = _years_from_env_var(group_env_key(name, "HISTORY_YEARS"))
    if hist:
        ov["history_years"] = hist
    task = _years_from_env_var(group_env_key(name, "TASK_SEASON_YEARS"))
    if task:
        ov["task_season_years"] = task
    ps = os.environ.get(group_env_key(name, "PERIOD_START"), "").strip()
    if ps:
        ov["period_start"] = ps.split("T")[0]
    pe = os.environ.get(group_env_key(name, "PERIOD_END"), "").strip()
    if pe:
        ov["period_end"] = pe.split("T")[0]


def season_rule_for_group(group_name: str) -> str:
    return GROUP_SEASON_RULE.get(str(group_name or "").strip(), "none")


def season_rule_for_report(report_id: str) -> str:
    return REPORT_SEASON_RULE.get(str(report_id).strip(), "none")


def task_years_from_rule(season_rule: str, report_date: date) -> list[int]:
    cur = season_year_for_date(report_date)
    if season_rule in ("year_plus_one", "plow_year_plus_one"):
        return [cur]
    if season_rule == "report_year":
        return [report_date.year]
    if season_rule == "both_years":
        return [cur - 1, cur]
    return []


def history_years_from_rule(season_rule: str, report_date: date) -> list[int]:
    cur = season_year_for_date(report_date)
    if season_rule in ("year_plus_one", "plow_year_plus_one", "both_years"):
        return [cur - 1, cur]
    if season_rule == "report_year":
        return [report_date.year]
    return [report_date.year]


def default_task_years_for_group(group_name: str, report_date: date) -> list[int]:
    return task_years_from_rule(season_rule_for_group(group_name), report_date)


def default_history_years_for_group(group_name: str, report_date: date) -> list[int]:
    return history_years_from_rule(season_rule_for_group(group_name), report_date)


def default_task_years_for_report_rule(season_rule: str, report_date: date) -> list[int]:
    return task_years_from_rule(season_rule, report_date)


def default_history_years_for_report_rule(season_rule: str, report_date: date) -> list[int]:
    return history_years_from_rule(season_rule, report_date)


def plow_only_for_rule(season_rule: str) -> bool:
    return season_rule == "plow_year_plus_one"


def applies_task_season_filter(group_name: str) -> bool:
    """Нужно ли фильтровать machine_task.season для этой группы."""
    rule = season_rule_for_group(group_name)
    if rule == "none":
        return False
    if rule == "plow_year_plus_one":
        return group_name.strip() == "2.4 Вспашка"
    return True


def merge_task_years_from_file(
    season_rule: str,
    report_date: date,
    group_years: list[int],
    *,
    has_file_override: bool,
) -> list[int]:
    """
    Выбор сезонов из редактора планов важнее правила в коде.

    Ранее для year_plus_one здесь оставался только последний год: выбор
    «2026 и 2027» в редакторе превращался в один сезон 2027, и опрыскивание
    считалось 2713.89 га вместо 6215.87 га за оба сезона.
    """
    if has_file_override and group_years:
        if season_rule in (
            "year_plus_one", "plow_year_plus_one", "both_years", "report_year",
        ):
            return list(group_years)
    return default_task_years_for_report_rule(season_rule, report_date)


def _normalize_years(raw: Any) -> list[int]:
    years: list[int] = []
    if raw is None:
        return years
    items = raw if isinstance(raw, (list, tuple, set)) else [raw]
    for item in items:
        try:
            y = int(item)
        except (TypeError, ValueError):
            continue
        if 1990 <= y <= 2100:
            years.append(y)
    return sorted(set(years))


def _years_from_meta(meta: dict | None) -> list[int]:
    if not meta:
        return []
    years = _normalize_years(meta.get("season_years"))
    if years:
        return years
    return _normalize_years(meta.get("season_year"))


def _parse_iso_date(raw: Any) -> date | None:
    text = str(raw or "").strip().split("T")[0]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _season_ranges_path() -> Path | None:
    configured = (os.environ.get("SVODKI_SEASON_RANGES_PATH") or "").strip()
    if configured:
        return Path(configured)
    upload = (os.environ.get("SVODKI_UPLOAD_DIR") or os.environ.get("UPLOAD_DIR") or "").strip()
    if upload:
        return Path(upload) / "season_ranges.json"
    bundled = Path(__file__).resolve().parents[4] / "data" / "season_ranges.json"
    if bundled.is_file():
        return bundled
    return None


def load_season_store(report_date: date | None = None, *, reload: bool = False) -> dict[str, Any]:
    """season_ranges.json + calendar_seasons."""
    global _STORE_CACHE, _STORE_CACHE_DATE, _STORE_CACHE_SIG
    rd = report_date or date.today()
    sig = _store_file_signature()
    if (
        not reload
        and _STORE_CACHE is not None
        and _STORE_CACHE_DATE == rd
        and _STORE_CACHE_SIG == sig
    ):
        return _STORE_CACHE

    try:
        from season_calendar import load_season_ranges_store

        store = load_season_ranges_store(rd)
    except ImportError:
        path = _season_ranges_path()
        raw: dict[str, Any] = {}
        if path and path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded
            except (OSError, json.JSONDecodeError):
                pass
        by_group: dict[str, Any] = {}
        bg = raw.get("by_group")
        if isinstance(bg, dict):
            by_group = {str(k): v for k, v in bg.items() if isinstance(v, dict)}
        store = {"calendar_seasons": raw.get("calendar_seasons") or {}, "by_group": by_group}

    _STORE_CACHE = store
    _STORE_CACHE_DATE = rd
    _STORE_CACHE_SIG = sig
    return store


def combine_season_start_for_date(report_date: date) -> date:
    """Начало уборки. Выбранные годы сезона эту дату не сдвигают.

    В процессе API дата берётся из настроек. В скрипте сводки /app нет в
    PYTHONPATH, поэтому дальше читаем то, что генерация уже положила в env:
    SVODKI_COMBINE_SEASON_START_DATE, SVODKI_COMBINE_SEASON_START,
    затем SVODKI_GRP_5_0_Уборка_PERIOD_START.
    Общий SVODKI_SEASON_START_DATE не читаем: его пишет календарь (01.07).
    """
    try:
        from app.legacy.manual_overrides import (
            combine_season_start_for_date as _from_settings,
        )

        return _from_settings(report_date)
    except Exception:
        pass
    for key in (
        "SVODKI_COMBINE_SEASON_START_DATE",
        "SVODKI_COMBINE_SEASON_START",
        group_env_key("5.0 Уборка", "PERIOD_START"),
    ):
        parsed = _parse_iso_date((os.environ.get(key) or "").strip())
        if parsed:
            return parsed
    start = date(report_date.year, 6, 10)
    if report_date < start:
        return date(report_date.year - 1, 6, 10)
    return start


def default_period_start_for_group(group_name: str, report_date: date) -> date:
    name = str(group_name or "").strip()
    if name.startswith("5.0 Уборка"):
        return combine_season_start_for_date(report_date)
    if name.startswith("2.1 "):
        return date(report_date.year, 1, 1)
    if name.startswith("1.1 ") or name.startswith("3.1 ") or name.startswith("3.2 "):
        return date(report_date.year, 3, 1)
    if "Уборка картофеля" in name or (name.startswith("5.") and "овощ" in name.lower()):
        return date(report_date.year, 3, 1)
    raw = (os.environ.get("SVODKI_SEASON_START_DATE") or os.environ.get("SEASON_START_DATE") or "").strip()
    if raw:
        parsed = _parse_iso_date(raw)
        if parsed:
            return parsed
    return date(report_date.year, 3, 14)


def _env_override_for_group(group_name: str, report_date: date) -> dict[str, Any]:
    """Переопределения из env subprocess (inject_season_range_env)."""
    name = str(group_name or "").strip()
    ov: dict[str, Any] = {}
    _apply_per_group_env(name, ov)

    if name.startswith("1.1 "):
        ps = os.environ.get("SVODKI_FERT_PERIOD_START", "").strip()
        pe = os.environ.get("SVODKI_FERT_PERIOD_END", "").strip()
        if ps and "period_start" not in ov:
            ov["period_start"] = ps.split("T")[0]
        if pe and "period_end" not in ov:
            ov["period_end"] = pe.split("T")[0]
        if "history_years" not in ov:
            fert_years = _years_from_env_var("SVODKI_FERT_SEASON_YEARS")
            if fert_years:
                ov["history_years"] = fert_years

    if name.startswith("3.1 ") or name.startswith("3.2 ") or "Опрыск" in name:
        if "history_years" not in ov:
            spray_hist = _years_from_env_var("SVODKI_SPRAY_SEASON_YEARS")
            if spray_hist:
                ov["history_years"] = spray_hist
        if "task_season_years" not in ov:
            spray_task = _years_from_env_var("SVODKI_SPRAY_TASK_SEASON_YEARS")
            if spray_task:
                ov["task_season_years"] = spray_task
        ps = os.environ.get("SVODKI_SPRAY_PERIOD_START", "").strip()
        pe = os.environ.get("SVODKI_SPRAY_PERIOD_END", "").strip()
        if ps and "period_start" not in ov:
            ov["period_start"] = ps.split("T")[0]
        if pe and "period_end" not in ov:
            ov["period_end"] = pe.split("T")[0]

    if name.startswith("4.0 Посев") or name.startswith("4.1 Посев"):
        if "history_years" not in ov:
            seed_years = _years_from_env_var("SVODKI_SEEDING_SEASON_YEARS")
            if seed_years:
                ov["history_years"] = seed_years

    if name.startswith("2.4 Вспашка"):
        ps = (os.environ.get("SVODKI_SEASON_START_DATE") or os.environ.get("SEASON_START_DATE") or "").strip()
        if ps:
            ov["period_start"] = ps.split("T")[0]

    rule = season_rule_for_group(name)
    task_str = (os.environ.get("SEASON_TASK_YEAR_STR") or "").strip()
    if applies_task_season_filter(name) and "task_season_years" not in ov:
        if rule == "both_years":
            # МУ/диск: 2026+2027 — не подменять одиночным SEASON_TASK_YEAR_STR из batch env.
            hist = ov.get("history_years") or []
            if len(hist) >= 2:
                ov["task_season_years"] = list(hist)
        elif rule in ("year_plus_one", "plow_year_plus_one"):
            # SEASON_TASK_YEAR_STR — явный сезон группы. Глобальный SEASON_YEAR
            # на сайте — календарный год уборки; подставлять его сюда нельзя:
            # вспашка, опрыскивание и посев (year+1) отбрасываются и факт = 0.
            if task_str:
                ov["task_season_years"] = _normalize_years([task_str])

    global_years = _normalize_years(
        [p.strip() for p in os.environ.get("SVODKI_SEASON_YEARS", "").split(",") if p.strip()]
    )
    if global_years and "history_years" not in ov:
        ov["history_years"] = global_years

    _ = report_date
    return ov


def _calendar_for_group(calendar: dict[str, Any], group_meta: dict[str, Any]) -> dict[str, Any]:
    """Общий календарь сезонов, поверх него — даты конкретной агрооперации."""
    cal = dict(calendar or {})
    raw = group_meta.get("year_ranges") if isinstance(group_meta, dict) else None
    if not isinstance(raw, dict):
        return cal
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        ps = _parse_iso_date(entry.get("period_start"))
        pe = _parse_iso_date(entry.get("period_end"))
        if ps and pe and ps <= pe:
            cal[str(key)] = {"period_start": ps.isoformat(), "period_end": pe.isoformat()}
    return cal


def _has_own_year_ranges(group_meta: dict[str, Any]) -> bool:
    raw = group_meta.get("year_ranges") if isinstance(group_meta, dict) else None
    if not isinstance(raw, dict):
        return False
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        ps = _parse_iso_date(entry.get("period_start"))
        pe = _parse_iso_date(entry.get("period_end"))
        if ps and pe and ps <= pe:
            return True
    return False


def _editor_group_override(run_override: dict[str, Any] | None, group_name: str) -> dict[str, Any]:
    """Срез run_override.by_group для одной операции (proverka / тематические сводки)."""
    raw = (run_override or {}).get("by_group") or {}
    entry = raw.get(group_name) or {}
    return dict(entry) if isinstance(entry, dict) else {}


def resolve_group_season_config(
    group_name: str,
    report_date: date,
    *,
    store: dict[str, Any] | None = None,
    run_override: dict[str, Any] | None = None,
    reload: bool = False,
) -> dict[str, Any]:
    """
    Итоговые сезоны и диапазон для одной группы работ.
    Приоритет task/history: SVODKI_FORCE_GROUP_SEASONS (разовый запуск сводки 2)
    → by_group[name] в run_override → season_ranges.json →
    report-level run_override → env → defaults.

    Report-level task_season_years (часто один год по season_rule) не должен
    перебивать выбор «2026+2027» в редакторе для 3.1 Опрыскивание.
    """
    name = str(group_name or "").strip()
    rule = season_rule_for_group(name)
    st = store if store is not None else load_season_store(report_date, reload=reload)
    calendar = st.get("calendar_seasons") or {}
    by_group = st.get("by_group") or {}
    gm = by_group.get(name) or {}

    file_years = _years_from_meta(gm)
    has_file_override = bool(file_years)

    run_ov = dict(run_override or {})
    group_ov = _editor_group_override(run_override, name)
    ov = dict(run_ov)
    env_ov = _env_override_for_group(name, report_date)
    for key, val in env_ov.items():
        ov.setdefault(key, val)

    force_env = os.environ.get("SVODKI_FORCE_GROUP_SEASONS", "").strip().lower() in {
        "1", "true", "yes",
    }
    forced_history = _normalize_years(env_ov.get("history_years")) if force_env else []
    forced_task = _normalize_years(env_ov.get("task_season_years")) if force_env else []

    if forced_history:
        history_years = forced_history
    elif "history_years" in group_ov:
        history_years = _normalize_years(group_ov.get("history_years"))
    elif has_file_override:
        history_years = list(file_years)
    elif "history_years" in run_ov:
        history_years = _normalize_years(run_ov.get("history_years"))
    else:
        history_years = _normalize_years(ov.get("history_years"))
        if not history_years:
            history_years = sorted(set(default_history_years_for_group(name, report_date)))

    # Редактор (season_years) важнее одного года из batch/run_override.
    # applies_task_season_filter ложен для 2.8: сводка не режет machine_task.season,
    # но Cropwise всё равно должен взять оба сезона плана, как у опрыскивания.
    # Иначе run_override или сезон сессии (часто 2026) выкидывает 2027.
    editor_sets_task_years = has_file_override and (
        applies_task_season_filter(name) or rule == "plow_year_plus_one"
    )
    if forced_task:
        task_years = forced_task
    elif "task_season_years" in group_ov:
        task_years = _normalize_years(group_ov.get("task_season_years"))
    elif editor_sets_task_years:
        task_years = merge_task_years_from_file(
            rule, report_date, file_years, has_file_override=True,
        )
    elif "task_season_years" in run_ov:
        task_years = _normalize_years(run_ov.get("task_season_years"))
    else:
        task_years = _normalize_years(ov.get("task_season_years"))
        if not task_years and editor_sets_task_years:
            task_years = merge_task_years_from_file(
                rule, report_date, file_years, has_file_override=has_file_override,
            )

    default_start = default_period_start_for_group(name, report_date)
    is_harvest = name.startswith("5.0 Уборка")
    group_calendar = _calendar_for_group(calendar, gm)
    own_ranges = _has_own_year_ranges(gm)
    # Даты сезонов операции задают окно сами, в том числе для уборки.
    period_locked = is_harvest and not own_ranges
    if own_ranges:
        period_start = None
        period_end = None
    else:
        period_start = None if period_locked else group_ov.get("period_start", run_ov.get("period_start"))
        period_end = None if period_locked else group_ov.get("period_end", run_ov.get("period_end"))

    if (
        not period_locked
        and period_start is None
        and period_end is None
        and history_years
        and (has_file_override or own_ranges)
    ):
        try:
            from season_calendar import merged_period_for_years

            ps, pe = merged_period_for_years(history_years, report_date, group_calendar)
            period_start = ps.isoformat()
            period_end = pe.isoformat()
        except ImportError:
            period_start = period_end = None

    if not period_locked and not own_ranges and period_start is None:
        period_start = ov.get("period_start")
    if not period_locked and not own_ranges and period_end is None:
        period_end = ov.get("period_end")

    if not period_start or not period_end:
        try:
            from season_calendar import merged_period_for_years

            # none: окно дат до report_date (не календарь Cropwise — иначе обрезка на 01.07).
            if period_locked:
                ps, pe = combine_season_start_for_date(report_date), report_date
            elif rule == "none" and not has_file_override and not own_ranges:
                ps, pe = default_start, report_date
            elif history_years and (has_file_override or own_ranges or rule != "none"):
                ps, pe = merged_period_for_years(history_years, report_date, group_calendar)
            elif name.startswith("5.0 Уборка"):
                ps, pe = default_start, report_date
            else:
                ps, pe = default_start, report_date
        except ImportError:
            ps, pe = default_start, report_date
        period_start = period_start or ps.isoformat()
        period_end = period_end or pe.isoformat()

    ps_iso = str(period_start).split("T")[0]
    pe_iso = str(period_end).split("T")[0]

    return {
        "group_name": name,
        "season_rule": rule,
        "history_years": history_years,
        "task_season_years": task_years,
        "period_start": ps_iso,
        "period_end": pe_iso,
        "task_season_plow_only": plow_only_for_rule(rule) and name == "2.4 Вспашка",
        "has_file_override": has_file_override,
        "has_own_ranges": own_ranges,
        "crop_priority_year": history_years[-1] if history_years else season_year_for_date(report_date),
    }


def resolve_report_season_config(
    work_groups: tuple[str, ...] | list[str],
    season_rule: str,
    report_date: date,
    run_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Сезоны сводки: объединение resolve_group_season_config по её группам работ."""
    groups = [str(g).strip() for g in work_groups if str(g).strip()]
    store = load_season_store(report_date)
    ov = dict(run_override or {})

    group_years: list[int] = []
    for gn in groups:
        gm = (store.get("by_group") or {}).get(gn) or {}
        group_years.extend(_years_from_meta(gm))
    group_years = sorted(set(group_years))
    has_file_override = any(
        _years_from_meta((store.get("by_group") or {}).get(gn)) for gn in groups
    )

    # Тот же разбор, что у редактора планов и у генерации сводки.
    # Report-level task_season_years (часто один год year_plus_one) не подменяет
    # выбор «2026+2027» у группы: он уже учтён внутри resolve_group_season_config.
    per_group = {
        gn: resolve_group_season_config(gn, report_date, store=store, run_override=ov)
        for gn in groups
    }

    def _union_years(key: str) -> list[int]:
        found: list[int] = []
        for cfg in per_group.values():
            found.extend(_normalize_years(cfg.get(key)))
        return sorted(set(found))

    task_years = _union_years("task_season_years")
    if not task_years:
        task_years = _normalize_years(ov.get("task_season_years"))
    if not task_years:
        task_years = merge_task_years_from_file(
            season_rule, report_date, group_years, has_file_override=has_file_override,
        )

    history_years = _union_years("history_years")
    if not history_years:
        history_years = _normalize_years(ov.get("history_years"))
    if not history_years:
        history_years = sorted(set(group_years or default_history_years_for_report_rule(season_rule, report_date)))

    period_start = ov.get("period_start")
    period_end = ov.get("period_end")
    if not period_start or not period_end:
        if groups:
            cfg0 = resolve_group_season_config(groups[0], report_date, store=store, run_override=ov)
            period_start = period_start or cfg0["period_start"]
            period_end = period_end or cfg0["period_end"]
        else:
            period_start = period_start or report_date.replace(month=3, day=1).isoformat()
            period_end = period_end or report_date.isoformat()

    return {
        "work_groups": groups,
        "season_rule": season_rule,
        "task_season_years": task_years,
        "history_years": history_years,
        "crop_priority_year": history_years[-1] if history_years else season_year_for_date(report_date),
        "period_start": str(period_start).split("T")[0],
        "period_end": str(period_end).split("T")[0],
        "shift_start_hour": ov.get("shift_start_hour"),
        "shift_end_hour": ov.get("shift_end_hour"),
        "task_season_plow_only": bool(
            ov.get("task_season_plow_only", plow_only_for_rule(season_rule))
        ),
        "by_group": per_group,
    }


def fact_season_years_for_group(group_name: str, report_date: date) -> list[int]:
    """Годы machine_task.season для суток и сезонного факта.

    Это чипы «Сезоны» в редакторе планов: сохранённый выбор, иначе годы,
    которые редактор показывает для группы (history_years, для вспашки
    и опрыскивания обычно год отчёта и следующий).
    """
    cfg = resolve_group_season_config(group_name, report_date)
    task_years = _normalize_years(cfg.get("task_season_years"))
    if task_years:
        return task_years
    history = _normalize_years(cfg.get("history_years"))
    if history:
        return history
    defaults = default_history_years_for_group(group_name, report_date)
    return defaults or [season_year_for_date(report_date)]


def season_label_for_group(group_name: str, report_date: date) -> str:
    """Подпись «Сезон:» — те же годы, что чипы редактора и факт сводки.

    Один сохранённый год (например только 2027 у «4.1 Посев (овощи)»)
    не разворачивается обратно в пару правила year_plus_one.
    """
    years = fact_season_years_for_group(group_name, report_date)
    if len(years) == 1:
        return str(years[0])
    if len(years) > 1:
        return "+".join(str(y) for y in years)
    return str(season_year_for_date(report_date))


def task_season_str_for_group(group_name: str, report_date: date) -> str | None:
    if not applies_task_season_filter(group_name):
        return None
    cfg = resolve_group_season_config(group_name, report_date)
    ty = cfg.get("task_season_years") or []
    if len(ty) == 1:
        return str(ty[0])
    return None


def editor_check_years(
    group_name: str,
    report_date: date,
    *,
    store: dict[str, Any] | None = None,
) -> list[int]:
    """
    Сезоны Cropwise из редактора планов.

    Несколько лет — складывать. Один год — только он, без второго сезона.
    Пустой список: в файле плана этой операции нет, остаётся прежнее правило.
    """
    name = str(group_name or "").strip()
    st = store if store is not None else load_season_store(report_date)
    return _years_from_meta((st.get("by_group") or {}).get(name))


def task_season_years_set_for_group(group_name: str, report_date: date) -> set[str] | None:
    """Сезоны редактора планов для суточного и сезонного факта."""
    years = fact_season_years_for_group(group_name, report_date)
    if not years:
        return None
    return {str(y) for y in years}


def winter_spray_task_seasons(
    season_task_seasons: set[str] | None,
    report_date: date,
) -> set[str]:
    """Сезоны MachineTask, при которых озимые рапс/пшеница остаются в опрыскивании.

    Обычно это текущий сезон (посев озимых; с июля это год отчёта + 1, до июля —
    год отчёта). Если редактор задаёт несколько сезонов, берётся текущий сезон,
    когда он есть в наборе, иначе — максимальный год.
    """
    next_year = str(season_year_for_date(report_date))
    if not season_task_seasons:
        return {next_year}
    if next_year in season_task_seasons:
        return {next_year}
    numeric = sorted(int(y) for y in season_task_seasons if str(y).strip().isdigit())
    if numeric:
        return {str(numeric[-1])}
    return {next_year}


def task_fact_season_text(task: dict) -> str:
    """machine_task.season. Пустое значение — задание не входит в выбранные сезоны."""
    for key in ("season", "year", "season_year"):
        raw = task.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            return text
    return ""


def filter_tasks_by_group_seasons(
    tasks: list[dict],
    group_name: str,
    report_date: date,
) -> list[dict]:
    """Оставить задания сезонов группы из редактора планов.

    Один и тот же список режет и сутки, и сезонную площадь.
    """
    allowed = task_season_years_set_for_group(group_name, report_date)
    if not allowed:
        return list(tasks)
    return [task for task in tasks if task_fact_season_text(task) in allowed]


def editor_period_bounds(group_name: str, report_date: date) -> tuple[date, date] | None:
    """Свои даты сезонов операции из редактора. None — дат ещё не сохраняли."""
    cfg = resolve_group_season_config(str(group_name or "").strip(), report_date)
    if not cfg.get("has_own_ranges"):
        return None
    try:
        start = date.fromisoformat(str(cfg["period_start"])[:10])
        end = date.fromisoformat(str(cfg["period_end"])[:10])
    except ValueError:
        return None
    return start, end


def season_start_from_editor(
    group_names: str | list[str] | tuple[str, ...],
    report_date: date,
    fallback: date,
) -> date:
    """Начало окна сводки.

    Если свои даты сохранены у каждой группы сводки — берётся самый ранний старт.
    Иначе остаётся прежнее начало сводки, чтобы группа без своих дат не обрезалась.
    """
    if isinstance(group_names, str):
        names = [group_names]
    else:
        names = [str(name).strip() for name in group_names if str(name).strip()]
    bounds = [editor_period_bounds(name, report_date) for name in names]
    if names and all(item is not None for item in bounds):
        return min(item[0] for item in bounds if item is not None)
    return fallback


def period_range_for_group(group_name: str, report_date: date) -> tuple[date, date]:
    cfg = resolve_group_season_config(group_name, report_date)
    return (
        date.fromisoformat(cfg["period_start"]),
        date.fromisoformat(cfg["period_end"]),
    )


def season_env_for_group(group_name: str, cfg: dict[str, Any]) -> dict[str, str]:
    env: dict[str, str] = {}
    name = group_name.strip()
    years = cfg.get("history_years") or []
    task_years = cfg.get("task_season_years") or []
    if years:
        env["SVODKI_SEASON_YEARS"] = ",".join(str(y) for y in years)
        env["SEASON_YEAR"] = str(years[-1])
    if len(task_years) == 1:
        env["SEASON_TASK_YEAR_STR"] = str(task_years[0])
    if name.startswith("1.1 "):
        if cfg.get("period_start"):
            env["SVODKI_FERT_PERIOD_START"] = str(cfg["period_start"]).split("T")[0]
        if cfg.get("period_end"):
            env["SVODKI_FERT_PERIOD_END"] = str(cfg["period_end"]).split("T")[0]
        if years:
            env["SVODKI_FERT_SEASON_YEARS"] = ",".join(str(y) for y in years)
    if name.startswith("4.0 Посев") or name.startswith("4.1 Посев"):
        if years:
            env["SVODKI_SEEDING_SEASON_YEARS"] = ",".join(str(y) for y in years)
    if name.startswith("3.1 ") or name.startswith("3.2 ") or "Опрыск" in name:
        if years:
            env["SVODKI_SPRAY_SEASON_YEARS"] = ",".join(str(y) for y in years)
        if task_years:
            env["SVODKI_SPRAY_TASK_SEASON_YEARS"] = ",".join(str(y) for y in task_years)
        if cfg.get("period_start"):
            env["SVODKI_SPRAY_PERIOD_START"] = str(cfg["period_start"]).split("T")[0]
        if cfg.get("period_end"):
            env["SVODKI_SPRAY_PERIOD_END"] = str(cfg["period_end"]).split("T")[0]
    if name.startswith("5.0 Уборка") and cfg.get("period_start"):
        iso = str(cfg["period_start"]).split("T")[0]
        env["SVODKI_COMBINE_SEASON_START"] = iso
    if years:
        env[group_env_key(name, "HISTORY_YEARS")] = ",".join(str(y) for y in years)
    if task_years:
        env[group_env_key(name, "TASK_SEASON_YEARS")] = ",".join(str(y) for y in task_years)
    if cfg.get("period_start"):
        env[group_env_key(name, "PERIOD_START")] = str(cfg["period_start"]).split("T")[0]
    if cfg.get("period_end"):
        env[group_env_key(name, "PERIOD_END")] = str(cfg["period_end"]).split("T")[0]
    return env


def inject_season_range_env(env: dict[str, str], report_date: date | None = None) -> None:
    """Подставить сезоны всех групп в env генерации PDF / сводок."""
    rd = report_date or date.today()
    path = _season_ranges_path()
    if path and path.is_file():
        env["SVODKI_SEASON_RANGES_PATH"] = str(path.resolve())
        upload = path.parent
        env["SVODKI_UPLOAD_DIR"] = str(upload.resolve())

    store = load_season_store(rd, reload=True)
    all_groups = set(GROUP_SEASON_RULE) | set((store.get("by_group") or {}).keys())

    for gn in sorted(all_groups):
        cfg = resolve_group_season_config(gn, rd, store=store)
        for k, v in season_env_for_group(gn, cfg).items():
            if k in ("SEASON_YEAR", "SVODKI_SEASON_YEARS", "SVODKI_SEASON_START_DATE", "SEASON_START_DATE"):
                continue
            if k in _COLLIDING_SHARED_ENV_KEYS:
                continue
            env[k] = v

    # Общие SEASON_YEAR / SEASON_START_DATE не публикуем. Год вспашки (2027)
    # попадал в 1_main_dop и сдвигал окно доп. таблиц сводки 5 на 01.01.2027.
    # У каждой группы свой SVODKI_GRP_*. Сводки 10, 11 и 13 получают дату
    # уборки отдельно, после этого вызова.
    # SEASON_TASK_YEAR_STR тоже общий. Иначе один сохранённый год
    # «4.1 Посев (овощи)» = 2027 становился сезоном вспашки, опрыскивания
    # и «4.0 Посев», и их листы теряли 2026.
    for key in (
        "SEASON_YEAR",
        "SVODKI_SEASON_YEARS",
        "SVODKI_SEASON_START_DATE",
        "SEASON_START_DATE",
        "SEASON_TASK_YEAR_STR",
    ):
        env.pop(key, None)
