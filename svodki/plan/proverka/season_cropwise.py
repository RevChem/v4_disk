"""
Cropwise UI + API v3 по сезонам 2026/2027.

Сезон сессии (seasonValue) — work.json verified (#task_id, без фантомов).
Другой сезон — API v3 machine_task.season + mtfmi covered_area_hourly.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import requests

from plans import API_TOKEN, BASE_URL, FOLDER_TO_FIRM, TZ_MSK
from proverka.cropwise_ui import aggregate_ui_task_rows, parse_work_report_body
from proverka.reports_config import REPORTS
from proverka.work_groups import work_group_wt_map

_WORK_GROUP_MAP: dict[str, list[int]] = work_group_wt_map()

REPORT_WT_EXTRAS: dict[str, list[int]] = {
    "8": [73],
}


def work_type_ids_for_report(report_id: str) -> list[int]:
    spec = REPORTS[report_id]
    out: set[int] = set()
    for group_name in spec.work_groups:
        ids = _WORK_GROUP_MAP.get(group_name)
        if ids:
            out.update(int(x) for x in ids)
    out.update(REPORT_WT_EXTRAS.get(report_id, []))
    if not out:
        raise ValueError(
            f"work_type_ids не заданы для отчёта {report_id} "
            f"(groups={spec.work_groups})"
        )
    return sorted(out)


def _parse_dt(s: Any) -> datetime | None:
    if not s:
        return None
    if isinstance(s, (int, float)):
        try:
            ts = float(s)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_MSK)
    return dt.astimezone(TZ_MSK)


def _get_all(endpoint: str, extra: dict | None = None) -> list[dict]:
    headers = {"X-User-Api-Token": API_TOKEN}
    out: list[dict] = []
    from_id = 0
    while True:
        params = {**(extra or {}), "from_id": from_id, "limit": 1000}
        r = requests.get(f"{BASE_URL}/{endpoint}", headers=headers, params=params, timeout=120)
        r.raise_for_status()
        body = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        out.extend(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        # API отдаёт страницу по 100, даже если просили limit=1000.
        # Раньше цикл останавливался на obtained_records < 1000 и карта
        # поле→агрофирма обрывалась на первой сотне полей.
        obtained = int(meta.get("obtained_records") or len(chunk) or 0)
        if not last_id or obtained < 100:
            break
        from_id = int(last_id) + 1
    return out


def task_window_pad_hours() -> int:
    """
    Запас по краям окна смены при выборке machine_tasks.

    Выборка идёт по end_time. Задание, начавшееся в 20:00 и закрытое в 07:30
    (уже после cutoff 06:00), в старом варианте не попадало в API вообще —
    а его часы внутри смены Cropwise UI считает. Такие строки UI превращались
    в «сирот» и вычитались из итога. Площадь всё равно режется по часам
    covered_area_hourly, поэтому расширение окна ничего лишнего не добавляет.
    """
    try:
        return max(0, int(os.environ.get("PROVERKA_TASK_WINDOW_PAD_HOURS", "0")))
    except ValueError:
        return 0


def _shift_iso(ts: str, hours: int) -> str:
    dt = _parse_dt(ts)
    if dt is None:
        return ts
    return (dt + timedelta(hours=hours)).isoformat()[:19].replace("+03:00", "")


def _fetch_tasks_in_window(
    wt_ids: list[int], t0: str, t1: str, *, pad_hours: int | None = None,
) -> list[dict]:
    pad = task_window_pad_hours() if pad_hours is None else max(0, int(pad_hours))
    lo = _shift_iso(t0, -pad) if pad else t0
    hi = _shift_iso(t1, pad) if pad else t1

    by_id: dict[int, dict] = {}
    wt_list = sorted({int(x) for x in wt_ids})
    wt_str = ",".join(str(x) for x in wt_list)
    base = {"end_time_gt_eq": lo, "end_time_lt": hi}

    batched = _get_all("machine_tasks", {**base, "work_type_id": wt_str})
    for t in batched:
        tid = t.get("id")
        if tid is not None:
            by_id[int(tid)] = t

    # Поштучный проход — только fallback для типов работ, по которым пачка
    # ничего не вернула. Раньше он выполнялся всегда: 1 + len(wt_ids) запросов
    # вместо одного, на ровном месте.
    covered = {int(t.get("work_type_id") or 0) for t in batched}
    missing = [wt for wt in wt_list if wt not in covered]
    for wt in missing:
        for t in _get_all("machine_tasks", {**base, "work_type_id": wt}):
            tid = t.get("id")
            if tid is not None:
                by_id[int(tid)] = t
    return list(by_id.values())


_field_to_firm_cache: dict[int, str] | None = None
# Старый кэш писался плоским словарём после одной страницы fields (100 записей).
_FIELD_TO_FIRM_CACHE_VERSION = 2


def _field_to_firm_cache_path() -> Path | None:
    """
    Файл кэша карты поле→агрофирма.

    Модульный глобал не переживает ProcessPool/subprocess, поэтому fields +
    field_groups + group_folders тянулись заново в каждом процессе на каждую
    сводку. Кладём карту на диск рядом с остальными артефактами прогона.
    """
    raw = (os.environ.get("PROVERKA_CACHE_DIR") or os.environ.get("RECONCILE_OUT_DIR") or "").strip()
    if not raw:
        return None
    try:
        d = Path(raw)
        d.mkdir(parents=True, exist_ok=True)
        return d / "field_to_firm_cache.json"
    except OSError:
        return None


def get_cached_field_to_firm(*, refresh: bool = False) -> dict[int, str]:
    """Один раз за proverka: fields + field_groups + group_folders."""
    global _field_to_firm_cache
    if not refresh and _field_to_firm_cache is not None:
        return _field_to_firm_cache

    path = _field_to_firm_cache_path()
    if not refresh and path is not None and path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            loaded = _field_to_firm_from_cache_payload(raw)
            if loaded:
                _field_to_firm_cache = loaded
                return _field_to_firm_cache
        except (OSError, ValueError):
            pass

    _field_to_firm_cache = _build_field_to_firm()
    if path is not None and _field_to_firm_cache:
        try:
            path.write_text(
                json.dumps(
                    {
                        "v": _FIELD_TO_FIRM_CACHE_VERSION,
                        "map": {str(k): v for k, v in _field_to_firm_cache.items()},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
    return _field_to_firm_cache


def _field_to_firm_from_cache_payload(raw: Any) -> dict[int, str] | None:
    """Плоский кэш без версии собран по первой странице API и его нельзя брать."""
    if not isinstance(raw, dict):
        return None
    if raw.get("v") != _FIELD_TO_FIRM_CACHE_VERSION or not isinstance(raw.get("map"), dict):
        return None
    out: dict[int, str] = {}
    for key, firm in raw["map"].items():
        try:
            out[int(key)] = str(firm)
        except (TypeError, ValueError):
            continue
    return out or None


def clear_field_to_firm_cache() -> None:
    global _field_to_firm_cache
    _field_to_firm_cache = None
    path = _field_to_firm_cache_path()
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _build_field_to_firm() -> dict[int, str]:
    fields = _get_all("fields")
    fgs = _get_all("field_groups")
    gfs = _get_all("group_folders")
    parent_map = {g["id"]: g.get("parent_id") for g in gfs}
    root_ids = {g["id"] for g in gfs if not g.get("parent_id")}
    fg_to_folder = {fg["id"]: fg.get("group_folder_id") for fg in fgs}
    field_to_firm: dict[int, str] = {}

    def resolve_root(rid: int | None) -> int | None:
        seen: set[int] = set()
        while rid and rid not in seen:
            seen.add(rid)
            if rid in root_ids:
                return rid
            rid = parent_map.get(rid)
        return None

    for f in fields:
        fid = f.get("id")
        gf = f.get("field_group_id")
        if fid is None or gf is None:
            continue
        root = resolve_root(fg_to_folder.get(gf))
        firm = FOLDER_TO_FIRM.get(root) if root else None
        if firm:
            field_to_firm[int(fid)] = firm
    return field_to_firm


def _area_in_shift(items: list[dict], day_start: datetime, cutoff: datetime) -> float:
    """Как в модулях сводок (3_main_real и др.): h < cutoff, без нижней границы day_start."""
    _ = day_start
    total = 0.0
    for item in items:
        for entry in item.get("covered_area_hourly") or []:
            h = _parse_dt(entry[0])
            if h and h < cutoff:
                total += float(entry[1] or 0)
    return total


def _area_by_field_in_shift(
    items: list[dict], day_start: datetime, cutoff: datetime,
) -> dict[int, float]:
    _ = day_start
    by_field: dict[int, float] = defaultdict(float)
    for item in items:
        fid = item.get("field_id")
        if fid is None:
            continue
        for entry in item.get("covered_area_hourly") or []:
            h = _parse_dt(entry[0])
            if h and h < cutoff:
                by_field[int(fid)] += float(entry[1] or 0)
    return dict(by_field)


def _fetch_mtfmi_batched(task_ids: list[int], *, log_prefix: str = "") -> dict[int, list[dict]]:
    """mtfmi батчами по machine_task_id (как сводка 2 / mtfmi_batch_fetch)."""
    from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks

    unique = sorted({int(t) for t in task_ids if t is not None})
    if not unique:
        return {}

    async def _run() -> dict[int, list[dict]]:
        sem = asyncio.Semaphore(20)
        async with httpx.AsyncClient(
            headers={"X-User-Api-Token": API_TOKEN},
            timeout=120,
        ) as client:
            return await fetch_mtfmi_map_for_tasks(
                client, sem, BASE_URL, unique, log_prefix=log_prefix,
            )

    return asyncio.run(_run())


UNMAPPED_FIRM_KEY = "(поле без агрофирмы)"


def _aggregate_api_by_season(
    tasks: list[dict],
    mtfmi_map: dict[int, list[dict]],
    field_to_firm: dict[int, str],
    day_start: datetime,
    cutoff: datetime,
) -> tuple[dict[str, dict], set[int]]:
    by_season_firm: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    unmapped_by_season: dict[str, float] = defaultdict(float)
    task_counts: dict[str, int] = defaultdict(int)
    valid_task_ids: set[int] = set()

    for t in tasks:
        tid = t.get("id")
        if tid is None:
            continue
        tid_i = int(tid)
        valid_task_ids.add(tid_i)
        season = str(t.get("season") or "none")
        items = mtfmi_map.get(tid_i, [])
        area = _area_in_shift(items, day_start, cutoff)
        if area <= 0:
            continue
        task_counts[season] += 1
        by_field = _area_by_field_in_shift(items, day_start, cutoff)
        for fid, ha in by_field.items():
            firm = field_to_firm.get(fid)
            if firm:
                by_season_firm[season][firm] += ha
            else:
                # Поле без маппинга folder→firm. Раньше такие гектары просто
                # выбрасывались и итог API оказывался ниже без объяснения.
                unmapped_by_season[season] += ha

    api_by_season: dict[str, dict] = {}
    for season in sorted(set(by_season_firm) | set(unmapped_by_season)):
        firms = {
            k: round(v, 2)
            for k, v in sorted(by_season_firm[season].items(), key=lambda x: -x[1])
            if v >= 0.01
        }
        unmapped = round(unmapped_by_season.get(season, 0.0), 2)
        api_by_season[season] = {
            "by_firm": firms,
            "unmapped_ha": unmapped,
            "total_ha": round(sum(firms.values()) + unmapped, 2),
            "tasks_count": task_counts.get(season, 0),
        }
    return api_by_season, valid_task_ids


@dataclass
class ShiftWindowCache:
    """Задачи + mtfmi за смену (один раз на batch proverka / сводку 2)."""

    t0: str
    t1: str
    field_to_firm: dict[int, str]
    all_tasks: list[dict]
    mtfmi_map: dict[int, list[dict]]

    @classmethod
    def build(
        cls,
        t0: str,
        t1: str,
        wt_ids: list[int],
        *,
        log_prefix: str = "",
    ) -> ShiftWindowCache:
        f2f = get_cached_field_to_firm()
        tasks = _fetch_tasks_in_window(wt_ids, t0, t1)
        task_ids = [int(t["id"]) for t in tasks if t.get("id") is not None]
        mtfmi = _fetch_mtfmi_batched(task_ids, log_prefix=log_prefix)
        return cls(t0, t1, f2f, tasks, mtfmi)

    def api_for_wt_ids(self, wt_ids: list[int]) -> tuple[dict[str, dict], set[int]]:
        day_start = _parse_dt(self.t0)
        cutoff = _parse_dt(self.t1)
        if day_start is None or cutoff is None:
            return {}, set()
        wt_set = {int(x) for x in wt_ids}
        tasks = [
            t for t in self.all_tasks
            if int(t.get("work_type_id") or 0) in wt_set
        ]
        return _aggregate_api_by_season(
            tasks, self.mtfmi_map, self.field_to_firm, day_start, cutoff,
        )


def compute_api_by_season(
    wt_ids: list[int],
    t0: str,
    t1: str,
    *,
    field_to_firm: dict[int, str] | None = None,
    shift_cache: ShiftWindowCache | None = None,
) -> tuple[dict[str, dict], set[int]]:
    """API v3: machine_tasks.season + mtfmi в окне смены."""
    if shift_cache is not None:
        return shift_cache.api_for_wt_ids(wt_ids)

    day_start = _parse_dt(t0)
    cutoff = _parse_dt(t1)
    if day_start is None or cutoff is None:
        return {}, set()

    f2f = field_to_firm if field_to_firm is not None else get_cached_field_to_firm()
    tasks = _fetch_tasks_in_window(wt_ids, t0, t1)
    task_ids = [int(t["id"]) for t in tasks if t.get("id") is not None]
    mtfmi_map = _fetch_mtfmi_batched(task_ids)
    return _aggregate_api_by_season(tasks, mtfmi_map, f2f, day_start, cutoff)


def preferred_ui_session(
    season_rule: str,
    task_season_years: list[int] | None,
) -> int | None:
    """Сезон Cropwise UI, нужный для корректного work.json (обычно 2027 осенью)."""
    years = [int(y) for y in (task_season_years or [])]
    if not years:
        return None
    if season_rule == "both_years":
        return max(years)
    if len(years) == 1:
        return years[0]
    return max(years)


def ui_session_ok_for_years(
    ui_session: int | None,
    task_season_years: list[int] | None,
) -> bool:
    """
    Годится ли сезон сессии Cropwise для сводки.

    work.json отдаёт только сезон, выбранный в сессии Cropwise: ни
    `?seasonValue=`, ни параметры тела запроса его не меняют. Поэтому сессия
    полезна, если её сезон входит в выбранные сводкой, — остальные сезоны
    берутся из API v3.
    """
    years = [int(y) for y in (task_season_years or [])]
    if ui_session is None or not years:
        return False
    return int(ui_session) in years


def ui_session_blocks_both_years(
    ui_session: int | None,
    season_rule: str,
    task_season_years: list[int] | None,
) -> tuple[bool, int | None]:
    """
    True, если сезон сессии Cropwise не входит в выбранные сводкой сезоны.

    Раньше сессия 2026 при выборе 2026+2027 считалась заблокированной, потому
    что «нужным» признавался только max(годов). Второй сезон приходит из API v3,
    так что достаточно попадания сессии в любой из выбранных годов.
    """
    need = preferred_ui_session(season_rule, task_season_years)
    if need is None or ui_session is None:
        return False, need
    if season_rule not in ("both_years", "year_plus_one", "plow_year_plus_one"):
        return False, need
    if not (task_season_years or []):
        return False, need
    return not ui_session_ok_for_years(ui_session, task_season_years), need


def build_seasons_for_years(
    years: list[int],
    ui_session: int | None,
    ui_total: float,
    ui_by_firm: dict[str, float],
    api_by_season: dict[str, dict],
) -> dict[str, dict]:
    """
    Блок по сезонам: сезон сессии — из work.json, остальные — из API v3.

    Заменяет пропорциональное разложение одного итога UI на сезоны: «Всего»
    из work.json относится ровно к сезону сессии, а не к сумме сезонов.
    """
    out: dict[str, dict] = {}
    wanted = {str(y) for y in years} or set(api_by_season)
    for year in sorted(wanted | set(api_by_season)):
        api = api_by_season.get(year) or {}
        if ui_session is not None and year == str(int(ui_session)):
            out[year] = {
                "source": "work_json_session_season",
                "total_ha": round(float(ui_total), 2),
                "by_firm": {
                    k: round(float(v), 2)
                    for k, v in (ui_by_firm or {}).items()
                    if float(v) >= 0.01
                },
                "tasks_count": api.get("tasks_count", 0),
            }
        else:
            out[year] = {
                "source": "api_v3_machine_task_season",
                "total_ha": round(float(api.get("total_ha") or 0), 2),
                "by_firm": dict(api.get("by_firm") or {}),
                "tasks_count": api.get("tasks_count", 0),
            }
    return out


def infer_ui_session_season(
    ui_verified: dict[str, Any],
    api_by_season: dict[str, dict],
    task_season_years: list[int] | None = None,
) -> int | None:
    """
    seasonValue из HTML иногда недоступен (GET work.json, batch после API v3).
    UI verified total сопоставляем с API по сезонам.
    """
    ui_total = float(ui_verified.get("fact_total_ha") or 0)
    if ui_total <= 0:
        if task_season_years and len(task_season_years) == 1:
            return int(task_season_years[0])
        return None

    api_totals = {
        int(y): float((api_by_season.get(y) or {}).get("total_ha") or 0)
        for y in ("2026", "2027")
        if float((api_by_season.get(y) or {}).get("total_ha") or 0) > 0
    }
    # Раньше здесь стояли пороги «>=800 га → 2027, <=300 га → 2026». В межсезонье
    # объёмы падают ниже 300 га, сезон определялся неверно и уводил всю ветку
    # логики. Опираемся на близость к фактическим объёмам API по сезонам.
    if api_totals:
        best = min(api_totals.keys(), key=lambda y: abs(ui_total - api_totals[y]))
        others = [y for y in api_totals if y != best]
        # Решение принимаем, только если оно однозначное (ближайший сезон
        # заметно ближе остальных), иначе честно возвращаем None.
        if not others:
            return best
        margin = min(abs(ui_total - api_totals[y]) for y in others)
        if margin > abs(ui_total - api_totals[best]) * 1.25:
            return best
    if task_season_years:
        return max(int(y) for y in task_season_years)
    return None


def build_seasons_block_api_only(api_by_season: dict[str, dict]) -> dict[str, dict]:
    """API v3 по machine_task.season — надёжно при неверном seasonValue в UI."""
    out: dict[str, dict] = {}
    for year in ("2026", "2027"):
        api = api_by_season.get(year) or {}
        firms = {
            k: round(float(v), 2)
            for k, v in (api.get("by_firm") or {}).items()
            if float(v or 0) >= 0.01
        }
        out[year] = {
            "source": "api_v3_machine_task_season",
            "total_ha": round(float(api.get("total_ha") or 0), 2),
            "by_firm": firms,
            "tasks_count": api.get("tasks_count", 0),
        }
    return out


def build_seasons_block(
    ui_season: int | None,
    ui_verified: dict[str, Any],
    api_by_season: dict[str, dict],
) -> dict[str, dict]:
    ui_by_firm = ui_verified.get("fact_by_firm") or {}
    ui_total = round(float(ui_verified.get("fact_total_ha") or 0), 2)
    out: dict[str, dict] = {}
    for year in ("2026", "2027"):
        api = api_by_season.get(year) or {}
        api_firms = api.get("by_firm") or {}
        api_total = float(api.get("total_ha") or 0)
        if ui_season and int(year) == ui_season:
            out[year] = {
                "source": "work_json_verified",
                "total_ha": ui_total,
                "by_firm": {k: round(v, 2) for k, v in ui_by_firm.items() if v >= 0.01},
                "tasks_count": api.get("tasks_count", 0),
            }
        else:
            out[year] = {
                "source": "api_v3_machine_task_season",
                "total_ha": round(api_total, 2),
                "by_firm": api_firms,
                "tasks_count": api.get("tasks_count", 0),
            }
    return out


def merge_seasons_for_task_years(
    seasons: dict[str, dict],
    task_years: list[int] | None,
) -> tuple[float, dict[str, float]]:
    """Сумма сезонов, соответствующих фильтру сводки."""
    years = [str(y) for y in (task_years or []) if str(y) in seasons]
    if not years:
        years = sorted(seasons.keys())
    by_firm: dict[str, float] = defaultdict(float)
    total = 0.0
    for year in years:
        block = seasons.get(year) or {}
        total += float(block.get("total_ha") or 0)
        for firm, ha in (block.get("by_firm") or {}).items():
            by_firm[firm] += float(ha)
    return (
        round(total, 2),
        {k: round(v, 2) for k, v in sorted(by_firm.items(), key=lambda x: -x[1]) if v >= 0.01},
    )


def _api_total_all_seasons(api_by_season: dict[str, dict]) -> tuple[float, dict[str, float]]:
    """Сумма API v3 по всем season для wt без season-фильтра (овощи и т.п.)."""
    by_firm: dict[str, float] = defaultdict(float)
    total = 0.0
    for block in api_by_season.values():
        total += float(block.get("total_ha") or 0)
        for firm, ha in (block.get("by_firm") or {}).items():
            by_firm[firm] += float(ha or 0)
    return (
        round(total, 2),
        {k: round(v, 2) for k, v in sorted(by_firm.items(), key=lambda x: -x[1]) if v >= 0.01},
    )


def _ui_verified_from_work(
    work_body: dict[str, Any],
    firm_names: list[str],
    fact_hints: list[str],
    valid_task_ids: set[int],
) -> tuple[dict[str, Any], dict[str, Any], float]:
    """verified #task_id из work.json, только задания из API (work_type + смена)."""
    parsed = parse_work_report_body(
        work_body, firm_names, fact_hints, valid_task_ids=valid_task_ids,
    )
    raw_total = float(parsed.get("fact_total_ha") or 0)
    detail_rows = parsed.get("detail_rows") or []
    ui_verified = aggregate_ui_task_rows(detail_rows, valid_task_ids)
    verified_total = float(ui_verified.get("fact_total_ha") or 0)
    if verified_total <= 0 and detail_rows:
        ui_verified = aggregate_ui_task_rows(detail_rows, None)
        verified_total = float(ui_verified.get("fact_total_ha") or 0)
    if verified_total <= 0 and raw_total > 0:
        ui_verified = {
            "fact_total_ha": raw_total,
            "fact_by_firm": parsed.get("fact_by_firm") or {},
            "task_rows": [],
        }
        verified_total = raw_total
    return parsed, ui_verified, verified_total


