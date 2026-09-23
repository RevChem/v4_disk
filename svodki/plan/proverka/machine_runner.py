"""Запуск сверки количества машин между сводками.

Полностью независим от runner.py (сверка по гектарам / Cropwise UI):
свой цикл, свой результат, свой JSON-файл на диске. Единственное общее —
проверяемые сводки берутся из того же report_catalog / OPERATION_PEERS.

ЭТАП 1 (сейчас): источник факта по машинам — ЗАГЛУШКА (см. _stub_machines_
today_by_firm ниже). Она нужна, чтобы обкатать весь путь целиком (job →
API → фронт), не дожидаясь, пока каждая сводка (1, 2, 10, 11, …) научится
реально считать уникальные machine_id за смену.

ЭТАП 2 (следующим шагом, по одной сводке за раз): подставить сюда реальные
данные — читать machines_today_by_firm, которое будет отдавать сама сводка
(через extractors.py, по аналогии с area_today_by_firm), вместо стаба.
Форма данных (operations[group_name]["machines_today_by_firm"]) уже
рассчитана на это — при переходе на реальные факты сам build_machine_
cross_check() в machine_compare.py менять не придётся.

ГРАФТ СВОДКИ 1 (см. _apply_report1_donor_graft ниже): в боевой генерации
отчётов сводка 1 для части операций (боронование, посев, опрыскивание,
дискование, культивация) НЕ считает машины сама — при совместном запуске
с донором (сводки 4/5/6/7/8) она получает готовое число графтом/PDF-page
graft (см. backend/app/legacy/report_generation.py:
REPORT1_FAST_SLAVE_GROUPS_BY_ID / _report1_fast_slave_ids,
SVODKI_REPORT1_SKIP_DONOR_PERF_FETCH=1 по умолчанию). extractors.py же
всегда пересчитывает сводку 1 полностью самостоятельно (через
_fetch_other_group_stats), другим кодом, с другими порогами и окном —
и для графт-групп это число никогда не показывается в реальном PDF.
Раньше сверка на этом ловила ложный DIFF. Ниже перед сверкой те же
группы у отчёта "1" заменяются на уже посчитанные числа донора из этой
же пачки — так сверка сравнивает именно то, что реально попадает в PDF
сводки 1.
"""
from __future__ import annotations

import asyncio
import hashlib
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


def _apply_report1_donor_graft(all_results: dict[str, Any]) -> None:
    """Подставляет в результат сводки 1 machines_today_by_firm донора для
    групп, которые в реальной генерации отчётов заполняются графтом, а не
    собственным расчётом сводки 1 (см. REPORT1_FAST_SLAVE_GROUPS_BY_ID).
    Ничего не делает, если сводки 1 нет в батче, донора нет в батче, или
    графт в этой конфигурации выключен (см. _report1_graft_enabled).
    """
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
        machines = dict(donor_op.get("machines_today_by_firm") or {})
        r1_op["machines_today_by_firm"] = machines
        r1_op["total_machines"] = donor_op.get(
            "total_machines", sum(machines.values()),
        )
        r1_op["_grafted_from_report_id"] = donor_rid

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
            machines = dict(donor_op.get("machines_today_by_firm") or {})
            r1_op["machines_today_by_firm"] = machines
            r1_op["total_machines"] = donor_op.get(
                "total_machines", sum(machines.values()),
            )
            r1_op["_grafted_from_report_id"] = "6"


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


# Условный список агрофирм для заглушки. Реальные сводки берут AGROFIRMS
# своего модуля (см. extractors.py) — тут это только для тех отчётов,
# для которых факт по машинам ещё не подключён.
_STUB_FIRMS = ["Фирма 1", "Фирма 2", "Фирма 3", "Фирма 4"]

# Сводки, для которых machines_today_by_firm уже реально считается в
# extractors.py — либо через _pack(machines_today_by_firm=...) (1, 3, 4,
# 5, 7, 8, 9, 10, 11), либо напрямую в operations[...]["machines_today_
# by_firm"] для многогрупповых сводок (2, 6, 9). У всех источник в итоге
# один и тот же: len(...) по уникальным machine_id, которые уже считает
# сам модуль сводки (per_firm_machine / pm_counted / "machines"/"n_mach").
# Стаб теперь не нужен ни для одной подключённой сводки — оставлен в коде
# только как безопасный fallback (см. _real_report_result), если у какой-то
# сводки временно нет модуля/данных.
REAL_MACHINE_DATA_REPORT_IDS: set[str] = {
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "14",
}


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


def _stub_machines_today_by_firm(
    group_name: str,
    rid: str,
    report_date: date,
    *,
    agree_with: str | None = None,
) -> dict[str, int]:
    """ЗАГЛУШКА: детерминированное псевдослучайное число машин на фирму.

    agree_with — если задан report_id, "эталонный" для этой операции
    (обычно первый пир), то генерируем ТЕ ЖЕ числа, что и у него — чтобы
    в стабе по умолчанию сверка сходилась (status=OK), а расхождение можно
    было включить явно (см. STUB_FORCE_DIFF ниже) и проверить, что диалог
    на фронте корректно подсвечивает DIFF.
    """
    seed_rid = agree_with or rid
    out: dict[str, int] = {}
    for firm in _STUB_FIRMS:
        key = f"{group_name}|{seed_rid}|{report_date.isoformat()}|{firm}"
        h = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16)
        out[firm] = h % 12  # 0..11 машин "на фирму" — просто для наглядности
    return out


