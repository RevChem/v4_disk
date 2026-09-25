"""Запуск скриптов svodki/plan и сохранение XLSX/PDF на диск (без БД)."""
from __future__ import annotations

import gc
import json
import re
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.config import settings
from app.legacy.report_generation import (
    ReportGenerationCancelled,
    ReportGenerationError,
    _cfg_id_norm,
    _configs_for_ordered_ids,
    _convert_xlsx_to_pdf,
    _dedicated_heavy_report_lanes_enabled,
    _donor_retry_rounds,
    _env_flag,
    _failed_report_retry_rounds,
    _max_concurrent_report_workers,
    _maybe_add_report2_for_r23,
    _maybe_add_report27_1,
    _prepare_ordered_report_ids,
    _report1_required_donor_ids,
    _report_execution_waves,
    _resolve_report_config,
    _resolve_scripts_dirs,
    _resolve_storage_title,
    _resolve_svodki_root,
    _reorder_report1_workbook_file,
    _run_api_data_prefetch,
    _run_report_script,
    _split_report_execution_lanes,
    _uses_unified_prefetch_data,
    USE_API_CACHE_ENV_VAR,
    USE_PREFETCH_DATA_ENV_VAR,
)

_REPORT1_FAMILY_IDS = frozenset({"1", "1.18"})


_DURATIONS_FILE = "generation_durations.json"
_JOB_PROGRESS_DIR = ".generation_job_progress"


def _job_progress_path(job_id: str) -> Path:
    folder = Path(settings.upload_dir) / _JOB_PROGRESS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{job_id}.json"


def init_job_progress(job_id: str) -> None:
    path = _job_progress_path(job_id)
    path.write_text(json.dumps({"completed_report_ids": []}, ensure_ascii=False), encoding="utf-8")


def record_job_report_completed(job_id: str | None, report_id: str) -> None:
    jid = str(job_id or "").strip()
    rid = str(report_id or "").strip()
    if not jid or not rid:
        return
    path = _job_progress_path(jid)
    data: dict[str, list[str]] = {"completed_report_ids": []}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("completed_report_ids"), list):
                data["completed_report_ids"] = [str(x).strip() for x in raw["completed_report_ids"] if str(x).strip()]
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    if rid not in data["completed_report_ids"]:
        data["completed_report_ids"].append(rid)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def list_job_completed_report_ids(job_id: str, report_ids: list[str] | None = None) -> list[str]:
    jid = str(job_id or "").strip()
    if not jid:
        return []
    path = _job_progress_path(jid)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    done = [str(x).strip() for x in (raw.get("completed_report_ids") or []) if str(x).strip()]
    if not report_ids:
        return done
    wanted = {str(x).strip() for x in report_ids if str(x).strip()}
    return [rid for rid in done if rid in wanted]


def clear_job_progress(job_id: str) -> None:
    path = _job_progress_path(job_id)
    if path.is_file():
        path.unlink(missing_ok=True)


@dataclass
class GeneratedFile:
    report_id: str
    title: str
    summary_date: date
    xlsx_path: Path | None
    pdf_path: Path | None
    duration_seconds: float | None = None


def _durations_path(summary_date: date) -> Path:
    return _output_folder(summary_date) / _DURATIONS_FILE


def record_report_duration(summary_date: date, report_id: str, seconds: float) -> None:
    rid = str(report_id or "").strip()
    if not rid or seconds < 0:
        return
    path = _durations_path(summary_date)
    data: dict[str, float] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {str(k): float(v) for k, v in raw.items() if str(k).strip()}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            data = {}
    data[rid] = round(float(seconds), 1)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_report_durations(summary_date: date) -> dict[str, float]:
    path = _durations_path(summary_date)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, val in raw.items():
        rid = str(key or "").strip()
        if not rid:
            continue
        try:
            out[rid] = round(float(val), 1)
        except (TypeError, ValueError):
            continue
    return out


