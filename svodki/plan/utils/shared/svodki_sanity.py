# -*- coding: utf-8 -*-
"""Встроенные проверки данных Cropwise: культура, обработанная площадь, задания.

Расчёт сводок НЕ меняет: только считает замечания и печатает итог (строки ``DATA_CHECK``)
при завершении процесса. Работает на данных, которые сводка и так уже загрузила, — новых
запросов к API нет (кроме одноразовой докачки history_items, см. ``settled``).

Цена. Один проход по строкам площади, O(строк). В строке DATA_CHECK — до SAMPLES примеров.
Полный список строк с ошибкой печатается отдельно (DATA_CHECK_PLACES), чтобы окно в интерфейсе
показало все задания. Выключить целиком: ``SVODKI_SANITY=0``.

Что проверяется (поля — по документации Cropwise API v3):

  Строка площади (MachineTaskFieldMappingItem)
    AREA_BAD         covered_area не число / NaN / < 0
    AREA_HOURLY      сумма covered_area_hourly расходится с covered_area (смены посчитаются неверно)
    AREA_GT_WORK     covered_area больше work_area (work_area включает перекрытия, т.е. ≥ covered)
    AREA_RATE        выработка га/ч по work_duration нереальна
    AREA_NO_TIME     площадь есть, а work_duration = 0
    AREA_MANUAL      covered_area задана вручную (manually_defined_covered_area)
    ROW_DUP          две строки одного задания на одно поле и одну историю
    FIELD_UNKNOWN    поля нет в справочнике полей сводки — площадь выпадет из отчёта

  Культура (HistoryItem)
    CROP_NO_HISTORY  у строки нет history_item_id — культура неизвестна
    CROP_UNRESOLVED  history_item не найден или без crop_id
    CROP_FIELD       history_item.field_id ≠ field_id строки (культура чужого поля)
    CROP_SPLIT       на одном поле в одном году строки ссылаются на разные культуры
    CROP_WORK        уборка по «пару» и т.п. (список SVODKI_SANITY_NO_HARVEST_CROPS)

  Задание (MachineTask)
    TASK_SUM         сумма строк ≠ task.covered_area (сводка молча масштабирует к task)
    TASK_NO_ROWS     у задания есть площадь, но нет строк по полям (делится поровну по фирмам)
    TASK_ROWS_ONLY   строки с площадью, а task.covered_area = 0 (в сводку не попадёт)
    TASK_TIME        end_time < start_time или слишком длинное задание
    TASK_SEASON      season задания вне допустимого для операции/сводки
    WT_UNMAPPED      work_type_id нет ни в одной группе «Редактора планов»

Пороги — переменные окружения SVODKI_SANITY_* (см. блок «Пороги» ниже).
"""
from __future__ import annotations

import atexit
import json
import os
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

MARK = "DATA_CHECK"
SAMPLES = 3  # примеров в короткой строке DATA_CHECK
_EX_CAP = 5000  # полный список для окна «все задания»