def compute_cropwise_verified_by_wt(
    work_body: dict[str, Any],
    firm_names: list[str],
    fact_hints: list[str],
    wt_ids: list[int],
    t0: str,
    t1: str,
    ui_session_season: int | None = None,
    *,
    field_to_firm: dict[int, str] | None = None,
    shift_cache: ShiftWindowCache | None = None,
) -> dict[str, Any]:
    """
    Сводки без season-фильтра (4, 8, 11…): work.json + API только для valid_task_ids
    (machine_task с нужным work_type_id в окне смены), без разбивки по season.
    """
    api_by_season, valid_task_ids = compute_api_by_season(
        wt_ids, t0, t1, field_to_firm=field_to_firm, shift_cache=shift_cache,
    )
    parsed, ui_verified, verified_total = _ui_verified_from_work(
        work_body, firm_names, fact_hints, valid_task_ids,
    )
    by_firm = ui_verified.get("fact_by_firm") or {}
    use_api_fallback = False
    report_blank = isinstance(work_body, dict) and bool(work_body.get("reportBlank"))
    if (verified_total <= 0 or report_blank) and api_by_season:
        api_total, api_by_firm = _api_total_all_seasons(api_by_season)
        if api_total > 0:
            use_api_fallback = True
            verified_total = api_total
            by_firm = api_by_firm
    raw_total = float(parsed.get("fact_total_ha") or 0)
    parsed["fact_total_ha_raw"] = raw_total
    parsed["fact_total_ha_verified"] = verified_total
    parsed["fact_by_firm_verified"] = by_firm
    # Сравниваем с «Всего» в Cropwise UI (regionRows), а не с verified #task_id.
    compare_total = raw_total if raw_total > 0 else verified_total
    compare_by_firm = parsed.get("fact_by_firm") or by_firm
    parsed["fact_total_ha"] = round(compare_total, 2)
    parsed["fact_by_firm"] = compare_by_firm
    parsed["ui_orphan_tasks"] = parsed.get("ui_orphan_tasks") or ui_verified.get("orphan_task_ids")
    api_total, api_by_firm_all = _api_total_all_seasons(api_by_season)
    parsed["fact_total_ha_api"] = api_total
    return {
        "parsed": parsed,
        "seasons": build_seasons_block_api_only(api_by_season) if api_by_season else {},
        "ui_session_seasonValue": ui_session_season,
        "required_ui_sessionValue": None,
        "ui_session_blocked": False,
        "cropwise_api_fallback": use_api_fallback,
        "cropwise_total_ha": round(compare_total, 2),
        "cropwise_by_firm": compare_by_firm,
        "cropwise_ui_ha": round(raw_total, 2),
        "cropwise_api_ha": api_total,
        "cropwise_api_by_firm": api_by_firm_all,
        "cropwise_ui_vs_api_ha": round(raw_total - api_total, 2),
        "ui_balance": parsed.get("ui_balance"),
        "valid_task_ids": valid_task_ids,
        "api_by_season": api_by_season,
    }