def list_completed_report_ids(summary_date: date, report_ids: list[str] | None = None) -> list[str]:
    folder = _output_folder(summary_date)
    if not folder.is_dir():
        return []
    existing: set[str] = set()
    for path in folder.iterdir():
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        rid = _report_id_from_filename(path.name)
        if rid:
            existing.add(rid)
    if not report_ids:
        return sorted(existing, key=lambda s: _report_sort_key({"report_id": s}))
    return [rid for rid in report_ids if rid in existing]


def _output_folder(summary_date: date) -> Path:
    folder = Path(settings.upload_dir) / summary_date.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _safe_filename(title: str, ext: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title.strip())[:180]
    if not stem.lower().endswith(ext.lower()):
        stem = f"{stem}{ext}"
    return stem


def _report_id_from_title(title: str) -> str:
    m = re.match(r"^(\d+(?:\.\d+)?)", str(title or "").strip())
    return m.group(1) if m else ""


def _report_id_from_filename(filename: str) -> str:
    stem = Path(filename).name
    if re.match(r"^[0-9a-f]{8}_", stem, re.IGNORECASE):
        stem = stem.split("_", 1)[1]
    stem = Path(stem).stem
    return _report_id_from_title(stem)


def _purge_same_report_files(folder: Path, report_id: str, *, ext: str | None = None) -> None:
    """Удаляет старые файлы отчёта report_id в folder.

    Если ext не задан — чистит и .pdf, и .xlsx (обратная совместимость).
    Если задан — чистит только файлы с этим расширением, чтобы сохранение
    одного формата (например, .xlsx) не удаляло только что сохранённый
    другой формат (.pdf) того же отчёта, и наоборот.
    """
    if not report_id or not folder.is_dir():
        return
    exts = {ext.lower()} if ext else {".pdf", ".xlsx"}
    for path in list(folder.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in exts:
            continue
        if _report_id_from_filename(path.name) == report_id:
            path.unlink(missing_ok=True)


def _save_artifact(src: Path, summary_date: date, title: str, ext: str, report_id: str = "") -> Path:
    folder = _output_folder(summary_date)
    rid = report_id or _report_id_from_title(title)
    _purge_same_report_files(folder, rid, ext=ext)
    dest = folder / _safe_filename(title, ext)
    shutil.copy2(src, dest)
    return dest


def _execute_single_report_to_disk(
    cfg,
    *,
    summary_date: date,
    request_report_date: date,
    logo_root: Path,
    temp_root: Path,
    api_snapshot_path: Path | None,
    prefetch_data_path: Path | None,
    plan_script_dir: Path | None,
    api_snapshot_read_only: bool,
    generation_job_id: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[GeneratedFile | None, dict[str, str] | None, list[str]]:
    """Потокобезопасный запуск одной сводки (локальные логи, без общего списка)."""
    local_logs: list[str] = []
    cfg_id = _cfg_id_norm(cfg)
    storage_title = _resolve_storage_title(cfg, summary_date)
    report_temp = temp_root / f"{cfg_id}_{uuid.uuid4().hex}"
    report_temp.mkdir(parents=True, exist_ok=True)

    report_started = time.monotonic()
    try:
        local_logs.append(f"[{cfg.title}] Старт")
        script_started = time.monotonic()
        xlsx_path, stdout = _run_report_script(
            cfg=cfg,
            plan_script_dir=plan_script_dir,
            output_dir=report_temp,
            report_date=request_report_date,
            api_snapshot_path=api_snapshot_path,
            prefetch_data_path=prefetch_data_path,
            api_snapshot_read_only=api_snapshot_read_only,
            generation_job_id=generation_job_id,
            should_cancel=should_cancel,
        )
        local_logs.append(
            f"[{cfg.title}] Время скрипта XLSX: {time.monotonic() - script_started:.1f} с"
        )
        if cfg_id in _REPORT1_FAMILY_IDS:
            _reorder_report1_workbook_file(xlsx_path, local_logs, report_label=cfg_id)

        saved_pdf: Path | None = None
        pdf_started = time.monotonic()
        pdf_path = _convert_xlsx_to_pdf(
            src=xlsx_path,
            output_dir=report_temp,
            logo_root=logo_root,
            summary_date=summary_date,
        )
        saved_pdf = _save_artifact(pdf_path, summary_date, storage_title, ".pdf", report_id=cfg_id)
        local_logs.append(f"[{cfg.title}] Время PDF: {time.monotonic() - pdf_started:.1f} с")
        local_logs.append(f"[{cfg.title}] PDF: {saved_pdf.name}")

        saved_xlsx: Path | None = None
        try:
            saved_xlsx = _save_artifact(xlsx_path, summary_date, storage_title, ".xlsx", report_id=cfg_id)
            local_logs.append(f"[{cfg.title}] XLSX: {saved_xlsx.name}")
        except Exception as exc:
            # Наличие xlsx рядом с pdf — приятный бонус (нужен, в частности, для
            # сверки «В файле» в proverka), но не должен ронять генерацию отчёта.
            local_logs.append(f"[{cfg.title}] Не удалось сохранить XLSX: {exc}")

        if stdout.strip():
            local_logs.extend(
                f"[{cfg.title}] {line}" for line in stdout.splitlines()[-5:] if line.strip()
            )

        duration_seconds = round(time.monotonic() - report_started, 1)
        record_report_duration(summary_date, cfg_id, duration_seconds)
        record_job_report_completed(generation_job_id, cfg_id)
        local_logs.append(f"[{cfg.title}] Общее время отчёта: {duration_seconds:.1f} с")
        item = GeneratedFile(
            report_id=cfg_id,
            title=storage_title,
            summary_date=summary_date,
            xlsx_path=saved_xlsx,
            pdf_path=saved_pdf,
            duration_seconds=duration_seconds,
        )
        return item, None, local_logs
    except ReportGenerationCancelled:
        local_logs.append(f"[{cfg.title}] Отменено пользователем")
        return (
            None,
            {
                "report_id": cfg_id,
                "title": cfg.title,
                "error": "cancelled",
                "cancelled": "1",
            },
            local_logs,
        )
    except Exception as exc:
        local_logs.append(f"[{cfg.title}] Ошибка: {exc}")
        return (
            None,
            {
                "report_id": cfg_id,
                "title": cfg.title,
                "error": str(exc) or exc.__class__.__name__,
            },
            local_logs,
        )


def run_single_report_to_disk(
    cfg,
    *,
    summary_date: date,
    request_report_date: date,
    logo_root: Path,
    temp_root: Path,
    api_snapshot_path: Path | None,
    prefetch_data_path: Path | None,
    plan_script_dir: Path | None,
    logs: list[str],
) -> GeneratedFile:
    item, failed, local_logs = _execute_single_report_to_disk(
        cfg,
        summary_date=summary_date,
        request_report_date=request_report_date,
        logo_root=logo_root,
        temp_root=temp_root,
        api_snapshot_path=api_snapshot_path,
        prefetch_data_path=prefetch_data_path,
        plan_script_dir=plan_script_dir,
        api_snapshot_read_only=False,
    )
    logs.extend(local_logs)
    if failed is not None:
        raise ReportGenerationError(failed.get("error") or "report_failed")
    assert item is not None
    return item


def _run_disk_cfgs_sequential(
    seq_cfgs: list,
    *,
    summary_date: date,
    request_report_date: date,
    logo_root: Path,
    temp_root: Path,
    api_snapshot_path: Path | None,
    prefetch_data_path: Path | None,
    plan_script_dir: Path | None,
    api_snapshot_read_only: bool,
    generation_job_id: str | None = None,
    is_cancelled: Callable[[str], bool] | None = None,
    cancelled: list[dict[str, str]] | None = None,
) -> tuple[list[GeneratedFile], list[dict[str, str]], list[str]]:
    created: list[GeneratedFile] = []
    failed: list[dict[str, str]] = []
    logs: list[str] = []
    for cfg in seq_cfgs:
        cfg_id = _cfg_id_norm(cfg)
        if is_cancelled is not None and is_cancelled(cfg_id):
            logs.append(f"[{cfg.title}] Пропуск: отменено")
            if cancelled is not None:
                cancelled.append({"report_id": cfg_id, "title": cfg.title})
            continue
        should_cancel = (
            (lambda rid=cfg_id: is_cancelled(rid)) if is_cancelled is not None else None
        )
        item, fail, plogs = _execute_single_report_to_disk(
            cfg,
            summary_date=summary_date,
            request_report_date=request_report_date,
            logo_root=logo_root,
            temp_root=temp_root,
            api_snapshot_path=api_snapshot_path,
            prefetch_data_path=prefetch_data_path,
            plan_script_dir=plan_script_dir,
            api_snapshot_read_only=api_snapshot_read_only,
            generation_job_id=generation_job_id,
            should_cancel=should_cancel,
        )
        logs.extend(plogs)
        if item is not None:
            created.append(item)
        if fail is not None:
            if fail.get("cancelled"):
                if cancelled is not None:
                    cancelled.append(
                        {"report_id": fail.get("report_id") or cfg_id, "title": fail.get("title") or cfg.title}
                    )
            else:
                failed.append(fail)
    return created, failed, logs


def _execute_disk_reports_parallel(
    cfgs: list,
    *,
    summary_date: date,
    request_report_date: date,
    logo_root: Path,
    temp_root: Path,
    api_snapshot_path: Path | None,
    prefetch_data_path: Path | None,
    plan_script_dir: Path | None,
    api_snapshot_read_only: bool,
    max_workers: int,
    report1_fast_slave_ids: frozenset[str],
    generation_job_id: str | None = None,
    is_cancelled: Callable[[str], bool] | None = None,
    cancelled: list[dict[str, str]] | None = None,
) -> tuple[list[GeneratedFile], list[dict[str, str]], list[str]]:
    created: list[GeneratedFile] = []
    failed: list[dict[str, str]] = []
    logs: list[str] = []
    cfg_by_id = {_cfg_id_norm(c): c for c in cfgs}

    def _worker(cfg) -> tuple[GeneratedFile | None, dict[str, str] | None, list[str]]:
        cfg_id = _cfg_id_norm(cfg)
        if is_cancelled is not None and is_cancelled(cfg_id):
            return (
                None,
                {
                    "report_id": cfg_id,
                    "title": cfg.title,
                    "error": "cancelled",
                    "cancelled": "1",
                    "skipped": "1",
                },
                [f"[{cfg.title}] Пропуск: отменено"],
            )
        should_cancel = (
            (lambda rid=cfg_id: is_cancelled(rid)) if is_cancelled is not None else None
        )
        return _execute_single_report_to_disk(
            cfg,
            summary_date=summary_date,
            request_report_date=request_report_date,
            logo_root=logo_root,
            temp_root=temp_root,
            api_snapshot_path=api_snapshot_path,
            prefetch_data_path=prefetch_data_path,
            plan_script_dir=plan_script_dir,
            api_snapshot_read_only=api_snapshot_read_only,
            generation_job_id=generation_job_id,
            should_cancel=should_cancel,
        )

    def _run_parallel_waves(
        lane_cfgs: list,
        *,
        lane_workers: int,
        lane_label: str,
        donor_retry: bool,
        out_created: list[GeneratedFile],
        out_failed: list[dict[str, str]],
        out_logs: list[str],
    ) -> None:
        if not lane_cfgs:
            return
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
            if not wave_cfgs:
                continue
            if len(waves) > 1:
                prefix = f"{lane_label}, " if lane_label else ""
                out_logs.append(
                    f"{prefix}этап {wave_ix + 1}/{len(waves)}: "
                    + ", ".join(_cfg_id_norm(c) for c in wave_cfgs)
                )
            wave_cfgs = [
                cfg
                for cfg in wave_cfgs
                if is_cancelled is None or not is_cancelled(_cfg_id_norm(cfg))
            ]
            if not wave_cfgs:
                continue
            mw = max(1, min(int(lane_workers), len(wave_cfgs)))
            executor = ThreadPoolExecutor(max_workers=mw)
            try:
                futures = {executor.submit(_worker, cfg): cfg for cfg in wave_cfgs}
                for future in as_completed(futures):
                    item, fail, plogs = future.result()
                    out_logs.extend(plogs)
                    if item is not None:
                        out_created.append(item)
                    elif fail is not None:
                        if fail.get("cancelled"):
                            if cancelled is not None:
                                cancelled.append(
                                    {
                                        "report_id": str(fail.get("report_id") or ""),
                                        "title": str(fail.get("title") or ""),
                                    }
                                )
                        else:
                            out_failed.append(fail)
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            next_wave_is_r1 = (
                wave_ix + 1 < len(waves)
                and any(_cfg_id_norm(c) in _REPORT1_FAMILY_IDS for c in waves[wave_ix + 1])
            )
            donor_rounds = _donor_retry_rounds()
            if next_wave_is_r1 and donor_retry and report1_fast_slave_ids and donor_rounds > 0:
                def _lane_created_ids() -> set[str]:
                    return {f.report_id for f in out_created}

                for donor_round in range(1, donor_rounds + 1):
                    missing = sorted(report1_fast_slave_ids - _lane_created_ids())
                    if not missing:
                        break
                    retry_cfgs = [cfg_by_id[rid] for rid in missing if rid in cfg_by_id]
                    if not retry_cfgs:
                        break
                    out_logs.append(
                        "Сводка 1 fast-path: повтор доноров "
                        f"(раунд {donor_round}/{donor_rounds}, последовательно): "
                        + ", ".join(missing)
                    )
                    gc.collect()
                    retried = set(missing)
                    out_failed[:] = [
                        f for f in out_failed if str(f.get("report_id") or "") not in retried
                    ]
                    rc, rf, rl = _run_disk_cfgs_sequential(
                        retry_cfgs,
                        summary_date=summary_date,
                        request_report_date=request_report_date,
                        logo_root=logo_root,
                        temp_root=temp_root,
                        api_snapshot_path=api_snapshot_path,
                        prefetch_data_path=prefetch_data_path,
                        plan_script_dir=plan_script_dir,
                        api_snapshot_read_only=api_snapshot_read_only,
                        generation_job_id=generation_job_id,
                        is_cancelled=is_cancelled,
                        cancelled=cancelled,
                    )
                    out_logs.extend(rl)
                    out_created.extend(rc)
                    out_failed.extend(rf)

    use_lanes = _dedicated_heavy_report_lanes_enabled()
    bulk_cfgs, r1_lane_cfgs, r2_lane_cfgs, r1_isolated = _split_report_execution_lanes(
        cfgs,
        report1_fast_slave_ids=report1_fast_slave_ids,
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
        lane_jobs: list[tuple[str, list, int, bool]] = []
        if bulk_cfgs:
            lane_jobs.append(("bulk", bulk_cfgs, int(max_workers), False))
        if r1_lane_cfgs:
            lane_jobs.append(("сводка 1", r1_lane_cfgs, 1, True))
        if r2_lane_cfgs:
            lane_jobs.append(("сводка 2", r2_lane_cfgs, 1, False))

        lane_pool = ThreadPoolExecutor(max_workers=len(lane_jobs))

        def _lane_job(
            label: str,
            lane_cfgs: list,
            workers: int,
            donor_retry: bool,
        ) -> tuple[list[GeneratedFile], list[dict[str, str]], list[str]]:
            lc: list[GeneratedFile] = []
            lf: list[dict[str, str]] = []
            ll: list[str] = []
            _run_parallel_waves(
                lane_cfgs,
                lane_workers=workers,
                lane_label=label,
                donor_retry=donor_retry,
                out_created=lc,
                out_failed=lf,
                out_logs=ll,
            )
            return lc, lf, ll

        try:
            futures = {
                lane_pool.submit(_lane_job, label, lane_cfgs, workers, donor_retry): label
                for label, lane_cfgs, workers, donor_retry in lane_jobs
            }
            for future in as_completed(futures):
                lc, lf, ll = future.result()
                logs.extend(ll)
                created.extend(lc)
                failed.extend(lf)
        finally:
            lane_pool.shutdown(wait=False, cancel_futures=True)
    else:
        _run_parallel_waves(
            cfgs,
            lane_workers=int(max_workers),
            lane_label="",
            donor_retry=bool(report1_fast_slave_ids),
            out_created=created,
            out_failed=failed,
            out_logs=logs,
        )

    if is_cancelled is not None and is_cancelled(None):
        logs.append("Генерация прервана: отмена всей задачи")
        return created, failed, logs

    failed_retry_rounds = _failed_report_retry_rounds()
    for failed_round in range(1, failed_retry_rounds + 1):
        if not failed:
            break
        if is_cancelled is not None and is_cancelled(None):
            break
        failed_ids: list[str] = []
        seen: set[str] = set()
        for item in failed:
            rid = str(item.get("report_id") or "").strip()
            if rid and rid not in seen and rid in cfg_by_id:
                seen.add(rid)
                failed_ids.append(rid)
        if not failed_ids:
            break
        retry_cfgs = [cfg_by_id[rid] for rid in failed_ids]
        logs.append(
            "Повтор упавших сводок (последовательно, раунд "
            f"{failed_round}/{failed_retry_rounds}): "
            + ", ".join(failed_ids)
        )
        gc.collect()
        time.sleep(10.0 * failed_round)
        failed[:] = [f for f in failed if str(f.get("report_id") or "") not in set(failed_ids)]
        rc, rf, rl = _run_disk_cfgs_sequential(
            retry_cfgs,
            summary_date=summary_date,
            request_report_date=request_report_date,
            logo_root=logo_root,
            temp_root=temp_root,
            api_snapshot_path=api_snapshot_path,
            prefetch_data_path=prefetch_data_path,
            plan_script_dir=plan_script_dir,
            api_snapshot_read_only=api_snapshot_read_only,
            generation_job_id=generation_job_id,
            is_cancelled=is_cancelled,
            cancelled=cancelled,
        )
        logs.extend(rl)
        created.extend(rc)
        failed.extend(rf)

    if report1_fast_slave_ids:
        def _created_ids() -> set[str]:
            return {f.report_id for f in created}

        for rescue_round in range(1, _donor_retry_rounds() + 1):
            missing = sorted(report1_fast_slave_ids - _created_ids())
            if not missing:
                break
            rescue_cfgs = [cfg_by_id[rid] for rid in missing if rid in cfg_by_id]
            if not rescue_cfgs:
                break
            logs.append(
                "Финальный повтор доноров сводки 1 "
                f"(раунд {rescue_round}/{_donor_retry_rounds()}, последовательно): "
                + ", ".join(missing)
            )
            gc.collect()
            time.sleep(15.0 * rescue_round)
            failed[:] = [f for f in failed if str(f.get("report_id") or "") not in set(missing)]
            rc, rf, rl = _run_disk_cfgs_sequential(
                rescue_cfgs,
                summary_date=summary_date,
                request_report_date=request_report_date,
                logo_root=logo_root,
                temp_root=temp_root,
                api_snapshot_path=api_snapshot_path,
                prefetch_data_path=prefetch_data_path,
                plan_script_dir=plan_script_dir,
                api_snapshot_read_only=api_snapshot_read_only,
                generation_job_id=generation_job_id,
                is_cancelled=is_cancelled,
                cancelled=cancelled,
            )
            logs.extend(rl)
            created.extend(rc)
            failed.extend(rf)

        missing = sorted(report1_fast_slave_ids - _created_ids())
        if not missing:
            r1_failed = [
                str(f.get("report_id") or "")
                for f in failed
                if str(f.get("report_id") or "") in _REPORT1_FAMILY_IDS
            ]
            if r1_failed:
                r1_cfgs = [cfg_by_id[rid] for rid in r1_failed if rid in cfg_by_id]
                if r1_cfgs:
                    logs.append(
                        "Повтор сводки 1 после успешных доноров: " + ", ".join(r1_failed)
                    )
                    failed[:] = [
                        f for f in failed if str(f.get("report_id") or "") not in set(r1_failed)
                    ]
                    rc, rf, rl = _run_disk_cfgs_sequential(
                        r1_cfgs,
                        summary_date=summary_date,
                        request_report_date=request_report_date,
                        logo_root=logo_root,
                        temp_root=temp_root,
                        api_snapshot_path=api_snapshot_path,
                        prefetch_data_path=prefetch_data_path,
                        plan_script_dir=plan_script_dir,
                        api_snapshot_read_only=api_snapshot_read_only,
                        generation_job_id=generation_job_id,
                        is_cancelled=is_cancelled,
                        cancelled=cancelled,
                    )
                    logs.extend(rl)
                    created.extend(rc)
                    failed.extend(rf)

    return created, failed, logs


def generate_reports_to_disk(
    summary_date: date,
    report_ids: list[str] | None = None,
    *,
    generation_job_id: str | None = None,
    is_cancelled: Callable[[str], bool] | None = None,
    cancelled: list[dict[str, str]] | None = None,
) -> tuple[list[GeneratedFile], list[dict[str, str]], list[str]]:
    """Запускает скрипты из svodki/plan и сохраняет PDF (или Excel) в uploads/<date>/."""
    svodki_root = _resolve_svodki_root()
    primary_script_dir, plan_script_dir = _resolve_scripts_dirs(svodki_root)
    logo_root = svodki_root
    request_report_date = summary_date
    user_ordered_ids = _maybe_add_report27_1(_prepare_ordered_report_ids(report_ids))
    ordered_ids, _r23_auto_report2 = _maybe_add_report2_for_r23(user_ordered_ids, request_report_date)
    ordered_ids = _maybe_add_report27_1(ordered_ids)
    selected = _configs_for_ordered_ids(ordered_ids)
    report1_fast_slave_ids = _report1_required_donor_ids(ordered_ids)

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
    prefetch_data_path: Path | None = None

    logs: list[str] = [
        f"Дата: {summary_date.isoformat()}",
        f"Скрипты: {primary_script_dir}",
        f"Отчёты: {', '.join(ordered_ids)}",
    ]
    if use_api_cache:
        logs.append(f"JSON-кэш GET (sitecustomize): {api_snapshot_path}")
    else:
        logs.append(
            "Режим: каждая сводка загружает данные из Cropwise самостоятельно "
            "(без svodki_prefetch и без общего JSON-снимка)."
        )
    created: list[GeneratedFile] = []
    failed: list[dict[str, str]] = []

    try:
        resolved = []
        for base_cfg in selected:
            try:
                resolved.append(_resolve_report_config(primary_script_dir, plan_script_dir, base_cfg))
            except Exception as exc:
                failed.append(
                    {
                        "report_id": base_cfg.id,
                        "title": base_cfg.title,
                        "error": str(exc),
                    }
                )
                logs.append(f"[{base_cfg.title}] Ошибка подготовки: {exc}")

        prefetch_ready = False
        if _env_flag(USE_PREFETCH_DATA_ENV_VAR, False) and any(
            _uses_unified_prefetch_data(c) for c in resolved
        ):
            prefetch_data_path = snapshot_root / (
                f"cropwise_prefetch_{summary_date.isoformat()}_{uuid.uuid4().hex}.json"
            )
            logs.extend(
                _run_api_data_prefetch(
                    primary_script_dir=primary_script_dir,
                    plan_script_dir=plan_script_dir,
                    svodki_root=svodki_root,
                    output_path=prefetch_data_path,
                    report_date=request_report_date,
                    report_ids=ordered_ids,
                    report1_fast_slave_ids=report1_fast_slave_ids,
                )
            )
            prefetch_ready = True
        elif _env_flag(USE_PREFETCH_DATA_ENV_VAR, False):
            logs.append("Prefetch JSON отключён для выбранных сводок.")
        else:
            logs.append("Prefetch JSON отключён (SVODKI_USE_PREFETCH_DATA=0).")

        worker_cap = _max_concurrent_report_workers()
        logs.append(f"Параллельных сводок (bulk): {worker_cap}")

        batch_created, batch_failed, batch_logs = _execute_disk_reports_parallel(
            resolved,
            summary_date=summary_date,
            request_report_date=request_report_date,
            logo_root=logo_root,
            temp_root=temp_root,
            api_snapshot_path=api_snapshot_path,
            prefetch_data_path=prefetch_data_path if prefetch_ready else None,
            plan_script_dir=plan_script_dir,
            api_snapshot_read_only=prefetch_ready,
            max_workers=worker_cap,
            report1_fast_slave_ids=report1_fast_slave_ids,
            generation_job_id=generation_job_id,
            is_cancelled=is_cancelled,
            cancelled=cancelled,
        )
        created.extend(batch_created)
        failed.extend(batch_failed)
        logs.extend(batch_logs)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    cancelled_count = len(cancelled or [])
    logs.append(
        f"Готово: {len(created)} файлов, ошибок: {len(failed)}"
        + (f", отменено: {cancelled_count}" if cancelled_count else "")
    )
    return created, failed, logs


def convert_xlsx_file_to_pdf(
    src: Path,
    output_dir: Path | None = None,
    *,
    summary_date: date | None = None,
) -> Path:
    """Конвертирует готовый Excel в PDF с шапкой (LibreOffice + reportlab)."""
    svodki_root = _resolve_svodki_root()
    out_dir = output_dir or src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    return _convert_xlsx_to_pdf(
        src=src,
        output_dir=out_dir,
        logo_root=svodki_root,
        summary_date=summary_date,
    )


def _display_title_from_filename(filename: str, summary_date: date | None = None) -> str:
    stem = Path(filename).name
    if re.match(r"^[0-9a-f]{8}_", stem, re.IGNORECASE):
        stem = stem.split("_", 1)[1]
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    rid = _report_id_from_title(stem)
    if rid:
        short = rid + ".Сводка"
        if summary_date:
            return f"{short} · {summary_date.isoformat()}"
        return short
    return stem or filename


def _report_sort_key(item: dict[str, str]) -> tuple:
    rid = item.get("report_id") or _report_id_from_filename(item.get("filename") or "")
    if not rid:
        return (9999, 0, item.get("filename") or "")
    parts = rid.split(".")
    nums: list[int] = []
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 2:
        nums.append(0)
    return tuple(nums)


def list_generated_files(summary_date: date) -> list[dict[str, str]]:
    folder = _output_folder(summary_date)
    durations = load_report_durations(summary_date)
    items: list[dict[str, str]] = []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() != ".pdf":
            continue
        stat = path.stat()
        rid = _report_id_from_filename(path.name)
        row = {
            "filename": path.name,
            "title": _display_title_from_filename(path.name, summary_date),
            "report_id": rid,
            "format": "pdf",
            "size_bytes": str(stat.st_size),
            "modified_at": str(int(stat.st_mtime)),
        }
        if rid and rid in durations:
            row["generation_seconds"] = str(durations[rid])
        items.append(row)
    items.sort(key=_report_sort_key)
    return items


def list_available_dates() -> list[dict[str, str | int]]:
    root = Path(settings.upload_dir)
    if not root.is_dir():
        return []
    out: list[dict[str, str | int]] = []
    for child in sorted(root.iterdir(), reverse=True):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        pdf_count = sum(1 for p in child.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
        if pdf_count == 0:
            continue
        out.append({"summary_date": child.name, "count": pdf_count})
    return out
