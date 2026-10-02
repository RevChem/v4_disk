from __future__ import annotations

import gc
import io
import json
import logging
import math
import os
import re
import signal
from collections.abc import Callable
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_cropwise_token, get_namolot_config, settings, _upload_dir_path
from app.crud import create_summary, get_visible_summary_by_title, list_summaries_by_date
from app.database import SessionLocal
from app.legacy.manual_overrides import (
    combine_season_start_for_date,
    rating_period_from_for_date,
    r23_work_data_from_report2_bundle_path,
    r23_work_narrative_prefill_path,
    write_effective_manual_store_for_date,
)
from app.legacy.plans_editor import inject_plans_edits_env
from app.legacy.season_meta import inject_season_range_env
from app.models import Summary

logger = logging.getLogger(__name__)


def _allow_replace_visible_summaries() -> bool:
    """Глобальный override (для отладки/миграций)."""
    return _env_flag("SVODKI_ALLOW_REPLACE_VISIBLE", False)


def _keep_published_summary(
    report_id: str,
    *,
    replace_visible_summaries: bool,
    requested_report_ids: frozenset[str],
) -> bool:
    """
    True — не трогать уже опубликованную сводку.
    Ручная генерация (replace_visible_summaries=True) пересобирает выбранные отчёты.
    Фоновые задачи (расписание, почта) не перезаписывают опубликованное.
    Сводку 1/1.18 не меняют доноры, если 1 не входит в запрос.
    """
    if _allow_replace_visible_summaries():
        return False
    rid = str(report_id or "").strip()
    if not replace_visible_summaries:
        return True
    if rid in {"1", "1.18"}:
        return rid not in requested_report_ids
    return False


def _summary_is_published(item: Summary | None) -> bool:
    return bool(item is not None and item.visible)


def _publish_summaries(db: Session, summaries: list[Summary]) -> None:
    """После XLSX-postprocess и PDF-graft делаем сводки доступными в API и скачивании."""
    ids = [int(s.id) for s in summaries if getattr(s, "id", None) is not None]
    if not ids:
        return
    for sid in ids:
        row = db.get(Summary, sid)
        if row is not None and not row.visible:
            row.visible = True
    db.commit()


def _script_run_timeout() -> float | None:
    try:
        t = int(settings.svodki_script_timeout_sec)
    except (TypeError, ValueError):
        t = 3600
    return float(t) if t > 0 else None


def _libreoffice_run_timeout() -> float | None:
    try:
        t = int(settings.svodki_libreoffice_timeout_sec)
    except (TypeError, ValueError):
        t = 900
    return float(t) if t > 0 else None


def _pdf_convert_max_workers() -> int:
    try:
        return max(1, int(settings.svodki_pdf_convert_max_workers))
    except (TypeError, ValueError):
        return 2


_pdf_convert_semaphore: threading.BoundedSemaphore | None = None
_pdf_convert_semaphore_size = 0


def _get_pdf_convert_semaphore() -> threading.BoundedSemaphore:
    global _pdf_convert_semaphore, _pdf_convert_semaphore_size
    n = _pdf_convert_max_workers()
    if _pdf_convert_semaphore is None or _pdf_convert_semaphore_size != n:
        _pdf_convert_semaphore = threading.BoundedSemaphore(n)
        _pdf_convert_semaphore_size = n
    return _pdf_convert_semaphore

# Только одна пачка генерации одновременно — иначе параллельные prefetch + скрипты
# перегружают Cropwise API (429 Too Many Requests).
_generation_run_lock = threading.Lock()


@dataclass(frozen=True)
class ReportConfig:
    id: str
    script: str
    title: str


@dataclass(frozen=True)
class ResolvedReportConfig:
    id: str
    script: str
    title: str
    script_dir: Path


class ReportGenerationError(RuntimeError):
    pass


class ReportGenerationCancelled(RuntimeError):
    """Пользовательская отмена задачи генерации на сервере."""


def _cfg_id_norm(cfg: ReportConfig | ResolvedReportConfig) -> str:
    return str(getattr(cfg, "id", "") or "").strip()


def _is_analytics_report_23(cfg: ReportConfig | ResolvedReportConfig) -> bool:
    return _cfg_id_norm(cfg) == "23"


def _is_report_22_only_run(report_ids: list[str] | None) -> bool:
    if report_ids is None:
        return False
    normalized = [str(x).strip() for x in report_ids if str(x).strip()]
    return normalized == ["22"]


def _report1_v2_script_enabled() -> bool:
    """Быстрый скрипт svodki/plan/1_main_ammonium_v2.py — читает prefetch JSON."""
    override = (os.environ.get("SVODKI_REPORT1_SCRIPT") or "").strip()
    return bool(override) and "v2" in override.lower()


def _report1_standalone_enabled() -> bool:
    """Автономная сводка 1: все данные из API, без доноров и PDF-graft."""
    if _env_flag("SVODKI_REPORT1_STANDALONE", False):
        return True
    override = (os.environ.get("SVODKI_REPORT1_SCRIPT") or "").strip()
    return bool(override) and "standalone" in override.lower()


def _report1_uses_prefetch_json(cfg: ReportConfig | ResolvedReportConfig) -> bool:
    """Сводка 1 v2 использует единый prefetch (legacy 1 — живой mtfmi для 1.1 МУ)."""
    if _report1_standalone_enabled():
        return False
    return _cfg_id_norm(cfg) in {"1", "1.18"} and _report1_v2_script_enabled()


def _uses_unified_prefetch_data(cfg: ReportConfig | ResolvedReportConfig) -> bool:
    """
    Единый prefetch JSON (svodki_prefetch.py).

    По умолчанию выключен: каждый plan-скрипт сам ходит в Cropwise (как локально).
    Legacy opt-in: SVODKI_USE_PREFETCH_DATA=1.
    """
    if not _env_flag(USE_PREFETCH_DATA_ENV_VAR, False):
        return False
    if _report1_standalone_enabled() and _cfg_id_norm(cfg) in {"1", "1.18"}:
        return False
    if _report1_uses_prefetch_json(cfg):
        return True
    return _cfg_id_norm(cfg) not in {
        "1", "1.18", "2", "14", "22", "23", "24", "25", "26", "27", "27.1",
    }


# Отчёты с той же структурой Excel/PDF, что «1» (шапки по листам, manual row breaks).
_REPORT1_FAMILY_IDS: frozenset[str] = frozenset({"1", "1.18"})
# Сводки 3–9: page setup (fitToWidth, поля, fitToHeight=0) задаётся в _prepare_xlsx_for_pdf;
# авто-высота строк там для них уже отключена. Без prepare LO режет широкие листы по колонкам.
_FAST_PDF_PREP_SKIP_IDS: frozenset[str] = frozenset()
# Сводка 27 (кол-во) и 27.1 (время) — один период/логика, разные метрики.
_REPORT27_FAMILY_IDS: frozenset[str] = frozenset({"27", "27.1"})


def _is_report27_family(cfg_or_id: ReportConfig | ResolvedReportConfig | str) -> bool:
    if isinstance(cfg_or_id, str):
        return str(cfg_or_id or "").strip() in _REPORT27_FAMILY_IDS
    return _cfg_id_norm(cfg_or_id) in _REPORT27_FAMILY_IDS