def split_total_by_api(
    total: float,
    by_firm: dict[str, float],
    api_by_season: dict[str, dict],
    years: list[int] | None,
) -> dict[str, dict]:
    """
    Разложить готовый итог UI по сезонам пропорционально API.

    Используется только когда сводка не фильтрует сезоны: тогда итог UI
    целиком относится к сверке, а разбивка по сезонам нужна лишь для
    наглядности. Если сезоны заданы, итог собирает build_seasons_for_years:
    work.json отдаёт только сезон сессии Cropwise, поэтому пропорциональное
    разложение его «Всего» занизило бы остальные сезоны.
    """
    ys = [str(y) for y in (years or [])] or sorted(api_by_season.keys())
    weights = {y: float((api_by_season.get(y) or {}).get("total_ha") or 0) for y in ys}
    denom = sum(weights.values())
    out: dict[str, dict] = {}
    for year in ("2026", "2027"):
        if year not in ys:
            api = api_by_season.get(year) or {}
            out[year] = {
                "source": "api_v3_machine_task_season",
                "total_ha": round(float(api.get("total_ha") or 0), 2),
                "by_firm": dict(api.get("by_firm") or {}),
                "tasks_count": api.get("tasks_count", 0),
            }
            continue
        share = (weights.get(year, 0.0) / denom) if denom > 0 else (1.0 / max(len(ys), 1))
        out[year] = {
            "source": "work_json_total_split_by_api",
            "total_ha": round(total * share, 2),
            "by_firm": {k: round(float(v) * share, 2) for k, v in by_firm.items()},
            "tasks_count": (api_by_season.get(year) or {}).get("tasks_count", 0),
            "share_of_ui_total": round(share, 4),
        }
    return out


