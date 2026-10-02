#!/usr/bin/env python3
"""
Сводная таблица: все сводки vs Cropwise UI (из reconcile_all_*.json или отдельных файлов).

  python3 proverka/summary.py --date 2026-09-11
  python3 proverka/summary.py --json /app/reconcile_all_2026-09-11.json
  python3 proverka/summary.py --date 2026-09-11 --csv report.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

PLAN_DIR = Path(__file__).resolve().parent.parent
if str(PLAN_DIR) not in sys.path:
    sys.path.insert(0, str(PLAN_DIR))

from proverka.ha_thresholds import diff_is_significant  # noqa: E402
from proverka.reports_config import REPORTS, list_report_ids  # noqa: E402


def _default_out_dir() -> Path:
    return Path(os.environ.get("RECONCILE_OUT_DIR", str(PLAN_DIR.parents[1])))


def load_batch(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "reports" in data:
        return data
    # одиночный reconcile_reportN_*.json
    rid = str(data.get("report_id") or "")
    if not rid and data.get("svodka"):
        rid = str(data["svodka"].get("report_id") or "")
    return {"report_date": data.get("svodka", {}).get("report_date"), "reports": {rid: data}}


def find_batch_file(out_dir: Path, report_date: str) -> Path | None:
    p = out_dir / f"reconcile_all_{report_date}.json"
    if p.is_file():
        return p
    parts: dict[str, Any] = {"report_date": report_date, "reports": {}}
    for rid in list_report_ids():
        single = out_dir / f"reconcile_report{rid}_{report_date}.json"
        if single.is_file():
            one = load_batch(single)
            parts["reports"][rid] = one["reports"][rid]
    if parts["reports"]:
        return None  # caller merges parts
    return None


def load_for_date(out_dir: Path, report_date: str) -> dict[str, Any]:
    batch = out_dir / f"reconcile_all_{report_date}.json"
    if batch.is_file():
        return load_batch(batch)
    merged: dict[str, Any] = {"report_date": report_date, "reports": {}}
    for rid in list_report_ids():
        single = out_dir / f"reconcile_report{rid}_{report_date}.json"
        if single.is_file():
            one = load_batch(single)
            merged["reports"][rid] = one["reports"][rid]
    if not merged["reports"]:
        raise FileNotFoundError(
            f"Нет JSON для {report_date} в {out_dir}. "
            f"Сначала: python3 proverka/reconcile.py --all --date {report_date}"
        )
    return merged


def _row_from_result(rid: str, result: dict[str, Any]) -> dict[str, Any]:
    spec = REPORTS.get(rid)
    title = (spec.title if spec else result.get("title")) or rid
    svodka = result.get("svodka") or {}
    cmp_ = result.get("comparison") or {}
    work = result.get("cropwise_work_report") or {}
    parsed = result.get("cropwise_parsed") or {}
    sp = svodka.get("season_policy") or cmp_.get("season_policy") or {}

    sv_ha = float(svodka.get("total_ha") or cmp_.get("svodka_total_ha") or 0)
    if spec and not spec.cropwise_comparable and not spec.cropwise_per_operation:
        cw_ha = 0.0
        diff = 0.0
    else:
        cw_ha = float(
            cmp_.get("cropwise_fact_total_ha")
            or parsed.get("fact_total_ha")
            or 0
        )
        diff = round(sv_ha - cw_ha, 2)
    if cmp_.get("total_diff_ha") is not None:
        diff = float(cmp_["total_diff_ha"])
    api_ha = float(cmp_.get("cropwise_api_ha") or 0)

    ui_degraded = bool(cmp_.get("ui_degraded"))
    ui_session_blocked = bool(cmp_.get("ui_session_blocked"))
    ui_session = cmp_.get("ui_session_seasonValue")
    required_session = cmp_.get("required_ui_sessionValue")
    report_blank = bool((work.get("body") or {}).get("reportBlank"))
    if spec and not spec.cropwise_comparable and not spec.cropwise_per_operation:
        status = "1↔2↔N"
    elif not work.get("ok"):
        status = "UI error"
    elif ui_session_blocked and required_session and abs(diff) > 5.0:
        if bool(cmp_.get("cropwise_api_fallback")) or report_blank:
            status = f"UI season {ui_session}"
        else:
            status = "season?" if len(sp.get("task_season_years") or []) > 1 else "DIFF"
    elif ui_session_blocked and required_session and bool(cmp_.get("cropwise_api_fallback")):
        status = "OK" if abs(diff) <= 5.0 else "DIFF"
    elif ui_degraded and sv_ha > 1.0:
        status = "UI degraded"
    elif bool(cmp_.get("cropwise_api_fallback")) and cw_ha > 0:
        status = "OK" if abs(diff) <= 5.0 else "DIFF"
    elif parsed.get("error") or report_blank:
        status = "blank"
    elif not diff_is_significant(diff):
        status = "OK"
    elif api_ha > 0 and abs(sv_ha - api_ha) <= 5.0:
        # Сводка сошлась с API v3 самого Cropwise, а «Всего» из work.json — нет.
        # Сезоны тут ни при чём (раньше такое помечалось «season?»): лишние гектары
        # есть только в UI, поэтому и искать их надо в work.json, а не в фильтре.
        status = "UI≠API"
    elif sp.get("task_season_years") and len(sp.get("task_season_years") or []) > 1:
        status = "season?"
    else:
        status = "DIFF"

    firms_with_diff = 0
    for d in (cmp_.get("by_firm_diff") or {}).values():
        if diff_is_significant(float(d)):
            firms_with_diff += 1

    # «В файле» — пилот: то, что реально лежит на листе «Сводка» уже
    # сохранённого .xlsx отчёта (см. proverka/file_values.py). Есть не для
    # всех отчётов/дат (нужен сохранённый xlsx) — тогда оба поля None,
    # и колонка «В файле» просто не показывается на фронте.
    file_values = result.get("file_values") or {}
    file_ha: float | None = None
    file_diff_ha: float | None = None
    if file_values.get("found"):
        file_ha = float(file_values.get("file_ha") or 0)
        file_diff_ha = round(file_ha - sv_ha, 2)

    return {
        "id": rid,
        "title": title,
        "svodka_ha": sv_ha,
        "cropwise_ha": cw_ha,
        "cropwise_ui_ha": float(cmp_.get("cropwise_ui_ha") or cw_ha),
        "cropwise_api_ha": float(cmp_.get("cropwise_api_ha") or 0),
        "ui_vs_api_ha": float(cmp_.get("cropwise_ui_vs_api_ha") or 0),
        "diff_ha": diff,
        "file_ha": file_ha,
        "file_diff_ha": file_diff_ha,
        "status": status,
        "fact_column": cmp_.get("fact_column") or parsed.get("fact_column") or "",
        "season": (sp.get("label") or sp.get("task_season_label") or "")[:80],
        "firms_with_diff": firms_with_diff,
        "work_type_ids": svodka.get("work_type_ids") or [],
        "warnings": list(cmp_.get("warnings") or []),
    }


CROPWISE_VIOLATION_STATUSES = frozenset({
    "DIFF", "UI error", "blank", "season?", "UI degraded", "UI≠API",
})


def compute_violations(
    summary_rows: list[dict[str, Any]],
    cross_check_rows: list[dict[str, Any]] | None = None,
    *,
    job_error: str | None = None,
) -> dict[str, Any]:
    """Сводка нарушений для UI: Cropwise + сверка сводок между собой."""
    cross_check_rows = cross_check_rows or []
    violations: list[dict[str, Any]] = []
    error_logs: list[dict[str, str]] = []

    if job_error:
        error_logs.append({"source": "proverka", "message": job_error})

    for r in summary_rows:
        status = str(r.get("status") or "")
        diff = abs(float(r.get("diff_ha") or 0))
        if status in ("blank", "UI error") and diff < 0.01:
            continue
        if r.get("is_crop_row") and status == "DIFF":
            violations.append(
                {
                    "kind": "cropwise_crop",
                    "report_id": r.get("id"),
                    "title": r.get("title"),
                    "status": status,
                    "svodka_ha": r.get("svodka_ha"),
                    "cropwise_ha": r.get("cropwise_ha"),
                    "diff_ha": r.get("diff_ha"),
                    "season": r.get("season") or "",
                    "detail": (
                        f"Cropwise / культура «{r.get('crop')}»: сводка {r.get('svodka_ha')} га, "
                        f"UI {r.get('cropwise_ha')} га (Δ {r.get('diff_ha')} га)"
                    ),
                }
            )
            error_logs.append(
                {
                    "source": r.get("title") or "культура",
                    "message": (
                        f"Cropwise / культура «{r.get('crop')}»: сводка {r.get('svodka_ha')} га, "
                        f"UI {r.get('cropwise_ha')} га (Δ {r.get('diff_ha')} га)"
                    ),
                }
            )
            continue
        if status in CROPWISE_VIOLATION_STATUSES or status.startswith("UI season "):
            violations.append(
                {
                    "kind": "cropwise",
                    "report_id": r.get("id"),
                    "title": r.get("title"),
                    "status": status,
                    "svodka_ha": r.get("svodka_ha"),
                    "cropwise_ha": r.get("cropwise_ha"),
                    "diff_ha": r.get("diff_ha"),
                    "season": r.get("season") or "",
                    "detail": _violation_detail_cropwise(r),
                }
            )
            if status in ("UI error", "blank"):
                error_logs.append(
                    {
                        "source": f"сводка {r.get('id')}",
                        "message": _violation_detail_cropwise(r),
                    }
                )

    for row in cross_check_rows:
        if row.get("status") != "DIFF":
            # Даже когда сумма по всем культурам сошлась, внутри могла быть
            # компенсация: одна культура в плюс, другая в минус. Проверяем
            # культуры отдельно и в этом случае.
            for crop_row in row.get("crops") or []:
                if crop_row.get("status") != "DIFF":
                    continue
                violations.append(
                    {
                        "kind": "cross_check_crop",
                        "report_id": row.get("canonical_report"),
                        "title": f"{row.get('title') or row.get('group_name')} · {crop_row.get('crop')}",
                        "status": "DIFF",
                        "max_diff_ha": crop_row.get("max_diff_ha"),
                        "peer_reports": crop_row.get("active_reports") or [],
                        "detail": _violation_detail_cross_crop(row, crop_row),
                    }
                )
                error_logs.append(
                    {
                        "source": f"сверка {row.get('display_name') or row.get('group_name')} · {crop_row.get('crop')}",
                        "message": _violation_detail_cross_crop(row, crop_row),
                    }
                )
            continue
        violations.append(
            {
                "kind": "cross_check",
                "report_id": row.get("canonical_report"),
                "title": row.get("title") or row.get("group_name"),
                "status": "DIFF",
                "max_diff_ha": row.get("max_diff_ha"),
                "peer_reports": row.get("active_reports") or row.get("peer_reports") or [],
                "detail": _violation_detail_cross(row),
            }
        )
        error_logs.append(
            {
                "source": f"сверка {row.get('display_name') or row.get('group_name')}",
                "message": _violation_detail_cross(row),
            }
        )
        # Плюс отдельные расхождения по культуре внутри этой же операции.
        for crop_row in row.get("crops") or []:
            if crop_row.get("status") != "DIFF":
                continue
            violations.append(
                {
                    "kind": "cross_check_crop",
                    "report_id": row.get("canonical_report"),
                    "title": f"{row.get('title') or row.get('group_name')} · {crop_row.get('crop')}",
                    "status": "DIFF",
                    "max_diff_ha": crop_row.get("max_diff_ha"),
                    "peer_reports": crop_row.get("active_reports") or [],
                    "detail": _violation_detail_cross_crop(row, crop_row),
                }
            )

    cropwise_count = sum(1 for v in violations if v["kind"] == "cropwise")
    cropwise_crop_count = sum(1 for v in violations if v["kind"] == "cropwise_crop")
    cross_count = sum(1 for v in violations if v["kind"] == "cross_check")
    cross_crop_count = sum(1 for v in violations if v["kind"] == "cross_check_crop")
    cropwise_diff_count = sum(
        1 for r in summary_rows
        if str(r.get("status") or "") == "DIFF" and not r.get("is_crop_row")
    )
    return {
        "violation_count": len(violations),
        "cropwise_diff_count": cropwise_diff_count,
        "cropwise_violation_count": cropwise_count,
        "cropwise_crop_violation_count": cropwise_crop_count,
        "cross_check_violation_count": cross_count,
        "cross_check_crop_violation_count": cross_crop_count,
        "violations": violations,
        "error_logs": error_logs,
    }


def _violation_detail_cropwise(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "")
    diff = row.get("diff_ha")
    if status == "DIFF":
        return f"Расхождение с Cropwise: сводка {row.get('svodka_ha')} га, UI {row.get('cropwise_ha')} га (Δ {diff} га)"
    if status == "UI degraded":
        return (
            f"Cropwise UI отдал урезанный/пустой отчёт: сводка {row.get('svodka_ha')} га, "
            f"UI {row.get('cropwise_ha')} га (Δ {diff} га). "
            "Проверьте work.json в браузере."
        )
    if status == "season?":
        season = row.get("season") or "season"
        return (
            f"Сводка считает несколько сезонов ({season}). Cropwise собран как "
            f"«work.json за сезон сессии + API v3 за остальные сезоны», "
            f"остаток Δ {diff} га — расхождение источников, а не фильтр сезона"
        )
    if status == "UI≠API":
        return (
            f"Сводка {row.get('svodka_ha')} га сходится с API v3 "
            f"{row.get('cropwise_api_ha')} га, а «Всего» в work.json — "
            f"{row.get('cropwise_ha')} га (Δ {diff} га). Лишние гектары есть только "
            "в UI Cropwise: смотрите строки work.json, а не сезоны"
        )
    if status == "blank":
        return "Cropwise UI вернул пустой отчёт (cookie или reportBlank)"
    if status == "UI error":
        return "Ошибка запроса Cropwise UI (cookie/сеть)"
    return status


def _violation_detail_cross(row: dict[str, Any]) -> str:
    totals = row.get("totals") or {}
    parts = [f"{rid}: {ha} га" for rid, ha in sorted(totals.items())]
    md = row.get("max_diff_ha")
    name = row.get("display_name") or row.get("group_name")
    cw = row.get("cropwise_ha")
    if cw is not None:
        return (
            f"Сводка расходится с Cropwise по операции «{name}»: "
            f"{'; '.join(parts)}; Cropwise {cw} га (max Δ {md} га)"
        )
    return f"Сводки не совпадают по операции «{name}»: {'; '.join(parts)} (max Δ {md} га)"


def _violation_detail_cross_crop(row: dict[str, Any], crop_row: dict[str, Any]) -> str:
    totals = crop_row.get("totals") or {}
    parts = [f"{rid}: {ha} га" for rid, ha in sorted(totals.items())]
    md = crop_row.get("max_diff_ha")
    name = row.get("display_name") or row.get("group_name")
    crop = crop_row.get("crop")
    cw = crop_row.get("cropwise_ha")
    if cw is not None:
        return (
            f"Сводка расходится с Cropwise по «{name}» / культуре «{crop}»: "
            f"{'; '.join(parts)}; Cropwise {cw} га (max Δ {md} га)"
        )
    return f"Сводки не совпадают по «{name}» / культуре «{crop}»: {'; '.join(parts)} (max Δ {md} га)"


def per_op_row_title(rid: str, group_name: str) -> str:
    from proverka.operation_registry import display_group_name

    name = display_group_name(group_name)
    if rid == "1":
        return f"ХК Крупно · {name}"
    if rid == "2":
        return f"ХК · {name}"
    spec = REPORTS.get(rid)
    prefix = spec.title if spec else rid
    return f"{prefix} · {name}"


def _row_from_per_operation(rid: str, group_name: str, oc: dict[str, Any]) -> dict[str, Any]:
    from proverka.operation_compare import operation_for_proverka_display

    op = operation_for_proverka_display(oc)
    pseudo = {
        "svodka": {
            "total_ha": op.get("total_ha"),
            "area_today_by_firm": op.get("area_today_by_firm") or {},
            "season_policy": oc.get("season_policy") or {},
            "work_type_ids": op.get("work_type_ids") or [],
        },
        "comparison": oc.get("comparison") or {},
        "cropwise_work_report": oc.get("cropwise_work_report") or {},
        "cropwise_parsed": oc.get("cropwise_parsed") or {},
    }
    row = _row_from_result(rid, pseudo)
    row["title"] = per_op_row_title(rid, group_name)
    row["operation"] = group_name
    canon = oc.get("canonical_report")
    src = oc.get("svodka_source_report") or canon
    if src:
        row["season"] = f"факт сводки {src} · {(row.get('season') or '')}"[:80]
    local_diff = oc.get("svodka_local_diff_ha")
    if local_diff is None:
        local_diff = oc.get("svodka_r2_diff_ha")
    if local_diff is not None and diff_is_significant(float(local_diff)):
        local_op = oc.get("svodka_op_local") or oc.get("svodka_op_r2") or {}
        local_label = f"св.{rid}={float(local_op.get('total_ha') or 0):.2f}"
        row["season"] = (
            f"{row.get('season', '')} · {local_label} (Δ{float(local_diff):+.2f})"
        )[:120]
    work = oc.get("cropwise_work_report") or {}
    if not work.get("ok") and not row.get("status"):
        row["status"] = "UI error"
    return row


def _cropwise_crop_summary_rows(
    rid: str,
    group_name: str,
    oc: dict[str, Any],
) -> list[dict[str, Any]]:
    """Строки «сводка ↔ Cropwise» по культурам (опрыскивание, посев, …)."""
    from proverka.operation_registry import display_group_name

    base = per_op_row_title(rid, group_name)
    op_label = display_group_name(group_name)
    out: list[dict[str, Any]] = []
    for cc in oc.get("crop_comparisons") or []:
        status = str(cc.get("status") or "OK")
        diff = float(cc.get("diff_ha") or 0)
        out.append({
            "id": rid,
            "title": f"{base} · {cc.get('crop')}",
            "operation": group_name,
            "operation_display": op_label,
            "crop": cc.get("crop"),
            "is_crop_row": True,
            "svodka_ha": float(cc.get("svodka_ha") or 0),
            "cropwise_ha": float(cc.get("cropwise_ha") or 0),
            "cropwise_ui_ha": float(cc.get("cropwise_ha") or 0),
            "cropwise_api_ha": 0.0,
            "ui_vs_api_ha": 0.0,
            "diff_ha": diff,
            "status": status,
            "fact_column": "",
            "season": f"культура · {op_label}"[:80],
            "firms_with_diff": sum(
                1 for fr in (cc.get("firms") or []) if fr.get("status") == "DIFF"
            ),
            "work_type_ids": oc.get("work_type_ids") or [],
            "warnings": [],
            "crop_firms": cc.get("firms") or [],
        })
    return out


def build_rows(batch: dict[str, Any]) -> list[dict[str, Any]]:
    reports = batch.get("reports") or {}
    rows: list[dict[str, Any]] = []
    for rid in list_report_ids():
        if rid not in reports:
            continue
        result = reports[rid]
        spec = REPORTS.get(rid)
        if spec and spec.cropwise_per_operation:
            op_cmp = result.get("operation_comparisons") or {}
            for group_name in sorted(op_cmp.keys()):
                oc = op_cmp[group_name]
                rows.append(_row_from_per_operation(rid, group_name, oc))
                rows.extend(_cropwise_crop_summary_rows(rid, group_name, oc))
            continue
        rows.append(_row_from_result(rid, result))
        groups = tuple(spec.work_groups or ()) if spec else ()
        if len(groups) == 1 and result.get("crop_comparisons"):
            rows.extend(_cropwise_crop_summary_rows(rid, groups[0], {
                "crop_comparisons": result.get("crop_comparisons") or [],
                "work_type_ids": (result.get("svodka") or {}).get("work_type_ids") or [],
            }))
    return rows


def print_table(rows: list[dict[str, Any]], report_date: str) -> None:
    print("\n" + "=" * 100)
    print(f"СВОДКА СВЕРКИ | дата отчёта {report_date}")
    print("=" * 100)
    print(
        f"{'ID':>3}  {'Работа':<22} {'Сводка':>10} {'Cropwise':>10} {'API v3':>10} {'Diff':>10}  "
        f"{'Статус':<8} {'Фирм≠':>5}  Колонка UI"
    )
    print("-" * 110)
    for r in rows:
        print(
            f"{r['id']:>3}  {r['title']:<22} {r['svodka_ha']:10.2f} {r['cropwise_ha']:10.2f} "
            f"{r.get('cropwise_api_ha', 0):10.2f} "
            f"{r['diff_ha']:10.2f}  {r['status']:<8} {r['firms_with_diff']:>5}  "
            f"{(r['fact_column'] or '—')[:30]}"
        )
        for w in r.get("warnings") or []:
            print(f"{'':>3}  ⚠ {w}")
    print("-" * 110)
    ok = sum(1 for r in rows if r["status"] == "OK")
    diff = sum(1 for r in rows if r["status"] == "DIFF")
    print(f"Итого сводок: {len(rows)} | OK: {ok} | DIFF: {diff} | прочее: {len(rows) - ok - diff}")
    print("\nСтатусы: OK — diff=0; DIFF — расхождение; UI≠API — сводка сошлась с API v3,")
    print("         лишнее только в work.json; season? — несколько сезонов, остаток после")
    print("         сборки UI+API; blank — нет данных UI; UI error — cookie/сеть.")
    print("Cropwise: work.json отдаёт только сезон сессии, остальные сезоны — из API v3.\n")


def write_csv(rows: list[dict[str, Any]], path: Path, report_date: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "report_date", "id", "title", "svodka_ha", "cropwise_ha",
                "cropwise_ui_ha", "cropwise_api_ha", "ui_vs_api_ha", "diff_ha",
                "status", "firms_with_diff", "fact_column", "season", "work_type_ids",
                "warnings",
            ],
            extrasaction="ignore",
            restval="",
        )
        w.writeheader()
        for r in rows:
            w.writerow({**r, "report_date": report_date, "work_type_ids": ",".join(map(str, r["work_type_ids"]))})


def print_firm_matrix(batch: dict[str, Any], report_ids: list[str] | None = None) -> None:
    """По каждой агрофирме — svodka / cropwise / diff по выбранным сводкам."""
    ids = report_ids or [r["id"] for r in build_rows(batch)]
    reports = batch.get("reports") or {}
    all_firms: set[str] = set()
    for rid in ids:
        sv = (reports.get(rid) or {}).get("svodka") or {}
        all_firms.update(sv.get("area_today_by_firm") or {})
        parsed = (reports.get(rid) or {}).get("cropwise_parsed") or {}
        all_firms.update(parsed.get("fact_by_firm") or {})

    print("\n--- По агрофирмам (сводка | Cropwise | diff), га ---")
    for firm in sorted(all_firms):
        parts = []
        for rid in ids:
            res = reports.get(rid) or {}
            sv = float((res.get("svodka") or {}).get("area_today_by_firm", {}).get(firm, 0) or 0)
            cw = float((res.get("cropwise_parsed") or {}).get("fact_by_firm", {}).get(firm, 0) or 0)
            if sv or cw:
                parts.append(f"[{rid}] {sv:.1f}/{cw:.1f}/{sv - cw:+.1f}")
        if parts:
            print(f"  {firm}: " + "  ".join(parts))


def main() -> int:
    parser = argparse.ArgumentParser(description="Сводная таблица сверки proverka")
    parser.add_argument("--date", default=os.environ.get("REPORT_DATE", ""), help="YYYY-MM-DD")
    parser.add_argument("--json", dest="json_path", help="Путь к reconcile_all_*.json")
    parser.add_argument("--csv", dest="csv_path", help="Сохранить таблицу в CSV")
    parser.add_argument("--firms", action="store_true", help="Детализация по агрофирмам")
    parser.add_argument(
        "--reports",
        nargs="*",
        metavar="ID",
        help="Только эти ID (по умолчанию все из JSON)",
    )
    args = parser.parse_args()

    out_dir = _default_out_dir()
    if args.json_path:
        batch = load_batch(Path(args.json_path))
        report_date = str(batch.get("report_date") or args.date or "?")
    else:
        if not args.date:
            print("ERROR: укажите --date или --json", file=sys.stderr)
            return 1
        batch = load_for_date(out_dir, args.date)
        report_date = args.date

    rows = build_rows(batch)
    if args.reports:
        allowed = set(args.reports)
        rows = [r for r in rows if r["id"] in allowed]

    if not rows:
        print("ERROR: нет данных для таблицы", file=sys.stderr)
        return 1

    print_table(rows, report_date)
    if args.firms:
        print_firm_matrix(batch, [r["id"] for r in rows])
    if args.csv_path:
        write_csv(rows, Path(args.csv_path), report_date)
        print(f"CSV: {args.csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
