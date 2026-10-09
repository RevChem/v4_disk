"""Запуск сверки количества машин между сводками.

Работает по готовым файлам, как проверка Cropwise (file_values.py): сводки
заново не считаются и в API за фактами не ходят. Число машин читается из уже
сохранённых .xlsx в uploads/<дата>/ (см. file_machines.py), затем те же файлы
сверяются между собой через build_machine_cross_check (machine_compare.py).

Сводки, у которых файла нет за выбранную дату, пропускаются и попадают в
missing_file_report_ids. Сводка 2 — главная для сверки: число машин берётся из
таблицы «КОЛИЧЕСТВО ЗАДЕЙСТВОВАННЫХ АГРЕГАТОВ», колонка даты отчёта.

Графт сводки 1 здесь не нужен: в файле сводки 1 уже лежит то число, которое
попало в Excel после генерации. apply_report1_donor_area_graft остаётся для
runner.py (сверка по гектарам).
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Callable

from proverka.machine_compare import build_machine_cross_check
from proverka.operation_registry import (
    OPERATION_PEERS,
    peer_reports_for_group,
    peer_reports_for_machine_group,
)
from proverka.reports_config import REPORTS

# Дублирует backend/app/legacy/report_generation.py:REPORT1_FAST_SLAVE_GROUPS_BY_ID.
# Группы, которые в боевой генерации сводки 1 заполняются графтом из донора
# (report_id донора -> имя группы, как в OPERATION_PEERS/display_group_name).
# "9" (Вспашка + Глубокорыхление) сюда намеренно не входит — это отдельный
# лист, который сводка 1 считает сама всегда (см. комментарий в оригинале).
REPORT1_ID = "1"
REPORT1_FAST_SLAVE_GROUPS_BY_ID: dict[str, str] = {
    "4": "2.1 Боронование",
    "5": "4.0 Посев",
    "6": "3.1 Опрыскивание",
    "7": "2.9 Дискование",
    "8": "2.11 Культивация",
}
# Как report_generation.py (report1_fast_groups): при доноре 6 графтятся обе
# операции опрыскивания, не только 3.1 из REPORT1_FAST_SLAVE_GROUPS_BY_ID.
REPORT1_DONOR6_EXTRA_GRAFT_GROUPS: tuple[str, ...] = ("3.2 Опрыскивание (овощи)",)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no")


def _report1_graft_enabled() -> bool:
    """Дублирует backend/app/legacy/report_generation.py:_report1_fast_slave_ids
    (без импорта самого backend-пакета — proverka намеренно от него не зависит).
    Если реальную сводку 1 запускают в standalone-режиме (SVODKI_REPORT1_
    STANDALONE=1), графта в бою нет — тогда и здесь его подставлять не надо.
    """
    if _env_flag("SVODKI_REPORT1_STANDALONE", False):
        return False
    return _env_flag("SVODKI_REPORT1_SKIP_DONOR_PERF_FETCH", True)


def _copy_donor_op_area_fields(donor_op: dict[str, Any], r1_op: dict[str, Any], donor_rid: str) -> None:
    """Гектары за смену — как в PDF сводки 1 (листы доноров 4/5/6/7/8)."""
    import copy

    area = donor_op.get("area_today_by_firm")
    if isinstance(area, dict):
        r1_op["area_today_by_firm"] = copy.deepcopy(area)
    if donor_op.get("total_ha") is not None:
        r1_op["total_ha"] = round(float(donor_op["total_ha"]), 2)
    elif isinstance(area, dict):
        r1_op["total_ha"] = round(sum(float(v or 0) for v in area.values()), 2)
    crop = donor_op.get("area_today_by_firm_crop")
    if isinstance(crop, dict) and crop:
        r1_op["area_today_by_firm_crop"] = copy.deepcopy(crop)
    else:
        r1_op.pop("area_today_by_firm_crop", None)
    r1_op["_grafted_area_from_report_id"] = donor_rid


def apply_report1_donor_area_graft(all_results: dict[str, Any]) -> None:
    """Подставляет area_today/total_ha (и культуры, если есть у донора) в сводку 1."""
    reports = all_results.get("reports") or {}
    r1 = reports.get(REPORT1_ID)
    if not r1 or not _report1_graft_enabled():
        return
    r1_ops = (r1.get("svodka") or {}).get("operations") or {}
    for donor_rid, group_name in REPORT1_FAST_SLAVE_GROUPS_BY_ID.items():
        donor = reports.get(donor_rid)
        if not donor:
            continue
        donor_op = (donor.get("svodka") or {}).get("operations", {}).get(group_name)
        if not donor_op:
            continue
        r1_op = r1_ops.get(group_name)
        if r1_op is None:
            continue
        _copy_donor_op_area_fields(donor_op, r1_op, donor_rid)

    donor6 = reports.get("6")
    if donor6:
        donor6_ops = (donor6.get("svodka") or {}).get("operations") or {}
        for group_name in REPORT1_DONOR6_EXTRA_GRAFT_GROUPS:
            donor_op = donor6_ops.get(group_name)
            if not donor_op:
                continue
            r1_op = r1_ops.get(group_name)
            if r1_op is None:
                continue
            _copy_donor_op_area_fields(donor_op, r1_op, "6")

    import copy

    op_cmp = r1.get("operation_comparisons") or {}
    for gn, r1_op in r1_ops.items():
        if not r1_op.get("_grafted_area_from_report_id"):
            continue
        grafted = copy.deepcopy(r1_op)
        if gn in op_cmp:
            op_cmp[gn]["svodka_op_local"] = grafted
            op_cmp[gn]["svodka_op_r2"] = grafted


def _expand_machine_peer_report_ids(report_ids: list[str]) -> list[str]:
    """Добавляет peer-сводки для machine cross-check (напр. 14 для 5.0 Уборка)."""
    present = set(report_ids)
    needed = set(present)
    for group_name in OPERATION_PEERS:
        peers = set(peer_reports_for_machine_group(group_name))
        if present & peers:
            needed |= peers
    return sorted(
        (rid for rid in needed if rid in REPORTS),
        key=lambda x: (len(x), x),
    )


class MachineProverkaCancelled(Exception):
    pass


def run_machine_proverka_batch(
    report_date: date,
    report_ids: list[str],
    out_dir: Path,
    *,
    write_json: bool = True,
    progress: Callable[[str], None] | None = None,
    should_cancel_report: Callable[[str | None], bool] | None = None,
) -> dict[str, Any]:
    """Сверка числа машин по готовым .xlsx сводок за одну дату."""
    from proverka.file_machines import (
        REPORTS_WITHOUT_MACHINES_IN_FILE,
        MachineFileMissing,
        machines_from_path,
    )
    from proverka.file_values import find_report_xlsx

    def prog(msg: str) -> None:
        if progress:
            progress(msg)

    if should_cancel_report and should_cancel_report(None):
        raise MachineProverkaCancelled("cancelled")

    report_ids = _expand_machine_peer_report_ids(report_ids)
    unknown = [rid for rid in report_ids if rid not in REPORTS]
    known_ids = [rid for rid in report_ids if rid in REPORTS]

    prog("Чтение готовых Excel…")

    all_results: dict[str, Any] = {"reports": {}, "report_date": report_date.isoformat()}
    cancelled_ids: list[str] = []
    file_report_ids: list[str] = []
    missing_files: list[str] = []
    no_machine_data: list[str] = []
    read_errors: dict[str, str] = {}
    for rid in known_ids:
        if should_cancel_report and should_cancel_report(rid):
            cancelled_ids.append(rid)
            continue
        if rid in REPORTS_WITHOUT_MACHINES_IN_FILE:
            no_machine_data.append(rid)
            continue
        prog(f"Сводка {rid}: {REPORTS[rid].title} (файл Excel)")
        path = find_report_xlsx(report_date, rid)
        if path is None:
            missing_files.append(rid)
            continue
        try:
            by_group = machines_from_path(path, rid, report_date)
        except MachineFileMissing:
            missing_files.append(rid)
            continue
        except Exception as exc:  # noqa: BLE001
            read_errors[rid] = str(exc)
            prog(f"Сводка {rid}: файл не прочитан ({exc})")
            continue
        operations = {
            group: {
                "machines_today_by_firm": dict(machines),
                "total_machines": sum(machines.values()),
            }
            for group, machines in by_group.items()
        }
        if not operations:
            no_machine_data.append(rid)
            continue
        file_report_ids.append(rid)
        all_results["reports"][rid] = {
            "report_id": rid,
            "svodka": {"operations": operations},
            "file": path.name,
        }

    prog("Сверка количества машин…")
    machine_cross_check = build_machine_cross_check(all_results)
    all_results["machine_cross_check"] = machine_cross_check

    batch_path = out_dir / f"reconcile_machines_all_{report_date.isoformat()}.json"
    if write_json:
        out_dir.mkdir(parents=True, exist_ok=True)
        batch_path.write_text(
            json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    return {
        "report_date": report_date.isoformat(),
        "report_ids": known_ids,
        "unknown_report_ids": unknown,
        "cancelled_report_ids": cancelled_ids,
        "file_report_ids": file_report_ids,
        "missing_file_report_ids": missing_files,
        "no_machine_data_report_ids": no_machine_data,
        "file_read_errors": read_errors,
        "batch_json": str(batch_path) if write_json else None,
        "machine_cross_check": machine_cross_check,
        "machine_cross_check_rows": machine_cross_check.get("operations") or [],
    }