def _env_f(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


ENABLED = (os.environ.get("SVODKI_SANITY", "1").strip().lower()
           not in {"0", "false", "no", "off", "нет"})

# ------------------------------------------------------------------ Пороги
HOURLY_REL = _env_f("SVODKI_SANITY_HOURLY_REL", 0.02)  # доля от covered_area
HOURLY_ABS = _env_f("SVODKI_SANITY_HOURLY_ABS", 0.10)  # га
WORK_TOL = _env_f("SVODKI_SANITY_WORK_TOL", 1.10)
TASK_REL = _env_f("SVODKI_SANITY_TASK_REL", 0.02)
TASK_ABS = _env_f("SVODKI_SANITY_TASK_ABS", 0.5)       # как DIFF_MIN_HA в proverka
MAX_HA_H = _env_f("SVODKI_SANITY_MAX_HA_H", 250.0)     # га/ч на одну строку
MIN_NO_TIME_HA = _env_f("SVODKI_SANITY_NO_TIME_HA", 1.0)
MAX_TASK_H = _env_f("SVODKI_SANITY_MAX_TASK_H", 48.0)
WT_UNMAPPED_MIN_HA = _env_f("SVODKI_SANITY_WT_MIN_HA", 1.0)  # меньше — не шумим
NO_HARVEST_CROPS = frozenset(
    x.strip().casefold()
    for x in (os.environ.get("SVODKI_SANITY_NO_HARVEST_CROPS")
              or "пар,чистый пар,черный пар,чёрный пар,сидеральный пар,залежь").split(",")
    if x.strip()
)

# (серьёзность, пояснение)
CODES: dict[str, tuple[str, str]] = {
    "AREA_BAD": ("CRIT", "covered_area не число или отрицательная"),
    "CROP_NO_HISTORY": ("CRIT", "у строки нет history_item_id — культура неизвестна"),
    "CROP_UNRESOLVED": ("CRIT", "history_item не найден или без культуры"),
    "CROP_FIELD": ("CRIT", "history_item принадлежит другому полю"),
    "AREA_HOURLY": ("WARN", "сумма covered_area_hourly ≠ covered_area"),
    "AREA_GT_WORK": ("WARN", "covered_area больше work_area"),
    "AREA_RATE": ("WARN", "нереальная выработка га/ч"),
    "AREA_NO_TIME": ("WARN", "площадь есть, времени работы нет"),
    "ROW_DUP": ("WARN", "дубль строки (задание, поле, история)"),
    "CROP_SPLIT": ("WARN", "на поле в одном году разные культуры"),
    "CROP_WORK": ("WARN", "вид работ не подходит культуре"),
    "TASK_SUM": ("WARN", "сумма строк ≠ task.covered_area (сводка масштабирует к заданию)"),
    "TASK_NO_ROWS": ("WARN", "у задания нет строк по полям (площадь делится поровну)"),
    "TASK_ROWS_ONLY": ("WARN", "строки с площадью при task.covered_area = 0"),
    "TASK_TIME": ("WARN", "некорректное время задания"),
    "FIELD_UNKNOWN": ("INFO", "поля нет в справочнике (площадь выпадет из сводки)"),
    "AREA_MANUAL": ("INFO", "площадь задана вручную"),
    "TASK_SEASON": ("WARN", "season задания вне допустимого для операции/сводки"),
    "WT_UNMAPPED": ("WARN", "вид работ не входит ни в одну группу «Редактора планов» (площадь выпадет из сводок)"),
    "CROP_SILAGE": ("WARN", "скашивание кукурузы (wt 84) без уборки на силос в 5.0"),
}
_ORDER = {"CRIT": 0, "WARN": 1, "INFO": 2}

# ------------------------------------------------------------------ Состояние
_N: Counter = Counter()          # код → число замечаний
_HA: Counter = Counter()         # код → га под замечанием
_EX: dict[str, list[str]] = {}   # код → примеры
_SEEN_FIELDS: dict[str, set[int]] = {}  # код → поля, уже попавшие в список
_STAT = {"rows": 0, "tasks": 0}

# history_item_id → (crop_id << 52) | (year << 40) | field_id   (одно int вместо кортежа)
_HIST: dict[int, int] = {}
_DIRTY: dict[int, int] = {}      # ещё не записано в хранилище
_TASK: dict[int, tuple[int, float]] = {}   # task_id → (work_type_id, covered_area)
_DONE: set[int] = set()          # задания, чьи строки уже проверены
_FY: dict[int, int] = {}         # field_id*4096+year → crop_id
_SPLIT: set[int] = set()
_FIELD_AREA: dict = {}           # ссылка на словарь сводки field_id → га (не копия)
_HARVEST_WT: frozenset[int] | None = None
_KNOWN_WT: frozenset[int] | None = None
_WT_TO_GROUPS: dict[int, list[str]] | None = None
_PLOW_WT: frozenset[int] | None = None
_TASK_SEASON_SEEN: set[tuple[int, str]] = set()
_SILAGE_MOW_HA = 0.0
_SILAGE_HARVEST_HA = 0.0
_CORN_SILAGE_CHOP_WT = 84
_CORN_SILAGE_LABEL = "кукуруза на силос"
_FILE_TOTAL_REL = _env_f("SVODKI_SANITY_FILE_REL", 0.02)
_FILE_TOTAL_ABS = _env_f("SVODKI_SANITY_FILE_ABS", 0.5)
_registered = False

_FID_MASK = (1 << 40) - 1


def pack_hist(crop_id: int, year: int, field_id: int) -> int:
    return (int(crop_id) << 52) | ((int(year) & 0xFFF) << 40) | (int(field_id) & _FID_MASK)


def unpack_hist(v: int) -> tuple[int, int, int]:
    return v >> 52, (v >> 40) & 0xFFF, v & _FID_MASK


# ------------------------------------------------------------------ Утилиты
def _f(v) -> float | None:
    """float или None; NaN → None."""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x != x else x


def _i(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _field_id_from_note(note: str) -> int | None:
    """«поле=4419» в тексте замечания. Нет числа — замечание не про поле."""
    mark = "поле="
    i = note.find(mark)
    if i < 0:
        return None
    digits: list[str] = []
    for ch in note[i + len(mark):]:
        if ch.isdigit():
            digits.append(ch)
        else:
            break
    return int("".join(digits)) if digits else None


def _hit(code: str, ha: float = 0.0, note: str = "") -> None:
    """Одно поле — одна строка. Второе задание на том же поле чинится тем же способом."""
    field_id = _field_id_from_note(note) if note else None
    if field_id is not None:
        seen = _SEEN_FIELDS.setdefault(code, set())
        if field_id in seen:
            if ha:
                _HA[code] += abs(ha)
            return
        seen.add(field_id)
    _N[code] += 1
    if ha:
        _HA[code] += abs(ha)
    ex = _EX.setdefault(code, [])
    if note and len(ex) < _EX_CAP:
        ex.append(note)


def _ensure_registered() -> None:
    global _registered
    if not _registered:
        _registered = True
        atexit.register(report)


def _wt_ignored() -> frozenset[int]:
    """Транспорт и прочие нерабочие id: SVODKI_SANITY_WT_IGNORE=7,8."""
    raw = os.environ.get("SVODKI_SANITY_WT_IGNORE") or ""
    return frozenset(int(x) for x in raw.replace(";", ",").split(",") if x.strip().isdigit())


def _known_wts() -> frozenset[int]:
    """work_type_id из «Редактора планов» плюс осознанные добавки proverka.

    Id 84 (силос) добавляется к «5.0 Уборка» (WORK_GROUP_WT_EXTRAS), в plans.WORK_GROUPS его нет.
    Пустой реестр (импорт не удался) отключает проверку, чтобы не засорять лог.
    """
    global _KNOWN_WT
    if _KNOWN_WT is None:
        ids: set[int] = set()
        try:
            from plans import WORK_GROUPS  # уже с правками редактора

            ids.update(int(w) for _, group in WORK_GROUPS for w in group)
        except Exception:
            pass
        try:
            from proverka.work_groups import WORK_GROUP_WT_EXTRAS

            ids.update(int(w) for group in WORK_GROUP_WT_EXTRAS.values() for w in group)
        except Exception:
            pass
        _KNOWN_WT = frozenset(ids)
    if not _KNOWN_WT:
        return _KNOWN_WT
    return _KNOWN_WT | _wt_ignored()


def _harvest_wts() -> frozenset[int]:
    global _HARVEST_WT
    if _HARVEST_WT is None:
        try:
            from plans import WORK_GROUPS

            _HARVEST_WT = frozenset(
                int(w) for name, ids in WORK_GROUPS if name == "5.0 Уборка" for w in ids
            )
        except Exception:
            _HARVEST_WT = frozenset()
    return _HARVEST_WT


def _plow_wts() -> frozenset[int]:
    global _PLOW_WT
    if _PLOW_WT is None:
        try:
            from plans import WORK_GROUPS

            _PLOW_WT = frozenset(
                int(w) for name, ids in WORK_GROUPS if name == "2.4 Вспашка" for w in ids
            )
        except Exception:
            _PLOW_WT = frozenset()
    return _PLOW_WT


def _wt_to_groups() -> dict[int, list[str]]:
    global _WT_TO_GROUPS
    if _WT_TO_GROUPS is None:
        try:
            from plans import WORK_GROUPS

            m: dict[int, list[str]] = {}
            for name, ids in WORK_GROUPS:
                for wt in ids:
                    m.setdefault(int(wt), []).append(name)
            _WT_TO_GROUPS = m
        except Exception:
            _WT_TO_GROUPS = {}
    return _WT_TO_GROUPS


def _report_date() -> date | None:
    raw = (os.environ.get("REPORT_DATE") or "").strip().split("T")[0]
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _file_checks_enabled() -> bool:
    if not ENABLED:
        return False
    raw = (os.environ.get("SVODKI_SANITY_FILE", "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off", "нет"}


def _log_immediate(
    severity: str,
    code: str,
    message: str,
    report_id: str | None = None,
) -> str:
    label = (report_id or os.environ.get("SVODKI_REPORT_ID") or "").strip() or "?"
    line = f"{MARK} [{severity}] сводка={label} {code}: {message}"
    print(line, flush=True)
    return line


def _check_task_season(task: dict) -> None:
    """Допустимый season machine_task для операций сводки (season_ranges + season_policy)."""
    rd = _report_date()
    if rd is None:
        return
    tid = _i(task.get("id"))
    wt = _i(task.get("work_type_id")) or 0
    if tid is None or not wt:
        return
    try:
        from season_unified import resolve_group_season_config, task_fact_season_text
    except Exception:
        return
    season = task_fact_season_text(task)
    if not season:
        return
    label = (os.environ.get("SVODKI_REPORT_ID") or "").strip() or "?"
    plow_wts = _plow_wts()
    for group in _wt_to_groups().get(wt, ()):
        cfg = resolve_group_season_config(group, rd)
        allowed = cfg.get("task_season_years") or []
        if not allowed:
            continue
        if cfg.get("task_season_plow_only") and wt not in plow_wts:
            continue
        allowed_s = {str(y) for y in allowed}
        if season in allowed_s:
            continue
        key = (tid, group)
        if key in _TASK_SEASON_SEEN:
            continue
        _TASK_SEASON_SEEN.add(key)
        need = "+".join(sorted(allowed_s))
        _hit(
            "TASK_SEASON",
            _f(task.get("covered_area")) or 0.0,
            f"задание={tid} season={season} нужен={need} операция={group} сводка={label}",
        )


def _note_silage_area(wt: int, hid: int | None, ha: float, crop_names: dict) -> None:
    global _SILAGE_MOW_HA, _SILAGE_HARVEST_HA
    if ha < 0.01:
        return
    if wt == _CORN_SILAGE_CHOP_WT:
        _SILAGE_MOW_HA = round(_SILAGE_MOW_HA + ha, 2)
        return
    if wt not in _harvest_wts() or hid is None:
        return
    name = (crop_names.get(hid) or "").casefold()
    if _CORN_SILAGE_LABEL in name:
        _SILAGE_HARVEST_HA = round(_SILAGE_HARVEST_HA + ha, 2)


def note_silage_harvest_ha(ha: float) -> None:
    """Явный факт «Кукуруза на силос» в уборке 5.0 (если модуль уже посчитал итог)."""
    global _SILAGE_HARVEST_HA
    if ENABLED and ha > _SILAGE_HARVEST_HA:
        _SILAGE_HARVEST_HA = round(float(ha), 2)


def _check_silage_balance() -> None:
    if _SILAGE_MOW_HA >= 0.01 and _SILAGE_HARVEST_HA < 0.01:
        _hit(
            "CROP_SILAGE",
            _SILAGE_MOW_HA,
            f"скашивание wt={_CORN_SILAGE_CHOP_WT}={_SILAGE_MOW_HA:.2f} га; "
            f"уборка 5.0 «{_CORN_SILAGE_LABEL}»={_SILAGE_HARVEST_HA:.2f} га",
        )


def _cross_op_dir(report_date: date) -> Path:
    try:
        from report_platform import upload_dir
    except Exception:
        ud = (os.environ.get("UPLOAD_DIR") or os.environ.get("SVODKI_UPLOAD_DIR") or "uploads").strip()
        root = Path(ud)
        if not root.is_absolute():
            root = Path.cwd() / root
    else:
        root = upload_dir()
    d = root / report_date.isoformat() / ".data_check" / "cross_op"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cross_op_daily_from_report2(
    work_data: dict,
    shift_date: date,
) -> dict[str, float]:
    """Суточный факт по операциям сводки 2 (колонка последнего дня / смена)."""
    out: dict[str, float] = {}
    for group_name, gd in (work_data or {}).items():
        if not isinstance(gd, dict):
            continue
        total = 0.0
        for _firm, by_date in (gd.get("area_by_firm_date") or {}).items():
            if isinstance(by_date, dict):
                total += float(by_date.get(shift_date) or 0)
        if total >= 0.01:
            out[str(group_name)] = round(total, 2)
    return out


def publish_cross_op_snapshot(operations: dict[str, float]) -> None:
    """Маленький JSON агрегатов операций — для CROSS_OP после всех сводок."""
    if not ENABLED:
        return
    rd = _report_date()
    rid = (os.environ.get("SVODKI_REPORT_ID") or "").strip()
    if rd is None or not rid or not operations:
        return
    payload = {
        "report_id": rid,
        "report_date": rd.isoformat(),
        "operations": {str(k): round(float(v or 0), 2) for k, v in operations.items()},
    }
    path = _cross_op_dir(rd) / f"{rid}.json"
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def finalize_cross_op_checks(
    report_date: date | None = None,
    report_id: str | None = None,
) -> list[str]:
    """Сверка факта за смену между сводками по JSON-снимкам (без proverka batch)."""
    if not ENABLED:
        return []
    rd = report_date or _report_date()
    if rd is None:
        return []
    lines: list[str] = []
    folder = _cross_op_dir(rd)
    by_report: dict[str, dict[str, float]] = {}
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            lines.append(
                _log_immediate(
                    "WARN", "CROSS_OP", f"не прочитан снимок {path.name}", report_id=report_id,
                )
            )
            continue
        rid = str(data.get("report_id") or path.stem)
        ops = data.get("operations") or {}
        if isinstance(ops, dict):
            by_report[rid] = {str(k): float(v or 0) for k, v in ops.items()}
    if len(by_report) < 2:
        return lines
    try:
        from proverka.ha_thresholds import diff_is_significant
        from proverka.operation_registry import OPERATION_PEERS, peer_reports_for_group
    except Exception as exc:
        lines.append(
            _log_immediate(
                "WARN", "CROSS_OP", f"модуль сверки недоступен: {exc!r}", report_id=report_id,
            )
        )
        return lines
    present = set(by_report)
    for group_name in OPERATION_PEERS:
        peers = [p for p in peer_reports_for_group(group_name) if p in present]
        if len(peers) < 2:
            continue
        totals = {p: by_report[p].get(group_name) for p in peers}
        nums = [v for v in totals.values() if v is not None]
        if len(nums) < 2:
            continue
        max_diff = round(max(nums) - min(nums), 2)
        if not diff_is_significant(max_diff):
            continue
        parts = ", ".join(f"{p}={totals[p]:.2f}" for p in peers if totals.get(p) is not None)
        lines.append(
            _log_immediate(
                "CRIT",
                "CROSS_OP",
                f"операция={group_name} расхождение={max_diff:.2f} га ({parts})",
                report_id=report_id,
            )
        )
    return lines


def log_cross_op_data_check(all_results: dict) -> None:
    """CROSS_OP по результатам proverka (cross_check уже посчитан)."""
    if not ENABLED:
        return
    cross = all_results.get("cross_check") or {}
    for row in cross.get("operations") or []:
        if not isinstance(row, dict):
            continue
        if row.get("status") != "DIFF":
            continue
        group = row.get("display_name") or row.get("group_name") or "?"
        diff = float(row.get("max_diff_ha") or 0)
        peers = row.get("active_reports") or row.get("peer_reports") or []
        totals = row.get("totals") or {}
        parts = ", ".join(f"{p}={float(totals.get(p) or 0):.2f}" for p in peers if p in totals)
        _log_immediate(
            "CRIT",
            "CROSS_OP",
            f"операция={group} расхождение={diff:.2f} га ({parts})",
        )


def check_file_total_after_save(
    calc_ha: float | None,
    *,
    xlsx_path: Path | str | None = None,
    report_date: date | None = None,
    report_id: str | None = None,
) -> str | None:
    """Сверка «Итого» в xlsx с расчётом модуля (SVODKI_SANITY_FILE=0 отключает)."""
    if not _file_checks_enabled():
        return None
    rd = report_date or _report_date()
    rid = (report_id or os.environ.get("SVODKI_REPORT_ID") or "").strip() or "?"
    if rd is None:
        return None
    try:
        from proverka.file_values import read_file_total_ha, read_file_total_ha_from_path
    except Exception as exc:
        return _log_immediate(
            "WARN", "FILE_TOTAL", f"модуль чтения xlsx недоступен: {exc!r}", report_id=rid,
        )
    fv: dict
    if xlsx_path is not None:
        fv = read_file_total_ha_from_path(Path(xlsx_path))
    else:
        fv = read_file_total_ha(rd, rid)
    if fv.get("error"):
        return _log_immediate("WARN", "FILE_TOTAL", str(fv.get("error")), report_id=rid)
    if not fv.get("found"):
        return None
    file_ha = float(fv.get("file_ha") or 0)
    if calc_ha is None:
        return None
    calc = round(float(calc_ha), 2)
    diff = abs(calc - file_ha)
    tol = max(_FILE_TOTAL_ABS, _FILE_TOTAL_REL * max(calc, file_ha, 0.01))
    if diff <= tol:
        return None
    return _log_immediate(
        "CRIT",
        "FILE_TOTAL",
        f"расчёт={calc:.2f} га файл={file_ha:.2f} га Δ={diff:.2f} га",
        report_id=rid,
    )


def emit_calc_total_ha(total_ha: float) -> None:
    """Строка для родительского процесса генерации (парсится в report_generation)."""
    print(f"DATA_CHECK_CALC_TOTAL={round(float(total_ha), 2):.2f}", flush=True)


# ------------------------------------------------------------------ Регистрация данных
def register_field_areas(mapping: dict | None) -> None:
    """Запомнить ссылку на словарь field_id → площадь (га), который сводка уже построила."""
    global _FIELD_AREA
    if ENABLED and mapping:
        _FIELD_AREA = mapping
        _ensure_registered()


def remember_history(hi: dict) -> None:
    """HistoryItem из API → (crop_id, year, field_id). Зовётся для каждого загруженного history_item."""
    if not ENABLED:
        return
    hid = _i(hi.get("id"))
    if hid is None:
        return
    v = pack_hist(_i(hi.get("crop_id")) or 0, _i(hi.get("year")) or 0, _i(hi.get("field_id")) or 0)
    if _HIST.get(hid) != v:
        _HIST[hid] = v
        _DIRTY[hid] = v


def settled(hid: int, have_crop_name: bool) -> bool:
    """True — history_item уже известен и повторно качать его не нужно.

    Без проверок (SVODKI_SANITY=0) совпадает с прежним условием «культура уже в кэше».
    Для item без crop_id (удалённая/пустая культура) сохраняет факт «уже смотрели», чтобы
    не тянуть его из API при каждом запуске.
    """
    if not ENABLED:
        return have_crop_name
    v = _HIST.get(hid)
    if v is None:
        return False
    return have_crop_name or (v >> 52) == 0


def load_meta(hid: int, packed: int) -> None:
    """Из хранилища (history_meta). Заодно чинит проверку сезонов на пути через хранилище."""
    if not ENABLED:
        return
    _HIST.setdefault(int(hid), int(packed))
    year = (int(packed) >> 40) & 0xFFF
    if year:
        try:
            from season_mismatch_log import remember_history_item

            remember_history_item(int(hid), year)
        except Exception:
            pass


def take_dirty() -> list[tuple[int, int]]:
    """Новые history_item для записи в хранилище; список сбрасывается."""
    if not _DIRTY:
        return []
    out = list(_DIRTY.items())
    _DIRTY.clear()
    return out


def has_dirty() -> bool:
    return bool(_DIRTY)


# ------------------------------------------------------------------ Задания
def on_tasks(tasks) -> None:
    """machine_tasks, только что загруженные/полученные из хранилища."""
    if not ENABLED or not tasks:
        return
    _ensure_registered()
    for t in tasks:
        tid = _i(t.get("id"))
        if tid is None or tid in _TASK:
            continue
        cov = _f(t.get("covered_area")) or 0.0
        _TASK[tid] = (_i(t.get("work_type_id")) or 0, cov)
        _STAT["tasks"] += 1
        _check_task_season(t)
        wt = _TASK[tid][0]
        if wt and cov >= WT_UNMAPPED_MIN_HA:
            known = _known_wts()
            if known and wt not in known:
                _hit(
                    "WT_UNMAPPED",
                    cov,
                    f"задание={tid} work_type={wt} сезон={t.get('season')} {cov:.1f} га",
                )
        st, en = t.get("start_time"), t.get("end_time")
        if st and en:
            try:
                d0 = datetime.fromisoformat(str(st).replace("Z", "+00:00"))
                d1 = datetime.fromisoformat(str(en).replace("Z", "+00:00"))
                hours = (d1 - d0).total_seconds() / 3600.0
            except (TypeError, ValueError):
                continue
            if hours < 0:
                _hit("TASK_TIME", cov, f"задание={tid} конец раньше начала ({hours:.1f} ч)")
            elif hours > MAX_TASK_H:
                _hit("TASK_TIME", cov, f"задание={tid} длится {hours:.0f} ч")


# ------------------------------------------------------------------ Строки площади
def on_mtfmi(mtfmi_map: dict, requested=None, *, crops_known: bool = True) -> None:
    """Строки площади по заданиям: {task_id: [row, ...]}.

    ``requested`` — id заданий, которые запрашивали: те, что вернулись без строк, проверяются
    на TASK_NO_ROWS (по самой карте их не видно).
    ``crops_known=False`` — history_items для этих строк не загружались: проверки культуры
    пропускаются (иначе каждая строка стала бы CROP_UNRESOLVED).
    """
    if not ENABLED or not mtfmi_map and not requested:
        return
    _ensure_registered()
    try:
        from mtfmi_batch_fetch import _HISTORY_CROP as crop_names
    except Exception:
        crop_names = {}
    harvest = _harvest_wts() if NO_HARVEST_CROPS else frozenset()
    fa = _FIELD_AREA
    crops_known = crops_known and bool(_HIST)

    for raw_tid, rows in (mtfmi_map or {}).items():
        tid = _i(raw_tid)
        if tid is None or tid in _DONE:
            continue
        _DONE.add(tid)
        wt, task_cov = _TASK.get(tid, (0, None))
        total = 0.0
        seen: set | None = set() if rows and len(rows) > 1 else None

        for r in rows or ():
            _STAT["rows"] += 1
            raw_c = r.get("covered_area")
            c = _f(raw_c)
            fid = _i(r.get("field_id"))
            hid = _i(r.get("history_item_id"))
            where = f"задание={tid} поле={fid}"
            if raw_c is not None and (c is None or c < 0):
                _hit("AREA_BAD", 0.0, f"{where} covered_area={raw_c!r}")
                continue
            if not c:
                continue
            total += c
            manual = r.get("manually_defined_covered_area") is True

            # --- площадь
            if manual:
                _hit("AREA_MANUAL", c, f"{where} {c:.2f} га")
            else:
                h = r.get("covered_area_hourly")
                if h:
                    s = 0.0
                    for e in h:
                        s += _f(e[1]) or 0.0
                    if abs(s - c) > max(HOURLY_ABS, HOURLY_REL * c):
                        _hit("AREA_HOURLY", s - c, f"{where} Σчасов={s:.2f} covered={c:.2f}")
                wd = _f(r.get("work_duration"))
                if wd is not None:
                    if wd > 0:
                        rate = c / (wd / 3600.0)
                        if rate > MAX_HA_H:
                            _hit("AREA_RATE", c, f"{where} {c:.1f} га за {wd / 3600:.2f} ч = {rate:.0f} га/ч")
                    elif c >= MIN_NO_TIME_HA:
                        _hit("AREA_NO_TIME", c, f"{where} {c:.1f} га, work_duration=0")
            wa = _f(r.get("work_area"))
            if wa and wa > 0 and c > wa * WORK_TOL + 0.05:
                _hit("AREA_GT_WORK", c - wa, f"{where} covered={c:.2f} work_area={wa:.2f}")

            # --- поле
            # Площадь строки больше площади поля не ошибка: несколько проходов дают
            # covered_area больше tillable_area. На гектары сводки это не влияет.
            if fid is not None and fa and fa.get(fid) is None:
                _hit("FIELD_UNKNOWN", c, where)

            if seen is not None and fid is not None:
                key = (fid, hid)
                if key in seen:
                    _hit("ROW_DUP", c, f"{where} history={hid}")
                seen.add(key)

            # --- культура
            if not crops_known:
                continue
            if hid is None:
                _hit("CROP_NO_HISTORY", c, where)
                continue
            v = _HIST.get(hid)
            if v is None:
                _hit("CROP_UNRESOLVED", c, f"{where} history={hid} не найден")
                continue
            crop_id, year, h_fid = unpack_hist(v)
            if not crop_id:
                _hit("CROP_UNRESOLVED", c, f"{where} history={hid} без crop_id")
                continue
            if h_fid and fid and h_fid != fid:
                _hit("CROP_FIELD", c, f"{where} history={hid} принадлежит полю {h_fid}")
            if fid and year:
                k = fid * 4096 + year
                prev = _FY.setdefault(k, crop_id)
                if prev != crop_id and k not in _SPLIT:
                    _SPLIT.add(k)
                    _hit("CROP_SPLIT", c, f"поле={fid} год={year} культуры {prev} и {crop_id}")
            if harvest and wt in harvest:
                name = (crop_names.get(hid) or "").casefold()
                if name in NO_HARVEST_CROPS:
                    _hit("CROP_WORK", c, f"{where} уборка по культуре «{name}»")
            _note_silage_area(wt, hid, c, crop_names)

        # --- задание целиком
        if task_cov is not None:
            if task_cov > 0 and total <= 0:
                _hit("TASK_NO_ROWS", task_cov, f"задание={tid} covered_area={task_cov:.2f}")
            elif task_cov <= 0 < total:
                _hit("TASK_ROWS_ONLY", total, f"задание={tid} Σстрок={total:.2f}")
            elif task_cov > 0 and abs(total - task_cov) > max(TASK_ABS, TASK_REL * task_cov):
                _hit("TASK_SUM", total - task_cov, f"задание={tid} Σстрок={total:.2f} задание={task_cov:.2f}")

    for raw_tid in requested or ():
        tid = _i(raw_tid)
        if tid is None or tid in _DONE or (mtfmi_map and tid in mtfmi_map):
            continue
        _DONE.add(tid)
        info = _TASK.get(tid)
        if info and info[1] > 0:
            _hit("TASK_NO_ROWS", info[1], f"задание={tid} covered_area={info[1]:.2f}")


# ------------------------------------------------------------------ Итог
def summary() -> dict:
    return {
        "report": (os.environ.get("SVODKI_REPORT_ID") or "").strip() or "?",
        "rows": _STAT["rows"],
        "tasks": _STAT["tasks"],
        "codes": {
            c: {"severity": CODES[c][0], "count": n, "ha": round(_HA[c], 2),
                "text": CODES[c][1], "examples": list(_EX.get(c, ()))}
            for c, n in _N.items()
        },
    }


def report(out=None) -> None:
    """Печатает замечания. Вызывается при выходе из процесса; можно и вручную."""
    if ENABLED:
        _check_silage_balance()
    if not ENABLED or not (_STAT["rows"] or _STAT["tasks"] or _N):
        return
    out = out or sys.stdout
    s = summary()
    label = s["report"]
    codes = sorted(s["codes"].items(), key=lambda kv: (_ORDER[kv[1]["severity"]], -kv[1]["ha"]))
    crit = sum(1 for _, v in codes if v["severity"] == "CRIT")
    warn = sum(1 for _, v in codes if v["severity"] == "WARN")
    try:
        for code, v in codes:
            shown = v["examples"][:SAMPLES]
            ex = "; ".join(shown)
            print(f"{MARK} [{v['severity']}] сводка={label} {code}: {v['count']} шт, "
                  f"{v['ha']:.1f} га — {v['text']}" + (f" | {ex}" if ex else ""), file=out, flush=True)
            if v["examples"]:
                blob = json.dumps(v["examples"], ensure_ascii=False, separators=(",", ":"))
                print(f"{MARK}_PLACES сводка={label} code={code} {blob}", file=out, flush=True)
        verdict = "замечаний нет" if not codes else f"CRIT={crit} WARN={warn}"
        print(f"{MARK} итог сводка={label}: проверено строк={s['rows']} заданий={s['tasks']}; {verdict}",
              file=out, flush=True)
        path = (os.environ.get("SVODKI_SANITY_JSON") or "").strip()
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(s, fh, ensure_ascii=False)
    except (OSError, ValueError):
        pass  # поток закрыт при завершении — проверки не должны ронять сводку


def reset() -> None:
    """Для тестов."""
    for d in (_N, _HA, _EX, _HIST, _DIRTY, _TASK, _FY, _SEEN_FIELDS):
        d.clear()
    _DONE.clear()
    _SPLIT.clear()
    _STAT.update(rows=0, tasks=0)
    global _FIELD_AREA, _SILAGE_MOW_HA, _SILAGE_HARVEST_HA
    _FIELD_AREA = {}
    _TASK_SEASON_SEEN.clear()
    _SILAGE_MOW_HA = 0.0
    _SILAGE_HARVEST_HA = 0.0
