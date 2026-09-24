"""
Отчёт «ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ»
=========================================
Для каждой агрофирмы — отдельный лист Excel с:
  - СОБСТВЕННЫЕ: комбайны (machine_group 5/9), площадь, время, выработка — из API
  - НАЕМНЫЕ: та же структура; техника machine_group_id == HIRED_GROUP_ID (7)
    с заданиями уборки на полях фирмы. Если наёмных нет — секция не выводится
    (без пустых строк-заглушек).

СОБСТВЕННЫЕ / НАЕМНЫЕ — данные из API (machine_task_field_mapping_items, machines).
Использует: machines, machine_tasks, machine_task_field_mapping_items,
field_groups, machine_region_mapping_items.

Доп. лист «Уборка, овощи» — по той же структуре (СОБСТВЕННЫЕ/НАЕМНЫЕ), но только
для группы работ «5.4 Уборка картофеля, лука» (из WORK_GROUPS в plans) и только
для АФ Чистопольская — единственной агрофирмы, где выращиваются овощи.
"""

import asyncio
import base64
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx
import pandas as pd
import requests

from plans import (
    AGROFIRMS_META,
    AVAILABLE_OWN,
    FIELD_NAME_FORCE_RULES,
    FIRM_TO_FOLDER,
    HIRED_GROUP_ID,
    PLAN_RATE_PER_GROUP,
    PREFERRED_CROP_ORDER,
    SKIP_CROPS,
    TZ_MSK,
    WORK_GROUPS,
)
from task_area_shared import (
    CUTOFF_BUFFER_HOURS,
    HARVESTER_GROUP_IDS,
    SILAGE_CORN_MOWING_WT_IDS,
    harvest_item_area_for_shift,
)

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

API_TOKEN = "6DayNcBDr9BWEZgst1Dy"
BASE_URL = "https://operations.cropwise.com/api/v3"
API_TOKEN = (
    (os.environ.get("CROPWISE_API_TOKEN") or "").strip()
    or (os.environ.get("API_TOKEN") or "").strip()
    or API_TOKEN
)
BASE_URL = (os.environ.get("CROPWISE_BASE_URL") or "").strip() or BASE_URL
REPORT_DATE = date(2026, 9, 14)
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

# Плановая выработка (га/сутки на 1 ед. техники) — берётся из plans.PLAN_RATE_PER_GROUP
# по ключу группы работ, а не хранится здесь отдельной константой.
SUMMARY_WORK_GROUP = "5.0 Уборка"  # группа работ для листа «Сводка» (зерновые/масличные)
PLANNED_OUTPUT: float = PLAN_RATE_PER_GROUP[SUMMARY_WORK_GROUP]

# Отчёт считается не по календарным суткам, а по смене:
# [REPORT_DATE-1 06:00 .. REPORT_DATE 06:00) — например, для REPORT_DATE=2026-07-17
# это данные с 2026-07-16 06:00 по 2026-07-17 05:59.
DAY_START_HOUR: int = 6

try:
    CUTOFF_BUFFER_HOURS: int = int(
        (os.environ.get("SVODKI_REPORT10_CUTOFF_BUFFER_HOURS") or str(CUTOFF_BUFFER_HOURS)).strip()
    )
except ValueError:
    pass


def _shift_datetimes(
    report_date: date, day_start_hour: int = DAY_START_HOUR,
) -> tuple[datetime, datetime]:
    """Смена [report_date-1 day_start_hour .. report_date day_start_hour) в TZ_MSK."""
    cutoff_dt = datetime(
        report_date.year, report_date.month, report_date.day,
        day_start_hour, 0, 0, tzinfo=TZ_MSK,
    )
    day_start_dt = cutoff_dt - timedelta(days=1)
    return day_start_dt, cutoff_dt


def _shift_window(report_date: date, day_start_hour: int = DAY_START_HOUR) -> tuple[str, str]:
    """Возвращает (day_start_str, cutoff_str) для смены report_date."""
    day_start_dt, cutoff_dt = _shift_datetimes(report_date, day_start_hour)
    return day_start_dt.strftime("%Y-%m-%dT%H:%M:%S"), cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            from datetime import timezone
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_MSK)
    return dt.astimezone(TZ_MSK)


def _normalize_fmi_for_shift(
    fmi_list: list[dict],
    report_date: date,
    task_wt: dict[int, int] | None = None,
) -> list[dict]:
    """Оставляет FMI с площадью за смену; covered_area — как 10/11."""
    day_start_dt, cutoff_dt = _shift_datetimes(report_date)
    out: list[dict] = []
    for row in fmi_list:
        tid = row.get("machine_task_id")
        wt = task_wt.get(int(tid)) if task_wt and tid is not None else None
        area = harvest_item_area_for_shift(
            row, cutoff_dt,
            day_start=day_start_dt,
            work_type_id=wt,
            parse_dt=_parse_dt,
        )
        if area <= 0:
            continue
        new_row = dict(row)
        new_row["covered_area"] = area
        out.append(new_row)
    return out

# Агрофирма (внутреннее имя) → (group_folder_id, machine_region_id)
# group_folder_id — для полей, machine_region_id — для комбайнов в наличии
# Берётся из plans.AGROFIRMS_META (folder_id, region_id, ...) — первые два элемента,
# чтобы не дублировать эти ID ещё раз в этом файле.
AGROFIRMS: dict[str, tuple[int, int]] = {
    firm: (ids[0], ids[1]) for firm, ids in AGROFIRMS_META.items()
}

# Отображаемое название для заголовков (fallback, если API не вернёт name)
# Приоритет: group_folders.name из API → этот словарь
FIRM_DISPLAY_NAMES: dict[str, str] = {
    "АФ Чистопольская": 'ООО АФ "ЧИСТОПОЛЬСКАЯ"',
    "АгроРаздолье": 'ООО "АГРОРАЗДОЛЬЕ"',
    "Аксу-Агро": 'ООО "АКСУ АГРО"',
    "НурлатАгро": 'ООО "НУРЛАТ АГРО"',
    "Дрожжаное Рассвет Агро": 'ООО "ДРОЖЖАНОЕ РАССВЕТ АГРО"',
    "Актай": 'ООО "АКТАЙ"',
    "Лениногорская Птицефабрика": 'ООО "ЛЕНИНОГОРСКАЯ ПТИЦЕФАБРИКА"',
}

WORK_TYPE_DISPLAY = "5.0 Уборка"

# Лист «Уборка, овощи» (картофель, лук) — по аналогии с основным листом уборки,
# но только для группы работ 5.4 из plans и только для АФ Чистопольская
# (единственная агрофирма, где выращиваются овощи).
VEG_HARVEST_GROUP_NAME = "5.4 Уборка картофеля, лука"
VEG_HARVEST_FIRM = "АФ Чистопольская"
VEG_HARVEST_WORK_TYPE_IDS: tuple[int, ...] = tuple(
    wt_id
    for group_name, wt_ids in WORK_GROUPS
    if group_name == VEG_HARVEST_GROUP_NAME
    for wt_id in wt_ids
)

# work_type_id уборки для листа «Сводка» (зерновые/масличные/кукуруза/бобовые/свекла/рапс).
# Состав — из plans.WORK_GROUPS["5.0 Уборка"] (как GRAIN_WT_IDS в 11_main_harvest.py):
# все виды уборки группы 5.0, кроме овощей (5.4) и обобщённых wt=6/7.
# «Количество собственных комбайнов на полях» на слайде 1 должно совпадать
# с числом строк СОБСТВЕННЫЕ на листе фирмы (комбайны групп 5/9, covered_area > 0).
_ALL_HARVEST_5_0_WT_IDS: set[int] = {
    wt for gname, wt_ids in WORK_GROUPS if gname == WORK_TYPE_DISPLAY for wt in wt_ids
}
if not _ALL_HARVEST_5_0_WT_IDS:
    raise RuntimeError(
        f"Группа {WORK_TYPE_DISPLAY!r} отсутствует (или пуста) в plans.WORK_GROUPS."
    )
HARVEST_WORK_TYPE_IDS: tuple[int, ...] = tuple(sorted(
    _ALL_HARVEST_5_0_WT_IDS - set(VEG_HARVEST_WORK_TYPE_IDS) - {6, 7}
    | set(SILAGE_CORN_MOWING_WT_IDS)
))

# =============================================================================
# NAMOLOT API — валовый сбор (т) по гос. номеру техники за смену
# =============================================================================
# Источник массы намолота — отчёт «Намолот» (1С), не harvested_weight из Cropwise.
# Площадь по-прежнему считается из Cropwise (FMI) — это не меняется.
#
# Проблема сопоставления: гос. номер в Cropwise и в Namolot записан по-разному:
#   Cropwise: "6183АН"        — цифры + буквы слитно, без региона
#   Cropwise: "3710АК"        — то же (комбайны CLAAS Tucano, Актай)
#   Namolot:  "АН 9333 16"    — буквы + номер + регион, через пробелы
#   Namolot:  "AK 3710"       — буквы латиницей + номер (без региона)
#   Namolot:  "3874 АР 16"    — цифры + буквы + регион (стандартный формат)
#   Cropwise: "3874АР"        — цифры + буквы слитно, без региона
# Сопоставляем не по всей базе Namolot вслепую, а внутри уже отфильтрованного
# по агрофирме/смене набора техники (own_df / hired_df и так строятся только
# по машинам, реально работавшим на полях фирмы в эту смену) — это само по
# себе резко снижает риск коллизий. Дополнительно матчим не только по цифрам
# основного номера, но и по буквам (отсортированным), чтобы не перепутать,
# например, "1145 АН" и "1145 ПР".

NAMOLOT_BASE_URL = (
    (os.environ.get("NAMOLOT_BASE_URL") or "").strip()
    or "http://10.0.1.5/hpp_elevator_new/hs/ground/report"
)
NAMOLOT_USERNAME = os.environ.get("NAMOLOT_USERNAME", "Сводка").strip()
NAMOLOT_PASSWORD = os.environ.get("NAMOLOT_PASSWORD", "3VzLqA6S").strip()

# ВАЖНО: quantity в ответе API — предположительно килограммы (см. 10_main_tasks.py).
NAMOLOT_QTY_TO_TONNES: float = 1.0 / 1000.0

_DIGITS_RE = re.compile(r"\d+")
_CYR_LETTER_RE = re.compile(r"[А-ЯЁ]")

# Буквы, разрешённые на гос.номерах РФ (ГОСТ Р 50577), совпадают по начертанию
# с латиницей — на практике в Cropwise/Namolot они иногда вводятся в другой
# раскладке (латиницей), хотя выглядят идентично кириллице. Нормализуем перед
# сравнением, чтобы такие записи не терялись молча.
# R→Р: частая опечатка — на номере буква Р, но на латинской раскладке её
# путают с R (визуально ближе P, но в данных Cropwise встречается и R).
_LAT_TO_CYR_PLATE_LETTERS: dict[str, str] = {
    "A": "А", "B": "В", "E": "Е", "K": "К", "M": "М", "H": "Н",
    "O": "О", "P": "Р", "R": "Р", "C": "С", "T": "Т", "Y": "У", "X": "Х",
    "U": "У",
}

# Ручное сопоставление: combine_harvester_id в Namolot → registration_number в Cropwise.
# Используется, когда автоматическая нормализация не может связать записи:
#   — серийный/внутренний номер в Namolot vs госномер в Cropwise (R0ACR5950331 → 3175);
#   — разные госномера одной и той же машины (1114 АО → 2966АМ);
#   — явное закрепление пары при нестандартном формате (АМ 2963 16 → 2963АМ и т.п.).
NAMOLOT_TO_CROPWISE_REG: dict[str, str] = {
    "R0ACR5950331": "3175",
    "1114 АО": "2966АМ",
    "АМ 2963 16": "2963АМ",
    "АМ 2964 16": "2964АМ",
    "ОУ 3156": "3156ОУ",
    "74-80": "7480АЕ",
}


def _normalize_plate_str(raw: str | None) -> str:
    """Верхний регистр + латинские омоглифы → кириллица (для сравнения номеров)."""
    s = str(raw or "").upper().strip()
    return "".join(_LAT_TO_CYR_PLATE_LETTERS.get(ch, ch) for ch in s)


def _reg_main_digit_groups(raw: str | None) -> list[str]:
    """Группы цифр в нормализованном гос.номере."""
    s = _normalize_plate_str(raw)
    return _DIGITS_RE.findall(s) if s else []


def _reg_signature(raw: str | None) -> str:
    """Нормализованная сигнатура гос. номера техники: цифры основного номера +
    отсортированные буквы (кириллица, с приведением латинских омоглифов).
    Без учёта пробелов, региона и порядка букв/цифр в строке.

    Cropwise "6183АН"     -> "6183АН"  (одна группа цифр — она и есть номер)
    Cropwise "3710АК"     -> "3710АК"
    Namolot  "АН 9333 16" -> "9333АН"  (основной номер — самая длинная группа цифр)
    Namolot  "AK 3710"    -> "3710АК"  (буквы латиницей → кириллица перед сравнением)
    Namolot  "3874 АР 16" -> "3874АР"  (регион «16» отбрасывается)

    Буквы учитываются, чтобы не перепутать номера с одинаковыми цифрами, но
    разными буквами (например "1145 АН" и "1145 ПР" дают разные сигнатуры).
    Латинские буквы, совпадающие по начертанию с разрешёнными на номерах РФ
    (A/B/E/K/M/H/O/P/C/T/Y/X), приводятся к кириллице — они визуально
    неотличимы от кириллических, но как строки не совпадают.
    """
    if not raw:
        return ""
    s = _normalize_plate_str(raw)
    digit_groups = _reg_main_digit_groups(raw)
    if not digit_groups:
        return ""
    # Основной номер — самая длинная группа цифр (у Cropwise она одна; у Namolot
    # после неё идёт регион — отдельная, более короткая группа, напр. «16»).
    main_digits = max(digit_groups, key=len)
    letters = "".join(sorted(_CYR_LETTER_RE.findall(s)))
    if not letters:
        return ""
    return f"{main_digits}{letters}"


def _reg_main_digits(raw: str | None) -> str:
    """Основной номер (только цифры, без региона) — для резервного сопоставления,
    когда в Cropwise гос.номер введён без букв (например "9371" вместо "9371ТТ").
    """
    if not raw:
        return ""
    digit_groups = _reg_main_digit_groups(raw)
    return max(digit_groups, key=len) if digit_groups else ""


