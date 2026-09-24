# -*- coding: utf-8 -*-
"""
Cropwise Operations API — сводный отчёт по уборке культур по холдингу
======================================================================
- day_start / cutoff: смена 06:00–06:00 (как сводка 11)
- Агрофирма: по полю (field → field_group → group_folder), не по machine_region
- Площадь: mtfmi (covered_area / hourly), как сводка 11; agro_operations — только вес и --fast
- Prefetch JSON: SVODKI_PREFETCH_DATA_PATH
- Подпись «Сводка за …» = календарный день смены (day_start.date())
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import sys
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import pandas as pd
import requests

from excel_number_fmt import summary_cell_value_and_format, NF_PCT, _coerce_float
from task_area_shared import (
    CORN_GRAIN_CROP,
    CORN_SILAGE_CROP,
    CUTOFF_BUFFER_HOURS,
    HARVESTER_GROUP_IDS,
    SILAGE_CORN_MOWING_WT_IDS,
    collect_silage_corn_field_ids,
    crop_key_for_harvest_area,
    harvest_item_area_for_shift,
    resolve_corn_subtype,
)
from plans import (
    API_TOKEN as _PLANS_API_TOKEN,
    BASE_URL as _PLANS_BASE_URL,
    TZ_MSK,
    FIRM_TO_FOLDER,
    FOLDER_TO_FIRM,
    FIRM_ORDER,
    FIELD_NAME_FORCE_RULES,
    WORK_GROUPS,
    HARDCODED_PLAN_FLAT,
    HIRED_GROUP_ID,
)

API_TOKEN = (os.environ.get("CROPWISE_API_TOKEN") or "6DayNcBDr9BWEZgst1Dy").strip() or _PLANS_API_TOKEN
BASE_URL = (
    (os.environ.get("CROPWISE_BASE_URL") or "").strip()
    or _PLANS_BASE_URL
).rstrip("/")

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

REPORT_DATE = date(2026, 9, 5)
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])
SEASON_YEAR = REPORT_DATE.year

# =============================================================================
# NAMOLOT API — валовый сбор (замена ручного ввода с фронта / модального окна)
# =============================================================================
# Источник истины по валовому сбору (т) теперь — отчёт «Намолот» (1С), а не
# ручной ввод оператора и не harvested_weight из Cropwise agro_operations.
# Площадь (га) по-прежнему считается из Cropwise (mtfmi/agro_operations) —
# это не меняется.

NAMOLOT_BASE_URL = (
    (os.environ.get("NAMOLOT_BASE_URL") or "").strip()
    or "http://10.0.1.5/hpp_elevator_new/hs/ground/report"
)
NAMOLOT_USERNAME = os.environ.get("NAMOLOT_USERNAME", "Сводка").strip()
NAMOLOT_PASSWORD = os.environ.get("NAMOLOT_PASSWORD", "3VzLqA6S").strip()

# ВАЖНО: quantity в ответе API — предположительно килограммы (иначе цифры по
# одному полю получались бы на 3 порядка выше правдоподобных). Если окажется,
# что это уже тонны — поменять коэффициент на 1.0.
NAMOLOT_QTY_TO_TONNES: float = 1.0 / 1000.0

# =============================================================================
# NET WEIGHT API (agro_buh) — зачётный вес овощей за сезон
# =============================================================================
NET_WEIGHT_BASE_URL = (
    (os.environ.get("NET_WEIGHT_BASE_URL") or "").strip()
    or "http://10.0.1.5/agro_buh/hs/NetWeight/GetWeight"
)
NET_WEIGHT_USERNAME = (
    (os.environ.get("NET_WEIGHT_USERNAME") or "").strip() or NAMOLOT_USERNAME
)
NET_WEIGHT_PASSWORD = (
    (os.environ.get("NET_WEIGHT_PASSWORD") or "").strip() or NAMOLOT_PASSWORD
)
# Weight в ответе API — килограммы (как quantity в Namolot).
try:
    NET_WEIGHT_TO_TONNES: float = float(
        os.environ.get("NET_WEIGHT_TO_TONNES", str(1.0 / 1000.0))
    )
except ValueError:
    NET_WEIGHT_TO_TONNES = 1.0 / 1000.0

# Сопоставление ведём НЕ по организации/культуре из самого Namolot (текстовые
# поля organization/crop там ненадёжны — см. историю переписки), а по полю
# (spp). Правило: базовый код поля — это всё до первого пробела или "(" —
# всегда совпадает в обеих системах, суффикс (культура/год/уточнение) может
# быть добавлен с любой стороны:
#   CW "ФКО-02"                    <-> Namolot "ФКО-02 Картофель 26г."
#   CW "РВ-04-25(АА) Пшеница озимая" <-> Namolot "РВ-04-25"
# По найденному field_id агрофирму и культуру берём из тех же справочников
# Cropwise, что использует остальной отчёт (field_to_firm_map/field_to_crop_map) —
# так и агрофирма, и название культуры сразу совпадают с CROP_CONFIGS/
# GENERAL_CROP_ORDER без отдельного словаря соответствий.

_FIELD_BASE_CODE_RE = re.compile(r"^[^\s(]+")


def _field_base_code(name: str | None) -> str:
    """Базовый код поля — всё до первого пробела или '(' , в верхнем регистре."""
    if not name:
        return ""
    m = _FIELD_BASE_CODE_RE.match(name.strip())
    if not m:
        return ""
    # Namolot иногда помечает поле префиксом «*» (напр. «*АИ-04-56(АФ15) …»),
    # в Cropwise то же поле без звёздочки — иначе тонны не попадают в сумму.
    return m.group(0).upper().lstrip("*")


# Канонические названия культур отчёта — сюда сопоставляем текстовое поле
# "crop" записи Namolot. Кукуруза обрабатывается отдельно (см. ниже) —
# ей нужно уточнение силос/зерно по данным Cropwise (field_id), которого
# в самом тексте "crop" нет.
_CANONICAL_CROP_NAMES: list[str] = [
    "Рапс озимый",
    "Рапс яровой",
    "Пшеница озимая",
    "Тритикале озимая",
    "Пшеница яровая",
    "Соя",
    "Подсолнечник",
]

# Известные расхождения в названии культуры между Cropwise и Namolot
# (род, опечатки вроде «Трикитакле» в Cropwise). Ключ — нормализованная
# строка (нижний регистр, «ё»→«е», без суффикса «26г.»).
_CROP_NAME_ALIASES: dict[str, str] = {
    "тритикале озимый": "Тритикале озимая",
    "тритикале яровой": "Тритикале яровая",
    "трикитакле озимая": "Тритикале озимая",
    "трикитакле озимый": "Тритикале озимая",
}

_YEAR_SUFFIX_RE = re.compile(r"\s*\d{2,4}\s*г?\.?\s*$")


def _normalize_crop_key(name: str | None) -> str:
    """Нормализация названия культуры для сопоставления alias/канон."""
    s = re.sub(r"\s+", " ", str(name or "").strip())
    s = _YEAR_SUFFIX_RE.sub("", s).strip()
    return s.casefold().replace("ё", "е")


def _normalize_crop_alias(name: str | None) -> str | None:
    """Приводит сырое название культуры к каноническому имени отчёта."""
    if not name:
        return None
    s = str(name).strip()
    if not s:
        return None
    key = _normalize_crop_key(s)
    if key in _CROP_NAME_ALIASES:
        return _CROP_NAME_ALIASES[key]
    for canon in _CANONICAL_CROP_NAMES:
        if _normalize_crop_key(canon) == key:
            return canon
    return s


def _canonical_crop_from_namolot_text(crop_text: str | None) -> str | None:
    """
    Сопоставляет сырое поле "crop" записи Namolot с каноническим названием
    культуры отчёта (GENERAL_CROP_ORDER и т.п.).

    ВАЖНО: это ОСНОВНОЙ источник культуры для агрегации намолота, а не
    запасной вариант. Раньше культура бралась из Cropwise field_to_crop_map
    (history_items текущего сезона), но этот справочник может не покрывать
    часть полей (нет активной записи history_items, устаревшие данные и
    т.п.) — тогда совершенно легитимные тонны намолота молча выбрасывались
    из суммы как "unmatched_fields", хотя агрофирма по полю определялась
    нормально. Сопоставление по тексту самой записи Namolot проверено
    вручную (см. диагностику по «Пшеница озимая» — сумма «за сутки»
    совпала с контрольным значением 1037 т), поэтому теперь оно в
    приоритете; field_to_crop_map остаётся только как запасной вариант,
    если текст "crop" не удалось разобрать.
    """
    if not crop_text:
        return None
    t = str(crop_text).strip()
    if not t:
        return None
    t_norm = _normalize_crop_key(t)
    if not t_norm:
        return None
    if "кукуруз" in t_norm:
        return "__corn__"
    aliased = _CROP_NAME_ALIASES.get(t_norm)
    if aliased:
        return aliased
    if "тритикал" in t_norm or "трикитакл" in t_norm:
        return "Тритикале озимая"
    for name in _CANONICAL_CROP_NAMES:
        if _normalize_crop_key(name) == t_norm:
            return name
    # Запасное частичное совпадение — на случай лишних слов/опечаток
    # в тексте "crop" (например, дополнительных пометок сорта).
    for name in _CANONICAL_CROP_NAMES:
        name_low = name.casefold()
        if name_low in t_norm or t_norm in name_low:
            return name
    return None


_field_base_code_to_id: dict[str, int] | None = None


def _build_field_base_code_index() -> dict[str, int]:
    """base_code (нормализованное начало названия поля Cropwise) -> field_id."""
    global _field_base_code_to_id
    if _field_base_code_to_id is not None:
        return _field_base_code_to_id
    index: dict[str, int] = {}
    dupes: dict[str, list[str]] = defaultdict(list)
    for f in get_cached_fields():
        fid = f.get("id")
        name = f.get("name") or f.get("title") or ""
        code = _field_base_code(name)
        if not code or fid is None:
            continue
        if code in index and index[code] != int(fid):
            dupes[code].append(name)
            continue
        index[code] = int(fid)
    if dupes:
        print(
            f"  [namolot] ВНИМАНИЕ: неоднозначный базовый код поля "
            f"(несколько полей Cropwise дают один и тот же код): {dict(dupes)}",
            flush=True,
        )
    _field_base_code_to_id = index
    return index


def _namolot_auth_header() -> dict:
    if not (NAMOLOT_USERNAME and NAMOLOT_PASSWORD):
        return {}
    creds = f"{NAMOLOT_USERNAME}:{NAMOLOT_PASSWORD}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(creds).decode("ascii")}


def fetch_namolot_records(date_begin: date, date_end: date) -> list[dict]:
    """Сырые записи «Намолот» — только через namolot_client (без повторного live GET)."""
    from namolot_client import fetch_namolot_records as _fetch_with_cache

    return _fetch_with_cache(date_begin, date_end)


def _parse_namolot_datetime(value) -> datetime | None:
    """
    Разбирает поле "date" записи Namolot в полноценный datetime с TZ_MSK.

    Поддерживает то же многообразие форматов, что и остальной отчёт
    (ISO-строка, unix timestamp в секундах/мс), но, в отличие от старой
    parse_date_value из первого скрипта, всегда возвращает datetime
    (с временем, а не голую дату) и приводит его к TZ_MSK — это нужно,
    чтобы корректно сравнивать запись с границами смены 06:00–06:00.

    Отдельно поддерживается формат 1С "ДД.ММ.ГГГГ ЧЧ:ММ:СС" (и его
    укороченные варианты без секунд/времени) — именно в таком виде
    приходит поле "date" из реального API «Намолот». Без этой ветки
    datetime.fromisoformat падает с ValueError, запись молча уходит в
    undated_records и выпадает из суммы целиком (и «за сутки», и «за
    сезон»), что и даёт неверный итог.
    """
    if value is None or value == "" or value == 0:
        return None

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.lstrip("-").isdigit():
            return _parse_namolot_datetime(int(stripped))
        s = stripped
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt: datetime | None = None
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_MSK)
        else:
            dt = dt.astimezone(TZ_MSK)
        return dt

    if isinstance(value, (int, float)):
        ts = value / 1000 if abs(value) > 10 ** 12 else value
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except (ValueError, OSError, OverflowError):
            return None

    return None


@dataclass
class _NamolotAgg:
    # ключ: (агрофирма, культура) -> тонны
    season: dict = field(default_factory=lambda: defaultdict(float))
    day: dict = field(default_factory=lambda: defaultdict(float))
    unmatched_fields: set = field(default_factory=set)
    undated_records: int = 0


_namolot_cache: dict[date, _NamolotAgg] = {}


def _build_namolot_agg(label_date: date) -> _NamolotAgg:
    """Загружает и агрегирует Namolot один раз за прогон скрипта.

    Период запроса — от самого раннего начала сезона (зерновой или овощной)
    до отчётной даты. Дальше данные делятся на «за сезон» (нарастающим
    итогом) и «за сутки» СТРОГО по смене 06:00–06:00 (как весь остальной
    отчёт), а не по календарной дате поля "date":

      - день смены с меткой label_date длится с 06:00 label_date
        до 05:59:59 (label_date + 1);
      - запись относится к этой смене, если её полное время (дата+время
        из поля "date") попадает в [day_start, cutoff);
      - «за сезон» — сумма всех записей с временем строго раньше cutoff
        текущей смены (записи «из будущего», ещё не наступившей смены,
        не учитываются — это позволяет держать сезонный итог
        синхронным с сутками отчёта, даже если API уже вернул более
        поздние строки).
    """
    if label_date in _namolot_cache:
        return _namolot_cache[label_date]

    period_start = min(SEASON_START, VEG_SEASON_START)
    records = fetch_namolot_records(period_start, REPORT_DATE)

    field_base_index = _build_field_base_code_index()
    field_to_firm_map = load_field_to_firm()
    field_to_crop_map = get_field_to_crop()

    # Смена, соответствующая label_date: 06:00 label_date -> 06:00 (label_date+1).
    day_start, cutoff = compute_time_range(label_date + timedelta(days=1))

    agg = _NamolotAgg()
    unmatched_crop_texts: Counter = Counter()
    for r in records:
        spp = r.get("spp")
        fid = field_base_index.get(_field_base_code(spp))
        firm = field_to_firm_map.get(fid) if fid is not None else None

        crop_raw = r.get("crop")
        crop = _canonical_crop_from_namolot_text(crop_raw)
        if crop == "__corn__":
            # Уточнение силос/зерно всё равно требует field_id (Cropwise) —
            # определяем по операции «Скашивание» на поле, как и раньше.
            crop = _resolve_corn_subtype("Кукуруза", fid) if fid is not None else None
        if not crop:
            # Запасной путь — старое сопоставление через Cropwise, на
            # случай если текст "crop" пустой/нераспознаваемый.
            crop = field_to_crop_map.get(fid) if fid is not None else None

        if not firm or not crop:
            agg.unmatched_fields.add(spp or "")
            if not crop:
                unmatched_crop_texts[str(crop_raw or "")] += 1
            continue
        qty_t = float(r.get("quantity") or 0) * NAMOLOT_QTY_TO_TONNES
        if qty_t <= 0:
            continue

        rec_dt = _parse_namolot_datetime(r.get("date"))
        if rec_dt is None:
            agg.undated_records += 1
            continue

        # Запись из ещё не наступившей (относительно текущей смены) смены
        # в расчёт не берём вовсе — ни в сезон, ни в сутки.
        if rec_dt >= cutoff:
            continue

        key = (firm, crop)
        agg.season[key] += qty_t
        if day_start <= rec_dt < cutoff:
            agg.day[key] += qty_t

    if unmatched_crop_texts:
        print(
            f"  [namolot] ВНИМАНИЕ: не распознан текст культуры (записей): "
            f"{dict(unmatched_crop_texts.most_common(20))}", flush=True,
        )
    if agg.unmatched_fields:
        shown = sorted(agg.unmatched_fields)[:30]
        more = "" if len(agg.unmatched_fields) <= 30 else f" (и ещё {len(agg.unmatched_fields) - 30})"
        print(
            f"  [namolot] ВНИМАНИЕ: не сопоставлены поля "
            f"({len(agg.unmatched_fields)}): {shown}{more}", flush=True,
        )
    if agg.undated_records:
        print(
            f"  [namolot] ВНИМАНИЕ: записей без распознанной даты/времени "
            f"(пропущены): {agg.undated_records}", flush=True,
        )
    print(
        f"  [namolot] смена {day_start.strftime('%d.%m %H:%M')} — "
        f"{cutoff.strftime('%d.%m %H:%M')}, записей всего: {len(records)}, "
        f"комбинаций фирма/культура: {len(agg.season)}", flush=True,
    )
    _namolot_cache[label_date] = agg
    return agg


def get_namolot_harvest(
    firm_name: str,
    crop_name: str,
    label_date: date,
) -> tuple[float, float]:
    """(валовый сбор за сезон, за сутки), т — по агрофирме и культуре."""
    agg = _build_namolot_agg(label_date)
    key = (firm_name, crop_name)
    return round(agg.season.get(key, 0.0)), round(agg.day.get(key, 0.0))


def get_namolot_harvest_holding(
    crop_name: str,
    label_date: date,
) -> tuple[float, float]:
    """(валовый сбор за сезон, за сутки), т — по культуре суммарно по холдингу."""
    agg = _build_namolot_agg(label_date)
    season = sum(v for (_, c), v in agg.season.items() if c == crop_name)
    day = sum(v for (_, c), v in agg.day.items() if c == crop_name)
    return round(season), round(day)

def resolve_combine_season_start(report_date: date) -> date:
    """Начало комбайнового сезона для report_date (10 июня ± env override)."""
    season_start = date(report_date.year, 6, 10)
    if report_date < season_start:
        season_start = date(report_date.year - 1, 6, 10)
    # SVODKI_COMBINE_SEASON_START — начало уборки (10.06); SVODKI_SEASON_START_DATE
    # из season_ranges часто календарный год (01.07) и тянет прошлый сезон в факт.
    _season_start_raw = (
        os.environ.get("SVODKI_COMBINE_SEASON_START")
        or os.environ.get("SVODKI_SEASON_START_DATE")
        or os.environ.get("SEASON_START_DATE")
        or ""
    ).strip()
    if _season_start_raw:
        try:
            season_start = date.fromisoformat(_season_start_raw.split("T")[0])
        except ValueError:
            pass
    return season_start


def resolve_veg_season_start(report_date: date) -> date:
    """Сезон овощей 5.4 (1 июня ± SVODKI_WORK_SEASON_START_DATE)."""
    veg_start = date(report_date.year, 6, 1)
    if report_date < veg_start:
        veg_start = date(report_date.year - 1, 6, 1)
    _veg_work_season_raw = (
        os.environ.get("SVODKI_WORK_SEASON_START_DATE")
        or os.environ.get("SVODKI_SVODKA2_SEASON_START")
        or ""
    ).strip()
    if _veg_work_season_raw:
        try:
            veg_start = date.fromisoformat(_veg_work_season_raw.split("T")[0])
        except ValueError:
            pass
    return veg_start


def configure_for_report_date(report_date: date) -> None:
    """После dynamic import: синхронизировать globals и сбросить кэши."""
    global REPORT_DATE, SEASON_YEAR, SEASON_START, VEG_SEASON_START
    global _harvest_cache, _harvest_cache_date, _field_to_crop_cache, _silage_corn_field_ids_cache
    global _net_weight_cache
    global _cached_fields, _cached_field_groups, _cached_group_folders
    global _folder_fld_ids_cache, _field_base_code_to_id
    REPORT_DATE = report_date
    SEASON_YEAR = report_date.year
    SEASON_START = resolve_combine_season_start(report_date)
    VEG_SEASON_START = resolve_veg_season_start(report_date)
    _harvest_cache = None
    _harvest_cache_date = None
    _field_to_crop_cache = None
    _silage_corn_field_ids_cache = None
    _cached_fields = None
    _cached_field_groups = None
    _cached_group_folders = None
    _folder_fld_ids_cache = None
    _field_base_code_to_id = None
    _net_weight_cache.clear()


SEASON_START = resolve_combine_season_start(REPORT_DATE)

DAY_START_HOUR: int = 6   # час начала суток
DAY_END_HOUR:   int = 6   # час конца суток (если == DAY_START_HOUR → следующий день)

# Буфер поверх cutoff при отборе заданий по end_time: часть заданий закрывается
# в API с задержкой (комбайн доработал поле, но end_time/синхронизация пришли
# позже реального конца смены). Раньше буфер был жёстко зашит как 2 часа —
# из-за этого задания, закрывшиеся позже 08:00, вообще не попадали в выборку
# tasks_today/tasks_season, и их covered_area_hourly (относящаяся к нужной
# смене) нигде не суммировалась → «Убрано за сутки» получалось заниженным.
# Переопределяется через SVODKI_REPORT10_CUTOFF_BUFFER_HOURS (по умолчанию +2 ч, как 11).
try:
    CUTOFF_BUFFER_HOURS: int = int(
        (os.environ.get("SVODKI_REPORT10_CUTOFF_BUFFER_HOURS") or str(CUTOFF_BUFFER_HOURS)).strip()
    )
except ValueError:
    pass

USE_FAST_MODE = "--fast" in sys.argv

SEMAPHORE_LIMIT = 40
OTHER_CROP_KEY = "__other__"

# Порядок агрофирм берётся из plans
AGROFARM_ORDER: list[str] = FIRM_ORDER

# work_type_id для уборки — из WORK_GROUPS["5.0 Уборка"]
HARVEST_WT_IDS: set[int] = {
    wt_id
    for group_name, wt_ids in WORK_GROUPS
    if group_name == "5.0 Уборка"
    for wt_id in wt_ids
}
# wt для mtfmi-площади зерновых (без овощных 75/76/77)
GRAIN_AREA_WT_IDS: frozenset[int] = frozenset({6, 7, 111, 118, 125, 126, 145})
# + операции «Скашивание» — для «Кукуруза на силос» (площадь из machine_tasks/mtfmi)
AREA_TASK_WT_IDS: frozenset[int] = GRAIN_AREA_WT_IDS | SILAGE_CORN_MOWING_WT_IDS
ALL_WT_IDS = AREA_TASK_WT_IDS

# Овощные культуры — не входят в зерновой намолот и «Иные культуры»
VEGETABLE_FIELD_CROPS: frozenset[str] = frozenset({
    "Картофель", "Морковь", "Свекла столовая", "Свекла", "Капуста", "Лук",
})

# work_type_id для уборки овощей (5.4) — как в сводке 1: все wt из WORK_GROUPS
VEGETABLE_WORK_TYPE_IDS: list[int] = next(
    (list(wt_ids) for group_name, wt_ids in WORK_GROUPS
     if group_name == "5.4 Уборка картофеля, лука" and wt_ids),
    [75, 76, 77],
)
# Обратная совместимость (первый wt)
VEGETABLE_WORK_TYPE: int = VEGETABLE_WORK_TYPE_IDS[0] if VEGETABLE_WORK_TYPE_IDS else 76

# Зерновая уборка (5.0) — без овощных wt 75/76/77 (они в 5.4)
GRAIN_HARVEST_WT_IDS: frozenset[int] = frozenset(
    wt for wt in HARVEST_WT_IDS if wt not in set(VEGETABLE_WORK_TYPE_IDS)
)

# Порядок культур в «Общий намолот» / уборке (сводки 1, 10, 13).
# 1–6 зерновые/масличные; 7 — «Кукуруза, из них»; 7.1/7.2 — зерно/силос.
GENERAL_CROP_ORDER = [
    "Рапс озимый",
    "Пшеница озимая",
    "Тритикале озимая",
    "Рапс яровой",
    "Соя",
    "Подсолнечник",
    "Кукуруза на зерно",
    "Кукуруза на силос",
]

CORN_SUMMARY_LABEL = "Кукуруза, из них"
_CORN_CROPS = frozenset({CORN_GRAIN_CROP, CORN_SILAGE_CROP})
_GENERAL_CROP_ORDER_NON_CORN = [c for c in GENERAL_CROP_ORDER if c not in _CORN_CROPS]

_GENERAL_HARVEST_ROW_NUM: dict[str, int] = {
    "Рапс озимый": 1,
    "Пшеница озимая": 2,
    "Тритикале озимая": 3,
    "Рапс яровой": 4,
    "Соя": 5,
    "Подсолнечник": 6,
}
CORN_SUMMARY_ROW_NUM = 7
CORN_GRAIN_ROW_NUM = "7.1"
CORN_SILAGE_ROW_NUM = "7.2"

# Все уборочные культуры с планами (+ Пшеница яровая — без отдельного листа).
_HARVEST_CROP_ORDER = [
    *GENERAL_CROP_ORDER,
    "Пшеница яровая",
]

# Листы по культурам в XLSX/PDF (стр. 3+).
_REPORT10_CROP_SHEET_ORDER = list(GENERAL_CROP_ORDER)

# work_type_id на культуру (для фильтрации tasks/ops по API).
# Для «Кукурузы на зерно» — wt=118 (5.3 Уборка кукурузы на зерно, из plans).
# Для «Кукурузы на силос» wt здесь не используется напрямую: операции берутся
# по SILAGE_CORN_MOWING_WT_IDS (см. _get_ops_for_crop) — значение оставлено
# для совместимости типов CROP_CONFIGS и не участвует в запросе operations.
_CROP_TO_WT: dict[str, int] = {
    "Рапс озимый":          145,
    "Рапс яровой":          145,
    "Пшеница озимая":       126,
    "Тритикале озимая":     126,
    "Пшеница яровая":       126,
    "Соя":                  126,
    "Подсолнечник":         125,
    "Кукуруза на зерно":    118,
    "Кукуруза на силос":    118,
}

# CROP_CONFIGS: crop → (work_type_id, {firm: (plan_ha, [])})
# plan_ha берётся из plans.HARDCODED_PLAN["5.0 Уборка"][firm][crop] (0.0, если план не задан);
# plan_ids = [] — площадь из mtfmi (cache); вес из agro_operations / ручной ввод
_HARVEST_PLAN_GROUP = "5.0 Уборка"

CROP_CONFIGS: dict[str, tuple[int, dict[str, tuple[float, list[int]]]]] = {
    crop: (
        _CROP_TO_WT[crop],
        {
            firm: (
                HARDCODED_PLAN_FLAT.get((_HARVEST_PLAN_GROUP, firm, crop), 0.0),
                [],
            )
            for firm in FIRM_ORDER
        },
    )
    for crop in _HARVEST_CROP_ORDER
}

# VEGETABLE_CONFIG: план уборки овощей берётся из
# plans.HARDCODED_PLAN["5.4 Уборка картофеля, лука"]["АФ Чистопольская"] (0.0, если план не задан).
# Структура: crop → (plan_ha, plan_ids, harvest_total, harvest_today, fact_manual, fact_today_man)
# harvest_* и fact_* выставляются в 0 — подтягиваются из API динамически.
_VEG_HARVEST_PLAN_GROUP = "5.4 Уборка картофеля, лука"
_VEG_HARVEST_FIRM = "АФ Чистопольская"

# Отображаемое название в отчёте → название культуры в HARDCODED_PLAN
_VEG_DISPLAY_TO_PLAN_CROP: dict[str, str] = {
    "Картофель":       "Картофель",
    "Морковь":         "Морковь",
    "Свекла столовая": "Свекла столовая",
}

VEGETABLE_CONFIG: dict[str, tuple[float, list[int], float, float, float, float]] = {
    display_crop: (
        HARDCODED_PLAN_FLAT.get(
            (_VEG_HARVEST_PLAN_GROUP, _VEG_HARVEST_FIRM, plan_crop), 0.0
        ),
        [], 0, 0, 0.0, 0,
    )
    for display_crop, plan_crop in _VEG_DISPLAY_TO_PLAN_CROP.items()
}

# Сезон овощей (5.4) — с 1 июня (как сводка 2 HARVEST_SEASON_START).
VEG_SEASON_START = resolve_veg_season_start(REPORT_DATE)

_VEG_HARVEST_CROPS = frozenset(_VEG_DISPLAY_TO_PLAN_CROP.keys())
# Правило "площадь >= 0.5 га" на уборку овощей (5.4) не распространяется
# (как в сводке 11) — отсекаем только cov <= 0, иначе реальные факты вроде
# 0.41 га пропадали бы из отчёта.
_VEG_MIN_AREA_HA = 0.0
SVODKA10_VEG_FIX = "2026-07-14a"
SVODKA10_NET_WEIGHT = "2026-09-07b"
SVODKA10_CROP_FIX = "2026-07-16d"
SVODKA10_HDR_FIX = "2026-07-15b"

COLUMNS = [
    "№ п/п", "Агрофирма", "План, га",
    "Факт общий, га", "Валовый сбор общий, т",
    "Факт за сутки, га", "Валовый сбор за сутки, т",
    "Доля убранной площади в %",
    "Урожайность за сутки (по валовому сбору), т/га",
    "Урожайность за сезон (по валовому сбору), т/га",
]

GENERAL_COLUMNS = [
    "№ п/п", "Культура", "План, га",
    "Факт общий, га", "Валовый сбор общий, т",
    "Факт за сутки, га", "Валовый сбор за сутки, т",
    "Урожайность общая (по валовому сбору), т/га",
    "Доля убранной площади в %",
    "Средняя выработка, га / комбайн",
]

HOLDING_SUMMARY_COLUMNS = [
    "№ п/п", "Культура", "Посеянная площадь, га",
    "Убранная площадь, га", "Остаток к уборке, га",
    "Осталось убрать, %", "Валовой сбор, т",
    "Урожайность за сезон, т/га", "Убрано за сутки, га",
    "Валовый сбор за сутки, т", "Урожайность за сутки, т/га",
]

VEG_COLUMNS = [
    "№ п/п", "Культура", "План, га",
    "Факт общий, га", "Факт за сутки, га",
    "Убранная площадь в %",
    "Валовый сбор общий, т", "Валовый сбор за сутки, т",
    "Зачетный вес за сезон, т",
    "Урожайность за сезон (по валовому сбору), т/га",
]

VEG_PLAN_KEYWORDS: list[tuple[str, str]] = [
    ("картофель", "Картофель"),
    ("морковь", "Морковь"),
    ("свекла", "Свекла столовая"),
    ("свёкла", "Свекла столовая"),
]


# =============================================================================
# ВЫЧИСЛЕНИЕ ВРЕМЕННОГО ДИАПАЗОНА
# =============================================================================

def compute_time_range(
    report_date:    date,
    day_start_hour: int = DAY_START_HOUR,
    day_end_hour:   int = DAY_END_HOUR,
) -> tuple[datetime, datetime]:
    """
    Возвращает (day_start, cutoff) как datetime с TZ_MSK.

    Если day_start_hour == day_end_hour → суточный диапазон
        day_start = (report_date - 1 день) в day_start_hour:00
        cutoff    =  report_date           в day_end_hour:00

    Если day_start_hour != day_end_hour → дневная смена того же дня
        day_start = report_date в day_start_hour:00
        cutoff    = report_date в day_end_hour:00
    """
    if day_start_hour == day_end_hour:
        day_start = datetime(
            report_date.year, report_date.month, report_date.day,
            day_start_hour, 0, 0, tzinfo=TZ_MSK
        ) - timedelta(days=1)
        cutoff = datetime(
            report_date.year, report_date.month, report_date.day,
            day_end_hour, 0, 0, tzinfo=TZ_MSK
        )
    else:
        day_start = datetime(
            report_date.year, report_date.month, report_date.day,
            day_start_hour, 0, 0, tzinfo=TZ_MSK
        )
        cutoff = datetime(
            report_date.year, report_date.month, report_date.day,
            day_end_hour, 0, 0, tzinfo=TZ_MSK
        )
    return day_start, cutoff


def _label_report_date(day_start: datetime) -> date:
    return day_start.date()


def _norm(text: object) -> str:
    return str(text or "").casefold().replace(" ", "").replace("\u00a0", "")


def _cov_from_hourly(item: dict, t_from: datetime, t_to: datetime) -> float:
    hourly = item.get("covered_area_hourly") or []
    cov = 0.0
    for entry in hourly:
        h = parse_dt(entry[0])
        if h and t_from <= h < t_to:
            cov += float(entry[1] or 0.0)
    return cov


def _item_area_in_shift(item: dict, t_from: datetime, t_to: datetime) -> float:
    """
    Площадь item в границах временного окна [t_from, t_to).

    Как в сводке 11: если у записи есть covered_area_hourly — берём строго
    сумму по окну смены (даже если она равна 0, когда все часы задания
    попали в другую смену). covered_area (без разбивки по времени)
    используется как fallback только когда почасовых данных нет вовсе —
    иначе для заданий, пересекающих границу суток, «Факт за сутки»
    задваивался бы на весь covered_area задания.
    """
    hourly = item.get("covered_area_hourly") or []
    if not hourly:
        return float(item.get("covered_area") or 0.0)
    return _cov_from_hourly(item, t_from, t_to)


def _item_area_upto_cutoff(
    item: dict,
    t_to: datetime,
    t_from: datetime | None = None,
) -> float:
    """
    Площадь item по почасовым записям в [t_from, t_to).

    Для сезонного «Факт общий» t_from = season_start (10.06 уборки), чтобы
    не подтягивать прошлый календарный сезон из hourly. Для «за сутки»
    t_from = day_start смены.
    """
    hourly = item.get("covered_area_hourly") or []
    if not hourly:
        return float(item.get("covered_area") or 0.0)
    cov = 0.0
    for entry in hourly:
        h = parse_dt(entry[0])
        if not h or h >= t_to:
            continue
        if t_from is not None and h < t_from:
            continue
        cov += float(entry[1] or 0.0)
    return cov


def _task_shift_bounds(task: dict) -> tuple[datetime, datetime] | None:
    """Границы смены по end_time задания (06:00–06:00)."""
    end_dt = parse_dt(task.get("end_time"))
    if end_dt is None:
        return None
    cal = end_dt.date()
    anchor = datetime(
        cal.year, cal.month, cal.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    if end_dt < anchor:
        cal = cal - timedelta(days=1)
    shift_start = datetime(
        cal.year, cal.month, cal.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    return shift_start, shift_start + timedelta(days=1)


def _item_area_for_harvest_task(
    item: dict,
    task: dict,
    wt: int,
    *,
    t_to: datetime,
    per_shift_mowing: bool,
    season_start: datetime | None = None,
    day_start: datetime | None = None,
) -> float:
    """
    Площадь MTFMI по заданию уборки.

    Зерновые wt (5.0 Уборка): _item_area_upto_cutoff — как сводка 2.
    Скашивание кукурузы на силос: площадь в окне смены задания, иначе
    tasks_season с _item_area_upto_cutoff даёт тысячи га от прошлых косов.
    """
    if wt in SILAGE_CORN_MOWING_WT_IDS and per_shift_mowing:
        bounds = _task_shift_bounds(task)
        if bounds is None:
            return 0.0
        shift_start, shift_end = bounds
        if season_start is not None and shift_start < season_start:
            return 0.0
        window_start = shift_start
        if day_start is not None:
            window_start = max(shift_start, day_start)
        window_end = min(shift_end, t_to)
        if window_start >= window_end:
            return 0.0
        return _item_area_in_shift(item, window_start, window_end)
    t_from = day_start if day_start is not None else season_start
    return _item_area_upto_cutoff(item, t_to, t_from)


# =============================================================================
# HTTP
# =============================================================================

session = requests.Session()
session.headers.update({
    "X-User-Api-Token": API_TOKEN,
    "Content-Type":     "application/json",
})
REQUEST_TIMEOUT = 120

# Кэши
_agro_ops_cache:       dict[tuple[int, int], list[dict]] = {}
_cached_fields:        list[dict] | None = None
_cached_field_groups:  list[dict] | None = None
_folder_fld_ids_cache: dict[int, set[int]] | None = None
_field_to_crop_cache:  dict[int, str] | None = None
_PREFETCH_ENDPOINTS: dict | None = None
_PREFETCH_LOAD_DONE = False
_harvest_cache = None
_harvest_cache_date: date | None = None


def _prefetch_endpoints() -> dict | None:
    global _PREFETCH_ENDPOINTS, _PREFETCH_LOAD_DONE
    if _PREFETCH_LOAD_DONE:
        return _PREFETCH_ENDPOINTS
    _PREFETCH_LOAD_DONE = True
    raw = (os.environ.get("SVODKI_PREFETCH_DATA_PATH") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        endpoints = data.get("endpoints")
        if isinstance(endpoints, dict):
            _PREFETCH_ENDPOINTS = endpoints
            profile = str(data.get("prefetch_profile") or "").strip()
            print(
                f"  Prefetch JSON: {path.name}"
                + (f" ({profile})" if profile else ""),
                flush=True,
            )
    except Exception as exc:
        print(f"  Prefetch JSON не прочитан ({exc}), живой API", flush=True)
    return _PREFETCH_ENDPOINTS


def _mtfmi_map_from_prefetch(task_ids: set[int]) -> dict[int, list[dict]]:
    endpoints = _prefetch_endpoints()
    if not endpoints:
        return {}
    rows = endpoints.get("machine_task_field_mapping_items") or []
    out: dict[int, list[dict]] = {}
    for row in rows:
        try:
            tid = int(row.get("machine_task_id"))
        except Exception:
            continue
        if tid not in task_ids:
            continue
        out.setdefault(tid, []).append(row)
    return out


def parse_dt(s) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        ts = float(s)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(s).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_MSK)
    else:
        dt = dt.astimezone(TZ_MSK)
    return dt


def get_all_from_id(endpoint: str, extra_params: dict | None = None) -> list[dict]:
    params  = dict(extra_params or {})
    results = []
    from_id = 0
    for _ in range(500):
        r = session.get(
            f"{BASE_URL}/{endpoint}",
            params={**params, "from_id": from_id, "limit": 1000},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        body  = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        results.extend(chunk)
        meta    = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = last_id + 1
    return results


MASS_REQUEST_BATCH_SIZE = 300


def _chunked(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _mass_request_sync(endpoint: str, ids: list[int] | set[int]) -> list[dict]:
    """POST /{endpoint}/mass_request батчами по id."""
    id_list = list(dict.fromkeys(int(i) for i in ids if i is not None))
    if not id_list:
        return []
    url = f"{BASE_URL}/{endpoint}/mass_request"
    out: list[dict] = []
    for batch in _chunked(id_list, MASS_REQUEST_BATCH_SIZE):
        r = session.post(url, json={"data": batch}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        out.extend(r.json().get("data", []))
    return out


def _harvest_preload_wt_ids() -> set[int]:
    wts = set(GRAIN_HARVEST_WT_IDS) | set(SILAGE_CORN_MOWING_WT_IDS)
    for _crop, (wt, _firms) in CROP_CONFIGS.items():
        wts.add(int(wt))
    return wts


def _field_ids_from_preloaded_agro_ops(
    wt_ids: set[int] | list[int],
    season: int = SEASON_YEAR,
) -> set[int]:
    out: set[int] = set()
    for wt in wt_ids:
        for op in get_agro_operations(int(wt), season=season):
            fid = op.get("field_id")
            if fid is not None:
                out.add(int(fid))
    return out


def _collect_report_field_ids(
    tasks: list[dict],
    mtfmi_map: dict[int, list[dict]],
    extra_field_ids: set[int] | None = None,
) -> set[int]:
    from report_resources_fetch import collect_report_resource_ids

    field_ids, _, _ = collect_report_resource_ids(tasks, mtfmi_map)
    if extra_field_ids:
        field_ids.update(extra_field_ids)
    return field_ids


def _fields_from_prefetch_subset(field_ids: set[int]) -> list[dict] | None:
    ep = _prefetch_endpoints()
    if not ep or not ep.get("fields"):
        return None
    all_fields = ep["fields"]
    if not field_ids:
        return all_fields
    fid_set = field_ids
    return [f for f in all_fields if f.get("id") in fid_set]


def ensure_report_fields_loaded_sync(field_ids: set[int]) -> None:
    """Точечная загрузка fields (mass_request) — sync-путь (--fast и fallback)."""
    global _cached_fields, _field_base_code_to_id
    if _cached_fields is not None:
        return
    pref = _fields_from_prefetch_subset(field_ids)
    if pref is not None:
        _cached_fields = pref
        print(f"  Поля из prefetch JSON: {len(_cached_fields)}", flush=True)
        _field_base_code_to_id = None
        return
    if field_ids:
        print(f"  Загружаем {len(field_ids)} полей через mass_request...", flush=True)
        _cached_fields = _mass_request_sync("fields", field_ids)
    else:
        print("  [WARN] field_ids пуст — полная загрузка fields", flush=True)
        _cached_fields = get_all_from_id("fields")
    print(f"  Поля для отчёта: {len(_cached_fields)}", flush=True)
    _field_base_code_to_id = None


async def ensure_report_fields_loaded_async(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    field_ids: set[int],
) -> None:
    """Точечная загрузка fields (mass_request) — async-путь prepare_harvest_cache."""
    global _cached_fields, _field_base_code_to_id
    if _cached_fields is not None:
        return
    pref = _fields_from_prefetch_subset(field_ids)
    if pref is not None:
        _cached_fields = pref
        print(f"  Поля из prefetch JSON: {len(_cached_fields)}", flush=True)
        _field_base_code_to_id = None
        return
    if field_ids:
        from report_resources_fetch import load_fields_list_async

        _cached_fields = await load_fields_list_async(
            client, BASE_URL, field_ids, semaphore,
        )
    else:
        print("  [WARN] field_ids пуст — полная загрузка fields", flush=True)
        _cached_fields = get_all_from_id("fields")
    print(f"  Поля для отчёта: {len(_cached_fields)}", flush=True)
    _field_base_code_to_id = None


def _fetch_history_items_for_fields(field_ids: set[int], year: int) -> list[dict]:
    ep = _prefetch_endpoints()
    if ep and ep.get("history_items"):
        fid_set = field_ids
        return [
            hi for hi in ep["history_items"]
            if hi.get("field_id") is not None
            and int(hi["field_id"]) in fid_set
            and int(hi.get("year") or year) == year
        ]
    if not field_ids:
        return get_all_from_id("history_items", {"year": year})
    print(
        f"    [API] history_items year={year} для {len(field_ids)} полей...",
        flush=True,
    )
    results: list[dict] = []
    for batch in _chunked(sorted(field_ids), 300):
        rows = get_all_from_id(
            "history_items",
            {"year": year, "field_id": ",".join(str(i) for i in batch)},
        )
        results.extend(rows)
    if not results:
        print(
            "    [API] history_items по field_id пусто — fallback по году",
            flush=True,
        )
        return get_all_from_id("history_items", {"year": year})
    return results


def get_agro_operations(work_type_id: int,
                         season: int = SEASON_YEAR) -> list[dict]:
    key = (work_type_id, season)
    if key not in _agro_ops_cache:
        ep = _prefetch_endpoints()
        ops: list[dict] | None = None
        if ep and ep.get("agro_operations"):
            ops = [
                o for o in ep.get("agro_operations") or []
                if o.get("work_type_id") == work_type_id
            ]
            if ops:
                print(
                    f"    agro_operations wt={work_type_id} из prefetch: {len(ops)}",
                    flush=True,
                )
            else:
                print(
                    f"    agro_operations wt={work_type_id}: в prefetch пусто, "
                    f"запрос API...",
                    flush=True,
                )
                ops = None
        if ops is None:
            print(f"    [API] agro_operations wt={work_type_id}...", flush=True)
            from machine_tasks_batch_fetch import fetch_by_work_type_ids_sync
            ops = fetch_by_work_type_ids_sync(
                get_all_from_id,
                "agro_operations",
                [work_type_id],
                {"season": season},
                log_prefix="[r10] ",
            )
        _agro_ops_cache[key] = ops
    return _agro_ops_cache[key]


def preload_agro_operations(wt_ids: set[int] | list[int], season: int = SEASON_YEAR) -> None:
    """Batch-загрузка agro_operations для набора work_type_id (заполняет кэш)."""
    from collections import defaultdict

    missing = sorted({int(w) for w in wt_ids if (int(w), season) not in _agro_ops_cache})
    if not missing:
        return
    ep = _prefetch_endpoints()
    if ep and ep.get("agro_operations"):
        still_missing: list[int] = []
        for w in missing:
            ops = [
                o for o in ep.get("agro_operations") or []
                if o.get("work_type_id") == w
            ]
            if ops:
                _agro_ops_cache[(w, season)] = ops
            else:
                still_missing.append(w)
        missing = still_missing
        if not missing:
            return
    from machine_tasks_batch_fetch import fetch_by_work_type_ids_sync
    all_ops = fetch_by_work_type_ids_sync(
        get_all_from_id, "agro_operations", missing, {"season": season}, log_prefix="[r10] preload ",
    )
    by_wt: dict[int, list[dict]] = defaultdict(list)
    for op in all_ops:
        wid = op.get("work_type_id")
        if wid is not None:
            by_wt[int(wid)].append(op)
    for w in missing:
        _agro_ops_cache[(w, season)] = by_wt.get(w, [])


def get_cached_fields() -> list[dict]:
    global _cached_fields
    if _cached_fields is None:
        ep = _prefetch_endpoints()
        if ep and ep.get("fields"):
            _cached_fields = ep["fields"]
        else:
            print(
                "  [WARN] get_cached_fields: targeted load не выполнен — "
                "полная загрузка fields",
                flush=True,
            )
            _cached_fields = get_all_from_id("fields")
    return _cached_fields


def get_cached_field_groups() -> list[dict]:
    global _cached_field_groups
    if _cached_field_groups is None:
        ep = _prefetch_endpoints()
        if ep and ep.get("field_groups"):
            _cached_field_groups = ep.get("field_groups") or []
        else:
            _cached_field_groups = get_all_from_id("field_groups")
    return _cached_field_groups


_cached_group_folders: list[dict] | None = None


def get_cached_group_folders() -> list[dict]:
    global _cached_group_folders
    if _cached_group_folders is None:
        ep = _prefetch_endpoints()
        if ep and ep.get("group_folders"):
            _cached_group_folders = ep.get("group_folders") or []
        else:
            _cached_group_folders = get_all_from_id("group_folders")
    return _cached_group_folders


def _folder_parent_map(group_folders: list[dict]) -> dict[int, int | None]:
    parent: dict[int, int | None] = {}
    for gf in group_folders:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = (gf.get("parent_id")
               or gf.get("parent_group_folder_id")
               or gf.get("group_folder_parent_id"))
        parent[int(fid)] = int(pid) if pid is not None else None
    return parent


def _resolve_to_holding_root(
    folder_id:  int | None,
    parent_map: dict[int, int | None],
    root_ids:   set[int],
) -> int | None:
    if folder_id is None:
        return None
    cur = int(folder_id)
    seen: set[int] = set()
    for _ in range(64):
        if cur in root_ids:
            return cur
        if cur in seen:
            return None
        seen.add(cur)
        nxt = parent_map.get(cur)
        if nxt is None:
            return None
        cur = int(nxt)
    return None


def build_field_to_folder() -> dict[int, int]:
    """
    Строит словарь field_id -> root_folder_id через полную иерархию папок.
    Аналог load_field_to_firm из 2_main_ideal_compact.py — обходит parent_id
    вплоть до корневых папок холдинга из FIRM_TO_FOLDER.
    """
    all_fields = get_cached_fields()
    all_fgs    = get_cached_field_groups()
    all_gfs    = get_cached_group_folders()

    fgs_map    = {fg["id"]: fg for fg in all_fgs}
    parent_map = _folder_parent_map(all_gfs)
    root_ids   = set(FIRM_TO_FOLDER.values())

    result: dict[int, int] = {}
    for f in all_fields:
        fid = f.get("id")
        if fid is None:
            continue
        fgid       = f.get("field_group_id")
        raw_folder = f.get("group_folder_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(fgid, {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None
        root = _resolve_to_holding_root(rint, parent_map, root_ids)
        if root is not None:
            result[int(fid)] = root
    return result


def load_field_to_firm() -> dict[int, str]:
    """field_id → агрофирма (как в сводке 11)."""
    all_fields = get_cached_fields()
    all_fgs = get_cached_field_groups()
    all_gfs = get_cached_group_folders()
    fgs_map = {fg["id"]: fg for fg in all_fgs}
    parent_map = _folder_parent_map(all_gfs)
    root_ids = set(FIRM_TO_FOLDER.values())
    field_to_firm: dict[int, str] = {}

    for f in all_fields:
        fid = f.get("id")
        if fid is None:
            continue
        fgid = f.get("field_group_id")
        raw_folder = f.get("group_folder_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(fgid, {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None
        root = _resolve_to_holding_root(rint, parent_map, root_ids)
        if root is not None:
            firm = FOLDER_TO_FIRM.get(root)
            if firm:
                field_to_firm[int(fid)] = firm

    for f in all_fields:
        fid = f.get("id")
        if fid is None:
            continue
        blob = _norm(f"{f.get('name') or ''} {f.get('title') or ''}")
        for needle, firm_key in FIELD_NAME_FORCE_RULES:
            if needle in blob:
                field_to_firm[int(fid)] = firm_key
                break

    return field_to_firm


def get_folder_fld_ids() -> dict[int, set[int]]:
    global _folder_fld_ids_cache
    if _folder_fld_ids_cache is None:
        field_to_folder = build_field_to_folder()
        result: dict[int, set[int]] = defaultdict(set)
        for fid, folder in field_to_folder.items():
            result[folder].add(fid)
        _folder_fld_ids_cache = dict(result)
    return _folder_fld_ids_cache


_silage_corn_field_ids_cache: set[int] | None = None


def get_silage_corn_field_ids() -> set[int]:
    """
    field_id полей с операцией «Скашивание» (SILAGE_CORN_MOWING_WT_IDS) в сезоне —
    уборка кукурузы на таких полях считается «Кукуруза на силос».
    """
    global _silage_corn_field_ids_cache
    if _silage_corn_field_ids_cache is None:
        field_ids = collect_silage_corn_field_ids(
            SEASON_YEAR,
            lambda wt_id, season: get_agro_operations(wt_id, season=season),
        )
        _silage_corn_field_ids_cache = field_ids
        print(f"    кукуруза на силос (Скашивание): {len(field_ids)} полей", flush=True)
    return _silage_corn_field_ids_cache


def _resolve_corn_subtype(crop_name: str, field_id: int | None) -> str:
    return resolve_corn_subtype(crop_name, field_id, get_silage_corn_field_ids())


def get_field_to_crop() -> dict[int, str]:
    """
    Строит словарь field_id -> crop_name через history_items + crops.
    Аналог логики из 2_main_ideal_compact.py:
    используется для разделения культур с одинаковым work_type_id
    (например, Рапс озимый / Рапс яровой оба имеют wt=145).

    Кукуруза дополнительно разбивается на «на силос» / «на зерно»
    по признаку операции «Скашивание» на поле (см. _resolve_corn_subtype),
    а не по названию культуры из history_items/crops.
    """
    global _field_to_crop_cache
    if _field_to_crop_cache is None:
        print("    [API] crops...", flush=True)
        crops_map = {c["id"]: (c.get("name") or c.get("title", ""))
                     for c in get_all_from_id("crops")}
        print(f"    [API] history_items year={SEASON_YEAR}...", flush=True)
        his_all = get_all_from_id("history_items", {"year": SEASON_YEAR})
        # Prefetch может содержать 2026+2027; для уборки — только год отчёта.
        his_all = [
            hi for hi in his_all
            if int(hi.get("year") or SEASON_YEAR) == SEASON_YEAR
        ]
        result: dict[int, str] = {}
        for hi in his_all:
            fid     = hi.get("field_id")
            crop_id = hi.get("crop_id")
            active  = hi.get("active")
            if fid and crop_id and active and crop_id in crops_map:
                fid_i = int(fid)
                crop_name = _normalize_crop_alias(crops_map[crop_id]) or crops_map[crop_id]
                result[fid_i] = _resolve_corn_subtype(crop_name, fid_i)
        _field_to_crop_cache = result
        print(f"    field_to_crop: {len(result)} полей", flush=True)
    return _field_to_crop_cache


def _get_ops_for_crop(crop_name: str, work_type_id: int) -> list[dict]:
    """
    agro_operations для культуры — единая точка входа вместо разрозненных
    fallback-ов на несуществующий wt=127.
    Для «Кукуруза на силос» операции берутся по признаку «Скашивание»
    (SILAGE_CORN_MOWING_WT_IDS) — это специфично только для 10_main.
    Для всех остальных культур (включая «Кукуруза на зерно») — по wt_id
    из plans (_CROP_TO_WT / CROP_CONFIGS).
    """
    if crop_name == CORN_SILAGE_CROP:
        ops: list[dict] = []
        for wt_id in SILAGE_CORN_MOWING_WT_IDS:
            ops.extend(get_agro_operations(wt_id))
        return ops
    return get_agro_operations(work_type_id)


# =============================================================================
# АСИНХРОННЫЕ ЗАПРОСЫ
# =============================================================================

async def fetch_all_pages(
    client:    httpx.AsyncClient,
    url:       str,
    params:    dict,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    items   = []
    from_id = 0
    async with semaphore:
        while True:
            r     = await client.get(url, params={**params,
                                                   "from_id": from_id,
                                                   "limit":   1000})
            body  = r.json()
            chunk = body.get("data", [])
            if not chunk:
                break
            items.extend(chunk)
            meta    = body.get("meta", {}).get("response", {})
            last_id = meta.get("last_record_id")
            if not last_id or meta.get("obtained_records", 0) < 100:
                break
            from_id = last_id + 1
    return items


async def fetch_mtfmi(
    client:    httpx.AsyncClient,
    task_id:   int,
    semaphore: asyncio.Semaphore,
) -> tuple[int, list[dict]]:
    pref = _mtfmi_map_from_prefetch({task_id})
    if task_id in pref:
        return task_id, pref[task_id]
    items = await fetch_all_pages(
        client,
        f"{BASE_URL}/machine_task_field_mapping_items",
        {"machine_task_id": task_id},
        semaphore,
    )
    return task_id, items


async def fetch_mtao(
    client:    httpx.AsyncClient,
    op_id:     int,
    semaphore: asyncio.Semaphore,
) -> tuple[int, list[dict]]:
    items = await fetch_all_pages(
        client,
        f"{BASE_URL}/machine_task_agro_operation_mapping_items",
        {"agro_operation_id": op_id},
        semaphore,
    )
    return op_id, items


# =============================================================================
# БАТЧ-ЗАПРОСЫ mtfmi / mtao (Cropwise поддерживает список значений через
# запятую как equal-фильтр на дочерних объектах) — вместо одного HTTP-запроса
# на каждый machine_task_id / agro_operation_id делаем по одному запросу на
# пачку до *_BATCH_SIZE id. ВАЖНО: это батчинг именно Cropwise API — к
# Namolot ("Намолот", fetch_namolot_records) отношения не имеет: тот
# запрашивается одним GET за весь диапазон дат (date_begin/date_end) и
# списков id вообще не принимает, поэтому здесь не батчуется и не меняется.
# =============================================================================

MTFMI_BATCH_SIZE = 300   # сколько machine_task_id класть в один запрос mtfmi
AGRO_OP_BATCH_SIZE = 300  # сколько agro_operation_id класть в один запрос mtao


async def fetch_mtfmi_batch(
    client:    httpx.AsyncClient,
    task_ids:  list[int],
    semaphore: asyncio.Semaphore,
) -> dict[int, list[dict]]:
    """
    Батч-версия fetch_mtfmi: сначала берём то, что уже есть в prefetch JSON,
    остальное — пачками по MTFMI_BATCH_SIZE machine_task_id за один запрос
    (endpoint принимает список значений через запятую), вместо одного
    запроса на каждую задачу. Пачки, упавшие с ошибкой, повторяем поштучно
    через fetch_mtfmi — так частичный сбой не роняет весь батч.
    """
    task_ids = list(dict.fromkeys(task_ids))  # dedupe, сохраняя порядок
    out = _mtfmi_map_from_prefetch(set(task_ids))
    missing = [tid for tid in task_ids if tid not in out]
    if not missing:
        return out

    url = f"{BASE_URL}/machine_task_field_mapping_items"
    batches = _chunked(missing, MTFMI_BATCH_SIZE)
    batch_results = await asyncio.gather(*[
        fetch_all_pages(
            client, url,
            {"machine_task_id": ",".join(str(tid) for tid in batch)},
            semaphore,
        )
        for batch in batches
    ], return_exceptions=True)

    failed_ids: list[int] = []
    for batch, res in zip(batches, batch_results):
        if isinstance(res, BaseException):
            failed_ids.extend(batch)
            continue
        bucket: dict[int, list[dict]] = defaultdict(list)
        for item in res:
            tid = item.get("machine_task_id")
            if tid is not None:
                bucket[int(tid)].append(item)
        for tid in batch:
            out[tid] = bucket.get(tid, [])

    if failed_ids:
        print(
            f"  Повторяем поштучно {len(failed_ids)} задач (батч mtfmi не удался)...",
            flush=True,
        )
        retry_results = await asyncio.gather(*[
            fetch_mtfmi(client, tid, semaphore) for tid in failed_ids
        ], return_exceptions=True)
        for tid, res in zip(failed_ids, retry_results):
            if isinstance(res, BaseException):
                print(f"    [mtfmi] не удалось получить task_id={tid}: {res}", flush=True)
                out[tid] = []
            else:
                _, items = res
                out[tid] = items
    return out


async def fetch_mtao_batch(
    client:    httpx.AsyncClient,
    op_ids:    list[int],
    semaphore: asyncio.Semaphore,
) -> dict[int, list[dict]]:
    """Батч-версия fetch_mtao — по аналогии с fetch_mtfmi_batch, пачками
    по AGRO_OP_BATCH_SIZE agro_operation_id, с поштучным повтором упавших
    пачек через fetch_mtao."""
    op_ids = list(dict.fromkeys(op_ids))
    if not op_ids:
        return {}

    url = f"{BASE_URL}/machine_task_agro_operation_mapping_items"
    batches = _chunked(op_ids, AGRO_OP_BATCH_SIZE)
    batch_results = await asyncio.gather(*[
        fetch_all_pages(
            client, url,
            {"agro_operation_id": ",".join(str(oid) for oid in batch)},
            semaphore,
        )
        for batch in batches
    ], return_exceptions=True)

    out: dict[int, list[dict]] = {}
    failed_ids: list[int] = []
    for batch, res in zip(batches, batch_results):
        if isinstance(res, BaseException):
            failed_ids.extend(batch)
            continue
        bucket: dict[int, list[dict]] = defaultdict(list)
        for item in res:
            oid = item.get("agro_operation_id")
            if oid is not None:
                bucket[int(oid)].append(item)
        for oid in batch:
            out[oid] = bucket.get(oid, [])

    if failed_ids:
        print(
            f"  Повторяем поштучно {len(failed_ids)} agro_operation_id (батч mtao не удался)...",
            flush=True,
        )
        retry_results = await asyncio.gather(*[
            fetch_mtao(client, oid, semaphore) for oid in failed_ids
        ], return_exceptions=True)
        for oid, res in zip(failed_ids, retry_results):
            if isinstance(res, BaseException):
                print(f"    [mtao] не удалось получить agro_operation_id={oid}: {res}", flush=True)
                out[oid] = []
            else:
                _, items = res
                out[oid] = items
    return out


# =============================================================================
# ВЫЧИСЛЕНИЕ ПЛОЩАДИ (асинхронно)
# =============================================================================

async def fetch_area_for_firm_async(
    client:       httpx.AsyncClient,
    semaphore:    asyncio.Semaphore,
    plan_ids:     list[int],
    work_type_id: int,
    day_start:    datetime,
    cutoff:       datetime,
    tasks_map:    dict[int, dict],
    firm_name:    str,
    field_to_firm: dict[int, str],
) -> tuple[float, float]:
    """Возвращает (fact_total, fact_today) по полям агрофирмы."""
    if not plan_ids:
        return 0.0, 0.0

    ops = get_agro_operations(work_type_id)
    ops = [o for o in ops if o.get("agri_work_plan_id") in set(plan_ids)]
    if not ops:
        return 0.0, 0.0

    mtao_map = await fetch_mtao_batch(client, [op["id"] for op in ops], semaphore)

    task_ids: set[int] = set()
    for items in mtao_map.values():
        for m in items:
            task_ids.add(m["machine_task_id"])

    season_start = datetime(
        SEASON_START.year, SEASON_START.month, SEASON_START.day, tzinfo=TZ_MSK
    )

    # Расширяем на CUTOFF_BUFFER_HOURS часов: задания с end_time чуть позже
    # cutoff (в т.ч. закрытые в API с задержкой) тоже могут содержать
    # hourly-записи внутри смены. _item_area_upto_cutoff обрежет лишнее.
    cutoff_ext = cutoff + timedelta(hours=CUTOFF_BUFFER_HOURS)
    tasks_cumul = [
        tasks_map[tid] for tid in task_ids
        if tid in tasks_map
        and (dt := parse_dt(tasks_map[tid].get("end_time")))
        and season_start <= dt < cutoff_ext
    ]
    tasks_today = [
        tasks_map[tid] for tid in task_ids
        if tid in tasks_map
        and (dt := parse_dt(tasks_map[tid].get("end_time")))
        and day_start <= dt < cutoff_ext
    ]

    all_task_ids_needed = list({t["id"] for t in tasks_cumul + tasks_today})
    if not all_task_ids_needed:
        return 0.0, 0.0

    mtfmi_map = await fetch_mtfmi_batch(client, all_task_ids_needed, semaphore)

    def sum_covered(
        task_list: list[dict],
        t_from: datetime,
        t_to: datetime,
    ) -> float:
        total = 0.0
        for t in task_list:
            wt = t.get("work_type_id")
            for item in mtfmi_map.get(t["id"], []):
                fid = item.get("field_id")
                if fid is None:
                    continue
                if field_to_firm.get(int(fid)) != firm_name:
                    continue
                total += harvest_item_area_for_shift(
                    item, t_to,
                    day_start=t_from,
                    work_type_id=wt,
                    parse_dt=parse_dt,
                )
        return total

    fact_total = round(sum_covered(tasks_cumul, season_start, cutoff), 1)
    fact_today = round(sum_covered(tasks_today, day_start, cutoff), 1)
    return fact_total, fact_today


def _all_harvest_tasks_map(cache: "HarvestDataCache") -> dict[int, dict]:
    """Все machine_tasks уборки из cache (сезонный список, как сводка 11)."""
    return {int(t["id"]): t for t in cache.tasks_season if t.get("id") is not None}


def _cap_area_to_plan(fact: float, plan: float) -> float:
    """Факт общий не может превышать план по культуре/агрофирме."""
    if plan > 0 and fact > plan:
        return round(plan, 1)
    return round(fact, 1)


def _crop_area_from_cache(
    cache: "HarvestDataCache",
    crop_name: str,
    work_type_id: int,
    firm_name: str,
    field_to_crop_map: dict[int, str] | None = None,
) -> tuple[float, float]:
    """Площадь из предрасчёта cache (mtfmi, как сводка 11)."""
    del work_type_id, field_to_crop_map
    return cache.area_by_firm_crop.get((firm_name, crop_name), (0.0, 0.0))


def _firm_area_from_cache_11_style(
    cache: "HarvestDataCache",
    firm_name: str,
) -> tuple[float, float]:
    """Площадь по фирме — как aggregate_harvest_by_firm в сводке 11."""
    return cache.area_by_firm_total.get(firm_name, (0.0, 0.0))


def _other_crops_area_from_cache(
    cache: "HarvestDataCache",
    field_to_crop_map: dict[int, str] | None = None,
) -> tuple[float, float]:
    """Поля без crop / культуры вне GENERAL_CROP_ORDER — остаток к сводке 11."""
    del field_to_crop_map
    fact_total = fact_today = 0.0
    for firm_name in AGROFARM_ORDER:
        ft, fd = cache.area_by_firm_crop.get(
            (firm_name, OTHER_CROP_KEY), (0.0, 0.0),
        )
        fact_total += ft
        fact_today += fd
    return round(fact_total, 1), round(fact_today, 2)


def _populate_harvest_cache_tables(cache: "HarvestDataCache") -> None:
    """Один проход mtfmi → таблицы площади (сходимость со сводкой 11)."""
    field_to_crop = get_field_to_crop()
    general_set = set(GENERAL_CROP_ORDER)
    machines_map = load_machines_map()

    season_fc: dict[tuple[str, str], float] = defaultdict(float)
    today_fc: dict[tuple[str, str], float] = defaultdict(float)
    season_firm: dict[str, float] = defaultdict(float)
    today_firm: dict[str, float] = defaultdict(float)
    machines_by_crop: dict[str, set[int]] = defaultdict(set)
    _note = None
    try:
        from proverka.task_facts import bind_capture

        _bound = bind_capture()
        _note = _bound[0] if _bound else None
    except Exception:
        _note = None

    def process(
        task_list: list[dict],
        t_from: datetime,
        t_to: datetime,
        fc: dict[tuple[str, str], float],
        firm_tot: dict[str, float],
        *,
        day_start: datetime | None = None,
        count_machines: bool = False,
    ) -> None:
        for task in task_list:
            mid = task.get("machine_id")
            if mid is None:
                continue
            wt = task.get("work_type_id")
            if wt not in AREA_TASK_WT_IDS:
                continue
            wt_i = int(wt)
            is_mowing = wt_i in SILAGE_CORN_MOWING_WT_IDS
            for item in cache.mtfmi_map.get(task["id"], []):
                fid = item.get("field_id")
                if fid is None:
                    continue
                fid_i = int(fid)
                firm = cache.field_to_firm.get(fid_i)
                if not firm:
                    continue
                crop = field_to_crop.get(fid_i)
                if is_mowing and crop != CORN_SILAGE_CROP:
                    continue
                cov = harvest_item_area_for_shift(
                    item, t_to,
                    day_start=day_start,
                    work_type_id=wt_i,
                    parse_dt=parse_dt,
                )
                if cov <= 0:
                    continue
                firm_tot[firm] += cov
                if crop in VEGETABLE_FIELD_CROPS:
                    continue
                crop_key = crop_key_for_harvest_area(
                    wt_i, crop, general_set, OTHER_CROP_KEY,
                )
                fc[(firm, crop_key)] += cov
                if _note is not None and count_machines:
                    _note(
                        group="5.0 Уборка",
                        firm=firm,
                        crop=str(crop_key or ""),
                        field_id=fid_i,
                        task_id=int(task["id"]) if task.get("id") is not None else None,
                        machine_id=int(mid) if mid is not None else None,
                        ha=cov,
                        season=str(task.get("season") or ""),
                    )
                if count_machines and _is_harvest_combine(mid, machines_map):
                    if is_mowing:
                        machine_crop = CORN_SILAGE_CROP
                    elif crop in general_set:
                        machine_crop = crop
                    elif crop:
                        machine_crop = crop
                    else:
                        machine_crop = None
                    if machine_crop:
                        machines_by_crop[machine_crop].add(int(mid))

    process(
        cache.tasks_season, cache.season_start, cache.cutoff,
        season_fc, season_firm,
    )
    process(
        cache.tasks_today, cache.day_start, cache.cutoff,
        today_fc, today_firm,
        day_start=cache.day_start,
        count_machines=True,
    )

    area_by_firm_crop: dict[tuple[str, str], tuple[float, float]] = {}
    for firm_name in AGROFARM_ORDER:
        for crop_name in GENERAL_CROP_ORDER:
            area_by_firm_crop[(firm_name, crop_name)] = (
                round(season_fc.get((firm_name, crop_name), 0.0), 1),
                round(today_fc.get((firm_name, crop_name), 0.0), 2),
            )
        area_by_firm_crop[(firm_name, OTHER_CROP_KEY)] = (
            round(season_fc.get((firm_name, OTHER_CROP_KEY), 0.0), 1),
            round(today_fc.get((firm_name, OTHER_CROP_KEY), 0.0), 2),
        )

    cache.field_to_crop = field_to_crop
    cache.area_by_firm_crop = area_by_firm_crop
    cache.area_by_firm_total = {
        firm: (
            round(season_firm.get(firm, 0.0), 1),
            round(today_firm.get(firm, 0.0), 2),
        )
        for firm in AGROFARM_ORDER
    }
    cache.machines_by_crop = dict(machines_by_crop)


def _holding_crop_area_from_cache(
    cache: "HarvestDataCache",
    crop_name: str,
) -> tuple[float, float]:
    fact_total = fact_today = 0.0
    for firm_name in AGROFARM_ORDER:
        ft, fd = cache.area_by_firm_crop.get((firm_name, crop_name), (0.0, 0.0))
        fact_total += ft
        fact_today += fd
    return round(fact_total, 1), round(fact_today, 2)


def _other_crops_harvest_from_agro(report_date: date) -> tuple[float, float, float, float, list[str]]:
    """Валовый сбор «иных» культур из agro_operations (площадь — из mtfmi)."""
    label_date = _label_report_date(compute_time_range(report_date)[0])
    shift_date_str = label_date.isoformat()
    all_ops: list[dict] = []
    for wt in GRAIN_HARVEST_WT_IDS:
        all_ops.extend(get_agro_operations(wt))
    if not all_ops:
        return 0.0, 0.0, 0.0, 0.0, []
    df = pd.DataFrame(all_ops)
    df["completed_area"] = pd.to_numeric(df.get("completed_area"), errors="coerce").fillna(0)
    df["harvested_weight"] = pd.to_numeric(df.get("harvested_weight"), errors="coerce").fillna(0)
    df.loc[df["harvested_weight"] < 0.01, "harvested_weight"] = 0.0
    _ensure_completed_date(df)
    df["_crop"] = df.apply(_get_crop_name_from_op, axis=1)
    df = df[
        df["_crop"].notna()
        & (df["_crop"] != "")
        & ~df["_crop"].isin(set(GENERAL_CROP_ORDER))
        & ~df["_crop"].isin(VEGETABLE_FIELD_CROPS)
    ]
    if df.empty:
        return 0.0, 0.0, 0.0, 0.0, []
    firm_cumul = df[df["completed_date"].astype(str).str[:10] <= shift_date_str]
    firm_today = df[df["completed_date"].astype(str).str[:10] == shift_date_str]
    other_h = round(firm_cumul["harvested_weight"].sum())
    other_hd = round(firm_today["harvested_weight"].sum())
    by_crop = firm_cumul.groupby("_crop")["completed_area"].sum()
    other_names = sorted(k for k, v in by_crop.items() if v > 0)
    return 0.0, 0.0, other_h, other_hd, other_names


def _harvest_from_agro_df(
    firm_df: pd.DataFrame,
    label_date: date,
) -> tuple[float, float, float, float]:
    """Валовый сбор из agro_operations; площадь — только для режима --fast."""
    shift_date_str = label_date.isoformat()
    if firm_df.empty or "completed_date" not in firm_df.columns:
        empty = pd.DataFrame(columns=["completed_area", "harvested_weight"])
        firm_cumul = firm_today = empty
    else:
        firm_cumul = firm_df[
            firm_df["completed_date"].astype(str).str[:10] <= shift_date_str
        ]
        firm_today = firm_df[
            firm_df["completed_date"].astype(str).str[:10] == shift_date_str
        ]
    fact_total = round(firm_cumul["completed_area"].sum(), 1)
    harvest_total = round(firm_cumul["harvested_weight"].sum())
    fact_today = round(firm_today["completed_area"].sum(), 2)
    harvest_today = round(firm_today["harvested_weight"].sum())
    return fact_total, fact_today, harvest_total, harvest_today


@dataclass
class HarvestDataCache:
    field_to_firm: dict[int, str]
    day_start: datetime
    cutoff: datetime
    season_start: datetime
    label_date: date
    tasks_by_wt: dict[int, dict[int, dict]]
    tasks_today: list[dict]
    tasks_season: list[dict]
    mtfmi_map: dict[int, list[dict]]
    mtao_by_op: dict[int, list[dict]] = field(default_factory=dict)
    field_to_crop: dict[int, str] = field(default_factory=dict)
    area_by_firm_crop: dict[tuple[str, str], tuple[float, float]] = field(
        default_factory=dict,
    )
    area_by_firm_total: dict[str, tuple[float, float]] = field(default_factory=dict)
    machines_by_crop: dict[str, set[int]] = field(default_factory=dict)


_machines_map_cache: dict[int, dict] | None = None


def load_machines_map() -> dict[int, dict]:
    global _machines_map_cache
    if _machines_map_cache is not None:
        return _machines_map_cache
    ep = _prefetch_endpoints()
    if ep and ep.get("machines"):
        rows = ep.get("machines") or []
    else:
        rows = get_all_from_id("machines")
    _machines_map_cache = {
        int(m["id"]): m for m in rows if m.get("id") is not None
    }
    return _machines_map_cache


def _is_harvest_combine(
    machine_id: int | None,
    machines_map: dict[int, dict],
) -> bool:
    if machine_id is None:
        return False
    grp = machines_map.get(int(machine_id), {}).get("machine_group_id")
    if grp is None:
        return False
    grp_i = int(grp)
    return grp_i in HARVESTER_GROUP_IDS or grp_i == HIRED_GROUP_ID


def _avg_output_per_combine(
    area_today: float,
    machines: set[int] | None,
) -> float:
    n = len(machines or ())
    if n <= 0 or area_today <= 0:
        return 0.0
    return round(area_today / n, 1)


async def _fetch_mtfmi_for_tasks(
    client: httpx.AsyncClient,
    task_ids: list[int],
    semaphore: asyncio.Semaphore,
) -> dict[int, list[dict]]:
    task_set = set(task_ids)
    pref = _mtfmi_map_from_prefetch(task_set)
    if len(pref) == len(task_set):
        print(f"  mtfmi из prefetch JSON: {len(pref)} задач (без HTTP)", flush=True)
        return pref
    return await fetch_mtfmi_batch(client, task_ids, semaphore)


async def _fetch_tasks_and_mtfmi_like_11(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    day_start: datetime,
    cutoff: datetime,
    season_start: datetime,
) -> tuple[list[dict], list[dict], dict[int, list[dict]]]:
    """Тот же набор tasks/mtfmi, что fetch_all_tasks_and_mtfmi в сводке 11."""
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    season_start_str = season_start.strftime("%Y-%m-%dT%H:%M:%S")
    # Расширяем верхнюю границу запроса на CUTOFF_BUFFER_HOURS часов: задания
    # с end_time чуть позже cutoff (в т.ч. закрытые в API с задержкой) могут
    # содержать hourly-записи внутри смены. _item_area_upto_cutoff отфильтрует
    # только нужное окно.
    cutoff_ext_str = (cutoff + timedelta(hours=CUTOFF_BUFFER_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")

    ep = _prefetch_endpoints()
    if ep and ep.get("machine_tasks"):
        all_rows = ep.get("machine_tasks") or []
        tasks_today: list[dict] = []
        tasks_season: list[dict] = []
        seen_today: set[int] = set()
        seen_season: set[int] = set()
        for t in all_rows:
            wt = t.get("work_type_id")
            if wt not in ALL_WT_IDS:
                continue
            end_dt = parse_dt(t.get("end_time"))
            if not end_dt:
                continue
            tid = t.get("id")
            if tid is None:
                continue
            tid_i = int(tid)
            # Расширяем верхнюю границу на CUTOFF_BUFFER_HOURS часов: задания
            # с end_time чуть позже cutoff (в т.ч. закрытые в API с задержкой)
            # тоже могут содержать hourly-записи внутри смены. Лишнее
            # отфильтруется по covered_area_hourly.
            cutoff_ext = cutoff + timedelta(hours=CUTOFF_BUFFER_HOURS)
            if end_dt >= day_start and end_dt < cutoff_ext and tid_i not in seen_today:
                seen_today.add(tid_i)
                tasks_today.append(t)
            if end_dt >= season_start and end_dt < cutoff_ext and tid_i not in seen_season:
                seen_season.add(tid_i)
                tasks_season.append(t)
        print(
            f"  machine_tasks из prefetch: сутки={len(tasks_today)}, "
            f"сезон={len(tasks_season)}",
            flush=True,
        )
        # Prefetch часто без wt скашивания (вне «5.0 Уборка») — догружаем из API.
        from machine_tasks_batch_fetch import fetch_machine_tasks_by_wt_async

        silage_wts = sorted(SILAGE_CORN_MOWING_WT_IDS)
        sup_today_raw, sup_season_raw = await asyncio.gather(
            fetch_machine_tasks_by_wt_async(
                fetch_all_pages, client, BASE_URL, silage_wts,
                {"end_time_gt_eq": day_start_str, "end_time_lt": cutoff_ext_str},
                semaphore, log_prefix="[r10] silage+ ",
            ),
            fetch_machine_tasks_by_wt_async(
                fetch_all_pages, client, BASE_URL, silage_wts,
                {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_ext_str},
                semaphore, log_prefix="[r10] silage+ ",
            ),
        )
        for t in sup_today_raw:
            tid_i = int(t["id"])
            if tid_i not in seen_today:
                seen_today.add(tid_i)
                tasks_today.append(t)
        for t in sup_season_raw:
            tid_i = int(t["id"])
            if tid_i not in seen_season:
                seen_season.add(tid_i)
                tasks_season.append(t)
        if sup_today_raw or sup_season_raw:
            print(
                f"  machine_tasks скашивание (API): +{len(sup_today_raw)} сутки, "
                f"+{len(sup_season_raw)} сезон",
                flush=True,
            )
    else:
        from machine_tasks_batch_fetch import fetch_machine_tasks_by_wt_async

        tasks_today_raw, tasks_season_raw = await asyncio.gather(
            fetch_machine_tasks_by_wt_async(
                fetch_all_pages, client, BASE_URL, ALL_WT_IDS,
                {"end_time_gt_eq": day_start_str, "end_time_lt": cutoff_ext_str},
                semaphore, log_prefix="[r10] ",
            ),
            fetch_machine_tasks_by_wt_async(
                fetch_all_pages, client, BASE_URL, ALL_WT_IDS,
                {"end_time_gt_eq": season_start_str, "end_time_lt": cutoff_ext_str},
                semaphore, log_prefix="[r10] ",
            ),
        )
        seen_today = set()
        tasks_today = []
        for t in tasks_today_raw:
            if t["id"] not in seen_today:
                seen_today.add(t["id"])
                tasks_today.append(t)
        seen_season = set()
        tasks_season = []
        for t in tasks_season_raw:
            if t["id"] not in seen_season:
                seen_season.add(t["id"])
                tasks_season.append(t)
        print(
            f"  Заданий за сутки: {len(tasks_today)}, "
            f"за сезон (с {season_start.date().strftime('%d.%m.%Y')}): {len(tasks_season)}",
            flush=True,
        )

    all_task_ids = list({t["id"] for t in tasks_today + tasks_season})
    mtfmi_map: dict[int, list[dict]] = {}
    if all_task_ids:
        print(f"  Загружаем mtfmi для {len(all_task_ids)} заданий...", flush=True)
        mtfmi_map = await _fetch_mtfmi_for_tasks(client, all_task_ids, semaphore)
    return tasks_today, tasks_season, mtfmi_map


def _tasks_by_wt_from_lists(
    tasks_today: list[dict],
    tasks_season: list[dict],
) -> dict[int, dict[int, dict]]:
    tasks_by_wt: dict[int, dict[int, dict]] = {wt: {} for wt in ALL_WT_IDS}
    for t in tasks_today + tasks_season:
        wt = t.get("work_type_id")
        tid = t.get("id")
        if wt in ALL_WT_IDS and tid is not None:
            tasks_by_wt[int(wt)][int(tid)] = t
    return tasks_by_wt


async def prepare_harvest_cache_async(report_date: date) -> HarvestDataCache:
    day_start, cutoff = compute_time_range(report_date)
    _season = resolve_combine_season_start(report_date)
    season_start = datetime(_season.year, _season.month, _season.day, tzinfo=TZ_MSK)
    label_date = _label_report_date(day_start)

    preload_wt_ids = _harvest_preload_wt_ids()
    preload_agro_operations(preload_wt_ids, SEASON_YEAR)
    extra_field_ids = _field_ids_from_preloaded_agro_ops(preload_wt_ids)

    headers = {"X-User-Api-Token": API_TOKEN}
    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    async with httpx.AsyncClient(headers=headers, timeout=REQUEST_TIMEOUT, limits=pool) as client:
        tasks_today, tasks_season, mtfmi_map = await _fetch_tasks_and_mtfmi_like_11(
            client, semaphore, day_start, cutoff, season_start,
        )
        field_ids = _collect_report_field_ids(
            tasks_today + tasks_season, mtfmi_map, extra_field_ids,
        )
        print(
            f"  Ресурсы отчёта: полей={len(field_ids)} "
            f"(mtfmi+agro_operations)",
            flush=True,
        )
        await ensure_report_fields_loaded_async(client, semaphore, field_ids)
        field_to_firm = load_field_to_firm()
    tasks_by_wt = _tasks_by_wt_from_lists(tasks_today, tasks_season)

    cache = HarvestDataCache(
        field_to_firm=field_to_firm,
        day_start=day_start,
        cutoff=cutoff,
        season_start=season_start,
        label_date=label_date,
        tasks_by_wt=tasks_by_wt,
        tasks_today=tasks_today,
        tasks_season=tasks_season,
        mtfmi_map=mtfmi_map,
    )
    _populate_harvest_cache_tables(cache)
    return cache


def prepare_harvest_cache(report_date: date) -> HarvestDataCache:
    global _harvest_cache, _harvest_cache_date
    if _harvest_cache is not None and _harvest_cache_date == report_date:
        return _harvest_cache
    _harvest_cache = asyncio.run(prepare_harvest_cache_async(report_date))
    _harvest_cache_date = report_date
    return _harvest_cache


def fetch_harvest_for_firm(plan_ids: list[int],
                            work_type_id: int) -> float:
    if not plan_ids:
        return 0.0
    ops = get_agro_operations(work_type_id)
    ops = [o for o in ops if o.get("agri_work_plan_id") in set(plan_ids)]
    return round(sum(
        o.get("harvested_weight") or 0
        for o in ops
        if (o.get("harvested_weight") or 0) > 0.01
    ))


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def _extract_text_from_plan(obj) -> str:
    if isinstance(obj, str):  return obj.lower()
    if isinstance(obj, dict): return " ".join(_extract_text_from_plan(v)
                                               for v in obj.values() if v)
    if isinstance(obj, list): return " ".join(_extract_text_from_plan(x)
                                               for x in obj if x)
    return str(obj).lower() if obj is not None else ""


def _build_plan_to_vegetable_crop(plan_ids: set) -> dict[int, str]:
    plan_to_crop: dict[int, str] = {}
    for pid in plan_ids:
        if not pid:
            continue
        try:
            r    = session.get(f"{BASE_URL}/agri_work_plans/{pid}",
                                timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            body = r.json()
            p    = body.get("data", body) if isinstance(body, dict) else {}
            text = _extract_text_from_plan(p)
            for keyword, crop_name in VEG_PLAN_KEYWORDS:
                if keyword in text:
                    plan_to_crop[pid] = crop_name
                    break
        except Exception:
            pass
    return plan_to_crop


def _ensure_completed_date(df: pd.DataFrame) -> None:
    if df.empty:
        return
    if "completed_date" not in df.columns and "completed_datetime" in df.columns:
        df["completed_date"] = df["completed_datetime"].astype(str).str[:10]
    elif "completed_date" in df.columns:
        df["completed_date"] = df["completed_date"].astype(str).str[:10]
    else:
        df["completed_date"] = ""


def _safe_sheet_name(name: str) -> str:
    s = "".join(c for c in name if c not in ":*?/\\[]")[:31]
    return s or "Sheet"


# =============================================================================
# СТИЛИЗАЦИЯ EXCEL (как сводка 11: фикс. ширины, авто-высота строк)
# =============================================================================

_WIDTHS_CROP_FIRM: dict[int, float] = {
    1: 6, 2: 32, 3: 12, 4: 14, 5: 14, 6: 12, 7: 14, 8: 14, 9: 18, 10: 18,
}

_WIDTHS_GENERAL: dict[int, float] = {
    1: 6, 2: 20, 3: 12, 4: 14, 5: 14, 6: 12, 7: 14, 8: 18, 9: 14, 10: 18,
}

_WIDTHS_VEG: dict[int, float] = {
    1: 6, 2: 20, 3: 12, 4: 14, 5: 12, 6: 14, 7: 14, 8: 14, 9: 16, 10: 18,
}


def _make_table_styles() -> dict:
    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    return {
        "border": border,
        "align_c": Alignment(horizontal="center", vertical="center", wrap_text=True),
        "align_l": Alignment(horizontal="left", vertical="center", wrap_text=True),
        "fill_hdr": PatternFill("solid", fgColor="8093B3"),
        "fill_data": PatternFill("solid", fgColor="FFFFFF"),
        "fill_total": PatternFill("solid", fgColor="BDD7EE"),
        "font_hdr": Font(bold=True, color="FFFFFF"),
        "font_total": Font(bold=True),
    }


def _style_cell(cell, s: dict, *, fill=None, font=None, align=None) -> None:
    cell.border = s["border"]
    if fill:
        cell.fill = fill
    if font:
        cell.font = font
    if align:
        cell.alignment = align


def _harvest_col_header_label(col: str) -> str:
    """Подпись шапки для Excel/PDF: перенос только по словам, не «Валовы / й сбор»."""
    key = str(col or "").strip()
    labels = {
        "Валовый сбор общий, т": "Валовый сбор\nобщий, т",
        "Валовый сбор за сутки, т": "Валовый сбор\nза сутки, т",
        "Урожайность общая (по валовому сбору), т/га":
            "Урожайность общая\n(по валовому сбору), т/га",
        "Урожайность за сутки (по валовому сбору), т/га":
            "Урожайность за сутки\n(по валовому сбору), т/га",
        "Урожайность за сезон (по валовому сбору), т/га":
            "Урожайность за сезон\n(по валовому сбору), т/га",
        "Зачетный вес за сезон, т": "Зачетный вес за сезон, т",
    }
    return labels.get(key, key)


def _estimate_wrapped_lines(value: object, col_width: float | None) -> int:
    text = str(value or "").strip()
    if not text:
        return 1
    width = float(col_width or 10)
    chars_per_line = max(1, int(width * 0.78))
    parts = text.splitlines() or [text]
    lines = 0
    for part in parts:
        p = part.strip()
        if not p:
            lines += 1
            continue
        lines += max(1, math.ceil(len(p) / chars_per_line))
    return max(1, lines)


def _auto_fit_row_heights(ws, start_row: int, end_row: int, max_col: int) -> None:
    for r in range(start_row, end_row + 1):
        max_lines = 1
        for c in range(1, max_col + 1):
            cell = ws.cell(row=r, column=c)
            if cell.value in (None, ""):
                continue
            col_letter = get_column_letter(c)
            col_width = ws.column_dimensions[col_letter].width
            max_lines = max(max_lines, _estimate_wrapped_lines(cell.value, col_width))
        target = min(200.0, max(20.0, max_lines * 16.0 + 4))
        current = ws.row_dimensions[r].height
        ws.row_dimensions[r].height = max(current or 0.0, target)


def _harvest_col_is_percent(col: str) -> bool:
    c = str(col or "").replace("\n", " ")
    low = c.lower()
    return "%" in c or "в %" in low or "осталось убрать" in low


def _harvest_pct_for_excel(raw) -> tuple[Any, str | None]:
    """Сводка 10 хранит проценты в шкале 0–100 (0.7 = 0,7%); Excel ждёт долю."""
    if raw is None or raw == "":
        return "", None
    n = _coerce_float(raw)
    if n is None:
        return raw, None
    return n / 100.0, NF_PCT


def _write_formatted_harvest_sheet(
    wb: openpyxl.Workbook,
    sheet_name: str,
    df: pd.DataFrame,
    col_widths: dict[int, float],
) -> openpyxl.worksheet.worksheet.Worksheet | None:
    """Таблица с первой строки — шапка PDF рисуется отдельно (как сводка 11)."""
    if df.empty:
        return None
    ws = wb.create_sheet(title=_safe_sheet_name(sheet_name))
    s = _make_table_styles()
    cols = list(df.columns)
    n_cols = len(cols)
    hdr_row = 1

    for ci, col in enumerate(cols, start=1):
        c = ws.cell(row=hdr_row, column=ci, value=_harvest_col_header_label(col))
        _style_cell(
            c, s, fill=s["fill_hdr"], font=s["font_hdr"], align=s["align_c"],
        )

    for ri, row in enumerate(df.itertuples(index=False), start=hdr_row + 1):
        row_data = {cols[i]: row[i] for i in range(n_cols)}
        label_col = cols[1] if n_cols > 1 else cols[0]
        is_total = str(row_data.get(label_col, "")).strip() == "Итого"
        fill = s["fill_total"] if is_total else s["fill_data"]
        font = s["font_total"] if is_total else None
        for ci, col in enumerate(cols, start=1):
            raw = row_data.get(col, "")
            if _harvest_col_is_percent(col):
                val, nf = _harvest_pct_for_excel(raw)
            else:
                val, nf = summary_cell_value_and_format(col, raw)
            c = ws.cell(row=ri, column=ci, value=val)
            if nf:
                c.number_format = nf
            align = s["align_c"]
            if ci == 2 and not is_total:
                align = s["align_l"]
            _style_cell(c, s, fill=fill, font=font, align=align)

    for ci in range(1, n_cols + 1):
        ws.column_dimensions[get_column_letter(ci)].width = col_widths.get(ci, 16)

    end_row = hdr_row + len(df)
    _auto_fit_row_heights(ws, hdr_row, hdr_row, n_cols)
    ws.row_dimensions[hdr_row].height = max(
        ws.row_dimensions[hdr_row].height or 0.0, 78.0,
    )
    _auto_fit_row_heights(ws, hdr_row + 1, end_row, n_cols)
    return ws


def _write_other_crops_footnote(
    ws,
    df: pd.DataFrame,
    other_names: list[str],
) -> None:
    if not other_names or ws is None:
        return
    note_row = 1 + len(df) + 1
    note_text = "* Иные культуры: " + ", ".join(sorted(other_names))
    ws.merge_cells(
        start_row=note_row, start_column=1,
        end_row=note_row, end_column=len(df.columns),
    )
    cell = ws.cell(row=note_row, column=1, value=note_text)
    cell.font = Font(name="Calibri", size=8, italic=True, color="555555")
    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.row_dimensions[note_row].height = 28.0


# =============================================================================
# ПОСТРОЕНИЕ ОТЧЁТА ПО КУЛЬТУРЕ
# =============================================================================

def other_crop_area_for_firm(
    cache: "HarvestDataCache",
    firm_name: str,
) -> tuple[float, float]:
    """Остаток площади (поля без crop / вне GENERAL_CROP_ORDER) → «Иные культуры»."""
    return cache.area_by_firm_crop.get((firm_name, OTHER_CROP_KEY), (0.0, 0.0))


def _harvest_for_firm_crop(
    crop_name: str,
    work_type_id: int,
    firm_name: str,
    label_date: date,
    field_to_firm_map: dict[int, str],
    field_to_crop_map: dict[int, str],
) -> tuple[float, float]:
    """Валовый сбор по фирме/культуре — теперь из Namolot API, не из Cropwise."""
    return get_namolot_harvest(firm_name, crop_name, label_date)


def firm_crop_row_from_cache(
    cache: "HarvestDataCache",
    firm_name: str,
    crop_name: str,
    work_type_id: int,
    plan_ha: float,
    row_num: int,
    label_date: date,
) -> dict:
    """Одна строка отчёта по фирме/культуре — только lookup cache + agro вес."""
    field_to_firm_map = cache.field_to_firm
    field_to_crop_map = cache.field_to_crop or get_field_to_crop()
    fact_total, fact_today = cache.area_by_firm_crop.get(
        (firm_name, crop_name), (0.0, 0.0),
    )
    harvest_total, harvest_today = _harvest_for_firm_crop(
        crop_name, work_type_id, firm_name, label_date,
        field_to_firm_map, field_to_crop_map,
    )
    share_pct = round(fact_total / plan_ha * 100, 1) if plan_ha else 0.0
    yield_today = round(harvest_today / fact_today, 1) if fact_today else 0.0
    yield_season = round(harvest_total / fact_total, 1) if fact_total else 0.0
    return {
        "№ п/п": row_num,
        "Агрофирма": firm_name,
        "План, га": plan_ha,
        "Факт общий, га": fact_total,
        "Валовый сбор общий, т": harvest_total,
        "Факт за сутки, га": fact_today,
        "Валовый сбор за сутки, т": harvest_today,
        "Доля убранной площади в %": share_pct,
        "Урожайность за сутки (по валовому сбору), т/га": yield_today,
        "Урожайность за сезон (по валовому сбору), т/га": yield_season,
    }


def build_crop_report(
    crop_name:    str,
    work_type_id: int,
    agrofirms:    dict[str, tuple[float, list[int]]],
    report_date:  date,
    cache:        "HarvestDataCache | None" = None,
) -> pd.DataFrame:
    all_plan_ids = set()
    for _, (_, plan_ids) in agrofirms.items():
        all_plan_ids.update(plan_ids)

    ops = _get_ops_for_crop(crop_name, work_type_id)
    if all_plan_ids:
        ops = [o for o in ops if o.get("agri_work_plan_id") in all_plan_ids]

    # DIAGNOSTIC: при установке переменной окружения DIAGNOSE_CROP
    # распечатываем подробную информацию по операциям для указанной культуры.
    try:
        diag_crop = os.environ.get("DIAGNOSE_CROP")
    except Exception:
        diag_crop = None
    if diag_crop and diag_crop == crop_name:
        print(f"[DIAG] crop={crop_name} wt={work_type_id} ops_count={len(ops)}")
        try:
            total_completed = sum(float((o.get("completed_area") or 0)) for o in ops)
            total_harv = sum(float((o.get("harvested_weight") or 0)) for o in ops)
        except Exception:
            total_completed = None
            total_harv = None
        print(f"[DIAG] total_completed_area={total_completed} total_harvested_weight={total_harv}")
        dd = {}
        for o in ops:
            cd = str((o.get("completed_date") or o.get("completed_datetime") or ""))[:10]
            if cd not in dd:
                dd[cd] = 0.0
            dd[cd] += float(o.get("completed_area") or 0)
        print("[DIAG] by_date (date -> sum_completed_area):")
        for k in sorted(dd.keys()):
            print("   ", k, dd[k])
        print("[DIAG] sample ops (id, completed_area, harvested_weight, completed_date, agri_work_plan_id):")
        for o in ops[:50]:
            print("   ", o.get("id"), o.get("completed_area"), o.get("harvested_weight"),
                  str((o.get("completed_date") or o.get("completed_datetime") or ""))[:19], o.get("agri_work_plan_id"))

    df = pd.DataFrame(ops) if ops else pd.DataFrame()
    use_folder_fallback = not all_plan_ids and not df.empty

    if use_folder_fallback:
        field_to_firm_map = cache.field_to_firm if cache else load_field_to_firm()
        df["firm_name"] = df["field_id"].map(
            lambda fid: field_to_firm_map.get(int(fid)) if pd.notna(fid) else None
        )
        field_to_crop_map = get_field_to_crop()
        df["_resolved_crop"] = df["field_id"].map(
            lambda fid: field_to_crop_map.get(int(fid)) if pd.notna(fid) else None
        )
        df = df[df["_resolved_crop"] == crop_name].copy()

    if not df.empty:
        df["completed_area"]    = pd.to_numeric(df["completed_area"],    errors="coerce").fillna(0)
        df["harvested_weight"]  = pd.to_numeric(df["harvested_weight"],  errors="coerce").fillna(0)
        df.loc[df["harvested_weight"] < 0.01, "harvested_weight"] = 0.0
        _ensure_completed_date(df)
        df["agri_work_plan_id"] = df["agri_work_plan_id"].astype("Int64")

    if cache is None and not USE_FAST_MODE:
        cache = prepare_harvest_cache(report_date)

    day_start = cache.day_start if cache else None
    cutoff = cache.cutoff if cache else None
    label_date = cache.label_date if cache else _label_report_date(
        compute_time_range(report_date)[0]
    )
    field_to_firm_map = cache.field_to_firm if cache else load_field_to_firm()
    field_to_crop_map = (
        cache.field_to_crop if cache and cache.field_to_crop else get_field_to_crop()
    )

    tasks_map: dict[int, dict] = {}
    if cache and not USE_FAST_MODE:
        tasks_map = _all_harvest_tasks_map(cache)

    rows = []
    firm_order_with_plans = [
        (firm_name, agrofirms[firm_name])
        for firm_name in AGROFARM_ORDER
        if firm_name in agrofirms
    ]

    # Асинхронный блок: все агрофирмы с plan_ids параллельно
    async def fetch_all_firms() -> dict[str, tuple[float, float]]:
        headers = {"X-User-Api-Token": API_TOKEN}
        semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
        pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
        async with httpx.AsyncClient(headers=headers,
                                      timeout=REQUEST_TIMEOUT,
                                      limits=pool) as client:
            results = await asyncio.gather(*[
                fetch_area_for_firm_async(
                    client, semaphore,
                    plan_ids, work_type_id,
                    day_start, cutoff,
                    tasks_map,
                    firm_name,
                    field_to_firm_map,
                )
                for firm_name, (_, plan_ids) in firm_order_with_plans
                if plan_ids and day_start and cutoff and not USE_FAST_MODE
            ])
        firms_with_plans = [
            firm_name
            for firm_name, (_, plan_ids) in firm_order_with_plans
            if plan_ids and day_start and cutoff and not USE_FAST_MODE
        ]
        return dict(zip(firms_with_plans, results))

    async_results: dict[str, tuple[float, float]] = {}
    use_cache_area = (
        cache
        and cache.area_by_firm_crop
        and day_start
        and cutoff
        and not USE_FAST_MODE
    )
    if not use_cache_area and any(
        plan_ids and day_start and cutoff and not USE_FAST_MODE
        for _, (_, plan_ids) in firm_order_with_plans
    ):
        async_results = asyncio.run(fetch_all_firms())

    for i, (firm_name, (plan_ha, plan_ids)) in \
            enumerate(firm_order_with_plans, start=1):

        # Площадь (га) — как и раньше, из Cropwise.
        if use_cache_area or (cache and day_start and cutoff and not USE_FAST_MODE):
            fact_total, fact_today = _crop_area_from_cache(
                cache, crop_name, work_type_id, firm_name, field_to_crop_map,
            )
        elif plan_ids and day_start and cutoff and not USE_FAST_MODE:
            fact_total, fact_today = async_results.get(firm_name, (0.0, 0.0))
        else:
            if df.empty:
                firm_df = pd.DataFrame(
                    columns=["completed_area", "harvested_weight",
                             "completed_date"])
            elif use_folder_fallback:
                firm_df = df[df["firm_name"] == firm_name]
            elif plan_ids:
                firm_df = df[df["agri_work_plan_id"].isin(plan_ids)]
            else:
                firm_df = pd.DataFrame(
                    columns=["completed_area", "harvested_weight",
                             "completed_date"])
            fact_total, fact_today, _, _ = \
                _harvest_from_agro_df(firm_df, label_date)

        fact_total = _cap_area_to_plan(fact_total, plan_ha)

        # Валовый сбор (т) — из Namolot API.
        harvest_total, harvest_today = get_namolot_harvest(
            firm_name, crop_name, label_date,
        )

        share_pct    = round(fact_total / plan_ha * 100, 1) if plan_ha    else 0.0
        yield_today  = round(harvest_today / fact_today, 1) if fact_today else 0.0
        yield_season = round(harvest_total / fact_total, 1) if fact_total else 0.0

        rows.append({
            "№ п/п":                                           i,
            "Агрофирма":                                       firm_name,
            "План, га":                                        plan_ha,
            "Факт общий, га":                                  fact_total,
            "Валовый сбор общий, т":                           harvest_total,
            "Факт за сутки, га":                                fact_today,
            "Валовый сбор за сутки, т":                        harvest_today,
            "Доля убранной площади в %":                       share_pct,
            "Урожайность за сутки (по валовому сбору), т/га":  yield_today,
            "Урожайность за сезон (по валовому сбору), т/га":  yield_season,
        })

    result = pd.DataFrame(rows, columns=COLUMNS)
    if result.empty:
        return result

    total_plan    = sum(v[0] for v in agrofirms.values())
    total_fact    = result["Факт общий, га"].sum()
    total_harvest = result["Валовый сбор общий, т"].sum()
    total_today   = result["Факт за сутки, га"].sum()
    total_h_today = result["Валовый сбор за сутки, т"].sum()

    result = pd.concat([result, pd.DataFrame([{
        "№ п/п":                                           "",
        "Агрофирма":                                       "Итого",
        "План, га":                                        total_plan,
        "Факт общий, га":                                  round(total_fact, 1),
        "Валовый сбор общий, т":                           round(total_harvest),
        "Факт за сутки, га":                               round(total_today, 2),
        "Валовый сбор за сутки, т":                        round(total_h_today),
        "Доля убранной площади в %":
            round(total_fact / total_plan * 100, 1) if total_plan else 0.0,
        "Урожайность за сутки (по валовому сбору), т/га":
            round(total_h_today / total_today, 1) if total_today else 0.0,
        "Урожайность за сезон (по валовому сбору), т/га":
            round(total_harvest / total_fact, 1) if total_fact else 0.0,
    }])], ignore_index=True)
    return result


# =============================================================================
# ОБЩИЙ НАМОЛОТ
# =============================================================================

# Вспомогательные константы и функция для блока «Иные культуры»
# (аналог 1_main_dop.py/write_summary_sheet)
_GENERAL_CROP_ORDER_SET: set[str] = set(GENERAL_CROP_ORDER)


def _get_crop_name_from_op(op: dict) -> str:
    """Извлекает название культуры из записи agro_operation.

    Для кукурузы название дополнительно уточняется до «на силос» / «на зерно»
    по признаку операции «Скашивание» на поле (см. _resolve_corn_subtype).
    """
    name = ""
    for key in ("crop_name", "crop_title", "culture_name", "culture_title"):
        v = str(op.get(key) or "").strip()
        if v:
            name = v
            break
    if not name:
        return name
    fid = op.get("field_id")
    try:
        fid_i = int(fid) if fid is not None else None
    except (TypeError, ValueError):
        fid_i = None
    return _resolve_corn_subtype(name, fid_i)

def _holding_harvest_from_agro(
    crop_name: str,
    work_type_id: int,
    label_date: date,
    field_to_firm_map: dict[int, str],
    field_to_crop_map: dict[int, str],
) -> tuple[float, float, float, float]:
    """Валовый сбор по культуре суммарно по холдингу — из Namolot API."""
    harvest, harvest_today = get_namolot_harvest_holding(crop_name, label_date)
    return 0.0, 0.0, harvest, harvest_today


def _sum_harvest_entries(*parts: dict | None) -> dict:
    valid = [p for p in parts if p]
    if not valid:
        return {
            "План, га": 0.0,
            "Факт общий, га": 0.0,
            "Валовый сбор общий, т": 0.0,
            "Факт за сутки, га": 0.0,
            "Валовый сбор за сутки, т": 0.0,
        }
    return {
        "План, га": sum(float(p.get("План, га", 0) or 0) for p in valid),
        "Факт общий, га": sum(float(p.get("Факт общий, га", 0) or 0) for p in valid),
        "Валовый сбор общий, т": sum(float(p.get("Валовый сбор общий, т", 0) or 0) for p in valid),
        "Факт за сутки, га": sum(float(p.get("Факт за сутки, га", 0) or 0) for p in valid),
        "Валовый сбор за сутки, т": sum(float(p.get("Валовый сбор за сутки, т", 0) or 0) for p in valid),
    }


def _general_harvest_row(
    row_num: int | str,
    crop_label: str,
    d: dict,
    machines: set[int] | None = None,
) -> dict:
    fact = float(d.get("Факт общий, га", 0) or 0)
    harvest = float(d.get("Валовый сбор общий, т", 0) or 0)
    plan = float(d.get("План, га", 0) or 0)
    fact_today = float(d.get("Факт за сутки, га", 0) or 0)
    harvest_today = float(d.get("Валовый сбор за сутки, т", 0) or 0)
    return {
        "№ п/п": row_num,
        "Культура": crop_label,
        "План, га": plan,
        "Факт общий, га": round(fact, 1),
        "Валовый сбор общий, т": round(harvest),
        "Факт за сутки, га": round(fact_today, 2),
        "Валовый сбор за сутки, т": round(harvest_today),
        "Урожайность общая (по валовому сбору), т/га":
            round(harvest / fact, 1) if fact else 0.0,
        "Доля убранной площади в %":
            round(fact / plan * 100, 1) if plan else 0.0,
        "Средняя выработка, га / комбайн":
            _avg_output_per_combine(fact_today, machines),
    }


def _build_general_harvest_display_rows(
    crop_data: dict[str, dict],
    machines_by_crop: dict[str, set[int]] | None = None,
) -> list[dict]:
    """Строки «Общий намолот»: кукуруза — итог + подстроки зерно/силос (без «Иные культуры»)."""
    machines_by_crop = machines_by_crop or {}
    rows: list[dict] = []
    for crop_name in _GENERAL_CROP_ORDER_NON_CORN:
        if crop_name not in crop_data:
            continue
        rows.append(
            _general_harvest_row(
                _GENERAL_HARVEST_ROW_NUM[crop_name],
                crop_name,
                crop_data[crop_name],
                machines_by_crop.get(crop_name),
            )
        )

    grain = crop_data.get(CORN_GRAIN_CROP)
    silage = crop_data.get(CORN_SILAGE_CROP)
    if grain or silage:
        grain_machines = machines_by_crop.get(CORN_GRAIN_CROP, set())
        silage_machines = machines_by_crop.get(CORN_SILAGE_CROP, set())
        summary = _sum_harvest_entries(grain, silage)
        rows.append(_general_harvest_row(
            CORN_SUMMARY_ROW_NUM, CORN_SUMMARY_LABEL, summary,
            grain_machines | silage_machines,
        ))
        if grain:
            rows.append(_general_harvest_row(
                CORN_GRAIN_ROW_NUM, CORN_GRAIN_CROP, grain, grain_machines,
            ))
        if silage:
            rows.append(_general_harvest_row(
                CORN_SILAGE_ROW_NUM, CORN_SILAGE_CROP, silage, silage_machines,
            ))
    return rows


def build_general_harvest(report_date: date,
                          cache: HarvestDataCache | None = None) -> pd.DataFrame:
    if cache is None and not USE_FAST_MODE:
        cache = prepare_harvest_cache(report_date)
    label_date = (
        cache.label_date if cache
        else _label_report_date(compute_time_range(report_date)[0])
    )
    field_to_firm_map = cache.field_to_firm if cache else load_field_to_firm()
    field_to_crop_map = (
        cache.field_to_crop if cache and cache.field_to_crop else get_field_to_crop()
    )
    crop_data: dict[str, dict] = {}
    for crop_name in GENERAL_CROP_ORDER:
        if crop_name not in CROP_CONFIGS:
            continue
        work_type_id, agrofirms = CROP_CONFIGS[crop_name]
        report = build_crop_report(
            crop_name, work_type_id, agrofirms, report_date, cache,
        )
        if report.empty:
            continue
        data_rows = report[report["Агрофирма"] != "Итого"]
        if data_rows.empty:
            continue
        if cache and not USE_FAST_MODE:
            _, _, harvest, harvest_today = _holding_harvest_from_agro(
                crop_name, work_type_id, label_date,
                field_to_firm_map, field_to_crop_map,
            )
        else:
            harvest = data_rows["Валовый сбор общий, т"].sum()
            harvest_today = data_rows["Валовый сбор за сутки, т"].sum()
        plan_sum = data_rows["План, га"].sum()
        fact_sum = data_rows["Факт общий, га"].sum()
        crop_data[crop_name] = {
            "План, га":                 plan_sum,
            "Факт общий, га":           _cap_area_to_plan(fact_sum, plan_sum),
            "Валовый сбор общий, т":    harvest,
            "Факт за сутки, га":        data_rows["Факт за сутки, га"].sum(),
            "Валовый сбор за сутки, т": harvest_today,
        }

    machines_by_crop = (
        cache.machines_by_crop if cache and not USE_FAST_MODE else {}
    )
    rows = _build_general_harvest_display_rows(crop_data, machines_by_crop)

    result = pd.DataFrame(rows, columns=GENERAL_COLUMNS)
    if result.empty:
        return result

    total_rows = result[~result["Культура"].isin({CORN_GRAIN_CROP, CORN_SILAGE_CROP})]
    th = total_rows["Валовый сбор общий, т"].sum()
    tp = total_rows["План, га"].sum()
    thd = total_rows["Валовый сбор за сутки, т"].sum()
    if cache and not USE_FAST_MODE:
        tf = round(sum(
            _firm_area_from_cache_11_style(cache, f)[0] for f in AGROFARM_ORDER
        ), 1)
        tfd = round(sum(
            _firm_area_from_cache_11_style(cache, f)[1] for f in AGROFARM_ORDER
        ), 2)
    else:
        tf = total_rows["Факт общий, га"].sum()
        tfd = total_rows["Факт за сутки, га"].sum()

    holding_machines: set[int] = set()
    for crop_name in GENERAL_CROP_ORDER:
        holding_machines |= machines_by_crop.get(crop_name, set())

    result = pd.concat([result, pd.DataFrame([{
        "№ п/п":    "", "Культура": "Итого",
        "План, га": tp,
        "Факт общий, га":                                 round(tf, 1),
        "Валовый сбор общий, т":                          round(th),
        "Факт за сутки, га":                              round(tfd, 2),
        "Валовый сбор за сутки, т":                       round(thd),
        "Урожайность общая (по валовому сбору), т/га":
            round(th / tf, 1) if tf else 0.0,
        "Доля убранной площади в %":
            round(tf / tp * 100, 1) if tp else 0.0,
        "Средняя выработка, га / комбайн":
            _avg_output_per_combine(tfd, holding_machines),
    }])], ignore_index=True)
    return result


# =============================================================================
# ИНЫЕ КУЛЬТУРЫ (всё, что не входит в GENERAL_CROP_ORDER)
# Аналог блока «Иные культуры» из 1_main_dop.py/write_summary_sheet
# =============================================================================

def build_other_crops_harvest(
    report_date: date,
    cache: "HarvestDataCache | None" = None,
) -> pd.DataFrame:
    """
    Культуры вне GENERAL_CROP_ORDER. Площадь — mtfmi; вес — agro_operations.
    """
    if cache is None and not USE_FAST_MODE:
        cache = prepare_harvest_cache(report_date)

    if cache and not USE_FAST_MODE:
        field_to_crop_map = get_field_to_crop()
        other_names = sorted({
            c for c in set(field_to_crop_map.values())
            if c and c not in set(GENERAL_CROP_ORDER)
            and c not in VEGETABLE_FIELD_CROPS
        })
        rows = []
        for i, crop_name in enumerate(other_names, start=1):
            fact_total = fact_today = 0.0
            for firm_name in AGROFARM_ORDER:
                ft, fd = _crop_area_from_cache(
                    cache, crop_name, 0, firm_name, field_to_crop_map,
                )
                fact_total += ft
                fact_today += fd
            if fact_total <= 0 and fact_today <= 0:
                continue
            rows.append({
                "№ п/п": i,
                "Культура": crop_name,
                "План, га": 0.0,
                "Факт общий, га": round(fact_total, 1),
                "Валовый сбор общий, т": 0,
                "Факт за сутки, га": round(fact_today, 2),
                "Валовый сбор за сутки, т": 0,
                "Урожайность общая (по валовому сбору), т/га": 0.0,
                "Доля убранной площади в %": 0.0,
                "Средняя выработка, га / комбайн": 0.0,
            })
        if not rows:
            return pd.DataFrame(columns=GENERAL_COLUMNS)
        result = pd.DataFrame(rows, columns=GENERAL_COLUMNS)
        tf = result["Факт общий, га"].sum()
        tfd = result["Факт за сутки, га"].sum()
        result = pd.concat([result, pd.DataFrame([{
            "№ п/п": "", "Культура": "Итого",
            "План, га": 0.0,
            "Факт общий, га": round(tf, 1),
            "Валовый сбор общий, т": 0,
            "Факт за сутки, га": round(tfd, 2),
            "Валовый сбор за сутки, т": 0,
            "Урожайность общая (по валовому сбору), т/га": 0.0,
            "Доля убранной площади в %": 0.0,
            "Средняя выработка, га / комбайн": 0.0,
        }])], ignore_index=True)
        return result

    report_date_str = report_date.strftime("%Y-%m-%d")
    day_start_str   = (report_date - timedelta(days=1)).strftime("%Y-%m-%d") \
                      if DAY_START_HOUR == DAY_END_HOUR \
                      else report_date.strftime("%Y-%m-%d")

    # Загружаем операции по всем wt_id уборки (кэш уже заполнен основным циклом)
    all_ops: list[dict] = []
    for wt_id in GRAIN_HARVEST_WT_IDS:
        all_ops.extend(get_agro_operations(wt_id))

    if not all_ops:
        return pd.DataFrame(columns=GENERAL_COLUMNS)

    df = pd.DataFrame(all_ops)

    # Добавляем folder_id (уже кэшировано после основного цикла)
    all_fields   = get_cached_fields()
    all_fgs      = get_cached_field_groups()
    field_to_fg  = {f["id"]: f.get("field_group_id") for f in all_fields}
    fg_to_folder = {fg["id"]: fg.get("group_folder_id") for fg in all_fgs}
    df["folder_id"] = df["field_id"].map(
        lambda fid: fg_to_folder.get(field_to_fg.get(fid))
        if pd.notna(fid) else None
    )

    df["completed_area"]   = pd.to_numeric(df.get("completed_area"),   errors="coerce").fillna(0)
    df["harvested_weight"] = pd.to_numeric(df.get("harvested_weight"), errors="coerce").fillna(0)
    df.loc[df["harvested_weight"] < 0.01, "harvested_weight"] = 0.0
    _ensure_completed_date(df)

    # Определяем crop_name для каждой операции
    df["_crop"] = df.apply(_get_crop_name_from_op, axis=1)

    # Оставляем только строки с культурами вне GENERAL_CROP_ORDER
    df_other = df[
        df["_crop"].notna()
        & (df["_crop"] != "")
        & ~df["_crop"].isin(_GENERAL_CROP_ORDER_SET)
        & ~df["_crop"].isin(VEGETABLE_FIELD_CROPS)
    ]

    if df_other.empty:
        return pd.DataFrame(columns=GENERAL_COLUMNS)

    date_col = df_other["completed_date"].astype(str).str[:10]
    df_cumul = df_other[date_col <= report_date_str]
    df_today = df_other[date_col == day_start_str]

    # Группируем по культуре
    fact_by_crop:    dict[str, float] = defaultdict(float)
    harvest_by_crop: dict[str, float] = defaultdict(float)
    today_by_crop:   dict[str, float] = defaultdict(float)
    h_today_by_crop: dict[str, float] = defaultdict(float)

    for _, row in df_cumul.iterrows():
        crop = row["_crop"]
        fact_by_crop[crop]    += float(row["completed_area"])
        harvest_by_crop[crop] += float(row["harvested_weight"])

    for _, row in df_today.iterrows():
        crop = row["_crop"]
        today_by_crop[crop]   += float(row["completed_area"])
        h_today_by_crop[crop] += float(row["harvested_weight"])

    # Строим строки df
    rows = []
    for i, crop in enumerate(sorted(fact_by_crop.keys()), start=1):
        fact    = round(fact_by_crop[crop], 1)
        harvest = round(harvest_by_crop[crop])
        today   = round(today_by_crop.get(crop, 0.0), 2)
        h_today = round(h_today_by_crop.get(crop, 0.0))
        rows.append({
            "№ п/п":                                          i,
            "Культура":                                       crop,
            "План, га":                                       0.0,
            "Факт общий, га":                                 fact,
            "Валовый сбор общий, т":                          harvest,
            "Факт за сутки, га":                               today,
            "Валовый сбор за сутки, т":                       h_today,
            "Урожайность общая (по валовому сбору), т/га":
                round(harvest / fact, 1) if fact else 0.0,
            "Доля убранной площади в %":                      0.0,
            "Средняя выработка, га / комбайн":                0.0,
        })

    if not rows:
        return pd.DataFrame(columns=GENERAL_COLUMNS)

    result = pd.DataFrame(rows, columns=GENERAL_COLUMNS)

    tf  = result["Факт общий, га"].sum()
    th  = result["Валовый сбор общий, т"].sum()
    tfd = result["Факт за сутки, га"].sum()
    thd = result["Валовый сбор за сутки, т"].sum()

    result = pd.concat([result, pd.DataFrame([{
        "№ п/п": "", "Культура": "Итого",
        "План, га":                                       0.0,
        "Факт общий, га":                                 round(tf, 1),
        "Валовый сбор общий, т":                          round(th),
        "Факт за сутки, га":                              round(tfd, 2),
        "Валовый сбор за сутки, т":                       round(thd),
        "Урожайность общая (по валовому сбору), т/га":
            round(th / tf, 1) if tf else 0.0,
        "Доля убранной площади в %":                      0.0,
        "Средняя выработка, га / комбайн":                0.0,
    }])], ignore_index=True)
    return result


# =============================================================================
# СВОДНАЯ ИНФОРМАЦИЯ ПО УБОРКЕ
# =============================================================================

def _summary_from_general(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=HOLDING_SUMMARY_COLUMNS)
    rows = []
    for _, r in df.iterrows():
        plan       = r["План, га"]
        fact       = r["Факт общий, га"]
        harvest    = r["Валовый сбор общий, т"]
        fact_today = r["Факт за сутки, га"]
        h_today    = r["Валовый сбор за сутки, т"]
        rows.append({
            "№ п/п":                    r.get("№ п/п", ""),
            "Культура":                 r.get("Культура", r.get("Агрофирма", "")),
            "Посеянная площадь, га":    plan,
            "Убранная площадь, га":   round(fact, 1),
            "Остаток к уборке, га":     round(plan - fact, 1) if plan else 0.0,
            "Осталось убрать, %":
                round((plan - fact) / plan * 100, 2) if plan else 0.0,
            "Валовой сбор, т":          harvest,
            "Урожайность за сезон, т/га":
                round(harvest / fact, 1) if fact else 0.0,
            "Убрано за сутки, га":    round(fact_today, 2),
            "Валовый сбор за сутки, т": round(h_today),
            "Урожайность за сутки, т/га":
                round(h_today / fact_today, 1) if fact_today else 0.0,
        })
    return pd.DataFrame(rows, columns=HOLDING_SUMMARY_COLUMNS)


def build_holding_summary_grain(report_date: date,
                                cache: HarvestDataCache | None = None) -> pd.DataFrame:
    return _summary_from_general(build_general_harvest(report_date, cache))


# =============================================================================
# ОВОЩНЫЕ КУЛЬТУРЫ
# =============================================================================

def _normalize_veg_crop_name(crop: str | None) -> str | None:
    if not crop:
        return None
    if crop in _VEG_HARVEST_CROPS:
        return crop
    if crop == "Свекла":
        return "Свекла столовая"
    low = crop.lower()
    for key in _VEG_HARVEST_CROPS:
        if low.startswith(key.lower()):
            return key
    return None


_net_weight_cache: dict[date, dict[str, float]] = {}


def _net_weight_auth_header() -> dict:
    if not (NET_WEIGHT_USERNAME and NET_WEIGHT_PASSWORD):
        return {}
    creds = f"{NET_WEIGHT_USERNAME}:{NET_WEIGHT_PASSWORD}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(creds).decode("ascii")}


def _format_net_weight_param(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def _net_weight_request_timeout() -> tuple[float, float]:
    try:
        connect = max(
            1.0,
            float((os.environ.get("NET_WEIGHT_CONNECT_TIMEOUT_SEC") or "10").strip()),
        )
    except ValueError:
        connect = 10.0
    try:
        read = max(
            1.0,
            float(
                (os.environ.get("NET_WEIGHT_READ_TIMEOUT_SEC")
                 or os.environ.get("NET_WEIGHT_TIMEOUT_SEC")
                 or "30").strip()
            ),
        )
    except ValueError:
        read = 30.0
    return connect, read


def fetch_net_weight_records(
    start_date: date,
    end_date: date,
) -> list[dict]:
    """Сырые записи NetWeight/GetWeight за период [start_date, end_date]."""
    params = {
        "StartDate": _format_net_weight_param(start_date),
        "EndDate": _format_net_weight_param(end_date),
    }
    try:
        resp = requests.get(
            NET_WEIGHT_BASE_URL,
            params=params,
            headers=_net_weight_auth_header(),
            timeout=_net_weight_request_timeout(),
            proxies={"http": None, "https": None},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(
            f"  [net_weight] ошибка запроса {params['StartDate']}..{params['EndDate']}: {e}",
            flush=True,
        )
        return []
    if not isinstance(data, list):
        print(f"  [net_weight] неожиданный формат ответа: {type(data)}", flush=True)
        return []
    return data


def get_net_weight_by_crop(label_date: date) -> dict[str, float]:
    """Зачётный вес за сезон (т) по культурам овощного отчёта."""
    if label_date in _net_weight_cache:
        return _net_weight_cache[label_date]

    period_start = VEG_SEASON_START
    records = fetch_net_weight_records(period_start, label_date)
    by_crop: dict[str, float] = defaultdict(float)
    unmatched: Counter = Counter()
    for r in records:
        crop = _normalize_veg_crop_name(r.get("Product"))
        if not crop:
            unmatched[str(r.get("Product") or "")] += 1
            continue
        weight_t = float(r.get("Weight") or 0) * NET_WEIGHT_TO_TONNES
        if weight_t <= 0:
            continue
        by_crop[crop] += weight_t

    if unmatched:
        print(
            f"  [net_weight] ВНИМАНИЕ: не распознан Product (записей): "
            f"{dict(unmatched.most_common(20))}",
            flush=True,
        )
    print(
        f"  [net_weight] {SVODKA10_NET_WEIGHT} сезон {period_start}..{label_date}, "
        f"культур: {len(by_crop)}, записей: {len(records)}",
        flush=True,
    )
    result = {crop: round(weight) for crop, weight in by_crop.items()}
    _net_weight_cache[label_date] = result
    return result


def _veg_mtfmi_cov(item: dict, hourly_cutoff: datetime) -> float:
    """Площадь по mtfmi до cutoff смены — как collect_work_data в сводке 2."""
    hourly = item.get("covered_area_hourly") or []
    cov = 0.0
    for entry in hourly:
        h = parse_dt(entry[0])
        if h and h < hourly_cutoff:
            cov += float(entry[1] or 0)
    if not hourly:
        cov = float(item.get("covered_area") or 0)
    return cov


def _veg_shift_info(
    end_time_raw,
    report_date: date,
) -> tuple[date, datetime, datetime] | None:
    """Дата смены и окно [day_start, cutoff) — как 2_main_ideal_compact.py."""
    end_dt = parse_dt(end_time_raw)
    if end_dt is None:
        return None
    cal_date = end_dt.date()
    cutoff = datetime(
        cal_date.year, cal_date.month, cal_date.day,
        DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    if end_dt < cutoff:
        cal_date = cal_date - timedelta(days=1)
        cutoff = datetime(
            cal_date.year, cal_date.month, cal_date.day,
            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
        ) + timedelta(days=1)
    else:
        cutoff = cutoff + timedelta(days=1)
    day_start = cutoff - timedelta(days=1)
    if not (VEG_SEASON_START <= cal_date < report_date):
        return None
    return cal_date, day_start, cutoff


# Кол-во уникальных машин за смену на уборке овощей (5.4 Уборка картофеля,
# лука) — заполняется build_vegetables_report() как побочный результат,
# читается get_vegetables_machines_today_by_firm() для проверки по машинам
# (proverka/extractors.py:_extract_10). Сигнатуру/возврат build_vegetables_
# report трогать не стали, чтобы не задеть build_holding_summary_vegetables
# и остальных вызывающих.
_VEG_MACHINES_TODAY_BY_FIRM: dict[str, int] = {}


def get_vegetables_machines_today_by_firm() -> dict[str, int]:
    """Число уникальных machine_id за отчётные сутки по фирмам (сейчас — по
    единственной овощной фирме _VEG_HARVEST_FIRM). Актуально после вызова
    build_vegetables_report() для той же даты."""
    return dict(_VEG_MACHINES_TODAY_BY_FIRM)


def build_vegetables_report(report_date: date) -> pd.DataFrame:
    global _VEG_MACHINES_TODAY_BY_FIRM
    label_date = _label_report_date(compute_time_range(report_date)[0])
    report_shift_date = report_date - timedelta(days=1)
    veg_machines_today: set[int] = set()
    veg_season_start = datetime(
        VEG_SEASON_START.year, VEG_SEASON_START.month, VEG_SEASON_START.day,
        DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    cutoff_dt = datetime(
        report_date.year, report_date.month, report_date.day,
        DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    ) + timedelta(days=1)
    cutoff_dt_ext = cutoff_dt + timedelta(hours=CUTOFF_BUFFER_HOURS)

    print(
        f"    [VEG] {SVODKA10_VEG_FIX} wt={VEGETABLE_WORK_TYPE_IDS} "
        f"сезон с {VEG_SEASON_START} фирма={_VEG_HARVEST_FIRM}",
        flush=True,
    )
    net_weight_by_crop = get_net_weight_by_crop(label_date)
    from machine_tasks_batch_fetch import fetch_by_work_type_ids_sync

    all_tasks = fetch_by_work_type_ids_sync(
        get_all_from_id,
        "machine_tasks",
        VEGETABLE_WORK_TYPE_IDS,
        {
            "end_time_gt_eq": veg_season_start.strftime("%Y-%m-%dT%H:%M:%S"),
            "end_time_lt":    cutoff_dt_ext.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        log_prefix="[r10 VEG] ",
    )
    print(f"    [VEG] заданий: {len(all_tasks)}", flush=True)

    tasks_in_period: list[dict] = []
    no_shift_tasks: list[dict] = []
    for t in all_tasks:
        shift = _veg_shift_info(t.get("end_time"), report_date)
        if shift is not None:
            tasks_in_period.append(t)
        elif t.get("end_time") in (None, ""):
            no_shift_tasks.append(t)

    all_tasks_for_mtfmi = tasks_in_period + no_shift_tasks
    if not all_tasks_for_mtfmi:
        _VEG_MACHINES_TODAY_BY_FIRM = {_VEG_HARVEST_FIRM: 0}
        print("    [VEG] нет заданий в периоде", flush=True)
        rows_empty = []
        for i, (crop_name, config) in enumerate(VEGETABLE_CONFIG.items(), start=1):
            plan_ha = config[0]
            harvest_total, harvest_today = get_namolot_harvest(
                _VEG_HARVEST_FIRM, crop_name, label_date,
            )
            entry = {
                "№ п/п": i, "Культура": crop_name, "План, га": plan_ha,
                "Факт общий, га": 0.0, "Факт за сутки, га": 0.0,
                "Убранная площадь в %": 0.0,
                "Валовый сбор общий, т": harvest_total,
                "Валовый сбор за сутки, т": harvest_today,
                "Зачетный вес за сезон, т": net_weight_by_crop.get(crop_name, 0),
                "Урожайность за сезон (по валовому сбору), т/га": 0.0,
            }
            rows_empty.append(entry)
        result = pd.DataFrame(rows_empty, columns=VEG_COLUMNS)
        tp = result["План, га"].sum()
        th = result["Валовый сбор общий, т"].sum()
        tz = result["Зачетный вес за сезон, т"].sum()
        result = pd.concat([result, pd.DataFrame([{
            "№ п/п": "", "Культура": "Итого",
            "План, га": tp, "Факт общий, га": 0.0, "Факт за сутки, га": 0.0,
            "Убранная площадь в %": 0.0,
            "Валовый сбор общий, т": round(th), "Валовый сбор за сутки, т": 0.0,
            "Зачетный вес за сезон, т": round(tz),
            "Урожайность за сезон (по валовому сбору), т/га": 0.0,
        }])], ignore_index=True)
        return result

    task_ids = [t["id"] for t in all_tasks_for_mtfmi]
    headers  = {"X-User-Api-Token": API_TOKEN}

    async def _fetch_all_mtfmi() -> dict[int, list[dict]]:
        semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
        pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
        async with httpx.AsyncClient(headers=headers, timeout=REQUEST_TIMEOUT,
                                      limits=pool) as client:
            return await fetch_mtfmi_batch(client, task_ids, semaphore)

    mtfmi_map = asyncio.run(_fetch_all_mtfmi())
    print(f"    [VEG] mtfmi загружено для {len(mtfmi_map)} заданий", flush=True)

    field_to_crop_map = get_field_to_crop()
    field_to_firm_map = load_field_to_firm()
    agg: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    _note = None
    try:
        from proverka.task_facts import bind_capture

        _bound = bind_capture()
        _note = _bound[0] if _bound else None
    except Exception:
        _note = None

    for task in tasks_in_period:
        tid = task["id"]
        shift = _veg_shift_info(task.get("end_time"), report_date)
        if not shift:
            continue
        t_date, _day_start, hourly_cutoff = shift
        for item in mtfmi_map.get(tid, []):
            fid = item.get("field_id")
            if not fid:
                continue
            fid_i = int(fid)
            if field_to_firm_map.get(fid_i) != _VEG_HARVEST_FIRM:
                continue
            crop = _normalize_veg_crop_name(field_to_crop_map.get(fid_i))
            if not crop:
                continue
            cov = _veg_mtfmi_cov(item, hourly_cutoff)
            if cov <= _VEG_MIN_AREA_HA:
                continue
            agg[crop][t_date] += cov
            if t_date == report_shift_date and task.get("machine_id") is not None:
                veg_machines_today.add(task["machine_id"])
            if _note is not None and t_date == report_shift_date:
                _mid = task.get("machine_id")
                _note(
                    group="5.4 Уборка картофеля, лука",
                    firm=_VEG_HARVEST_FIRM,
                    crop=crop,
                    field_id=fid_i,
                    task_id=int(tid),
                    machine_id=int(_mid) if _mid is not None else None,
                    ha=cov,
                    season=str(task.get("season") or ""),
                )

    for task in no_shift_tasks:
        tid = task["id"]
        t_date = report_date
        for item in mtfmi_map.get(tid, []):
            fid = item.get("field_id")
            if not fid:
                continue
            fid_i = int(fid)
            if field_to_firm_map.get(fid_i) != _VEG_HARVEST_FIRM:
                continue
            crop = _normalize_veg_crop_name(field_to_crop_map.get(fid_i))
            if not crop:
                continue
            cov = float(item.get("covered_area") or 0)
            if cov <= _VEG_MIN_AREA_HA:
                continue
            agg[crop][t_date] += cov

    _VEG_MACHINES_TODAY_BY_FIRM = {_VEG_HARVEST_FIRM: len(veg_machines_today)}

    crop_area: dict[str, dict[str, float]] = {}
    for crop_name in VEGETABLE_CONFIG:
        by_date = agg.get(crop_name, {})
        crop_area[crop_name] = {
            "total": round(sum(v for d, v in by_date.items() if d < report_date), 1),
            "today": round(by_date.get(report_shift_date, 0.0), 1),
        }

    rows = []
    for i, (crop_name, config) in enumerate(VEGETABLE_CONFIG.items(), start=1):
        plan_ha = config[0]
        harvest_total, harvest_today = get_namolot_harvest(
            _VEG_HARVEST_FIRM, crop_name, label_date,
        )
        fact_manual    = config[4] if len(config) >= 5 else 0.0
        fact_today_man = config[5] if len(config) >= 6 else 0.0

        area_entry = crop_area.get(crop_name)
        fact_total = area_entry["total"] if area_entry else 0.0
        fact_today = area_entry["today"] if area_entry else 0.0

        if fact_total == 0 and fact_manual:
            fact_total = fact_manual
        if fact_today == 0 and fact_today_man:
            fact_today = fact_today_man

        share      = round(fact_total / plan_ha * 100, 1) if plan_ha else 0.0
        yld_season = round(harvest_total / fact_total, 2) if fact_total else 0.0

        rows.append({
            "№ п/п":                                          i,
            "Культура":                                       crop_name,
            "План, га":                                       plan_ha,
            "Факт общий, га":                                 fact_total,
            "Факт за сутки, га":                               fact_today,
            "Убранная площадь в %":                           share,
            "Валовый сбор общий, т":                          harvest_total,
            "Валовый сбор за сутки, т":                       harvest_today,
            "Зачетный вес за сезон, т":                       net_weight_by_crop.get(crop_name, 0),
            "Урожайность за сезон (по валовому сбору), т/га": yld_season,
        })

    result = pd.DataFrame(rows, columns=VEG_COLUMNS)

    tp  = result["План, га"].sum()
    tf  = result["Факт общий, га"].sum()
    th  = result["Валовый сбор общий, т"].sum()
    tfd = result["Факт за сутки, га"].sum()
    thd = result["Валовый сбор за сутки, т"].sum()
    tz  = result["Зачетный вес за сезон, т"].sum()

    result = pd.concat([result, pd.DataFrame([{
        "№ п/п": "", "Культура": "Итого",
        "План, га":                 tp,
        "Факт общий, га":           round(tf, 1),
        "Факт за сутки, га":        round(tfd, 2),
        "Убранная площадь в %":
            round(tf / tp * 100, 1) if tp else 0.0,
        "Валовый сбор общий, т":    round(th),
        "Валовый сбор за сутки, т": round(thd),
        "Зачетный вес за сезон, т": round(tz),
        "Урожайность за сезон (по валовому сбору), т/га":
            round(th / tf, 2) if tf else 0.0,
    }])], ignore_index=True)
    return result


def build_holding_summary_vegetables(report_date: date) -> pd.DataFrame:
    df = build_vegetables_report(report_date)
    if df.empty:
        return pd.DataFrame(columns=HOLDING_SUMMARY_COLUMNS)
    df_renamed = df.rename(columns={
        "Культура":               "Культура",
        "Факт общий, га":         "Факт общий, га",
        "Факт за сутки, га":      "Факт за сутки, га",
        "Валовый сбор общий, т":  "Валовый сбор общий, т",
        "Валовый сбор за сутки, т": "Валовый сбор за сутки, т",
    })
    return _summary_from_general(df_renamed)


# =============================================================================
# РЕЖИМ --discover
# =============================================================================

def run_discover_plans() -> None:
    folder_names = {3: "АФЧ", 4: "АР", 5: "Аксу",
                    6: "ДРА", 7: "Актай", 9: "ЛПФ", 11: "Нурлат"}
    all_fields   = get_cached_fields()
    all_fgs      = get_cached_field_groups()
    field_to_fg  = {f["id"]: f.get("field_group_id") for f in all_fields}
    fg_to_folder = {fg["id"]: fg.get("group_folder_id") for fg in all_fgs}

    for wt_id, wt_name in [
        (118,                    "кукуруза на зерно"),
        (126,                    "зерновые"),
        (VEGETABLE_WORK_TYPE,    "овощи"),
        *[(wt, "кукуруза на силос (Скашивание)") for wt in sorted(SILAGE_CORN_MOWING_WT_IDS)],
    ]:
        print(f"\n{'='*60}\nwork_type_id={wt_id} ({wt_name})\n{'='*60}")
        ops = get_agro_operations(wt_id)
        if not ops:
            print("  Нет операций.")
            continue

        plan_area:   dict = defaultdict(float)
        plan_weight: dict = defaultdict(float)
        plan_folder: dict = {}
        plan_months: dict = defaultdict(list)

        for o in ops:
            pid = o.get("agri_work_plan_id")
            if not pid:
                continue
            plan_area[pid]   += o.get("completed_area") or 0
            w = o.get("harvested_weight") or 0
            plan_weight[pid] += w if w > 0.01 else 0
            fid    = o.get("field_id")
            folder = fg_to_folder.get(field_to_fg.get(fid))
            if folder:
                plan_folder[pid] = folder
            d = (o.get("completed_date") or
                 o.get("completed_datetime") or "")[:7]
            if d:
                plan_months[pid].append(d)

        plan_to_crop = (
            _build_plan_to_vegetable_crop(set(plan_folder))
            if wt_name == "овощи" else {}
        )
        for folder_id, firm in folder_names.items():
            plans = [
                (pid, plan_area[pid], plan_weight[pid],
                 dict(Counter(plan_months[pid])))
                for pid, fld in plan_folder.items()
                if fld == folder_id
            ]
            if not plans:
                continue
            print(f"\n{firm} (folder={folder_id}):")
            for pid, area, weight, months in sorted(plans):
                crop_str = (f"  → {plan_to_crop[pid]}"
                             if plan_to_crop.get(pid) else "")
                print(f"  plan={pid:>5}  area={area:>7.0f}га  "
                      f"вес={weight:>7.0f}т  месяцы={months}{crop_str}")


# =============================================================================
# ЗАПУСК
# =============================================================================

if __name__ == "__main__":
    if "--discover" in sys.argv:
        run_discover_plans()
        sys.exit(0)

    if USE_FAST_MODE:
        print("(режим --fast: completed_area из agro_operations, без mtfmi)\n")

    day_start, cutoff = compute_time_range(REPORT_DATE)
    preload_wt_ids = _harvest_preload_wt_ids()
    preload_agro_operations(preload_wt_ids, SEASON_YEAR)

    if USE_FAST_MODE:
        ensure_report_fields_loaded_sync(
            _field_ids_from_preloaded_agro_ops(preload_wt_ids),
        )
        cache = None
    else:
        cache = prepare_harvest_cache(REPORT_DATE)

    label_date = cache.label_date if cache else _label_report_date(day_start)

    print(f"Сводный отчёт по уборке культур — платформа {REPORT_DATE}")
    print(f"Сутки {label_date.strftime('%d.%m.%Y')} | диапазон: {day_start} — {cutoff}\n")
    print("Загружаем данные...", flush=True)

    output_file = f"crop_report_holding_{REPORT_DATE}.xlsx"

    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    # 1. Общий намолот
    print("  Общий намолот...", flush=True)
    general = build_general_harvest(REPORT_DATE, cache)
    ws_general = None
    if not general.empty:
        ws_general = _write_formatted_harvest_sheet(
            wb, "Общий намолот", general, _WIDTHS_GENERAL,
        )
        print("  Лист: Общий намолот")

    # 2. Овощи
    print("  Овощи...", flush=True)
    vegetables = build_vegetables_report(REPORT_DATE)
    if not vegetables.empty:
        _write_formatted_harvest_sheet(wb, "Овощи", vegetables, _WIDTHS_VEG)
        print("  Лист: Овощи")

    # 3. По культурам (порядок = стр. 3+ в PDF, без «Пшеница яровая»)
    print(f"  [10] crop sheets {SVODKA10_CROP_FIX}: {_REPORT10_CROP_SHEET_ORDER}", flush=True)
    for crop_name in _REPORT10_CROP_SHEET_ORDER:
        cfg = CROP_CONFIGS.get(crop_name)
        if not cfg:
            continue
        work_type_id, agrofirms = cfg
        print(f"  {crop_name}...", flush=True)
        report = build_crop_report(
            crop_name, work_type_id, agrofirms, REPORT_DATE, cache,
        )
        if not report.empty:
            _write_formatted_harvest_sheet(
                wb, crop_name, report, _WIDTHS_CROP_FIRM,
            )
            print(f"  Лист: {_safe_sheet_name(crop_name)} ({len(report)} строк)")

    wb.save(output_file)
    print(f"\nСохранено: {output_file}")