def _stub_report_result(
    rid: str,
    report_date: date,
    canonical_rid_by_group: dict[str, str],
    *,
    force_diff_groups: set[str] | None = None,
) -> dict[str, Any]:
    """Собирает заглушечный result для одной сводки в форме, которую ждёт
    machine_compare.build_machine_cross_check (operations[group]
    ["machines_today_by_firm"]).
    """
    force_diff_groups = force_diff_groups or set()
    operations: dict[str, dict[str, Any]] = {}
    for group_name, peers in OPERATION_PEERS.items():
        if rid not in peers:
            continue
        canon = canonical_rid_by_group.get(group_name, rid)
        agree_with = None if (rid == canon or group_name in force_diff_groups) else canon
        machines = _stub_machines_today_by_firm(
            group_name, rid, report_date, agree_with=agree_with,
        )
        operations[group_name] = {"machines_today_by_firm": machines}
    return {
        "report_id": rid,
        "svodka": {"operations": operations},
    }


def _real_report_result(rid: str, report_date: date) -> dict[str, Any]:
    """Реальный факт по машинам через тот же источник, что и у гектаров
    (proverka.svodka_facts.compute_svodka_facts) — сам модуль сводки уже
    отдаёт machines_today_by_firm в operations[group_name] (см. extractors.py).
    """
    from proverka.svodka_facts import compute_svodka_facts

    spec = REPORTS[rid]
    svodka = asyncio.run(compute_svodka_facts(spec, report_date))
    return {"report_id": rid, "svodka": svodka}


def run_machine_proverka_batch(
    report_date: date,
    report_ids: list[str],
    out_dir: Path,
    *,
    write_json: bool = True,
    progress: Callable[[str], None] | None = None,
    should_cancel_report: Callable[[str | None], bool] | None = None,
    force_diff_groups: list[str] | None = None,
) -> dict[str, Any]:
    """Прогон проверки по машинам для набора сводок за одну дату.

    Пока считает по ЗАГЛУШКЕ (см. модуль). Сигнатура и форма возврата
    рассчитаны так, чтобы service.py / API / фронт не пришлось переделывать
    на этапе 2, когда источник факта станет реальным.
    """
    def prog(msg: str) -> None:
        if progress:
            progress(msg)

    if should_cancel_report and should_cancel_report(None):
        raise MachineProverkaCancelled("cancelled")

    report_ids = _expand_machine_peer_report_ids(report_ids)
    unknown = [rid for rid in report_ids if rid not in REPORTS]
    known_ids = [rid for rid in report_ids if rid in REPORTS]

    try:
        from report_platform import bootstrap_platform

        bootstrap_platform(report_date, do_reload_plans=True)
    except Exception:
        pass

    prog("Сбор фактов по машинам…")

    # "эталонная" сводка для каждой операции — та, что не 1/2 (или последняя
    # из активных), чтобы заглушка была стабильна и не зависела от порядка
    canonical_rid_by_group: dict[str, str] = {}
    present = set(known_ids)
    for group_name in OPERATION_PEERS:
        peers = [r for r in peer_reports_for_machine_group(group_name) if r in present]
        if not peers:
            continue
        canonical_rid_by_group[group_name] = next(
            (r for r in peers if r not in ("1", "2")), peers[-1],
        )

    all_results: dict[str, Any] = {"reports": {}, "report_date": report_date.isoformat()}
    cancelled_ids: list[str] = []
    stub_used_for: list[str] = []
    real_used_for: list[str] = []
    for rid in known_ids:
        if should_cancel_report and should_cancel_report(rid):
            cancelled_ids.append(rid)
            continue
        if rid in REAL_MACHINE_DATA_REPORT_IDS:
            prog(f"Сводка {rid}: {REPORTS[rid].title} (машины, реальный факт)")
            try:
                all_results["reports"][rid] = _real_report_result(rid, report_date)
                real_used_for.append(rid)
                continue
            except Exception as exc:  # noqa: BLE001
                prog(
                    f"Сводка {rid}: не удалось получить реальный факт "
                    f"по машинам ({exc}), временно заглушка"
                )
        prog(f"Сводка {rid}: {REPORTS[rid].title} (машины, заглушка)")
        stub_used_for.append(rid)
        all_results["reports"][rid] = _stub_report_result(
            rid, report_date, canonical_rid_by_group,
            force_diff_groups=set(force_diff_groups or []),
        )

    _apply_report1_donor_graft(all_results)

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
        "is_stub_data": bool(stub_used_for),
        "real_data_report_ids": real_used_for,
        "stub_data_report_ids": stub_used_for,
        "batch_json": str(batch_path) if write_json else None,
        "machine_cross_check": machine_cross_check,
        "machine_cross_check_rows": machine_cross_check.get("operations") or [],
    }