def _cropwise_reg_for_namolot(raw_id: str | None) -> str | None:
    """Гос.номер Cropwise для записи Namolot, если задан ручной маппинг."""
    key = str(raw_id or "").strip()
    if not key:
        return None
    return NAMOLOT_TO_CROPWISE_REG.get(key)


def _reg_source_for_namolot(raw_id: str | None) -> str:
    """Строка гос.номера для расчёта сигнатуры при индексации Namolot:
    из словаря (Cropwise) или исходный combine_harvester_id."""
    mapped = _cropwise_reg_for_namolot(raw_id)
    return mapped if mapped is not None else str(raw_id or "")


_FIELD_CODE_RE = re.compile(r"^[^\s(]+")


def _namolot_field_code(spp: str | None) -> str:
    """Код поля из поля "spp" записи Namolot.

    В Namolot "spp" — это код поля + название культуры + год через пробел,
    например "АФЧ-01-01 Рапс яровой 26г." или "ФКО-02 Картофель 26г.".
    В Cropwise то же поле называется просто кодом, без культуры и года —
    "АФЧ-01-01" / "ФКО-02". Берём первый токен до пробела."""
    s = str(spp or "").strip()
    if not s:
        return ""
    m = _FIELD_CODE_RE.match(s)
    return m.group(0) if m else ""


def _norm_field_code(code: str | None) -> str:
    """Нормализация кода поля для сопоставления Namolot "spp" <-> Cropwise
    field.name (регистр/пробелы/«ё» не должны мешать сравнению)."""
    return re.sub(r"\s+", "", str(code or "").strip().upper().replace("Ё", "Е"))


def _namolot_auth_header() -> dict:
    if not (NAMOLOT_USERNAME and NAMOLOT_PASSWORD):
        return {}
    creds = f"{NAMOLOT_USERNAME}:{NAMOLOT_PASSWORD}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(creds).decode("ascii")}


def _namolot_log(msg: str) -> None:
    print(f"  [namolot] {msg}", flush=True)


def _namolot_proxy_env() -> dict[str, str]:
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")
    return {k: os.environ[k] for k in keys if os.environ.get(k)}


def fetch_namolot_records(date_begin: date, date_end: date) -> list[dict]:
    """Сырые записи «Намолот» — только через namolot_client (без повторного live GET)."""
    _namolot_log(f"=== запрос {date_begin} .. {date_end} ===")
    from namolot_client import fetch_namolot_records as _fetch_with_cache

    records = _fetch_with_cache(date_begin, date_end)
    _namolot_log(f"namolot_client: {len(records)} записей")
    return records


