"""
API v3 по окну смены: справочная разбивка и кэш полей.

Колонка Cropwise в проверке собирается из work.json обоих сезонов,
а не из смеси UI и API.
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

# 73 — фрезерование, отдельная операция 2.6. В запрос культивации его не добавляем:
# иначе колонка «Обработка почвы» смешивает два вида работ.
REPORT_WT_EXTRAS: dict[str, list[int]] = {}


def work_type_ids_for_report(report_id: str) -> list[int]:
    spec = REPORTS[report_id]
    out: set[int] = set()
    for group_name in spec.work_groups:
        ids = work_group_wt_map().get(group_name)
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


def _lower_bound(day_start: datetime | None, cutoff: datetime) -> datetime | None:
    """Нижняя граница окна смены — только при расширенной выборке заданий.

    Сводки берут задание целиком по end_time смены и считают его часы как ``h < cutoff``
    без нижней границы; при PROVERKA_TASK_WINDOW_PAD_HOURS = 0 (по умолчанию) проверка
    считает так же, и цифры совпадают со сводками.

    При PAD > 0 в выборку попадают задания соседних смен (по end_time вне окна). Их часы
    до начала смены уже учтены в своей смене, и без нижней границы они считались бы второй
    раз. Поэтому при PAD > 0 считаем строго часы окна [day_start, cutoff) — как Cropwise UI.
    Вызовы с day_start >= cutoff (старый признак «границы нет») границы не получают.
    """
    if task_window_pad_hours() <= 0:
        return None
    if not isinstance(day_start, datetime) or day_start >= cutoff:
        return None
    return day_start


def _hourly_in_shift(
    item: dict, day_start: datetime | None, cutoff: datetime,
) -> float:
    lo = _lower_bound(day_start, cutoff)
    total = 0.0
    for entry in item.get("covered_area_hourly") or []:
        h = _parse_dt(entry[0])
        if h is None or h >= cutoff:
            continue
        if lo is not None and h < lo:
            continue
        total += float(entry[1] or 0)
    return total


def _area_in_shift(items: list[dict], day_start: datetime, cutoff: datetime) -> float:
    """Площадь в окне смены [day_start, cutoff)."""
    return sum(_hourly_in_shift(item, day_start, cutoff) for item in items)


def _area_by_field_in_shift(
    items: list[dict], day_start: datetime, cutoff: datetime,
) -> dict[int, float]:
    by_field: dict[int, float] = defaultdict(float)
    for item in items:
        fid = item.get("field_id")
        if fid is None:
            continue
        by_field[int(fid)] += _hourly_in_shift(item, day_start, cutoff)
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


def season_rule_for_report(report_id: str) -> str:
    return REPORTS[report_id].season_rule


# Публичные алиасы для pull-скриптов
build_field_to_firm = get_cached_field_to_firm
get_all = _get_all
fetch_tasks_in_window = _fetch_tasks_in_window
