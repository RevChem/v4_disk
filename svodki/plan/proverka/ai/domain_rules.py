"""Детерминированные agronomy-правила до вызова LLM."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from proverka.task_errors import gosnumber_for, task_url

_RULES_PATH = Path(__file__).resolve().parent / "rules.yaml"

# Только «6.3 Скашивание с измельчением кукурузы» (work_type 84).
SILAGE_CORN_MOWING_WT_IDS = frozenset({84})
CORN_SILAGE_CROP = "Кукуруза на силос"
HARVEST_GROUP = "5.0 Уборка"


def _custom_rules_path() -> Path:
    upload = (
        os.environ.get("UPLOAD_DIR")
        or os.environ.get("SVODKI_UPLOAD_DIR")
        or "uploads"
    )
    return Path(upload) / "proverka" / "ai_rules.yaml"


def load_rules_yaml() -> dict[str, Any]:
    """Сначала uploads/proverka/ai_rules.yaml (редактор UI), иначе bundled rules.yaml."""
    custom = _custom_rules_path()
    if custom.is_file():
        try:
            data = yaml.safe_load(custom.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, yaml.YAMLError):
            pass
    if not _RULES_PATH.is_file():
        return {}
    try:
        return yaml.safe_load(_RULES_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def llm_prompt_context() -> str:
    """Текст для system prompt DeepSeek: общий контекст + описания правил."""
    ctx = load_rules_yaml()
    parts: list[str] = []
    base = str(ctx.get("prompt_context") or "").strip()
    if base:
        parts.append(base)
    for rule in ctx.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        title = str(rule.get("title") or rule.get("id") or "").strip()
        desc = str(rule.get("description") or "").strip()
        if title and desc:
            parts.append(f"### {title}\n{desc}")
    return "\n\n".join(parts)


def _silage_harvest_ha(batch: dict[str, Any]) -> float:
    """Факт «Кукуруза на силос» в уборке 5.0 из batch."""
    reports = batch.get("reports") or {}
    best = 0.0
    for rid, res in reports.items():
        sv = res.get("svodka") or {}
        ops = sv.get("operations") or {}
        op = ops.get(HARVEST_GROUP) or {}
        if not op:
            for gn, o in ops.items():
                if gn.startswith("5.0"):
                    op = o
                    break
        detail = op.get("detail_rows") or sv.get("detail_rows") or []
        for row in detail:
            crop = str(row.get("crop") or row.get("Культура") or row.get("culture") or "")
            if CORN_SILAGE_CROP in crop or crop == CORN_SILAGE_CROP:
                ha = float(
                    row.get("area_today")
                    or row.get("Факт за сутки, га")
                    or row.get("fact_today")
                    or 0
                )
                best = max(best, ha)
        crops = op.get("crops") or op.get("by_crop") or {}
        if isinstance(crops, dict):
            for crop_name, val in crops.items():
                if CORN_SILAGE_CROP in str(crop_name):
                    if isinstance(val, dict):
                        best = max(best, float(val.get("area_today") or val.get("total_ha") or 0))
                    else:
                        best = max(best, float(val or 0))
    return round(best, 2)


def _shift_window(report_date: date) -> tuple[datetime, datetime]:
    from plans import TZ_MSK

    h = 6
    shift = report_date - timedelta(days=1)
    day_start = datetime(shift.year, shift.month, shift.day, h, 0, tzinfo=TZ_MSK)
    cutoff = datetime(report_date.year, report_date.month, report_date.day, h, 0, tzinfo=TZ_MSK)
    return day_start, cutoff


def _mowing_tasks_in_shift(report_date: date) -> list[dict[str, Any]]:
    from proverka.season_cropwise import ShiftWindowCache, _area_in_shift, _parse_dt

    day_start, cutoff = _shift_window(report_date)
    t0 = day_start.isoformat()[:19]
    t1 = cutoff.isoformat()[:19]
    wt_list = sorted(SILAGE_CORN_MOWING_WT_IDS)
    cache = ShiftWindowCache.build(t0, t1, wt_list, log_prefix="[ai rules] ")

    out: list[dict[str, Any]] = []
    for t in cache.all_tasks:
        wt = int(t.get("work_type_id") or 0)
        if wt not in SILAGE_CORN_MOWING_WT_IDS:
            continue
        tid = t.get("id")
        if tid is None:
            continue
        tid_i = int(tid)
        items = cache.mtfmi_map.get(tid_i, [])
        area = round(_area_in_shift(items, day_start, cutoff), 2)
        if area < 0.01:
            continue
        machine_id = int(t["machine_id"]) if t.get("machine_id") is not None else None
        out.append({
            "task_id": tid_i,
            "machine_id": machine_id,
            "work_type_id": wt,
            "season": str(t.get("season") or ""),
            "area_ha": area,
            "field_id": t.get("field_id"),
            "gosnumber": gosnumber_for(machine_id, ""),
            "task_url": task_url(machine_id, tid_i),
        })
    out.sort(key=lambda x: -float(x.get("area_ha") or 0))
    return out


def run_domain_rules(batch: dict[str, Any]) -> list[dict[str, Any]]:
    """Все rule_findings для digest."""
    findings: list[dict[str, Any]] = []
    rd = batch.get("report_date") or ""
    try:
        report_date = date.fromisoformat(str(rd).split("T")[0])
    except ValueError:
        return findings

    mowing = _mowing_tasks_in_shift(report_date)
    mowing_total = round(sum(float(t.get("area_ha") or 0) for t in mowing), 2)
    silage_ha = _silage_harvest_ha(batch)

    if mowing_total >= 0.01 and silage_ha < 0.01:
        findings.append({
            "rule_id": "silage_mowing_without_harvest",
            "severity": "critical",
            "title": "Кукуруза на скашивание без уборки на силос",
            "summary": (
                f"За смену скашивание (силосные wt): {mowing_total:.2f} га, "
                f"но «{CORN_SILAGE_CROP}» в уборке 5.0 = {silage_ha:.2f} га."
            ),
            "mowing_total_ha": mowing_total,
            "silage_harvest_ha": silage_ha,
            "related_tasks": mowing[:15],
            "suggested_action": (
                "Проверьте классификацию уборки: поля со скашиванием должны попасть "
                "в «Кукуруза на силос», не в зерновую уборку."
            ),
        })

    return findings