def _load_report_catalog_module():
    import importlib.util

    root = Path(settings.svodki_root).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    path = root / "plan" / "report_catalog.py"
    if not path.is_file():
        raise ReportGenerationError(f"Не найден каталог сводок: {path}")
    spec = importlib.util.spec_from_file_location("svodki_report_catalog", path)
    if spec is None or spec.loader is None:
        raise ReportGenerationError(f"Не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _artifact_title_for_report_id(report_id: str, fallback: str = "") -> str:
    rid = str(report_id or "").strip()
    if not rid:
        return fallback
    try:
        cat = _load_report_catalog_module()
        fn = getattr(cat, "artifact_title_for_report", None)
        if callable(fn):
            title = fn(rid, fallback)
            if title:
                return title
        titles = getattr(cat, "REPORT_ARTIFACT_TITLES", None) or {}
        if rid in titles:
            return str(titles[rid])
    except Exception:
        pass
    return fallback


def _artifact_base_title(cfg: ResolvedReportConfig) -> str:
    cfg_id = _cfg_id_norm(cfg)
    titled = _artifact_title_for_report_id(cfg_id, cfg.title)
    return titled or cfg.title


def _build_report_configs_base() -> tuple[ReportConfig, ...]:
    cat = _load_report_catalog_module()
    return tuple(ReportConfig(e.id, e.script, e.title) for e in cat.REPORT_CATALOG)


def _catalog_constants():
    cat = _load_report_catalog_module()
    return (
        dict(cat.REPORT_ID_ALIASES),
        frozenset(cat.EXPLICIT_ONLY_REPORT_IDS),
        frozenset(cat.REPORT_PICKER_HIDDEN_IDS),
        frozenset(cat.REPORT_PICKER_DEFAULT_SELECTED_IDS),
    )


REPORT_CONFIGS_BASE: tuple[ReportConfig, ...] = _build_report_configs_base()
(
    REPORT_ID_ALIASES,
    EXPLICIT_ONLY_REPORT_IDS,
    REPORT_PICKER_HIDDEN_IDS,
    REPORT_PICKER_DEFAULT_SELECTED_IDS,
) = _catalog_constants()
# Основной каталог скриптов — svodki/plan (plans.py, report_catalog.py).

TRANSIENT_MARKERS = (
    "json.decoder.JSONDecodeError",
    "httpx.ReadError",
    "httpcore.ReadError",
    "httpx.ConnectError",
    "httpx.RemoteProtocolError",
    "httpx.ReadTimeout",
    "Too Many Requests",
)
# Коды 429/502/503/504 раньше искались как голые подстроки: любое число в выводе упавшего
# скрипта («1429.5 га», id задачи 50431) превращало обычную ошибку в «временный сбой»,
# и скрипт перезапускался до 5 раз × 3 раунда. Теперь код должен стоять рядом с текстом
# HTTP-ошибки (так пишут requests/httpx) либо после слов status / HTTP.
_TRANSIENT_HTTP_RE = re.compile(
    r"(?<!\d)(?:429|502|503|504)(?!\d)\s+"
    r"(?:Client\s+Error|Server\s+Error|Too\s+Many\s+Requests|Bad\s+Gateway"
    r"|Service\s+Unavailable|Gateway\s+Time-?out)"
    r"|(?:status(?:_code)?|HTTP)\W{0,3}(?:429|502|503|504)(?!\d)",
    re.IGNORECASE,
)
# SIGKILL/OOM (-9), SIGSEGV (-11), SIGPIPE (-13), SIGTERM (-15).
TRANSIENT_EXIT_SIGNALS = frozenset({9, 11, 13, 15})


def _format_ui_report_title(report_id: str, raw_title: str) -> str:
    """Человекочитаемое название для UI (без «РАБОТА ТЕХНИКИ»)."""
    import re

    rid = str(report_id).strip()
    t = str(raw_title or "").strip()
    t = re.sub(rf"^{re.escape(rid)}\.?\s*", "", t, flags=re.I)
    t = re.sub(r"^РАБОТА\s+ТЕХНИКИ\s+", "", t, flags=re.I)
    t = t.strip(" .")
    if t and t[0].isalpha():
        t = t[0].upper() + t[1:]
    return f"{rid}. {t}" if t else str(raw_title)


def list_report_catalog_for_ui() -> list[dict[str, str | bool]]:
    """Каталог сводок для UI: единый источник с REPORT_CONFIGS_BASE."""
    rows: list[dict[str, str | bool]] = []
    for cfg in REPORT_CONFIGS_BASE:
        rid = str(cfg.id)
        rows.append(
            {
                "id": rid,
                "title": _format_ui_report_title(rid, str(cfg.title)),
                "picker_visible": rid not in REPORT_PICKER_HIDDEN_IDS,
                "default_selected": rid in REPORT_PICKER_DEFAULT_SELECTED_IDS,
            }
        )
    def _sort_key(row: dict[str, str | bool]) -> tuple:
        rid = str(row["id"])
        try:
            return (0, -float(rid))
        except ValueError:
            return (1, rid)

    rows.sort(key=_sort_key)
    return rows


def _script_max_attempts() -> int:
    try:
        return max(1, int(settings.svodki_script_retries))
    except (TypeError, ValueError):
        return 3


def _donor_retry_rounds() -> int:
    try:
        return max(0, int(settings.svodki_donor_retry_rounds))
    except (TypeError, ValueError):
        return 3


def _failed_report_retry_rounds() -> int:
    try:
        return max(0, int(settings.svodki_failed_report_retry_rounds))
    except (TypeError, ValueError):
        return 2


def _subprocess_exit_is_transient(rc: int) -> bool:
    if rc == 0:
        return False
    if rc < 0:
        return abs(rc) in TRANSIENT_EXIT_SIGNALS
    if rc >= 128:
        return (rc - 128) in TRANSIENT_EXIT_SIGNALS
    return False


def _season_mismatch_log_lines(stdout: str, title: str) -> list[str]:
    """Строки несовпадения сезона культуры — из всего stdout, не только из хвоста."""
    out: list[str] = []
    for line in (stdout or "").splitlines():
        text = line.strip()
        if "SEASON_MISMATCH" not in text:
            continue
        out.append(f"[{title}] {text}")
        if len(out) >= 80:
            break
    return out


def _subprocess_should_retry(rc: int, stdout: str, stderr: str, attempt: int, max_attempts: int) -> bool:
    if rc == 0 or attempt >= max_attempts:
        return False
    merged = f"{stdout}\n{stderr}"
    return (
        _subprocess_exit_is_transient(rc)
        or any(marker in merged for marker in TRANSIENT_MARKERS)
        or _TRANSIENT_HTTP_RE.search(merged) is not None
    )


def _subprocess_retry_backoff_sec(rc: int, attempt: int) -> float:
    if _subprocess_exit_is_transient(rc):
        # После OOM/SIGKILL даём контейнеру время освободить RAM.
        return float(attempt * 15)
    return float(attempt * 2)

API_SNAPSHOT_ENV_VAR = "SVODKI_API_CACHE_PATH"
PREFETCH_DATA_ENV_VAR = "SVODKI_PREFETCH_DATA_PATH"
PREFETCH_STRICT_ENV_VAR = "SVODKI_PREFETCH_STRICT"
API_SNAPSHOT_READ_ONLY_ENV_VAR = "SVODKI_API_CACHE_READ_ONLY"
PREFETCH_ONLY_ENV_VAR = "SVODKI_PREFETCH_ONLY"
PREFETCH_BEFORE_REPORTS_ENV_VAR = "SVODKI_PREFETCH_BEFORE_REPORTS"
USE_PREFETCH_DATA_ENV_VAR = "SVODKI_USE_PREFETCH_DATA"
USE_API_CACHE_ENV_VAR = "SVODKI_USE_API_CACHE"
DISABLE_API_CACHE_ENV_VAR = "SVODKI_DISABLE_API_CACHE"
REPORT23_FERTILIZER_TRANSPORT_MACHINES_ENV = "SVODKI_R23_FERTILIZER_TRANSPORT_MACHINES"
# Файл с числом (приоритетнее env): веб пишет в каталог запуска, ranis читает до docx.
REPORT23_MACHINES_SIDECAR_ENV = "SVODKI_R23_MACHINES_SIDECAR"
REPORT23_EDITOR_BODY_SIDECAR_ENV = "SVODKI_R23_EDITOR_BODY_SIDECAR"
REPORT23_WORK_SNAPSHOT_ENV = "SVODKI_R23_WORK_SNAPSHOT_OUT"
R23_DOCUMENT_REBUILD_ONLY_ENV = "SVODKI_R23_DOC_REBUILD_ONLY"
R23_WORK_DATA_BUNDLE_OUT_ENV = "SVODKI_R23_WORK_DATA_BUNDLE_OUT"
R23_WORK_DATA_BUNDLE_IN_ENV = "SVODKI_R23_WORK_DATA_BUNDLE_IN"
R23_BUNDLE_ONLY_ENV = "SVODKI_R23_BUNDLE_ONLY"
R23_WORK_DATA_BUNDLE_FILENAME = "r23_work_data_from_report2.json"
REPORT25_MODE_ENV = "SVODKI_REPORT25_MODE"
HARVEST_GROSS_ENV = "SVODKI_REPORT_HARVEST_GROSS_JSON"
REPORT_FOOTNOTES_ENV = "SVODKI_REPORT_HARVEST_FOOTNOTES_JSON"


def _normalize_report_harvest_gross(value: object) -> dict | None:
    if value is None:
        return None
    parsed: object = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None

    def _float_field(entry: dict, field: str) -> float | None:
        if field not in entry or entry[field] is None or entry[field] == "":
            return None
        try:
            return float(entry[field])
        except (TypeError, ValueError):
            return None

    out: dict[str, dict] = {}
    for section in ("crops", "vegetables"):
        block = parsed.get(section)
        if not isinstance(block, dict) or not block:
            continue
        cleaned: dict[str, dict] = {}
        for crop_name, entry in block.items():
            name = str(crop_name or "").strip()
            if not name or not isinstance(entry, dict):
                continue
            row: dict = {}
            by_firm_raw = entry.get("by_firm")
            by_firm_cleaned: dict[str, dict[str, float]] = {}
            season_sum = 0.0
            day_sum = 0.0
            any_season = False
            any_day = False
            if isinstance(by_firm_raw, dict):
                for firm_name, firm_entry in by_firm_raw.items():
                    firm = str(firm_name or "").strip()
                    if not firm or not isinstance(firm_entry, dict):
                        continue
                    firm_row: dict[str, float] = {}
                    season_v = _float_field(firm_entry, "season_t")
                    day_v = _float_field(firm_entry, "day_t")
                    if season_v is not None:
                        firm_row["season_t"] = season_v
                        season_sum += season_v
                        any_season = True
                    if day_v is not None:
                        firm_row["day_t"] = day_v
                        day_sum += day_v
                        any_day = True
                    if firm_row:
                        by_firm_cleaned[firm] = firm_row
            if by_firm_cleaned:
                row["by_firm"] = by_firm_cleaned
                if any_season:
                    row["season_t"] = season_sum
                if any_day:
                    row["day_t"] = day_sum
            else:
                # Старый формат: только итог по холдингу (без by_firm).
                for field in ("season_t", "day_t"):
                    val = _float_field(entry, field)
                    if val is not None:
                        row[field] = val
            if row:
                cleaned[name] = row
        if cleaned:
            out[section] = cleaned
    return out or None


def _normalize_report25_mode(value: object) -> str | None:
    mode = str(value or "").strip().lower()
    if mode in {"morning", "day", "утро", "день"}:
        return "morning" if mode in {"morning", "утро"} else "day"
    return None


def _report25_mode_label(mode: str) -> str:
    return "утро" if mode == "morning" else "день"


def _report25_storage_title(base_title: str, mode: str | None) -> str:
    normalized = _normalize_report25_mode(mode)
    if not normalized:
        return base_title
    if "(утро)" in base_title or "(день)" in base_title:
        return base_title
    suffix = " (утро)" if normalized == "morning" else " (день)"
    return f"{base_title}{suffix}"


def _normalize_report22_cutoff(value: object) -> str | None:
    text = str(value or "").strip()
    if re.match(r"^\d{2}:\d{2}$", text):
        return text
    return None


def _coerce_optional_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_report27_period_header(
    raw: str | None,
) -> tuple[date | None, date | None, str | None, str | None]:
    text = str(raw or "").strip()
    if not text:
        return None, None, None, None
    parts = [p.strip() for p in text.split(";")]
    if len(parts) < 4:
        return None, None, None, None
    d_from = _coerce_optional_date(parts[0])
    d_to = _coerce_optional_date(parts[1])
    t_from = _normalize_report22_cutoff(parts[2])
    t_cutoff = _normalize_report22_cutoff(parts[3])
    if not d_from or not d_to or not t_from or not t_cutoff:
        return None, None, None, None
    return d_from, d_to, t_from, t_cutoff


def _build_report22_window_iso(
    date_from: date | None,
    time_from: str | None,
    date_to: date | None,
    time_to: str | None,
) -> tuple[str | None, str | None]:
    if date_from is None or date_to is None:
        return None, None
    tf = _normalize_report22_cutoff(time_from) or "06:00"
    tt = _normalize_report22_cutoff(time_to)
    if not tt:
        return None, None
    h1, m1 = tf.split(":", 1)
    h2, m2 = tt.split(":", 1)
    start = f"{date_from.isoformat()}T{int(h1):02d}:{int(m1):02d}:00"
    end = f"{date_to.isoformat()}T{int(h2):02d}:{int(m2):02d}:00"
    return start, end


def _build_report2_period_window_iso(
    date_from: date | None,
    time_from: str | None,
    date_to: date | None,
    time_to: str | None,
) -> tuple[str | None, str | None]:
    """
    Окно сводки 2 Excel за период. При срезе «до DD.MM 06:00» последняя смена — день DD.MM,
    поэтому конец окна сдвигаем на +1 сутки 06:00 (как смена [D 06:00, D+1 06:00)).
    """
    start, end = _build_report22_window_iso(date_from, time_from, date_to, time_to)
    if not start or not end:
        return start, end
    tt = _normalize_report22_cutoff(time_to) or "06:00"
    if tt != "06:00":
        return start, end
    we = _parse_report_window_for_iso(end)
    if we is None:
        return start, end
    we = we + timedelta(days=1)
    return start, we.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_report_window_for_iso(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _report22_storage_title(
    base_title: str,
    cutoff_hhmm: str | None,
    time_from_hhmm: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> str:
    from_h = _normalize_report22_cutoff(time_from_hhmm)
    to_h = _normalize_report22_cutoff(cutoff_hhmm)
    if date_from and date_to and from_h and to_h:
        from_label = f"{date_from.strftime('%d.%m')} {from_h}"
        to_label = f"{date_to.strftime('%d.%m')} {to_h}"
        suffix = f" с {from_label} до {to_label}"
        if suffix in base_title:
            return base_title
        return f"{base_title}{suffix}"
    if from_h and to_h:
        suffix = f" с {from_h} до {to_h}"
        if suffix in base_title:
            return base_title
        return f"{base_title}{suffix}"
    hhmm = to_h
    if not hhmm:
        return base_title
    if f" на {hhmm}" in base_title:
        return base_title
    return f"{base_title} на {hhmm}"


_job_subproc_registry_lock = threading.Lock()
_job_active_subprocesses: dict[str, list[subprocess.Popen]] = {}


def kill_generation_job_subprocesses(job_id: str) -> None:
    """Сразу гасим все процессы, связанные с задачей (вызывается из POST /generation-queue/{id}/cancel)."""
    with _job_subproc_registry_lock:
        procs = list(_job_active_subprocesses.get(job_id, ()))
    for p in procs:
        _terminate_report_subprocess_tree(p)


def _register_job_subprocess(job_id: str | None, proc: subprocess.Popen) -> None:
    if job_id:
        with _job_subproc_registry_lock:
            _job_active_subprocesses.setdefault(job_id, []).append(proc)


def _unregister_job_subprocess(job_id: str | None, proc: subprocess.Popen) -> None:
    if not job_id:
        return
    with _job_subproc_registry_lock:
        bucket = _job_active_subprocesses.get(job_id)
        if not bucket:
            return
        try:
            bucket.remove(proc)
        except ValueError:
            pass
        if not bucket:
            del _job_active_subprocesses[job_id]


def _popen_group_spawn_kwargs() -> dict[str, bool]:
    if sys.platform == "win32":
        return {}
    return {"start_new_session": True}


def _terminate_report_subprocess_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _run_logged_subprocess_interruptible(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    log_dir: Path,
    generation_job_id: str | None,
    should_cancel: Callable[[], bool] | None,
    timeout_sec: float | None,
) -> tuple[int, str, str]:
    """Запуск с stdout/stderr в файлы (нет риска переполнения PIPE) и поддержкой немедленной отмены."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stem = log_dir / f"_subexec_{uuid.uuid4().hex}"
    out_path = stem.with_suffix(".stdout.log")
    err_path = stem.with_suffix(".stderr.log")

    proc: subprocess.Popen | None = None
    try:
        with open(out_path, "wb") as stdout_f, open(err_path, "wb") as stderr_f:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=env,
                stdout=stdout_f,
                stderr=stderr_f,
                **_popen_group_spawn_kwargs(),
            )
        deadline = None if timeout_sec is None else time.monotonic() + float(timeout_sec)
        poll = 0.2
        _register_job_subprocess(generation_job_id, proc)
        assert proc is not None
        while proc.poll() is None:
            if deadline is not None and time.monotonic() > deadline:
                _terminate_report_subprocess_tree(proc)
                try:
                    proc.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    pass
                err_tail = ""
                try:
                    err_tail = err_path.read_text(encoding="utf-8", errors="replace")[-1200:]
                except OSError:
                    pass
                raise ReportGenerationError(
                    f"Процесс не завершился за {int(timeout_sec) if timeout_sec else 0} с. "
                    f"{(' ' + err_tail.strip())[:1200]}".strip()
                ) from None
            if should_cancel is not None and should_cancel():
                _terminate_report_subprocess_tree(proc)
            time.sleep(poll)
        proc.wait(timeout=10)
        rc = int(proc.returncode if proc.returncode is not None else -1)

        stdout_text = ""
        stderr_text = ""
        try:
            stdout_text = out_path.read_text(encoding="utf-8", errors="replace")
            stderr_text = err_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

        if should_cancel is not None and should_cancel():
            raise ReportGenerationCancelled()

        return rc, stdout_text, stderr_text
    finally:
        if proc is not None:
            _unregister_job_subprocess(generation_job_id, proc)
        for pth in (out_path, err_path):
            try:
                pth.unlink(missing_ok=True)
            except OSError:
                pass


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _report1_isolated_11_enabled() -> bool:
    """
    Изолированный режим 1.1 МУ: снимок PDF/XLSX сразу после скрипта 1_main_ammonium,
    восстановление листов собственного расчёта до/после graft. Отключить: SVODKI_REPORT1_ISOLATED_11=0.
    """
    raw = (os.environ.get("SVODKI_REPORT1_ISOLATED_11") or "").strip().lower()
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return True


def _report1_script_snapshot_paths(snap: Path, master_id: str) -> dict[str, Path]:
    return {
        "script_xlsx": snap / f"{master_id}_script.xlsx",
        "pristine": snap / f"{master_id}_pristine.xlsx",
        "initial_pdf": snap / f"{master_id}_initial.pdf",
        "final_xlsx": snap / f"{master_id}.xlsx",
    }


def _report1_initial_xlsx_for_restore(snap: Path, master_id: str) -> Path:
    paths = _report1_script_snapshot_paths(snap, master_id)
    for key in ("pristine", "script_xlsx"):
        candidate = paths[key]
        if candidate.is_file():
            return candidate
    return paths["final_xlsx"]


def _snapshot_report1_script_reference(
    *,
    temp_root: Path,
    master_id: str,
    xlsx_path: Path | None = None,
    pdf_path: Path | None = None,
    logs: list[str],
    title: str,
) -> None:
    """Эталон 1.1 МУ до Excel/PDF postprocess (graft, regenerate, slave fill)."""
    snap = temp_root / "_xlsx_snap"
    snap.mkdir(parents=True, exist_ok=True)
    paths = _report1_script_snapshot_paths(snap, master_id)
    saved: list[str] = []
    try:
        if xlsx_path is not None and xlsx_path.is_file():
            if not paths["script_xlsx"].is_file():
                shutil.copy2(xlsx_path, paths["script_xlsx"])
                saved.append("script.xlsx")
            if not paths["pristine"].is_file():
                shutil.copy2(xlsx_path, paths["pristine"])
                saved.append("pristine.xlsx")
        if pdf_path is not None and pdf_path.is_file() and not paths["initial_pdf"].is_file():
            shutil.copy2(pdf_path, paths["initial_pdf"])
            saved.append("initial.pdf")
        if saved:
            logs.append(
                f"[{title}] Снимок 1.1 МУ ({', '.join(saved)}) — эталон до postprocess."
            )
    except OSError as exc:
        logs.append(f"[{title}] Предупреждение: снимок 1.1 МУ не сохранён — {exc}")


def _elapsed_seconds(start: float) -> str:
    return f"{time.monotonic() - start:.1f} с"


def _inject_cropwise_http_env(env: dict[str, str]) -> None:
    """Единые лимиты HTTP к Cropwise для prefetch и subprocess-скриптов."""
    env["SVODKI_PREFETCH_CONCURRENCY"] = str(settings.svodki_prefetch_concurrency)
    env["SVODKI_PREFETCH_MTFMI_CONCURRENCY"] = str(settings.svodki_prefetch_mtfmi_concurrency)
    env["SVODKI_PREFETCH_HTTP_RETRIES"] = str(settings.svodki_prefetch_http_retries)
    r22 = str(settings.svodki_report22_http_concurrency)
    env["SVODKI_REPORT22_HTTP_CONCURRENCY"] = r22
    env.setdefault("SVODKI_REPORT2_HTTP_CONCURRENCY", r22)
    env.setdefault("SVODKI_MTFMI_HTTP_CHUNK", "300")
    env.setdefault("SVODKI_SKIP_DONOR_XLSX_RECALC", "1")
    if settings.svodki_task_store_enabled:
        store_dir = (settings.svodki_task_store_dir or "").strip() or str(
            (_upload_dir_path() / ".task_store").resolve()
        )
        env.setdefault("SVODKI_TASK_STORE_DIR", store_dir)


def _inject_namolot_env(env: dict[str, str]) -> None:
    """Прокинуть Namolot (1С) в subprocess — иначе пустые env из compose затирают defaults."""
    base, user, pwd = get_namolot_config()
    env["NAMOLOT_BASE_URL"] = base
    env["NAMOLOT_USERNAME"] = user
    env["NAMOLOT_PASSWORD"] = pwd
    upload_path = _upload_dir_path()
    env.setdefault("UPLOAD_DIR", str(upload_path))
    cache = upload_path / ".namolot_cache" / "records.json"
    env["NAMOLOT_CACHE_FILE"] = str(cache)
    if cache.is_file():
        env["NAMOLOT_PREFER_CACHE"] = "1"


def _ensure_namolot_available(env: dict[str, str]) -> None:
    """Не генерировать сводки 10/13 с нулями молча — нужен кэш или доступный API."""
    cache = Path((env.get("NAMOLOT_CACHE_FILE") or "").strip())
    if cache.is_file():
        return
    import base64
    from urllib.parse import urlencode

    import requests

    base = (env.get("NAMOLOT_BASE_URL") or "").strip()
    user = (env.get("NAMOLOT_USERNAME") or "").strip()
    pwd = (env.get("NAMOLOT_PASSWORD") or "").strip()
    if not base:
        raise ReportGenerationError(
            "Namolot: не задан NAMOLOT_BASE_URL. Проверьте .env на сервере."
        )
    url = base + "?" + urlencode({"date_begin": "2026-01-01", "date_end": "2026-01-01"})
    headers = {}
    if user and pwd:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            return
    except Exception:
        pass
    raise ReportGenerationError(
        "Namolot (1С) недоступен с сервера и кэш не найден: "
        f"{cache}. С вашего ПК выполните: bash deploy/sync_namolot_cache_to_server.sh "
        "(REMOTE_BASES='/home/razrab1/v3 disk') — затем перегенерируйте сводку."
    )


def _max_concurrent_report_workers() -> int:
    """Сколько сводок запускать параллельно: явный лимит или IO-friendly auto."""
    try:
        configured = int(settings.svodki_generation_max_workers)
    except (TypeError, ValueError):
        configured = 0
    if configured > 0:
        return max(1, configured)
    n = os.cpu_count() or 4
    try:
        auto_cap = int(settings.svodki_generation_auto_max_workers)
    except (TypeError, ValueError):
        auto_cap = 8
    # Старый auto давал 1 воркер на 2-core VPS. Эти отчёты в основном ждут API/IO,
    # но слишком высокий параллелизм перегружает Cropwise (429).
    return max(4, min(max(1, auto_cap), max(4, n * 4)))


def generate_server_reports(
    summary_date: date,
    db: Session,
    report_ids: list[str] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    *,
    report_23_fertilizer_transport_machines: int | None = None,
    r23_document_rebuild_only: bool = False,
    report25_mode: str | None = None,
    report22_cutoff: str | None = None,
    report22_time_from: str | None = None,
    report22_date_from: date | None = None,
    report22_date_to: date | None = None,
    report5_cutoff: str | None = None,
    report5_time_from: str | None = None,
    report2_cutoff: str | None = None,
    report2_time_from: str | None = None,
    report2_date_from: date | None = None,
    report2_date_to: date | None = None,
    report27_cutoff: str | None = None,
    report27_time_from: str | None = None,
    report27_date_from: date | None = None,
    report27_date_to: date | None = None,
    report_harvest_gross: dict | None = None,
    generation_job_id: str | None = None,
    replace_visible_summaries: bool = False,
) -> tuple[list[Summary], list[dict[str, str]], list[str], bool]:
    """
    Формирует сводки так же, как десктоп-приложение:
    - запускает те же python-скрипты;
    - сохраняет xlsx с названием из меню;
    - конвертирует xlsx в pdf;
    - сохраняет pdf в хранилище и БД.

    Параллельность: все сводки из списка запускаются в одном пуле потоков (лимит —
    SVODKI_GENERATION_MAX_WORKERS или ~⅔ от числа CPU). Отмена: при наличии
    generation_job_id процессы прерываются по SIGKILL (группа), плюс флаг через
    should_cancel каждые ~0,2 с; ещё не начавшиеся задачи после отмены снимаются
    с очереди.

    После пачки: для сводок 1 / 1.18 показатели отчётов 3,4,6,7,8 по умолчанию
    копируются в их XLSX (листы «Выраб. — …») и PDF пересобирается; подстановка
    PDF-страниц для этих отчётов не выполняется. Отчёт 5 (стр. 28) — по-прежнему
    через PDF. Отключить Excel-этап: SVODKI_REPORT1_XLSX_SLAVE_FILL=0.
    См. также pymupdf-склейку: pdf_graft_merge_last_col.py.

    Важный нюанс скриптов: они формируют «Сводка за ...» как предыдущий день относительно REPORT_DATE.
    Поэтому для даты платформы YYYY-MM-DD передаём REPORT_DATE = та же дата.

    Если передан should_cancel / generation_job_id, пользователь может прервать ответ
    мгновенно (останов процессов), четвёртый элемент кортежа при необходимости — True (остановка по отмене).
    """
    report27_date_from = _coerce_optional_date(report27_date_from)
    report27_date_to = _coerce_optional_date(report27_date_to)
    _generation_run_lock.acquire()
    try:
        return _generate_server_reports_locked(
            summary_date=summary_date,
            db=db,
            report_ids=report_ids,
            should_cancel=should_cancel,
            report_23_fertilizer_transport_machines=report_23_fertilizer_transport_machines,
            r23_document_rebuild_only=r23_document_rebuild_only,
            report25_mode=report25_mode,
            report22_cutoff=report22_cutoff,
            report22_time_from=report22_time_from,
            report22_date_from=report22_date_from,
            report22_date_to=report22_date_to,
            report5_cutoff=report5_cutoff,
            report5_time_from=report5_time_from,
            report2_cutoff=report2_cutoff,
            report2_time_from=report2_time_from,
            report2_date_from=report2_date_from,
            report2_date_to=report2_date_to,
            report27_cutoff=report27_cutoff,
            report27_time_from=report27_time_from,
            report27_date_from=report27_date_from,
            report27_date_to=report27_date_to,
            report_harvest_gross=report_harvest_gross,
            generation_job_id=generation_job_id,
            replace_visible_summaries=replace_visible_summaries,
        )
    finally:
        _generation_run_lock.release()


def _generate_server_reports_locked(
    summary_date: date,
    db: Session,
    report_ids: list[str] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    *,
    report_23_fertilizer_transport_machines: int | None = None,
    r23_document_rebuild_only: bool = False,
    report25_mode: str | None = None,
    report22_cutoff: str | None = None,
    report22_time_from: str | None = None,
    report22_date_from: date | None = None,
    report22_date_to: date | None = None,
    report5_cutoff: str | None = None,
    report5_time_from: str | None = None,
    report2_cutoff: str | None = None,
    report2_time_from: str | None = None,
    report2_date_from: date | None = None,
    report2_date_to: date | None = None,
    report27_cutoff: str | None = None,
    report27_time_from: str | None = None,
    report27_date_from: date | None = None,
    report27_date_to: date | None = None,
    report_harvest_gross: dict | None = None,
    generation_job_id: str | None = None,
    replace_visible_summaries: bool = False,
) -> tuple[list[Summary], list[dict[str, str]], list[str], bool]:
    total_started = time.monotonic()
    svodki_root = _resolve_svodki_root()
    primary_script_dir, plan_script_dir = _resolve_scripts_dirs(svodki_root)
    logo_root = svodki_root
    request_report_date = summary_date
    created: list[Summary] = []
    failed: list[dict[str, str]] = []
    user_ordered_ids = _maybe_add_report27_1(_prepare_ordered_report_ids(report_ids))
    requested_report_ids = frozenset(user_ordered_ids)
    r23_fast_doc_rebuild = bool(r23_document_rebuild_only) and user_ordered_ids == ["23"]
    if r23_fast_doc_rebuild:
        ordered_ids, r23_auto_report2 = user_ordered_ids, False
    else:
        ordered_ids, r23_auto_report2 = _maybe_add_report2_for_r23(
            user_ordered_ids, request_report_date
        )
        ordered_ids = _maybe_add_report27_1(ordered_ids)
    report_22_only = _is_report_22_only_run(ordered_ids)
    selected_base_configs = _configs_for_ordered_ids(ordered_ids)
    logs: list[str] = [
        f"Дата платформы: {summary_date.isoformat()}",
        f"REPORT_DATE для скриптов: {request_report_date.isoformat()}",
        f"Каталог сводок: {svodki_root}",
        f"Каталог скриптов (основной, plan): {primary_script_dir}",
        f"Порядок генерации: {', '.join(ordered_ids)}",
    ]
    if _report1_standalone_enabled() and (set(ordered_ids) & {"1", "1.18"}):
        logs.append(
            "Сводка 1 standalone: листы уборки (10/11) считаются внутри скрипта сводки 1, "
            "отдельные PDF 10/11 не добавляются автоматически."
        )
    if r23_auto_report2:
        logs.append(
            "Сводка 23: файл снимка сводки 2 не найден на диске (PDF мог быть сформирован "
            "раньше без export). Записываем снимок из данных сводки 2 — без нового PDF."
        )
    if plan_script_dir is not None:
        logs.append(f"Каталог скриптов (fallback, new): {plan_script_dir}")

    upload_root = Path(settings.upload_dir)
    upload_root.mkdir(parents=True, exist_ok=True)
    snapshot_root = upload_root / "_api_snapshots"
    snapshot_root.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=f"svodki_{summary_date.isoformat()}_", dir=upload_root))
    use_api_cache = _env_flag(USE_API_CACHE_ENV_VAR, False)
    api_snapshot_path: Path | None = None
    if use_api_cache:
        api_snapshot_path = snapshot_root / (
            f"cropwise_snapshot_{summary_date.isoformat()}_{uuid.uuid4().hex}.json"
        )
        logs.append(f"JSON-снимок Cropwise API (кэш GET): {api_snapshot_path}")
    else:
        logs.append(
            "Cropwise API: каждая сводка загружает данные самостоятельно "
            "(без svodki_prefetch и без общего JSON-снимка)."
        )
    prefetch_data_path: Path | None = None
    if _env_flag(USE_PREFETCH_DATA_ENV_VAR, False):
        prefetch_data_path = snapshot_root / (
            f"cropwise_prefetch_{summary_date.isoformat()}_{uuid.uuid4().hex}.json"
        )
    was_cancelled = False
    try:
        ordered_resolved: list[ResolvedReportConfig | None] = []
        for base_cfg in selected_base_configs:
            if should_cancel is not None and should_cancel():
                logs.append("Генерация остановлена по запросу пользователя (при подготовке конфигов).")
                return created, failed, logs, True
            try:
                cfg = _resolve_report_config(primary_script_dir, plan_script_dir, base_cfg)
                ordered_resolved.append(cfg)
            except Exception as exc:
                failed.append(
                    {
                        "report_id": base_cfg.id,
                        "title": base_cfg.title,
                        "script_file": base_cfg.script,
                        "error": str(exc) or exc.__class__.__name__,
                    }
                )
                logs.append(f"[{base_cfg.title}] Ошибка подготовки: {exc}")
                ordered_resolved.append(None)

        cfg_by_id: dict[str, ResolvedReportConfig] = {
            c.id: c for c in ordered_resolved if c is not None
        }
        ordered_cfgs = [cfg_by_id[i] for i in ordered_ids if i in cfg_by_id]
        report1_fast_slave_ids = _report1_fast_slave_ids(ordered_ids)
        report1_required_donor_ids = _report1_required_donor_ids(ordered_ids)
        report1_skip_harvest_fetch = _report1_skip_harvest_fetch(ordered_ids)
        report1_fast_groups = [
            REPORT1_FAST_SLAVE_GROUPS_BY_ID[rid]
            for rid in sorted(report1_fast_slave_ids)
        ]
        if "6" in report1_fast_slave_ids:
            report1_fast_groups.append("3.2 Опрыскивание (овощи)")
        standalone_hybrid_planned = _report1_standalone_harvest_hybrid(ordered_ids) and report1_skip_harvest_fetch
        if _report1_standalone_enabled() and ("1" in ordered_ids or "1.18" in ordered_ids):
            if standalone_hybrid_planned:
                logs.append(
                    "Сводка 1 standalone hybrid: perf-листы из API; уборка — из доноров 10/11 "
                    "(без третьего harvest API в сводке 1)."
                )
            else:
                logs.append(
                    "Сводка 1 standalone: все листы из API, без доноров 3–8 и без post-process graft."
                )
        elif report1_fast_groups:
            logs.append(
                "Сводка 1 fast-path: пропуск расчёта листов, которые заполняются донорами: "
                + ", ".join(report1_fast_groups)
            )
            if report1_fast_slave_ids and ("1" in ordered_ids or "1.18" in ordered_ids):
                logs.append(
                    "Сводка 1 fast-path: генерация 1 / 1.18 — отдельным этапом после доноров "
                    f"({', '.join(sorted(report1_required_donor_ids))})"
                )
        if report1_skip_harvest_fetch:
            logs.append(
                "Сводка 1 harvest fast-path: «Сводка овощи»/намолот из доноров 11/10 "
                "(без повторного API внутри сводки 1)."
            )
        worker_cap = _max_concurrent_report_workers()
        logs.append(f"Параллельных сводок не более: {worker_cap}")

        if should_cancel is not None and should_cancel():
            logs.append("Генерация остановлена по запросу пользователя (до запуска сводок).")
            return created, failed, logs, True

        prefetch_data_enabled = _env_flag(USE_PREFETCH_DATA_ENV_VAR, False)
        prefetch_needed = any(_uses_unified_prefetch_data(cfg) for cfg in ordered_cfgs)
        prefetch_data_ready = False
        if prefetch_data_enabled and prefetch_needed and not r23_fast_doc_rebuild and not report_22_only:
            prefetch_started = time.monotonic()
            try:
                pf_logs = _run_api_data_prefetch(
                    primary_script_dir=primary_script_dir,
                    plan_script_dir=plan_script_dir,
                    svodki_root=svodki_root,
                    output_path=prefetch_data_path,
                    report_date=request_report_date,
                    report_ids=ordered_ids,
                    report1_fast_slave_ids=report1_fast_slave_ids,
                    generation_job_id=generation_job_id,
                    should_cancel=should_cancel,
                )
            except ReportGenerationCancelled:
                logs.append("Генерация остановлена по запросу пользователя (сбор prefetch JSON).")
                return created, failed, logs, True
            logs.extend(pf_logs)
            logs.append(f"Время этапа prefetch JSON: {_elapsed_seconds(prefetch_started)}")
            prefetch_data_ready = True
        elif report_22_only:
            logs.append(
                "Сводка 22: prefetch JSON пропущен — скрипт загружает данные за смену "
                "напрямую из Cropwise (как при локальном запуске)."
            )
        elif r23_fast_doc_rebuild:
            logs.append(
                "Пропуск единого prefetch JSON: сводка 23 только пересборка документа "
                "(ручной текст + простои)."
            )
        elif prefetch_data_enabled and not prefetch_needed:
            logs.append("Пропуск единого prefetch JSON: выбранные сводки его не используют.")
        else:
            logs.append("Единый prefetch JSON отключён: используется только runtime cache.")

        prefetch_enabled = _env_flag(PREFETCH_BEFORE_REPORTS_ENV_VAR, False)
        if prefetch_enabled:
            prefetch_failed, prefetch_logs, was_cancelled = _prefetch_reports_parallel(
                ordered_cfgs,
                request_report_date=request_report_date,
                temp_root=temp_root,
                api_snapshot_path=api_snapshot_path,
                plan_script_dir=plan_script_dir,
                max_workers=worker_cap,
                should_cancel=should_cancel,
                generation_job_id=generation_job_id,
            )
            logs.extend(prefetch_logs)
            failed.extend(prefetch_failed)
            if was_cancelled:
                return created, failed, logs, True
            if prefetch_failed:
                logs.append("Генерация Excel/PDF не запущена: сначала нужно успешно собрать JSON-снимок API.")
                return created, failed, logs, False
        else:
            logs.append("Prefetch API отключён: JSON-снимок заполняется во время одного прохода генерации.")

        if "23" in ordered_ids:
            donor_bundle = r23_work_data_from_report2_bundle_path(request_report_date)
            if _r23_bundle_matches_platform_date(donor_bundle, request_report_date):
                logs.append(
                    "Сводка 23: блок по работам из уже сформированной сводки 2 "
                    f"({donor_bundle.name})"
                )
            elif "2" in ordered_ids:
                logs.append(
                    "Сводка 23: снимок сводки 2 будет записан после завершения сводки 2 "
                    f"на {request_report_date.isoformat()}"
                )
            else:
                logs.append(
                    "Сводка 23: снимок сводки 2 не найден — сначала сформируйте сводку 2 "
                    f"на {request_report_date.isoformat()}"
                )

        reports_started = time.monotonic()
        batch_created, batch_failed, p_logs, was_cancelled = _execute_reports_parallel(
            ordered_cfgs,
            summary_date=summary_date,
            request_report_date=request_report_date,
            logo_root=logo_root,
            temp_root=temp_root,
            api_snapshot_path=api_snapshot_path,
            prefetch_data_path=prefetch_data_path if prefetch_data_ready else None,
            api_snapshot_read_only=prefetch_enabled,
            plan_script_dir=plan_script_dir,
            max_workers=worker_cap,
            should_cancel=should_cancel,
            r23_auto_report2=r23_auto_report2,
            report_23_fertilizer_transport_machines=report_23_fertilizer_transport_machines,
            r23_document_rebuild_only=r23_document_rebuild_only,
            report25_mode=report25_mode,
            report22_cutoff=report22_cutoff,
            report22_time_from=report22_time_from,
            report22_date_from=report22_date_from,
            report22_date_to=report22_date_to,
            report5_cutoff=report5_cutoff,
            report5_time_from=report5_time_from,
            report2_cutoff=report2_cutoff,
            report2_time_from=report2_time_from,
            report2_date_from=report2_date_from,
            report2_date_to=report2_date_to,
            report27_cutoff=report27_cutoff,
            report27_time_from=report27_time_from,
            report27_date_from=report27_date_from,
            report27_date_to=report27_date_to,
            report_harvest_gross=report_harvest_gross,
            generation_job_id=generation_job_id,
            report1_fast_groups=report1_fast_groups,
            report1_fast_slave_ids=report1_required_donor_ids,
            report1_skip_harvest_fetch=report1_skip_harvest_fetch,
            replace_visible_summaries=replace_visible_summaries,
            requested_report_ids=requested_report_ids,
        )
        created.extend(batch_created)
        failed.extend(batch_failed)
        logs.extend(p_logs)
        if report1_required_donor_ids:
            created_ids = {_report_id_from_summary_title(item.title) for item in created}
            missing_donors = sorted(report1_required_donor_ids - created_ids)
            if missing_donors:
                logs.append(
                    "Сводка 1 fast-path: доноры не сформированы, сводки 1/1.18 не будут опубликованы: "
                    + ", ".join(missing_donors)
                )
                kept: list[Summary] = []
                for item in created:
                    rid = _report_id_from_summary_title(item.title)
                    if rid in {"1", "1.18"}:
                        failed.append(
                            {
                                "report_id": rid,
                                "title": item.title,
                                "script_file": "1_main_ammonium__1_.py",
                                "error": "Fast-path сводки 1 требует доноры: " + ", ".join(missing_donors),
                            }
                        )
                    else:
                        kept.append(item)
                created = kept
        logs.append(f"Время этапа скрипты+PDF+БД: {_elapsed_seconds(reports_started)}")
        has_report1 = any(
            _report_id_from_summary_title(item.title) in {"1", "1.18"} for item in created
        )
        standalone_hybrid = (
            has_report1
            and _report1_standalone_harvest_hybrid(ordered_ids)
            and report1_skip_harvest_fetch
        )
        if has_report1 and _report1_standalone_enabled() and not standalone_hybrid:
            logs.append(
                "Сводка 1 standalone: XLSX slave fill, finalize, graft и restore пропущены — "
                "используется PDF из скрипта."
            )
        elif standalone_hybrid:
            logs.append(
                "Сводка 1 standalone hybrid: подстановка листов уборки из Excel/PDF доноров 10/11."
            )
            postprocess_started = time.monotonic()
            skip_pdf_slaves = _apply_report1_xlsx_slave_postprocess(
                temp_root=temp_root,
                created=created,
                logo_root=logo_root,
                logs=logs,
                replace_visible_summaries=replace_visible_summaries,
                requested_report_ids=requested_report_ids,
                graft_spec=REPORT1_HARVEST_ONLY_GRAFT_SPEC,
            )
            logs.append(
                f"Время этапа XLSX-подстановка уборки в сводку 1: {_elapsed_seconds(postprocess_started)}"
            )
            finalize_started = time.monotonic()
            _finalize_report1_pdf_page_order(
                created=created,
                temp_root=temp_root,
                logs=logs,
                logo_root=logo_root,
                convert_fn=_convert_xlsx_to_pdf,
            )
            logs.append(f"Время этапа порядка страниц сводки 1: {_elapsed_seconds(finalize_started)}")
            graft_started = time.monotonic()
            _apply_batch_pdf_grafts(
                created,
                logs,
                db=db,
                summary_date=summary_date,
                report1_skip_pdf_slaves=skip_pdf_slaves,
                temp_root=temp_root,
                replace_visible_summaries=replace_visible_summaries,
                requested_report_ids=requested_report_ids,
                report1_graft_spec=REPORT1_HARVEST_ONLY_GRAFT_SPEC,
            )
            logs.append(f"Время этапа PDF-подстановки уборки: {_elapsed_seconds(graft_started)}")
            _restamp_report1_pdf_headers(
                created=created,
                temp_root=temp_root,
                logs=logs,
                logo_root=logo_root,
            )
        elif has_report1:
            logs.append(
                "Сводка 1: снимок PDF/XLSX после скрипта и восстановление листов 1.1 МУ "
                "после finalize/graft/посева (эталон — 1_main_ammonium__1_.py)."
            )
            postprocess_started = time.monotonic()
            skip_pdf_slaves = _apply_report1_xlsx_slave_postprocess(
                temp_root=temp_root,
                created=created,
                logo_root=logo_root,
                logs=logs,
                replace_visible_summaries=replace_visible_summaries,
                requested_report_ids=requested_report_ids,
            )
            logs.append(f"Время этапа XLSX-подстановка в сводку 1: {_elapsed_seconds(postprocess_started)}")
            finalize_started = time.monotonic()
            _finalize_report1_pdf_page_order(
                created=created,
                temp_root=temp_root,
                logs=logs,
                logo_root=logo_root,
                convert_fn=_convert_xlsx_to_pdf,
            )
            logs.append(f"Время этапа порядка страниц сводки 1: {_elapsed_seconds(finalize_started)}")
            pre_graft_restore_started = time.monotonic()
            if _restore_report1_own_calc_pdf_pages_batch(
                created=created,
                temp_root=temp_root,
                logs=logs,
                cleanup=False,
                stage="после finalize, перед PDF-подстановкой",
            ):
                _restamp_report1_pdf_headers(
                    created=created,
                    temp_root=temp_root,
                    logs=logs,
                    logo_root=logo_root,
                )
            logs.append(
                "Время этапа восстановления PDF листов МУ после finalize: "
                f"{_elapsed_seconds(pre_graft_restore_started)}"
            )
            graft_started = time.monotonic()
            _apply_batch_pdf_grafts(
                created,
                logs,
                db=db,
                summary_date=summary_date,
                report1_skip_pdf_slaves=skip_pdf_slaves,
                temp_root=temp_root,
                replace_visible_summaries=replace_visible_summaries,
                requested_report_ids=requested_report_ids,
            )
            logs.append(f"Время этапа PDF-подстановки: {_elapsed_seconds(graft_started)}")
            restamp_started = time.monotonic()
            _restamp_report1_pdf_headers(
                created=created,
                temp_root=temp_root,
                logs=logs,
                logo_root=logo_root,
            )
            logs.append(f"Время этапа штампа шапок сводки 1: {_elapsed_seconds(restamp_started)}")
            restore_started = time.monotonic()
            if _restore_report1_own_calc_pdf_pages_batch(
                created=created,
                temp_root=temp_root,
                logs=logs,
                cleanup=False,
                stage="после graft/restamp",
            ):
                _restamp_report1_pdf_headers(
                    created=created,
                    temp_root=temp_root,
                    logs=logs,
                    logo_root=logo_root,
                )
            logs.append(
                f"Время этапа восстановления PDF листов МУ после graft: {_elapsed_seconds(restore_started)}"
            )
            seeding_started = time.monotonic()
            _ensure_report1_seeding_pages_batch(
                created=created,
                temp_root=temp_root,
                logs=logs,
                logo_root=logo_root,
                convert_fn=_convert_xlsx_to_pdf,
            )
            logs.append(
                f"Время этапа восстановления страниц посева сводки 1: {_elapsed_seconds(seeding_started)}"
            )
            post_seed_restore_started = time.monotonic()
            if _restore_report1_own_calc_pdf_pages_batch(
                created=created,
                temp_root=temp_root,
                logs=logs,
                cleanup=True,
                stage="финал после посева",
            ):
                _restamp_report1_pdf_headers(
                    created=created,
                    temp_root=temp_root,
                    logs=logs,
                    logo_root=logo_root,
                )
            logs.append(
                "Время этапа финального восстановления PDF листов МУ: "
                f"{_elapsed_seconds(post_seed_restore_started)}"
            )
        # Пользовательский порядок/подстановки страниц и правки ячеек — для любой пачки
        # (в т.ч. standalone-сводки 1 и генерации без сводки 1). Раньше вызывались только
        # в non-standalone ветке → после включения SVODKI_REPORT1_STANDALONE подстановки
        # перестали применяться при следующей генерации.
        if not was_cancelled and created:
            custom_order_started = time.monotonic()
            _apply_report1_saved_page_order(
                created=created,
                logs=logs,
                db=db,
                summary_date=summary_date,
            )
            logs.append(
                f"Время этапа пользовательского порядка страниц: "
                f"{_elapsed_seconds(custom_order_started)}"
            )
            cell_edits_started = time.monotonic()
            _apply_saved_cell_edits_after_generation(created=created, logs=logs)
            logs.append(
                f"Время этапа пользовательских правок ячеек: "
                f"{_elapsed_seconds(cell_edits_started)}"
            )
            try:
                from app.application.summary_comments_hooks import apply_comments_after_generation

                apply_comments_after_generation(created=created, logs=logs)
            except Exception as exc:
                logs.append(f"Комментарии после генерации — {exc}")
        if not was_cancelled and created:
            publish_started = time.monotonic()
            _publish_summaries(db, created)
            if replace_visible_summaries:
                logs.append(
                    "Пачка генерации завершена: сводки опубликованы (доступны в списке и для скачивания)."
                )
            else:
                logs.append(
                    "Пачка генерации завершена: сводки опубликованы. "
                    "Уже существовавшие опубликованные сводки не перезаписывались."
                )
            logs.append(f"Время этапа публикации: {_elapsed_seconds(publish_started)}")
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
        try:
            from .disk_retention import prune_api_snapshots_after_generation

            snap_stats = prune_api_snapshots_after_generation(summary_date)
            deleted = int(snap_stats.get("manifests_deleted") or 0)
            if deleted:
                logs.append(
                    f"Очистка JSON-снимков Cropwise за {summary_date.isoformat()}: "
                    f"удалено комплектов {deleted}."
                )
        except Exception:
            logger.warning(
                "Failed to prune Cropwise API snapshots after generation: date=%s",
                summary_date,
                exc_info=True,
            )
    logs.append(f"Общее время генерации пачки: {_elapsed_seconds(total_started)}")
    return created, failed, logs, was_cancelled


def _resolve_svodki_root() -> Path:
    configured = (settings.svodki_root or "").strip()
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute():
            root = (Path(__file__).resolve().parents[2] / root).resolve()
    else:
        root = (Path(__file__).resolve().parents[2] / "svodki").resolve()
    if not root.is_dir():
        raise ReportGenerationError(f"Не найден каталог со сводками: {root}")
    return root


def _resolve_scripts_dirs(svodki_root: Path) -> tuple[Path, Path | None]:
    plan_dir = svodki_root / "plan"
    if not plan_dir.is_dir():
        raise ReportGenerationError(f"Не найден каталог со скриптами: {plan_dir}")
    return plan_dir, None


def _resolve_svodki_support_file(root: Path, filename: str) -> Path | None:
    plan_dir = root / "plan"
    if not plan_dir.is_dir():
        return None
    try:
        import importlib.util

        paths_mod_path = plan_dir / "plan_paths.py"
        if paths_mod_path.is_file():
            spec = importlib.util.spec_from_file_location("_svodki_plan_paths", paths_mod_path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                found = mod.find_support_file(filename)
                if found is not None:
                    return found
    except Exception:
        pass
    path = plan_dir / filename
    return path if path.is_file() else None


def _resolve_report_config(primary_dir: Path, fallback_dir: Path | None, cfg: ReportConfig) -> ResolvedReportConfig:
    preferred_dir = primary_dir
    try:
        script_name = _resolve_script_name(preferred_dir, cfg.id, cfg.script)
    except ReportGenerationError:
        if fallback_dir is None:
            raise
        script_name = _resolve_script_name(fallback_dir, cfg.id, cfg.script)
        preferred_dir = fallback_dir
    return ResolvedReportConfig(
        id=cfg.id,
        script=script_name,
        title=cfg.title,
        script_dir=preferred_dir,
    )


def _reorder_generation_ids(ids: list[str]) -> list[str]:
    """Порядок: (2, 23) → доноры / уборка → 1, 1.18 → остальные."""
    s = set(ids)
    w1 = [x for x in ("2", "23") if x in s]
    if _report1_standalone_enabled() and (s & {"1", "1.18"}):
        w_harvest = [x for x in ("10", "11") if x in s]
        w2 = [x for x in ("1", "1.18") if x in s]
        used = set(w1) | set(w_harvest) | set(w2)
        tail = [x for x in ids if x not in used]
        return w1 + w_harvest + w2 + tail
    w_slave = [x for x in ("3", "4", "5", "6", "7", "8") if x in s]
    w2 = [x for x in ("1", "1.18") if x in s]
    used = set(w1) | set(w_slave) | set(w2)
    tail = [x for x in ids if x not in used]
    return w1 + w_slave + w2 + tail


def _dedicated_heavy_report_lanes_enabled() -> bool:
    env_raw = os.environ.get("SVODKI_DEDICATED_HEAVY_REPORT_LANES")
    if env_raw is not None and str(env_raw).strip():
        return _env_flag("SVODKI_DEDICATED_HEAVY_REPORT_LANES", True)
    return bool(settings.svodki_dedicated_heavy_report_lanes)


def _cfgs_standalone_harvest_hybrid(cfgs: list[ResolvedReportConfig]) -> bool:
    if not _report1_standalone_enabled():
        return False
    ids = {_cfg_id_norm(c) for c in cfgs}
    return bool({"1", "1.18"} & ids) and REPORT1_HARVEST_DONOR_IDS <= ids


def _split_report_execution_lanes(
    cfgs: list[ResolvedReportConfig],
    *,
    report1_fast_slave_ids: frozenset[str],
) -> tuple[list[ResolvedReportConfig], list[ResolvedReportConfig], list[ResolvedReportConfig], bool]:
    """
    bulk — 0, 0.1, 3–9, 24…; r1/r2 — отдельные дорожки (не делят ThreadPool).
    Сводку 1 выносим только если standalone или нет доноров 3–8 в этой же пачке.
    Standalone + 10/11: сводка 1 в bulk (этап после доноров), без параллельного harvest API.
    """
    r2 = [c for c in cfgs if _cfg_id_norm(c) == "2"]
    r1_all = [c for c in cfgs if _cfg_id_norm(c) in _REPORT1_FAMILY_IDS]
    hybrid = _cfgs_standalone_harvest_hybrid(cfgs)
    isolate_r1 = bool(r1_all) and (
        (_report1_standalone_enabled() and not hybrid) or not report1_fast_slave_ids
    )
    r1 = r1_all if isolate_r1 else []
    bulk = [c for c in cfgs if c not in r2 and c not in r1]
    return bulk, r1, r2, isolate_r1


def _report_execution_waves(
    cfgs: list[ResolvedReportConfig],
    *,
    report1_fast_slave_ids: frozenset[str] | None = None,
) -> list[list[ResolvedReportConfig]]:
    """
    Разбиение пачки на этапы:
    - сводка 2 до 23 (снимок work_data);
    - при fast-path сводки 1 — доноры 3–8 и остальные, затем 1 / 1.18 (меньше RAM и API).
    """
    ids = {_cfg_id_norm(c) for c in cfgs}
    waves: list[list[ResolvedReportConfig]] = []

    if "2" in ids and "23" in ids:
        waves.append([c for c in cfgs if _cfg_id_norm(c) == "2"])
        rest = [c for c in cfgs if _cfg_id_norm(c) != "2"]
    else:
        rest = list(cfgs)

    defer_r1 = bool({"1", "1.18"} & ids) and (
        bool(report1_fast_slave_ids) or _cfgs_standalone_harvest_hybrid(cfgs)
    )
    if defer_r1:
        before_r1 = [c for c in rest if _cfg_id_norm(c) not in {"1", "1.18"}]
        r1_cfgs = [c for c in rest if _cfg_id_norm(c) in {"1", "1.18"}]
        if before_r1:
            waves.append(before_r1)
        if r1_cfgs:
            waves.append(r1_cfgs)
    elif rest:
        waves.append(rest)

    return waves if waves else [cfgs]


def _r23_work_data_bundle_path(temp_root: Path, ordered_ids: list[str]) -> Path | None:
    if "2" in ordered_ids and "23" in ordered_ids:
        return (temp_root / R23_WORK_DATA_BUNDLE_FILENAME).resolve()
    return None


def _r23_bundle_matches_platform_date(bundle_path: Path, platform_date: date) -> bool:
    """Bundle записан сводкой 2 с тем же REPORT_DATE (штампом), что и дата платформы."""
    if not bundle_path.is_file():
        return False
    try:
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    stamp = str(payload.get("report_stamp") or "").strip()
    if stamp:
        return stamp == platform_date.isoformat()
    expected = (platform_date - timedelta(days=1)).isoformat()
    return str(payload.get("report_date") or "").strip() == expected


def _maybe_add_report2_for_r23(
    ordered_ids: list[str],
    platform_date: date,
) -> tuple[list[str], bool]:
    """
    Сводка 23 читает JSON-снимок work_data (report2_YYYY-MM-DD.json).
    Обычно его пишет сводка 2 в утреннем пакете 0–9.
    Если в запросе только 23 и файла нет — тихо пересобираем только снимок.
    """
    if "23" not in ordered_ids or "2" in ordered_ids:
        return ordered_ids, False
    donor = r23_work_data_from_report2_bundle_path(platform_date)
    if donor.is_file() and _r23_bundle_matches_platform_date(donor, platform_date):
        return ordered_ids, False
    return _reorder_generation_ids(["2", *ordered_ids]), True


def _maybe_add_report27_1(ordered_ids: list[str]) -> list[str]:
    """Сводка 27.1 (время остановок) всегда формируется вместе с 27."""
    if "27" not in ordered_ids or "27.1" in ordered_ids:
        return ordered_ids
    out: list[str] = []
    for rid in ordered_ids:
        out.append(rid)
        if rid == "27":
            out.append("27.1")
    return out


def _prepare_ordered_report_ids(report_ids: list[str] | None) -> list[str]:
    """Нормализует список id: алиасы, без дублей. Сводка 23 только если явно в запросе (или «все»)."""
    if report_ids is None:
        raw = [cfg.id for cfg in REPORT_CONFIGS_BASE if cfg.id not in EXPLICIT_ONLY_REPORT_IDS]
        seen = set(raw)
    else:
        raw = []
        seen: set[str] = set()
        for item in report_ids:
            rid = str(item or "").strip()
            if not rid:
                continue
            rid = REPORT_ID_ALIASES.get(rid, rid)
            if rid in seen:
                continue
            raw.append(rid)
            seen.add(rid)
    return _reorder_generation_ids(raw)


def _configs_for_ordered_ids(ids: list[str]) -> list[ReportConfig]:
    by_id = {cfg.id: cfg for cfg in REPORT_CONFIGS_BASE}
    selected: list[ReportConfig] = []
    unknown: list[str] = []
    for rid in ids:
        cfg = by_id.get(rid)
        if cfg is None:
            unknown.append(rid)
        else:
            selected.append(cfg)
    if unknown:
        raise ReportGenerationError(f"Неизвестные report_id: {', '.join(sorted(set(unknown)))}")
    return selected


def _select_report_configs(report_ids: list[str] | None) -> list[ReportConfig]:
    """Совместимость: все отчёты или выбранные, с порядком 2 → 23 → 1 → 1.18 → хвост."""
    return _configs_for_ordered_ids(_prepare_ordered_report_ids(report_ids))


def _execute_resolved_report(
    cfg: ResolvedReportConfig,
    *,
    summary_date: date,
    request_report_date: date,
    logo_root: Path,
    temp_root: Path,
    api_snapshot_path: Path,
    prefetch_data_path: Path | None,
    api_snapshot_read_only: bool,
    plan_script_dir: Path | None,
    report_23_fertilizer_transport_machines: int | None = None,
    r23_document_rebuild_only: bool = False,
    report25_mode: str | None = None,
    report22_cutoff: str | None = None,
    report22_time_from: str | None = None,
    report22_date_from: date | None = None,
    report22_date_to: date | None = None,
    report5_cutoff: str | None = None,
    report5_time_from: str | None = None,
    report2_cutoff: str | None = None,
    report2_time_from: str | None = None,
    report2_date_from: date | None = None,
    report2_date_to: date | None = None,
    report27_cutoff: str | None = None,
    report27_time_from: str | None = None,
    report27_date_from: date | None = None,
    report27_date_to: date | None = None,
    report_harvest_gross: dict | None = None,
    generation_job_id: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
    report1_fast_groups: list[str] | None = None,
    report1_skip_harvest_fetch: bool = False,
    r23_work_data_bundle_path: Path | None = None,
    r23_auto_report2: bool = False,
    replace_visible_summaries: bool = False,
    requested_report_ids: frozenset[str] | None = None,
    db: Session,
    detach_row: bool = False,
) -> tuple[Summary | None, dict[str, str] | None, list[str]]:
    logs: list[str] = []
    report_started = time.monotonic()
    logs.append(f"[{cfg.title}] Старт")
    requested_ids = requested_report_ids or frozenset()
    try:
        cfg_id = _cfg_id_norm(cfg)
        storage_title = _resolve_storage_title(
            cfg,
            summary_date,
            report25_mode=report25_mode,
            report22_cutoff=report22_cutoff,
            report22_time_from=report22_time_from,
            report22_date_from=report22_date_from,
            report22_date_to=report22_date_to,
            report2_cutoff=report2_cutoff,
            report2_time_from=report2_time_from,
            report2_date_from=report2_date_from,
            report2_date_to=report2_date_to,
            report27_cutoff=report27_cutoff,
            report27_time_from=report27_time_from,
            report27_date_from=report27_date_from,
            report27_date_to=report27_date_to,
        )
        existing_published = get_visible_summary_by_title(db, summary_date, storage_title)
        if existing_published is not None and _keep_published_summary(
            cfg_id,
            replace_visible_summaries=replace_visible_summaries,
            requested_report_ids=requested_ids,
        ):
            logs.append(
                f"[{cfg.title}] Уже опубликована (id={existing_published.id}) — "
                "повторная генерация пропущена, файл не изменится."
            )
            if detach_row:
                db.expunge(existing_published)
            return existing_published, None, logs

        report_temp = temp_root / f"{cfg.id}_{uuid.uuid4().hex}"
        report_temp.mkdir(parents=True, exist_ok=True)
        script_started = time.monotonic()
        r25_mode = _normalize_report25_mode(report25_mode) if cfg_id == "25" else None
        r22_cutoff = _normalize_report22_cutoff(report22_cutoff) if cfg_id == "22" else None
        r22_time_from = _normalize_report22_cutoff(report22_time_from) if cfg_id == "22" else None
        r22_date_from = report22_date_from if cfg_id == "22" else None
        r22_date_to = report22_date_to if cfg_id == "22" else None
        r2_cutoff = _normalize_report22_cutoff(report2_cutoff) if cfg_id == "2" else None
        r2_time_from = _normalize_report22_cutoff(report2_time_from) if cfg_id == "2" else None
        r2_date_from = report2_date_from if cfg_id == "2" else None
        r2_date_to = report2_date_to if cfg_id == "2" else None
        r27_cutoff = _normalize_report22_cutoff(report27_cutoff) if _is_report27_family(cfg_id) else None
        r27_time_from = _normalize_report22_cutoff(report27_time_from) if _is_report27_family(cfg_id) else None
        r27_date_from = report27_date_from if _is_report27_family(cfg_id) else None
        r27_date_to = report27_date_to if _is_report27_family(cfg_id) else None
        r5_cutoff = _normalize_report22_cutoff(report5_cutoff) if cfg_id == "5" else None
        r5_time_from = _normalize_report22_cutoff(report5_time_from) if cfg_id == "5" else None
        xlsx_path, stdout = _run_report_script(
            cfg=cfg,
            plan_script_dir=plan_script_dir,
            output_dir=report_temp,
            report_date=request_report_date,
            api_snapshot_path=api_snapshot_path,
            prefetch_data_path=prefetch_data_path,
            api_snapshot_read_only=api_snapshot_read_only,
            report_23_fertilizer_transport_machines=report_23_fertilizer_transport_machines,
            r23_document_rebuild_only=r23_document_rebuild_only,
            report25_mode=r25_mode,
            report22_cutoff=r22_cutoff,
            report22_time_from=r22_time_from,
            report22_date_from=r22_date_from,
            report22_date_to=r22_date_to,
            report5_cutoff=r5_cutoff,
            report5_time_from=r5_time_from,
            report2_cutoff=r2_cutoff,
            report2_time_from=r2_time_from,
            report2_date_from=r2_date_from,
            report2_date_to=r2_date_to,
            report27_cutoff=r27_cutoff,
            report27_time_from=r27_time_from,
            report27_date_from=r27_date_from,
            report27_date_to=r27_date_to,
            report_harvest_gross=report_harvest_gross,
            generation_job_id=generation_job_id,
            should_cancel=should_cancel,
            report1_fast_groups=report1_fast_groups,
            r23_work_data_bundle_path=r23_work_data_bundle_path,
            r23_auto_report2=r23_auto_report2,
            report1_skip_harvest_fetch=report1_skip_harvest_fetch,
        )
        logs.append(f"[{cfg.title}] Время скрипта XLSX: {_elapsed_seconds(script_started)}")
        if cfg_id == "2":
            donor_p = r23_work_data_from_report2_bundle_path(request_report_date)
            if _r23_bundle_matches_platform_date(donor_p, request_report_date):
                logs.append(f"[{cfg.title}] Снимок для сводки 23 сохранён: {donor_p.name}")
            else:
                hint = ""
                for line in reversed(stdout.splitlines()):
                    if "[r23←2]" in line or "r23_from_report2" in line.lower():
                        hint = f" Последняя строка скрипта: {line.strip()}"
                        break
                raise ReportGenerationError(
                    f"Сводка 2 не записала снимок для сводки 23 ({donor_p.name}). "
                    "В логе скрипта должна быть строка [r23←2]. "
                    "Проверьте, что на сервере обновлены svodki/plan/utils/shared/r23_from_report2.py "
                    "и 2_main_ideal_compact.py."
                    + hint
                )
            if r23_auto_report2:
                if stdout.strip():
                    logs.extend(_season_mismatch_log_lines(stdout, cfg.title))
                    logs.extend(
                        f"[{cfg.title}] {line}"
                        for line in stdout.splitlines()[-10:]
                        if line.strip() and "SEASON_MISMATCH" not in line
                    )
                logs.append(
                    f"[{cfg.title}] Автозапуск для сводки 23: PDF сводки 2 не публикуем."
                )
                return None, None, logs
        if _cfg_id_norm(cfg) == "23":
            snap = report_temp / ".r23_work_narrative_snapshot.txt"
            if snap.is_file() and snap.stat().st_size > 0:
                try:
                    dest = r23_work_narrative_prefill_path(summary_date)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(snap, dest)
                except OSError as exc:
                    logs.append(f"[{cfg.title}] не сохранён снимок текста для редактора: {exc}")
        logs.append(f"[{cfg.title}] XLSX: {xlsx_path.name}")
        if stdout.strip():
            logs.extend(_season_mismatch_log_lines(stdout, cfg.title))
            logs.extend(
                f"[{cfg.title}] {line}"
                for line in stdout.splitlines()[-10:]
                if line.strip() and "SEASON_MISMATCH" not in line
            )

        _normalize_excel_sheet_titles_file(xlsx_path, cfg_id, logs)

        if cfg_id in {"1", "1.18"}:
            _reorder_report1_workbook_file(xlsx_path, logs, report_label=cfg_id)
            _snapshot_report1_script_reference(
                temp_root=temp_root,
                master_id=cfg_id,
                xlsx_path=xlsx_path,
                logs=logs,
                title=cfg.title,
            )

        try:
            snap_root = temp_root / "_xlsx_snap"
            snap_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(xlsx_path, snap_root / f"{cfg.id}.xlsx")
        except OSError as exc:
            logs.append(
                f"[{cfg.title}] Предупреждение: не скопирован снимок в _xlsx_snap/{cfg.id}.xlsx: {exc}"
            )

        db_started = time.monotonic()
        publish_immediately = cfg_id not in {"1", "1.18"}
        report2_excel_only = cfg_id == "2" and r2_date_from is not None and r2_date_to is not None
        report27_excel_only = _is_report27_family(cfg_id)
        if report2_excel_only or report27_excel_only:
            if " (Excel)" not in storage_title:
                storage_title = f"{storage_title} (Excel)"
            logs.append(f"[{cfg.title}] Произвольный период: сохраняем только Excel, без PDF.")
            created_item = _store_generated_xlsx(
                db=db,
                xlsx_path=xlsx_path,
                title=storage_title,
                summary_date=summary_date,
                visible=publish_immediately,
                report_id=cfg_id,
                replace_visible_summaries=replace_visible_summaries,
                requested_report_ids=requested_ids,
            )
        else:
            pdf_started = time.monotonic()
            pdf_path = _convert_xlsx_to_pdf(
                src=xlsx_path,
                output_dir=report_temp,
                logo_root=logo_root,
                summary_date=summary_date,
            )
            logs.append(f"[{cfg.title}] Время PDF-конвертации: {_elapsed_seconds(pdf_started)}")
            logs.append(f"[{cfg.title}] PDF: {pdf_path.name}")
            if cfg_id in {"1", "1.18"}:
                _snapshot_report1_script_reference(
                    temp_root=temp_root,
                    master_id=cfg_id,
                    pdf_path=pdf_path,
                    logs=logs,
                    title=cfg.title,
                )
            created_item = _store_generated_pdf(
                db=db,
                pdf_path=pdf_path,
                title=storage_title,
                summary_date=summary_date,
                visible=publish_immediately,
                report_id=cfg_id,
                replace_visible_summaries=replace_visible_summaries,
                requested_report_ids=requested_ids,
            )
        db.refresh(created_item)
        if detach_row:
            db.expunge(created_item)
        if publish_immediately:
            logs.append(f"[{cfg.title}] Сохранено в БД и сразу опубликовано: id={created_item.id}")
        else:
            logs.append(f"[{cfg.title}] Сохранено в БД скрыто до финальной сборки: id={created_item.id}")
        logs.append(f"[{cfg.title}] Время записи в БД: {_elapsed_seconds(db_started)}")
        logs.append(f"[{cfg.title}] Общее время отчёта: {_elapsed_seconds(report_started)}")
        return created_item, None, logs
    except ReportGenerationCancelled:
        logs.append(f"[{cfg.title}] Прервано по запросу пользователя.")
        raise
    except Exception as exc:
        return (
            None,
            {
                "report_id": cfg.id,
                "title": cfg.title,
                "script_file": cfg.script,
                "error": str(exc) or exc.__class__.__name__,
            },
            logs + [f"[{cfg.title}] Ошибка: {exc}"],
        )


def _execute_reports_parallel(
    cfgs: list[ResolvedReportConfig],
    *,
    summary_date: date,
    request_report_date: date,
    logo_root: Path,
    temp_root: Path,
    api_snapshot_path: Path,
    prefetch_data_path: Path | None,
    api_snapshot_read_only: bool,
    plan_script_dir: Path | None,
    max_workers: int,
    should_cancel: Callable[[], bool] | None = None,
    report_23_fertilizer_transport_machines: int | None = None,
    r23_document_rebuild_only: bool = False,
    report25_mode: str | None = None,
    report22_cutoff: str | None = None,
    report22_time_from: str | None = None,
    report22_date_from: date | None = None,
    report22_date_to: date | None = None,
    report5_cutoff: str | None = None,
    report5_time_from: str | None = None,
    report2_cutoff: str | None = None,
    report2_time_from: str | None = None,
    report2_date_from: date | None = None,
    report2_date_to: date | None = None,
    report27_cutoff: str | None = None,
    report27_time_from: str | None = None,
    report27_date_from: date | None = None,
    report27_date_to: date | None = None,
    report_harvest_gross: dict | None = None,
    generation_job_id: str | None = None,
    report1_fast_groups: list[str] | None = None,
    report1_fast_slave_ids: frozenset[str] | None = None,
    report1_skip_harvest_fetch: bool = False,
    r23_work_data_bundle_path: Path | None = None,
    r23_auto_report2: bool = False,
    replace_visible_summaries: bool = False,
    requested_report_ids: frozenset[str] | None = None,
) -> tuple[list[Summary], list[dict[str, str]], list[str], bool]:
    created: list[Summary] = []
    failed: list[dict[str, str]] = []
    logs: list[str] = []
    cancelled = False
    requested_ids = requested_report_ids or frozenset()

    if not cfgs:
        return created, failed, logs, cancelled

    def _worker(cfg: ResolvedReportConfig) -> tuple[Summary | None, dict[str, str] | None, list[str]]:
        thread_db = SessionLocal()
        try:
            return _execute_resolved_report(
                cfg,
                summary_date=summary_date,
                request_report_date=request_report_date,
                logo_root=logo_root,
                temp_root=temp_root,
                api_snapshot_path=api_snapshot_path,
                prefetch_data_path=prefetch_data_path,
                api_snapshot_read_only=api_snapshot_read_only,
                plan_script_dir=plan_script_dir,
                report_23_fertilizer_transport_machines=report_23_fertilizer_transport_machines,
                r23_document_rebuild_only=r23_document_rebuild_only,
                report25_mode=report25_mode,
                report22_cutoff=report22_cutoff,
                report22_time_from=report22_time_from,
                report22_date_from=report22_date_from,
                report22_date_to=report22_date_to,
                report5_cutoff=report5_cutoff,
                report5_time_from=report5_time_from,
                report2_cutoff=report2_cutoff,
                report2_time_from=report2_time_from,
                report2_date_from=report2_date_from,
                report2_date_to=report2_date_to,
                report27_cutoff=report27_cutoff,
                report27_time_from=report27_time_from,
                report27_date_from=report27_date_from,
                report27_date_to=report27_date_to,
                report_harvest_gross=report_harvest_gross,
                generation_job_id=generation_job_id,
                should_cancel=should_cancel,
                report1_fast_groups=report1_fast_groups if _cfg_id_norm(cfg) in {"1", "1.18"} else None,
                report1_skip_harvest_fetch=report1_skip_harvest_fetch,
                r23_work_data_bundle_path=r23_work_data_bundle_path,
                r23_auto_report2=r23_auto_report2,
                replace_visible_summaries=replace_visible_summaries,
                requested_report_ids=requested_ids,
                db=thread_db,
                detach_row=True,
            )
        finally:
            thread_db.close()

    def _run_cfgs_sequential(
        seq_cfgs: list[ResolvedReportConfig],
    ) -> tuple[list[Summary], list[dict[str, str]], list[str], bool]:
        seq_created: list[Summary] = []
        seq_failed: list[dict[str, str]] = []
        seq_logs: list[str] = []
        seq_cancelled = False
        for cfg in seq_cfgs:
            if should_cancel and should_cancel():
                seq_cancelled = True
                seq_logs.append("Генерация остановлена по запросу пользователя.")
                break
            try:
                c_item, f_item, p_logs = _worker(cfg)
            except ReportGenerationCancelled:
                seq_cancelled = True
                seq_logs.append("Генерация остановлена по запросу пользователя.")
                break
            seq_logs.extend(p_logs)
            if c_item is not None:
                seq_created.append(c_item)
            if f_item is not None:
                seq_failed.append(f_item)
        return seq_created, seq_failed, seq_logs, seq_cancelled

    cfg_by_id = {_cfg_id_norm(c): c for c in cfgs}

    def _run_parallel_waves(
        lane_cfgs: list[ResolvedReportConfig],
        *,
        lane_workers: int,
        lane_label: str,
        donor_retry: bool,
        out_created: list[Summary],
        out_failed: list[dict[str, str]],
        out_logs: list[str],
    ) -> bool:
        """Возвращает True если отменено."""
        if not lane_cfgs:
            return False
        lane_cancelled = False
        if lane_label:
            out_logs.append(
                f"Дорожка «{lane_label}»: "
                + ", ".join(_cfg_id_norm(c) for c in lane_cfgs)
                + f" (воркеров: {lane_workers})"
            )
        waves = _report_execution_waves(
            lane_cfgs,
            report1_fast_slave_ids=report1_fast_slave_ids if donor_retry else frozenset(),
        )
        for wave_ix, wave_cfgs in enumerate(waves):
            if lane_cancelled or not wave_cfgs:
                continue
            if len(waves) > 1:
                prefix = f"{lane_label}, " if lane_label else ""
                out_logs.append(
                    f"{prefix}этап {wave_ix + 1}/{len(waves)}: "
                    + ", ".join(_cfg_id_norm(c) for c in wave_cfgs)
                )
            mw = max(1, min(int(lane_workers), len(wave_cfgs)))
            executor = ThreadPoolExecutor(max_workers=mw)
            future_to_cfg: dict = {}
            try:
                future_to_cfg = {executor.submit(_worker, cfg): cfg for cfg in wave_cfgs}
                for future in as_completed(future_to_cfg):
                    try:
                        c_item, f_item, p_logs = future.result()
                    except ReportGenerationCancelled:
                        lane_cancelled = True
                        out_logs.append("Генерация остановлена по запросу пользователя.")
                        break
                    out_logs.extend(p_logs)
                    if c_item is not None:
                        out_created.append(c_item)
                    if f_item is not None:
                        out_failed.append(f_item)

                    if should_cancel and should_cancel():
                        lane_cancelled = True
                        for f in future_to_cfg:
                            if not f.done():
                                f.cancel()
                        out_logs.append("Генерация остановлена по запросу пользователя.")
                        break
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            if lane_cancelled:
                break

            next_wave_is_r1 = (
                wave_ix + 1 < len(waves)
                and any(_cfg_id_norm(c) in _REPORT1_FAMILY_IDS for c in waves[wave_ix + 1])
            )
            donor_rounds = _donor_retry_rounds()
            if next_wave_is_r1 and donor_retry and report1_fast_slave_ids and donor_rounds > 0:
                def _lane_created_ids() -> set[str]:
                    return {_report_id_from_summary_title(item.title) for item in out_created}

                for donor_round in range(1, donor_rounds + 1):
                    missing_donors = sorted(report1_fast_slave_ids - _lane_created_ids())
                    if not missing_donors:
                        break
                    retry_cfgs = [cfg_by_id[rid] for rid in missing_donors if rid in cfg_by_id]
                    if not retry_cfgs:
                        break
                    out_logs.append(
                        "Сводка 1 fast-path: повтор доноров "
                        f"(раунд {donor_round}/{donor_rounds}, последовательно): "
                        + ", ".join(missing_donors)
                    )
                    gc.collect()
                    retried_ids = {_cfg_id_norm(c) for c in retry_cfgs}
                    out_failed[:] = [
                        f for f in out_failed if str(f.get("report_id") or "") not in retried_ids
                    ]
                    r_created, r_failed, r_logs, r_cancelled = _run_cfgs_sequential(retry_cfgs)
                    out_logs.extend(r_logs)
                    out_created.extend(r_created)
                    out_failed.extend(r_failed)
                    if r_cancelled:
                        lane_cancelled = True
                        break
        return lane_cancelled

    use_lanes = _dedicated_heavy_report_lanes_enabled()
    bulk_cfgs, r1_lane_cfgs, r2_lane_cfgs, r1_isolated = _split_report_execution_lanes(
        cfgs,
        report1_fast_slave_ids=report1_fast_slave_ids or frozenset(),
    )
    if use_lanes and (r1_lane_cfgs or r2_lane_cfgs):
        logs.append(
            "Параллельные дорожки: "
            + ", ".join(
                part
                for part in (
                    f"bulk ({len(bulk_cfgs)})" if bulk_cfgs else "",
                    "сводка 1" if r1_lane_cfgs else "",
                    "сводка 2" if r2_lane_cfgs else "",
                )
                if part
            )
        )
        if not r1_isolated and any(_cfg_id_norm(c) in _REPORT1_FAMILY_IDS for c in cfgs):
            logs.append(
                "Сводка 1 в общей дорожке: нужны доноры 3–8 из этой же пачки "
                "(standalone или SVODKI_DEDICATED_HEAVY_REPORT_LANES=0 для старого режима)."
            )
        lane_jobs: list[tuple[str, list[ResolvedReportConfig], int, bool]] = []
        if bulk_cfgs:
            lane_jobs.append(("bulk", bulk_cfgs, int(max_workers), False))
        if r1_lane_cfgs:
            lane_jobs.append(("сводка 1", r1_lane_cfgs, 1, True))
        if r2_lane_cfgs:
            lane_jobs.append(("сводка 2", r2_lane_cfgs, 1, False))

        lane_pool = ThreadPoolExecutor(max_workers=len(lane_jobs))

        def _lane_job(
            label: str,
            lane_cfgs: list[ResolvedReportConfig],
            workers: int,
            donor_retry: bool,
        ) -> tuple[list[Summary], list[dict[str, str]], list[str], bool]:
            lc: list[Summary] = []
            lf: list[dict[str, str]] = []
            ll: list[str] = []
            stop = _run_parallel_waves(
                lane_cfgs,
                lane_workers=workers,
                lane_label=label,
                donor_retry=donor_retry,
                out_created=lc,
                out_failed=lf,
                out_logs=ll,
            )
            return lc, lf, ll, stop

        try:
            futures = {
                lane_pool.submit(_lane_job, label, lane_cfgs, workers, donor_retry): label
                for label, lane_cfgs, workers, donor_retry in lane_jobs
            }
            for future in as_completed(futures):
                try:
                    lc, lf, ll, stop = future.result()
                except ReportGenerationCancelled:
                    cancelled = True
                    logs.append("Генерация остановлена по запросу пользователя.")
                    break
                logs.extend(ll)
                created.extend(lc)
                failed.extend(lf)
                if stop:
                    cancelled = True
                if should_cancel and should_cancel():
                    cancelled = True
                    logs.append("Генерация остановлена по запросу пользователя.")
                    break
        finally:
            lane_pool.shutdown(wait=False, cancel_futures=True)
    else:
        cancelled = _run_parallel_waves(
            cfgs,
            lane_workers=int(max_workers),
            lane_label="",
            donor_retry=bool(report1_fast_slave_ids),
            out_created=created,
            out_failed=failed,
            out_logs=logs,
        )

    # После параллельной пачки — только упавшие, по одной (снижает повторный -9/OOM/429).
    failed_retry_rounds = _failed_report_retry_rounds()
    for failed_round in range(1, failed_retry_rounds + 1):
        if cancelled or not failed:
            break
        failed_ids: list[str] = []
        seen_failed: set[str] = set()
        for item in failed:
            rid = str(item.get("report_id") or "").strip()
            if rid and rid not in seen_failed and rid in cfg_by_id:
                seen_failed.add(rid)
                failed_ids.append(rid)
        if not failed_ids:
            break
        retry_cfgs = [cfg_by_id[rid] for rid in failed_ids]
        logs.append(
            "Повтор упавших сводок (последовательно, раунд "
            f"{failed_round}/{failed_retry_rounds}): "
            + ", ".join(failed_ids)
        )
        logger.warning(
            "Retrying failed reports sequentially (round %s/%s): %s",
            failed_round,
            failed_retry_rounds,
            ", ".join(failed_ids),
        )
        gc.collect()
        time.sleep(10.0 * failed_round)
        retried_ids = set(failed_ids)
        failed[:] = [
            f for f in failed if str(f.get("report_id") or "") not in retried_ids
        ]
        r_created, r_failed, r_logs, r_cancelled = _run_cfgs_sequential(retry_cfgs)
        logs.extend(r_logs)
        created.extend(r_created)
        failed.extend(r_failed)
        if r_cancelled:
            cancelled = True
            break

    # Финальный rescue доноров сводки 1 (часто падают из-за OOM, сводка 1 ждёт 11/10…).
    if not cancelled and report1_fast_slave_ids:
        def _created_report_ids() -> set[str]:
            return {_report_id_from_summary_title(item.title) for item in created}

        for rescue_round in range(1, _donor_retry_rounds() + 1):
            missing_donors = sorted(report1_fast_slave_ids - _created_report_ids())
            if not missing_donors:
                break
            rescue_cfgs = [cfg_by_id[rid] for rid in missing_donors if rid in cfg_by_id]
            if not rescue_cfgs:
                break
            logs.append(
                "Финальный повтор доноров сводки 1 "
                f"(раунд {rescue_round}/{_donor_retry_rounds()}, последовательно): "
                + ", ".join(missing_donors)
            )
            logger.warning(
                "Final donor rescue for report 1 (round %s): %s",
                rescue_round,
                ", ".join(missing_donors),
            )
            gc.collect()
            time.sleep(15.0 * rescue_round)
            retried_ids = set(missing_donors)
            failed[:] = [
                f for f in failed if str(f.get("report_id") or "") not in retried_ids
            ]
            r_created, r_failed, r_logs, r_cancelled = _run_cfgs_sequential(rescue_cfgs)
            logs.extend(r_logs)
            created.extend(r_created)
            failed.extend(r_failed)
            if r_cancelled:
                cancelled = True
                break

        # Если доноры появились — повторить сводку 1, если она упала из-за них.
        missing_donors = sorted(report1_fast_slave_ids - _created_report_ids())
        if not missing_donors:
            report1_failed_ids = [
                str(f.get("report_id") or "")
                for f in failed
                if str(f.get("report_id") or "") in _REPORT1_FAMILY_IDS
            ]
            if report1_failed_ids:
                r1_cfgs = [cfg_by_id[rid] for rid in report1_failed_ids if rid in cfg_by_id]
                if r1_cfgs:
                    logs.append(
                        "Повтор сводки 1 после успешных доноров: "
                        + ", ".join(report1_failed_ids)
                    )
                    gc.collect()
                    failed[:] = [
                        f
                        for f in failed
                        if str(f.get("report_id") or "") not in set(report1_failed_ids)
                    ]
                    r_created, r_failed, r_logs, r_cancelled = _run_cfgs_sequential(r1_cfgs)
                    logs.extend(r_logs)
                    created.extend(r_created)
                    failed.extend(r_failed)
                    if r_cancelled:
                        cancelled = True

    return created, failed, logs, cancelled


def _prefetch_reports_parallel(
    cfgs: list[ResolvedReportConfig],
    *,
    request_report_date: date,
    temp_root: Path,
    api_snapshot_path: Path,
    plan_script_dir: Path | None,
    max_workers: int,
    should_cancel: Callable[[], bool] | None = None,
    generation_job_id: str | None = None,
) -> tuple[list[dict[str, str]], list[str], bool]:
    failed: list[dict[str, str]] = []
    logs: list[str] = [
        f"Prefetch API: старт, отчётов: {len(cfgs)}, JSON: {api_snapshot_path}"
    ]
    cancelled = False
    mw = max(1, min(int(max_workers), len(cfgs))) if cfgs else 1

    def _worker(cfg: ResolvedReportConfig) -> tuple[dict[str, str] | None, list[str]]:
        p_logs = [f"[{cfg.title}] Prefetch API: старт"]
        try:
            report_temp = temp_root / f"prefetch_{cfg.id}_{uuid.uuid4().hex}"
            report_temp.mkdir(parents=True, exist_ok=True)
            _, stdout = _run_report_script(
                cfg=cfg,
                plan_script_dir=plan_script_dir,
                output_dir=report_temp,
                report_date=request_report_date,
                api_snapshot_path=api_snapshot_path,
                prefetch_only=True,
                generation_job_id=generation_job_id,
                should_cancel=should_cancel,
            )
            if stdout.strip():
                p_logs.extend(
                    f"[{cfg.title}] {line}"
                    for line in stdout.splitlines()[-8:]
                    if line.strip()
                )
            p_logs.append(f"[{cfg.title}] Prefetch API: OK")
            return None, p_logs
        except ReportGenerationCancelled:
            raise
        except Exception as exc:
            return (
                {
                    "report_id": cfg.id,
                    "title": cfg.title,
                    "script_file": cfg.script,
                    "error": str(exc) or exc.__class__.__name__,
                },
                p_logs + [f"[{cfg.title}] Prefetch API: ошибка: {exc}"],
            )

    if not cfgs:
        return failed, logs, cancelled

    executor = ThreadPoolExecutor(max_workers=mw)
    try:
        future_to_cfg = {executor.submit(_worker, cfg): cfg for cfg in cfgs}
        for future in as_completed(future_to_cfg):
            try:
                f_item, p_logs = future.result()
            except ReportGenerationCancelled:
                cancelled = True
                logs.append("Prefetch API остановлен по запросу пользователя.")
                break
            logs.extend(p_logs)
            if f_item is not None:
                failed.append(f_item)

            if should_cancel and should_cancel():
                cancelled = True
                for f in future_to_cfg:
                    if not f.done():
                        f.cancel()
                logs.append("Prefetch API остановлен по запросу пользователя.")
                break
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if not failed and not cancelled:
        logs.append(f"Prefetch API: готово, JSON-снимок: {api_snapshot_path}")
    return failed, logs, cancelled


def _report_id_filename_re(report_id: str) -> re.Pattern[str]:
    """Regex для имён скриптов: id в начале имени файла."""
    if report_id == "0":
        # «0_1…» / «0.1…» — сводка 0.1; иначе «0» ошибочно подхватывает краткий утренний скрипт.
        return re.compile(r"^0(?!(?:\.\d|_\d))(?:\D|$)")
    return re.compile(rf"^{re.escape(report_id)}(?:\D|$)")


def _resolve_script_name(script_dir: Path, report_id: str, preferred_name: str) -> str:
    rid = str(report_id or "").strip()
    if rid in {"1", "1.18"}:
        override = (os.environ.get("SVODKI_REPORT1_SCRIPT") or "").strip()
        if override:
            override_path = script_dir / override
            if override_path.is_file():
                return override
    preferred = script_dir / preferred_name
    if preferred.exists():
        return preferred_name

    patterns = [
        f"{report_id}.main*.py",
        f"{report_id}.*main*.py",
        f"*{report_id}*.main*.py",
        f"{report_id}_main*.py",
        f"*{report_id}_main*.py",
    ]
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(script_dir.glob(pattern))
    id_re = _report_id_filename_re(report_id)
    candidates = [p for p in candidates if id_re.match(p.name)]
    if not candidates:
        # Фолбэк для "грязных" имён вида "3.main_real (6).py", "3_main copy.py" и т.п.
        # Берём только .py и только файлы, где id стоит в начале.
        candidates = [
            p
            for p in script_dir.glob("*.py")
            if id_re.match(p.name) and "main" in p.name.lower()
        ]
    if not candidates:
        # Последний шанс: любой python-файл, начинающийся с report_id.
        candidates = [p for p in script_dir.glob("*.py") if id_re.match(p.name)]
    if not candidates:
        raise ReportGenerationError(f"Не найден скрипт для сводки {report_id}: {preferred_name}")
    dotted_main_prefix = f"{report_id}.main"
    uniq = sorted(
        set(candidates),
        key=lambda p: (
            0 if p.name.lower().startswith(dotted_main_prefix.lower()) else 1,
            ("(" in p.name or ")" in p.name),
            len(p.name),
            p.name.lower(),
        ),
    )
    return uniq[0].name


def _python_for_scripts(script_dir: Path) -> str:
    explicit = os.environ.get("SVODKI_PYTHON", "").strip()
    if explicit:
        return explicit
    # Prefer the current interpreter used by API container to guarantee
    # availability of dependencies from backend/requirements.txt.
    current = (sys.executable or "").strip()
    if current:
        return current
    base = script_dir.parent
    for rel in (
        ("python", "bin", "python3"),
        ("python", "bin", "python"),
    ):
        candidate = base.joinpath(*rel)
        if candidate.exists():
            return str(candidate)
    for name in ("python3", "python", "py"):
        found = shutil.which(name)
        if found:
            return found
    return os.environ.get("PYTHON", "python3")


def _pythonpath_for_scripts(script_dir: Path, fallback_script_dir: Path | None) -> str:
    plan_root = script_dir.resolve()
    py_paths: list[str] = []
    seen: set[str] = set()

    def _add_py_path(p: Path) -> None:
        try:
            r = p.resolve()
        except OSError:
            return
        if not r.is_dir():
            return
        s = str(r)
        if s not in seen:
            seen.add(s)
            py_paths.append(s)

    try:
        import importlib.util

        paths_mod_path = plan_root / "plan_paths.py"
        if paths_mod_path.is_file():
            spec = importlib.util.spec_from_file_location("_svodki_plan_paths", paths_mod_path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                for entry in mod.plan_pythonpath_entries():
                    _add_py_path(entry)
    except Exception:
        _add_py_path(plan_root)

    if not py_paths:
        _add_py_path(plan_root)
    if fallback_script_dir is not None:
        _add_py_path(Path(fallback_script_dir))
    if os.environ.get("PYTHONPATH"):
        py_paths.append(os.environ["PYTHONPATH"])
    return os.pathsep.join(py_paths)


def _run_api_data_prefetch(
    *,
    primary_script_dir: Path,
    plan_script_dir: Path | None,
    svodki_root: Path,
    output_path: Path,
    report_date: date,
    report_ids: list[str] | None = None,
    report1_fast_slave_ids: frozenset[str] | None = None,
    generation_job_id: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[str]:
    prefetch_script = primary_script_dir / "svodki_prefetch.py"
    if not prefetch_script.exists() and plan_script_dir is not None:
        prefetch_script = plan_script_dir / "svodki_prefetch.py"
    if not prefetch_script.exists():
        raise ReportGenerationError(f"Не найден сборщик данных API: {prefetch_script}")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["SVODKI_DISABLE_API_CACHE"] = "1"
    env.setdefault("PYTHONWARNINGS", "ignore")
    env["REPORT_DATE"] = report_date.isoformat()
    if report_ids:
        env["SVODKI_PREFETCH_REPORT_IDS"] = ",".join(str(x) for x in report_ids if str(x).strip())
    if report1_fast_slave_ids:
        env["SVODKI_PREFETCH_REPORT1_FAST_SLAVES"] = ",".join(sorted(report1_fast_slave_ids))
    # Сам prefetcher должен ходить в живой Cropwise, а не пытаться читать
    # ещё несуществующий собственный JSON через sitecustomize: путь
    # передаётся только аргументом командной строки.
    env.pop(PREFETCH_DATA_ENV_VAR, None)
    env.pop(PREFETCH_STRICT_ENV_VAR, None)
    env.pop(API_SNAPSHOT_ENV_VAR, None)
    env.pop(API_SNAPSHOT_READ_ONLY_ENV_VAR, None)
    tok = get_cropwise_token()
    if tok:
        env["CROPWISE_API_TOKEN"] = tok
    cw_base = (settings.cropwise_base_url or "").strip()
    if cw_base:
        env["CROPWISE_BASE_URL"] = cw_base
    if settings.svodki_season_start_date:
        env["SVODKI_SEASON_START_DATE"] = str(settings.svodki_season_start_date)
        env["SEASON_START_DATE"] = str(settings.svodki_season_start_date)
    if report_ids and "26" in {str(x).strip() for x in report_ids}:
        harvest_start = combine_season_start_for_date(report_date)
        env["SVODKI_COMBINE_SEASON_START"] = harvest_start.isoformat()
    inject_plans_edits_env(env, Path(output_path.parent))
    inject_season_range_env(env, report_date)
    env["PYTHONPATH"] = _pythonpath_for_scripts(primary_script_dir, plan_script_dir)
    _inject_cropwise_http_env(env)

    python_exe = _python_for_scripts(primary_script_dir)
    timeout_sec = _script_run_timeout()
    started = time.monotonic()
    last_out = ""
    last_err = ""
    last_rc = -1
    max_attempts = _script_max_attempts()
    for attempt in range(1, max_attempts + 1):
        if should_cancel is not None and should_cancel():
            raise ReportGenerationCancelled()
        last_rc, last_out, last_err = _run_logged_subprocess_interruptible(
            [python_exe, str(prefetch_script), str(output_path)],
            cwd=Path(output_path.parent),
            env=env,
            log_dir=output_path.parent,
            generation_job_id=generation_job_id,
            should_cancel=should_cancel,
            timeout_sec=timeout_sec,
        )
        if last_rc == 0:
            break
        if not _subprocess_should_retry(last_rc, last_out, last_err, attempt, max_attempts):
            break
        logger.warning(
            "Prefetch JSON transient failure (attempt %s/%s, rc=%s), retry in %ss",
            attempt,
            max_attempts,
            last_rc,
            _subprocess_retry_backoff_sec(last_rc, attempt),
        )
        if _subprocess_exit_is_transient(last_rc):
            gc.collect()
        time.sleep(_subprocess_retry_backoff_sec(last_rc, attempt))

    logs = [f"Prefetch JSON: отдельный сборщик данных, время: {_elapsed_seconds(started)}"]
    if last_out:
        logs.extend(line for line in last_out.splitlines()[-30:] if line.strip())
    if last_rc != 0:
        combined = "\n".join(line for line in (last_out or "").splitlines() if line.strip())
        err_m = (last_err or combined or "").strip()
        fatal = next(
            (line for line in reversed((last_out or "").splitlines()) if "[prefetch] FATAL:" in line),
            "",
        )
        detail = fatal or err_m
        raise ReportGenerationError(
            f"Prefetch JSON завершился с кодом {last_rc}. {detail[:2400]}".strip()
        )
    logs.append(f"Prefetch JSON: готово, файл: {output_path}")
    return logs


def _run_report_script(
    cfg: ResolvedReportConfig,
    plan_script_dir: Path | None,
    output_dir: Path,
    report_date: date,
    api_snapshot_path: Path | None = None,
    prefetch_data_path: Path | None = None,
    api_snapshot_read_only: bool = False,
    prefetch_only: bool = False,
    report_23_fertilizer_transport_machines: int | None = None,
    r23_document_rebuild_only: bool = False,
    report25_mode: str | None = None,
    report22_cutoff: str | None = None,
    report22_time_from: str | None = None,
    report22_date_from: date | None = None,
    report22_date_to: date | None = None,
    report5_cutoff: str | None = None,
    report5_time_from: str | None = None,
    report2_cutoff: str | None = None,
    report2_time_from: str | None = None,
    report2_date_from: date | None = None,
    report2_date_to: date | None = None,
    report27_cutoff: str | None = None,
    report27_time_from: str | None = None,
    report27_date_from: date | None = None,
    report27_date_to: date | None = None,
    report_harvest_gross: dict | None = None,
    generation_job_id: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
    report1_fast_groups: list[str] | None = None,
    report1_skip_harvest_fetch: bool = False,
    r23_work_data_bundle_path: Path | None = None,
    r23_auto_report2: bool = False,
) -> tuple[Path, str]:
    script_dir = cfg.script_dir
    script_path = script_dir / cfg.script
    if not script_path.exists():
        raise ReportGenerationError(f"Файл не найден: {script_path}")

    python_exe = _python_for_scripts(script_dir)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PYTHONWARNINGS", "ignore")
    env["REPORT_DATE"] = report_date.isoformat()
    env["SVODKI_REPORT_ID"] = str(cfg.id)
    if report1_fast_groups and _cfg_id_norm(cfg) in {"1", "1.18"} and not _report1_standalone_enabled():
        env["REPORT1_SKIP_PERF_GROUPS"] = "|".join(report1_fast_groups)
    if _report1_standalone_enabled() and _cfg_id_norm(cfg) in {"1", "1.18"}:
        env["REPORT1_STANDALONE"] = "1"
        env.pop("REPORT1_SKIP_PERF_GROUPS", None)
        if report1_skip_harvest_fetch:
            env["REPORT1_SKIP_HARVEST_FETCH"] = "1"
        else:
            env.pop("REPORT1_SKIP_HARVEST_FETCH", None)
    elif report1_skip_harvest_fetch and _cfg_id_norm(cfg) in {"1", "1.18"}:
        env["REPORT1_SKIP_HARVEST_FETCH"] = "1"
    eff_manual = output_dir / f".manual_overrides_effective_{uuid.uuid4().hex}.json"
    write_effective_manual_store_for_date(report_date, eff_manual)
    env["SVODKI_MANUAL_OVERRIDES_PATH"] = str(eff_manual)
    cfg_id = _cfg_id_norm(cfg)
    report27_date_from = _coerce_optional_date(report27_date_from)
    report27_date_to = _coerce_optional_date(report27_date_to)
    report27_time_from = _normalize_report22_cutoff(report27_time_from) or "06:00"
    report27_cutoff = _normalize_report22_cutoff(report27_cutoff) or "06:00"
    if cfg_id in {"10", "11", "13", "26"} or _is_report27_family(cfg_id):
        if cfg_id in {"10", "11", "13", "26"}:
            harvest_start = combine_season_start_for_date(report_date)
            env["SVODKI_COMBINE_SEASON_START"] = harvest_start.isoformat()
            if _cfg_id_norm(cfg) == "26":
                env["DATE_TO"] = report_date.isoformat()
                env["DATE_FROM"] = harvest_start.isoformat()
            if _cfg_id_norm(cfg) in {"10", "11", "13"}:
                if settings.svodki_season_start_date:
                    env["SVODKI_WORK_SEASON_START_DATE"] = str(settings.svodki_season_start_date)
                env["SVODKI_SEASON_START_DATE"] = harvest_start.isoformat()
                env["SEASON_START_DATE"] = harvest_start.isoformat()
    inject_plans_edits_env(env, output_dir)
    inject_season_range_env(env, report_date)
    # season_ranges для «5.0 Уборка» задаёт календарь (01.07), а сводки 10/11/13 —
    # комбайновый сезон с 10.06; восстанавливаем после inject_season_range_env.
    if _cfg_id_norm(cfg) in {"10", "11", "13"}:
        _harvest_start = combine_season_start_for_date(report_date)
        _harvest_iso = _harvest_start.isoformat()
        env["SVODKI_COMBINE_SEASON_START"] = _harvest_iso
        env["SVODKI_SEASON_START_DATE"] = _harvest_iso
        env["SEASON_START_DATE"] = _harvest_iso
    # Не использовать унаследованный SVODKI_API_CACHE_PATH из процесса web: иначе
    # возможен «липкий» снимок с прошлых запусков/другой даты.
    env.pop(API_SNAPSHOT_ENV_VAR, None)
    env.pop(API_SNAPSHOT_READ_ONLY_ENV_VAR, None)
    env.pop(DISABLE_API_CACHE_ENV_VAR, None)
    use_api_cache = _env_flag(USE_API_CACHE_ENV_VAR, False)
    if use_api_cache and api_snapshot_path is not None:
        env[API_SNAPSHOT_ENV_VAR] = str(api_snapshot_path)
        if api_snapshot_read_only:
            env[API_SNAPSHOT_READ_ONLY_ENV_VAR] = "1"
    else:
        env[DISABLE_API_CACHE_ENV_VAR] = "1"
    env.pop(PREFETCH_DATA_ENV_VAR, None)
    env.pop(PREFETCH_STRICT_ENV_VAR, None)
    if prefetch_data_path is not None and _uses_unified_prefetch_data(cfg):
        env[PREFETCH_DATA_ENV_VAR] = str(prefetch_data_path)
        standalone_r1 = _report1_standalone_enabled() and cfg_id in {"1", "1.18"}
        env[PREFETCH_STRICT_ENV_VAR] = (
            "0" if (_report1_uses_prefetch_json(cfg) or standalone_r1) else "1"
        )
    elif cfg_id in {"1", "1.18"} and not _report1_standalone_enabled():
        env["SVODKI_REPORT1_LIVE_MTFMI"] = "1"
    elif _cfg_id_norm(cfg) == "2":
        env["SVODKI_REPORT2_LIVE_API"] = "1"
    if prefetch_only:
        env[PREFETCH_ONLY_ENV_VAR] = "1"
    env.pop(REPORT23_FERTILIZER_TRANSPORT_MACHINES_ENV, None)
    env.pop(REPORT23_MACHINES_SIDECAR_ENV, None)
    env.pop(REPORT23_EDITOR_BODY_SIDECAR_ENV, None)
    env.pop(REPORT23_WORK_SNAPSHOT_ENV, None)
    env.pop(R23_DOCUMENT_REBUILD_ONLY_ENV, None)
    env.pop(R23_WORK_DATA_BUNDLE_OUT_ENV, None)
    env.pop(R23_WORK_DATA_BUNDLE_IN_ENV, None)
    env.pop(R23_BUNDLE_ONLY_ENV, None)
    env.pop(REPORT25_MODE_ENV, None)
    env.pop("DAY_START_HOUR", None)
    env.pop("DAY_START_MINUTE", None)
    env.pop("REPORT_WINDOW_START", None)
    env.pop("REPORT_WINDOW_END", None)
    env.pop(HARVEST_GROSS_ENV, None)
    env.pop(REPORT_FOOTNOTES_ENV, None)
    r25_mode = _normalize_report25_mode(report25_mode) if _cfg_id_norm(cfg) == "25" else None
    if r25_mode:
        env[REPORT25_MODE_ENV] = r25_mode
    donor_bundle = r23_work_data_from_report2_bundle_path(report_date)
    if cfg_id == "2":
        env[R23_WORK_DATA_BUNDLE_OUT_ENV] = str(donor_bundle)
        if r23_auto_report2:
            env[R23_BUNDLE_ONLY_ENV] = "1"
    elif cfg_id == "23" and not r23_document_rebuild_only and not prefetch_only:
        if donor_bundle.is_file():
            env[R23_WORK_DATA_BUNDLE_IN_ENV] = str(donor_bundle)
    r_cutoff: str | None = None
    r_time_from: str | None = None
    r_date_from: date | None = None
    r_date_to: date | None = None
    r_window_start: str | None = None
    r_window_end: str | None = None
    if cfg_id == "22":
        r_cutoff = _normalize_report22_cutoff(report22_cutoff)
        r_time_from = _normalize_report22_cutoff(report22_time_from)
        r_date_from = report22_date_from
        r_date_to = report22_date_to
    elif cfg_id == "2":
        r_cutoff = _normalize_report22_cutoff(report2_cutoff)
        r_time_from = _normalize_report22_cutoff(report2_time_from)
        r_date_from = report2_date_from
        r_date_to = report2_date_to
    elif _is_report27_family(cfg_id):
        r_cutoff = report27_cutoff
        r_time_from = report27_time_from
        r_date_from = report27_date_from
        r_date_to = report27_date_to
        if r_date_from and r_date_to:
            env["REPORT27_DATE_FROM"] = r_date_from.isoformat()
            env["REPORT27_DATE_TO"] = r_date_to.isoformat()
            env["REPORT27_TIME_FROM"] = r_time_from
            env["REPORT27_CUTOFF"] = r_cutoff
            env["DATE_FROM"] = r_date_from.isoformat()
            env["DATE_TO"] = r_date_to.isoformat()
        else:
            period_from = rating_period_from_for_date(report_date)
            env["DATE_TO"] = report_date.isoformat()
            env["DATE_FROM"] = period_from.isoformat()
            r_date_from = period_from
            r_date_to = report_date
            r_time_from = "06:00"
            r_cutoff = "06:00"
    elif cfg_id == "5":
        r_cutoff = _normalize_report22_cutoff(report5_cutoff)
        r_time_from = _normalize_report22_cutoff(report5_time_from) or "06:00"
        if r_cutoff:
            data_date = report_date - timedelta(days=1)
            r_window_start, r_window_end = _build_report22_window_iso(
                data_date, r_time_from, data_date, r_cutoff
            )
    if r_date_from is not None and r_date_to is not None:
        if cfg_id == "2" or _is_report27_family(cfg_id):
            r_window_start, r_window_end = _build_report2_period_window_iso(
                r_date_from, r_time_from, r_date_to, r_cutoff
            )
        else:
            r_window_start, r_window_end = _build_report22_window_iso(
                r_date_from, r_time_from, r_date_to, r_cutoff
            )
    elif r_cutoff and cfg_id != "5":
        effective_from = r_time_from or "06:00"
        r_window_start, r_window_end = _build_report22_window_iso(
            report_date, effective_from, report_date, r_cutoff
        )
    if r_window_start and r_window_end:
        env["REPORT_WINDOW_START"] = r_window_start
        env["REPORT_WINDOW_END"] = r_window_end
        if _is_report27_family(cfg_id):
            ws = _parse_report_window_for_iso(r_window_start)
            we = _parse_report_window_for_iso(r_window_end)
            if ws and we and ws < we:
                env["DATE_FROM"] = ws.date().isoformat()
                end_label = we - timedelta(seconds=1)
                env["DATE_TO"] = (
                    end_label.date().isoformat()
                    if end_label.date() >= ws.date()
                    else we.date().isoformat()
                )
    if r_cutoff and cfg_id == "22":
        hour_s, minute_s = r_cutoff.split(":", 1)
        env["REPORT_HOUR"] = str(int(hour_s))
        env["REPORT_MINUTE"] = str(int(minute_s))
    if r_time_from and not r_window_start and cfg_id == "22":
        hour_s, minute_s = r_time_from.split(":", 1)
        env["DAY_START_HOUR"] = str(int(hour_s))
        env["DAY_START_MINUTE"] = str(int(minute_s))
    if _is_analytics_report_23(cfg) and not prefetch_only:
        env[REPORT23_WORK_SNAPSHOT_ENV] = str((output_dir / ".r23_work_narrative_snapshot.txt").resolve())
        try:
            payload = json.loads(eff_manual.read_text(encoding="utf-8"))
            # Непустое значение: весь блок по работам в docx = этот текст; простои добавляются скриптом после.
            body_raw = (payload.get("23") or {}).get("analytics_document_body")
            body = str(body_raw if body_raw is not None else "")
        except (OSError, ValueError, TypeError):
            body = ""
        if body:
            try:
                sidecar_ed = output_dir / ".r23_editor_body.txt"
                sidecar_ed.write_text(body, encoding="utf-8")
                env[REPORT23_EDITOR_BODY_SIDECAR_ENV] = str(sidecar_ed.resolve())
            except OSError:
                pass
        if r23_document_rebuild_only:
            env[R23_DOCUMENT_REBUILD_ONLY_ENV] = "1"
    if (
        _is_analytics_report_23(cfg)
        and report_23_fertilizer_transport_machines is not None
        and not prefetch_only
    ):
        n = int(report_23_fertilizer_transport_machines)
        env[REPORT23_FERTILIZER_TRANSPORT_MACHINES_ENV] = str(n)
        try:
            sidecar = output_dir / ".r23_fertilizer_transport_machines"
            sidecar.write_text(str(n), encoding="utf-8")
            env[REPORT23_MACHINES_SIDECAR_ENV] = str(sidecar.resolve())
        except OSError:
            pass
    tok = get_cropwise_token()
    if tok:
        env["CROPWISE_API_TOKEN"] = tok
    cw_base = (settings.cropwise_base_url or "").strip()
    if cw_base:
        env["CROPWISE_BASE_URL"] = cw_base
    gross_norm = _normalize_report_harvest_gross(report_harvest_gross)
    if gross_norm and cfg_id in {"1", "1.18", "10", "13"}:
        env[HARVEST_GROSS_ENV] = json.dumps(gross_norm, ensure_ascii=False)

    env["PYTHONPATH"] = _pythonpath_for_scripts(script_dir, plan_script_dir)
    _inject_cropwise_http_env(env)
    if cfg_id in {"1", "1.18", "10", "13", "14"}:
        _inject_namolot_env(env)
        _ensure_namolot_available(env)

    cmd = [python_exe, str(script_path)]
    if cfg_id == "13":
        cmd.extend(["--date", report_date.isoformat()])
    last_out = ""
    last_err = ""
    last_code = -1
    timeout_sec = _script_run_timeout()
    log_dir = Path(output_dir)
    max_attempts = _script_max_attempts()
    for attempt in range(1, max_attempts + 1):
        if should_cancel is not None and should_cancel():
            raise ReportGenerationCancelled()
        try:
            proc_code, last_out, last_err = _run_logged_subprocess_interruptible(
                cmd,
                cwd=log_dir,
                env=env,
                log_dir=log_dir,
                generation_job_id=generation_job_id,
                should_cancel=should_cancel,
                timeout_sec=timeout_sec,
            )
        except ReportGenerationCancelled:
            raise
        last_code = proc_code
        if proc_code == 0:
            break
        if not _subprocess_should_retry(proc_code, last_out, last_err, attempt, max_attempts):
            break
        logger.warning(
            "Report script transient failure %s attempt %s/%s (rc=%s), retry in %ss",
            cfg.script,
            attempt,
            max_attempts,
            proc_code,
            _subprocess_retry_backoff_sec(proc_code, attempt),
        )
        if _subprocess_exit_is_transient(proc_code):
            gc.collect()
        time.sleep(_subprocess_retry_backoff_sec(proc_code, attempt))

    if last_code != 0:
        if should_cancel is not None and should_cancel():
            raise ReportGenerationCancelled()
        err_text = (last_err or last_out).strip()
        raise ReportGenerationError(
            f"Скрипт {cfg.script} завершился с кодом {last_code}. {err_text[:1200]}".strip()
        )

    if prefetch_only:
        return output_dir / "_prefetch_only.xlsx", last_out

    saved_path = _extract_saved_xlsx_path(last_out, output_dir, cfg.title, report_date)
    return saved_path, last_out


def _extract_saved_xlsx_path(stdout: str, output_dir: Path, title: str, report_date: date) -> Path:
    for line in stdout.splitlines():
        if "Сохранено:" not in line:
            continue
        rest = line.split("Сохранено:", 1)[1].strip()
        fname = rest.split()[0] if rest else ""
        if not fname:
            continue
        original = Path(fname) if os.path.isabs(fname) else output_dir / fname
        renamed = _rename_output_to_title(original, title, report_date)
        if renamed.exists():
            return renamed
    candidates = sorted(output_dir.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise ReportGenerationError("Скрипт не создал xlsx-файл.")
    renamed = _rename_output_to_title(candidates[0], title, report_date)
    if not renamed.exists():
        raise ReportGenerationError("Не удалось определить итоговый xlsx-файл.")
    return renamed


def _rename_output_to_title(filepath: Path, title: str, fallback_date: date | None) -> Path:
    safe = re.sub(r'[/\\:*?"<>|]', "-", title).strip() or "отчет"
    date_str = _format_ru_date(_title_display_date(title, fallback_date)) if fallback_date else ""
    new_name = f"{safe} за {date_str}.xlsx" if date_str else f"{safe}.xlsx"
    new_path = filepath.parent / new_name
    if new_path != filepath and filepath.exists():
        filepath.replace(new_path)
        return new_path
    return filepath


def _report_display_date(report_date: date | None) -> date | None:
    if report_date is None:
        return None
    return report_date - timedelta(days=1)


def _title_report_id(title: str) -> str:
    return _extract_report_id_from_stem(str(title or "").strip())


def _title_display_date(title: str, report_date: date | None) -> date | None:
    if report_date is None:
        return None
    rid = _title_report_id(title)
    if rid == "22":
        return report_date
    if rid == "25":
        t = (title or "").casefold()
        if "(утро)" in t or "(утрен" in t:
            return report_date - timedelta(days=1)
        return report_date
    return _report_display_date(report_date)


def _format_ru_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _title_with_report_day(title: str, report_date: date) -> str:
    return f"{title} за {_format_ru_date(_title_display_date(title, report_date) or report_date)}"


def _resolve_storage_title(
    cfg: ResolvedReportConfig,
    summary_date: date,
    *,
    report25_mode: str | None = None,
    report22_cutoff: str | None = None,
    report22_time_from: str | None = None,
    report22_date_from: date | None = None,
    report22_date_to: date | None = None,
    report2_cutoff: str | None = None,
    report2_time_from: str | None = None,
    report2_date_from: date | None = None,
    report2_date_to: date | None = None,
    report27_cutoff: str | None = None,
    report27_time_from: str | None = None,
    report27_date_from: date | None = None,
    report27_date_to: date | None = None,
) -> str:
    cfg_id = _cfg_id_norm(cfg)
    base_title = _artifact_base_title(cfg)
    r25_mode = _normalize_report25_mode(report25_mode) if cfg_id == "25" else None
    r22_cutoff = _normalize_report22_cutoff(report22_cutoff) if cfg_id == "22" else None
    r22_time_from = _normalize_report22_cutoff(report22_time_from) if cfg_id == "22" else None
    r22_date_from = report22_date_from if cfg_id == "22" else None
    r22_date_to = report22_date_to if cfg_id == "22" else None
    r2_cutoff = _normalize_report22_cutoff(report2_cutoff) if cfg_id == "2" else None
    r2_time_from = _normalize_report22_cutoff(report2_time_from) if cfg_id == "2" else None
    r2_date_from = report2_date_from if cfg_id == "2" else None
    r2_date_to = report2_date_to if cfg_id == "2" else None
    r27_cutoff = _normalize_report22_cutoff(report27_cutoff) if _is_report27_family(cfg_id) else None
    r27_time_from = _normalize_report22_cutoff(report27_time_from) if _is_report27_family(cfg_id) else None
    r27_date_from = report27_date_from if _is_report27_family(cfg_id) else None
    r27_date_to = report27_date_to if _is_report27_family(cfg_id) else None

    if cfg_id == "25":
        storage_title = _report25_storage_title(base_title, r25_mode)
    elif cfg_id == "22" and (r22_cutoff or r22_time_from or r22_date_from or r22_date_to):
        storage_title = _report22_storage_title(
            base_title, r22_cutoff, r22_time_from, r22_date_from, r22_date_to
        )
    elif cfg_id == "2" and (r2_cutoff or r2_time_from or r2_date_from or r2_date_to):
        storage_title = _report22_storage_title(
            base_title, r2_cutoff, r2_time_from, r2_date_from, r2_date_to
        )
    elif _is_report27_family(cfg_id) and (r27_cutoff or r27_time_from or r27_date_from or r27_date_to):
        storage_title = _report22_storage_title(
            base_title, r27_cutoff, r27_time_from, r27_date_from, r27_date_to
        )
    else:
        storage_title = base_title
    storage_title = _title_with_report_day(storage_title, summary_date)
    report2_excel_only = cfg_id == "2" and r2_date_from is not None and r2_date_to is not None
    report27_excel_only = _is_report27_family(cfg_id)
    if report2_excel_only or report27_excel_only:
        if " (Excel)" not in storage_title:
            storage_title = f"{storage_title} (Excel)"
    return storage_title


def _parse_export_stem_title_date(stem: str) -> tuple[str, date] | None:
    m_ru = re.search(r"\s+за\s+(\d{2})\.(\d{2})\.(\d{4})$", stem.strip(), re.I)
    if m_ru:
        try:
            dd = date(int(m_ru.group(3)), int(m_ru.group(2)), int(m_ru.group(1)))
        except ValueError:
            return None
        title = stem[: m_ru.start()].strip()
        return (title, dd) if title else None
    m = re.search(r"_(\d{4}-\d{2}-\d{2})$", stem.strip())
    if not m:
        return None
    ds = m.group(1)
    try:
        dd = date.fromisoformat(ds)
    except ValueError:
        return None
    title = stem[: m.start()].strip()
    if not title:
        return None
    return title, dd


def _pdf_stem_with_prev_day(stem: str) -> str:
    parsed = _parse_export_stem_title_date(stem)
    if not parsed:
        return stem
    title, dd = parsed
    if re.search(r"\s+за\s+\d{2}\.\d{2}\.\d{4}$", stem.strip(), re.I):
        return stem
    return f"{title} за {_format_ru_date(dd - timedelta(days=1))}"


# Подстановка страниц PDF сводки 1 из отчётов 3–8/5: по умолчанию ВКЛ.
# Страница донора + правый столбец с исходной страницы сводки 1 (pymupdf), см. pdf_graft_merge_last_col.py
# Отключить только сводку 1: SVODKI_DISABLE_REPORT1_PDF_PAGE_GRAFT=1
# Доля ширины «последнего столбца»: SVODKI_PDF_GRAFT_LAST_COL_FRAC (по умол. 0.11)
# Левый блок по умолчанию растеризуется (один слой вместо двух таблиц в PDF): откл. SVODKI_PDF_GRAFT_VECTOR_LEFT=1
# DPI левой растра: SVODKI_PDF_GRAFT_LEFT_RASTER_DPI (по умол. 132, 96–240)
# Только целые страницы без склейки столбца: SVODKI_PDF_GRAFT_NO_LAST_COL_MERGE=1
# Вертикаль 1:1 (старое): SVODKI_PDF_GRAFT_LEGACY_VERTICAL=1
# Стр. посева ← отчёт 5: белая маска (pdf_page_whiteout); SVODKI_REPORT1_PAGE28_WHITEOUT=0 — выкл.
# Все graft: SVODKI_DISABLE_PDF_PAGE_GRAFT=1
# Данные 3/4/5/6/7/8 в сводку 1: по умолчанию копируются в Excel до PDF (SVODKI_REPORT1_XLSX_SLAVE_FILL=0 — выкл.)
# Номера страниц — запасной вариант; при генерации пересчитываются по листам мастера.
REPORT1_PDF_GRAFT_FROM_SLAVES_SPEC: tuple[tuple[int, str, int], ...] = (
    (2, "11", 2),
    (3, "10", 1),
    (4, "10", 2),
    # 3–9: первые страницы отдельных сводок → соответствующие таблицы сводки 1.
    # Номера стр. — запасные; при генерации пересчитываются по листам мастера.
    (25, "3", 1),
    (33, "4", 1),
    (34, "7", 1),
    (11, "8", 1),
    (5, "5", 1),
    (14, "6", 1),
    (13, "9", 1),
)

# Подстановка в XLSX сводки 1 (листы «Выраб. — …»). Отчёт 5: блок «КУЛЬТУРЫ» (таблица выработки).
REPORT1_XLSX_SLAVE_GRAFT_SPEC: tuple[tuple[int, str, int], ...] = REPORT1_PDF_GRAFT_FROM_SLAVES_SPEC

REPORT1_FAST_SLAVE_GROUPS_BY_ID: dict[str, str] = {
    "4": "2.1 Боронование",
    "5": "4.0 Посев",
    "6": "3.1 Опрыскивание",
    "7": "2.9 Дискование",
    "8": "2.11 Культивация",
    # 9: вспашка+глубокорыхление — отдельный лист, не через SKIP_PERF_GROUPS.
}
REPORT1_HARVEST_DONOR_IDS: frozenset[str] = frozenset({"10", "11"})
REPORT1_HARVEST_ONLY_GRAFT_SPEC: tuple[tuple[int, str, int], ...] = (
    (2, "11", 2),
    (3, "10", 1),
    (4, "10", 2),
)


def _report1_standalone_harvest_hybrid(ordered_ids: list[str]) -> bool:
    """Standalone 1 + доноры 10/11 в пачке: уборку не считаем в 1, подставляем из доноров."""
    if not _report1_standalone_enabled():
        return False
    ids = set(ordered_ids)
    if not ({"1", "1.18"} & ids):
        return False
    return REPORT1_HARVEST_DONOR_IDS <= ids


def _report1_harvest_donors_in_batch(ordered_ids: list[str]) -> frozenset[str]:
    ids = set(ordered_ids)
    if not ({"1", "1.18"} & ids):
        return frozenset()
    return REPORT1_HARVEST_DONOR_IDS & ids


def _report1_skip_harvest_fetch(ordered_ids: list[str]) -> bool:
    """Сводка 1 не тянет 10/11 повторно — листы из XLSX/PDF доноров той же пачки."""
    return _report1_harvest_donors_in_batch(ordered_ids) >= REPORT1_HARVEST_DONOR_IDS


def _report1_required_donor_ids(ordered_ids: list[str]) -> frozenset[str]:
    req = _report1_fast_slave_ids(ordered_ids)
    if _report1_skip_harvest_fetch(ordered_ids):
        req = req | REPORT1_HARVEST_DONOR_IDS
    return req


def _report1_fast_slave_ids(ordered_ids: list[str]) -> frozenset[str]:
    """Groups in report 1 that can be left as skeletons because same-batch donors fill them."""
    if _report1_standalone_enabled():
        return frozenset()
    if not _env_flag("SVODKI_REPORT1_SKIP_DONOR_PERF_FETCH", True):
        return frozenset()
    ids = set(ordered_ids)
    if "1" not in ids and "1.18" not in ids:
        return frozenset()
    return frozenset(rid for rid in REPORT1_FAST_SLAVE_GROUPS_BY_ID if rid in ids)


def _report1_pdf_graft_operations_enabled() -> tuple[tuple[int, str, int], ...]:
    if _env_flag("SVODKI_DISABLE_REPORT1_PDF_PAGE_GRAFT", False):
        return ()
    try:
        from .report1_layout_editor import effective_pdf_graft_spec

        custom = effective_pdf_graft_spec()
        if custom is not None:
            return custom
    except Exception:
        pass
    return REPORT1_PDF_GRAFT_FROM_SLAVES_SPEC


def _report_id_from_summary_title(title: str) -> str:
    """Сопоставляет запись Summary.title с report_id из REPORT_CONFIGS_BASE."""
    t = (title or "").strip()
    if re.match(r"^0\.1(?:\s|\.|[А-Яа-яЁё]|$)", t):
        return "0.1"
    m = re.match(r"^(\d+\.\d+)", t)
    if m:
        return m.group(1)
    m = re.match(r"^(\d+)\.", t)
    if m:
        return m.group(1)
    return ""


def find_published_summary_by_report_id(
    db: Session,
    summary_date: date,
    report_id: str,
) -> Summary | None:
    rid = str(report_id or "").strip()
    if not rid:
        return None
    for item in list_summaries_by_date(db, summary_date, visible_only=True):
        if _report_id_from_summary_title(item.title) == rid:
            return item
    return None


def _latest_pdf_paths_by_report_id(db: Session, summary_date: date) -> dict[str, Path]:
    """По дате на платформе: последняя запись Summary на каждый report_id (макс. id)."""
    best: dict[str, tuple[int, Path]] = {}
    for item in list_summaries_by_date(db, summary_date, visible_only=False):
        rid = _report_id_from_summary_title(item.title)
        if not rid:
            continue
        p = Path(item.file_path).resolve()
        if not p.is_file():
            continue
        prev = best.get(rid)
        if prev is None or int(item.id) > prev[0]:
            best[rid] = (int(item.id), p)
    return {k: v[1] for k, v in best.items()}


def _load_pdf_graft_merge_module():
    import importlib.util

    try:
        root = _resolve_svodki_root()
    except ReportGenerationError:
        return None
    path = _resolve_svodki_support_file(root, "pdf_graft_merge_last_col.py")
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location("_pdf_graft_merge_last_col", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_report1_pdf_page_order_module():
    import importlib.util
    import sys

    sys.modules.pop("_report1_pdf_page_order", None)

    try:
        root = _resolve_svodki_root()
    except ReportGenerationError:
        return None
    path = _resolve_svodki_support_file(root, "report1_pdf_page_order.py")
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location("_report1_pdf_page_order", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_report1_sheet_order_module():
    import importlib.util
    import sys

    try:
        root = _resolve_svodki_root()
    except ReportGenerationError:
        return None
    path = _resolve_svodki_support_file(root, "report1_sheet_order.py")
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location("_report1_sheet_order", path)
    if spec is None or spec.loader is None:
        return None
    plan_dir = str(path.parent.resolve())
    path_added = False
    if plan_dir not in sys.path:
        sys.path.insert(0, plan_dir)
        path_added = True
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None
    finally:
        if path_added:
            try:
                sys.path.remove(plan_dir)
            except ValueError:
                pass


def _normalize_excel_sheet_titles_file(
    xlsx_path: Path,
    report_id: str,
    logs: list[str],
) -> bool:
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        logs.append(f"Сводка {report_id}: normalize sheet titles — {exc}")
        return False
    support = _resolve_svodki_support_file(Path(settings.svodki_root).expanduser(), "excel_sheet_titles.py")
    plan_dir = support.parent if support else Path(settings.svodki_root).expanduser() / "plan" / "utils" / "excel"
    if str(plan_dir) not in sys.path:
        sys.path.insert(0, str(plan_dir))
    try:
        from excel_sheet_titles import normalize_workbook_sheet_titles
    except Exception as exc:
        logs.append(f"Сводка {report_id}: excel_sheet_titles — {exc}")
        return False
    try:
        wb = load_workbook(xlsx_path, read_only=False, data_only=False, keep_vba=True)
        try:
            changes = normalize_workbook_sheet_titles(wb, report_id)
            if changes:
                wb.save(xlsx_path)
                logs.append(
                    f"Сводка {report_id}: переименованы листы Excel под PDF: "
                    + "; ".join(changes[:5])
                    + ("…" if len(changes) > 5 else "")
                )
                return True
        finally:
            wb.close()
    except Exception as exc:
        logs.append(f"Сводка {report_id}: ошибка normalize sheet titles — {exc}")
    return False


def _reorder_report1_workbook_file(
    xlsx_path: Path,
    logs: list[str],
    *,
    report_label: str = "1",
) -> bool:
    """Переставляет листы Excel сводки 1 в порядок эталона «ПРИМЕР.pdf»."""
    if not xlsx_path.is_file():
        return False
    try:
        mod = _load_report1_sheet_order_module()
    except Exception as exc:
        logs.append(f"Сводка {report_label}: модуль порядка листов — {exc}")
        return False
    if mod is None:
        logs.append(
            f"Сводка {report_label}: report1_sheet_order.py не загружен — порядок листов не изменён."
        )
        return False
    reorder_fn = getattr(mod, "reorder_report1_workbook_sheets", None)
    if not callable(reorder_fn):
        return False
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        logs.append(f"Сводка {report_label}: перестановка листов Excel — {exc}")
        return False
    try:
        wb = load_workbook(xlsx_path, read_only=False, data_only=False, keep_vba=True)
        try:
            changed = bool(reorder_fn(wb))
            if changed:
                wb.save(xlsx_path)
                logs.append(
                    f"Сводка {report_label}: порядок листов Excel переставлен "
                    f"по эталону «1.СВОДКА ХК Крупно ПРИМЕР.pdf»."
                )
            return changed
        finally:
            wb.close()
    except Exception as exc:
        logs.append(f"Сводка {report_label}: ошибка перестановки листов Excel — {exc}")
        return False


def _apply_report1_saved_page_order(
    *,
    created: list[Summary],
    logs: list[str],
    db: Session | None = None,
    summary_date: date | None = None,
) -> None:
    """Применяет сохранённый пользователем порядок страниц PDF для всех сводок."""
    try:
        from .report1_page_order_editor import apply_saved_page_order_to_pdf
    except Exception as exc:
        logs.append(f"Пользовательский порядок страниц — {exc}")
        return
    donor_pdfs: dict[str, Path] = {}
    if db is not None and summary_date is not None:
        donor_pdfs.update(_latest_pdf_paths_by_report_id(db, summary_date))
    for item in created:
        rid = _report_id_from_summary_title(item.title)
        if not rid:
            continue
        p = Path(item.file_path).resolve()
        if p.is_file() and p.suffix.lower() == ".pdf":
            donor_pdfs[rid] = p
    for item in created:
        rid = _report_id_from_summary_title(item.title)
        if not rid:
            continue
        pdf_path = Path(item.file_path).resolve()
        if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
            continue
        apply_saved_page_order_to_pdf(
            pdf_path,
            logs,
            report_id=rid,
            report_label=rid,
            donor_pdfs=donor_pdfs,
            summary_id=int(item.id),
            refresh_base=True,
        )


def _apply_saved_cell_edits_after_generation(
    *,
    created: list[Summary],
    logs: list[str],
) -> None:
    """Перенести/применить сохранённые правки ячеек к свежесгенерированным PDF."""
    try:
        from .cell_edits_store import (
            adopt_cell_edits_for_summary,
            get_cell_edit_pdf_base_path,
            list_summary_cell_edits,
            load_cell_edits_store,
            save_cell_edits_store,
        )
        from .pdf_cell_edits import apply_cell_edits_to_pdf
    except Exception as exc:
        logs.append(f"Пользовательские правки ячеек — {exc}")
        return

    store = load_cell_edits_store()
    changed = False
    for item in created:
        rid = _report_id_from_summary_title(item.title)
        if not rid:
            continue
        pdf_path = Path(item.file_path).resolve()
        if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
            continue
        edits = adopt_cell_edits_for_summary(
            store,
            int(item.id),
            report_id=rid,
            base_pdf_path=str(pdf_path),
        )
        if not edits:
            continue
        try:
            # Свежий PDF генерации = новая база без прошлых оверлеев.
            cell_base = get_cell_edit_pdf_base_path(
                store,
                int(item.id),
                pdf_path,
                report_id=rid,
                refresh=True,
            )
            apply_cell_edits_to_pdf(
                target_pdf_path=pdf_path,
                base_pdf_path=cell_base,
                edits=edits,
            )
            changed = True
            logs.append(
                f"Сводка {rid}: применены пользовательские правки ячеек ({len(edits)})."
            )
        except Exception as exc:
            logs.append(f"Сводка {rid}: ошибка правок ячеек — {exc}")
    if changed:
        save_cell_edits_store(store)


def _finalize_report1_pdf_page_order(
    *,
    created: list[Summary],
    temp_root: Path,
    logs: list[str],
    logo_root: Path | None = None,
    convert_fn=None,
) -> None:
    """Финальная перестановка страниц PDF сводки 1 (эталон «ПРИМЕР.pdf»)."""
    page_order_mod = _load_report1_pdf_page_order_module()
    if page_order_mod is None:
        return
    reorder_pdf = getattr(page_order_mod, "reorder_report1_pdf", None)
    if not callable(reorder_pdf):
        return
    snap = temp_root / "_xlsx_snap"
    for master_id in ("1", "1.18"):
        item = next(
            (c for c in created if _report_id_from_summary_title(c.title) == master_id),
            None,
        )
        if item is None:
            continue
        pdf_path = Path(item.file_path).resolve()
        if not pdf_path.is_file():
            continue
        xlsx_path = snap / f"{master_id}.xlsx"
        if xlsx_path.is_file():
            _reorder_report1_workbook_file(xlsx_path, logs, report_label=master_id)
        if xlsx_path.is_file() and logo_root is not None and convert_fn is not None:
            fill_mod = _load_report1_xlsx_slave_fill_module()
            regen = (
                getattr(fill_mod, "regenerate_report1_pdf_from_xlsx", None)
                if fill_mod is not None
                else None
            )
            regen_ok = False
            if callable(regen):
                regen_ok = regen(
                    xlsx_path,
                    pdf_destination=pdf_path,
                    logo_root=logo_root,
                    convert_fn=convert_fn,
                    logs=logs,
                    report_label=master_id,
                    summary_date=item.summary_date,
                )
                if regen_ok:
                    logs.append(
                        f"Сводка {master_id}: PDF пересобран из финального XLSX "
                        f"(LibreOffice, порядок листов = эталон; без permute страниц)."
                    )
            if (
                not regen_ok
                and callable(reorder_pdf)
                and xlsx_path.is_file()
            ):
                logs.append(
                    f"Сводка {master_id}: пересборка PDF из XLSX не удалась — "
                    "запасной вариант: перестановка страниц PDF."
                )
                reorder_pdf(pdf_path, xlsx_path, logs, report_label=master_id)


def _restamp_report1_pdf_headers(
    *,
    created: list[Summary],
    temp_root: Path,
    logs: list[str],
    logo_root: Path,
) -> None:
    """
    Пересчитывает шапки PDF сводки 1 после ensure/reorder и PDF-подстановки.

    LibreOffice и вставка страниц посева штампуют шапки по промежуточному порядку
    листов; после перестановки и подстановки намолота/овощей из сводки 10 нужен
    финальный штамп по фактическому числу страниц.
    """
    if _env_flag("SVODKI_REPORT1_SKIP_FINAL_HEADER_STAMP", False):
        logs.append(
            "Сводка 1: финальный штамп шапок отключён (SVODKI_REPORT1_SKIP_FINAL_HEADER_STAMP=1)."
        )
        return
    snap = temp_root / "_xlsx_snap"
    if not snap.is_dir():
        return
    page_order_mod = _load_report1_pdf_page_order_module()
    prepare_hdr = (
        getattr(page_order_mod, "prepare_header_workbook", None)
        if page_order_mod is not None
        else None
    )
    for master_id in ("1", "1.18"):
        item = next(
            (c for c in created if _report_id_from_summary_title(c.title) == master_id),
            None,
        )
        if item is None:
            continue
        pdf_path = Path(item.file_path).resolve()
        xlsx_path = snap / f"{master_id}.xlsx"
        if not pdf_path.is_file() or not xlsx_path.is_file():
            continue
        header_src = xlsx_path
        hdr_tmp: Path | None = None
        if callable(prepare_hdr):
            try:
                hdr_tmp = snap / f"{master_id}_hdr_{uuid.uuid4().hex}.xlsx"
                prepare_hdr(xlsx_path, hdr_tmp)
                header_src = hdr_tmp
            except Exception as exc:
                logs.append(
                    f"Сводка {master_id}: подготовка XLSX для штампа шапок — {exc}"
                )
                header_src = xlsx_path
        ok = _stamp_pdf_header(
            pdf_path,
            xlsx_path,
            logo_root,
            header_src=header_src,
            summary_date=item.summary_date,
            report_id=master_id,
        )
        if hdr_tmp is not None:
            hdr_tmp.unlink(missing_ok=True)
        if ok:
            try:
                from pypdf import PdfReader

                n_pg = len(PdfReader(str(pdf_path)).pages)
            except Exception:
                n_pg = None
            logs.append(
                f"Сводка {master_id}: шапки PDF пересчитаны по финальной раскладке"
                + (f" ({n_pg} стр.)." if n_pg is not None else ".")
            )
        else:
            logs.append(
                f"Сводка {master_id}: не удалось пересчитать шапки PDF после финальной раскладки."
            )


def _report1_own_calc_sheet_titles() -> tuple[str, ...]:
    fill_mod = _load_report1_xlsx_slave_fill_module()
    if fill_mod is None:
        return ()
    return tuple(getattr(fill_mod, "REPORT1_OWN_CALC_SHEETS", ()) or ())


def _try_restore_report1_own_calc_pdf(
    *,
    final_pdf: Path,
    initial_pdf: Path,
    initial_xlsx: Path,
    final_xlsx: Path,
    logs: list[str],
    report_label: str,
    stage: str = "",
) -> bool:
    page_order_mod = _load_report1_pdf_page_order_module()
    restore_fn = (
        getattr(page_order_mod, "restore_own_calc_pdf_pages", None)
        if page_order_mod is not None
        else None
    )
    sheet_titles = _report1_own_calc_sheet_titles()
    if not callable(restore_fn) or not sheet_titles:
        if stage:
            logs.append(
                f"Сводка {report_label}: восстановление PDF листов МУ ({stage}) — "
                "модуль restore недоступен."
            )
        return False
    missing: list[str] = []
    for p, label in (
        (final_pdf, "final_pdf"),
        (initial_pdf, "initial_pdf"),
        (initial_xlsx, "initial_xlsx"),
        (final_xlsx, "final_xlsx"),
    ):
        if not p.is_file():
            missing.append(label)
    if missing:
        logs.append(
            f"Сводка {report_label}: восстановление PDF листов МУ ({stage or 'этап'}) — "
            f"нет файлов: {', '.join(missing)}."
        )
        return False
    ok = restore_fn(
        final_pdf,
        initial_pdf,
        initial_xlsx,
        final_xlsx,
        sheet_titles,
        logs=logs,
        report_label=report_label,
    )
    if ok and stage:
        logs.append(f"Сводка {report_label}: восстановление PDF листов МУ ({stage}).")
    return ok


def _restore_report1_own_calc_pdf_pages_batch(
    *,
    created: list[Summary],
    temp_root: Path,
    logs: list[str],
    cleanup: bool = True,
    stage: str = "финал после graft/restamp",
) -> bool:
    """Восстанавливает PDF-страницы 1.1 МУ из снимка до Excel-postprocess."""
    sheet_titles = _report1_own_calc_sheet_titles()
    if not sheet_titles:
        return False
    snap = temp_root / "_xlsx_snap"
    if not snap.is_dir():
        return False
    any_restored = False
    for master_id in ("1", "1.18"):
        item = next(
            (c for c in created if _report_id_from_summary_title(c.title) == master_id),
            None,
        )
        if item is None:
            continue
        final_pdf = Path(item.file_path).resolve()
        paths = _report1_script_snapshot_paths(snap, master_id)
        initial_pdf = paths["initial_pdf"]
        initial_xlsx = _report1_initial_xlsx_for_restore(snap, master_id)
        final_xlsx = paths["final_xlsx"]
        if _try_restore_report1_own_calc_pdf(
            final_pdf=final_pdf,
            initial_pdf=initial_pdf,
            initial_xlsx=initial_xlsx,
            final_xlsx=final_xlsx,
            logs=logs,
            report_label=master_id,
            stage=stage,
        ):
            any_restored = True
        if cleanup:
            paths["pristine"].unlink(missing_ok=True)
            paths["script_xlsx"].unlink(missing_ok=True)
            initial_pdf.unlink(missing_ok=True)
    return any_restored


def _ensure_report1_seeding_pages_batch(
    *,
    created: list[Summary],
    temp_root: Path,
    logs: list[str],
    logo_root: Path,
    convert_fn,
) -> bool:
    """Повторно восстанавливает «4.0 Посев» и «Итого (все АФ)», если PDF короче Excel."""
    page_order_mod = _load_report1_pdf_page_order_module()
    ensure_fn = (
        getattr(page_order_mod, "ensure_report1_seeding_pages_in_pdf", None)
        if page_order_mod is not None
        else None
    )
    if not callable(ensure_fn):
        return False
    snap = temp_root / "_xlsx_snap"
    if not snap.is_dir():
        return False
    any_fixed = False
    for master_id in ("1", "1.18"):
        item = next(
            (c for c in created if _report_id_from_summary_title(c.title) == master_id),
            None,
        )
        if item is None:
            continue
        pdf_path = Path(item.file_path).resolve()
        xlsx_path = snap / f"{master_id}.xlsx"
        if not pdf_path.is_file() or not xlsx_path.is_file():
            continue
        if ensure_fn(
            pdf_path,
            xlsx_path,
            logo_root=logo_root,
            convert_fn=convert_fn,
            logs=logs,
            report_label=master_id,
            summary_date=item.summary_date,
        ):
            any_fixed = True
            _restamp_report1_pdf_headers(
                created=created,
                temp_root=temp_root,
                logs=logs,
                logo_root=logo_root,
            )
    return any_fixed


def _load_report1_xlsx_slave_fill_module():
    import importlib.util
    import sys

    mod_name = "_report1_xlsx_slave_fill"
    sys.modules.pop(mod_name, None)
    try:
        root = _resolve_svodki_root()
    except ReportGenerationError:
        return None
    path = _resolve_svodki_support_file(root, "report1_xlsx_slave_fill.py")
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _report1_xlsx_slave_fill_active() -> bool:
    mod = _load_report1_xlsx_slave_fill_module()
    return mod is not None and mod.env_enabled()


def _apply_report1_xlsx_slave_postprocess(
    *,
    temp_root: Path,
    created: list[Summary],
    logo_root: Path,
    logs: list[str],
    replace_visible_summaries: bool = False,
    requested_report_ids: frozenset[str] | None = None,
    graft_spec: tuple[tuple[int, str, int], ...] | None = None,
) -> frozenset[str]:
    """
    Копирует показатели из «Сводка» отчётов 3,4,6,7,8 в листы «Выраб. — …» книги сводки 1
    и пересобирает PDF поверх уже сохранённого файла. Возвращает slave_id, для которых
    не нужна PDF-подстановка страниц.
    """
    mod = _load_report1_xlsx_slave_fill_module()
    if mod is None:
        return frozenset()
    if not mod.env_enabled():
        logs.append(
            "Сводка 1: XLSX-подстановка из отчётов 3–8 отключена (SVODKI_REPORT1_XLSX_SLAVE_FILL=0). "
            "Данные в таблицах «Выраб.» — только из расчёта сводки 1 и PDF-подстановки (если включена)."
        )
        return frozenset()
    snap = temp_root / "_xlsx_snap"
    if not snap.is_dir():
        return frozenset()
    exclude: set[str] = set()
    for master_id in ("1", "1.18"):
        src_master = snap / f"{master_id}.xlsx"
        if not src_master.is_file():
            continue
        item = next(
            (c for c in created if _report_id_from_summary_title(c.title) == master_id),
            None,
        )
        if item is None:
            continue
        if _summary_is_published(item) and _keep_published_summary(
            master_id,
            replace_visible_summaries=replace_visible_summaries,
            requested_report_ids=requested_report_ids or frozenset(),
        ):
            logs.append(
                f"Сводка {master_id}: Excel-подстановка пропущена — сводка уже опубликована "
                f"(id={item.id}), файл не изменяется."
            )
            continue
        dest_pdf = Path(item.file_path).resolve()
        if not dest_pdf.is_file():
            continue
        paths = _report1_script_snapshot_paths(snap, master_id)
        initial_pdf = paths["initial_pdf"]
        if not initial_pdf.is_file():
            shutil.copy2(dest_pdf, initial_pdf)
        # Имя с осмысленным stem (1 / 1.18), иначе штамп PDF берёт «._merge_…uuid» в заголовок.
        work = snap / f"{src_master.stem}_{uuid.uuid4().hex}_fill.xlsx"
        pristine = paths["pristine"]
        if paths["script_xlsx"].is_file():
            shutil.copy2(paths["script_xlsx"], pristine)
        elif not pristine.is_file():
            shutil.copy2(src_master, pristine)
        shutil.copy2(src_master, work)

        skip_recalc = _env_flag("SVODKI_SKIP_DONOR_XLSX_RECALC", True)

        def _recalc_donor(donor_path: Path) -> bool:
            if skip_recalc:
                return True
            return bool(
                mod.recalc_workbook_via_libreoffice(
                    donor_path,
                    logs=logs,
                    ctx=f"XLSX сводка {master_id}",
                )
            )

        fill_fn = getattr(mod, "apply_slave_fills_with_retry", None)
        effective_graft = graft_spec if graft_spec is not None else REPORT1_XLSX_SLAVE_GRAFT_SPEC
        if callable(fill_fn):
            filled_rows, skip_pdf_overlay = fill_fn(
                work,
                snap,
                graft_spec=effective_graft,
                logs=logs,
                report_label=master_id,
                recalc_fn=_recalc_donor,
            )
        else:
            filled_rows, skip_pdf_overlay = mod.apply_slave_fills_to_report1_workbook(
                work,
                snap,
                graft_spec=effective_graft,
                logs=logs,
                report_label=master_id,
            )
        preserve_fn = getattr(mod, "preserve_report1_own_calc_sheets", None)
        if callable(preserve_fn):
            preserve_fn(work, pristine, logs=logs, report_label=master_id)
        exclude |= set(skip_pdf_overlay)
        _reorder_report1_workbook_file(work, logs, report_label=master_id)
        if callable(preserve_fn):
            preserve_fn(work, pristine, logs=logs, report_label=master_id)
        shutil.copy2(work, src_master)
        work.unlink(missing_ok=True)
        if filled_rows:
            logs.append(
                f"Сводка {master_id}: данные из Excel доноров {', '.join(sorted(filled_rows))} "
                f"— XLSX обновлён; PDF будет пересобран на этапе finalize из XLSX "
                f"(PDF-слой отключён для: {', '.join(sorted(skip_pdf_overlay))})."
            )
        else:
            logs.append(
                f"Сводка {master_id}: Excel-подстановка из доноров не обновила строки — "
                "будет PDF-подстановка (целые страницы, без склейки столбца)."
            )
    return frozenset(exclude)


def _pdf_graft_replace_pages(
    master_pdf: Path,
    operations: list[tuple[int, Path, int]],
    *,
    merge_last_column_from_master: bool = False,
) -> tuple[bool, list[str]]:
    mod = _load_pdf_graft_merge_module()
    if mod is None:
        return False, ["не найден svodki/plan/utils/pdf/pdf_graft_merge_last_col.py"]
    return mod.replace_pages(
        master_pdf,
        operations,
        merge_last_column_from_master=merge_last_column_from_master,
    )


def _load_pdf_page_whiteout():
    import importlib.util

    try:
        root = _resolve_svodki_root()
    except ReportGenerationError:
        return None
    path = _resolve_svodki_support_file(root, "pdf_page_whiteout.py")
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location("_pdf_page_whiteout", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _apply_report1_page28_r5_veg_white_mask(
    pdf_master: Path,
    logs: list[str],
    *,
    page_no: int = 28,
) -> None:
    """Поверх страницы посева после graft — белые зоны (овощные блоки донора 5)."""
    if not _env_flag("SVODKI_REPORT1_PAGE28_WHITEOUT", True):
        return
    mod = _load_pdf_page_whiteout()
    if mod is None:
        logs.append("PDF: не найден pdf_page_whiteout.py в svodki/plan/utils/pdf — маска посева пропущена.")
        return
    rects = mod.parse_whiteout_rects_from_env()
    if rects is None:
        rects = list(mod.REPORT1_R5_PAGE28_DEFAULT_RECTS)
    if mod.apply_pdf_whiteout_normalized(pdf_master, [page_no], rects):
        logs.append(
            f"PDF (сводка 1): стр. {page_no} — белая маска поверх овощных таблиц (донор PDF отчёта 5)."
        )
    else:
        logs.append(
            f"PDF (сводка 1): маска стр. {page_no} не применена (мало страниц в PDF, сбой записи, "
            "или ни один слой не сработал; установите pymupdf и/или "
            "SVODKI_PDF_WHITEOUT_DEBUG=1 для traceback)."
        )


def _apply_batch_pdf_grafts(
    created: list[Summary],
    logs: list[str],
    *,
    db: Session | None = None,
    summary_date: date | None = None,
    report1_skip_pdf_slaves: frozenset[str] | None = None,
    temp_root: Path | None = None,
    replace_visible_summaries: bool = False,
    requested_report_ids: frozenset[str] | None = None,
    report1_graft_spec: tuple[tuple[int, str, int], ...] | None = None,
) -> None:
    """Подставляет страницы в PDF на диске. Донор: пачка `created` и при необходимости БД за ту же дату."""
    if _env_flag("SVODKI_DISABLE_PDF_PAGE_GRAFT", False):
        return
    by_id: dict[str, Path] = {}
    if db is not None and summary_date is not None:
        try:
            by_id = _latest_pdf_paths_by_report_id(db, summary_date)
        except Exception as exc:
            logs.append(f"PDF подстановка: не удалось прочитать сводки из БД ({exc})")
    for item in created:
        rid = _report_id_from_summary_title(item.title)
        if rid:
            p = Path(item.file_path).resolve()
            if p.is_file():
                by_id[rid] = p

    if by_id:
        logs.append("PDF подстановка: участвуют report_id " + ", ".join(sorted(by_id)))

    page28_whiteout_paths: list[tuple[Path, int]] = []
    xlsx_fill_active = _report1_xlsx_slave_fill_active()
    page_order_mod = _load_report1_pdf_page_order_module()
    fill_mod = _load_report1_xlsx_slave_fill_module()
    snap_dir = (temp_root / "_xlsx_snap") if temp_root is not None else None
    for master_id in ("1", "1.18"):
        pdf_master = by_id.get(master_id)
        if not pdf_master or not pdf_master.is_file():
            continue
        if db is not None and summary_date is not None:
            published_master = next(
                (
                    item
                    for item in list_summaries_by_date(db, summary_date, visible_only=True)
                    if _report_id_from_summary_title(item.title) == master_id
                ),
                None,
            )
            if published_master is not None and _keep_published_summary(
                master_id,
                replace_visible_summaries=replace_visible_summaries,
                requested_report_ids=requested_report_ids or frozenset(),
            ):
                logs.append(
                    f"PDF подстановка (сводка {master_id}): пропуск — сводка уже опубликована "
                    f"(id={published_master.id}), файл не изменяется."
                )
                continue
        graft_spec = report1_graft_spec or _report1_pdf_graft_operations_enabled()
        if (
            snap_dir is not None
            and page_order_mod is not None
            and fill_mod is not None
            and hasattr(page_order_mod, "resolve_graft_spec_pages")
            and hasattr(fill_mod, "resolve_master_sheet_for_slave")
        ):
            snap_xlsx = snap_dir / f"{master_id}.xlsx"
            if snap_xlsx.is_file():
                graft_spec = page_order_mod.resolve_graft_spec_pages(
                    snap_xlsx,
                    pdf_master,
                    graft_spec,
                    fill_mod.resolve_master_sheet_for_slave,
                    logs=logs,
                )
        resolved_graft: list[tuple[int, str, int]] = []
        for dst_p, slave_id, src_p in graft_spec:
            if (
                slave_id in {"10", "11"}
                and snap_dir is not None
                and page_order_mod is not None
                and hasattr(page_order_mod, "resolve_donor_sheet_first_pdf_page")
            ):
                sheet_name = {
                    ("10", 1): "Общий намолот",
                    ("10", 2): "Овощи",
                    ("11", 2): "Сводка овощи",
                }.get((slave_id, src_p))
                donor_pdf = by_id.get(slave_id)
                donor_xlsx = snap_dir / f"{slave_id}.xlsx"
                if sheet_name and donor_pdf and donor_pdf.is_file() and donor_xlsx.is_file():
                    found_src = page_order_mod.resolve_donor_sheet_first_pdf_page(
                        donor_xlsx,
                        donor_pdf,
                        sheet_name,
                    )
                    if found_src is not None:
                        if found_src != src_p:
                            logs.append(
                                f"PDF подстановка: отчёт {slave_id} «{sheet_name}» → "
                                f"стр.{found_src} донора (было {src_p})."
                            )
                        src_p = found_src
            resolved_graft.append((dst_p, slave_id, src_p))
        graft_spec = tuple(resolved_graft)
        ops_merge: list[tuple[int, Path, int]] = []
        ops_full_page: list[tuple[int, Path, int]] = []
        page5_graft_dst: int | None = None
        for dst_p, slave_id, src_p in graft_spec:
            if report1_skip_pdf_slaves and slave_id in report1_skip_pdf_slaves:
                continue
            if slave_id == "5":
                # Посев: на странице — одна таблица «Выраб. — 4.0 Посев» из Excel (блок КУЛЬТУРЫ донора 5).
                # PDF донора 5 содержит три таблицы на одной странице — не подставляем.
                logs.append(
                    f"PDF подстановка (сводка {master_id}): отчёт 5 — пропуск PDF-страницы "
                    f"(стр.{dst_p}: таблица посева из Excel)."
                )
                continue
            if slave_id == "3":
                # Внесение МУ: сводка 1 считает 1.1 сама; PDF/Excel донора 3 даёт другие итоги.
                logs.append(
                    f"PDF подстановка (сводка {master_id}): отчёт 3 — пропуск PDF-страницы "
                    f"(стр.{dst_p}: внесение МУ из расчёта сводки 1)."
                )
                continue
            donor = by_id.get(slave_id)
            if donor and donor.is_file():
                # Отчёт 3: число столбцов как у мастера; склейка правой полосы с мастером даёт
                # нули и визуально «две таблицы». Подставляем целую страницу донора.
                # Если Excel-подстановка не сработала (типично при первой пачке без кэша формул),
                # склейка столбца даёт наложение таблиц — подставляем целую страницу донора.
                if slave_id in {"3", "10", "11"} or xlsx_fill_active:
                    ops_full_page.append((dst_p, donor, src_p))
                else:
                    ops_merge.append((dst_p, donor, src_p))
            else:
                logs.append(
                    f"PDF подстановка (сводка {master_id}): нет PDF сводки {slave_id} за дату "
                    f"(нужна стр.{src_p} донора → стр.{dst_p} в сводке 1). Сгенерируйте {slave_id} "
                    "за тот же день или в одной пачке с 1."
                )
        if ops_full_page:
            ok, msgs = _pdf_graft_replace_pages(
                pdf_master,
                ops_full_page,
                merge_last_column_from_master=False,
            )
            logs.extend(f"PDF подстановка (сводка {master_id}): {m}" for m in msgs)
            if not ok:
                logs.append("PDF подстановка (сводка %s): ошибка — %s" % (master_id, "; ".join(msgs)))
        if ops_merge:
            ok, msgs = _pdf_graft_replace_pages(
                pdf_master,
                ops_merge,
                merge_last_column_from_master=True,
            )
            logs.extend(f"PDF подстановка (сводка {master_id}): {m}" for m in msgs)
            if not ok:
                logs.append("PDF подстановка (сводка %s): ошибка — %s" % (master_id, "; ".join(msgs)))
            elif page5_graft_dst is not None:
                page28_whiteout_paths.append((pdf_master, page5_graft_dst))

    pdf0 = by_id.get("0")
    pdf01 = by_id.get("0.1")
    if pdf0 and pdf01 and pdf0.is_file() and pdf01.is_file():
        try:
            veg_page = max(1, int(os.environ.get("SVODKI_STATUS_VPR_VEG_PDF_PAGE", "2")))
        except ValueError:
            veg_page = 2
        ok, msgs = _pdf_graft_replace_pages(
            pdf0,
            [(veg_page, pdf01, veg_page)],
            merge_last_column_from_master=False,
        )
        logs.extend(f"PDF подстановка (сводка 0 ← 0.1): {m}" for m in msgs)
        if not ok:
            logs.append("PDF подстановка (сводка 0): ошибка — " + "; ".join(msgs))

    for pdf_m, page_no in dict.fromkeys(page28_whiteout_paths):
        if pdf_m.is_file():
            _apply_report1_page28_r5_veg_white_mask(pdf_m, logs, page_no=page_no)


def _workbook_sheetnames(path: Path) -> list[str]:
    try:
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        names = [str(n or "").strip() for n in wb.sheetnames]
        wb.close()
        return names
    except Exception:
        return []



def _run_lo_convert_one(
    office_cmd: str,
    lo_profile_dir: Path,
    output_dir: Path,
    inp: Path,
    lo_timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    def _run_lo_convert(inp_path: Path) -> subprocess.CompletedProcess[str]:
        try:
            proc0 = subprocess.run(
                [
                    office_cmd,
                    "--headless",
                    f"-env:UserInstallation={lo_profile_dir.resolve().as_uri()}",
                    "--convert-to",
                    "pdf:calc_pdf_Export",
                    "--outdir",
                    str(output_dir),
                    str(inp_path),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=lo_timeout,
            )
        except subprocess.TimeoutExpired:
            raise ReportGenerationError(
                f"LibreOffice не завершил конвертацию за {int(lo_timeout) if lo_timeout else 0} с ({inp_path.name}). "
                "Увеличьте SVODKI_LIBREOFFICE_TIMEOUT_SEC."
            ) from None
        if proc0.returncode != 0:
            try:
                proc0 = subprocess.run(
                    [
                        office_cmd,
                        "--headless",
                        f"-env:UserInstallation={lo_profile_dir.resolve().as_uri()}",
                        "--convert-to",
                        "pdf",
                        "--outdir",
                        str(output_dir),
                        str(inp_path),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=lo_timeout,
                )
            except subprocess.TimeoutExpired:
                raise ReportGenerationError(
                    f"LibreOffice (fallback pdf) превысил {int(lo_timeout) if lo_timeout else 0} с ({inp_path.name})."
                ) from None
        return proc0

    return _run_lo_convert(inp)


def _convert_report9_pdf_by_sheets(
    source_for_pdf: Path,
    output_dir: Path,
    out_pdf: Path,
    *,
    office_cmd: str,
    lo_profile_dir: Path,
    lo_timeout: float | None,
) -> None:
    """Сводка 9: каждый лист Excel → отдельный PDF → склейка (сводка, культуры «Вспашка …», затем АФ)."""
    from openpyxl import load_workbook
    from pypdf import PdfReader, PdfWriter

    sheet_names = _workbook_sheetnames(source_for_pdf)
    if not sheet_names:
        raise ReportGenerationError(f"Сводка 9: не удалось прочитать листы {source_for_pdf.name}")

    tmp_dir = output_dir / f".svodki_r9_pdf_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    try:
        for idx in range(len(sheet_names)):
            wb = load_workbook(source_for_pdf, read_only=False, data_only=False, keep_vba=True)
            try:
                for j, name in enumerate(list(wb.sheetnames)):
                    if j != idx and name in wb.sheetnames:
                        wb.remove(wb[name])
                one_xlsx = tmp_dir / f"r9_sheet_{idx + 1}.xlsx"
                wb.save(one_xlsx)
            finally:
                wb.close()

            proc = _run_lo_convert_one(
                office_cmd, lo_profile_dir, tmp_dir, one_xlsx, lo_timeout,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                raise ReportGenerationError(
                    err or f"LibreOffice не создал PDF для листа {idx + 1} сводки 9 ({one_xlsx.name})"
                )
            one_pdf = tmp_dir / f"{one_xlsx.stem}.pdf"
            if not one_pdf.exists():
                raise ReportGenerationError(f"LibreOffice не записал PDF для листа {idx + 1} сводки 9")
            for page in PdfReader(str(one_pdf)).pages:
                writer.add_page(page)

        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        with open(out_pdf, "wb") as f:
            writer.write(f)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _convert_xlsx_to_pdf(
    src: Path,
    output_dir: Path,
    logo_root: Path,
    *,
    summary_date: date | None = None,
) -> Path:
    office_cmd = shutil.which("libreoffice") or shutil.which("soffice")
    if not office_cmd:
        raise ReportGenerationError("Не найден LibreOffice/soffice для конвертации в PDF.")

    tmp_for_pdf: Path | None = None
    lo_tmp_dir: Path | None = None
    lo_profile_dir: Path | None = None
    _get_pdf_convert_semaphore().acquire()
    try:
        lo_profile_dir = output_dir / f".lo_profile_{uuid.uuid4().hex}"
        lo_profile_dir.mkdir(parents=True, exist_ok=True)
        report_id = _extract_report_id_from_stem(src.stem)
        expects_landscape = report_id not in {
            "0",
            "0.1",
            "22",
            "23",
            "25",
            "26",
            "27",
            "27.1",
        }
        source_for_pdf = src
        if src.suffix.lower() == ".xlsx" and report_id not in _FAST_PDF_PREP_SKIP_IDS:
            if report_id == "22":
                tmp_for_pdf = _prepare_xlsx_portrait_only(src, output_dir)
            else:
                tmp_for_pdf = _prepare_xlsx_for_pdf(src, output_dir)
                if not tmp_for_pdf and expects_landscape:
                    tmp_for_pdf = _prepare_xlsx_landscape_only(src, output_dir)
            if tmp_for_pdf:
                source_for_pdf = tmp_for_pdf

        out_pdf = output_dir / f"{_pdf_stem_with_prev_day(src.stem)}.pdf"
        lo_timeout = _libreoffice_run_timeout()
        r9_sheetnames = _workbook_sheetnames(source_for_pdf)
        is_report9_pdf = _is_report9_book(
            report_id, source_for_pdf.stem, r9_sheetnames,
        )

        if is_report9_pdf:
            _convert_report9_pdf_by_sheets(
                source_for_pdf,
                output_dir,
                out_pdf,
                office_cmd=office_cmd,
                lo_profile_dir=lo_profile_dir,
                lo_timeout=lo_timeout,
            )
        else:
            proc = _run_lo_convert_one(
                office_cmd, lo_profile_dir, output_dir, source_for_pdf, lo_timeout,
            )
            if proc.returncode != 0 and source_for_pdf != src and src.exists():
                err_text = (proc.stderr or proc.stdout or "").lower()
                if "source file could not be loaded" in err_text:
                    proc = _run_lo_convert_one(
                        office_cmd, lo_profile_dir, output_dir, src, lo_timeout,
                    )

            if proc.returncode != 0:
                err_text = (proc.stderr or proc.stdout or "").lower()
                if "source file could not be loaded" in err_text and src.exists():
                    lo_tmp_dir = output_dir / f".svodki_lo_{uuid.uuid4().hex}"
                    lo_tmp_dir.mkdir(parents=True, exist_ok=True)
                    safe_src = lo_tmp_dir / f"source{src.suffix.lower()}"
                    shutil.copy2(src, safe_src)
                    proc = _run_lo_convert_one(
                        office_cmd, lo_profile_dir, output_dir, safe_src, lo_timeout,
                    )
                    tmp_pdf = output_dir / f"{safe_src.stem}.pdf"
                    if proc.returncode == 0 and tmp_pdf.exists():
                        out_pdf.unlink(missing_ok=True)
                        tmp_pdf.replace(out_pdf)

            if not out_pdf.exists():
                produced_pdf = output_dir / f"{source_for_pdf.stem}.pdf"
                if produced_pdf.exists():
                    out_pdf.unlink(missing_ok=True)
                    produced_pdf.replace(out_pdf)

            if proc.returncode != 0 or not out_pdf.exists():
                err = (proc.stderr or proc.stdout or "").strip()
                raise ReportGenerationError(err or f"LibreOffice не создал PDF для {src.name}")

        if not out_pdf.exists():
            raise ReportGenerationError(f"LibreOffice не создал PDF для {src.name}")

        # Сводка 3/4/…: если LO внезапно дал portrait — таблица режется по ширине
        # на десятки страниц. Пересобираем с минимальным landscape-prep.
        if (
            expects_landscape
            and src.suffix.lower() == ".xlsx"
            and not is_report9_pdf
            and not _pdf_page_is_landscape(out_pdf)
        ):
            land_tmp = _prepare_xlsx_landscape_only(src, output_dir)
            if land_tmp is not None:
                if tmp_for_pdf and tmp_for_pdf.exists() and tmp_for_pdf != land_tmp:
                    tmp_for_pdf.unlink(missing_ok=True)
                tmp_for_pdf = land_tmp
                source_for_pdf = land_tmp
                out_pdf.unlink(missing_ok=True)
                proc = _run_lo_convert_one(
                    office_cmd, lo_profile_dir, output_dir, source_for_pdf, lo_timeout,
                )
                if not out_pdf.exists():
                    produced_pdf = output_dir / f"{source_for_pdf.stem}.pdf"
                    if produced_pdf.exists():
                        produced_pdf.replace(out_pdf)
                if proc.returncode != 0 or not out_pdf.exists():
                    err = (proc.stderr or proc.stdout or "").strip()
                    raise ReportGenerationError(
                        err or f"LibreOffice не создал PDF (landscape retry) для {src.name}"
                    )

        if not _stamp_pdf_header(
            out_pdf,
            src,
            logo_root,
            header_src=source_for_pdf,
            summary_date=summary_date,
            report_id=report_id,
        ):
            raise ReportGenerationError(f"Не удалось добавить шапку в PDF: {src.name}")
        return out_pdf
    finally:
        if tmp_for_pdf and tmp_for_pdf.exists():
            tmp_for_pdf.unlink(missing_ok=True)
        if lo_tmp_dir and lo_tmp_dir.exists():
            shutil.rmtree(lo_tmp_dir, ignore_errors=True)
        if lo_profile_dir and lo_profile_dir.exists():
            shutil.rmtree(lo_profile_dir, ignore_errors=True)
        _get_pdf_convert_semaphore().release()


def _safe_delete_top_rows(ws, count: int) -> None:
    """delete_rows без правки merge ломает таблицы (в PDF остаётся колонка «№»)."""
    if count <= 0:
        return
    # Снимаем все merge, которые пересекают удаляемые строки или сдвинутся.
    saved: list[tuple[int, int, int, int]] = []
    for merged in list(ws.merged_cells.ranges):
        min_r, min_c, max_r, max_c = (
            merged.min_row,
            merged.min_col,
            merged.max_row,
            merged.max_col,
        )
        try:
            ws.unmerge_cells(str(merged))
        except Exception:
            continue
        if max_r <= count:
            continue  # полностью удалён
        if min_r <= count:
            # частично в зоне удаления — оставляем только хвост ниже
            min_r = count + 1
        min_r -= count
        max_r -= count
        if min_r >= 1 and max_r >= min_r:
            saved.append((min_r, min_c, max_r, max_c))
    ws.delete_rows(1, count)
    for min_r, min_c, max_r, max_c in saved:
        try:
            ws.merge_cells(
                start_row=min_r,
                start_column=min_c,
                end_row=max_r,
                end_column=max_c,
            )
        except Exception:
            pass


def _strip_harvest_excel_banner_rows(ws) -> None:
    """Убирает строку «УБОРКА: …» / сводный заголовок над таблицей (дубль штампа PDF)."""
    table_markers = ("№ п/п", "Культура", "Агрофирма", "Агрофирмы")
    table_row: int | None = None
    for row_idx in range(1, min(12, ws.max_row + 1)):
        val = ws.cell(row_idx, 1).value
        if isinstance(val, str) and val.strip() in table_markers:
            table_row = row_idx
            break
    if table_row is None or table_row <= 1:
        return
    a1 = ws.cell(1, 1).value
    if not isinstance(a1, str):
        return
    head = a1.strip().upper()
    if not any(
        token in head
        for token in ("УБОРКА", "НАМОЛОТ", "ЗЕРНОВЫЕ", "ОВОЩНЫЕ", "ОВОЩНЫХ", "ОБЩИЙ", "ВЫРАБОТКА", "ВИД РАБОТ")
    ):
        return
    _safe_delete_top_rows(ws, table_row - 1)


def _prepare_xlsx_for_pdf(src: Path, output_dir: Path) -> Path | None:
    try:
        from copy import copy as _copy_style

        from openpyxl import load_workbook
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.properties import PageSetupProperties
    except Exception:
        return None

    tmp_path = output_dir / f".svodki_pdf_{uuid.uuid4().hex}{src.suffix.lower()}"
    try:
        shutil.copy2(src, tmp_path)
        wb = load_workbook(tmp_path, read_only=False, data_only=False, keep_vba=True)
        stem = src.stem.strip()
        if re.match(r"^0\.1(?:\s|$)", stem):
            report_id = "0.1"
        else:
            report_id = _extract_report_id_from_stem(stem)
        sheet_titles = [str(getattr(ws, "title", "") or "").strip() for ws in wb.worksheets]
        is_report9_book = _is_report9_book(report_id, stem, sheet_titles)
        page_orientation = (
            "portrait"
            if report_id in {"0", "0.1", "22", "23", "25", "26", "27", "27.1"}
            else "landscape"
        )

        def _auto_fit_wrapped_row_heights(ws) -> None:
            merged_widths: dict[tuple[int, int], float] = {}
            for merged in ws.merged_cells.ranges:
                width = 0.0
                for col_idx in range(merged.min_col, merged.max_col + 1):
                    col_letter = get_column_letter(col_idx)
                    width += float(ws.column_dimensions[col_letter].width or 10)
                merged_widths[(merged.min_row, merged.min_col)] = max(width, 1.0)

            for row_idx in range(1, ws.max_row + 1):
                required_height = float(ws.row_dimensions[row_idx].height or 15)
                for col_idx in range(1, ws.max_column + 1):
                    cell = ws.cell(row_idx, col_idx)
                    value = cell.value
                    if value in (None, ""):
                        continue
                    text = str(value)
                    wrap_enabled = bool(getattr(cell.alignment, "wrap_text", False))
                    if "\n" not in text and not wrap_enabled:
                        continue

                    width = merged_widths.get((row_idx, col_idx))
                    if width is None:
                        col_letter = get_column_letter(col_idx)
                        width = float(ws.column_dimensions[col_letter].width or 10)
                    chars_per_line = max(1, int(width * 1.08))

                    lines = 0
                    for part in text.splitlines() or [text]:
                        part_len = len(part.strip())
                        lines += max(1, math.ceil(part_len / chars_per_line))
                    if lines <= 1:
                        continue

                    font_size = float(cell.font.sz or 11)
                    required_height = max(required_height, min(220.0, lines * font_size * 1.35 + 6))

                ws.row_dimensions[row_idx].height = required_height

        def _remove_report2_work_type_column_for_pdf(ws) -> None:
            """Keep the XLSX intact, but hide the redundant work-type column in report 2 PDFs."""
            header_rows: list[int] = []
            for row_idx in range(1, ws.max_row + 1):
                value = ws.cell(row_idx, 4).value
                if isinstance(value, str) and value.strip() == "Вид работ":
                    header_rows.append(row_idx)

            for header_row in header_rows:
                end_row = ws.max_row + 1
                for row_idx in range(header_row + 1, ws.max_row + 1):
                    value = ws.cell(row_idx, 1).value
                    if isinstance(value, str) and value.strip().upper() == "СОВОКУПНАЯ ПЛОЩАДЬ ПО АГРОФИРМАМ":
                        end_row = row_idx
                        break

                merged_title_rows: set[int] = set()
                for merged in list(ws.merged_cells.ranges):
                    if (
                        header_row < merged.min_row < end_row
                        and merged.min_row == merged.max_row
                        and merged.min_col == 1
                        and merged.max_col == 7
                    ):
                        row_idx = merged.min_row
                        ws.unmerge_cells(
                            start_row=merged.min_row,
                            start_column=merged.min_col,
                            end_row=merged.max_row,
                            end_column=merged.max_col,
                        )
                        ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=6)
                        merged_title_rows.add(row_idx)

                for row_idx in range(header_row, end_row):
                    if row_idx in merged_title_rows:
                        continue
                    for col_idx in range(4, ws.max_column):
                        src_cell = ws.cell(row_idx, col_idx + 1)
                        dst_cell = ws.cell(row_idx, col_idx)
                        try:
                            dst_cell.value = src_cell.value
                            if src_cell.has_style:
                                dst_cell.font = _copy_style(src_cell.font)
                                dst_cell.fill = _copy_style(src_cell.fill)
                                dst_cell.border = _copy_style(src_cell.border)
                                dst_cell.alignment = _copy_style(src_cell.alignment)
                                dst_cell.number_format = src_cell.number_format
                                dst_cell.protection = _copy_style(src_cell.protection)
                        except Exception:
                            pass
                    try:
                        ws.cell(row_idx, ws.max_column).value = None
                    except Exception:
                        pass

        disable_report2_column_shift = str(os.environ.get("SVODKI_DISABLE_REPORT2_PDF_SHIFT", "1")).strip().lower() not in {"0", "false", "no"}
        for ws in wb.worksheets:
            custom_print_area: str | None = None
            # Не вырезаем «ВЫРАБОТКА…/ВИД РАБОТ» из листов 3–9: эталон (сводка 3 за 29.07)
            # как раз с этими строками под штампом. delete_rows по merge ломал таблицы
            # (в PDF оставалась одна колонка «№»).
            if report_id in {"10", "12", "13", "14"}:
                try:
                    _strip_harvest_excel_banner_rows(ws)
                except Exception:
                    pass
            if report_id == "2" and not disable_report2_column_shift:
                try:
                    _remove_report2_work_type_column_for_pdf(ws)
                except Exception:
                    pass
            # Для 7-й сводки (дискование) перенос блока "НАЁМНЫЕ" вправо
            # в PDF-подготовке даёт артефакты (пустая/съехавшая таблица
            # у отдельных фирм, в частности при пустом блоке "СОБСТВЕННЫЕ").
            # Оставляем для неё исходную разметку листа.
            # Горизонтальная склейка: для 6-й сводки отключаем — как в сводке 3, вертикальные
            # разрывы и row breaks; иначе оценка страниц даёт 1 стр./лист и штамп PDF путает фирмы.
            if report_id in {"4", "8", "9"}:
                try:
                    own_row = None
                    hire_row = None
                    for row_idx in range(1, ws.max_row + 1):
                        value = ws.cell(row_idx, 1).value
                        if isinstance(value, str):
                            title = value.strip().upper()
                            if title.startswith("СОБСТВЕННЫЕ") and own_row is None:
                                own_row = row_idx
                            elif (
                                title.startswith("НАЁМНЫЕ") or title.startswith("НАЕМНЫЕ")
                            ) and hire_row is None:
                                hire_row = row_idx
                    if own_row and hire_row and hire_row > own_row:
                        width = max(1, ws.max_column)
                        gap = 2
                        target_col = width + gap + 1
                        own_height = max(1, hire_row - own_row)
                        hire_height = max(1, ws.max_row - hire_row + 1)
                        for sr in range(hire_row, ws.max_row + 1):
                            tr = own_row + (sr - hire_row)
                            for sc in range(1, width + 1):
                                tc = target_col + (sc - 1)
                                s_cell = ws.cell(sr, sc)
                                d_cell = ws.cell(tr, tc)
                                d_cell.value = s_cell.value
                                if s_cell.has_style:
                                    d_cell.font = _copy_style(s_cell.font)
                                    d_cell.fill = _copy_style(s_cell.fill)
                                    d_cell.border = _copy_style(s_cell.border)
                                    d_cell.alignment = _copy_style(s_cell.alignment)
                                    d_cell.number_format = s_cell.number_format
                                    d_cell.protection = _copy_style(s_cell.protection)
                        for sc in range(1, width + 1):
                            src_letter = get_column_letter(sc)
                            dst_letter = get_column_letter(target_col + sc - 1)
                            src_w = ws.column_dimensions[src_letter].width
                            if src_w:
                                ws.column_dimensions[dst_letter].width = src_w
                        for merged in list(ws.merged_cells.ranges):
                            if merged.min_row >= hire_row:
                                ws.merge_cells(
                                    start_row=merged.min_row + (own_row - hire_row),
                                    start_column=merged.min_col + (target_col - 1),
                                    end_row=merged.max_row + (own_row - hire_row),
                                    end_column=merged.max_col + (target_col - 1),
                                )
                        sheet_max_row = ws.max_row
                        for merged in list(ws.merged_cells.ranges):
                            if (
                                merged.min_row >= hire_row
                                and merged.max_row <= sheet_max_row
                                and merged.min_col >= 1
                                and merged.max_col <= width
                            ):
                                try:
                                    ws.unmerge_cells(
                                        start_row=merged.min_row,
                                        start_column=merged.min_col,
                                        end_row=merged.max_row,
                                        end_column=merged.max_col,
                                    )
                                except Exception:
                                    pass
                        for sr in range(hire_row, sheet_max_row + 1):
                            for sc in range(1, width + 1):
                                cell = ws.cell(sr, sc)
                                cell.value = None
                        end_row = own_row + max(own_height, hire_height) - 1
                        print_start_row = own_row
                        for r in range(1, own_row):
                            v = ws.cell(r, 1).value
                            if isinstance(v, str) and "отработавшая больше 10 га" in v.casefold():
                                print_start_row = r
                                break
                        end_col = target_col + width - 1
                        custom_print_area = (
                            f"A{print_start_row}:{get_column_letter(end_col)}{end_row}"
                        )
                except Exception:
                    pass

            # Не трогаем высоты: для 5 «Посев» вёрстка и ширины задаются в скрипте; auto_fit портит PDF.
            # 1 / 1.18: после Excel-подстановки из доноров авто-высота строк может раздувать шапку таблицы
            # и сбивать разрывы страниц и текст штампа по фирмам.
            if report_id not in _REPORT1_FAMILY_IDS | {"3", "4", "5", "6", "7", "8", "9", "10", "12", "25"}:
                try:
                    _auto_fit_wrapped_row_heights(ws)
                except Exception:
                    pass

            if report_id == "25":
                # Сводка 25 — компактная таблица A4 книжной ориентации; настройки из скрипта.
                ws.page_setup.orientation = ws.ORIENTATION_PORTRAIT
                try:
                    ws.page_setup.paperSize = ws.PAPERSIZE_A4
                except Exception:
                    pass
                ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True, autoPageBreaks=True)
                ws.page_setup.fitToWidth = 1
                ws.page_setup.fitToHeight = 1
                ws.page_setup.scale = None
                ws.page_margins.left = 0.12
                ws.page_margins.right = 0.12
                ws.page_margins.top = 0.25
                ws.page_margins.bottom = 0.25
                ws.print_options.horizontalCentered = True
                continue

            ws.page_setup.orientation = page_orientation
            try:
                ws.page_setup.paperSize = ws.PAPERSIZE_A4
            except Exception:
                pass
            ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True, autoPageBreaks=True)
            ws.page_setup.fitToWidth = 2 if custom_print_area else 1
            # fitToHeight=1 заставляет Calc ужать лист до одной «логической» страницы по высоте
            # и конфликтует с нашими явными row breaks в сводках 3/6 → «размножаются»
            # шапки секций и «Итого» уезжает на отдельный лист. Оставляем естественный перенос.
            ws.page_setup.fitToHeight = 0
            ws.page_setup.scale = None
            ws.page_margins.left = 0.35
            ws.page_margins.right = 0.35
            ws.page_margins.top = 1.3
            ws.page_margins.bottom = 0.5
            ws.print_options.horizontalCentered = True
            try:
                # Сохраняем row breaks только для сводок 1/2/0/20 (сложная вёрстка).
                # Сводки 3–9: breaks из Excel рассчитаны под другие поля; в PDF дают
                # «размножение» шапок секций и лишние страницы — LO сам переносит таблицу.
                if report_id not in _REPORT1_FAMILY_IDS | {"0", "0.1", "2", "20"}:
                    ws.row_breaks.brk = []
                ws.col_breaks.brk = []
            except Exception:
                pass
            try:
                if custom_print_area:
                    ws.print_area = custom_print_area
            except Exception:
                pass

        wb.save(tmp_path)
        wb.close()
        return tmp_path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        return None


def _stamp_summary_box_date_label(src: Path, summary_date: date | None = None) -> str:
    """Дата для блока «Сводка за …» в штампе PDF."""
    if summary_date is not None:
        rid = _extract_report_id_from_stem(src.stem)
        if rid == "22":
            display = summary_date
        elif rid == "25":
            display = summary_date
        else:
            display = summary_date - timedelta(days=1)
        return display.strftime("%d.%m.%Y")

    m_ru = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", src.name)
    m_iso = re.search(r"(\d{4}-\d{2}-\d{2})", src.name)
    if m_ru:
        try:
            stamp_date = date(int(m_ru.group(3)), int(m_ru.group(2)), int(m_ru.group(1)))
            return stamp_date.strftime("%d.%m.%Y")
        except Exception:
            pass
    elif m_iso:
        try:
            stamp_date = date.fromisoformat(m_iso.group(1)) - timedelta(days=1)
            return stamp_date.strftime("%d.%m.%Y")
        except Exception:
            pass
    return (date.today() - timedelta(days=1)).strftime("%d.%m.%Y")


def _pdf_page_is_landscape(pdf_path: Path) -> bool:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        if not reader.pages:
            return True
        box = reader.pages[0].mediabox
        return float(box.width) + 0.5 >= float(box.height)
    except Exception:
        return True


def _prepare_xlsx_portrait_only(src: Path, output_dir: Path) -> Path | None:
    """Минимальный prep для сводки 22: portrait + fitToWidth=1 (вертикальные страницы PDF).

    Row breaks из text_svodka_2 (одна агрооперация = одна страница) сохраняем.
    """
    try:
        from openpyxl import load_workbook
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.properties import PageSetupProperties
    except Exception:
        return None
    tmp_path = output_dir / f".svodki_pdf_port_{uuid.uuid4().hex}{src.suffix.lower()}"
    try:
        shutil.copy2(src, tmp_path)
        wb = load_workbook(tmp_path, read_only=False, data_only=False, keep_vba=True)
        for ws in wb.worksheets:
            ws.page_setup.orientation = "portrait"
            try:
                ws.page_setup.paperSize = ws.PAPERSIZE_A4
            except Exception:
                pass
            ws.sheet_properties.pageSetUpPr = PageSetupProperties(
                fitToPage=True, autoPageBreaks=True
            )
            ws.page_setup.fitToWidth = 1
            ws.page_setup.fitToHeight = 0
            ws.page_setup.scale = None
            ws.page_margins.left = 0.25
            ws.page_margins.right = 0.25
            ws.page_margins.top = 1.05
            ws.page_margins.bottom = 0.25
            ws.print_options.horizontalCentered = True
            try:
                ws.col_breaks.brk = []
            except Exception:
                pass
            last_col = get_column_letter(ws.max_column)
            try:
                ws.print_area = f"A1:{last_col}{ws.max_row}"
            except Exception:
                pass
        wb.save(tmp_path)
        wb.close()
        return tmp_path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        return None


def _prepare_xlsx_landscape_only(src: Path, output_dir: Path) -> Path | None:
    """Минимальный prep: только landscape + fitToWidth=1 (если полный prep упал)."""
    try:
        from openpyxl import load_workbook
        from openpyxl.worksheet.properties import PageSetupProperties
    except Exception:
        return None
    tmp_path = output_dir / f".svodki_pdf_land_{uuid.uuid4().hex}{src.suffix.lower()}"
    try:
        shutil.copy2(src, tmp_path)
        wb = load_workbook(tmp_path, read_only=False, data_only=False, keep_vba=True)
        for ws in wb.worksheets:
            ws.page_setup.orientation = "landscape"
            try:
                ws.page_setup.paperSize = ws.PAPERSIZE_A4
            except Exception:
                pass
            ws.sheet_properties.pageSetUpPr = PageSetupProperties(
                fitToPage=True, autoPageBreaks=True
            )
            ws.page_setup.fitToWidth = 1
            ws.page_setup.fitToHeight = 0
            ws.page_setup.scale = None
            ws.page_margins.left = 0.35
            ws.page_margins.right = 0.35
            ws.page_margins.top = 1.3
            ws.page_margins.bottom = 0.5
            try:
                ws.col_breaks.brk = []
            except Exception:
                pass
        wb.save(tmp_path)
        wb.close()
        return tmp_path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        return None


def _pdf_page_looks_like_summary_table(page_text: str) -> bool:
    """Сводный лист 14: колонка «Агрофирмы»/«Агрофирма», не фраза «по агрофирмам» в штампе."""
    t = (page_text or "").casefold().replace("\u00a0", " ")
    if "собственные" in t or "наёмные" in t or "наемные" in t:
        return False
    # Им. п. мн.ч. — заголовок колонки сводки 14; не путать с «…по агрофирмам».
    if "агрофирмы" in t:
        return True
    # Сводка 3 и др.: колонка «Агрофирма» отдельной строкой.
    return bool(re.search(r"(^|\n)\s*агрофирма\s*(\n|$)", t))


def _pdf_page_looks_like_own_section_start(page_text: str) -> bool:
    """Первая страница листа фирмы/уборки: вверху секция СОБСТВЕННЫЕ."""
    lines = [ln.strip() for ln in (page_text or "").splitlines() if ln.strip()]
    body: list[str] = []
    for ln in lines:
        lcf = ln.casefold()
        if lcf.startswith("сводка за"):
            continue
        if lcf.startswith("ооо ") or lcf.startswith("ооо\""):
            continue
        if lcf.startswith("вид работ"):
            continue
        if lcf.startswith("выработка техники"):
            continue
        body.append(ln)
    head = "\n".join(body[:30]).casefold()
    return "собственные" in head


def _headers_for_report14_by_pdf_pages(
    reader,
    sheetnames: list[str],
    n_pages: int,
    default_header: tuple[str, str],
) -> list[tuple[str, str]] | None:
    """
    Сводка 14: шапки по фактическим страницам PDF.
    Новый лист = страница со сводной таблицей АФ либо со стартом «СОБСТВЕННЫЕ».
    Продолжение таблицы (без СОБСТВЕННЫЕ) сохраняет шапку текущей АФ.
    """
    if n_pages < 1 or not sheetnames:
        return None
    per_sheet = [_report14_pdf_sheet_headers(name) for name in sheetnames]
    if not per_sheet:
        return None
    try:
        page_texts = [str(page.extract_text() or "") for page in reader.pages[:n_pages]]
    except Exception:
        return None
    if len(page_texts) != n_pages:
        return None

    sheet_idx = 0
    out: list[tuple[str, str]] = []
    for i, text in enumerate(page_texts):
        if i > 0:
            advance = _pdf_page_looks_like_summary_table(text) or _pdf_page_looks_like_own_section_start(
                text
            )
            if advance and sheet_idx + 1 < len(per_sheet):
                sheet_idx += 1
        out.append(per_sheet[sheet_idx])

    # Если страниц меньше, чем листов (пустые листы без PDF) — всё равно ок,
    # если прошли все страницы. Если листов не хватило — не принимаем.
    if sheet_idx >= len(per_sheet):
        return None
    return out


def _stamp_pdf_header(
    pdf_path: Path,
    src: Path,
    logo_root: Path,
    header_src: Path | None = None,
    summary_date: date | None = None,
    report_id: str | None = None,
) -> bool:
    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.lib.colors import HexColor
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas
    except Exception:
        return False

    logo_path = logo_root / "chistopole.png"
    if not logo_path.exists():
        alt = logo_root / "logo.png"
        if alt.exists():
            logo_path = alt

    report_id = (report_id or "").strip() or _extract_report_id_from_stem(src.stem)
    d_str = _stamp_summary_box_date_label(src, summary_date)

    try:
        reader = PdfReader(str(pdf_path))
        n_pages = len(reader.pages)
        default_header = _resolve_pdf_header_text(src)
        headers_pp = _build_pdf_headers_per_page(
            src,
            n_pages,
            layout_src=header_src,
            report_id=report_id,
        )
        if report_id == "14":
            sheetnames_14: list[str] = []
            # Имена листов — из исходного xlsx (порядок АФ), не из prep-копии.
            for book_14 in (
                src if src.suffix.lower() == ".xlsx" else None,
                header_src if header_src and header_src.suffix.lower() == ".xlsx" else None,
            ):
                if book_14 is None:
                    continue
                try:
                    from openpyxl import load_workbook as _load_wb_14

                    _wb14 = _load_wb_14(book_14, read_only=True, data_only=True)
                    sheetnames_14 = list(_wb14.sheetnames)
                    _wb14.close()
                    if sheetnames_14:
                        break
                except Exception:
                    sheetnames_14 = []
            refined14 = _headers_for_report14_by_pdf_pages(
                reader, sheetnames_14, n_pages, default_header
            )
            if refined14:
                headers_pp = refined14
        if len(headers_pp) < n_pages:
            pad = headers_pp[-1] if headers_pp else default_header
            headers_pp = list(headers_pp) + [pad] * (n_pages - len(headers_pp))
        elif len(headers_pp) > n_pages:
            headers_pp = headers_pp[:n_pages]

        from app.legacy.pdf_header_labels import apply_pdf_header_label_overrides

        headers_pp = apply_pdf_header_label_overrides(headers_pp, report_id)

        writer = PdfWriter()
        font_name = "Helvetica-Bold"
        font_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        for font_path in font_candidates:
            if Path(font_path).exists():
                try:
                    pdfmetrics.registerFont(TTFont("SvodkiCyrBold", font_path))
                    font_name = "SvodkiCyrBold"
                    break
                except Exception:
                    pass

        prev_report1_header: tuple[str, str] | None = None
        report1_header_shift = 0
        prev_firm_header: tuple[str, str] | None = None
        firm_header_shift = 0
        report1_drop_pages = _parse_report1_drop_pages() if report_id in _REPORT1_FAMILY_IDS else set()
        kept_page_idx = -1
        for idx, page in enumerate(reader.pages):
            if report_id in {"25", "26", "27", "27.1"}:
                writer.add_page(page)
                continue
            if report_id in _REPORT1_FAMILY_IDS and (idx + 1) in report1_drop_pages:
                continue
            kept_page_idx += 1
            header_idx = kept_page_idx
            if report_id in _REPORT1_FAMILY_IDS:
                header_idx = max(0, min(len(headers_pp) - 1, kept_page_idx - report1_header_shift))
            elif report_id in _FIRM_CONTINUATION_REPORT_IDS:
                header_idx = max(0, min(len(headers_pp) - 1, kept_page_idx - firm_header_shift))
            header_t1 = " ".join(str(headers_pp[header_idx][0] or "").split())
            header_t2 = " ".join(str(headers_pp[header_idx][1] or "").split())
            if report_id in _REPORT1_FAMILY_IDS:
                try:
                    if (
                        prev_report1_header is not None
                        and (header_t1, header_t2) != prev_report1_header
                        and _is_report1_firm_header(header_t1)
                    ):
                        page_text = str(page.extract_text() or "")
                        # Меняем фирму только на странице-старте блока фирмы.
                        # Если таблица продолжается, держим шапку предыдущей фирмы
                        # и сдвигаем индекс шапок для последующих страниц.
                        if not _looks_like_report1_firm_start_page(page_text):
                            header_t1, header_t2 = prev_report1_header
                            report1_header_shift += 1
                    prev_report1_header = (header_t1, header_t2)
                except Exception:
                    pass
            elif report_id in _FIRM_CONTINUATION_REPORT_IDS:
                try:
                    if (
                        prev_firm_header is not None
                        and (header_t1, header_t2) != prev_firm_header
                        and _is_report1_firm_header(header_t1)
                    ):
                        page_text = str(page.extract_text() or "")
                        if not _looks_like_work_report_firm_start_page(page_text):
                            header_t1, header_t2 = prev_firm_header
                            firm_header_shift += 1
                    if _is_report1_firm_header(header_t1):
                        prev_firm_header = (header_t1, header_t2)
                except Exception:
                    pass
            w = float(page.mediabox.width)
            h = float(page.mediabox.height)

            packet = io.BytesIO()
            c = canvas.Canvas(packet, pagesize=(w, h))

            if report_id == "22":
                header_clear_h = 72
                c.setFillColor(HexColor("#FFFFFF"))
                c.rect(0, h - header_clear_h, w, header_clear_h, fill=1, stroke=0)

                logo_x = 18
                logo_w = 28
                logo_h = 28
                logo_y = h - 42
                if logo_path.exists():
                    try:
                        c.drawImage(
                            ImageReader(str(logo_path)),
                            logo_x,
                            logo_y,
                            width=logo_w,
                            height=logo_h,
                            mask="auto",
                        )
                    except Exception:
                        pass

                c.setFillColor(HexColor("#111827"))
                c.setFont(font_name, 9.5)
                try:
                    from zoneinfo import ZoneInfo

                    _msk = ZoneInfo("Europe/Moscow")
                    _now = datetime.now(_msk)
                except Exception:
                    _now = datetime.now()
                _snap = _now.replace(
                    minute=0, second=0, microsecond=0
                ).strftime("%H:%M %d.%m.%Y")
                c.drawCentredString(w / 2, h - 36, f"Сводка на {_snap}")
                c.save()
                packet.seek(0)
                overlay_page = PdfReader(packet).pages[0]
                page.merge_page(overlay_page)
                writer.add_page(page)
                continue

            # Для отчёта 23 не перекрываем верх страницы белой "заглушкой":
            # иначе съедается полезный контент под шапкой.
            header_clear_h = 0 if report_id == "23" else 90
            if header_clear_h > 0:
                c.setFillColor(HexColor("#FFFFFF"))
                c.rect(0, h - header_clear_h, w, header_clear_h, fill=1, stroke=0)

            logo_x = 10
            logo_w = 26
            logo_h = 26
            logo_y = h - 46
            if logo_path.exists():
                try:
                    c.drawImage(
                        ImageReader(str(logo_path)),
                        logo_x,
                        logo_y,
                        width=logo_w,
                        height=logo_h,
                        mask="auto",
                    )
                except Exception:
                    pass

            box_w = 180
            box_h = 22
            x = max(20, w - box_w - 18)
            y = h - box_h - 10
            c.setFillColor(HexColor("#D9E1F2"))
            c.setStrokeColor(HexColor("#000000"))
            c.setLineWidth(0.35)
            c.rect(x, y, box_w, box_h, fill=1, stroke=1)
            c.setFillColor(HexColor("#111827"))
            c.setFont(font_name, 10.5)
            c.drawCentredString(x + box_w / 2, y + 6.5, f"Сводка за {d_str}")

            text_x = logo_x + logo_w + 4
            text_max_x = x - 8
            text_max_w = max(40, text_max_x - text_x)

            def _fit_one_line(text: str, font_sz: float) -> str:
                if not text:
                    return ""
                current = text.strip()
                while current and c.stringWidth(current, font_name, font_sz) > text_max_w:
                    current = current[:-1].rstrip()
                if current != text.strip() and current:
                    while current and c.stringWidth(current + "...", font_name, font_sz) > text_max_w:
                        current = current[:-1].rstrip()
                    return f"{current}..." if current else ""
                return current

            c.setFillColor(HexColor("#111827"))
            line1_fs = 8.8
            line2_fs = 7.8
            line_gap = 2.5
            line1_y = logo_y + logo_h - line1_fs - 1.0
            line2_y = line1_y - line2_fs - line_gap
            if header_t1:
                c.setFont(font_name, line1_fs)
                t1 = _fit_one_line(header_t1, line1_fs)
                if t1:
                    c.drawString(text_x, line1_y, t1)
            if header_t2:
                c.setFont(font_name, line2_fs)
                t2 = _fit_one_line(header_t2, line2_fs)
                if t2:
                    c.drawString(text_x, line2_y, t2)

            c.save()
            packet.seek(0)
            overlay_page = PdfReader(packet).pages[0]
            page.merge_page(overlay_page)
            writer.add_page(page)

        tmp_pdf = pdf_path.with_suffix(".tmp.pdf")
        with tmp_pdf.open("wb") as fp:
            writer.write(fp)
        tmp_pdf.replace(pdf_path)
        return True
    except Exception:
        return False


def _pdf_firm_title_line(sheet_title: str) -> str:
    name = " ".join(((sheet_title or "").strip()).split())
    if not name:
        return ""
    # Сводка 11: листы зерна помечены « (зерно)» только для уникальности имён в Excel.
    for suffix in (" (зерно)", " (овощи)"):
        if name.casefold().endswith(suffix):
            name = name[: -len(suffix)].rstrip()
            break
    normalized = name.casefold().replace(" ", "").replace("\u00a0", "").replace("-", "")
    firm_titles = {
        "афчистопольская": 'ООО "АФ ЧИСТОПОЛЬСКАЯ"',
        "агрораздолье": 'ООО "АГРОРАЗДОЛЬЕ"',
        "аксуагро": 'ООО "АКСУ-АГРО"',
        "нурлатагро": 'ООО "НУРЛАТАГРО"',
        "дрожжаноерассветагро": 'ООО "ДРОЖЖАНОЕ РАССВЕТ АГРО"',
        "актай": 'ООО "АКТАЙ"',
        "лениногорскаяптицефабрика": 'ООО "ЛЕНИНОГОРСКАЯ ПТИЦЕФАБРИКА"',
    }
    if normalized in firm_titles:
        return firm_titles[normalized]
    return f'ООО "{name.upper()}"'


def _extract_report_id_from_stem(stem: str) -> str:
    s = stem.strip()
    # «0.1 …» не должен определяться как отчёт «0» (иначе ломается шапка PDF и regex съедает только «0.»).
    if re.match(r"^0\.1(?:[\s._-]|$)", s):
        return "0.1"
    # Десятичные id вида «1.18» в начале имени файла (до пробела/подчёркивания/даты).
    m_dec = re.match(r"^\s*(\d+\.\d+)(?:[\s._-]|$)", s)
    if m_dec:
        return m_dec.group(1)
    m = re.match(r"^\s*(\d+)(?:[\.\s_-]|$)", s)
    return m.group(1) if m else ""


def _is_report1_firm_header(line1: str) -> bool:
    t = str(line1 or "").strip().upper()
    return t.startswith('ООО "')


_FIRM_CONTINUATION_REPORT_IDS = frozenset({"3", "4", "5", "6", "7", "8", "9", "14"})


def _looks_like_work_report_firm_start_page(page_text: str) -> bool:
    """
    Первая страница блока фирмы в сводках 3–9 / 14 — секция СОБСТВЕННЫЕ или пустой лист.
    Продолжение таблицы и блок НАЁМНЫЕ той же фирмы — не смена фирмы в штампе.
    """
    t = (page_text or "").casefold().replace("\u00a0", " ")
    if not t.strip():
        return False
    head_lines = [ln.strip() for ln in t.splitlines()[:35] if ln.strip()]
    head = " ".join(head_lines)
    if "агрофирм" in head and ("план" in head or "факт" in head):
        return False
    own_pos = head.find("собственные")
    hire_pos = min(
        [head.find("наёмные"), head.find("наемные")],
        key=lambda p: p if p >= 0 else 10**9,
    )
    if hire_pos >= 0 and (own_pos < 0 or hire_pos < own_pos):
        return False
    if own_pos >= 0:
        return True
    if "нет данных" in head:
        return True
    if "№ п/п" in head and "комбайн" in head:
        return True
    return False


def _looks_like_report1_firm_start_page(page_text: str) -> bool:
    """
    Первая страница блока фирмы в отчёте 1 содержит шапку таблицы по культурам.
    Продолжение таблицы обычно содержит только детальные секции и время HH:MM:SS.
    """
    t = (page_text or "").casefold().replace("\u00a0", " ")
    if not t.strip():
        return False
    has_culture_header = "культура" in t
    has_fertilizer_header = ("план обраб" in t) or ("факт обраб" in t) or ("остаток г" in t)
    if has_culture_header and has_fertilizer_header:
        return True

    # Иногда первая страница новой фирмы идёт без блока "Культура" (только детализация),
    # но нумерация таблицы начинается заново с 1 в верхней части страницы.
    head_lines = [ln.strip() for ln in t.splitlines()[:80] if ln.strip()]
    head_text = " ".join(head_lines)
    has_sections = ("собственные" in head_text) or ("наёмные" in head_text) or ("наемные" in head_text)
    starts_with_first_index = bool(re.search(r"(^|\s)1(\s|$)", head_text))
    return has_sections and starts_with_first_index


_REPORT8_FIRM_ORDER: tuple[str, ...] = (
    "АФ Чистопольская",
    "АгроРаздолье",
    "Аксу-Агро",
    "НурлатАгро",
    "Дрожжаное Рассвет Агро",
    "Актай",
    "Лениногорская Птицефабрика",
)

# Сводки 10/13 — виды работ (как в 11.main_proverka / 14.main)
_HARVEST_WT_LABEL_BY_ID: dict[int, str] = {
    124: "5.4 Уборка картофеля, лука",
    125: "5.1 Уборка подсолнечника",
    126: "5.2 Уборка зерновых и зернобобовых",
    127: "5.3 Уборка кукурузы на зерно",
    145: "5.8 Уборка рапса, горчицы, гречихи и проса",
}
_HARVEST_WT_LABEL_BY_CROP: dict[str, str] = {
    "Кукуруза на силос": "6.3 Скашивание с измельчением кукурузы",
}
_HARVEST_CROP_WORK_TYPE_ID: dict[str, int] = {
    "Рапс озимый": 145,
    "Рапс яровой": 145,
    "Пшеница озимая": 126,
    "Пшеница яровая": 126,
    "Тритикале озимая": 126,
    "Соя": 126,
    "Подсолнечник": 125,
    "Кукуруза на зерно": 127,
    "Кукуруза на силос": 127,
}
_HARVEST_SUMMARY_PDF_HEADERS: dict[str, tuple[str, str]] = {
    "Общий намолот": (
        "ОБЩИЙ НАМОЛОТ КУЛЬТУР ПО ХОЛДИНГУ",
        "ВИД РАБОТ: 5.0 Уборка",
    ),
    "Овощи": (
        "ОБЩАЯ УБОРКА ОВОЩНЫХ КУЛЬТУР ПО ХОЛДИНГУ",
        "ВИД РАБОТ: 5.4 Уборка картофеля, лука",
    ),
    "Зерновые культуры": (
        "ЗЕРНОВЫЕ КУЛЬТУРЫ",
        "ВИД РАБОТ: 5.2 Уборка зерновых и зернобобовых",
    ),
    "Овощные культуры": (
        "ОВОЩНЫЕ КУЛЬТУРЫ",
        "ВИД РАБОТ: 5.4 Уборка картофеля, лука",
    ),
    # Сводка 14: отдельные листы по овощам (не путать с зерновой «5.0 Уборка»).
    "Сводка, овощи": (
        "ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ",
        "ВИД РАБОТ: Уборка овощей",
    ),
    "Уборка овощей": (
        "ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ",
        "ВИД РАБОТ: Уборка овощей",
    ),
    "Уборка, овощи": (
        "ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ",
        "ВИД РАБОТ: Уборка овощи",
    ),
    "Уборка овощи": (
        "ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ",
        "ВИД РАБОТ: Уборка овощи",
    ),
}
_HARVEST_DEFAULT_WORK_LINE = "ВИД РАБОТ: 5.0 Уборка"
_HARVEST_TECH_TITLE = "ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ"


def _harvest_firm_sheet_names() -> frozenset[str]:
    keys: set[str] = set()
    for name in _REPORT8_FIRM_ORDER:
        keys.add(name.casefold().replace(" ", "").replace("\u00a0", "").replace("-", ""))
    return frozenset(keys)


def _harvest_work_type_line(*, crop_name: str | None = None, work_type_id: int | None = None) -> str:
    crop = (crop_name or "").strip()
    if crop in _HARVEST_WT_LABEL_BY_CROP:
        return f"ВИД РАБОТ: {_HARVEST_WT_LABEL_BY_CROP[crop]}"
    if work_type_id is not None and work_type_id in _HARVEST_WT_LABEL_BY_ID:
        return f"ВИД РАБОТ: {_HARVEST_WT_LABEL_BY_ID[work_type_id]}"
    return _HARVEST_DEFAULT_WORK_LINE


def _harvest_crop_for_sheet(sheet_name: str) -> str | None:
    sn = (sheet_name or "").strip()
    if not sn:
        return None
    if sn in _HARVEST_CROP_WORK_TYPE_ID:
        return sn
    for crop in _HARVEST_CROP_WORK_TYPE_ID:
        if crop.startswith(sn) or sn == crop[:31]:
            return crop
    return None


def _contains_own_section(page_text: str) -> bool:
    t = (page_text or "").casefold()
    return "собственные" in t


def _contains_hired_section(page_text: str) -> bool:
    t = (page_text or "").casefold()
    return ("наёмные" in t) or ("наемные" in t)


def _sheet_has_own_or_hired_section(ws) -> bool:
    try:
        max_row = int(ws.max_row or 1)
    except Exception:
        max_row = 1
    for row_idx in range(1, min(max_row + 1, 500)):
        value = ws.cell(row_idx, 1).value
        if isinstance(value, str):
            title = value.strip().upper()
            if title.startswith("СОБСТВЕННЫЕ") or title.startswith("НАЁМНЫЕ") or title.startswith("НАЕМНЫЕ"):
                return True
    return False


def _is_harvest_table_sheet(ws) -> bool:
    """Сводки 10/13/14: таблица с первой строки (№ п/п / Культура), без блоков СОБСТВЕННЫЕ."""
    a1 = ws.cell(1, 1).value
    if isinstance(a1, str) and a1.strip() == "№ п/п":
        return True
    b1 = ws.cell(1, 2).value
    if isinstance(b1, str) and b1.strip() in {"Культура", "Агрофирма", "Агрофирмы"}:
        return True
    return False


def _is_harvest_holding_sheet(ws, sheet_title: str) -> bool:
    """Лист сводки 10/13/14 — в PDF всегда есть страница, не «пустая детализация»."""
    sn = (sheet_title or "").strip()
    if sn in _HARVEST_SUMMARY_PDF_HEADERS:
        return True
    if _harvest_crop_for_sheet(sn):
        return True
    if _is_harvest_table_sheet(ws):
        return True
    a1 = ws.cell(1, 1).value
    if isinstance(a1, str):
        head = a1.strip().upper()
        if any(
            token in head
            for token in ("ЗЕРНОВЫЕ", "ОВОЩНЫЕ", "ОВОЩНЫХ", "НАМОЛОТ", "УБОРКА")
        ):
            return True
    return False


def _looks_like_harvest_holding_book(sheetnames: list[str]) -> bool:
    if not sheetnames:
        return False
    first = sheetnames[0].strip()
    if first == "Общий намолот":
        return True
    if first == "Зерновые культуры":
        return len(sheetnames) >= 2 and sheetnames[1].strip() in {
            "Овощи",
            "Овощные культуры",
        }
    return False


def _headers_for_harvest_holding_book(
    per_sheet: list[tuple[str, str]],
    n_pages: int,
    default_header: tuple[str, str],
) -> list[tuple[str, str]]:
    """Сводки 10/13/14: 1 лист Excel = 1 страница PDF (без LO-пересчёта страниц)."""
    if n_pages < 1:
        return [default_header]
    if not per_sheet:
        return [default_header] * n_pages
    if n_pages <= len(per_sheet):
        return per_sheet[:n_pages]
    pad = per_sheet[-1]
    return list(per_sheet) + [pad] * (n_pages - len(per_sheet))


def _is_detail_sheet_without_data(ws, sheet_title: str) -> bool:
    """Лист детализации без строк (write_detail_sheet выходит до СОБСТВЕННЫЕ) — в PDF страниц нет."""
    sn = (sheet_title or "").strip()
    if sn == "Сводка" or sn.startswith("Сводка,") or sn.startswith("Выраб."):
        return False
    if sn in {"Уборка, овощи", "Уборка овощи", "Уборка овощей"}:
        return False
    # Сводка 9: листы «Вспашка <культура>» — сводные таблицы, не пустая детализация АФ.
    if _report9_plow_crop_from_sheet(sn) is not None:
        return False
    if _is_harvest_holding_sheet(ws, sheet_title):
        return False
    return not _sheet_has_own_or_hired_section(ws)


def _parse_report1_drop_pages() -> set[int]:
    raw = (os.environ.get("SVODKI_REPORT1_DROP_PAGES") or "").strip()
    out: set[int] = set()
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            page_no = int(token)
        except ValueError:
            continue
        if page_no > 0:
            out.add(page_no)
    return out


# Листы «Выраб. — …» в Excel обрезаются до 31 символа — полные имена для PDF-штампа.
_REPORT1_PERF_GROUP_ORDER: tuple[str, ...] = (
    "2.1 Боронование",
    "2.9 Дискование",
    "2.6 Фрезерование",
    "2.11 Культивация",
    "4.0 Посев",
    "4.1 Посев (овощи)",
    "3.1 Опрыскивание",
    "3.2 Опрыскивание (овощи)",
    "2.12 Прикатывание",
    "2.7 Гребнеобразование",
    "5.0 Уборка",
    "5.4 Уборка картофеля, лука",
)
# Обрезанные Excel-имена (31 символ) → полное название группы для строки «ВИД РАБОТ».
_REPORT1_PERF_TRUNCATED_TAIL: dict[str, str] = {
    "3.2 Опрыскивание (овощ": "3.2 Опрыскивание (овощи)",
    "5.4 Уборка картофеля,": "5.4 Уборка картофеля, лука",
}


def _report1_perf_work_suffix(sn: str) -> str | None:
    """Полное название группы работ для листа «Выраб. — …» (учёт обрезки Excel)."""
    prefix = "Выраб. — "
    if not sn.startswith(prefix):
        return None
    tail = sn[len(prefix):].strip()
    if tail in _REPORT1_PERF_TRUNCATED_TAIL:
        return _REPORT1_PERF_TRUNCATED_TAIL[tail]
    if tail in _REPORT1_PERF_GROUP_ORDER:
        return tail
    # Excel мог обрезать имя посередине слова — ищем единственное совпадение по префиксу.
    matches = [g for g in _REPORT1_PERF_GROUP_ORDER if g.startswith(tail)]
    if len(matches) == 1:
        return matches[0]
    return tail


def _report1_pdf_sheet_headers(sheet_name: str) -> tuple[str, str]:
    sn = (sheet_name or "").strip()
    wt_main = "ВИД РАБОТ: 1.1 Внесение минеральных удобрений"
    wt_seeding = "ВИД РАБОТ: 4.0 Посев"
    wt_veg_seeding = "ВИД РАБОТ: 4.1 Посев (овощи)"
    report1_title = "СВОДКА ХК Крупно"
    fert_prefix = "Удобрения — "
    if sn.startswith(fert_prefix):
        crop_group = sn[len(fert_prefix):].strip()
        if crop_group in {"Травы и прочие", "Заготовка корма", "Однолетние травы"}:
            crop_group = "Заготовка корма"
        elif crop_group == "Пар":
            crop_group = "Пар"
        elif crop_group in {"Овощи (1)", "Овощи (2)", "Свекла столовая", "Овощи"}:
            crop_group = "Овощи"
        return ("СВОДКА ПО УДОБРЕНИЯМ", f"{wt_main} — {crop_group}")
    if sn == "Свод по удобрениям (ХК)":
        return ("СВОДКА ПО УДОБРЕНИЯМ", wt_main)
    if sn == "Вспашка + Глубокорыхление":
        return ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", "ВИД РАБОТ: 2.4 Вспашка, 2.8 Глубокорыхление")
    if sn == "Итого (все АФ)" or sn.startswith("ЯрСев блок"):
        return (report1_title, wt_seeding)
    if sn == "Овощи — посев":
        return (_pdf_firm_title_line("АФ Чистопольская"), wt_veg_seeding)
    if sn == "Сводка по культурам":
        return ("СВОДКА ПО КУЛЬТУРАМ (ХК)", wt_main)
    if sn == "Общий намолот":
        return (
            "ОБЩИЙ НАМОЛОТ КУЛЬТУР ПО ХОЛДИНГУ",
            "ВИД РАБОТ: 5.0 Уборка",
        )
    if sn == "Сводка овощи":
        return (
            "ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ",
            "ВИД РАБОТ: 5.4 Уборка овощей",
        )
    if sn == "Овощи":
        return (
            "ОБЩАЯ УБОРКА ОВОЩНЫХ КУЛЬТУР ПО ХОЛДИНГУ",
            "ВИД РАБОТ: 5.4 Уборка картофеля, лука",
        )
    if sn == "Свод по агрофирмам":
        return (report1_title, wt_main)
    if sn == "Выработка техники":
        return ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", wt_main)
    prefix = "Выраб. — "
    if sn.startswith(prefix):
        work_suffix = _report1_perf_work_suffix(sn) or sn[len(prefix):].strip()
        return ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", f"ВИД РАБОТ: {work_suffix}")
    # Если в названии листа явно есть агрофирма — используем её напрямую.
    sn_l = sn.casefold()
    firm_markers: tuple[tuple[str, str], ...] = (
        ("аф чистопольская", "АФ Чистопольская"),
        ("агрораздолье", "АгроРаздолье"),
        ("аксу-агро", "Аксу-Агро"),
        ("аксу агро", "Аксу-Агро"),
        ("нурлатагро", "НурлатАгро"),
        ("дрожжаное рассвет агро", "Дрожжаное Рассвет Агро"),
        ("актай", "Актай"),
        ("лениногорская птицефабрика", "Лениногорская Птицефабрика"),
    )
    for marker, firm_name in firm_markers:
        if marker in sn_l:
            return (_pdf_firm_title_line(firm_name), wt_main)
    return (_pdf_firm_title_line(sn), wt_main)


def _report3_pdf_sheet_headers(sheet_name: str) -> tuple[str, str]:
    sn = (sheet_name or "").strip()
    wt = "ВИД РАБОТ: 1.1 Внесение минеральных удобрений"
    if sn == "Сводка":
        return ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", wt)
    return (_pdf_firm_title_line(sn), wt)


def _report4_pdf_sheet_headers(sheet_name: str) -> tuple[str, str]:
    sn = (sheet_name or "").strip()
    wt = "ВИД РАБОТ: 2.1 Боронование зяби"
    if sn == "Сводка":
        return ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", wt)
    return (_pdf_firm_title_line(sn), wt)


def _work_report_sheet_headers(sheet_name: str, work_line: str) -> tuple[str, str]:
    sn = (sheet_name or "").strip()
    if sn == "Сводка":
        return ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", work_line)
    return (_pdf_firm_title_line(sn), work_line)


def _report8_pdf_sheet_headers(sheet_name: str) -> tuple[str, str]:
    sn = (sheet_name or "").strip()
    if "Фрезерование" in sn:
        if sn == "Фрезерование":
            return (_pdf_firm_title_line("АФ Чистопольская"), "ВИД РАБОТ: 2.6 Фрезерование")
        if sn == "Выраб. — 2.6 Фрезерование":
            return ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", "ВИД РАБОТ: 2.6 Фрезерование")
        return _work_report_sheet_headers(sn, "ВИД РАБОТ: 2.6 Фрезерование")
    return _work_report_sheet_headers(sn, "ВИД РАБОТ: 2.11 Культивация")


def _report9_plow_crop_from_sheet(sheet_name: str) -> str | None:
    """Лист культуры сводки 9: «Вспашка Рапс озимый», «Вспашка Соя (2)», … — не АФ."""
    sn = (sheet_name or "").strip()
    prefix = "Вспашка "
    if not sn.startswith(prefix):
        return None
    crop = sn[len(prefix):].strip()
    if crop.endswith(")") and " (" in crop:
        crop = crop.rsplit(" (", 1)[0].strip()
    return crop or None


def _report5_seeding_crop_from_sheet(sheet_name: str) -> str | None:
    """Лист культуры сводки 5: «Посев Рапс озимый», «Посев Пшеница озимая» — не АФ.

    Нельзя штамповать через _pdf_firm_title_line: получается
    ООО \"ПОСЕВ РАПС ОЗИМЫЙ\", а firm-continuation копирует этот заголовок
    на следующий лист культуры (пшеница озимая).
    """
    sn = (sheet_name or "").strip()
    prefix = "Посев "
    if not sn.startswith(prefix):
        return None
    crop = sn[len(prefix):].strip()
    if crop.endswith(")") and " (" in crop:
        crop = crop.rsplit(" (", 1)[0].strip()
    return crop or None


def _report5_seeding_crop_title(crop_name: str) -> str:
    crop = " ".join((crop_name or "").strip().split())
    if not crop:
        return "ПОСЕВ"
    return f"ПОСЕВ {crop.upper()}"


def _report9_pdf_sheet_headers(sheet_name: str) -> tuple[str, str]:
    """Отчёт 9: сводка + листы вспашки по культурам; АФ — ООО и оба вида работ."""
    sn = (sheet_name or "").strip()
    wt_both = "ВИД РАБОТ: 2.4 Вспашка, 2.8 Глубокорыхление"
    title = "ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ"
    if sn == "Сводка":
        return (title, wt_both)
    # Листы культур (после фикса мультикультур) — не штамповать как ООО «ВСПАШКА …»:
    # иначе firm-continuation держит первую культуру и сдвигает шапки АФ.
    crop = _report9_plow_crop_from_sheet(sn)
    if crop is not None:
        return (title, f"ВИД РАБОТ: 2.4 Вспашка — {crop}")
    return (_pdf_firm_title_line(sn), wt_both)


def _is_report9_book(report_id: str, stem: str, sheetnames: list[str]) -> bool:
    if report_id == "9":
        return True
    if not sheetnames or sheetnames[0].strip() != "Сводка":
        return False
    if stem.lower().startswith("report_plowing"):
        return True
    # Переименованный xlsx: «9.РАБОТА ТЕХНИКИ Вспашка…» — листы «Вспашка <культура>».
    return (
        len(sheetnames) >= 2
        and _report9_plow_crop_from_sheet(sheetnames[1]) is not None
        and ("вспашк" in stem.casefold() or "plowing" in stem.casefold())
    )


def _report11_pdf_sheet_headers(sheet_name: str) -> tuple[str, str]:
    sn = (sheet_name or "").strip()
    wt_grain = "ВИД РАБОТ: Уборка"
    wt_veg = "ВИД РАБОТ: Уборка овощей"
    title = "ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ"
    if sn == "Сводка":
        return (title, wt_grain)
    if sn == "Сводка овощи":
        return (title, wt_veg)
    if not sn:
        return ("", "")
    # Лист «… (зерно)» — зерновая детализация; без суффикса — овощи (как сводка 10).
    is_grain = sn.casefold().endswith(" (зерно)")
    wt = wt_grain if is_grain else wt_veg
    return (_pdf_firm_title_line(sn), wt)


def _report12_pdf_sheet_headers(sheet_name: str) -> tuple[str, str]:
    sn = (sheet_name or "").strip()
    if sn == "Элеваторы":
        return (
            "СТАТИСТИКА ПРОСТОЯ ГРУЗОВОГО ТРАНСПОРТА НА ЭЛЕВАТОРАХ",
            "",
        )
    if sn:
        return (
            _pdf_firm_title_line(sn),
            "СТАТИСТИКА ПРОСТОЯ ГРУЗОВОГО ТРАНСПОРТА И КОМБАЙНОВ ПРИ УБОРКЕ УРОЖАЯ",
        )
    return ("СТАТИСТИКА ПРОСТОЯ ПРИ УБОРКЕ УРОЖАЯ", "")


def _report14_pdf_sheet_headers(sheet_name: str) -> tuple[str, str]:
    """Сводка 14: сводки зерно/овощи + лист на каждую АФ (многостраничные) + «Уборка, овощи»."""
    sn = (sheet_name or "").strip()
    if sn in _HARVEST_SUMMARY_PDF_HEADERS:
        return _HARVEST_SUMMARY_PDF_HEADERS[sn]
    if sn == "Сводка":
        return (_HARVEST_TECH_TITLE, _HARVEST_DEFAULT_WORK_LINE)
    if sn in {"Уборка, овощи", "Уборка овощи", "Уборка овощей"}:
        return (
            _pdf_firm_title_line("АФ Чистопольская"),
            "ВИД РАБОТ: Уборка овощи",
        )
    norm = sn.casefold().replace(" ", "").replace("\u00a0", "").replace("-", "")
    if norm in _harvest_firm_sheet_names():
        return (_pdf_firm_title_line(sn), _HARVEST_DEFAULT_WORK_LINE)
    return (_HARVEST_TECH_TITLE, _HARVEST_DEFAULT_WORK_LINE)


def _harvest_crop_holding_title(crop_name: str) -> str:
    crop = " ".join((crop_name or "").strip().split())
    if not crop:
        return ""
    return f"УБОРКА {crop.upper()} ПО ХОЛДИНГУ"


def _harvest_pdf_sheet_headers(sheet_name: str) -> tuple[str, str]:
    """Сводки 10/13: сводные листы — тема + вид работ; по культурам — «УБОРКА … ПО ХОЛДИНГУ»."""
    sn = (sheet_name or "").strip()
    if sn in _HARVEST_SUMMARY_PDF_HEADERS:
        return _HARVEST_SUMMARY_PDF_HEADERS[sn]
    norm = sn.casefold().replace(" ", "").replace("\u00a0", "").replace("-", "")
    if norm in _harvest_firm_sheet_names():
        return (_pdf_firm_title_line(sn), _HARVEST_DEFAULT_WORK_LINE)
    crop = _harvest_crop_for_sheet(sn)
    if crop:
        return (_harvest_crop_holding_title(crop), "")
    return (_HARVEST_TECH_TITLE, _HARVEST_DEFAULT_WORK_LINE)


def _build_pdf_headers_per_page(
    src: Path,
    n_pages: int,
    layout_src: Path | None = None,
    report_id: str | None = None,
) -> list[tuple[str, str]]:
    default_header = _resolve_pdf_header_text(src)
    header_book = layout_src or src
    if n_pages < 1 or header_book.suffix.lower() != ".xlsx":
        return [default_header] * max(1, n_pages)
    try:
        from openpyxl import load_workbook
    except Exception:
        return [default_header] * n_pages
    try:
        wb = load_workbook(header_book, read_only=True, data_only=True)
        sheetnames = list(wb.sheetnames)
        wb.close()
    except Exception:
        return [default_header] * n_pages

    report_id = (report_id or "").strip() or _extract_report_id_from_stem(src.stem)
    is_report1 = report_id in _REPORT1_FAMILY_IDS or (
        bool(sheetnames) and sheetnames[0].strip() == "Сводка по культурам"
    )
    if is_report1 and sheetnames:
        r1_xlsx = src if src.suffix.lower() == ".xlsx" else header_book
        r1_sheetnames = sheetnames
        if r1_xlsx != header_book:
            try:
                from openpyxl import load_workbook as _load_wb_r1

                _wb_r1 = _load_wb_r1(r1_xlsx, read_only=True, data_only=True)
                r1_sheetnames = list(_wb_r1.sheetnames)
                _wb_r1.close()
            except Exception:
                r1_sheetnames = sheetnames
        page_order_mod = _load_report1_pdf_page_order_module()
        build_r1_hdrs = (
            getattr(page_order_mod, "build_report1_pdf_headers", None)
            if page_order_mod is not None
            else None
        )
        if callable(build_r1_hdrs):
            ordered = build_r1_hdrs(
                r1_xlsx,
                n_pages,
                _report1_pdf_sheet_headers,
                default_header,
            )
            if ordered and len(ordered) >= n_pages:
                return ordered[:n_pages]
        per_sheet = [_report1_pdf_sheet_headers(name) for name in r1_sheetnames]
        exact_report1_headers = str(
            os.environ.get("SVODKI_REPORT1_EXACT_PAGE_HEADERS", "1")
        ).strip().lower() not in {"0", "false", "no"}
        if exact_report1_headers:
            exact = _headers_for_book_by_actual_pdf(
                r1_xlsx,
                n_pages,
                per_sheet,
                default_header,
            )
            if exact:
                return exact
        return _headers_for_book_by_row_breaks(
            r1_xlsx,
            n_pages,
            per_sheet,
            default_header,
            # Фолбэк-эвристика, если точный расчёт недоступен.
            no_breaks_extra_pages="spread_after_first",
        )
    is_harvest_book = report_id in {"10", "13"} or (
        not report_id and _looks_like_harvest_holding_book(sheetnames)
    )
    if is_harvest_book and sheetnames:
        per_harvest = [_harvest_pdf_sheet_headers(name) for name in sheetnames]
        return _headers_for_harvest_holding_book(per_harvest, n_pages, default_header)
    if report_id == "14" and sheetnames:
        per14 = [_report14_pdf_sheet_headers(name) for name in sheetnames]
        # Только точные counts по листам без fit — иначе АФ «переезжают» на чужие таблицы.
        exact14 = _headers_for_book_by_actual_pdf(
            header_book,
            n_pages,
            per14,
            default_header,
            allow_fit=False,
        )
        if exact14:
            return exact14
        # Фолбэк: row_breaks / spread — stamp потом перепривяжет по СОБСТВЕННЫЕ.
        return _headers_for_book_by_row_breaks(
            header_book,
            n_pages,
            per14,
            default_header,
            no_breaks_extra_pages="spread_after_first",
        )
    if report_id == "11" and sheetnames:
        per_11 = [_report11_pdf_sheet_headers(name) for name in sheetnames]
        exact_11 = _headers_for_book_by_actual_pdf(
            header_book,
            n_pages,
            per_11,
            default_header,
        )
        if exact_11:
            return exact_11
        return _headers_for_book_by_row_breaks(
            header_book,
            n_pages,
            per_11,
            default_header,
            no_breaks_extra_pages="spread_after_first",
        )
    if report_id == "12" and sheetnames:
        per_12 = [_report12_pdf_sheet_headers(name) for name in sheetnames]
        exact_12 = _headers_for_book_by_actual_pdf(
            header_book,
            n_pages,
            per_12,
            default_header,
        )
        if exact_12:
            return exact_12
        return _headers_for_book_by_row_breaks(
            header_book,
            n_pages,
            per_12,
            default_header,
            no_breaks_extra_pages="spread_after_first",
        )

    stem_l = src.stem.lower()
    is_report3_book = report_id == "3" or (bool(sheetnames) and sheetnames[0].strip() == "Сводка" and stem_l.startswith("report_fertilizer"))
    is_report4_book = report_id == "4" or (bool(sheetnames) and sheetnames[0].strip() == "Сводка" and stem_l.startswith("report_harrowing"))
    is_report7_book = report_id == "7" or (bool(sheetnames) and sheetnames[0].strip() == "Сводка" and stem_l.startswith("report_disking"))
    is_report8_book = report_id == "8" or (bool(sheetnames) and sheetnames[0].strip() == "Сводка" and stem_l.startswith("report_cultivation"))
    is_report9_book = _is_report9_book(report_id, stem_l, sheetnames)
    if is_report3_book and len(sheetnames) >= 2:
        per3 = [_report3_pdf_sheet_headers(name) for name in sheetnames]
        exact3 = _headers_for_book_by_actual_pdf(
            header_book,
            n_pages,
            per3,
            default_header,
        )
        if exact3:
            return exact3
        return _headers_for_book_by_row_breaks(
            header_book,
            n_pages,
            per3,
            default_header,
            no_breaks_extra_pages="spread_after_first",
        )
    is_report6_book = (
        report_id == "6"
        and len(sheetnames) >= 2
        and sheetnames[0].strip() == "Сводка"
    )
    if is_report6_book:
        wt6 = "ВИД РАБОТ: 3.1 Опрыскивание"
        line1_report6_summary = "ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ"

        wt6_veg = "ВИД РАБОТ: 3.2 Опрыскивание (овощи)"

        def _sheet_headers_report6(sheet_name: str) -> tuple[str, str]:
            sn = (sheet_name or "").strip()
            if sn == "Сводка":
                return (line1_report6_summary, wt6)
            sn_cf = sn.casefold().replace("\u00a0", " ")
            if "овощ" in sn_cf and "опрыск" in sn_cf:
                return (_pdf_firm_title_line("АФ Чистопольская"), wt6_veg)
            return (_pdf_firm_title_line(sn), wt6)

        per6 = [_sheet_headers_report6(name) for name in sheetnames]
        exact6 = _headers_for_book_by_actual_pdf(
            header_book,
            n_pages,
            per6,
            default_header,
        )
        if exact6:
            return exact6
        return _headers_for_book_by_row_breaks(
            header_book,
            n_pages,
            per6,
            default_header,
            no_breaks_extra_pages="spread_after_first",
        )
    is_report5_book = (
        report_id == "5"
        and len(sheetnames) >= 2
        and any((sn or "").strip() == "Сводка" for sn in sheetnames)
    )
    if is_report5_book:
        # Как у отчёта 1: полевой посев vs овощи — у овощей другая строка «ВИД РАБОТ»,
        # а в первой строке — ООО этой агрофирмы (из хвоста названия листа «Овощи …»).
        wt5_field = "ВИД РАБОТ: 4.0 Посев"
        wt5_veg = "ВИД РАБОТ: 4.1 Посев (овощи)"
        line1_report5_summary = "ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ"

        def _sheet_headers_report5(sheet_name: str) -> tuple[str, str]:
            sn = (sheet_name or "").strip()
            if sn == "Сводка":
                return (line1_report5_summary, wt5_field)
            if sn == "Итого (все АФ)" or sn.startswith("ЯрСев блок"):
                return (line1_report5_summary, wt5_field)
            # Листы «Посев <культура>» — как уборка 10: без ООО/кавычек.
            crop = _report5_seeding_crop_from_sheet(sn)
            if crop is not None:
                return (_report5_seeding_crop_title(crop), wt5_field)
            cf = sn.casefold().replace("\u00a0", " ")
            if cf.startswith("овощи"):
                m = re.match(r"^\s*Овощи\s+(.+)$", sn, flags=re.I)
                firm_raw = (m.group(1) or "").strip() if m else ""
                line1_veg = (
                    _pdf_firm_title_line(firm_raw)
                    if firm_raw
                    else _pdf_firm_title_line("АФ Чистопольская")
                )
                return (line1_veg, wt5_veg)
            return (_pdf_firm_title_line(sn), wt5_field)

        per5 = [_sheet_headers_report5(name) for name in sheetnames]
        exact5 = _headers_for_book_by_actual_pdf(
            header_book,
            n_pages,
            per5,
            default_header,
        )
        if exact5:
            return exact5
        return _headers_for_book_by_row_breaks(
            header_book,
            n_pages,
            per5,
            default_header,
            no_breaks_extra_pages="spread_after_first",
        )
    if is_report4_book and len(sheetnames) >= 2:
        per_sheet = [_report4_pdf_sheet_headers(name) for name in sheetnames]
        exact = _headers_for_book_by_actual_pdf(
            header_book,
            n_pages,
            per_sheet,
            default_header,
        )
        if exact:
            return exact
        return _headers_for_book_by_row_breaks(
            header_book,
            n_pages,
            per_sheet,
            default_header,
            no_breaks_extra_pages="last",
        )
    if is_report7_book and len(sheetnames) >= 2:
        per7 = [
            _work_report_sheet_headers(name, "ВИД РАБОТ: 2.9 Дискование")
            for name in sheetnames
        ]
        exact7 = _headers_for_book_by_actual_pdf(
            header_book,
            n_pages,
            per7,
            default_header,
        )
        if exact7:
            return exact7
        return _headers_for_book_by_row_breaks(
            header_book,
            n_pages,
            per7,
            default_header,
            no_breaks_extra_pages="spread_after_first",
        )
    if is_report8_book and len(sheetnames) >= 2:
        per8 = [_report8_pdf_sheet_headers(name) for name in sheetnames]
        exact8 = _headers_for_book_by_actual_pdf(
            header_book,
            n_pages,
            per8,
            default_header,
        )
        if exact8:
            return exact8
        return _headers_for_book_by_row_breaks(
            header_book,
            n_pages,
            per8,
            default_header,
            no_breaks_extra_pages="first",
        )
    if is_report9_book and len(sheetnames) >= 2:
        per_sheet9 = [_report9_pdf_sheet_headers(name) for name in sheetnames]
        exact9 = _headers_for_book_by_actual_pdf(
            header_book,
            n_pages,
            per_sheet9,
            default_header,
        )
        if exact9:
            return exact9
        return _headers_for_book_by_row_breaks(
            header_book,
            n_pages,
            per_sheet9,
            default_header,
            no_breaks_extra_pages="spread_after_first",
        )

    first_sn = sheetnames[0].strip() if sheetnames else ""
    if len(sheetnames) < 2 or first_sn not in ("Сводка", "Отчёт"):
        return [default_header] * n_pages

    per_sheet: list[tuple[str, str]] = [default_header]
    _, work_line = default_header
    for sn in sheetnames[1:]:
        per_sheet.append((_pdf_firm_title_line(sn), work_line))
    return _headers_for_book(sheetnames, n_pages, per_sheet, default_header)


def _headers_for_book(
    sheetnames: list[str],
    n_pages: int,
    per_sheet: list[tuple[str, str]],
    default_header: tuple[str, str],
    extra_pages: str = "first",
) -> list[tuple[str, str]]:
    n_s = len(sheetnames)
    if n_pages == n_s:
        idx_map = list(range(n_s))
    elif n_pages < n_s:
        idx_map = list(range(n_pages))
    elif extra_pages == "last":
        idx_map = list(range(n_s)) + [n_s - 1] * (n_pages - n_s)
    elif extra_pages == "spread_after_first" and n_s > 1:
        counts = [1] * n_s
        extra = n_pages - n_s
        idx = 1
        while extra > 0:
            counts[idx] += 1
            extra -= 1
            idx += 1
            if idx >= n_s:
                idx = 1
        idx_map = []
        for idx, count in enumerate(counts):
            idx_map.extend([idx] * count)
    else:
        pages_first = n_pages - (n_s - 1)
        if pages_first < 1:
            return [default_header] * n_pages
        idx_map = [0] * pages_first + list(range(1, n_s))
        if len(idx_map) != n_pages:
            return [default_header] * n_pages
    return [per_sheet[i] for i in idx_map]


def _headers_for_book_by_actual_pdf(
    src: Path,
    n_pages: int,
    per_sheet: list[tuple[str, str]],
    default_header: tuple[str, str],
    *,
    allow_fit: bool = True,
) -> list[tuple[str, str]] | None:
    """
    Точное распределение шапок по страницам: считаем реальное число PDF-страниц
    для каждого листа через LibreOffice и повторяем шапку листа нужное число раз.
    allow_fit=False — не перераспределять страницы (для сводки 14 иначе «плывут» АФ).
    """
    try:
        from openpyxl import load_workbook
        from pypdf import PdfReader
    except Exception:
        return None

    office_cmd = shutil.which("libreoffice") or shutil.which("soffice")
    if not office_cmd or not src.exists():
        return None

    lo_timeout = _libreoffice_run_timeout()
    tmp_dir = Path(tempfile.mkdtemp(prefix="svodki_header_pages_"))
    try:
        counts: list[int] = []
        sheet_count = len(per_sheet)
        for idx in range(sheet_count):
            wb = load_workbook(src, read_only=False, data_only=False, keep_vba=True)
            try:
                names = list(wb.sheetnames)
                sheet_title = names[idx] if idx < len(names) else ""
                active_ws = wb[names[idx]] if idx < len(names) else None
                if active_ws is not None and _is_detail_sheet_without_data(active_ws, sheet_title):
                    counts.append(0)
                    continue
                for j, name in enumerate(names):
                    if j != idx and name in wb.sheetnames:
                        wb.remove(wb[name])
                sheet_xlsx = tmp_dir / f"sheet_{idx + 1}.xlsx"
                wb.save(sheet_xlsx)
            finally:
                wb.close()

            proc = subprocess.run(
                [
                    office_cmd,
                    "--headless",
                    "--convert-to",
                    "pdf:calc_pdf_Export",
                    "--outdir",
                    str(tmp_dir),
                    str(sheet_xlsx),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=lo_timeout,
            )
            if proc.returncode != 0:
                proc = subprocess.run(
                    [
                        office_cmd,
                        "--headless",
                        "--convert-to",
                        "pdf",
                        "--outdir",
                        str(tmp_dir),
                        str(sheet_xlsx),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=lo_timeout,
                )
            if proc.returncode != 0:
                return None

            sheet_pdf = tmp_dir / f"{sheet_xlsx.stem}.pdf"
            if not sheet_pdf.exists():
                return None
            counts.append(max(1, len(PdfReader(str(sheet_pdf)).pages)))

        if allow_fit:
            counts = _fit_sheet_page_counts_to_pdf(counts, n_pages)
        elif sum(counts) != n_pages:
            return None
        headers: list[tuple[str, str]] = []
        for idx, count in enumerate(counts):
            headers.extend([per_sheet[idx]] * count)
        if len(headers) < n_pages:
            pad = headers[-1] if headers else default_header
            headers.extend([pad] * (n_pages - len(headers)))
        return headers[:n_pages]
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _print_area_last_row(ws) -> int:
    raw_area = getattr(ws, "print_area", None)
    if not raw_area:
        return int(ws.max_row or 1)
    areas = raw_area if isinstance(raw_area, (list, tuple)) else [str(raw_area)]
    max_row = 1
    found = False
    for area in areas:
        for row_s in re.findall(r"\$?[A-Z]+\$?(\d+)", str(area)):
            found = True
            try:
                max_row = max(max_row, int(row_s))
            except ValueError:
                pass
    return max_row if found else int(ws.max_row or 1)


def _estimated_sheet_page_count(ws) -> int:
    breaks = getattr(getattr(ws, "row_breaks", None), "brk", []) or []
    if breaks:
        return max(1, len(breaks) + 1)

    last_row = _print_area_last_row(ws)
    first_row = 1
    try:
        if getattr(ws, "print_area", None):
            nums = [int(x) for x in re.findall(r"\$?[A-Z]+\$?(\d+)", str(ws.print_area))]
            if nums:
                first_row = min(nums)
    except Exception:
        first_row = 1

    total_height = 0.0
    for row in range(first_row, max(first_row, last_row) + 1):
        dim = ws.row_dimensions[row]
        if getattr(dim, "hidden", False):
            continue
        total_height += float(dim.height or ws.sheet_format.defaultRowHeight or 15.0)

    is_landscape = str(getattr(ws.page_setup, "orientation", "") or "").lower() == "landscape"
    page_height_pt = 595.0 if is_landscape else 842.0  # A4 height in points.
    margins = ws.page_margins
    usable_height = page_height_pt - float(margins.top or 0) * 72.0 - float(margins.bottom or 0) * 72.0
    # В итоговом PDF сверху добавляется шапка (~90pt), реальная полезная высота меньше.
    # Без этого оценка страниц занижается, и заголовок фирмы может "перескочить" раньше времени.
    usable_height -= 92.0
    usable_height = max(260.0, usable_height)
    return max(1, math.ceil(total_height / usable_height))


def _fit_sheet_page_counts_to_pdf(
    counts: list[int],
    n_pages: int,
    minimums: list[int] | None = None,
) -> list[int]:
    counts = [max(0, int(c or 0)) for c in counts]
    minimums = [max(0, int(c or 0)) for c in (minimums or [0] * len(counts))]
    if len(minimums) < len(counts):
        minimums.extend([0] * (len(counts) - len(minimums)))
    if not counts or n_pages <= 0:
        return counts
    for idx, minimum in enumerate(minimums[: len(counts)]):
        counts[idx] = max(counts[idx], minimum)
    while sum(counts) > n_pages and any(c > minimums[i] for i, c in enumerate(counts)):
        idx = max(
            (i for i, c in enumerate(counts) if c > minimums[i]),
            key=lambda i: counts[i] - minimums[i],
        )
        counts[idx] -= 1
    spread_idx = 1
    while sum(counts) < n_pages:
        if len(counts) <= 1:
            counts[0] += 1
            continue
        counts[spread_idx] += 1
        spread_idx += 1
        if spread_idx >= len(counts):
            spread_idx = 1
    return counts


def _minimum_sheet_page_count(sheet_title: str, ws=None) -> int:
    # Не форсируем минимальное число страниц: иначе заголовки могут "съезжать"
    # при изменении состава таблиц на листе.
    if ws is not None and _is_detail_sheet_without_data(ws, sheet_title):
        return 0
    return 1


def _headers_for_book_by_row_breaks(
    src: Path,
    n_pages: int,
    per_sheet: list[tuple[str, str]],
    default_header: tuple[str, str],
    no_breaks_extra_pages: str = "first",
) -> list[tuple[str, str]]:
    try:
        from openpyxl import load_workbook

        wb = load_workbook(src, read_only=False, data_only=True)
        counts = []
        minimums = []
        for ws in wb.worksheets:
            title = str(getattr(ws, "title", "") or "").strip()
            minimum = _minimum_sheet_page_count(title, ws)
            if minimum == 0:
                counts.append(0)
                minimums.append(0)
                continue
            count = _estimated_sheet_page_count(ws)
            count = max(count, minimum)
            counts.append(count)
            minimums.append(minimum)
        wb.close()
    except Exception:
        return _headers_for_book(
            [str(i) for i in range(len(per_sheet))],
            n_pages,
            per_sheet,
            default_header,
            extra_pages=no_breaks_extra_pages,
        )

    counts = counts[: len(per_sheet)]
    if n_pages > len(per_sheet) and sum(counts) == len(per_sheet):
        return _headers_for_book(
            [str(i) for i in range(len(per_sheet))],
            n_pages,
            per_sheet,
            default_header,
            extra_pages=no_breaks_extra_pages,
        )

    counts = _fit_sheet_page_counts_to_pdf(counts, n_pages, minimums=minimums)
    headers: list[tuple[str, str]] = []
    for idx, count in enumerate(counts[: len(per_sheet)]):
        headers.extend([per_sheet[idx]] * count)

    if len(headers) < n_pages:
        pad = headers[-1] if headers else default_header
        headers.extend([pad] * (n_pages - len(headers)))
    return headers[:n_pages]


def _resolve_pdf_header_text(src: Path) -> tuple[str, str]:
    report_id = _extract_report_id_from_stem(src.stem)
    # Временные копии вида «._merge_1_<uuid>.xlsx» не парсятся как отчёт — чиним двухстрочную шапку.
    if not report_id and "merge" in src.stem.casefold():
        m_rid = re.search(r"(?:^|[._-])((?:\d+\.\d+)|(?:\d{1,2}))(?=[._-]|$)", src.stem)
        if m_rid:
            report_id = m_rid.group(1)
    multi_work_line = "ВИД РАБОТ: несколько групп (удобрения, обработка почвы, посев, защита, уборка и др.)"
    fixed_headers: dict[str, tuple[str, str]] = {
        "1": ("СВОДКА ХК Крупно", "ВИД РАБОТ: 1.1 Внесение минеральных удобрений"),
        "1.18": ("СВОДКА ХК Крупно (за сутки)", "ВИД РАБОТ: 1.1 Внесение минеральных удобрений"),
        "2": ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", multi_work_line),
        "3": ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", "ВИД РАБОТ: 1.1 Внесение минеральных удобрений"),
        "4": ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", "ВИД РАБОТ: 2.1 Боронование зяби"),
        "5": ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", "ВИД РАБОТ: 4.0 Посев"),
        "6": ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", "ВИД РАБОТ: 3.1 Опрыскивание"),
        "7": ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", "ВИД РАБОТ: 2.9 Дискование"),
        "8": ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", "ВИД РАБОТ: 2.11 Культивация"),
        "9": ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", "ВИД РАБОТ: 2.4 Вспашка, 2.8 Глубокорыхление"),
        "22": ("", ""),
        "10": (_HARVEST_TECH_TITLE, _HARVEST_DEFAULT_WORK_LINE),
        "11": ("ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ", "ВИД РАБОТ: Уборка"),
        "12": ("СТАТИСТИКА ПРОСТОЯ ПРИ УБОРКЕ УРОЖАЯ", ""),
        "13": (_HARVEST_TECH_TITLE, _HARVEST_DEFAULT_WORK_LINE),
        "14": (_HARVEST_TECH_TITLE, _HARVEST_DEFAULT_WORK_LINE),
    }
    if report_id in fixed_headers:
        return fixed_headers[report_id]
    title_raw = re.sub(r"_[0-9]{4}-[0-9]{2}-[0-9]{2}\.(xlsx|xls|pdf)$", "", src.name, flags=re.I)
    title_raw = Path(title_raw).stem.replace("_", " ").strip()
    if re.match(r"^\s*0\.1\s*", title_raw):
        title_raw = re.sub(r"^\s*0\.1\s*", "", title_raw, count=1)
    else:
        title_raw = re.sub(r"^\s*\d+\.\s*", "", title_raw, count=1)
    title_raw = " ".join(title_raw.split())
    return (title_raw or "Сводка", "")


def _safe_original_name(name: str, *, default_ext: str = "pdf") -> str:
    ext = default_ext if default_ext.startswith(".") else f".{default_ext}"
    base = Path(name or f"document{ext}").name
    if not base.lower().endswith(ext.lower()):
        base = f"{Path(base).stem}{ext}"
    out = []
    for ch in base:
        if ch.isalnum() or ch in "._- ":
            out.append(ch)
        else:
            out.append("_")
    safe = "".join(out).strip() or f"document{ext}"
    return safe[:200]


def _store_generated_xlsx(
    db: Session,
    xlsx_path: Path,
    title: str,
    summary_date: date,
    *,
    visible: bool = False,
    report_id: str = "",
    replace_visible_summaries: bool = False,
    requested_report_ids: frozenset[str] | None = None,
) -> Summary:
    rid = str(report_id or _report_id_from_summary_title(title) or "").strip()
    requested_ids = requested_report_ids or frozenset()
    existing_published = get_visible_summary_by_title(db, summary_date, title)
    if existing_published is not None and _keep_published_summary(
        rid,
        replace_visible_summaries=replace_visible_summaries,
        requested_report_ids=requested_ids,
    ):
        return existing_published

    folder = Path(settings.upload_dir) / summary_date.isoformat()
    folder.mkdir(parents=True, exist_ok=True)

    orig = _safe_original_name(xlsx_path.name, default_ext=".xlsx")
    destination = folder / f"{uuid.uuid4().hex}_{orig}"
    shutil.copy2(xlsx_path, destination)

    saved = create_summary(
        db=db,
        title=title,
        summary_date=summary_date,
        file_path=str(destination),
        visible=visible,
    )

    duplicates = list(
        db.scalars(
            select(Summary).where(
                Summary.summary_date == summary_date,
                Summary.title == title,
                Summary.id != saved.id,
            )
        ).all()
    )
    for item in duplicates:
        if item.visible and _keep_published_summary(
            rid,
            replace_visible_summaries=replace_visible_summaries,
            requested_report_ids=requested_ids,
        ):
            continue
        try:
            Path(item.file_path).unlink(missing_ok=True)
        except OSError:
            pass
        db.delete(item)
    if duplicates:
        db.commit()
    return saved


def _store_generated_pdf(
    db: Session,
    pdf_path: Path,
    title: str,
    summary_date: date,
    *,
    visible: bool = False,
    report_id: str = "",
    replace_visible_summaries: bool = False,
    requested_report_ids: frozenset[str] | None = None,
) -> Summary:
    rid = str(report_id or _report_id_from_summary_title(title) or "").strip()
    requested_ids = requested_report_ids or frozenset()
    existing_published = get_visible_summary_by_title(db, summary_date, title)
    if existing_published is not None and _keep_published_summary(
        rid,
        replace_visible_summaries=replace_visible_summaries,
        requested_report_ids=requested_ids,
    ):
        return existing_published

    folder = Path(settings.upload_dir) / summary_date.isoformat()
    folder.mkdir(parents=True, exist_ok=True)

    orig = _safe_original_name(pdf_path.name)
    destination = folder / f"{uuid.uuid4().hex}_{orig}"
    shutil.copy2(pdf_path, destination)

    saved = create_summary(
        db=db,
        title=title,
        summary_date=summary_date,
        file_path=str(destination),
        visible=visible,
    )

    duplicates = list(
        db.scalars(
            select(Summary).where(
                Summary.summary_date == summary_date,
                Summary.title == title,
                Summary.id != saved.id,
            )
        ).all()
    )
    for item in duplicates:
        if item.visible and _keep_published_summary(
            rid,
            replace_visible_summaries=replace_visible_summaries,
            requested_report_ids=requested_ids,
        ):
            continue
        try:
            Path(item.file_path).unlink(missing_ok=True)
        except OSError:
            pass
        db.delete(item)
    if duplicates:
        db.commit()
    return saved