def _parse_namolot_datetime(value) -> datetime | None:
    """
    Разбирает поле "date" записи Namolot в datetime.

    Поддерживает форматы, которые может присылать API:
      - None / "" / 0                         -> None (даты нет)
      - строка ISO 8601 (с временем или без)  -> datetime
      - число (unix timestamp, сек. или мс)   -> datetime

    Часовой пояс намеренно не проставляется (naive datetime) — весь этот
    скрипт (в т.ч. _shift_window) работает с наивным местным временем,
    поэтому и здесь остаёмся в той же системе координат, чтобы сравнение
    с границами смены было корректным.
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
            s = s[:-1]
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
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt

    if isinstance(value, (int, float)):
        ts = value / 1000 if abs(value) > 10 ** 12 else value
        try:
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError, OverflowError):
            return None

    return None


# Кэш по report_date: индекс намолота за смену (см. _NamolotMassIndex).
_namolot_signature_cache: dict[date, "_NamolotMassIndex"] = {}


# Известные расхождения в названии/согласовании рода культуры между Cropwise
# и Namolot — одна и та же культура, но записана по-разному (например,
# «Тритикале озимая» в Cropwise vs «Тритикале озимый» в Namolot). Ключ и
# значение — уже нормализованные (нижний регистр, «ё»→«е») строки.
CROP_NAME_ALIASES: dict[str, str] = {
    "тритикале озимая": "тритикале озимый",
    "тритикале яровая": "тритикале яровой",
}


def _normalize_crop_name(name: str | None) -> str:
    """Нормализация названия культуры для сопоставления Cropwise <-> Namolot
    (регистр, пробелы, «ё»/«е» не должны мешать сопоставлению), плюс известные
    расхождения в согласовании — см. CROP_NAME_ALIASES."""
    s = re.sub(r"\s+", " ", str(name or "").strip().lower().replace("ё", "е"))
    return CROP_NAME_ALIASES.get(s, s)


@dataclass
class _NamolotMassIndex:
    # сигнатура "цифры+буквы" -> firm_folder_id -> {культура (как в Namolot) -> масса, т}
    # firm_folder_id определяется по полю ("spp") записи Namolot — см.
    # _build_namolot_signature_index. Так одна и та же машина, отработавшая
    # за смену на полях двух агрофирм, не задваивает массу в обеих.
    by_signature_firm_crop: dict[str, dict[int, dict[str, float]]]
    # только "цифры" -> firm_folder_id -> {культура -> масса, т} (резервный путь —
    # когда в Cropwise номер без букв)
    by_digits_firm_crop: dict[str, dict[int, dict[str, float]]]
    # только "цифры" -> набор встреченных вариантов букв в Namolot (для контроля
    # неоднозначности резервного пути: если вариантов больше одного — сумма
    # по цифрам может относиться к разным машинам, это будет видно в логе)
    digit_letter_variants: dict[str, set]

    def mass_for(
        self, reg_num: str | None, crop_name: str | None, firm_folder_id: int | None,
    ) -> tuple[float, bool]:
        """(масса, is_fallback) для тройки (гос.номер, культура, агрофирма).
        Культура сопоставляется с записями Namolot по нормализованному названию —
        то, что определено по Cropwise (см. build_own_table), является источником
        истины для подписи строки; Namolot используется только для суммы массы по
        этой конкретной культуре и этой конкретной агрофирме (см. поле "spp").
        is_fallback=True — сопоставили только по цифрам (в Cropwise номер без букв)."""
        target = _normalize_crop_name(crop_name)
        # Культуры поля, которые не относятся к уборке (локальный 14.main их
        # не выдаёт — культура идёт через AgroOperation). Не подставляем массу 1С.
        _NON_HARVEST = {
            "зябь", "пар", "залежь", "залежь, лес", "обмен",
            "введение в оборот", "введение в оборот 2025 год",
            "введение в оборот 2026 год", "введение в оборот 2027 год",
        }

        def _pick(by_crop: dict[str, float]) -> float | None:
            if not by_crop:
                return None
            # 1) точное совпадение нормализованного имени
            for cname, mass in by_crop.items():
                if _normalize_crop_name(cname) == target:
                    return float(mass)
            if target in _NON_HARVEST or target.startswith("введение в оборот"):
                return None
            # 2) нечёткое: одно имя содержит другое (суффиксы года/сорта в 1С)
            if target and target != "—":
                fuzzy: list[tuple[float, float]] = []
                for cname, mass in by_crop.items():
                    n = _normalize_crop_name(cname)
                    if not n:
                        continue
                    if target in n or n in target:
                        fuzzy.append((float(mass), len(n)))
                if len(fuzzy) == 1:
                    return fuzzy[0][0]
            # 3) если у машины в Namolot (в этой агрофирме) ровно одна культура —
            #    берём её только когда Cropwise не определил культуру («—»).
            #    Иначе масса уходила бы на строку с ошибочной культурой (дубль комбайна).
            if len(by_crop) == 1 and (not target or target == "—"):
                return float(next(iter(by_crop.values())))
            return None

        sig = _reg_signature(reg_num)
        if sig:
            got = _pick(self.by_signature_firm_crop.get(sig, {}).get(firm_folder_id, {}))
            if got is not None:
                return got, False
        digits = _reg_main_digits(reg_num)
        if not digits:
            return 0.0, False
        got = _pick(self.by_digits_firm_crop.get(digits, {}).get(firm_folder_id, {}))
        if got is not None:
            return got, not bool(sig)
        return 0.0, not bool(sig)

    def crops_for(self, reg_num: str | None) -> set[str]:
        """Множество культур, зафиксированных Namolot за смену для этого гос.номера,
        по всем агрофирмам вместе (для сверки с культурой, определённой по
        Cropwise — см. build_own_table)."""
        sig = _reg_signature(reg_num)
        by_firm = self.by_signature_firm_crop.get(sig) if sig else None
        if not by_firm:
            digits = _reg_main_digits(reg_num)
            by_firm = self.by_digits_firm_crop.get(digits, {})
        out: set[str] = set()
        for by_crop in by_firm.values():
            out.update(by_crop.keys())
        return out

    def crops_for_firm(self, reg_num: str | None, firm_folder_id: int | None) -> set[str]:
        """Культуры Namolot за смену для (гос.номер, агрофирма)."""
        if firm_folder_id is None:
            return set()
        sig = _reg_signature(reg_num)
        by_firm = self.by_signature_firm_crop.get(sig) if sig else None
        if not by_firm:
            digits = _reg_main_digits(reg_num)
            by_firm = self.by_digits_firm_crop.get(digits, {})
        return set(by_firm.get(firm_folder_id, {}).keys())


def _build_namolot_signature_index(
    report_date: date,
    field_name_to_firm_folder: dict[str, int] | None = None,
) -> "_NamolotMassIndex":
    """Индекс намолота (т) за смену report_date, с сопоставлением по гос.номеру
    техники: основной путь — по полной сигнатуре (цифры+буквы), резервный —
    по одним цифрам (для случаев, когда в Cropwise гос.номер введён без букв).

    Раньше в Namolot поле date было чистой календарной датой без времени, и
    запись за смену report_date матчилась просто по совпадению календарного
    дня с day_start.date(). Теперь в поле date появилось время, поэтому
    запись относится к смене, только если её полное время (дата+время)
    попадает строго в окно [day_start, cutoff) = [report_date-1 06:00 ..
    report_date 06:00) — иначе смежные записи вокруг полуночи (например,
    00:10 или 03:09 следующего календарного дня) задваивались бы или
    терялись при сравнении по одной лишь дате.

    field_name_to_firm_folder — код поля Cropwise (без культуры/года) ->
    firm_folder_id. Каждая запись Namolot относит массу к конкретному полю
    через "spp" (код поля + культура + год, см. _namolot_field_code), а поле
    принадлежит ровно одной агрофирме. Индексируем по (сигнатура, агрофирма),
    а не только по сигнатуре — иначе машина, отработавшая за смену на полях
    двух агрофирм, получает одну и ту же суммарную массу в обеих (масса
    задваивается на уровне отчёта).
    """
    if report_date in _namolot_signature_cache:
        return _namolot_signature_cache[report_date]

    field_name_to_firm_folder = field_name_to_firm_folder or {}

    day_start_str, cutoff_str = _shift_window(report_date)
    day_start_dt = datetime.strptime(day_start_str, "%Y-%m-%dT%H:%M:%S")
    cutoff_dt = datetime.strptime(cutoff_str, "%Y-%m-%dT%H:%M:%S")
    label_date = day_start_dt.date()
    _namolot_log(f"=== индекс массы за смену REPORT_DATE={report_date} ===")
    _namolot_log(f"окно смены [{day_start_str} .. {cutoff_str})")
    _namolot_log(f"запрос API за календарные даты {label_date} .. {report_date}")

    # Шире окна смены на 1 день — в кэше/1С бывают записи до 06:00 соседних суток.
    records = fetch_namolot_records(label_date - timedelta(days=1), report_date)

    if not records:
        _namolot_log(
            "ВНИМАНИЕ: 0 записей — колонка «Масса собранного урожая, т» будет пустой "
            "по всем машинам"
        )

    by_signature_firm_crop: dict[str, dict[int, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(float))
    )
    by_digits_firm_crop: dict[str, dict[int, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(float))
    )
    digit_letter_variants: dict[str, set] = defaultdict(set)
    unmatched: set[str] = set()
    unmatched_field_codes: set[str] = set()
    undated = 0
    out_of_shift = 0
    zero_qty = 0
    in_shift = 0
    manual_mapped: set[str] = set()
    label_str = label_date.isoformat()
    for r in records:
        rec_dt = _parse_namolot_datetime(r.get("date"))
        if rec_dt is None:
            undated += 1
            continue
        if not (day_start_dt <= rec_dt < cutoff_dt):
            out_of_shift += 1
            continue
        in_shift += 1
        raw_id = r.get("combine_harvester_id")
        reg_src = _reg_source_for_namolot(raw_id)
        if _cropwise_reg_for_namolot(raw_id):
            manual_mapped.add(f"{raw_id!r} → {reg_src!r}")
        sig = _reg_signature(reg_src)
        digits = _reg_main_digits(reg_src)
        if not sig and not digits:
            unmatched.add(str(raw_id or ""))
            continue
        qty_t = float(r.get("quantity") or 0) * NAMOLOT_QTY_TO_TONNES
        if qty_t <= 0:
            zero_qty += 1
            continue

        # Агрофирма записи — по коду поля из "spp" (см. _namolot_field_code),
        # сопоставленному с полем Cropwise через field_name_to_firm_folder.
        # Без этого масса машины, отработавшей за смену на полях двух
        # агрофирм, задваивалась бы в обеих (см. docstring функции).
        field_code = _norm_field_code(_namolot_field_code(r.get("spp")))
        firm_folder_id = field_name_to_firm_folder.get(field_code) if field_code else None
        if firm_folder_id is None:
            unmatched_field_codes.add(field_code or str(r.get("spp") or ""))
            continue

        # Культура берётся прямо из записи Namolot (поле crop) — в API она есть
        # у каждой записи вместе с комбайном и полем.
        crop_name = str(r.get("crop") or "").strip() or "—"
        if sig:
            by_signature_firm_crop[sig][firm_folder_id][crop_name] += qty_t
        if digits:
            by_digits_firm_crop[digits][firm_folder_id][crop_name] += qty_t
            letters_part = sig[len(digits):] if sig.startswith(digits) else sig
            digit_letter_variants[digits].add(letters_part)

    ambiguous_digits = {d: v for d, v in digit_letter_variants.items() if len(v) > 1}
    total_mass_t = sum(
        mass
        for by_firm in by_signature_firm_crop.values()
        for by_crop in by_firm.values()
        for mass in by_crop.values()
    )
    _namolot_log(
        f"разбор записей: из API={len(records)}, в смене={in_shift}, "
        f"вне смены={out_of_shift}, без даты={undated}, qty<=0={zero_qty}, "
        f"номер не распознан={len(unmatched)}, поле (spp) не сопоставлено с "
        f"агрофирмой={len(unmatched_field_codes)}"
    )
    if unmatched_field_codes:
        shown_fc = sorted(unmatched_field_codes)[:20]
        more_fc = "" if len(unmatched_field_codes) <= 20 else f" (и ещё {len(unmatched_field_codes) - 20})"
        _namolot_log(
            f"ВНИМАНИЕ: код поля не сопоставлен с агрофирмой, масса этих записей "
            f"НЕ учтена ни в одной агрофирме ({len(unmatched_field_codes)}): {shown_fc}{more_fc}"
        )
    if ambiguous_digits:
        _namolot_log(
            f"ВНИМАНИЕ: резервное сопоставление по цифрам неоднозначно "
            f"для {len(ambiguous_digits)} номеров: "
            f"{dict(list(ambiguous_digits.items())[:10])}"
        )
    if unmatched:
        shown = sorted(unmatched)[:20]
        more = "" if len(unmatched) <= 20 else f" (и ещё {len(unmatched) - 20})"
        _namolot_log(f"ВНИМАНИЕ: не распознан гос.номер ({len(unmatched)}): {shown}{more}")
    if undated:
        _namolot_log(f"ВНИМАНИЕ: записей без даты/времени (пропущены): {undated}")
    if out_of_shift and not in_shift:
        _namolot_log(
            f"ВНИМАНИЕ: все {out_of_shift} записей вне окна смены — "
            f"проверьте REPORT_DATE (смена = [D-1 06:00 .. D 06:00))"
        )
    if manual_mapped:
        _namolot_log(f"ручное сопоставление номеров: {sorted(manual_mapped)}")
    _namolot_log(
        f"итог: техники с намолотом={len(by_signature_firm_crop)}, "
        f"суммарная масса={total_mass_t:.2f} т"
    )
    if by_signature_firm_crop:
        top = sorted(
            (
                (sig, sum(mass for by_crop in by_firm.values() for mass in by_crop.values()))
                for sig, by_firm in by_signature_firm_crop.items()
            ),
            key=lambda x: x[1],
            reverse=True,
        )[:5]
        _namolot_log(f"топ-5 по массе (сигнатура, т, по всем агрофирмам): {top}")
    result = _NamolotMassIndex(
        by_signature_firm_crop={
            sig: {firm: dict(crops) for firm, crops in by_firm.items()}
            for sig, by_firm in by_signature_firm_crop.items()
        },
        by_digits_firm_crop={
            digits: {firm: dict(crops) for firm, crops in by_firm.items()}
            for digits, by_firm in by_digits_firm_crop.items()
        },
        digit_letter_variants=dict(digit_letter_variants),
    )
    _namolot_signature_cache[report_date] = result
    return result


# Имена листов Excel (шапка PDF: «Уборка овощи» / «Уборка овощей» — в report_generation).
VEG_DETAIL_SHEET_NAME = "Уборка, овощи"
VEG_SUMMARY_SHEET_NAME = "Сводка, овощи"

# Лист «Сводка, овощи» — та же структура, что «Сводка», но по овощным work_type_id
# и только для VEG_HARVEST_FIRM. Технику считаем без ограничения по типу (не только
# комбайны), поэтому подписи колонок «...на 1 комбайн» переименовываем в «...на 1 ед. техники».
VEG_SUMMARY_COLUMN_RENAME: dict[str, str] = {
    "Плановая выработка, га/сутки на 1 комбайн": "Плановая выработка, га/сутки на 1 ед. техники",
    "Количество комбайнов на полях": "Количество собственной техники на полях",
    "Выработка за сутки, га/1 комбайн": "Выработка за сутки, га/1 ед. техники",
    "Часовая выработка на 1 комбайн, га": "Часовая выработка на 1 ед. техники, га",
    "Всего собств. комбайнов в наличии": "Всего собств. техники в наличии",
}

# Порядок культур в таблице (п.5 ТЗ): рапс яровой → озимый → пшеница → соя → подсолнечник → кукуруза.
_HARVEST_TECH_CROP_ORDER_HEAD: tuple[str, ...] = (
    "Рапс яровой",
    "Рапс озимый",
    "Пшеница озимая",
    "Соя",
    "Подсолнечник",
    "Кукуруза на зерно",
    "Кукуруза",
)
HARVEST_TECH_CROP_ORDER: tuple[str, ...] = _HARVEST_TECH_CROP_ORDER_HEAD + tuple(
    c for c in PREFERRED_CROP_ORDER if c not in _HARVEST_TECH_CROP_ORDER_HEAD
)
_HARVEST_TECH_CROP_INDEX = {name.lower(): i for i, name in enumerate(HARVEST_TECH_CROP_ORDER)}

# Урожайность, т/га внутри группы поля: отрезок [Ур.мин, Ур.мах] без аномалий,
# шаг = (макс−мин)/3 → зелёный / оранжевый / красный по значению; аномалии — серые.
YIELD_TOP_TERTILE_COLOR = "C6EFCE"  # 100–66,7% диапазона (от макс − шаг до макс)
YIELD_MID_TERTILE_COLOR = "FFC000"  # 66,6–33,4%
YIELD_BOTTOM_TERTILE_COLOR = "FFC7CE"  # 33,3–0%
YIELD_ANOMALY_COLOR = "D9D9D9"  # аномалия — серый
YIELD_ZERO_COLOR = "FFC7CE"  # нулевая урожайность / выработка — красный
YIELD_ANOMALY_THRESHOLDS: dict[str, float] = {
    "Рапс озимый": 4.4,
    "Пшеница озимая": 7.2,
    "Соя": 3.3,
    "Кукуруза на зерно": 10.2,
    "Рапс яровой": 3.6,
    "Подсолнечник": 3.45,
}
_YIELD_ANOMALY_INDEX = {
    _normalize_crop_name(name): threshold for name, threshold in YIELD_ANOMALY_THRESHOLDS.items()
}

# Колонки СОБСТВЕННЫЕ (зерновые) по макету PDF:
# № → Культура → Комбайн → Гос. номер → группа → … → урожайность.
# «Культура» — добавлена, т.к. один и тот же комбайн может за смену убирать две
# разные культуры; в этом случае он даёт не одну строку, а несколько — по одной
# на каждую культуру (площадь/время/выработка/масса — только по этой культуре).
OWN_COLUMNS = [
    "№ п/п",
    "Культура",
    "Комбайн",
    "Гос. номер",
    "Номер группы комбайнов на одном поле",
    "Обработанная площадь, га",
    "Время работы в полях, час",
    "Средняя выр-ка, га/час",
    "Средняя скорость на полях, км/ч",
    "Коэффициент использования конструкционной ширины захвата, %",
    "Масса собранного урожая, т",
    "Урожайность, т/га",
]

# СОБСТВЕННЫЕ и НАЕМНЫЕ — друг под другом, обе таблицы начинаются с колонки 1.
HIRED_START_COL = 1
# Между таблицами: 1 пустая строка-разделитель + 1 строка заголовка секции «НАЕМНЫЕ».
TABLE_GAP_ROWS = 2

# Цвета для групп комбайнов на одном поле (одна группа — один цвет)
GROUP_COLORS = ["FF6B6B", "FFA94D", "FFD43B", "69DB7C", "74C0FC", "B197FC", "DA77F2"]

# Колонка «Номер группы комбайнов на одном поле» (1-based)
GROUP_COL_IDX = 5
CROP_COL_IDX = OWN_COLUMNS.index("Культура") + 1
YIELD_COL_IDX = OWN_COLUMNS.index("Урожайность, т/га") + 1
OUTPUT_COL_IDX = OWN_COLUMNS.index("Средняя выр-ка, га/час") + 1

# Справочная заметка внизу листа
REFERENCE_NOTE = (
    "Справочно. На следующих страницах добавлена информация по урожайности комбайнов "
    "работающих на одном поле, одна группа выделена одним цветом и одним групповым номером, "
    "также присвоен номер группы. Есть поля где работало по одному комбайну - также выделено цветом."
)

# Колонки НАЕМНЫЕ — те же, что у СОБСТВЕННЫЕ (одинаковое оформление таблиц).
HIRED_COLUMNS = list(OWN_COLUMNS)

_SUMMARY_WIDTHS_14: dict[int, float] = {
    1: 28,
    2: 16,
    3: 14,
    4: 13,
    5: 15,
    6: 15,
    7: 13,
}

_OWN_WIDTHS_14: dict[int, float] = {
    1: 6,   # № п/п
    2: 14,  # Культура
    3: 30,  # «Техника» / «Комбайн» — длинные названия + перенос
    4: 11,  # Гос. номер (по центру)
    5: 10,  # Номер группы
    6: 12,  # Обработанная площадь
    7: 11,  # Время работы (по центру)
    8: 10,
    9: 10,
    10: 16,  # Коэффициент использования конструкционной ширины захвата
    11: 11,
    12: 10,
}

# Ширины НАЕМНЫЕ совпадают с СОБСТВЕННЫЕ.
_HIRED_WIDTHS_14: dict[int, float] = dict(_OWN_WIDTHS_14)

_HEADER_ROW_HEIGHT = 130.0
_DATA_ROW_HEIGHT = 15.0


def _cell_is_numeric(val) -> bool:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return True
    if val is None:
        return False
    s = str(val).strip().replace("\u00a0", "").replace(" ", "")
    if not s or s in {"-", "—"}:
        return False
    try:
        float(s.replace(",", "."))
        return True
    except ValueError:
        return False


def _apply_fixed_col_widths(ws, widths: dict[int, float], start_col: int = 1) -> None:
    from openpyxl.utils import get_column_letter
    for rel, w in widths.items():
        ws.column_dimensions[get_column_letter(start_col + rel - 1)].width = w


def _normalize_data_row_layout(
    ws,
    header_row: int,
    n_data_rows: int,
    n_cols: int,
    *,
    start_col: int = 1,
    left_text_cols: tuple[int, ...] = (2,),
    center_text_cols: tuple[int, ...] = (),
    wrap_cols: tuple[int, ...] = (),
) -> None:
    """
    wrap_cols — относительные (1-based в пределах таблицы) номера колонок, для которых
    включается перенос текста по словам (wrap_text=True); высота строки увеличивается
    под нужное число строк, чтобы длинное название (например, техники) не обрезалось.
    center_text_cols — текст по центру (Гос. номер, время работы и т.п.).
    """
    import math

    from openpyxl.styles import Alignment
    from openpyxl.utils import get_column_letter

    if n_data_rows <= 0:
        return
    align_c = Alignment(horizontal="center", vertical="center", wrap_text=False)
    align_l = Alignment(horizontal="left", vertical="center", wrap_text=False)
    align_l_wrap = Alignment(horizontal="left", vertical="center", wrap_text=True)

    # ширина колонок (в символах) для расчёта нужного числа строк при переносе
    wrap_widths = {
        rel: (ws.column_dimensions[get_column_letter(start_col + rel - 1)].width or 10)
        for rel in wrap_cols
    }

    for r in range(header_row + 1, header_row + n_data_rows + 1):
        max_lines = 1
        for c in range(start_col, start_col + n_cols):
            cell = ws.cell(row=r, column=c)
            rel = c - start_col + 1
            if rel in wrap_cols:
                cell.alignment = align_l_wrap
                text = str(cell.value or "").strip()
                if text:
                    chars_per_line = max(int(wrap_widths.get(rel, 10) * 0.9), 1)
                    # запас по ширине (перенос по словам может оставлять "хвосты")
                    lines = max(1, math.ceil(len(text) / chars_per_line * 1.2))
                    max_lines = max(max_lines, lines)
            elif rel == 1 or rel in center_text_cols:
                cell.alignment = align_c
            elif rel in left_text_cols:
                cell.alignment = align_l
            elif _cell_is_numeric(cell.value):
                cell.alignment = align_c
            else:
                cell.alignment = align_l
        ws.row_dimensions[r].height = max(_DATA_ROW_HEIGHT, max_lines * _DATA_ROW_HEIGHT)


def _group_color_for_number(grp: int) -> str:
    """Уникальный цвет номера группы в рамках одной сводки (без повторов по модулю)."""
    import colorsys

    n = int(grp)
    if n <= 0:
        return "FFFFFF"
    if n <= len(GROUP_COLORS):
        return GROUP_COLORS[n - 1]
    hue = ((n - 1) * 0.618033988749895) % 1.0
    red, green, blue = colorsys.hls_to_rgb(hue, 0.72, 0.62)
    return f"{int(red * 255):02X}{int(green * 255):02X}{int(blue * 255):02X}"


def _apply_group_column_colors(
    ws,
    header_row: int,
    group_list: list[int],
    *,
    group_col: int,
) -> None:
    from openpyxl.styles import PatternFill
    if not group_list:
        return
    for i, grp in enumerate(group_list):
        r = header_row + 1 + i
        color = _group_color_for_number(int(grp))
        ws.cell(row=r, column=group_col).fill = PatternFill("solid", fgColor=color)


def _crop_sort_key(crop_name: str) -> tuple[int, str]:
    """Ключ сортировки строк таблицы: рапс яровой → рапс озимый → пшеница озимая → …"""
    name = str(crop_name or "—").strip()
    idx = _HARVEST_TECH_CROP_INDEX.get(name.lower(), len(HARVEST_TECH_CROP_ORDER))
    return idx, name.lower()


def _is_yield_anomaly(crop_name: str, yield_val: float) -> bool:
    threshold = _YIELD_ANOMALY_INDEX.get(_normalize_crop_name(crop_name))
    if threshold is None:
        return False
    return yield_val > threshold


def _parse_yield_cell(val) -> float | None:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    if val is None:
        return None
    s = str(val).strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if not s or s in {"-", "—"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _yield_tertile_fill_by_value(
    yield_val: float,
    min_y: float,
    max_y: float,
    *,
    fill_top,
    fill_mid,
    fill_bottom,
):
    """Цвет ячейки урожайности по отрезку [min_y, max_y] и шагу (max−min)/3.

    Зелёный: [max−step, max] (100–66,6% диапазона),
    оранжевый: [max−2·step, max−step) (66,5–33,3%),
    красный: [min_y, max−2·step) (33,2–0%).
    """
    span = max_y - min_y
    if span <= 0:
        return fill_top
    step = span / 3.0
    upper = max_y - step
    lower = max_y - 2.0 * step
    if yield_val >= upper:
        return fill_top
    if yield_val >= lower:
        return fill_mid
    return fill_bottom


def _apply_yield_tertile_colors(
    ws,
    header_row: int,
    n_data_rows: int,
    *,
    start_col: int = 1,
    crop_col: int = CROP_COL_IDX,
    yield_col: int = YIELD_COL_IDX,
    group_col: int = GROUP_COL_IDX,
    output_col: int = OUTPUT_COL_IDX,
) -> None:
    """Подсветка урожайности: три полосы по значению внутри группы поля (ТЗ терции)."""
    from openpyxl.styles import PatternFill

    if n_data_rows <= 0:
        return
    fill_top = PatternFill("solid", fgColor=YIELD_TOP_TERTILE_COLOR)
    fill_mid = PatternFill("solid", fgColor=YIELD_MID_TERTILE_COLOR)
    fill_bottom = PatternFill("solid", fgColor=YIELD_BOTTOM_TERTILE_COLOR)
    fill_anomaly = PatternFill("solid", fgColor=YIELD_ANOMALY_COLOR)
    fill_zero = PatternFill("solid", fgColor=YIELD_ZERO_COLOR)

    crop_col_abs = start_col + crop_col - 1
    yield_col_abs = start_col + yield_col - 1
    group_col_abs = start_col + group_col - 1
    output_col_abs = start_col + output_col - 1

    rows_by_group: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)

    for r in range(header_row + 1, header_row + n_data_rows + 1):
        yield_val = _parse_yield_cell(ws.cell(row=r, column=yield_col_abs).value)
        output_val = _parse_yield_cell(ws.cell(row=r, column=output_col_abs).value)
        crop_name = str(ws.cell(row=r, column=crop_col_abs).value or "")
        grp = str(ws.cell(row=r, column=group_col_abs).value or "").strip()

        if output_val is not None and output_val == 0:
            ws.cell(row=r, column=output_col_abs).fill = fill_zero

        if yield_val is None:
            continue
        if yield_val == 0:
            ws.cell(row=r, column=yield_col_abs).fill = fill_zero
            continue
        if _is_yield_anomaly(crop_name, yield_val):
            ws.cell(row=r, column=yield_col_abs).fill = fill_anomaly
            continue
        rows_by_group[(grp, _normalize_crop_name(crop_name))].append((r, yield_val))

    for items in rows_by_group.values():
        if not items:
            continue
        yields = [y for _, y in items]
        min_y = min(yields)
        max_y = max(yields)
        for r, y in items:
            ws.cell(row=r, column=yield_col_abs).fill = _yield_tertile_fill_by_value(
                y, min_y, max_y,
                fill_top=fill_top, fill_mid=fill_mid, fill_bottom=fill_bottom,
            )


def style_report14_summary_sheet(ws, start_row: int, n_data_rows: int, n_cols: int) -> None:
    from excel_style import finish_harvest_table
    from openpyxl.styles import Alignment

    finish_harvest_table(
        ws,
        start_row=start_row,
        n_data_rows=n_data_rows,
        n_cols=n_cols,
        has_total_row=True,
    )
    _apply_fixed_col_widths(ws, _SUMMARY_WIDTHS_14)
    # Высокие заголовки + перенос — длинные подписи колонок не обрезаются в PDF.
    ws.row_dimensions[start_row].height = max(
        ws.row_dimensions[start_row].height or 0.0, 110.0,
    )
    align_hdr = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for c in range(1, n_cols + 1):
        ws.cell(row=start_row, column=c).alignment = align_hdr
    _normalize_data_row_layout(
        ws, start_row, max(n_data_rows - 1, 0), n_cols, left_text_cols=(1,),
    )
    if n_data_rows > 0:
        ws.row_dimensions[start_row + n_data_rows].height = _DATA_ROW_HEIGHT


def style_report14_firm_table(
    ws,
    header_row: int,
    n_data_rows: int,
    n_cols: int,
    *,
    start_col: int,
    col_widths: dict[int, float],
    group_list: list[int] | None = None,
    group_col: int | None = None,
    wrap_cols: tuple[int, ...] = (),
) -> None:
    from excel_style import style_report_range
    n_rows = 1 + max(n_data_rows, 0)
    style_report_range(
        ws,
        start_row=header_row,
        n_rows=n_rows,
        n_cols=n_cols,
        has_total_row=False,
        start_col=start_col,
        center_numeric=True,
    )
    _apply_fixed_col_widths(ws, col_widths, start_col=start_col)
    ws.row_dimensions[header_row].height = _HEADER_ROW_HEIGHT
    # PDF: кол.2 Культура и кол.3 Комбайн/Техника — слева; 4 Гос.номер и 7 Время — по центру.
    if start_col == 1:
        left_cols = (2, 3)
        center_cols = (4, 7)
    else:
        left_cols = (2, 3)
        center_cols = ()
    _normalize_data_row_layout(
        ws, header_row, n_data_rows, n_cols,
        start_col=start_col, left_text_cols=left_cols,
        center_text_cols=center_cols,
        wrap_cols=wrap_cols,
    )
    if group_list and group_col is not None:
        _apply_group_column_colors(ws, header_row, group_list, group_col=group_col)
    _apply_yield_tertile_colors(
        ws, header_row, n_data_rows,
        start_col=start_col, crop_col=CROP_COL_IDX, yield_col=YIELD_COL_IDX,
        group_col=GROUP_COL_IDX, output_col=OUTPUT_COL_IDX,
    )


def style_report14_firm_sheet(
    ws,
    row_own_header: int,
    own_n_data: int,
    row_hired_header: int | None,
    hired_n_data: int,
    group_list: list[int] | None,
    *,
    own_n_cols: int | None = None,
    hired_n_cols: int | None = None,
    hired_group_list: list[int] | None = None,
) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    own_cols = own_n_cols if own_n_cols is not None else len(OWN_COLUMNS)
    hired_cols = hired_n_cols if hired_n_cols is not None else len(HIRED_COLUMNS)

    # Одинаковое оформление: те же ширины, перенос «Комбайн»/«Техника»,
    # центрирование гос. номера и времени.
    style_report14_firm_table(
        ws, row_own_header, own_n_data, own_cols,
        start_col=1, col_widths=_OWN_WIDTHS_14,
        group_list=group_list, group_col=GROUP_COL_IDX,
        wrap_cols=(2, 3),
    )
    if row_hired_header is not None and hired_n_data > 0:
        style_report14_firm_table(
            ws, row_hired_header, hired_n_data, hired_cols,
            start_col=HIRED_START_COL, col_widths=_HIRED_WIDTHS_14,
            group_list=hired_group_list, group_col=GROUP_COL_IDX,
            wrap_cols=(2, 3),
        )

    sec_font = Font(bold=True, size=11)
    sec_fill = PatternFill("solid", fgColor="F3F4F6")
    sections: list[tuple[int, int, str]] = [
        (row_own_header, own_cols, "СОБСТВЕННЫЕ"),
    ]
    if row_hired_header is not None and hired_n_data > 0:
        sections.append((row_hired_header, hired_cols, "НАЕМНЫЕ"))
    for header_row, n_cols, title in sections:
        sec_row = header_row - 1
        ws.merge_cells(
            start_row=sec_row, start_column=1,
            end_row=sec_row, end_column=n_cols,
        )
        cell = ws.cell(row=sec_row, column=1, value=title)
        cell.font = sec_font
        cell.fill = sec_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[sec_row].height = 20.0


# HTTP-КЛИЕНТ
# =============================================================================

session = requests.Session()
session.headers.update({
    "X-User-Api-Token": API_TOKEN,
    "Content-Type": "application/json",
})
# Без явного timeout requests.get() может ждать ответ сколь угодно долго,
# если сервер/прокси/сеть "подвиснет" — оборачиваем session.get, чтобы
# таймаут применялся всегда, даже там, где его забыли явно указать.
_ORIGINAL_SESSION_GET = session.get
def _session_get_with_timeout(*args, **kwargs):
    kwargs.setdefault("timeout", (10, 60))  # (connect timeout, read timeout) в секундах
    return _ORIGINAL_SESSION_GET(*args, **kwargs)
session.get = _session_get_with_timeout


def get_all(endpoint: str, params: dict | None = None) -> list[dict]:
    params = dict(params or {})
    params.setdefault("per_page", 1000)
    results, page = [], 1
    while True:
        t0 = datetime.now()
        print(f"    [get_all] GET {endpoint} page={page} params={params} ...", flush=True)
        r = session.get(f"{BASE_URL}/{endpoint}", params={**params, "page": page})
        elapsed = (datetime.now() - t0).total_seconds()
        r.raise_for_status()
        body = r.json()
        chunk = body if isinstance(body, list) else body.get("data", [])
        results.extend(chunk)
        print(
            f"    [get_all] {endpoint} page={page}: {len(chunk)} записей "
            f"за {elapsed:.1f}с (всего собрано {len(results)})",
            flush=True,
        )
        if len(chunk) < params["per_page"]:
            break
        page += 1
    return results


def get_all_from_id(endpoint: str, params: dict | None = None) -> list[dict]:
    base_params = dict(params or {})
    results, from_id = [], 0
    iteration = 0
    while True:
        iteration += 1
        t0 = datetime.now()
        print(
            f"    [get_all_from_id] GET {endpoint} from_id={from_id} "
            f"(итерация {iteration}) ...", flush=True,
        )
        r = session.get(
            f"{BASE_URL}/{endpoint}",
            params={**base_params, "from_id": from_id, "limit": 1000},
        )
        elapsed = (datetime.now() - t0).total_seconds()
        r.raise_for_status()
        body = r.json()
        chunk = body.get("data", [])
        if not chunk:
            print(f"    [get_all_from_id] {endpoint}: пусто, конец", flush=True)
            break
        results.extend(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        obtained = meta.get("obtained_records", 0)
        print(
            f"    [get_all_from_id] {endpoint} from_id={from_id}: {len(chunk)} записей "
            f"за {elapsed:.1f}с (всего собрано {len(results)}, last_id={last_id}, obtained={obtained})",
            flush=True,
        )
        if not last_id or obtained < 100:
            break
        from_id = last_id + 1
    return results


# =============================================================================
# ДАННЫЕ
# =============================================================================

def load_machines() -> list[dict]:
    """Весь парк техники одним проходом (как load_reference_data в 2_main_ideal_compact)."""
    print("  Загружаем machines...", flush=True)
    machines = get_all_from_id("machines")
    print(f"    machines: {len(machines)} записей", flush=True)
    return machines


def split_machines(
    all_machines: list[dict],
) -> tuple[list[dict], list[dict], set[int], set[int], dict[int, int]]:
    """Разбивает загруженный парк на собственные комбайны / наёмные + machine_group map."""
    harvesters = [
        m for m in all_machines
        if m.get("machine_group_id") in HARVESTER_GROUP_IDS
    ]
    hired_machines = [
        m for m in all_machines
        if m.get("machine_group_id") == HIRED_GROUP_ID
    ]
    harvester_ids = {m["id"] for m in harvesters}
    hired_ids = {m["id"] for m in hired_machines}
    machine_group = {
        m["id"]: int(m.get("machine_group_id") or 0)
        for m in all_machines
        if m.get("id") is not None
    }
    return harvesters, hired_machines, harvester_ids, hired_ids, machine_group


def fetch_group_folders() -> dict[int, str]:
    """group_folder_id → name из API (для отображаемых названий фирм)."""
    folders = get_all("group_folders")
    return {f["id"]: (f.get("name") or "").strip() for f in folders if f.get("id") is not None}


def _folder_parent_map(gfs_all: list[dict]) -> dict[int, int | None]:
    return {
        int(g["id"]): (int(g["parent_id"]) if g.get("parent_id") is not None else None)
        for g in gfs_all
        if g.get("id") is not None
    }


def _resolve_to_holding_root(
    folder_id: int | None,
    parent_map: dict[int, int | None],
    root_ids: set[int],
) -> int | None:
    """Поднимаем group_folder до корня агрофирмы (как в 11_main_harvest)."""
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


def _norm_field_blob(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower().replace("ё", "е"))


def load_geo_reference() -> tuple[
    dict[int, int | None],
    dict[int, int | None],
    dict[int, str],
    dict[int, int],
    dict[str, int],
]:
    """fields / field_groups / group_folders + field_id → корневой folder_id агрофирмы.

    Дополнительно строит field_name_to_firm_folder — код поля (field.name,
    нормализованный) → firm_folder_id. Используется для разнесения массы
    Namolot по агрофирмам через поле "spp" (см. _build_namolot_signature_index):
    сама по себе техника может за смену отработать на полях двух агрофирм,
    и без этой привязки масса по гос.номеру задваивалась бы в обеих."""
    print("  Загружаем fields / field_groups / group_folders...", flush=True)
    all_fields = fetch_fields()
    all_field_groups = fetch_field_groups()
    all_group_folders = get_all("group_folders")
    folder_to_name = {
        f["id"]: (f.get("name") or "").strip()
        for f in all_group_folders
        if f.get("id") is not None
    }
    field_to_fg = {f["id"]: f.get("field_group_id") for f in all_fields}
    fg_to_folder = {fg["id"]: fg.get("group_folder_id") for fg in all_field_groups}
    parent_map = _folder_parent_map(all_group_folders)
    root_ids = set(FIRM_TO_FOLDER.values())
    field_to_firm_folder: dict[int, int] = {}

    for f in all_fields:
        fid = f.get("id")
        if fid is None:
            continue
        fgid = f.get("field_group_id")
        raw_folder = f.get("group_folder_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fg_to_folder.get(fgid) or fg_to_folder.get(int(fgid))
        try:
            rint = int(raw_folder) if raw_folder is not None else None
        except (TypeError, ValueError):
            rint = None
        root = _resolve_to_holding_root(rint, parent_map, root_ids)
        if root is not None:
            field_to_firm_folder[int(fid)] = root

    for f in all_fields:
        fid = f.get("id")
        if fid is None:
            continue
        blob = _norm_field_blob(f"{f.get('name') or ''} {f.get('title') or ''}")
        for needle, firm_key in FIELD_NAME_FORCE_RULES:
            if needle in blob:
                folder = FIRM_TO_FOLDER.get(firm_key)
                if folder is not None:
                    field_to_firm_folder[int(fid)] = int(folder)
                break

    print(f"  Полей с привязкой к агрофирме: {len(field_to_firm_folder)}", flush=True)

    # Код поля (field.name) -> firm_folder_id, для сопоставления с "spp" из Namolot.
    # При коллизии (два поля с одинаковым нормализованным кодом в разных фирмах)
    # оставляем первое и предупреждаем — иначе разнесение массы Namolot по полю
    # станет неоднозначным.
    field_name_to_firm_folder: dict[str, int] = {}
    _name_collisions: set[str] = set()
    for f in all_fields:
        fid = f.get("id")
        if fid is None:
            continue
        try:
            fid_i = int(fid)
        except (TypeError, ValueError):
            continue
        folder = field_to_firm_folder.get(fid_i)
        if folder is None:
            continue
        code = _norm_field_code(f.get("name"))
        if not code:
            continue
        if code in field_name_to_firm_folder and field_name_to_firm_folder[code] != folder:
            _name_collisions.add(code)
            continue
        field_name_to_firm_folder[code] = folder
    if _name_collisions:
        print(
            f"  ВНИМАНИЕ: код поля неоднозначен между агрофирмами для "
            f"{len(_name_collisions)} кодов (используется первое совпадение): "
            f"{sorted(_name_collisions)[:20]}",
            flush=True,
        )

    return field_to_fg, fg_to_folder, folder_to_name, field_to_firm_folder, field_name_to_firm_folder


def _assign_firm_folder_column(
    fmi_df: pd.DataFrame,
    field_to_firm_folder: dict[int, int],
    field_to_fg: dict[int, int | None],
    fg_to_folder: dict[int, int | None],
) -> pd.DataFrame:
    """folder_id строки FMI = агрофирма поля (машина может работать в нескольких АФ)."""

    def _folder_for_field(fid) -> int | None:
        if fid is None:
            return None
        try:
            fid_i = int(fid)
        except (TypeError, ValueError):
            return None
        if fid_i in field_to_firm_folder:
            return field_to_firm_folder[fid_i]
        fg = field_to_fg.get(fid_i) or field_to_fg.get(fid)
        if fg is None:
            return None
        raw = fg_to_folder.get(fg) or fg_to_folder.get(int(fg))
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    out = fmi_df.copy()
    out["folder_id"] = out["field_id"].map(_folder_for_field)
    return out


def fetch_machine_tasks(
    harvester_ids: set[int] | None,
    report_date: date,
    work_type_ids: tuple[int, ...] = HARVEST_WORK_TYPE_IDS,
) -> list[dict]:
    """
    Задания уборки за СМЕНУ report_date: [report_date-1 06:00 .. report_date 06:00)
    (по умолчанию work_type 124,125,126,127,145). Не календарные сутки!

    Отбор — как 10_main_harvest / 11_main_harvest: по end_time задания в окне смены
    (end_time >= day_start и end_time < cutoff + buffer), а не по пересечению start/end.
    """
    day_start_dt, cutoff_dt = _shift_datetimes(report_date)
    cutoff_ext_dt = cutoff_dt + timedelta(hours=CUTOFF_BUFFER_HOURS)
    day_start_str = day_start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_ext_str = cutoff_ext_dt.strftime("%Y-%m-%dT%H:%M:%S")
    seen = set()
    result = []
    for wt_id in work_type_ids:
        chunk = get_all("machine_tasks", {
            "work_type_id": wt_id,
            "end_time_gt_eq": day_start_str,
            "end_time_lt": cutoff_ext_str,
        })
        for t in chunk:
            mid = t.get("machine_id")
            tid = t.get("id")
            if tid in seen:
                continue
            end_dt = _parse_dt(t.get("end_time"))
            if not end_dt or end_dt < day_start_dt or end_dt >= cutoff_ext_dt:
                continue
            if harvester_ids is not None and mid not in harvester_ids:
                continue
            seen.add(tid)
            result.append(t)
    return result


# =============================================================================
# БАТЧ-ЗАПРОСЫ FMI (machine_task_field_mapping_items) по списку machine_task_id
# =============================================================================
# Комма-разделённый список значений в фильтре machine_task_id подтверждён
# рабочим на практике (см. 10_main_tasks.py: fetch_mtfmi_batch) — вместо
# полного скана диапазона по created_at запрашиваем записи пачками по
# FMI_BATCH_SIZE machine_task_id за один запрос. Упавшие пачки повторяем
# поштучно (фильтр по одному id — самый надёжный документированный вариант).
# Если батч в итоге не нашёл вообще ничего (пустой results и
# results_no_timetable) — на всякий случай остаётся старый полный скан
# (_fetch_fmi_full_scan_fallback) как последняя подстраховка.
#
# ВАЖНО: это батчинг именно Cropwise API. К Namolot (fetch_namolot_records)
# отношения не имеет: тот запрашивается одним GET за весь диапазон дат
# (date_begin/date_end) и списков id вообще не принимает — не батчуется и
# не меняется.

FMI_BATCH_SIZE = 300     # сколько machine_task_id класть в один запрос FMI
FMI_SEMAPHORE_LIMIT = 20


def _chunked(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


async def _fetch_all_pages_async(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    items, from_id = [], 0
    async with semaphore:
        while True:
            r = await client.get(url, params={**params, "from_id": from_id, "limit": 1000})
            r.raise_for_status()
            body = r.json()
            chunk = body.get("data", [])
            if not chunk:
                break
            items.extend(chunk)
            meta = body.get("meta", {}).get("response", {})
            last_id = meta.get("last_record_id")
            obtained = meta.get("obtained_records", 0)
            if not last_id or obtained < 1000:
                break
            from_id = last_id + 1
    return items


def _fmi_shift_split(
    chunk: list, task_ids: set[int], shift_date_strs: set[str],
) -> tuple[list, list]:
    """Разбивает записи FMI на (попавшие в смену по work_timetable, без совпадения),
    отфильтровав по task_ids. Та же логика, что раньше была внутри fetch_fmi."""
    results, results_no_timetable = [], []
    for row in chunk:
        if row.get("machine_task_id") not in task_ids:
            continue
        timetable = row.get("work_timetable") or []
        if timetable:
            if any(
                any(str(interval[0]).startswith(ds) for ds in shift_date_strs)
                for interval in timetable if interval
            ):
                results.append(row)
            else:
                results_no_timetable.append(row)
        else:
            results.append(row)
    return results, results_no_timetable


async def _fetch_fmi_batch_async(task_ids: set[int], report_date: date) -> list[dict]:
    shift_date_strs = {
        (report_date - timedelta(days=1)).strftime("%Y-%m-%d"),
        report_date.strftime("%Y-%m-%d"),
    }
    url = f"{BASE_URL}/machine_task_field_mapping_items"
    headers = {"X-User-Api-Token": API_TOKEN}
    batches = _chunked(sorted(task_ids), FMI_BATCH_SIZE)
    semaphore = asyncio.Semaphore(FMI_SEMAPHORE_LIMIT)

    results: list = []
    results_no_timetable: list = []

    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        batch_results = await asyncio.gather(*[
            _fetch_all_pages_async(
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
            r, rnt = _fmi_shift_split(res, task_ids, shift_date_strs)
            results.extend(r)
            results_no_timetable.extend(rnt)

        if failed_ids:
            print(
                f"    Повторяем поштучно {len(failed_ids)} задач (батч FMI не удался)...",
                flush=True,
            )
            retry_results = await asyncio.gather(*[
                _fetch_all_pages_async(client, url, {"machine_task_id": tid}, semaphore)
                for tid in failed_ids
            ], return_exceptions=True)
            for tid, res in zip(failed_ids, retry_results):
                if isinstance(res, BaseException):
                    print(f"    [fmi] не удалось получить task_id={tid}: {res}", flush=True)
                    continue
                r, rnt = _fmi_shift_split(res, task_ids, shift_date_strs)
                results.extend(r)
                results_no_timetable.extend(rnt)

    return results if results else results_no_timetable


def _fetch_fmi_full_scan_fallback(task_ids: set[int], report_date: date) -> list[dict]:
    """
    Старый безопасный вариант (полный скан) — подстраховка на случай, если
    батч-запрос по machine_task_id вообще ничего не нашёл (например, FMI ещё
    не проиндексированы под этот фильтр на момент запроса):
    1) Сначала created_at ±7 дней (FMI могут создаваться с задержкой).
    2) Если 0 — fallback: без created_at, полный скан по from_id.
    """
    shift_date_strs = {
        (report_date - timedelta(days=1)).strftime("%Y-%m-%d"),
        report_date.strftime("%Y-%m-%d"),
    }
    date_from = (report_date - timedelta(days=8)).strftime("%Y-%m-%dT00:00:00")
    date_to = (report_date + timedelta(days=7)).strftime("%Y-%m-%dT23:59:59")
    results, results_no_timetable, from_id = [], [], 0

    def process_chunk(chunk: list) -> None:
        r, rnt = _fmi_shift_split(chunk, task_ids, shift_date_strs)
        results.extend(r)
        results_no_timetable.extend(rnt)

    while True:
        r = session.get(
            f"{BASE_URL}/machine_task_field_mapping_items",
            params={
                "created_at_gt_eq": date_from,
                "created_at_lt_eq": date_to,
                "limit": 1000,
                "from_id": from_id,
            },
        )
        r.raise_for_status()
        body = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        process_chunk(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        obtained = meta.get("obtained_records", 0)
        if not last_id or obtained < 1000:
            break
        from_id = last_id + 1

    if results:
        return results
    if results_no_timetable:
        return results_no_timetable

    # Fallback: без created_at (FMI могут создаваться с задержкой)
    from_id = 0
    for _ in range(20):  # макс. 20k записей
        r = session.get(
            f"{BASE_URL}/machine_task_field_mapping_items",
            params={"limit": 1000, "from_id": from_id},
        )
        r.raise_for_status()
        body = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        process_chunk(chunk)
        if results or results_no_timetable:
            break
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        obtained = meta.get("obtained_records", 0)
        if not last_id or obtained < 1000:
            break
        from_id = last_id + 1

    return results if results else results_no_timetable


# =============================================================================
# КУЛЬТУРА ЗАДАНИЯ ЧЕРЕЗ AgroOperation
# =============================================================================
# Та же связка, что в 2_main_ideal_compact.py:
#   MachineTask -> MachineTaskAgroOperationMappingItem -> AgroOperation -> HistoryItem -> Crop.
# Нужна, чтобы разбить один и тот же комбайн на несколько строк, если за смену он
# убирал разные культуры: площадь/время/выработка — по данным Cropwise (FMI),
# культура для каждой FMI-записи определяется по её machine_task_id через эту связку,
# а при отсутствии однозначной операции — по резервной карте field_to_crop_general
# (сумма history_items по полю, без привязки к конкретному заданию).

AGRO_OP_BATCH_SIZE = 300


async def _fetch_mass_request_async(
    client: httpx.AsyncClient,
    endpoint: str,
    ids: list[int],
    semaphore: asyncio.Semaphore,
    batch_size: int = AGRO_OP_BATCH_SIZE,
) -> list[dict]:
    """POST .../{endpoint}/mass_request батчами по batch_size id — батчи уходят
    параллельно (ограничены semaphore), а не по очереди."""
    if not ids:
        return []
    url = f"{BASE_URL}/{endpoint}/mass_request"

    async def _one_batch(batch: list[int]) -> list[dict]:
        async with semaphore:
            r = await client.post(url, json={"data": batch})
        try:
            r.raise_for_status()
            body = r.json()
        except Exception as exc:
            print(f"    [{endpoint}/mass_request] батч из {len(batch)} id не удался: {exc}", flush=True)
            return []
        return body.get("data", [])

    batches = _chunked(ids, batch_size)
    batch_results = await asyncio.gather(*[_one_batch(b) for b in batches])
    out: list[dict] = []
    for res in batch_results:
        out.extend(res)
    return out


async def fetch_task_crop_via_agro_operation(
    task_ids: list[int],
    history_item_to_crop: dict[int, str],
) -> dict[int, str]:
    """
    Определяет культуру MachineTask через AgroOperation:
      MachineTask -> MachineTaskAgroOperationMappingItem -> AgroOperation -> HistoryItem -> Crop.
    Возвращает {machine_task_id: crop_name} только для заданий, у которых через эту
    связку нашлась РОВНО ОДНА однозначная культура. Если у задания несколько операций
    с разными культурами (или ни одной) — задание в результат не попадает, вызывающий
    код должен использовать резервную карту по полю (field_to_crop_general).
    """
    if not task_ids:
        return {}

    headers = {"X-User-Api-Token": API_TOKEN}
    semaphore = asyncio.Semaphore(FMI_SEMAPHORE_LIMIT)
    mtaomi_url = f"{BASE_URL}/machine_task_agro_operation_mapping_items"

    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        batches = _chunked(sorted(set(task_ids)), FMI_BATCH_SIZE)
        batch_results = await asyncio.gather(*[
            _fetch_all_pages_async(
                client, mtaomi_url,
                {"machine_task_id": ",".join(str(tid) for tid in batch)},
                semaphore,
            )
            for batch in batches
        ], return_exceptions=True)

        task_to_agro_ops: dict[int, set[int]] = defaultdict(set)
        failed = 0
        for batch, res in zip(batches, batch_results):
            if isinstance(res, BaseException):
                failed += len(batch)
                continue
            for item in res:
                tid = item.get("machine_task_id")
                aoid = item.get("agro_operation_id")
                if tid is not None and aoid is not None:
                    task_to_agro_ops[int(tid)].add(int(aoid))
        if failed:
            print(f"    [culture/agro_operation] не удалось получить mtaomi для {failed} заданий", flush=True)

        all_agro_op_ids = sorted({aoid for ids in task_to_agro_ops.values() for aoid in ids})
        agro_ops = await _fetch_mass_request_async(client, "agro_operations", all_agro_op_ids, semaphore)

    agro_op_to_history: dict[int, int] = {
        int(ao["id"]): int(ao["history_item_id"])
        for ao in agro_ops
        if ao.get("id") is not None and ao.get("history_item_id") is not None
    }

    task_crop: dict[int, str] = {}
    ambiguous = 0
    for tid, aoids in task_to_agro_ops.items():
        crops_found: set[str] = set()
        for aoid in aoids:
            hi_id = agro_op_to_history.get(aoid)
            if hi_id is None:
                continue
            crop = history_item_to_crop.get(hi_id)
            if crop:
                crops_found.add(crop)
        if len(crops_found) == 1:
            task_crop[tid] = next(iter(crops_found))
        elif len(crops_found) > 1:
            ambiguous += 1
    if ambiguous:
        print(
            f"    [culture/agro_operation] {ambiguous} заданий с неоднозначной культурой "
            f"через AgroOperation — для них используется резервная карта по полю",
            flush=True,
        )
    return task_crop


# Кукуруза на силос / на зерно — как 10_main_harvest.get_field_to_crop.
SILAGE_CORN_MOWING_WT_IDS: frozenset[int] = frozenset({74, 83, 84, 147, 149})
CORN_GRAIN_CROP = "Кукуруза на зерно"
CORN_SILAGE_CROP = "Кукуруза на силос"
_silage_corn_field_ids_cache: set[int] | None = None


def _get_silage_corn_field_ids(season_year: int) -> set[int]:
    """field_id полей с операцией «Скашивание» в текущем сезоне уборки."""
    global _silage_corn_field_ids_cache
    if _silage_corn_field_ids_cache is None:
        field_ids: set[int] = set()
        for wt_id in SILAGE_CORN_MOWING_WT_IDS:
            for op in get_all_from_id(
                "agro_operations", {"work_type_id": wt_id, "season": season_year},
            ):
                fid = op.get("field_id")
                if fid is not None:
                    field_ids.add(int(fid))
        _silage_corn_field_ids_cache = field_ids
        print(f"    кукуруза на силос (Скашивание): {len(field_ids)} полей", flush=True)
    return _silage_corn_field_ids_cache


def _resolve_corn_subtype(
    crop_name: str, field_id: int | None, silage_ids: set[int],
) -> str:
    if not crop_name or "кукуруза" not in crop_name.casefold():
        return crop_name
    if field_id is None:
        return crop_name
    return CORN_SILAGE_CROP if int(field_id) in silage_ids else CORN_GRAIN_CROP


def load_crop_reference(report_date: date) -> tuple[dict[int, str], dict[int, str]]:
    """
    (history_item_to_crop, field_to_crop_general):
      - history_item_to_crop — HistoryItem.id -> название культуры, нужен для связки
        через AgroOperation (fetch_task_crop_via_agro_operation);
      - field_to_crop_general — резервная карта field_id -> культура, используется,
        когда через AgroOperation культуру задания однозначно определить не удалось.

    Логика уборки — как 10_main_harvest.get_field_to_crop: history_items только
    за year=REPORT_DATE.year (не year+1), разбивка кукурузы по «Скашиванию».
    Раньше грузили year+1 и через build_field_to_crop_general — из‑за этого на
    уборке 2026 всплывала «Соя» (посев следующего сезона).
    """
    print("  Загружаем crops...", flush=True)
    crops_map = {c["id"]: c.get("name") or c.get("title", "") for c in get_all_from_id("crops")}

    season_year = report_date.year
    print(f"  Загружаем history_items year={season_year} (сезон уборки, как 10_main_harvest)...", flush=True)
    history_items = get_all_from_id("history_items", {"year": season_year})
    print(f"    history_items year={season_year}: {len(history_items)} записей", flush=True)

    silage_ids = _get_silage_corn_field_ids(season_year)

    history_item_to_crop: dict[int, str] = {}
    field_to_crop_general: dict[int, str] = {}
    for hi in history_items:
        if not hi.get("active"):
            continue
        hi_id, crop_id, fid = hi.get("id"), hi.get("crop_id"), hi.get("field_id")
        if crop_id is None:
            continue
        cname = (crops_map.get(int(crop_id)) or "").strip()
        if not cname or cname in SKIP_CROPS:
            continue
        fid_i = int(fid) if fid is not None else None
        crop_resolved = _resolve_corn_subtype(cname, fid_i, silage_ids)
        if hi_id is not None:
            history_item_to_crop[int(hi_id)] = crop_resolved
        if fid_i is not None:
            field_to_crop_general[fid_i] = crop_resolved

    print(f"    {len(history_item_to_crop)} history_item -> культура (для связки через AgroOperation)")
    print(f"    {len(field_to_crop_general)} полей с культурой (year={season_year})")

    return history_item_to_crop, field_to_crop_general


def fetch_fmi(task_ids: set[int], report_date: date) -> list[dict]:
    """
    FMI пачками по machine_task_id (см. _fetch_fmi_batch_async) — вместо
    полного скана диапазона по created_at, запрашиваем сразу только нужные
    задания, пачками по FMI_BATCH_SIZE id за один запрос.

    Смена report_date пересекает полночь ([report_date-1 06:00 .. report_date 06:00)),
    поэтому запись work_timetable может начинаться и датой report_date-1, и датой
    report_date — проверяем обе, иначе часть записей ошибочно уйдёт в results_no_timetable
    и потеряется (если хотя бы одна запись совпала с "чистой" датой report_date).

    Если батч не нашёл вообще ничего — подстраховка: старый полный скан
    (_fetch_fmi_full_scan_fallback).
    """
    if not task_ids:
        return []
    result = asyncio.run(_fetch_fmi_batch_async(task_ids, report_date))
    if result:
        return result
    print("    [fmi] батч не нашёл записей, пробуем полный скан...", flush=True)
    return _fetch_fmi_full_scan_fallback(task_ids, report_date)


def fetch_fields() -> list[dict]:
    return get_all_from_id("fields")


def fetch_field_groups() -> list[dict]:
    return get_all("field_groups")


def fetch_machine_region_mappings() -> list[dict]:
    return get_all_from_id("machine_region_mapping_items")


# =============================================================================
# СВОДКА ПО АГРОФИРМАМ
# =============================================================================

def build_summary_report(
    report_date: date,
    folder_to_name: dict[int, str] | None,
    *,
    firms: dict[str, tuple[int, int]] | None = None,
    plan_rate: float = PLANNED_OUTPUT,
    own_count_work_group: str = SUMMARY_WORK_GROUP,
    tasks: list[dict],
    fmi_list: list[dict],
    field_to_fg: dict[int, int | None],
    fg_to_folder: dict[int, int | None],
    field_to_firm_folder: dict[int, int],
) -> pd.DataFrame:
    """Таблица «ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ».

    firms — подмножество AGROFIRMS (по умолчанию — все агрофирмы, лист «Сводка»).
    tasks / fmi_list / field_to_fg / fg_to_folder — уже загружены в main (без
    повторных запросов к API, как collect_work_data в 2_main_ideal_compact).
    plan_rate — плановая выработка, га/сутки на 1 ед. техники; берётся из
    plans.PLAN_RATE_PER_GROUP по ключу нужной группы работ (по умолчанию —
    группа «5.0 Уборка»).
    own_count_work_group — ключ в plans.AVAILABLE_OWN для колонки
    «Всего собств. ... в наличии» (как в сводке 11), не подсчёт из region_mappings.
    """
    folder_to_name = folder_to_name or {}
    firms = firms if firms is not None else AGROFIRMS
    own_by_firm = dict(AVAILABLE_OWN.get(own_count_work_group) or {})
    task_to_machine = {t["id"]: t["machine_id"] for t in tasks}

    fmi_df = pd.DataFrame(fmi_list) if fmi_list else pd.DataFrame(
        columns=["machine_task_id", "field_id", "covered_area", "work_duration"]
    )
    if not fmi_df.empty:
        fmi_df["machine_id"] = fmi_df["machine_task_id"].map(task_to_machine)
        fmi_df = _assign_firm_folder_column(
            fmi_df, field_to_firm_folder, field_to_fg, fg_to_folder,
        )
        fmi_df["covered_area"] = pd.to_numeric(fmi_df["covered_area"], errors="coerce").fillna(0)
        fmi_df["work_duration"] = pd.to_numeric(fmi_df["work_duration"], errors="coerce").fillna(0)
        # Как в 11_main_harvest.py (`if cov <= 0: continue`): запись без реального
        # покрытия поля (covered_area <= 0) не считается фактической работой —
        # иначе такие машины лишний раз попадают в «Количество ... на полях»,
        # хотя фактически ничего не убрали за этот отрезок времени.
        fmi_df = fmi_df[fmi_df["covered_area"] > 0]
        try:
            from proverka.task_facts import bind_capture

            _bound = bind_capture()
        except Exception:
            _bound = None
        if _bound is not None and not fmi_df.empty:
            _folder_to_firm = {folder_id: name for name, (folder_id, _region) in firms.items()}
            _tasks_by_id = {t.get("id"): t for t in tasks}
            for rec in fmi_df.to_dict("records"):
                firm_name = _folder_to_firm.get(rec.get("folder_id"))
                if not firm_name:
                    continue
                task = _tasks_by_id.get(rec.get("machine_task_id")) or {}
                _mid = rec.get("machine_id")
                _bound[0](
                    group="5.0 Уборка",
                    firm=firm_name,
                    field_id=rec.get("field_id"),
                    task_id=rec.get("machine_task_id"),
                    machine_id=int(_mid) if _mid is not None and str(_mid) != "nan" else None,
                    ha=float(rec.get("covered_area") or 0),
                    season=str(task.get("season") or ""),
                )

    rows = []
    total_work_hours = 0.0
    for firm_name, (folder_id, _region_id) in firms.items():
        firm_fmi = fmi_df[fmi_df["folder_id"] == folder_id] if not fmi_df.empty else pd.DataFrame(
            columns=["machine_id", "covered_area", "work_duration"]
        )
        covered_area_sum = float(firm_fmi["covered_area"].sum())
        # work_duration в API — секунды периодов работы на полях (не календарные сутки).
        work_hours = float(firm_fmi["work_duration"].sum()) / 3600.0
        total_work_hours += work_hours
        harvesters_on_field = int(firm_fmi["machine_id"].nunique())
        # Парк «в наличии» — из справочника планов (AVAILABLE_OWN), как сводка 11.
        total_in_stock = int(own_by_firm.get(firm_name, 0))
        output_per_day = round(covered_area_sum / harvesters_on_field, 1) if harvesters_on_field else 0.0
        # Часовая = площадь / сумма часов работы периодов (не делить ещё на N комбайнов).
        hourly_per_combine = round(covered_area_sum / work_hours, 2) if work_hours else 0.0
        display_name = get_firm_display_name(firm_name, folder_id, folder_to_name)
        rows.append({
            "Агрофирмы": display_name,
            "Плановая выработка, га/сутки на 1 комбайн": plan_rate,
            "Количество комбайнов на полях": harvesters_on_field,
            "Убрано за сутки, га": round(covered_area_sum, 1),
            "Выработка за сутки, га/1 комбайн": output_per_day,
            "Часовая выработка на 1 комбайн, га": hourly_per_combine,
            "Всего собств. комбайнов в наличии": total_in_stock,
        })

    df = pd.DataFrame(rows)
    total_on_field = df["Количество комбайнов на полях"].sum()
    total_covered = df["Убрано за сутки, га"].sum()
    totals = {
        "Агрофирмы": "Итого",
        "Плановая выработка, га/сутки на 1 комбайн": plan_rate,
        "Количество комбайнов на полях": total_on_field,
        "Убрано за сутки, га": round(total_covered, 1),
        "Выработка за сутки, га/1 комбайн": round(total_covered / total_on_field, 1) if total_on_field else 0.0,
        "Часовая выработка на 1 комбайн, га": (
            round(total_covered / total_work_hours, 2) if total_work_hours else 0.0
        ),
        "Всего собств. комбайнов в наличии": df["Всего собств. комбайнов в наличии"].sum(),
    }
    df = pd.concat([df, pd.DataFrame([totals])], ignore_index=True)
    return df


# =============================================================================
# ДЕТАЛЬНЫЙ ОТЧЁТ ПО ФИРМЕ (СОБСТВЕННЫЕ + НАЕМНЫЕ)
# =============================================================================

def build_global_field_group_map(*fmi_lists: list[dict]) -> dict[int, int]:
    """Сквозные номера групп: одно поле (field_id) → один номер на всей сводке.

    Нумерация 1..N по sorted(field_id) среди всех полей с уборкой за смену
    (зерно + овощи). В PDF может быть «186», у нас может быть «4» — важна логика:
    комбайны на одном поле → один номер; одна машина → один номер на все строки
  (все культуры); собств. и наёмные на том же поле → тот же номер.
    """
    field_ids: set[int] = set()
    for fmi_list in fmi_lists:
        for rec in fmi_list or ():
            try:
                area = float(rec.get("covered_area") or 0)
            except (TypeError, ValueError):
                area = 0.0
            if area <= 0:
                continue
            fid = rec.get("field_id")
            if fid is None:
                continue
            try:
                field_ids.add(int(fid))
            except (TypeError, ValueError):
                continue
    return {fid: i + 1 for i, fid in enumerate(sorted(field_ids))}


# Минимальная площадь поля (га) для отдельной строки: меньшие фрагменты FMI
# (шум GPS на границе поля) сливаются в основное поле пары (машина, культура).
MIN_FIELD_AREA_HA = 1.0


def _collapse_small_field_fragments(agg: pd.DataFrame) -> pd.DataFrame:
    """Сливает мелкие field_id-фрагменты в основное поле (машина, культура).

    Без этого появляются строки 0.1–0.3 га с урожайностью 50+ т/га и дубли
    комбайна на одной культуре (PDF-17 vs PDF-16).
    """
    if agg.empty or len(agg) <= 1:
        return agg
    sum_cols = ("covered_area", "work_duration", "work_distance", "covered_area_by_track")
    parts: list[pd.DataFrame] = []
    for _, sub in agg.groupby(["machine_id", "crop"], sort=False):
        if len(sub) == 1:
            parts.append(sub)
            continue
        sub = sub.copy()
        primary_fid = sub.loc[sub["covered_area"].idxmax(), "field_id"]
        small = sub[sub["covered_area"] < MIN_FIELD_AREA_HA]
        keep = sub[sub["covered_area"] >= MIN_FIELD_AREA_HA].copy()
        if small.empty:
            parts.append(sub)
            continue
        if keep.empty:
            merged = sub.iloc[[0]].copy()
            merged["field_id"] = primary_fid
            for col in sum_cols:
                merged[col] = sub[col].sum()
            merged["implement_width"] = sub["implement_width"].max()
            parts.append(merged)
            continue
        if primary_fid in keep["field_id"].values:
            target_idx = keep.index[keep["field_id"] == primary_fid][0]
        else:
            target_idx = keep["covered_area"].idxmax()
        for col in sum_cols:
            keep.loc[target_idx, col] = keep.loc[target_idx, col] + small[col].sum()
        keep.loc[target_idx, "implement_width"] = max(
            float(keep.loc[target_idx, "implement_width"]),
            float(small["implement_width"].max()),
        )
        parts.append(keep)
    return pd.concat(parts, ignore_index=True)


def _reassign_spurious_crop_fragments(
    firm_fmi: pd.DataFrame,
    namolot_index: "_NamolotMassIndex",
    machine_info: dict[int, dict],
    firm_folder_id: int,
) -> pd.DataFrame:
    """Переносит FMI с ошибочной культурой в основную (Namolot vs Cropwise).

    Типичный случай: комбайн убирает рапс яровой, GPS-шум на границе поля даёт
    0.1–0.3 га с культурой поля из сезонной карты (рапс озимый) — без слияния
    в таблице две строки на одну машину.
    """
    if firm_fmi.empty or "crop" not in firm_fmi.columns:
        return firm_fmi
    df = firm_fmi.copy()
    for mid in df["machine_id"].dropna().unique():
        try:
            mid_i = int(mid)
        except (TypeError, ValueError):
            continue
        reg_num = (machine_info.get(mid_i) or {}).get("registration_number")
        nm_crops = namolot_index.crops_for_firm(reg_num, firm_folder_id)
        if not nm_crops:
            continue
        nm_norm = {_normalize_crop_name(c) for c in nm_crops}
        mask = df["machine_id"] == mid
        crop_areas = df.loc[mask].groupby("crop")["covered_area"].sum()
        confirmed = [c for c in crop_areas.index if _normalize_crop_name(c) in nm_norm]
        if not confirmed:
            continue
        primary = max(confirmed, key=lambda c: float(crop_areas[c]))
        for crop, area in crop_areas.items():
            if _normalize_crop_name(crop) in nm_norm:
                continue
            if float(area) < MIN_FIELD_AREA_HA:
                df.loc[mask & (df["crop"] == crop), "crop"] = primary
    return df


def build_own_table(firm_folder_id: int, report_date: date, harvesters: list[dict],
                    harvester_ids: set[int], task_to_machine: dict, fmi_list: list[dict],
                    field_to_fg: dict, fg_to_folder: dict,
                    task_to_implement: dict | None = None,
                    implements_dict: dict | None = None,
                    task_crop_via_operation: dict[int, str] | None = None,
                    field_to_crop_general: dict[int, str] | None = None,
                    field_to_group: dict[int, int] | None = None,
                    field_to_firm_folder: dict[int, int] | None = None,
                    field_name_to_firm_folder: dict[str, int] | None = None) -> tuple[pd.DataFrame, list[int]]:
    """
    Таблица СОБСТВЕННЫЕ по комбайнам.
    Данные — из API (machine_task_field_mapping_items, machines), не вручную.
    Группа = комбайны, работавшие на одном поле (одна группа — один цвет).
    field_to_group — сквозная карта field_id→номер группы (build_global_field_group_map).
    task_to_implement / implements_dict — для расчёта «Коэффициент использования
    ширины захвата, %» (как в 3_main_real.py): ширина берётся из implements по
    implement_id задания, привязанного к каждой FMI-записи.

    task_crop_via_operation / field_to_crop_general — определение культуры для
    каждой FMI-записи (см. fetch_task_crop_via_agro_operation / load_crop_reference):
    один и тот же комбайн может за смену убирать две разные культуры, поэтому
    группировка по (machine_id, культура); номер группы — по полю с max площадью
    для этой пары. Если crop-параметры не переданы, «Культура» = «—».

    Возвращает (DataFrame, номера групп по строкам для подсветки колонки «Номер группы»).
    """
    task_to_implement = task_to_implement or {}
    implements_dict = implements_dict or {}
    task_crop_via_operation = task_crop_via_operation or {}
    field_to_crop_general = field_to_crop_general or {}
    field_to_firm_folder = field_to_firm_folder or {}
    field_name_to_firm_folder = field_name_to_firm_folder or {}
    machine_info = {m["id"]: m for m in harvesters}
    fmi_df = pd.DataFrame(fmi_list) if fmi_list else pd.DataFrame(
        columns=["machine_task_id", "field_id", "covered_area", "work_duration",
                 "work_distance", "covered_area_by_track"]
    )
    if fmi_df.empty:
        return pd.DataFrame(columns=OWN_COLUMNS), []

    fmi_df["machine_id"] = fmi_df["machine_task_id"].map(task_to_machine)
    fmi_df = _assign_firm_folder_column(
        fmi_df, field_to_firm_folder, field_to_fg, fg_to_folder,
    )
    fmi_df["covered_area"] = pd.to_numeric(fmi_df["covered_area"], errors="coerce").fillna(0)
    fmi_df["work_duration"] = pd.to_numeric(fmi_df["work_duration"], errors="coerce").fillna(0)
    fmi_df["work_distance"] = pd.to_numeric(fmi_df.get("work_distance", 0), errors="coerce").fillna(0)
    fmi_df["covered_area_by_track"] = pd.to_numeric(
        fmi_df.get("covered_area_by_track", 0), errors="coerce"
    ).fillna(0)
    # Ширина захвата — из implements через implement_id задания, к которому относится
    # эта FMI-запись (machine_task_id -> implement_id -> implements.width).
    fmi_df["implement_width"] = fmi_df["machine_task_id"].map(task_to_implement).map(
        lambda iid: float((implements_dict.get(iid) or {}).get("width") or 0)
    )

    # Как на листе «Сводка»: без реального покрытия (covered_area <= 0) не считаем.
    fmi_df = fmi_df[fmi_df["covered_area"] > 0]

    firm_fmi = fmi_df[fmi_df["folder_id"] == firm_folder_id]
    if firm_fmi.empty:
        return pd.DataFrame(columns=OWN_COLUMNS), []

    # Ограничение по набору машин (собственные комбайны / наёмные / вся техника).
    if harvester_ids is not None:
        firm_fmi = firm_fmi[firm_fmi["machine_id"].isin(harvester_ids)]
        if firm_fmi.empty:
            return pd.DataFrame(columns=OWN_COLUMNS), []

    # Культура каждой FMI-записи: по полю (как 10_main_harvest), AgroOperation — запасной
    # путь, если для field_id культуры нет. Раньше приоритет был у AgroOperation на всё
    # задание — тогда одна культура «перекрывала» другое поле того же machine_task
    # (в т.ч. ошибочная «Соя» с year+1).
    if task_crop_via_operation or field_to_crop_general:
        def _row_crop(r) -> str:
            crop = field_to_crop_general.get(r["field_id"])
            if crop:
                return crop
            return task_crop_via_operation.get(r["machine_task_id"]) or "—"
        firm_fmi = firm_fmi.copy()
        firm_fmi["crop"] = firm_fmi.apply(_row_crop, axis=1)
    else:
        firm_fmi = firm_fmi.copy()
        firm_fmi["crop"] = "—"

    # Группа = поле: сквозной номер field_to_group[field_id]. Строка — (машина, культура, поле).
    if field_to_group is None:
        fields_ordered = sorted(firm_fmi["field_id"].unique())
        field_to_group = {fid: i + 1 for i, fid in enumerate(fields_ordered)}

    def _field_group_num(fid) -> int:
        try:
            fid_i = int(fid)
        except (TypeError, ValueError):
            return 1
        return field_to_group.get(fid_i, field_to_group.get(fid, 1))

    namolot_index = _build_namolot_signature_index(report_date, field_name_to_firm_folder)
    firm_fmi = _reassign_spurious_crop_fragments(
        firm_fmi, namolot_index, machine_info, firm_folder_id,
    )

    # Сначала (машина, культура, поле), сливаем GPS-фрагменты < MIN_FIELD_AREA_HA,
    # затем агрегируем до строк таблицы.
    field_agg = firm_fmi.groupby(["machine_id", "crop", "field_id"]).agg(
        covered_area=("covered_area", "sum"),
        work_duration=("work_duration", "sum"),
        work_distance=("work_distance", "sum"),
        covered_area_by_track=("covered_area_by_track", "sum"),
        implement_width=("implement_width", "max"),
    ).reset_index()
    field_agg = _collapse_small_field_fragments(field_agg)

    # Разбивка по (машина, культура) — как в эталонном PDF: одна строка на комбайн
    # и культуру. Номер группы — по полю с максимальной площадью для этой пары.
    mc_field_area = field_agg.groupby(["machine_id", "crop", "field_id"])["covered_area"].sum().reset_index()
    if not mc_field_area.empty:
        mc_primary_idx = mc_field_area.groupby(["machine_id", "crop"])["covered_area"].idxmax()
        machine_crop_primary_field: dict[tuple, int] = {}
        for row in mc_field_area.loc[mc_primary_idx].itertuples():
            try:
                machine_crop_primary_field[(row.machine_id, row.crop)] = int(row.field_id)
            except (TypeError, ValueError):
                continue
    else:
        machine_crop_primary_field = {}

    agg = field_agg.groupby(["machine_id", "crop"]).agg(
        covered_area=("covered_area", "sum"),
        work_duration=("work_duration", "sum"),
        work_distance=("work_distance", "sum"),
        covered_area_by_track=("covered_area_by_track", "sum"),
        implement_width=("implement_width", "max"),
    ).reset_index()

    rows = []
    group_list: list[int] = []
    for i, row in enumerate(agg.itertuples(), start=1):
        mc_key = (row.machine_id, row.crop)
        fid_i = machine_crop_primary_field.get(mc_key, 0)
        grp = _field_group_num(fid_i)
        group_list.append(grp)
        m = machine_info.get(row.machine_id, {})
        name = m.get("name", f"Машина {row.machine_id}")
        reg_num = m.get("registration_number", "—")
        crop_name = row.crop
        mass_raw, is_fallback = namolot_index.mass_for(reg_num, crop_name, firm_folder_id)
        mass_t = round(mass_raw, 2)
        if is_fallback and mass_t:
            digits = _reg_main_digits(reg_num)
            variants = namolot_index.digit_letter_variants.get(digits, set())
            if len(variants) > 1:
                print(
                    f"  [namolot] ВНИМАНИЕ: {name!r} (гос.номер Cropwise "
                    f"{reg_num!r} без букв) сопоставлен по цифрам {digits} "
                    f"неоднозначно (варианты букв в Namolot: {sorted(variants)}) "
                    f"— масса {mass_t} т ({crop_name}) может быть неточной", flush=True,
                )
        work_hours = row.work_duration / 3600.0
        work_h = int(row.work_duration // 3600)
        work_m = int((row.work_duration % 3600) // 60)
        time_str = f"{work_h}:{work_m:02d}:00"
        avg_output = round(row.covered_area / work_hours, 2) if work_hours else 0.0
        avg_speed = round((row.work_distance / 1000) / work_hours, 2) if work_hours else 0.0
        # Коэффициент использования ширины захвата, % — как в 3_main_real.py:
        # (фактически убранная по треку площадь) / (путь, км * ширина захвата, м / 10) * 100.
        w_real = row.implement_width
        l_pole = row.work_distance / 1000
        s_chist = row.covered_area_by_track
        width_coef = round(s_chist / l_pole / w_real * 10 * 100) if w_real and l_pole else 0
        rows.append({
            "№ п/п": i,
            "Культура": crop_name,
            "Комбайн": name,
            "Гос. номер": reg_num,
            "Номер группы комбайнов на одном поле": str(grp),
            "Обработанная площадь, га": round(row.covered_area, 2),
            "Время работы в полях, час": time_str,
            "Средняя выр-ка, га/час": avg_output,
            "Средняя скорость на полях, км/ч": avg_speed,
            "Коэффициент использования конструкционной ширины захвата, %": width_coef,
            "Масса собранного урожая, т": mass_t,
            "Урожайность, т/га": round(mass_t / row.covered_area, 2) if row.covered_area and mass_t else 0.0,
        })

    if rows:
        # П.5–7 ТЗ: культура → номер группы → урожайность ↓.
        def _row_sort_key(item: tuple) -> tuple:
            row, grp = item
            y = row.get("Урожайность, т/га")
            try:
                y_f = float(y) if y is not None else 0.0
            except (TypeError, ValueError):
                y_f = 0.0
            try:
                grp_i = int(grp) if grp is not None else 0
            except (TypeError, ValueError):
                grp_i = 0
            return (
                _crop_sort_key(row["Культура"]),
                grp_i,
                -y_f,
                str(row.get("Гос. номер") or ""),
                str(row.get("Комбайн") or ""),
            )

        paired = sorted(zip(rows, group_list), key=_row_sort_key)
        rows = []
        group_list = []
        for i, (row, grp) in enumerate(paired, start=1):
            row["№ п/п"] = i
            rows.append(row)
            group_list.append(grp)

    # Диагностика: культуры по Cropwise (в этих строках) vs культуры, которые
    # Namolot зафиксировал для того же гос.номера за смену. Cropwise — основной
    # источник для подписи строки, Namolot используется только для массы по
    # совпавшей культуре; расхождение стоит проверить (либо культура в Namolot
    # называется иначе и её масса нигде не учтена, либо Cropwise ошибочно не
    # разглядел вторую культуру).
    if task_crop_via_operation or field_to_crop_general:
        cropwise_crops_by_reg: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            cropwise_crops_by_reg[r["Гос. номер"]].add(r["Культура"])
        for reg_num, cw_crops in cropwise_crops_by_reg.items():
            nm_crops = namolot_index.crops_for(reg_num)
            if not nm_crops:
                continue
            cw_norm = {_normalize_crop_name(c) for c in cw_crops}
            nm_norm = {_normalize_crop_name(c) for c in nm_crops}
            if cw_norm == nm_norm:
                continue
            only_nm = {c for c in nm_crops if _normalize_crop_name(c) not in cw_norm}
            only_cw = {c for c in cw_crops if _normalize_crop_name(c) not in nm_norm}
            msg = f"    [культура/расхождение] гос.номер {reg_num!r}: Cropwise={sorted(cw_crops)}, Namolot={sorted(nm_crops)}"
            if only_cw:
                msg += f"; только в Cropwise: {sorted(only_cw)}"
            if only_nm:
                msg += f"; только в Namolot (масса НЕ учтена ни в одной строке!): {sorted(only_nm)}"
            print(msg, flush=True)

    return pd.DataFrame(rows, columns=OWN_COLUMNS), group_list


# =============================================================================
# ЗАПИСЬ В EXCEL
# =============================================================================

def _clean_firm_display_name(name: str) -> str:
    """Убирает ООО, кавычки и лишние пробелы из названия агрофирмы."""
    s = str(name or "").strip()
    if not s:
        return s
    s = re.sub(r"(?i)\bооо\b", "", s)
    s = s.replace('"', "").replace("«", "").replace("»", "").replace("'", "")
    s = re.sub(r"\s+", " ", s).strip()
    if s.upper().startswith("АФ "):
        s = s[3:].strip()
    return s or str(name).strip()


def get_firm_display_name(firm_name: str, folder_id: int, folder_to_name: dict[int, str]) -> str:
    """Краткое название агрофирмы без ООО и кавычек."""
    if firm_name in FIRM_DISPLAY_NAMES:
        raw = FIRM_DISPLAY_NAMES[firm_name]
    else:
        raw = folder_to_name.get(folder_id, "").strip() or firm_name
    return _clean_firm_display_name(raw)


def write_firm_sheet(writer, firm_name: str, folder_id: int, folder_to_name: dict[int, str],
                    own_df: pd.DataFrame, hired_df: pd.DataFrame,
                    report_date: date, start_row: int,
                    group_list: list[int] | None = None) -> tuple[int, int | None]:
    """
    Записывает лист фирмы: заголовок, СОБСТВЕННЫЕ, при наличии данных — НАЕМНЫЕ.
    start_row — Excel row (1-based) для первой таблицы.
    group_list — номера групп для подсветки колонки «Номер группы» (одна группа — один цвет).
    Возвращает (row_own_header, row_hired_header); row_hired_header=None, если наёмных нет.
    """
    from openpyxl.utils.dataframe import dataframe_to_rows
    sheet_name = firm_name[:31]  # Excel limit
    ws = writer.book[sheet_name]

    # start_row — строка секции «СОБСТВЕННЫЕ»; заголовок таблицы — на следующей строке.
    row_own_header = start_row + 1
    for r_idx, row in enumerate(dataframe_to_rows(own_df, index=False, header=True), row_own_header):
        for c_idx, val in enumerate(row, 1):
            cell = ws.cell(row=r_idx, column=c_idx, value=val)

    # НАЕМНЫЕ — только если есть фактические строки (без пустых заглушек).
    if hired_df is None or hired_df.empty:
        return row_own_header, None

    row_own_last = row_own_header + len(own_df)
    row_hired_header = row_own_last + TABLE_GAP_ROWS + 1
    for r_idx, row in enumerate(dataframe_to_rows(hired_df, index=False, header=True), row_hired_header):
        for c_idx, val in enumerate(row, 1):
            ws.cell(row=r_idx, column=c_idx, value=val)

    return row_own_header, row_hired_header


# =============================================================================
# ЗАПУСК
# =============================================================================

def main():
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 260)
    pd.set_option("display.float_format", "{:.2f}".format)

    report_date = REPORT_DATE
    print(f"ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ — {report_date}\n", flush=True)
    print("Загружаем справочники...", flush=True)

    def _step(msg: str) -> None:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    # Справочники — один раз (как load_reference_data в 2_main_ideal_compact).
    _step("→ load_machines() ...")
    all_machines = load_machines()
    harvesters, hired_machines, harvester_ids, hired_ids, _ = split_machines(
        all_machines,
    )
    print(f"  Комбайнов (собств.): {len(harvesters)}", flush=True)
    print(f"  Наёмных машин (group {HIRED_GROUP_ID}): {len(hired_machines)}", flush=True)

    _step("→ load_geo_reference() ...")
    field_to_fg, fg_to_folder, folder_to_name, field_to_firm_folder, field_name_to_firm_folder = (
        load_geo_reference()
    )

    _step("→ get_all_from_id('implements') ...")
    implements_dict = {imp["id"]: imp for imp in get_all_from_id("implements")}
    print(f"  Implements: {len(implements_dict)}", flush=True)

    _step("→ load_crop_reference() ...")
    print("  Загружаем культуры (AgroOperation + резерв по полю)...", flush=True)
    history_item_to_crop, field_to_crop_general = load_crop_reference(report_date)

    print("\nСобираем данные по уборке...", flush=True)

    # Задания уборки: собственные комбайны + наёмные (группа 7).
    _step("→ fetch_machine_tasks() (зерновые) ...")
    task_machine_ids = harvester_ids | hired_ids
    tasks = fetch_machine_tasks(task_machine_ids, report_date)
    task_ids = {t["id"] for t in tasks}
    task_to_machine = {t["id"]: t["machine_id"] for t in tasks}
    task_to_implement = {t["id"]: t.get("implement_id") for t in tasks}

    _step("→ fetch_fmi() (зерновые) ...")
    fmi_list = fetch_fmi(task_ids, report_date)
    task_to_wt = {int(t["id"]): int(t["work_type_id"]) for t in tasks if t.get("work_type_id") is not None}
    fmi_list = _normalize_fmi_for_shift(fmi_list, report_date, task_to_wt)
    print(
        f"  Зерновые — заданий: {len(tasks)}, FMI: {len(fmi_list)}",
        flush=True,
    )

    _step("→ fetch_task_crop_via_agro_operation() (async) ...")
    task_crop_via_operation = asyncio.run(
        fetch_task_crop_via_agro_operation(list(task_ids), history_item_to_crop)
    )
    print(
        f"    культура через AgroOperation определена для "
        f"{len(task_crop_via_operation)} из {len(task_ids)} заданий",
        flush=True,
    )

    veg_tasks: list[dict] = []
    veg_fmi_list: list[dict] = []
    veg_task_to_machine: dict[int, int] = {}
    veg_task_to_implement: dict[int, int | None] = {}
    veg_task_crop_via_operation: dict[int, str] = {}
    if VEG_HARVEST_WORK_TYPE_IDS:
        _step("→ fetch_machine_tasks() + fetch_fmi() (овощи) ...")
        veg_tasks = fetch_machine_tasks(
            None, report_date, work_type_ids=VEG_HARVEST_WORK_TYPE_IDS,
        )
        veg_task_ids = {t["id"] for t in veg_tasks}
        veg_task_to_machine = {t["id"]: t["machine_id"] for t in veg_tasks}
        veg_task_to_implement = {t["id"]: t.get("implement_id") for t in veg_tasks}
        veg_fmi_list = fetch_fmi(veg_task_ids, report_date)
        veg_task_to_wt = {
            int(t["id"]): int(t["work_type_id"])
            for t in veg_tasks if t.get("work_type_id") is not None
        }
        veg_fmi_list = _normalize_fmi_for_shift(veg_fmi_list, report_date, veg_task_to_wt)
        print(f"  Овощи — заданий: {len(veg_tasks)}, FMI: {len(veg_fmi_list)}", flush=True)
        if veg_task_ids:
            _step("→ fetch_task_crop_via_agro_operation() (овощи) ...")
            veg_task_crop_via_operation = asyncio.run(
                fetch_task_crop_via_agro_operation(list(veg_task_ids), history_item_to_crop)
            )
            print(
                f"    культура (овощи) через AgroOperation определена для "
                f"{len(veg_task_crop_via_operation)} из {len(veg_task_ids)} заданий",
                flush=True,
            )

    _step("данные загружены, переходим к построению отчётов")

    # Сводка (названия фирм — из API group_folders или FIRM_DISPLAY_NAMES) — только зерновые.
    # «Количество ... на полях» и детальные листы фирм считают одни и те же комбайны
    # (machine_group 5/9): иначе на слайде 1 число завышается за счёт тракторов и
    # прочей техники на work_type уборки (пример: НурлатАгро 28 на сводке vs 20 в
    # таблице СОБСТВЕННЫЕ). «Всего ... в наличии» — тоже только комбайны.
    _step("→ build_summary_report() (зерновые) ...")
    summary = build_summary_report(
        report_date,
        folder_to_name,
        plan_rate=PLANNED_OUTPUT,
        own_count_work_group=SUMMARY_WORK_GROUP,
        tasks=tasks,
        fmi_list=fmi_list,
        field_to_fg=field_to_fg,
        fg_to_folder=fg_to_folder,
        field_to_firm_folder=field_to_firm_folder,
    )
    print("\n=== СВОДКА (зерновые) ===")
    print(summary.to_string(index=False))

    # Сводка по овощам — та же структура, но только VEG_HARVEST_FIRM.
    _step("→ build_summary_report() (овощи) ...")
    summary_veg = build_summary_report(
        report_date,
        folder_to_name,
        firms={VEG_HARVEST_FIRM: AGROFIRMS[VEG_HARVEST_FIRM]},
        plan_rate=PLAN_RATE_PER_GROUP[VEG_HARVEST_GROUP_NAME],
        own_count_work_group=VEG_HARVEST_GROUP_NAME,
        tasks=veg_tasks,
        fmi_list=veg_fmi_list,
        field_to_fg=field_to_fg,
        fg_to_folder=fg_to_folder,
        field_to_firm_folder=field_to_firm_folder,
    ).rename(columns=VEG_SUMMARY_COLUMN_RENAME)
    print("\n=== СВОДКА, ОВОЩИ ===")
    print(summary_veg.to_string(index=False))

    # Детальные таблицы по фирмам
    _step("→ построение листов по фирмам ...")
    # Namolot индексируется в build_own_table, но ExcelWriter открывается раньше —
    # при read-only cwd падение на Writer обрывает запуск до первого build_own_table
    # и в консоли не появляется ни одной строки [namolot]. Прогреваем индекс заранее.
    _step("→ индекс Namolot за смену ...")
    _build_namolot_signature_index(report_date, field_name_to_firm_folder)

    field_to_group = build_global_field_group_map(fmi_list, veg_fmi_list)
    print(
        f"  Сквозные группы полей: {len(field_to_group)} полей "
        f"(зерно FMI={len(fmi_list)}, овощи FMI={len(veg_fmi_list)})",
        flush=True,
    )

    output_file = f"harvester_report_by_firm_{report_date}.xlsx"
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Сводка", index=False, startrow=0)
        ws = writer.book["Сводка"]
        style_report14_summary_sheet(
            ws, start_row=1, n_data_rows=len(summary), n_cols=len(summary.columns),
        )

        summary_veg.to_excel(writer, sheet_name=VEG_SUMMARY_SHEET_NAME, index=False, startrow=0)
        ws_veg = writer.book[VEG_SUMMARY_SHEET_NAME]
        style_report14_summary_sheet(
            ws_veg, start_row=1, n_data_rows=len(summary_veg), n_cols=len(summary_veg.columns),
        )
        print(f"  Лист: {VEG_SUMMARY_SHEET_NAME} (PDF: Уборка овощей)")

        for firm_name, (folder_id, _) in AGROFIRMS.items():
            own_df, group_list = build_own_table(
                folder_id, report_date, harvesters, harvester_ids,
                task_to_machine, fmi_list, field_to_fg, fg_to_folder,
                task_to_implement, implements_dict,
                task_crop_via_operation=task_crop_via_operation,
                field_to_crop_general=field_to_crop_general,
                field_to_group=field_to_group,
                field_to_firm_folder=field_to_firm_folder,
                field_name_to_firm_folder=field_name_to_firm_folder,
            )
            hired_df, hired_group_list = build_own_table(
                folder_id, report_date, hired_machines, hired_ids,
                task_to_machine, fmi_list, field_to_fg, fg_to_folder,
                task_to_implement, implements_dict,
                task_crop_via_operation=task_crop_via_operation,
                field_to_crop_general=field_to_crop_general,
                field_to_group=field_to_group,
                field_to_firm_folder=field_to_firm_folder,
                field_name_to_firm_folder=field_name_to_firm_folder,
            )
            sheet_name = firm_name[:31]
            writer.book.create_sheet(sheet_name)
            row_own, row_hired = write_firm_sheet(
                writer, firm_name, folder_id, folder_to_name,
                own_df, hired_df, report_date, start_row=1,
                group_list=group_list,
            )
            style_report14_firm_sheet(
                writer.book[sheet_name],
                row_own, len(own_df),
                row_hired, len(hired_df),
                group_list,
                own_n_cols=len(own_df.columns),
                hired_n_cols=len(hired_df.columns) if not hired_df.empty else None,
                hired_group_list=hired_group_list,
            )
            print(
                f"  Лист: {sheet_name} "
                f"(собств. {len(own_df)}, наёмн. {len(hired_df)})"
            )

        # ------------------------------------------------------------
        # Лист «Уборка, овощи» (картофель, лук) — только VEG_HARVEST_FIRM,
        # по аналогии с листами по фирмам, но задания только группы 5.4.
        # ------------------------------------------------------------
        if VEG_HARVEST_WORK_TYPE_IDS:
            veg_folder_id, _ = AGROFIRMS[VEG_HARVEST_FIRM]

            veg_own_machines = [
                m for m in all_machines
                if m.get("machine_group_id") != HIRED_GROUP_ID
            ]
            veg_own_ids = {m["id"] for m in veg_own_machines}
            veg_hired_machines = [
                m for m in all_machines
                if m.get("machine_group_id") == HIRED_GROUP_ID
            ]
            veg_hired_ids = {m["id"] for m in veg_hired_machines}

            veg_own_df, veg_group_list = build_own_table(
                veg_folder_id, report_date, veg_own_machines, veg_own_ids,
                veg_task_to_machine, veg_fmi_list, field_to_fg, fg_to_folder,
                veg_task_to_implement, implements_dict,
                task_crop_via_operation=veg_task_crop_via_operation,
                field_to_crop_general=field_to_crop_general,
                field_to_group=field_to_group,
                field_to_firm_folder=field_to_firm_folder,
                field_name_to_firm_folder=field_name_to_firm_folder,
            )
            veg_hired_df, veg_hired_group_list = build_own_table(
                veg_folder_id, report_date, veg_hired_machines, veg_hired_ids,
                veg_task_to_machine, veg_fmi_list, field_to_fg, fg_to_folder,
                veg_task_to_implement, implements_dict,
                task_crop_via_operation=veg_task_crop_via_operation,
                field_to_crop_general=field_to_crop_general,
                field_to_group=field_to_group,
                field_to_firm_folder=field_to_firm_folder,
                field_name_to_firm_folder=field_name_to_firm_folder,
            )
            _veg_rename = {
                "Комбайн": "Техника",
                "Номер группы комбайнов на одном поле": "Номер группы техники на одном поле",
            }
            veg_own_df = veg_own_df.rename(columns=_veg_rename)
            veg_hired_df = veg_hired_df.rename(columns=_veg_rename)
            veg_sheet_name = VEG_DETAIL_SHEET_NAME
            writer.book.create_sheet(veg_sheet_name)
            veg_row_own, veg_row_hired = write_firm_sheet(
                writer, veg_sheet_name, veg_folder_id, folder_to_name,
                veg_own_df, veg_hired_df, report_date, start_row=1,
                group_list=veg_group_list,
            )
            style_report14_firm_sheet(
                writer.book[veg_sheet_name],
                veg_row_own, len(veg_own_df),
                veg_row_hired, len(veg_hired_df),
                veg_group_list,
                own_n_cols=len(veg_own_df.columns),
                hired_n_cols=len(veg_hired_df.columns) if not veg_hired_df.empty else None,
                hired_group_list=veg_hired_group_list,
            )
            print(
                f"  Лист: {veg_sheet_name} (PDF: Уборка овощи) "
                f"(собств. {len(veg_own_df)}, наёмн. {len(veg_hired_df)})"
            )
        else:
            print(
                "  Уборка овощей: work_type_id группы "
                f"«{VEG_HARVEST_GROUP_NAME}» не найдены в WORK_GROUPS — лист пропущен"
            )

    print(f"\nСохранено: {output_file}")


if __name__ == "__main__":
    main()