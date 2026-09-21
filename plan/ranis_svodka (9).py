"""
Генератор аналитического отчёта «Информация по ВПР за сутки».

Источники данных:
  1. CropWise API (https://operations.cropwise.com/api/v3) — данные о работах,
     полях, машинах (логика из 2_main_ideal.py)
  2. Downtime API (https://mech.chistai.ru/v1/downtime) — данные о простоях

Результат: файл Word (.docx) с отчётом за указанную дату.

Использование:
    python ranis_svodka.py [YYYY-MM-DD]
    REPORT_DATE в окружении = TODAY сводки 2 (дата платформы); сутки в отчёте 23 = REPORT_DATE − 1
    (последняя колонка сводки 2). Явно задать сутки: SVODKI_ANALYTICS_DATE=YYYY-MM-DD.
    Альтернатива штампу: SVODKI_REPORT_STAMP=YYYY-MM-DD.
    Сервер может задать SVODKI_R23_FERTILIZER_TRANSPORT_MACHINES — число машин подвоза/загрузки удобрений для текста сводки.
    SVODKI_R23_EDITOR_BODY_SIDECAR — путь к UTF-8 файлу: непустой текст полностью заменяет автоматические абзацы
    по работам; блок «простои» в конце документа по-прежнему строится из API.
    SVODKI_R23_DOC_REBUILD_ONLY=1 — только пересборка: тот же ручной текст + простои, без запросов Cropwise
    (ожидается непустой SVODKI_R23_EDITOR_BODY_SIDECAR).
    SVODKI_R23_WORK_SNAPSHOT_OUT — при автогенерации: UTF-8 файл, куда записать текст блока по работам (без раздела простоев).
    Цифры по работам = снимок сводки 2 (SVODKI_R23_WORK_DATA_BUNDLE_IN). Без bundle — только при
    SVODKI_R23_ALLOW_LIVE_CROPWISE=1 (медленно) или SVODKI_R23_PREFETCH=1 (prefetch сводки 2).
    Prefetch с другой даты игнорируется (см. _validate_or_clear_prefetch).

Зависимости:
    pip install requests httpx python-docx openpyxl
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Cropwise: prefetch/API cache до requests (SVODKI_PREFETCH_DATA_PATH — снимок как у сводки 2).
def _svodki_bootstrap_api_hooks() -> None:
    sc = Path(__file__).resolve().parent / "sitecustomize.py"
    if not sc.is_file():
        return
    import importlib.util

    mod_name = "_svodki_sitecustomize_hooks"
    mod = sys.modules.get(mod_name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(mod_name, sc)
        if spec is None or spec.loader is None:
            return
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    install = getattr(mod, "install_svodki_api_hooks", None)
    if callable(install):
        install()


_svodki_bootstrap_api_hooks()

import httpx
import requests
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH


def _r23_fertilizer_transport_machine_count(fallback: int) -> int:
    """runpy init_globals `_SVODKI_R23_MACHINES`, файл в cwd, SIDECAR, env; иначе fallback из Cropwise."""
    g = globals()
    gv = g.get("_SVODKI_R23_MACHINES")
    if isinstance(gv, int) and 0 <= gv <= 999_999:
        return gv
    try:
        p = Path.cwd() / ".r23_fertilizer_transport_machines"
        if p.is_file():
            s = p.read_text(encoding="utf-8").strip()
            if s.isdigit():
                n = int(s)
                if 0 <= n <= 999_999:
                    return n
    except OSError:
        pass
    side = (os.environ.get("SVODKI_R23_MACHINES_SIDECAR") or "").strip()
    if side:
        try:
            p = Path(side)
            if p.is_file():
                s = p.read_text(encoding="utf-8").strip()
                if s.isdigit():
                    n = int(s)
                    if 0 <= n <= 999_999:
                        return n
        except OSError:
            pass
    raw = os.environ.get("SVODKI_R23_FERTILIZER_TRANSPORT_MACHINES")
    if raw is None:
        return fallback
    s = str(raw).strip()
    if not s:
        return fallback
    try:
        n = int(s)
    except ValueError:
        return fallback
    if n < 0:
        return fallback
    return n


# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

# Токен/базу задаёт контейнер (CROPWISE_API_TOKEN, CROPWISE_BASE_URL).
_LEGACY_FALLBACK_TOKEN = "6DayNcBDr9BWEZgst1Dy"
CROPWISE_TOKEN = (
    (os.environ.get("CROPWISE_API_TOKEN") or "").strip()
    or (os.environ.get("API_TOKEN") or "").strip()
    or _LEGACY_FALLBACK_TOKEN
).strip()
CROPWISE_URL = (
    os.environ.get("CROPWISE_BASE_URL", "").strip().rstrip("/")
    or "https://operations.cropwise.com/api/v3"
)

DOWNTIME_API_KEY = (
    (os.environ.get("SVODKI_DOWNTIME_API_KEY") or "").strip()
    or (os.environ.get("DOWNTIME_API_KEY") or "").strip()
    or "sk_74220500d6ee8ea412496a62fecdfc5f09f8f922"
)
DOWNTIME_URL = (
    (os.environ.get("SVODKI_DOWNTIME_URL") or "").strip()
    or (os.environ.get("DOWNTIME_URL") or "").strip()
    or "https://mech.chistai.ru/v1/downtime"
)

def _first_iso_date_in_argv() -> date | None:
    """Первый аргумент вида YYYY-MM-DD (игнорируя --diag и прочие флаги)."""
    for a in sys.argv[1:]:
        if a.startswith("-"):
            continue
        try:
            return date.fromisoformat(a.strip())
        except ValueError:
            continue
    return None


def _r23_expected_prefetch_date() -> str:
    """Дата в prefetch JSON должна совпадать с сутками отчёта (как TODAY у сводки 2)."""
    return REPORT_DATE.isoformat()


def _validate_or_clear_prefetch() -> None:
    """Prefetch сводки 2 принимаем только если report_date совпадает с выбранными сутками."""
    path_raw = (os.environ.get("SVODKI_PREFETCH_DATA_PATH") or "").strip()
    if not path_raw:
        return
    try:
        p = Path(path_raw)
        if not p.is_file():
            print("[r23] Prefetch не найден — запросы в Cropwise напрямую.", flush=True)
            os.environ.pop("SVODKI_PREFETCH_DATA_PATH", None)
            os.environ.pop("SVODKI_PREFETCH_STRICT", None)
            return
        import json

        data = json.loads(p.read_text(encoding="utf-8"))
        pf_date = str(data.get("report_date") or "").strip()
        expected = _r23_expected_prefetch_date()
        if pf_date and pf_date != expected:
            print(
                f"[r23] Prefetch за {pf_date} не совпадает с сутками {expected} — сброс.",
                flush=True,
            )
            os.environ.pop("SVODKI_PREFETCH_DATA_PATH", None)
            os.environ.pop("SVODKI_PREFETCH_STRICT", None)
            return
        print(
            f"[r23] Данные по работам из prefetch сводки 2 за {pf_date or expected}.",
            flush=True,
        )
    except Exception as exc:
        print(f"[r23] Ошибка проверки prefetch ({exc}) — сброс.", flush=True)
        os.environ.pop("SVODKI_PREFETCH_DATA_PATH", None)
        os.environ.pop("SVODKI_PREFETCH_STRICT", None)


def _resolve_analytics_dates() -> tuple[date, date, date]:
    """
    (REPORT_DATE, PREV_DATE, REPORT_STAMP).
    Как сводка 2: REPORT_DATE в env = TODAY (штамп, дата платформы);
    сутки в тексте 23 = штамп − 1 (последняя колонка сводки 2).
    """
    platform_stamp: date | None = None
    raw_stamp = (os.environ.get("REPORT_DATE") or "").strip()
    if raw_stamp:
        try:
            platform_stamp = date.fromisoformat(raw_stamp)
        except ValueError:
            platform_stamp = None

    analytics_raw = (os.environ.get("SVODKI_ANALYTICS_DATE") or "").strip()
    if analytics_raw:
        try:
            report = date.fromisoformat(analytics_raw)
        except ValueError:
            report = None
        else:
            # Старый лаунчер ошибочно ставил SVODKI_ANALYTICS_DATE = штамп платформы.
            if platform_stamp is None or report != platform_stamp:
                prev = report - timedelta(days=1)
                stamp = report + timedelta(days=1)
                return report, prev, stamp

    cli = _first_iso_date_in_argv()
    if cli is not None:
        stamp = cli
    elif os.environ.get("SVODKI_REPORT_STAMP"):
        stamp = date.fromisoformat(os.environ["SVODKI_REPORT_STAMP"].strip())
    elif os.environ.get("REPORT_DATE"):
        stamp = date.fromisoformat(os.environ["REPORT_DATE"].strip())
    else:
        stamp = date.today()

    report = stamp - timedelta(days=1)
    prev = report - timedelta(days=1)
    return report, prev, stamp


REPORT_DATE, PREV_DATE, REPORT_STAMP = _resolve_analytics_dates()
START_DATE = date(2026, 3, 15)  # начало сезона; как в сводке 2
_season_start_raw = (
    os.environ.get("SVODKI_SEASON_START_DATE")
    or os.environ.get("SEASON_START_DATE")
    or ""
).strip()
if _season_start_raw:
    try:
        START_DATE = date.fromisoformat(_season_start_raw)
    except ValueError:
        pass

TZ_MSK = timezone(timedelta(hours=3))

# Фирмы / виды работ / культуры — из plans_1 (как у компактной сводки 2), чтобы площади и срезы API совпадали.
_script_dir = str(Path(__file__).resolve().parent)
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)
from plans_1 import (
    FIELD_NAME_FORCE_RULES,
    FIRM_ORDER,
    FIRM_TO_FOLDER,
    FOLDER_TO_FIRM,
    HIRED_GROUP_ID,
    OZ_RAPE_CROP_NAMES,
    SKIP_CROPS,
    WORK_GROUPS,
)

YR_RAPE_CROP_NAMES = {"рапс яровой"}
RAPE_CROP_NAMES = OZ_RAPE_CROP_NAMES | YR_RAPE_CROP_NAMES

# Короткие аббревиатуры для отчёта
FIRM_SHORT: dict[str, str] = {
    "АФ Чистопольская":           "АФЧ",
    "АгроРаздолье":               "АР",
    "Аксу-Агро":                  "АА",
    "НурлатАгро":                 "НА",
    "Дрожжаное Рассвет Агро":     "ДРА",
    "Актай":                      "Актай",
    "Лениногорская Птицефабрика": "ЛПФ",
}

WORK_LABEL: dict[str, str] = {
    "1.1 Внесение минеральных удобрений": "разбрасывание минеральных удобрений",
    "2.1 Боронование":                    "боронование",
    "2.9 Дискование":                     "дискование",
    "2.4 Вспашка":                        "вспашка",
    "2.11 Культивация":                   "культивация",
    "4.0 Посев":                          "посев зерновых культур",
    "4.1 Посев (овощи)":                  "посев овощи",
    "3.1 Опрыскивание":                   "опрыскивание",
    "2.12 Прикатывание":                  "прикатывание",
    "2.7 Гребнеобразование":              "гребнеобразование",
    "2.8 Глубокорыхление":                "глубокорыхление",
    "2.6 Фрезерование":                   "фрезерование",
    "5.4 Уборка картофеля, лука":         "уборка картофеля и лука",
    "5.0 Уборка":                         "уборка",
}
# Предложный падеж фразы после «на …» в блоке про технику (нельзя просто label + «е»).
WORK_LABEL_AFTER_NA: dict[str, str] = {
    "1.1 Внесение минеральных удобрений": "разбрасывании минеральных удобрений",
    "2.1 Боронование":                    "бороновании",
    "2.9 Дискование":                     "дисковании",
    "2.4 Вспашка":                        "вспашке",
    "2.11 Культивация":                   "культивации",
    "4.0 Посев":                          "посеве",
    "4.1 Посев (овощи)":                  "посеве (овощи)",
    "3.1 Опрыскивание":                   "опрыскивании",
    "2.12 Прикатывание":                  "прикатывании",
    "2.7 Гребнеобразование":              "гребнеобразовании",
    "2.8 Глубокорыхление":                "глубокорыхлении",
    "2.6 Фрезерование":                   "фрезеровании",
    "5.4 Уборка картофеля, лука":         "уборке картофеля и лука",
    "5.0 Уборка":                         "уборке",
}
# Фраза после «на …» в строке «Работало техники всего на …» (как в эталонном PDF).
WORK_LABEL_MACHINES_ON: dict[str, str] = {
    "1.1 Внесение минеральных удобрений": "разбрасывание минеральных удобрений",
    "2.1 Боронование":                    "бороновании",
    "2.9 Дискование":                     "дисковании",
    "2.4 Вспашка":                        "вспашке",
    "2.11 Культивация":                   "культивации",
    "4.0 Посев":                          "посеве",
    "4.1 Посев (овощи)":                  "посеве",
    "3.1 Опрыскивание":                   "опрыскивании",
    "2.12 Прикатывание":                  "прикатывании",
    "2.7 Гребнеобразование":              "гребнеобразовании",
    "2.8 Глубокорыхление":                "глубокорыхлении",
    "2.6 Фрезерование":                   "фрезеровании",
    "5.4 Уборка картофеля, лука":         "уборке картофеля и лука",
    "5.0 Уборка":                         "уборке",
}
# Глагол в начале абзаца: «Проводилось/Проводилась/Проводился …»
WORK_INTRO_VERB: dict[str, str] = {
    "1.1 Внесение минеральных удобрений": "Проводилось",
    "2.1 Боронование":                    "Проводилось",
    "2.9 Дискование":                     "Проводилось",
    "2.4 Вспашка":                        "Проводилась",
    "2.11 Культивация":                   "Проводилась",
    "4.0 Посев":                          "Проводился",
    "4.1 Посев (овощи)":                  "Проводился",
    "3.1 Опрыскивание":                   "Проводилось",
    "2.12 Прикатывание":                  "Проводилось",
    "2.7 Гребнеобразование":              "Проводилось",
    "2.8 Глубокорыхление":                "Проводилось",
    "2.6 Фрезерование":                   "Проводилось",
    "5.4 Уборка картофеля, лука":         "Проводилась",
    "5.0 Уборка":                         "Проводилась",
}

MU_GROUP = "1.1 Внесение минеральных удобрений"

SEMAPHORE_LIMIT = 5

# Машины с площадью ниже этого порога не учитываются в счётчиках техники,
# но их площадь суммируется в общий объём работ.
MIN_COUNT_AREA_HA = 10.0

# Группы операций, для которых порог MIN_COUNT_AREA_HA не применяется:
# все машины учитываются независимо от обработанной площади.
MIN_COUNT_AREA_EXEMPT_GROUPS: frozenset[str] = frozenset({
    "уборка овощей",
    "посев овощей",
    "гребнеобразование",
    "глубокорыхление",
    "вспашка",
    "уборка",
})

# Подтипы агрегатов для Варианта Б (machine_subtype из /machines)
SELF_PROPELLED_SUBTYPES = {
    "spreader", "self_propelled_spreader", "self_propelled_sprayer",
    "sprayer", "combine_harvester", "forage_harvester",
    "self_propelled_mower", "self_propelled_baler",
}
TRACTOR_SUBTYPES = {
    "tractor", "powerful_tractor", "energy_saturated_tractor",
}

# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def is_skip_crop(crop_name: str) -> bool:
    if not crop_name:
        return True
    if crop_name in SKIP_CROPS:
        return True
    if crop_name.lower().startswith("введение в оборот"):
        return True
    return False


def _norm_field_label(name: str) -> str:
    return (name or "").strip().casefold().replace(" ", "").replace("\u00a0", "")


def _firm_from_field_name(field_rec: dict) -> str | None:
    blob = _norm_field_label(
        f"{field_rec.get('name') or ''} {field_rec.get('title') or ''}"
    )
    if not blob:
        return None
    for needle, firm_key in FIELD_NAME_FORCE_RULES:
        if needle in blob:
            return firm_key
    return None


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


def _group_folder_name_firm_hints(group_folders: list[dict]) -> dict[int, str]:
    hints: dict[int, str] = {}
    for gf in group_folders:
        fid = gf.get("id")
        if fid is None:
            continue
        blob = _norm_field_label(f"{gf.get('name') or ''} {gf.get('title') or ''}")
        if not blob:
            continue
        if any(x in blob for x in ("лениногор", "птицефабрик", "лпф", "lpf")):
            hints[int(fid)] = "Лениногорская Птицефабрика"
    return hints


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


def _folder_id_chain(folder_id: int | None, parent_map: dict[int, int | None]) -> list[int]:
    if folder_id is None:
        return []
    out: list[int] = []
    cur = int(folder_id)
    seen: set[int] = set()
    for _ in range(64):
        if cur in seen:
            break
        seen.add(cur)
        out.append(cur)
        nxt = parent_map.get(cur)
        if nxt is None:
            break
        cur = int(nxt)
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


def seconds_to_hhmm(sec: int) -> str:
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"{h} ч {m:02d} мин."


def round_hours(sec: int) -> str:
    """Округляет секунды до часов с точностью 0,5."""
    h = sec / 3600
    rounded = round(h * 2) / 2
    if rounded == int(rounded):
        return f"{int(rounded)} ч."
    return f"{rounded} ч."


# =============================================================================
# HTTP-СЕССИЯ CropWise
# =============================================================================

cw_session = requests.Session()
cw_session.headers.update({"X-User-Api-Token": CROPWISE_TOKEN})


def get_all_from_id(endpoint: str, extra_params: dict | None = None) -> list[dict]:
    params  = dict(extra_params or {})
    results = []
    from_id = 0
    for _ in range(500):
        r = cw_session.get(
            f"{CROPWISE_URL}/{endpoint}",
            params={**params, "from_id": from_id, "limit": 1000},
            timeout=120,
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


# =============================================================================
# ЗАГРУЗКА СПРАВОЧНИКОВ CropWise (из 2_main_ideal.py)
# =============================================================================

def load_field_to_firm(fields_all, fgs_all, gfs_all) -> dict[int, str]:
    fgs_map    = {fg["id"]: fg for fg in fgs_all}
    parent_map = _folder_parent_map(gfs_all)
    root_ids   = set(FIRM_TO_FOLDER.values())
    gf_hints   = _group_folder_name_firm_hints(gfs_all)

    field_to_firm: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        fgid       = f.get("field_group_id")
        raw_folder = f.get("group_folder_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(fgid, {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None

        firm_key = None
        for cid in _folder_id_chain(rint, parent_map):
            if cid in gf_hints:
                firm_key = gf_hints[cid]
                break
        if firm_key is None:
            root = _resolve_to_holding_root(rint, parent_map, root_ids)
            if root is not None:
                firm_key = FOLDER_TO_FIRM.get(root)
        if firm_key:
            field_to_firm[int(fid)] = firm_key

    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        fk = _firm_from_field_name(f)
        if fk:
            field_to_firm[int(fid)] = fk

    return field_to_firm


def _load_available_field_ids_in_period(
    period_start: date,
    period_end:   date,
    ihi_all:      list[dict],
) -> set[int]:
    """Возвращает field_id у которых available=True хотя бы в один день периода."""
    available_ids: set[int] = set()
    for r in ihi_all:
        if not r.get("available"):
            continue
        fid = r.get("historyable_id")
        if fid is None:
            continue
        raw_start = r.get("event_start_at")
        raw_end   = r.get("event_end_at")
        try:
            ev_start = date.fromisoformat(str(raw_start)[:10]) if raw_start else None
        except ValueError:
            ev_start = None
        try:
            ev_end = date.fromisoformat(str(raw_end)[:10]) if raw_end else None
        except ValueError:
            ev_end = None
        if ev_start is None:
            continue
        if ev_start <= period_end and (ev_end is None or ev_end >= period_start):
            available_ids.add(int(fid))
    print(f"    Полей available=True в периоде {period_start}\u2013{period_end}: {len(available_ids)}")
    return available_ids


def load_reference_data():
    print("  Загружаем crops...")
    crops_map = {c["id"]: c.get("name") or c.get("title", "")
                 for c in get_all_from_id("crops")}

    _crop_year = REPORT_DATE.year
    print(f"  Загружаем history_items year={_crop_year}...")
    his_all = get_all_from_id("history_items", {"year": _crop_year})
    field_to_crop: dict[int, str] = {}
    for hi in his_all:
        fid     = hi.get("field_id")
        crop_id = hi.get("crop_id")
        active  = hi.get("active")
        if fid and crop_id and crop_id in crops_map and active:
            field_to_crop[fid] = crops_map[crop_id]

    crop_field_ids = set(field_to_crop.keys())
    print(f"    {len(crop_field_ids)} полей с культурой в сезоне {_crop_year}")

    print("  Загружаем fields / field_groups / group_folders...")
    fields_all = get_all_from_id("fields")
    fgs_all    = get_all_from_id("field_groups")
    gfs_all    = get_all_from_id("group_folders")

    field_area: dict[int, float] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        field_area[fid] = (f.get("tillable_area") or
                           f.get("calculated_area") or 0.0)

    field_to_firm_all = load_field_to_firm(fields_all, fgs_all, gfs_all)

    # Фильтруем по активности полей в периоде — как в эталоне
    print("  Загружаем inventory_history_items (архивный статус полей)...")
    ihi_all = get_all_from_id("inventory_history_items", {"historyable_type": "Field"})
    print(f"    Всего записей inventory_history_items: {len(ihi_all)}")
    period_available_ids = _load_available_field_ids_in_period(START_DATE, REPORT_DATE, ihi_all)
    field_to_firm = {
        fid: firm for fid, firm in field_to_firm_all.items()
        if fid in crop_field_ids and fid in period_available_ids
    }
    print(f"    {len(field_to_firm)} полей для факта (активны в периоде, с культурой)")

    print("  Загружаем machines...")
    machines_all = get_all_from_id("machines")
    machine_group: dict[int, int] = {
        m["id"]: (m.get("machine_group_id") or 0)
        for m in machines_all
    }
    # Полный словарь машин — нужен для Варианта Б (machine_subtype)
    machines_map: dict[int, dict] = {m["id"]: m for m in machines_all}

    # Машины МТК (транспортные средства, не учитываются в блоке простоев)
    machine_is_mtk: dict[int, bool] = {}
    for m in machines_all:
        name = (m.get("name") or m.get("title") or "").upper()
        machine_is_mtk[m["id"]] = "МТК" in name

    return field_to_crop, field_to_firm, field_area, machine_group, machines_map, machine_is_mtk, crops_map


# =============================================================================
# АСИНХРОННАЯ ЗАГРУЗКА — один клиент и один семафор на весь процесс
# =============================================================================

async def fetch_all_pages_async(
    client:    httpx.AsyncClient,
    url:       str,
    params:    dict,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    items, from_id = [], 0
    while True:
        async with semaphore:
            r = await client.get(url, params={**params, "from_id": from_id, "limit": 1000})
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


def _shift_window(end_time_raw) -> tuple | None:
    """
    Возвращает (shift_date, shift_start, shift_cutoff) для задания.
    Смена дня D = [D 06:00, D+1 06:00).
    Если end_time < D 06:00 → задание относится к смене D-1.
    """
    end_dt = parse_dt(end_time_raw)
    if end_dt is None:
        return None
    cal_date = end_dt.date()
    anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                        6, 0, 0, tzinfo=TZ_MSK)
    if end_dt < anchor:
        cal_date = cal_date - timedelta(days=1)
        anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                            6, 0, 0, tzinfo=TZ_MSK)
    return cal_date, anchor, anchor + timedelta(days=1)


async def _collect_one_day_async(
    report_date:   date,
    field_to_crop: dict[int, str],
    field_to_firm: dict[int, str],
    field_area:    dict[int, float],
    machine_group: dict[int, int],
    machines_map:  dict[int, dict],
    machine_is_mtk: dict[int, bool] | None,
    crops_map:     dict[int, str] | None,
) -> dict:
    DAY_START_HOUR = 6
    day_start = datetime(report_date.year, report_date.month, report_date.day,
                         DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    cutoff    = day_start + timedelta(days=1)

    # Сезон с START_DATE 06:00 — как 2_main_ideal_compact (TODAY = report_date).
    season_start_str = datetime(
        START_DATE.year, START_DATE.month, START_DATE.day,
        DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    ).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str       = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    headers   = {"X-User-Api-Token": CROPWISE_TOKEN}
    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    pool      = httpx.Limits(max_connections=60, max_keepalive_connections=30)

    url_tasks = f"{CROPWISE_URL}/machine_tasks"
    url_mtfmi = f"{CROPWISE_URL}/machine_task_field_mapping_items"

    result: dict[str, dict] = {}

    def _shift_window(end_time_raw):
        """(shift_date, shift_day_start, shift_cutoff) или None."""
        end_dt = parse_dt(end_time_raw)
        if end_dt is None:
            return None
        cal_date = end_dt.date()
        anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
        if end_dt < anchor:
            cal_date = cal_date - timedelta(days=1)
            anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                                DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
        return cal_date, anchor, anchor + timedelta(days=1)

    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:

        # ------------------------------------------------------------------
        # 1. Загружаем все задания за СЕЗОН по всем группам параллельно
        #    (как в fetch_day_stats_for_work_types: season_start → cutoff)
        # ------------------------------------------------------------------
        all_wt_ids = [wt_id for _, wt_ids in WORK_GROUPS for wt_id in wt_ids]

        season_results = await asyncio.gather(*[
            fetch_all_pages_async(client, url_tasks, {
                "work_type_id":   wt_id,
                "end_time_gt_eq": season_start_str,
                "end_time_lt":    cutoff_str,
            }, semaphore)
            for wt_id in all_wt_ids
        ])

        # Раскладываем по группам, дедублируем
        group_tasks_raw: dict[str, list[dict]] = {}
        wt_idx = 0
        for group_name, wt_ids in WORK_GROUPS:
            seen_ids: set[int] = set()
            tasks_raw: list[dict] = []
            for _ in wt_ids:
                for t in season_results[wt_idx]:
                    if t["id"] not in seen_ids:
                        seen_ids.add(t["id"])
                        tasks_raw.append(t)
                wt_idx += 1
            group_tasks_raw[group_name] = tasks_raw

        # ------------------------------------------------------------------
        # 2. Определяем today_task_ids и task_shift для каждой группы
        #    Логика как в fetch_day_stats_for_work_types:
        #      today = shift_ds >= day_start and shift_co <= cutoff
        # ------------------------------------------------------------------
        group_tasks:       dict[str, list[dict]]         = {}
        group_today_ids:   dict[str, set[int]]           = {}
        task_shift_info:   dict[int, tuple]              = {}   # tid → (shift_date, shift_ds, shift_co)
        all_task_ids:      list[int]                     = []
        seen_task_ids:     set[int]                      = set()

        for group_name, tasks_raw in group_tasks_raw.items():
            tasks_with_area = [t for t in tasks_raw if (t.get("covered_area") or 0) > 0]
            today_ids: set[int] = set()
            for t in tasks_with_area:
                sw = _shift_window(t.get("end_time"))
                if sw is None:
                    continue
                shift_date, shift_ds, shift_co = sw
                task_shift_info[t["id"]] = sw
                if shift_ds >= day_start and shift_co <= cutoff:
                    today_ids.add(t["id"])
                if t["id"] not in seen_task_ids:
                    seen_task_ids.add(t["id"])
                    all_task_ids.append(t["id"])
            group_tasks[group_name]     = tasks_with_area
            group_today_ids[group_name] = today_ids

        # ------------------------------------------------------------------
        # 3. Загружаем mtfmi для всех заданий одним параллельным gather
        # ------------------------------------------------------------------
        if all_task_ids:
            mtfmi_results = await asyncio.gather(*[
                fetch_all_pages_async(client, url_mtfmi,
                                      {"machine_task_id": tid}, semaphore)
                for tid in all_task_ids
            ])
            mtfmi_map: dict[int, list[dict]] = {
                tid: items for tid, items in zip(all_task_ids, mtfmi_results)
            }
        else:
            mtfmi_map = {}

    # ------------------------------------------------------------------
    # 4. Считаем площадь по каждой группе — полностью по аналогии с
    #    fetch_day_stats_for_work_types: сезон + сутки раздельно.
    # ------------------------------------------------------------------
    for group_name, all_tasks in group_tasks.items():
        if not all_tasks:
            continue

        today_task_ids = group_today_ids[group_name]

        # Суточная площадь по полям: covered_area_hourly h < cutoff, нет нижней границы.
        # Fallback на covered_area если hourly пустой (для посева — в отличие от report3).
        today_area_by_field: dict[int, float] = defaultdict(float)
        for tid in today_task_ids:
            for item in mtfmi_map.get(tid, []):
                fid    = item.get("field_id")
                hourly = item.get("covered_area_hourly") or []
                cov    = 0.0
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and h >= day_start and h < cutoff:
                        cov += entry[1] or 0
                if not hourly:
                    cov = float(item.get("covered_area") or 0.0)
                if fid and cov > 0:
                    today_area_by_field[int(fid)] += cov

        # Сезонная и суточная агрегация по фирмам
        season_daily_area:     dict[str, dict[str, float]]    = {f: defaultdict(float) for f in FIRM_ORDER}
        season_daily_machines: dict[str, dict[str, set[int]]] = {f: defaultdict(set)   for f in FIRM_ORDER}
        season_field_cov:      dict[str, dict[int, float]]    = {f: defaultdict(float) for f in FIRM_ORDER}
        machines_own:    defaultdict = defaultdict(set)
        machines_hired:  defaultdict = defaultdict(set)
        machines_sp:     defaultdict = defaultdict(set)   # Вариант Б: SELF_PROPELLED_SUBTYPES
        machines_tr:     defaultdict = defaultdict(set)   # Вариант Б: TRACTOR_SUBTYPES
        machines_no_sub: defaultdict = defaultdict(set)   # Вариант Б: subtype не определён
        machines_transport: defaultdict = defaultdict(set)  # МТК / подвоз по фирмам (только МУ)

        # Промежуточный накопитель суточной площади по машине за день отчёта:
        # machine_day_area[firm][mid] = суммарная площадь за report_date.
        # Используется для фильтра MIN_COUNT_AREA_HA — машина попадает
        # в счётчики own/hired только если её суточная площадь >= порога.
        # Для групп из MIN_COUNT_AREA_EXEMPT_GROUPS порог не применяется.
        machine_day_area:  dict[str, dict[int, float]] = {f: defaultdict(float) for f in FIRM_ORDER}
        machine_day_meta:  dict[str, dict[int, dict]]  = {f: {} for f in FIRM_ORDER}  # firm→mid→{grp,m_sub}

        # Флаг: применять ли порог 10 га для данной группы операций
        _exempt = group_name.strip().lower() in MIN_COUNT_AREA_EXEMPT_GROUPS

        for task in all_tasks:
            tid = task["id"]
            mid = task.get("machine_id")
            sw  = task_shift_info.get(tid)
            if sw is None:
                continue
            shift_date, shift_ds, shift_co = sw

            items = mtfmi_map.get(tid, [])

            # Площадь по смене через hourly < shift_co, без нижней границы.
            # Fallback на covered_area если hourly пустой.
            firm_items_map: dict[str, list[tuple]] = defaultdict(list)
            for item in items:
                fid = item.get("field_id")
                if not fid:
                    continue
                fid_int = int(fid)
                firm = field_to_firm.get(fid_int)
                if not firm:
                    continue
                crop = field_to_crop.get(fid_int, "Не определена")
                if is_skip_crop(crop):
                    continue
                hourly = item.get("covered_area_hourly") or []
                cov = 0.0
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and h < shift_co:
                        cov += entry[1] or 0
                if not hourly:
                    cov = float(item.get("covered_area") or 0.0)
                if cov > 0.5:
                    firm_items_map[firm].append((item, cov, crop))

            # Вариант Б: subtype берётся один раз на задание — как в delete.py
            m_rec   = machines_map.get(mid, {}) if mid else {}
            m_sub   = m_rec.get("machine_subtype") or "—"
            grp     = machine_group.get(mid, 0) if mid else 0

            for firm, fitems in firm_items_map.items():
                # Сезонный факт — только дни строго до даты отчёта
                if shift_date < report_date:
                    cov_firm = sum(c for _, c, _ in fitems)
                    if cov_firm > 0:
                        day_key = shift_date.isoformat()
                        season_daily_area[firm][day_key] += cov_firm
                        if mid is not None:
                            season_daily_machines[firm][day_key].add(mid)
                    for item, cov_item, _ in fitems:
                        fid_int = int(item.get("field_id"))
                        if cov_item > 0:
                            season_field_cov[firm][fid_int] += cov_item

                # Накапливаем площадь по машине за день отчёта
                if mid and shift_date == report_date:
                    cov_firm = sum(c for _, c, _ in fitems)
                    machine_day_area[firm][mid] += cov_firm
                    machine_day_meta[firm][mid] = {"grp": grp, "m_sub": m_sub}
                    if (
                        group_name == MU_GROUP
                        and machine_is_mtk
                        and machine_is_mtk.get(mid)
                    ):
                        machines_transport[firm].add(mid)

        # Постобработка: добавляем машины в счётчики own/hired только если
        # их суточная площадь >= MIN_COUNT_AREA_HA (или группа в списке исключений).
        for firm in FIRM_ORDER:
            for mid, total_cov in machine_day_area[firm].items():
                if not _exempt and total_cov < MIN_COUNT_AREA_HA:
                    continue
                meta = machine_day_meta[firm].get(mid, {})
                grp   = meta.get("grp", 0)
                m_sub = meta.get("m_sub", "—")
                # Собственные / наёмные
                if grp == HIRED_GROUP_ID:
                    machines_hired[firm].add(mid)
                else:
                    machines_own[firm].add(mid)
                # Самоходные / тракторы / без подтипа (Вариант Б)
                if m_sub in SELF_PROPELLED_SUBTYPES:
                    machines_sp[firm].add(mid)
                elif m_sub in TRACTOR_SUBTYPES:
                    machines_tr[firm].add(mid)
                else:
                    machines_no_sub[firm].add(mid)

        # Собираем итоговый gd по группе
        gd: dict[str, Any] = {
            "area_by_firm":   defaultdict(float),
            "area_by_crop":   defaultdict(lambda: defaultdict(float)),
            "machines_own":   machines_own,
            "machines_hired": machines_hired,
            "machines_sp":    machines_sp,
            "machines_tr":    machines_tr,
            "machines_no_sub": machines_no_sub,
            "machines_transport": machines_transport,
            # Доп. поля для возможного использования в build_docx
            "season_daily_area":     season_daily_area,
            "season_daily_machines": season_daily_machines,
        }

        # area_by_firm и area_by_crop — из today_area_by_field (суточные)
        for fid, fid_cov in today_area_by_field.items():
            if fid_cov <= 0:
                continue
            firm = field_to_firm.get(fid)
            if not firm:
                continue
            crop = field_to_crop.get(fid, "Не определена")
            if is_skip_crop(crop):
                continue
            gd["area_by_firm"][firm]       += fid_cov
            gd["area_by_crop"][firm][crop] += fid_cov

        # Округляем
        for firm in list(gd["area_by_firm"].keys()):
            gd["area_by_firm"][firm] = round(gd["area_by_firm"][firm])
        for firm in list(gd["area_by_crop"].keys()):
            for crop in list(gd["area_by_crop"][firm].keys()):
                gd["area_by_crop"][firm][crop] = round(gd["area_by_crop"][firm][crop])

        if any(gd["area_by_firm"].values()):
            result[group_name] = gd

    return result


def collect_one_day(
    report_date:    date,
    field_to_crop:  dict[int, str],
    field_to_firm:  dict[int, str],
    field_area:     dict[int, float],
    machine_group:  dict[int, int],
    machines_map:   dict[int, dict],
    machine_is_mtk: dict[int, bool] | None = None,
    crops_map:      dict[int, str] | None = None,
) -> dict:
    return asyncio.run(_collect_one_day_async(
        report_date, field_to_crop, field_to_firm, field_area,
        machine_group, machines_map, machine_is_mtk, crops_map,
    ))

def load_downtime(report_date: date) -> list[dict]:
    """Загружает все простои за report_date из Downtime API.

    Дедуплицируем по id (или композитному ключу), потому что API изредка
    возвращает одну и ту же запись на нескольких страницах подряд — без
    этого защиты при перегенерации сводки итог простоев удваивается.
    """

    def _dedup_key(rec: dict) -> tuple:
        rid = rec.get("id")
        if rid is not None:
            return ("id", rid)
        return (
            "composite",
            rec.get("vehicle_id") or rec.get("vehicle") or "",
            rec.get("fio") or "",
            rec.get("started_at") or rec.get("date") or "",
            rec.get("duration") or 0,
            rec.get("reason") or "",
        )

    if not (DOWNTIME_API_KEY or "").strip():
        print("[r23] DOWNTIME_API_KEY пуст — простои не загружены.", flush=True)
        return []

    dt_str = report_date.isoformat()
    headers = {"X-API-Key": DOWNTIME_API_KEY}
    all_records: list[dict] = []
    seen_keys: set[tuple] = set()
    page = 1
    max_pages = 500
    while page <= max_pages:
        try:
            r = requests.get(
                DOWNTIME_URL,
                headers=headers,
                params={"date_from": dt_str, "date_to": dt_str, "page": page, "limit": 200},
                timeout=30,
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            print(
                f"[r23] Ошибка Downtime API за {dt_str} (стр. {page}): {exc}",
                file=sys.stderr,
                flush=True,
            )
            raise
        body = r.json()
        data = body.get("data", []) or []
        if not data:
            break
        new_added = 0
        for rec in data:
            key = _dedup_key(rec)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_records.append(rec)
            new_added += 1
        total = int(body.get("total") or 0)
        if total and len(all_records) >= total:
            break
        if new_added == 0:
            break
        page += 1
    return all_records


def load_mu_fert_breakdown(
    report_date: date,
    field_to_crop: dict[int, str],
) -> dict[str, int]:
    """
    Площадь по типам удобрений на рапсе озимом за сутки (из agro_operations wt=55).
    Ключи: oz_nitrate, oz_sulfate — целые га, как в эталонной сводке.
    """
    day_start = datetime(
        report_date.year, report_date.month, report_date.day, 6, 0, 0, tzinfo=TZ_MSK,
    )
    cutoff = day_start + timedelta(days=1)

    fert_name_map: dict[int, str] = {}
    for f in get_all_from_id("fertilizers"):
        fid = f.get("id")
        name = str(f.get("name") or f.get("title") or "").strip().lower()
        if fid and name:
            fert_name_map[int(fid)] = name

    result = {"oz_nitrate": 0.0, "oz_sulfate": 0.0}
    skip_statuses = {"canceled"}

    for op in get_all_from_id("agro_operations", {
        "work_type_id": 55,
        "season": str(report_date.year),
    }):
        if op.get("status") in skip_statuses:
            continue
        completed = float(op.get("completed_area") or 0.0)
        covered = float(op.get("covered_area") or 0.0)
        if op.get("status") == "planned":
            if covered <= 0:
                continue
            area = covered
        else:
            area = completed if completed > 0 else covered
            if area <= 0:
                continue

        field_id = op.get("field_id")
        if not field_id:
            continue
        crop = (field_to_crop.get(int(field_id)) or "").strip().lower()
        if crop not in OZ_RAPE_CROP_NAMES:
            continue

        op_dt = parse_dt(
            op.get("completed_datetime") or op.get("actual_start_datetime")
        )
        if op_dt is None or not (day_start <= op_dt < cutoff):
            continue

        fert_ids: list[int] = []
        for ami in op.get("application_mix_items") or []:
            if ami.get("applicable_type") == "Fertilizer":
                fid_f = ami.get("applicable_id")
                if fid_f and int(fid_f) not in fert_ids:
                    fert_ids.append(int(fid_f))
        if not fert_ids:
            continue

        share = area / len(fert_ids)
        for fid_f in fert_ids:
            fname = fert_name_map.get(fid_f, "")
            if (
                "селитр" in fname
                or ("аммиачн" in fname and "сульфат" not in fname)
                or "нитрат аммония" in fname
            ):
                result["oz_nitrate"] += share
            elif "сульфат" in fname:
                result["oz_sulfate"] += share

    return {k: int(round(v)) for k, v in result.items()}


def aggregate_downtime(records: list[dict]) -> dict:
    """
    Группирует простои по причинам и по агрофирмам.
    Агрофирма берётся напрямую из поля agrofirm, которое теперь возвращает
    Downtime API (заполняется из Vehicle.agrofirm через Celery-задачу
    sync_vehicle_agrofirms). Записи без agrofirm относим к «Техника МТК».

    Возвращает:
    {
      "total_units":   int,
      "total_seconds": int,
      "by_reason":     {reason: {"units": int, "seconds": int}},
      "by_firm":       {firm_name: {
                          "units": int, "seconds": int,
                          "avg_seconds": int,
                          "fio_set": set,
                          "reasons": [str],
                       }},
    }
    """
    # Учитываем только технику агрофирм холдинга — МТК и прочие исключаются.
    records = [r for r in records if r.get("agrofirm") in FIRM_TO_FOLDER]

    total_units   = len(records)
    total_seconds = sum(r.get("duration", 0) for r in records)

    by_reason: dict[str, dict] = defaultdict(lambda: {"units": 0, "seconds": 0})
    for r in records:
        reason = r.get("reason") or "Не указана"
        by_reason[reason]["units"]   += 1
        by_reason[reason]["seconds"] += r.get("duration", 0)

    by_firm: dict[str, dict] = defaultdict(lambda: {
        "units": 0, "seconds": 0,
        "avg_seconds_list": [],
        "fio_set": set(),
        "reason_seconds": defaultdict(int),
    })

    for r in records:
        firm_key = r.get("agrofirm")
        if not firm_key:
            continue
        by_firm[firm_key]["units"]   += 1
        by_firm[firm_key]["seconds"] += r.get("duration", 0)
        by_firm[firm_key]["avg_seconds_list"].append(r.get("duration", 0))
        fio = r.get("fio") or ""
        if fio:
            by_firm[firm_key]["fio_set"].add(fio)
        reason = r.get("reason") or ""
        if reason:
            by_firm[firm_key]["reason_seconds"][reason] += r.get("duration", 0)

    for v in by_firm.values():
        lst = v["avg_seconds_list"]
        v["avg_seconds"] = int(sum(lst) / len(lst)) if lst else 0
        rs = v.pop("reason_seconds", {})
        v["reasons"] = sorted(
            rs.keys(),
            key=lambda reason: (-rs[reason], reason),
        )

    return {
        "total_units":   total_units,
        "total_seconds": total_seconds,
        "by_reason":     dict(by_reason),
        "by_firm":       dict(by_firm),
    }


# =============================================================================
# ФОРМИРОВАНИЕ DOCX
# =============================================================================

def fmt_date_ru(d: date) -> str:
    """16 апреля"""
    months = ["января","февраля","марта","апреля","мая","июня",
              "июля","августа","сентября","октября","ноября","декабря"]
    return f"{d.day} {months[d.month - 1]}"


def firms_summary(area_by_firm: dict[str, float]) -> str:
    """АФЧ (1270 га.), АгроРаздолье (2743 га.), ..."""
    parts = []
    for firm in FIRM_ORDER:
        ha = area_by_firm.get(firm, 0)
        if ha > 0:
            parts.append(f"{FIRM_SHORT[firm]} ({int(ha)} га.)")
    return ", ".join(parts)


DOC_FONT_NAME = "Times New Roman"
DOC_FONT_SIZE = Pt(12)

# Плановая выработка на агрегат (га/сутки).
PLAN_HA_PER_MACHINE = 250


def _output_color(actual_ha: float, plan_ha: float) -> RGBColor:
    """
    Цвет выработки относительно плана:
      < 80%  — красный
      80–98% — жёлтый
      ≥ 98%  — зелёный
    """
    if plan_ha <= 0:
        return RGBColor(0, 0, 0)
    ratio = actual_ha / plan_ha
    if ratio >= 0.98:
        return RGBColor(0, 150, 0)
    if ratio >= 0.80:
        return RGBColor(200, 150, 0)
    return RGBColor(200, 0, 0)


def style_doc(doc: Document) -> None:
    style = doc.styles["Normal"]
    style.font.name = DOC_FONT_NAME
    style.font.size = DOC_FONT_SIZE


def _style_para(para, *, bold: bool = False, underline: bool = False) -> None:
    para.alignment = WD_ALIGN_PARAGRAPH.LEFT
    para.paragraph_format.space_before = Pt(0)
    para.paragraph_format.space_after = Pt(0)
    para.paragraph_format.line_spacing = 1.0
    for run in para.runs:
        run.font.name = DOC_FONT_NAME
        run.font.size = DOC_FONT_SIZE
        if bold:
            run.bold = True
        if underline:
            run.underline = True


def _add_run(
    para,
    text: str,
    *,
    bold: bool = False,
    underline: bool = False,
) -> None:
    run = para.add_run(text)
    run.font.name = DOC_FONT_NAME
    run.font.size = DOC_FONT_SIZE
    run.bold = bold
    if underline:
        run.underline = True


def _add_colored_run(para, text: str, color: RGBColor) -> None:
    run = para.add_run(text)
    run.font.name = DOC_FONT_NAME
    run.font.size = DOC_FONT_SIZE
    run.font.color.rgb = color


def _add_firm_machine_line(
    doc: Document,
    firm_short: str,
    total_f: int,
    avg_ha: float,
) -> None:
    p = doc.add_paragraph()
    _add_run(p, f"{firm_short} – {total_f} ед., выработка на агрегат: ")
    _add_colored_run(p, f"{avg_ha} га.", _output_color(avg_ha, PLAN_HA_PER_MACHINE))
    _style_para(p)


def _add_blank_line(doc: Document) -> None:
    doc.add_paragraph()


def _add_text_line(
    doc: Document,
    text: str,
    *,
    bold: bool = False,
    underline: bool = False,
) -> None:
    p = doc.add_paragraph()
    _add_run(p, text, bold=bold, underline=underline)
    _style_para(p, bold=bold, underline=underline)


def _add_label_value_line(doc: Document, label: str, value: str) -> None:
    p = doc.add_paragraph()
    _add_run(p, label, bold=True)
    _add_run(p, value)
    _style_para(p)


def _add_work_intro_line(doc: Document, text: str) -> None:
    """Вводный абзац по работе — подчёркнутый, как в эталонной сводке."""
    _add_text_line(doc, text, underline=True)


def _read_r23_editor_body() -> str:
    """Текст из веб-редактора ручных настроек: UTF-8 файл, путь в SVODKI_R23_EDITOR_BODY_SIDECAR."""
    side = (os.environ.get("SVODKI_R23_EDITOR_BODY_SIDECAR") or "").strip()
    if not side:
        return ""
    try:
        p = Path(side)
        if p.is_file():
            return p.read_text(encoding="utf-8")
    except OSError:
        pass
    return ""


def _append_editor_override_paragraphs(doc: Document, text: str) -> None:
    for line in text.replace("\r\n", "\n").split("\n"):
        _add_text_line(doc, line)


def _crop_area_by_firm(gd: dict, crop_match) -> dict[str, float]:
    out: dict[str, float] = {}
    for firm in FIRM_ORDER:
        total = 0.0
        for crop, ha in gd["area_by_crop"].get(firm, {}).items():
            if crop_match(crop):
                total += ha
        if total > 0:
            out[firm] = total
    return out


def _hired_line(gd: dict) -> str:
    parts = []
    for firm in FIRM_ORDER:
        hf = len(gd["machines_hired"].get(firm, set()))
        if hf > 0:
            parts.append(f"{FIRM_SHORT[firm]} – {hf} ед.")
    if not parts:
        return "Наемная техника: отсутствует."
    return "Наемная техника: " + ", ".join(parts)


def _work_intro_text(
    intro_verb: str,
    label: str,
    total_ha: int,
    area_by_firm: dict[str, float],
    prev_date: date,
    prev_total: int,
    delta: int,
) -> str:
    firms = firms_summary(area_by_firm)
    text = (
        f"{intro_verb} {label}, обработано {total_ha} га., а именно:\n"
        f"{firms}."
    )
    if prev_total > 0 or delta != 0:
        delta_text = (
            f"увеличение на {abs(delta)} га."
            if delta >= 0
            else f"уменьшение на {abs(delta)} га."
        )
        text += f" По сравнению с {fmt_date_ru(prev_date)} {delta_text}"
    return text


def _append_mu_crop_details(
    doc: Document,
    gd: dict,
    fert_breakdown: dict[str, int],
) -> None:
    oz_by_firm = _crop_area_by_firm(
        gd,
        lambda c: c.strip().lower() in OZ_RAPE_CROP_NAMES
        or ("рапс" in c.lower() and "озим" in c.lower()),
    )
    oz_total = int(sum(oz_by_firm.values()))
    if oz_total > 0:
        nitrate = fert_breakdown.get("oz_nitrate", 0)
        sulfate = fert_breakdown.get("oz_sulfate", 0)
        line = f"По рапсу озимому всего обработано: {oz_total} га."
        extras = []
        if nitrate > 0:
            extras.append(f"селитры аммиачной – {nitrate} га.")
        if sulfate > 0:
            extras.append(f"сульфата амония - {sulfate} га.")
        if extras:
            line += " Из них - " + "; ".join(extras)
        colon = line.index(":")
        _add_label_value_line(doc, line[: colon + 1], line[colon + 1 :])
        _add_label_value_line(
            doc,
            "Рапс озимый по агрофирмам",
            ": " + firms_summary(oz_by_firm) + ".",
        )

    yr_by_firm = _crop_area_by_firm(
        gd,
        lambda c: c.strip().lower() in YR_RAPE_CROP_NAMES
        or ("рапс" in c.lower() and "яров" in c.lower()),
    )
    if yr_by_firm:
        _add_label_value_line(
            doc,
            "Рапс яровой по агрофирмам",
            ": " + firms_summary(yr_by_firm) + ".",
        )

    wheat_by_firm = _crop_area_by_firm(
        gd,
        lambda c: "пшениц" in c.lower() and "озим" in c.lower(),
    )
    if wheat_by_firm:
        _add_label_value_line(
            doc,
            "По пшенице озимой",
            ": " + firms_summary(wheat_by_firm) + ".",
        )


def _append_work_machines_block(
    doc: Document,
    group_name: str,
    label: str,
    gd: dict,
) -> None:
    total_machines = sum(
        len(gd["machines_own"].get(firm, set()))
        + len(gd["machines_hired"].get(firm, set()))
        for firm in FIRM_ORDER
    )
    label_on = WORK_LABEL_MACHINES_ON.get(group_name, label)
    is_mu = group_name == MU_GROUP
    _add_text_line(
        doc,
        f"Работало техники всего на {label_on} {total_machines} ед., а именно:",
        bold=True,
        underline=is_mu,
    )

    for firm in FIRM_ORDER:
        total_f = len(gd["machines_own"].get(firm, set())) + len(
            gd["machines_hired"].get(firm, set())
        )
        if total_f <= 0:
            continue
        firm_ha = gd["area_by_firm"].get(firm, 0)
        avg_ha = round(firm_ha / total_f, 1) if total_f else 0.0
        _add_firm_machine_line(doc, FIRM_SHORT[firm], total_f, avg_ha)

    _add_text_line(doc, _hired_line(gd))

    if "удобрени" in label.lower():
        transport_ids: set = set()
        for firm in FIRM_ORDER:
            transport_ids |= gd.get("machines_transport", {}).get(firm, set())
        tr_count = _r23_fertilizer_transport_machine_count(len(transport_ids))
        if tr_count > 0:
            _add_text_line(
                doc,
                f"Данную технику обслуживало {tr_count} машин для подвоза/загрузки удобрений.",
            )


def _firm_downtime_time_hhmm(seconds: int) -> str:
    if seconds <= 0:
        return "0 ч 00 мин., "
    return seconds_to_hhmm(seconds) + ","


def _append_downtime_firm_blocks(doc: Document, work_data: dict, downtime_agg: dict) -> None:
    mu_gd = work_data.get(MU_GROUP)
    if mu_gd:
        _add_blank_line(doc)
        _add_text_line(doc, "По работам на разбрасывании МУ", bold=True)

    by_firm = downtime_agg["by_firm"]
    for firm in FIRM_ORDER:
        firm_short = FIRM_SHORT[firm]
        fd = by_firm.get(firm) or {}
        units = fd.get("units", 0)
        seconds = fd.get("seconds", 0)
        avg_seconds = fd.get("avg_seconds", 0)
        reasons = fd.get("reasons") or []

        _add_blank_line(doc)
        _add_text_line(doc, firm_short, bold=True)

        if mu_gd:
            sp_n = len(mu_gd["machines_sp"].get(firm, set()))
            tr_n = len(mu_gd["machines_tr"].get(firm, set()))
            transport_n = len(mu_gd.get("machines_transport", {}).get(firm, set()))
            _add_text_line(
                doc,
                f"Работало – {sp_n} агрегатов самоходных и {tr_n} прицепных.",
            )
            _add_text_line(doc, f"Их обслуживало {transport_n} машин подвоза.")

        _add_text_line(doc, f"Всего простоев {units},")
        dash = "–" if seconds > 0 and seconds % 3600 else "-"
        _add_text_line(doc, f"суммарное время простоя {dash} {round_hours(seconds)},")
        _add_text_line(doc, f"среднее время простоя - {_firm_downtime_time_hhmm(avg_seconds)}")
        reasons_str = ", ".join(reasons) if reasons else "отсутствует"
        _add_text_line(doc, f"топ причин простоя – {reasons_str}.")


def build_docx(
    report_date:  date,
    prev_date:    date,
    work_data:    dict,
    work_data_prev: dict,
    downtime_agg: dict,
    fert_breakdown: dict[str, int] | None = None,
) -> Document:
    doc = Document()
    style_doc(doc)
    fert_breakdown = fert_breakdown or {}

    editor_body = _read_r23_editor_body()
    if editor_body.strip():
        side = (os.environ.get("SVODKI_R23_EDITOR_BODY_SIDECAR") or "").strip()
        print(
            f"[r23] Ручной текст блока по работам ({side or 'sidecar'}): "
            "цифры Cropwise не подставляются, только простои из API.",
            flush=True,
        )
        _append_editor_override_paragraphs(doc, editor_body)
        _add_blank_line(doc)
    else:
        _add_text_line(
            doc,
            f"Информация по ВПР за сутки {fmt_date_ru(report_date)}:",
            bold=True,
        )

        work_order = [g for g, _ in WORK_GROUPS if g in work_data]
        for idx, group_name in enumerate(work_order):
            gd = work_data[group_name]
            gd_prev = work_data_prev.get(group_name, {})
            label = WORK_LABEL.get(group_name, group_name)
            intro_verb = WORK_INTRO_VERB.get(group_name, "Проводилось")
            area_by_firm = gd["area_by_firm"]
            total_ha = int(sum(area_by_firm.values()))
            prev_total = int(sum(gd_prev.get("area_by_firm", {}).values()))
            delta = total_ha - prev_total

            if idx > 0:
                _add_blank_line(doc)

            _add_work_intro_line(
                doc,
                _work_intro_text(
                    intro_verb, label, total_ha, area_by_firm,
                    prev_date, prev_total, delta,
                ),
            )

            if "удобрени" in label.lower():
                _append_mu_crop_details(doc, gd, fert_breakdown)

            _add_blank_line(doc)
            _append_work_machines_block(doc, group_name, label, gd)

        snap_out = (os.environ.get("SVODKI_R23_WORK_SNAPSHOT_OUT") or "").strip()
        if snap_out:
            try:
                Path(snap_out).write_text(
                    "\n".join(paragraph.text for paragraph in doc.paragraphs),
                    encoding="utf-8",
                )
            except OSError:
                pass

    _add_blank_line(doc)
    _add_text_line(doc, "Информация по агрофирмам и по простоям техники:", bold=True)

    total_u = downtime_agg["total_units"]
    total_s = downtime_agg["total_seconds"]
    _add_text_line(doc, f"Суммарно простоев: {total_u} ед. на {seconds_to_hhmm(total_s)}")

    by_reason = sorted(
        downtime_agg["by_reason"].items(),
        key=lambda x: x[1]["units"],
        reverse=True,
    )
    if by_reason:
        _add_text_line(doc, "Причины простоя:")
        for i, (reason, stats) in enumerate(by_reason, 1):
            _add_text_line(
                doc,
                f"{i}) причина «{reason}» количество {stats['units']} ед., "
                f"общее время простоя {round_hours(stats['seconds'])}",
            )

    _append_downtime_firm_blocks(doc, work_data, downtime_agg)

    return doc


def main() -> None:
    global REPORT_DATE, PREV_DATE, REPORT_STAMP
    REPORT_DATE, PREV_DATE, REPORT_STAMP = _resolve_analytics_dates()
    print(
        f"Штамп отчёта: {REPORT_STAMP} | "
        f"сутки в отчёте: {REPORT_DATE} | день сравнения: {PREV_DATE}"
    )
    _validate_or_clear_prefetch()

    fast = (os.environ.get("SVODKI_R23_DOC_REBUILD_ONLY") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if fast:
        editor_body = _read_r23_editor_body()
        if not editor_body.strip():
            print(
                "SVODKI_R23_DOC_REBUILD_ONLY: задайте непустой SVODKI_R23_EDITOR_BODY_SIDECAR (ручной текст сводки).",
                file=sys.stderr,
            )
            sys.exit(1)
        print("\nРежим пересборки: ручной текст + простои (без Cropwise)...")
        print(f"\nЗагружаем данные о простоях за {REPORT_DATE}...")
        downtime_records = load_downtime(REPORT_DATE)
        print(f"  Получено записей о простоях: {len(downtime_records)}")
        downtime_agg = aggregate_downtime(downtime_records)

        print("\nФормируем отчёт...")
        doc = build_docx(REPORT_DATE, PREV_DATE, {}, {}, downtime_agg)

        out_file = f"analytics_{REPORT_DATE}.docx"
        doc.save(out_file)
        print(f"Сохранено: {out_file}")
        print(f"\nГотово: {out_file}")
        return

    from r23_from_report2 import BUNDLE_IN_ENV, try_load_r23_bundle_for_analytics

    fert_breakdown: dict[str, int] = {}
    stamp = REPORT_STAMP
    bundle_in = (os.environ.get("SVODKI_R23_WORK_DATA_BUNDLE_IN") or "").strip()
    if bundle_in:
        from r23_from_report2 import peek_bundle_reporting_dates

        peeked = peek_bundle_reporting_dates(Path(bundle_in), stamp=stamp)
        if peeked is not None:
            REPORT_DATE, PREV_DATE = peeked
            print(
                f"[r23] Сутки из bundle сводки 2: {REPORT_DATE}, сравнение {PREV_DATE}",
                flush=True,
            )

    bundled = try_load_r23_bundle_for_analytics(REPORT_DATE, PREV_DATE)
    if bundled is not None:
        work_data, work_data_prev = bundled
        vsp = work_data.get("2.4 Вспашка") or {}
        spr = work_data.get("3.1 Опрыскивание") or {}
        vsp_m = sum(
            len(vsp.get("machines_own", {}).get(f, set()))
            + len(vsp.get("machines_hired", {}).get(f, set()))
            for f in FIRM_ORDER
        )
        spr_ha = sum((spr.get("area_by_firm") or {}).values())
        print(
            f"\nДанные по работам из сводки 2 (снимок): сутки {REPORT_DATE}, "
            f"сравнение {PREV_DATE}, групп {len(work_data)}, "
            f"вспашка {vsp_m} ед., опрыскивание {spr_ha} га"
        )
    else:
        bundle_in = (os.environ.get(BUNDLE_IN_ENV) or "").strip()
        if bundle_in:
            print(
                f"Ошибка: не удалось прочитать bundle сводки 2: {bundle_in}",
                file=sys.stderr,
            )
            sys.exit(1)

        allow_live = (os.environ.get("SVODKI_R23_ALLOW_LIVE_CROPWISE") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if not allow_live:
            print(
                "Нет снимка сводки 2 (bundle). Сначала сформируйте сводку 2 за эту дату "
                "или задайте SVODKI_R23_ALLOW_LIVE_CROPWISE=1 для прямого запроса Cropwise.",
                file=sys.stderr,
            )
            sys.exit(1)

        print("\nЗагружаем справочники CropWise...")
        (field_to_crop, field_to_firm,
         field_area, machine_group, machines_map, machine_is_mtk, crops_map) = load_reference_data()

        print(f"\nСобираем данные о работах за {REPORT_DATE}...")
        work_data = collect_one_day(
            REPORT_DATE, field_to_crop, field_to_firm,
            field_area, machine_group, machines_map,
            machine_is_mtk,
            crops_map=crops_map,
        )

        print(f"\nСобираем данные о работах за {PREV_DATE} (для сравнения)...")
        work_data_prev = collect_one_day(
            PREV_DATE, field_to_crop, field_to_firm,
            field_area, machine_group, machines_map,
            machine_is_mtk,
            crops_map=crops_map,
        )

        print("\nЗагружаем разбивку удобрений...")
        fert_breakdown = load_mu_fert_breakdown(REPORT_DATE, field_to_crop)
        print(
            f"  Селитра: {fert_breakdown.get('oz_nitrate', 0)} га, "
            f"сульфат: {fert_breakdown.get('oz_sulfate', 0)} га"
        )

    print(f"\nЗагружаем данные о простоях за {REPORT_DATE}...")
    downtime_records = load_downtime(REPORT_DATE)
    print(f"  Получено записей о простоях: {len(downtime_records)}")
    downtime_agg = aggregate_downtime(downtime_records)
    print(
        f"  Учтено по агрофирмам холдинга: {downtime_agg['total_units']} ед., "
        f"{seconds_to_hhmm(downtime_agg['total_seconds'])}",
        flush=True,
    )

    print("\nФормируем отчёт...")
    doc = build_docx(
        REPORT_DATE, PREV_DATE, work_data, work_data_prev, downtime_agg, fert_breakdown,
    )

    out_file = f"analytics_{REPORT_DATE}.docx"
    doc.save(out_file)
    print(f"Сохранено: {out_file}")
    print(f"\nГотово: {out_file}")


if __name__ == "__main__":
    main()

# =============================================================================
# ДИАГНОСТИКА — ВАРИАНТ А: через implement_id + /implements
# (орудие = отдельная запись в /implements, наёмность по legal_company)
# =============================================================================

def _diag_impl_load_implements():
    """Загружает справочник /implements и строит вспомогательные словари."""
    OWN_LEGAL_KEYWORDS = [
        "чистопольская", "агрораздолье", "аксу", "нурлат", "дрожжаное",
        "рассвет", "актай", "лениногорск", "птицефабрик",
    ]
    LEGAL_TO_FIRM = [
        ("чистопольская",  "АФ Чистопольская"),
        ("агрораздолье",   "АгроРаздолье"),
        ("аксу",           "Аксу-Агро"),
        ("нурлат",         "НурлатАгро"),
        ("дрожжаное",      "Дрожжаное Рассвет Агро"),
        ("рассвет",        "Дрожжаное Рассвет Агро"),
        ("актай",          "Актай"),
        ("лениногор",      "Лениногорская Птицефабрика"),
        ("птицефабрик",    "Лениногорская Птицефабрика"),
    ]
    IMPL_NAME_TO_FIRM = [
        ("1афч", "АФ Чистопольская"), ("афч", "АФ Чистопольская"),
        ("1дра", "Дрожжаное Рассвет Агро"), (" дра", "Дрожжаное Рассвет Агро"), ("дра", "Дрожжаное Рассвет Агро"),
        ("1ар",  "АгроРаздолье"), (" ар ", "АгроРаздолье"), ("агрораздол", "АгроРаздолье"),
        ("1аа",  "Аксу-Агро"), (" аа", "Аксу-Агро"), ("аксу", "Аксу-Агро"),
        ("1на",  "НурлатАгро"), (" на", "НурлатАгро"), ("нурлат", "НурлатАгро"),
        ("актай", "Актай"),
        ("лениногор", "Лениногорская Птицефабрика"), ("птицефабрик", "Лениногорская Птицефабрика"), ("лпф", "Лениногорская Птицефабрика"),
    ]

    cw_session_local = requests.Session()
    cw_session_local.headers.update({"X-User-Api-Token": CROPWISE_TOKEN})
    implements_all = get_all_from_id("implements")

    implement_name: dict[int, str] = {
        impl["id"]: (impl.get("name") or impl.get("custom_name") or f"impl={impl['id']}")
        for impl in implements_all
    }

    def _is_hired(impl):
        legal = (impl.get("legal_company") or "").lower().strip()
        if not legal:
            return False
        return not any(kw in legal for kw in OWN_LEGAL_KEYWORDS)

    def _impl_firm(impl):
        legal = (impl.get("legal_company") or "").lower().strip()
        for kw, firm in LEGAL_TO_FIRM:
            if kw in legal:
                return firm
        nm = (impl.get("name") or "").lower()
        for kw, firm in IMPL_NAME_TO_FIRM:
            if kw in nm:
                return firm
        addl = (impl.get("additional_info") or "").lower()
        for kw, firm in IMPL_NAME_TO_FIRM:
            if kw in addl:
                return firm
        return None

    implement_is_hired: dict[int, bool]      = {impl["id"]: _is_hired(impl)  for impl in implements_all}
    implement_firm:     dict[int, str | None] = {impl["id"]: _impl_firm(impl) for impl in implements_all}
    return implement_name, implement_is_hired, implement_firm


async def _diag_variant_a(report_date: date, field_to_crop, field_to_firm,
                           machine_group, machine_name_map,
                           implement_name, implement_is_hired):
    """
    ВАРИАНТ А — машина считается как:
      machine_id  → самоходная  (тягач/разбрасыватель/опрыскиватель)
      implement_id → прицепная  (орудие из /implements)
    Наёмность орудия — по legal_company из /implements.
    """
    DAY_START_HOUR = 6
    TZ = TZ_MSK
    day_start = datetime(report_date.year, report_date.month, report_date.day,
                         DAY_START_HOUR, 0, 0, tzinfo=TZ)
    cutoff    = day_start + timedelta(days=1)
    season_start_str = datetime(report_date.year, 1, 1, tzinfo=TZ).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str       = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    pool      = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    headers   = {"X-User-Api-Token": CROPWISE_TOKEN}

    # {firm: {"own_sp": set, "own_tr": set, "hired_sp": set, "hired_tr": set, "hired_no_subtype": set}}
    firm_machines: dict[str, dict[str, set]] = {
        firm: {"own_sp": set(), "own_tr": set(), "hired_sp": set(),
               "hired_tr": set(), "hired_no_subtype": set()}
        for firm in FIRM_ORDER
    }

    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        all_wt_ids = [wt_id for _, wt_ids in WORK_GROUPS for wt_id in wt_ids]
        season_results = await asyncio.gather(*[
            fetch_all_pages_async(client, f"{CROPWISE_URL}/machine_tasks",
                                  {"work_type_id": wt_id,
                                   "end_time_gt_eq": season_start_str,
                                   "end_time_lt": cutoff_str}, semaphore)
            for wt_id in all_wt_ids
        ])

        seen_ids: set[int] = set()
        today_tasks: list[dict] = []
        for chunk in season_results:
            for t in chunk:
                if t["id"] in seen_ids:
                    continue
                seen_ids.add(t["id"])
                if (t.get("covered_area") or 0) <= 0:
                    continue
                sw = _shift_window(t.get("end_time"), day_start, cutoff)
                if sw and sw[0] == report_date:
                    today_tasks.append(t)

        if not today_tasks:
            print("  [Вариант А] Заданий за дату не найдено.")
            return

        mtfmi_results = await asyncio.gather(*[
            fetch_all_pages_async(client, f"{CROPWISE_URL}/machine_task_field_mapping_items",
                                  {"machine_task_id": t["id"]}, semaphore)
            for t in today_tasks
        ])
        mtfmi_map = {t["id"]: items for t, items in zip(today_tasks, mtfmi_results)}

    def _get_cov(items, shift_cutoff):
        cov = 0.0
        hourly = items.get("covered_area_hourly") or []
        for entry in hourly:
            h = parse_dt(entry[0])
            if h and h < shift_cutoff:
                cov += entry[1] or 0
        if not hourly:
            cov = float(items.get("covered_area") or 0.0)
        return cov

    for task in today_tasks:
        tid     = task["id"]
        mid     = task.get("machine_id")
        impl_id = task.get("implement_id")
        sw      = _shift_window(task.get("end_time"), day_start, cutoff)
        if not sw:
            continue
        _, _, shift_co = sw

        for item in mtfmi_map.get(tid, []):
            fid = item.get("field_id")
            if not fid:
                continue
            fid_int = int(fid)
            firm = field_to_firm.get(fid_int)
            if not firm:
                continue
            crop = field_to_crop.get(fid_int, "")
            if is_skip_crop(crop):
                continue
            if _get_cov(item, shift_co) <= 0.5:
                continue

            # Самоходная (machine_id)
            if mid:
                grp = machine_group.get(mid, 0)
                key = "hired_sp" if grp == HIRED_GROUP_ID else "own_sp"
                firm_machines[firm][key].add(mid)

            # Прицепная (implement_id из /implements)
            if impl_id:
                if implement_is_hired.get(impl_id, False):
                    firm_machines[firm]["hired_tr"].add(impl_id)
                else:
                    firm_machines[firm]["own_tr"].add(impl_id)

    # ── Вывод ─────────────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"ВАРИАНТ А — implement_id из /implements  |  дата: {report_date}")
    print(f"{'='*65}")
    print(f"{'Фирма':<8} {'св.сам':>6} {'св.пр':>6} {'нм.сам':>7} {'нм.пр':>6} {'итого':>6}")
    print(f"{'─'*45}")
    grand = [0, 0, 0, 0]
    for firm in FIRM_ORDER:
        fm   = firm_machines[firm]
        own_sp  = len(fm["own_sp"])
        own_tr  = len(fm["own_tr"])
        hir_sp  = len(fm["hired_sp"])
        hir_tr  = len(fm["hired_tr"])
        total   = own_sp + own_tr + hir_sp + hir_tr
        if total == 0:
            continue
        short = FIRM_SHORT[firm]
        print(f"  {short:<6} {own_sp:>6} {own_tr:>6} {hir_sp:>7} {hir_tr:>6} {total:>6}")
        grand[0] += own_sp; grand[1] += own_tr; grand[2] += hir_sp; grand[3] += hir_tr
        # Детали
        for mid in sorted(fm["own_sp"]):
            print(f"    [св.сам]  mid={mid}  '{machine_name_map.get(mid, '?')}'")
        for iid in sorted(fm["own_tr"]):
            print(f"    [св.пр]   impl={iid}  '{implement_name.get(iid, '?')}'")
        for mid in sorted(fm["hired_sp"]):
            print(f"    [нм.сам]  mid={mid}  '{machine_name_map.get(mid, '?')}'")
        for iid in sorted(fm["hired_tr"]):
            print(f"    [нм.пр]   impl={iid}  '{implement_name.get(iid, '?')}'")
    print(f"{'─'*45}")
    print(f"  {'ИТОГО':<6} {grand[0]:>6} {grand[1]:>6} {grand[2]:>7} {grand[3]:>6} {sum(grand):>6}")


# =============================================================================
# ДИАГНОСТИКА — ВАРИАНТ Б: через machine_subtype (самоходные) и machine_id/implement_id
# Самоходные: subtype в SELF_PROPELLED_SUBTYPES
# Прицепные:  subtype в TRACTOR_SUBTYPES  (тягачи из machine_tasks)
# Наёмники без subtype — отдельная категория
# =============================================================================


async def _diag_variant_b(report_date: date, field_to_crop, field_to_firm,
                           machine_group, machine_name_map, machines_map: dict):
    """
    ВАРИАНТ Б — тип агрегата определяется по machine_subtype:
      SELF_PROPELLED_SUBTYPES → самоходный
      TRACTOR_SUBTYPES        → трактор (прицепной агрегат = тягач + орудие)
      None / другой           → «без подтипа» (наёмники часто без подтипа)
    """
    DAY_START_HOUR = 6
    TZ = TZ_MSK
    day_start = datetime(report_date.year, report_date.month, report_date.day,
                         DAY_START_HOUR, 0, 0, tzinfo=TZ)
    cutoff    = day_start + timedelta(days=1)
    season_start_str = datetime(report_date.year, 1, 1, tzinfo=TZ).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str       = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    pool      = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    headers   = {"X-User-Api-Token": CROPWISE_TOKEN}

    # {firm: {"self_propelled": set, "tractor": set, "no_subtype": set}}
    firm_machines: dict[str, dict[str, set]] = {
        firm: {"self_propelled": set(), "tractor": set(), "no_subtype": set()}
        for firm in FIRM_ORDER
    }

    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        all_wt_ids = [wt_id for _, wt_ids in WORK_GROUPS for wt_id in wt_ids]
        season_results = await asyncio.gather(*[
            fetch_all_pages_async(client, f"{CROPWISE_URL}/machine_tasks",
                                  {"work_type_id": wt_id,
                                   "end_time_gt_eq": season_start_str,
                                   "end_time_lt": cutoff_str}, semaphore)
            for wt_id in all_wt_ids
        ])

        seen_ids: set[int] = set()
        today_tasks: list[dict] = []
        for chunk in season_results:
            for t in chunk:
                if t["id"] in seen_ids:
                    continue
                seen_ids.add(t["id"])
                if (t.get("covered_area") or 0) <= 0:
                    continue
                sw = _shift_window(t.get("end_time"), day_start, cutoff)
                if sw and sw[0] == report_date:
                    today_tasks.append(t)

        if not today_tasks:
            print("  [Вариант Б] Заданий за дату не найдено.")
            return

        mtfmi_results = await asyncio.gather(*[
            fetch_all_pages_async(client, f"{CROPWISE_URL}/machine_task_field_mapping_items",
                                  {"machine_task_id": t["id"]}, semaphore)
            for t in today_tasks
        ])
        mtfmi_map = {t["id"]: items for t, items in zip(today_tasks, mtfmi_results)}

    def _get_cov(items, shift_cutoff):
        cov = 0.0
        hourly = items.get("covered_area_hourly") or []
        for entry in hourly:
            h = parse_dt(entry[0])
            if h and h < shift_cutoff:
                cov += entry[1] or 0
        if not hourly:
            cov = float(items.get("covered_area") or 0.0)
        return cov

    for task in today_tasks:
        tid = task["id"]
        mid = task.get("machine_id")
        if not mid:
            continue
        sw = _shift_window(task.get("end_time"), day_start, cutoff)
        if not sw:
            continue
        _, _, shift_co = sw

        m       = machines_map.get(mid, {})
        subtype = m.get("machine_subtype") or None

        for item in mtfmi_map.get(tid, []):
            fid = item.get("field_id")
            if not fid:
                continue
            fid_int = int(fid)
            firm = field_to_firm.get(fid_int)
            if not firm:
                continue
            crop = field_to_crop.get(fid_int, "")
            if is_skip_crop(crop):
                continue
            if _get_cov(item, shift_co) <= 0.5:
                continue

            if subtype in SELF_PROPELLED_SUBTYPES:
                firm_machines[firm]["self_propelled"].add(mid)
            elif subtype in TRACTOR_SUBTYPES:
                firm_machines[firm]["tractor"].add(mid)
            else:
                firm_machines[firm]["no_subtype"].add(mid)

    # ── Вывод ─────────────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"ВАРИАНТ Б — по machine_subtype  |  дата: {report_date}")
    print(f"Самоходные: {sorted(SELF_PROPELLED_SUBTYPES)}")
    print(f"Тракторы:   {sorted(TRACTOR_SUBTYPES)}")
    print(f"{'='*65}")
    print(f"{'Фирма':<8} {'самоход':>8} {'трактор':>8} {'без_типа':>9} {'итого':>6}")
    print(f"{'─'*45}")
    grand = [0, 0, 0]
    for firm in FIRM_ORDER:
        fm   = firm_machines[firm]
        sp   = len(fm["self_propelled"])
        tr   = len(fm["tractor"])
        no   = len(fm["no_subtype"])
        total = sp + tr + no
        if total == 0:
            continue
        short = FIRM_SHORT[firm]
        print(f"  {short:<6} {sp:>8} {tr:>8} {no:>9} {total:>6}")
        grand[0] += sp; grand[1] += tr; grand[2] += no
        # Детали
        for mid in sorted(fm["self_propelled"]):
            st = machines_map.get(mid, {}).get("machine_subtype", "?")
            print(f"    [самоход]   mid={mid}  subtype={st}  '{machine_name_map.get(mid, '?')}'")
        for mid in sorted(fm["tractor"]):
            st = machines_map.get(mid, {}).get("machine_subtype", "?")
            print(f"    [трактор]   mid={mid}  subtype={st}  '{machine_name_map.get(mid, '?')}'")
        for mid in sorted(fm["no_subtype"]):
            st = machines_map.get(mid, {}).get("machine_subtype") or "—"
            print(f"    [без_типа]  mid={mid}  subtype={st}  '{machine_name_map.get(mid, '?')}'")
    print(f"{'─'*45}")
    print(f"  {'ИТОГО':<6} {grand[0]:>8} {grand[1]:>8} {grand[2]:>9} {sum(grand):>6}")


# =============================================================================
# ТОЧКА ВХОДА ДЛЯ ДИАГНОСТИКИ
# Запуск: python ranis_svodka_diag.py --diag [YYYY-MM-DD]
# =============================================================================

def _shift_window(end_time_raw, day_start, cutoff):
    dt = parse_dt(end_time_raw)
    if dt is None:
        return None
    cal_date = dt.date()
    anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                        6, 0, 0, tzinfo=TZ_MSK)
    if dt < anchor:
        cal_date = cal_date - timedelta(days=1)
        anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                            6, 0, 0, tzinfo=TZ_MSK)
    shift_co = anchor + timedelta(days=1)
    return cal_date, anchor, shift_co


def run_diagnostics():
    report_date, prev_d, stamp = _resolve_analytics_dates()
    print(
        f"=== ДИАГНОСТИКА АГРЕГАТОВ (штамп {stamp}, сутки {report_date}, сравн. {prev_d}) ===\n"
    )
    print("Загружаем справочники...")
    field_to_crop, field_to_firm, field_area, machine_group, machines_map, machine_is_mtk, crops_map = load_reference_data()

    # Дополнительно: машины уже загружены в machines_map
    machine_name_map = {mid: (m.get("name") or m.get("title") or f"id={mid}")
                        for mid, m in machines_map.items()}

    # Вариант А: implements
    print("\nЗагружаем /implements для варианта А...")
    implement_name, implement_is_hired, implement_firm = _diag_impl_load_implements()

    # Запускаем оба варианта
    async def _run_both():
        await _diag_variant_a(report_date, field_to_crop, field_to_firm,
                               machine_group, machine_name_map,
                               implement_name, implement_is_hired)
        await _diag_variant_b(report_date, field_to_crop, field_to_firm,
                               machine_group, machine_name_map, machines_map)

    asyncio.run(_run_both())
    print("\nДиагностика завершена.")


if __name__ == "__main__":
    if "--diag" in sys.argv:
        run_diagnostics()
    else:
        main()