def compute_season_aware_cropwise(
    work_body: dict[str, Any],
    firm_names: list[str],
    fact_hints: list[str],
    wt_ids: list[int],
    t0: str,
    t1: str,
    ui_session_season: int | None,
    *,
    task_season_years: list[int] | None = None,
    season_rule: str = "",
    field_to_firm: dict[int, str] | None = None,
    shift_cache: ShiftWindowCache | None = None,
) -> dict[str, Any]:
    """
    Расчёт Cropwise для сверки.

    Главное изменение: `cropwise_total_ha` — это НАСТОЯЩЕЕ «Всего» из
    work.json, то есть ровно то число, которое видно в браузере. Раньше сюда
    попадал синтетический merged_total (UI-строки одного сезона + API другого),
    из-за чего отчёт показывал, например, 1545 га там, где в Cropwise 1766.
    Реконструкция по API осталась, но как отдельная величина `cropwise_api_ha`.
    """
    if season_rule == "none" and not task_season_years:
        return compute_cropwise_verified_by_wt(
            work_body, firm_names, fact_hints, wt_ids, t0, t1, ui_session_season,
            field_to_firm=field_to_firm, shift_cache=shift_cache,
        )

    api_by_season, valid_task_ids = compute_api_by_season(
        wt_ids, t0, t1, field_to_firm=field_to_firm, shift_cache=shift_cache,
    )
    parsed, ui_verified, verified_total = _ui_verified_from_work(
        work_body, firm_names, fact_hints, valid_task_ids,
    )
    raw_total = float(parsed.get("fact_total_ha") or 0)
    raw_by_firm = dict(parsed.get("fact_by_firm") or {})

    if ui_session_season is None:
        ui_session_season = infer_ui_session_season(
            ui_verified, api_by_season, task_season_years,
        )

    blocked, required_session = ui_session_blocks_both_years(
        ui_session_season, season_rule, task_season_years,
    )
    api_seasons = build_seasons_block_api_only(api_by_season)
    api_merged, api_merged_by_firm = merge_seasons_for_task_years(
        api_seasons, task_season_years,
    )

    report_blank = isinstance(work_body, dict) and bool(work_body.get("reportBlank"))
    ui_usable = raw_total > 0 and not report_blank
    years = [int(y) for y in (task_season_years or [])]

    if ui_usable and ui_session_ok_for_years(ui_session_season, years):
        # UI показывает только сезон своей сессии, поэтому итог сводки собираем
        # как «work.json за сезон сессии + API v3 за остальные выбранные сезоны».
        # Раньше сюда попадало одно «Всего» из UI, и при выборе 2026+2027
        # второй сезон терялся целиком.
        seasons = build_seasons_for_years(
            years, ui_session_season, raw_total, raw_by_firm, api_by_season,
        )
        compare_total, compare_by_firm = merge_seasons_for_task_years(seasons, years)
        use_api_only = False
    elif ui_usable and not years:
        seasons = split_total_by_api(
            raw_total, raw_by_firm, api_by_season, task_season_years,
        )
        compare_total = round(raw_total, 2)
        compare_by_firm = {k: round(float(v), 2) for k, v in raw_by_firm.items() if float(v) >= 0.01}
        use_api_only = False
    else:
        seasons = api_seasons
        compare_total = api_merged
        compare_by_firm = api_merged_by_firm
        use_api_only = True

    parsed["fact_total_ha_raw"] = round(raw_total, 2)
    parsed["fact_total_ha_verified"] = round(float(ui_verified.get("fact_total_ha") or 0), 2)
    parsed["fact_by_firm_verified"] = ui_verified.get("fact_by_firm") or {}
    parsed["fact_total_ha_api"] = api_merged
    parsed["fact_total_ha"] = compare_total
    parsed["fact_by_firm"] = compare_by_firm
    parsed["ui_orphan_tasks"] = parsed.get("ui_orphan_tasks") or ui_verified.get("orphan_task_ids")

    return {
        "parsed": parsed,
        "seasons": seasons,
        "ui_session_seasonValue": ui_session_season,
        "required_ui_sessionValue": required_session,
        "ui_session_blocked": blocked,
        "cropwise_api_fallback": use_api_only,
        "cropwise_total_ha": compare_total,
        "cropwise_by_firm": compare_by_firm,
        "cropwise_ui_ha": round(raw_total, 2),
        "cropwise_api_ha": api_merged,
        "cropwise_api_by_firm": api_merged_by_firm,
        "cropwise_ui_vs_api_ha": round(raw_total - api_merged, 2),
        # «Всего» из UI относится к одному сезону, поэтому сравнивать его с API
        # нужно за тот же сезон сессии, иначе разница сезонов выглядит ошибкой.
        "cropwise_api_session_season_ha": (
            round(
                float(
                    (api_by_season.get(str(int(ui_session_season))) or {}).get("total_ha") or 0
                ),
                2,
            )
            if ui_session_season is not None
            else None
        ),
        "ui_balance": parsed.get("ui_balance"),
        "valid_task_ids": valid_task_ids,
        "api_by_season": api_by_season,
    }


def season_rule_for_report(report_id: str) -> str:
    return REPORTS[report_id].season_rule


# Публичные алиасы для pull-скриптов
build_field_to_firm = get_cached_field_to_firm
get_all = _get_all
fetch_tasks_in_window = _fetch_tasks_in_window
