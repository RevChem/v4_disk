#!/usr/bin/env python3
"""
Excel-отчёт: сводки vs Cropwise UI + матрица сводок между собой.

  python3 proverka/export_excel.py --date 2026-09-11
  python3 proverka/export_excel.py --json /app/reconcile_all_2026-09-11.json

Docker (после reconcile --all):
  docker exec v3_disk-api-1 python3 /app/svodki/plan/proverka/export_excel.py --date 2026-09-11
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

PLAN_DIR = Path(__file__).resolve().parent.parent
if str(PLAN_DIR) not in sys.path:
    sys.path.insert(0, str(PLAN_DIR))

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from proverka.reports_config import REPORTS, list_report_ids  # noqa: E402
from proverka.summary import build_rows, load_for_date, per_op_row_title  # noqa: E402
from proverka.task_errors import collect_task_errors_from_batch  # noqa: E402

THIN = Side(style="thin", color="CCCCCC")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
FILL_HDR = PatternFill("solid", fgColor="1F4E79")
FILL_SUB = PatternFill("solid", fgColor="D6E4F0")
FILL_OK = PatternFill("solid", fgColor="E2EFDA")
FILL_BAD = PatternFill("solid", fgColor="FCE4D6")
FILL_TOTAL = PatternFill("solid", fgColor="FFF2CC")
FONT_HDR = Font(bold=True, color="FFFFFF", size=11)
FONT_BOLD = Font(bold=True, size=10)
FONT_NORM = Font(size=10)


def _style_header_row(ws, row: int, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = FILL_HDR
        cell.font = FONT_HDR
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER


def _write_row(ws, row: int, values: list, *, bold: bool = False, fill: PatternFill | None = None) -> None:
    for ci, val in enumerate(values, start=1):
        cell = ws.cell(row=row, column=ci, value=val)
        cell.font = FONT_BOLD if bold else FONT_NORM
        cell.border = BORDER
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        if isinstance(val, float):
            cell.number_format = "#,##0.00"
        if fill:
            cell.fill = fill


def _autowidth(ws, max_col: int, min_w: float = 10, max_w: float = 48) -> None:
    for ci in range(1, max_col + 1):
        letter = get_column_letter(ci)
        best = min_w
        for row in ws.iter_rows(min_col=ci, max_col=ci):
            for cell in row:
                if cell.value is not None:
                    best = max(best, min(max_w, len(str(cell.value)) + 2))
        ws.column_dimensions[letter].width = best


def _write_hyperlink_cell(ws, row: int, col: int, url: str, display: str) -> None:
    cell = ws.cell(row=row, column=col, value=display)
    cell.font = Font(size=10, color="0563C1", underline="single")
    cell.border = BORDER
    cell.alignment = Alignment(vertical="center", wrap_text=True)
    if url:
        cell.hyperlink = url


def _sheet_summary(
    wb: openpyxl.Workbook,
    rows: list[dict],
    report_date: str,
    task_errors: list[dict] | None = None,
) -> None:
    ws = wb.active
    ws.title = "Итоги"
    ws.merge_cells("A1:I1")
    t = ws["A1"]
    t.value = f"Сверка сводок и Cropwise UI | {report_date}"
    t.font = Font(bold=True, size=14, color="1F4E79")
    t.alignment = Alignment(horizontal="center")

    headers = [
        "ID", "Работа", "Сводка, га", "Cropwise UI, га", "API v3, га",
        "UI − API, га", "Diff, га", "Статус", "Фирм ≠", "Колонка UI", "Сезон",
        "Предупреждения",
    ]
    hr = 3
    for ci, h in enumerate(headers, 1):
        ws.cell(row=hr, column=ci, value=h)
    _style_header_row(ws, hr, len(headers))

    r = hr + 1
    for item in rows:
        diff = item["diff_ha"]
        fill = FILL_OK if item["status"] == "OK" else (FILL_BAD if item["status"] == "DIFF" else None)
        _write_row(
            ws, r,
            [
                item["id"], item["title"], item["svodka_ha"], item["cropwise_ha"],
                item.get("cropwise_api_ha") or 0, item.get("ui_vs_api_ha") or 0,
                diff, item["status"], item["firms_with_diff"],
                item.get("fact_column") or "", item.get("season") or "",
                "; ".join(item.get("warnings") or []),
            ],
            fill=fill,
        )
        r += 1

    err_rows = task_errors or []
    r += 2
    ws.cell(row=r, column=1, value="Ошибки (по заданиям Cropwise)")
    ws.cell(row=r, column=1).font = Font(bold=True, size=12, color="1F4E79")
    r += 1

    err_headers = [
        "Работа", "Сводка", "Сводка, га", "Cropwise, га", "Diff, га",
        "Сезон", "Госномер", "Задание",
    ]
    for ci, h in enumerate(err_headers, 1):
        ws.cell(row=r, column=ci, value=h)
    _style_header_row(ws, r, len(err_headers))
    r += 1

    err_rows = [e for e in err_rows if abs(float(e.get("diff_ha") or 0)) >= 0.01]
    if not err_rows:
        ws.cell(row=r, column=1, value="Нет расхождений на уровне заданий (или PROVERKA_TASK_ERRORS=0).")
        r += 1
    else:
        for item in err_rows:
            url = str(item.get("task_url") or "")
            tid = item.get("task_id")
            link_label = f"#{tid}" if tid is not None else ""
            _write_row(
                ws, r,
                [
                    item.get("work") or "",
                    item.get("report_id") or "",
                    item.get("svodka_ha"),
                    item.get("cropwise_ha"),
                    item.get("diff_ha"),
                    item.get("season") or "",
                    item.get("gosnumber") or "",
                    link_label if not url else "",
                ],
                fill=FILL_BAD,
            )
            if url and link_label:
                _write_hyperlink_cell(ws, r, 8, url, link_label)
            r += 1

    _autowidth(ws, max(len(headers), len(err_headers)))


def _sheet_firms(wb: openpyxl.Workbook, batch: dict[str, Any]) -> None:
    ws = wb.create_sheet("Фирмы")
    headers = ["Сводка", "Работа", "Агрофирма", "Сводка, га", "Cropwise, га", "Diff, га"]
    for ci, h in enumerate(headers, 1):
        ws.cell(row=1, column=ci, value=h)
    _style_header_row(ws, 1, len(headers))

    r = 2
    reports = batch.get("reports") or {}
    for rid in list_report_ids():
        if rid not in reports:
            continue
        res = reports[rid]
        spec = REPORTS.get(rid)
        if spec and spec.cropwise_per_operation:
            for gn, oc in sorted((res.get("operation_comparisons") or {}).items()):
                op = oc.get("svodka_op") or {}
                sv = op.get("area_today_by_firm") or {}
                cw = (oc.get("cropwise_parsed") or {}).get("fact_by_firm") or {}
                op_title = per_op_row_title(rid, gn)
                firms = sorted(set(sv) | set(cw))
                for firm in firms:
                    s_ha = float(sv.get(firm, 0) or 0)
                    c_ha = float(cw.get(firm, 0) or 0)
                    diff = round(s_ha - c_ha, 2)
                    fill = FILL_OK if abs(diff) < 0.01 else (FILL_BAD if s_ha or c_ha else None)
                    _write_row(ws, r, [rid, op_title, firm, s_ha, c_ha, diff], fill=fill)
                    r += 1
            continue
        title = spec.title if spec else rid
        sv = (res.get("svodka") or {}).get("area_today_by_firm") or {}
        cw = (res.get("cropwise_parsed") or {}).get("fact_by_firm") or {}
        firms = sorted(set(sv) | set(cw))
        for firm in firms:
            s_ha = float(sv.get(firm, 0) or 0)
            c_ha = float(cw.get(firm, 0) or 0)
            diff = round(s_ha - c_ha, 2)
            fill = FILL_OK if abs(diff) < 0.01 else (FILL_BAD if s_ha or c_ha else None)
            _write_row(ws, r, [rid, title, firm, s_ha, c_ha, diff], fill=fill)
            r += 1
    _autowidth(ws, len(headers))


def _sheet_svodki_matrix(wb: openpyxl.Workbook, batch: dict[str, Any]) -> None:
    """Сверка операций между сводками 1, 2, 3… (одинаковый вид работ)."""
    from proverka.operation_compare import build_cross_check

    ws = wb.create_sheet("Сводки между собой")
    cross = batch.get("cross_check") or build_cross_check(batch)
    rows = cross.get("operations") or []
    if not rows:
        ws.cell(row=1, column=1, value="Нет данных для сверки операций (нужны сводки 1/2 и тематические).")
        return

    ws.merge_cells("A1:H1")
    ws["A1"].value = (
        "Сверка по операциям: факт за смену должен совпадать в сводках 1, 2 и тематической (3, 4, 5…)"
    )
    ws["A1"].font = Font(bold=True, size=11)

    headers = ["Операция", "Сводки", "1, га", "2, га", "3+", "Max diff", "Статус"]
    hr = 3
    for ci, h in enumerate(headers, 1):
        ws.cell(row=hr, column=ci, value=h)
    _style_header_row(ws, hr, len(headers))

    r = hr + 1
    for item in rows:
        totals = item.get("totals") or {}
        active = item.get("active_reports") or []
        canon_vals = [
            f"{rid}={totals.get(rid, 0):.2f}"
            for rid in active
            if rid not in ("1", "2")
        ]
        fill = FILL_OK if item.get("status") == "OK" else (
            FILL_BAD if item.get("status") == "DIFF" else FILL_SUB
        )
        _write_row(
            ws, r,
            [
                item.get("display_name") or item.get("group_name"),
                "+".join(active),
                totals.get("1", ""),
                totals.get("2", ""),
                "; ".join(canon_vals) if canon_vals else "",
                item.get("max_diff_ha", ""),
                item.get("status"),
            ],
            fill=fill,
        )
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="Детализация по фирмам (только DIFF ≥ 0.01 га)")
    ws.cell(row=r, column=1).font = FONT_BOLD
    r += 1
    firm_headers = ["Операция", "Агрофирма", "Значения по сводкам", "Max diff"]
    for ci, h in enumerate(firm_headers, 1):
        ws.cell(row=r, column=ci, value=h)
    _style_header_row(ws, r, len(firm_headers))
    r += 1

    for item in rows:
        if item.get("status") != "DIFF":
            continue
        for fr in item.get("firms") or []:
            if (fr.get("max_diff_ha") or 0) < 0.01:
                continue
            vals = fr.get("values") or {}
            val_str = "; ".join(f"{rid}={v:.2f}" for rid, v in sorted(vals.items()))
            _write_row(
                ws, r,
                [
                    item.get("display_name") or item.get("group_name"),
                    fr.get("firm"),
                    val_str,
                    fr.get("max_diff_ha"),
                ],
                fill=FILL_BAD,
            )
            r += 1

    _autowidth(ws, max(len(headers), len(firm_headers)))


def _sheet_details(
    wb: openpyxl.Workbook,
    batch: dict[str, Any],
    sheet_name: str,
    source: str,
) -> None:
    ws = wb.create_sheet(sheet_name)
    if source == "svodka":
        headers = ["Сводка", "Работа", "Агрофирма", "Машина", "Тип", "Площадь, га"]
    else:
        headers = ["Сводка", "Работа", "Агрофирма", "Путь / задание", "Уровень", "Площадь, га"]

    for ci, h in enumerate(headers, 1):
        ws.cell(row=1, column=ci, value=h)
    _style_header_row(ws, 1, len(headers))

    r = 2
    reports = batch.get("reports") or {}
    for rid in list_report_ids():
        if rid not in reports:
            continue
        res = reports[rid]
        spec = REPORTS.get(rid)
        title = spec.title if spec else rid
        if source == "svodka":
            rows = (res.get("svodka") or {}).get("detail_rows") or []
            for d in rows:
                _write_row(
                    ws, r,
                    [rid, title, d.get("firm"), d.get("machine"), d.get("kind"), d.get("area_ha")],
                )
                r += 1
        else:
            rows = (res.get("cropwise_parsed") or {}).get("detail_rows") or []
            for d in rows:
                _write_row(
                    ws, r,
                    [rid, title, d.get("firm"), d.get("path") or d.get("label"),
                     d.get("level"), d.get("area_ha")],
                )
                r += 1
    _autowidth(ws, len(headers))


def _sheet_discrepancies(wb: openpyxl.Workbook, batch: dict[str, Any]) -> None:
    ws = wb.create_sheet("Расхождения")
    headers = [
        "Сводка", "Работа", "Агрофирма", "Уровень", "Источник",
        "Машина / путь", "Площадь, га", "Diff фирмы, га", "Примечание",
    ]
    for ci, h in enumerate(headers, 1):
        ws.cell(row=1, column=ci, value=h)
    _style_header_row(ws, 1, len(headers))

    r = 2
    reports = batch.get("reports") or {}
    for rid in list_report_ids():
        if rid not in reports:
            continue
        res = reports[rid]
        spec = REPORTS.get(rid)
        title = spec.title if spec else rid
        cmp_ = res.get("comparison") or {}
        firm_diffs = cmp_.get("by_firm_diff") or {}
        sv_firms = (res.get("svodka") or {}).get("area_today_by_firm") or {}
        cw_firms = (res.get("cropwise_parsed") or {}).get("fact_by_firm") or {}
        problem_firms = {
            f for f in set(sv_firms) | set(cw_firms)
            if abs(float(firm_diffs.get(f, sv_firms.get(f, 0) - cw_firms.get(f, 0)))) >= 0.01
        }
        if not problem_firms and abs(float(cmp_.get("total_diff_ha") or 0)) >= 0.01:
            problem_firms = set(sv_firms) | set(cw_firms)

        for firm in sorted(problem_firms):
            fd = round(
                float(firm_diffs.get(firm, float(sv_firms.get(firm, 0)) - float(cw_firms.get(firm, 0)))),
                2,
            )
            if abs(fd) < 0.01 and not problem_firms:
                continue
            note = ""
            seasons = res.get("cropwise_seasons") or {}
            if seasons:
                note = "Cropwise: UI verified + API season"
            else:
                sp = (res.get("svodka") or {}).get("season_policy") or {}
                if sp.get("task_season_years"):
                    note = "сводка фильтрует season"

            _write_row(
                ws, r,
                [rid, title, firm, "фирма", "—", "ИТОГО",
                 float(sv_firms.get(firm, 0)), fd, note],
                bold=True, fill=FILL_SUB,
            )
            r += 1
            _write_row(
                ws, r,
                [rid, title, firm, "деталь", "Cropwise", "—",
                 float(cw_firms.get(firm, 0)), "", ""],
            )
            r += 1
            _write_row(
                ws, r,
                [rid, title, firm, "деталь", "Сводка", "—",
                 float(sv_firms.get(firm, 0)), "", ""],
            )
            r += 1

            for d in (res.get("svodka") or {}).get("detail_rows") or []:
                if d.get("firm") != firm:
                    continue
                _write_row(
                    ws, r,
                    [rid, title, firm, "машина", "Сводка", d.get("machine"),
                     d.get("area_ha"), "", d.get("kind")],
                )
                r += 1
            for d in (res.get("cropwise_parsed") or {}).get("detail_rows") or []:
                if d.get("firm") != firm:
                    continue
                _write_row(
                    ws, r,
                    [rid, title, firm, "задание", "Cropwise", d.get("path") or d.get("label"),
                     d.get("area_ha"), "", ""],
                )
                r += 1
            r += 1  # пустая строка между фирмами

    _autowidth(ws, len(headers))


def build_workbook(batch: dict[str, Any]) -> openpyxl.Workbook:
    report_date = str(batch.get("report_date") or "")
    rows = build_rows(batch)
    task_errors = collect_task_errors_from_batch(batch)
    wb = openpyxl.Workbook()
    _sheet_summary(wb, rows, report_date, task_errors=task_errors)
    _sheet_firms(wb, batch)
    _sheet_svodki_matrix(wb, batch)
    _sheet_details(wb, batch, "Детали сводок", "svodka")
    _sheet_details(wb, batch, "Детали Cropwise", "cropwise")
    _sheet_discrepancies(wb, batch)
    return wb


def export_excel(batch: dict[str, Any], out_path: Path) -> Path:
    wb = build_workbook(batch)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Excel: сводки vs Cropwise")
    parser.add_argument("--date", default=os.environ.get("REPORT_DATE", ""))
    parser.add_argument("--json", dest="json_path")
    parser.add_argument(
        "-o", "--output",
        help="Путь к .xlsx (по умолчанию proverka_{date}.xlsx в RECONCILE_OUT_DIR)",
    )
    args = parser.parse_args()

    out_dir = Path(os.environ.get("RECONCILE_OUT_DIR", str(PLAN_DIR.parents[1])))

    if args.json_path:
        from proverka.summary import load_batch
        batch = load_batch(Path(args.json_path))
        report_date = str(batch.get("report_date") or args.date)
    else:
        if not args.date:
            print("ERROR: --date или --json", file=sys.stderr)
            return 1
        batch = load_for_date(out_dir, args.date)
        report_date = args.date

    out = Path(args.output) if args.output else out_dir / f"proverka_{report_date}.xlsx"
    export_excel(batch, out)
    print(f"Excel: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
