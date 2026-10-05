"""Факты по заданиям (задание × поле) за смену — вопросы без повторного прогона сводок.

Зачем
-----
Результат проверки хранит только агрегаты (агрофирма × культура). Ответить на вопрос
«какие именно задания были по МУ на сое» из них нельзя: строка «задание × поле» теряется
в цикле суммирования. Здесь при расчёте сводки сохраняется компактная таблица таких
строк, а дальше:

* фильтрация (группа работ / культура / фирма / машина / поле) делается в Python — модель
  не видит лишних строк;
* модели уходит только отфильтрованный срез в виде TSV (а не JSON) с вынесенными
  константными колонками и итогами по всем строкам, даже если сами строки усечены;
* для арифметики («сколько га», «у кого больше») модель не нужна вовсе — см. aggregate().

Инвариант
---------
Строки строятся по той же логике, что area_today сводки-источника (для сводки 3 —
3_main_real.build_all_firm_stats), поэтому сумма ``ha`` по агрофирме совпадает с
area_today этой агрофирмы. reconcile_with_area() проверяет это и попадает в
``svodka["task_facts_meta"]`` — расхождение сразу видно.

Файлы: ``task_facts_<дата>__r<id сводки>.tsv`` в каталоге проверки
(PROVERKA_TASK_FACTS_DIR → PROVERKA_CACHE_DIR → RECONCILE_OUT_DIR).
Отключить запись: PROVERKA_TASK_FACTS=0.
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

COLUMNS: tuple[str, ...] = (
    "report_id",
    "group",
    "date",
    "firm",
    "crop",
    "field_id",
    "field",
    "task_id",
    "machine_id",
    "machine",
    "own",
    "season",
    "end",
    "ha",
)
_INT_COLUMNS = frozenset({"field_id", "task_id", "machine_id"})

# Колонки, которые видит модель по умолчанию (короткие и достаточные для «какие задания»).
LLM_DEFAULT_COLUMNS: tuple[str, ...] = ("firm", "crop", "field", "machine", "task_id", "ha")
# Всегда выносятся в шапку, если константны на выборке (стоят копейки, дают контекст).
_CONTEXT_COLUMNS: tuple[str, ...] = ("date", "report_id", "group")

TASK_URL = "https://operations.cropwise.com/machines/{machine_id}/tasks/{task_id}"

_FILE_PREFIX = "task_facts_"


# ─────────────────────────── настройки ───────────────────────────


def facts_enabled() -> bool:
    return os.environ.get("PROVERKA_TASK_FACTS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def facts_dir() -> Path | None:
    raw = (
        os.environ.get("PROVERKA_TASK_FACTS_DIR")
        or os.environ.get("PROVERKA_CACHE_DIR")
        or os.environ.get("RECONCILE_OUT_DIR")
        or ""
    ).strip()
    return Path(raw) if raw else None


def task_url(machine_id: int | None, task_id: int | None) -> str:
    if machine_id is None or task_id is None:
        return ""
    return TASK_URL.format(machine_id=int(machine_id), task_id=int(task_id))


# ─────────────────────────── построение строк ───────────────────────────


def build_shift_task_facts(
    *,
    report_id: str,
    group: str,
    report_date: date,
    tasks_today: Iterable[dict],
    mtfmi_map: dict,
    field_to_firm: dict[int, str],
    field_to_crop: dict[int, str],
    cutoff: datetime,
    parse_dt: Callable[[Any], datetime | None],
    crop_field_ids: set[int] | None = None,
    machines_map: dict[int, dict] | None = None,
    field_names: dict[int, str] | None = None,
    hired_group_id: int | None = None,
    min_machine_ha: float = 0.5,
    crop_label: Callable[[str], str] | None = None,
    work_type_ids: set[int] | None = None,
    field_ok: Callable[[int], bool] | None = None,
    min_item_ha: float | None = None,
    machine_inclusive: bool = True,
    covered_area_if_zero: bool = False,
) -> list[dict[str, Any]]:
    """Строки «задание × поле» за смену, как считает area_today сводка 3.

    Логика 3_main_real.build_all_firm_stats:
      * задание попадает в смену (tasks_today уже отфильтрован по end_time);
      * поле из mtfmi должно быть в crop_field_ids и иметь агрофирму;
      * площадь = Σ covered_area_hourly с часом < cutoff;
      * агрофирма × машина учитываются, только если их сумма ≥ min_machine_ha.
    """
    machines_map = machines_map or {}
    field_names = field_names or {}
    per_key: dict[tuple[str, Any], list[tuple[float, dict[str, Any]]]] = defaultdict(list)
    date_iso = report_date.isoformat()

    for t in tasks_today:
        tid = t.get("id")
        if tid is None:
            continue
        if work_type_ids is not None and t.get("work_type_id") not in work_type_ids:
            continue
        items = mtfmi_map.get(tid)
        if items is None:
            try:
                items = mtfmi_map.get(int(tid))
            except (TypeError, ValueError):
                items = None
        if not items:
            continue
        mid = t.get("machine_id")
        end_dt = parse_dt(t.get("end_time"))
        end_txt = end_dt.strftime("%d.%m %H:%M") if end_dt else ""

        for item in items:
            fid = item.get("field_id")
            if fid is None:
                continue
            try:
                fid_i = int(fid)
            except (TypeError, ValueError):
                continue
            if crop_field_ids is not None and fid_i not in crop_field_ids:
                continue
            if field_ok is not None and not field_ok(fid_i):
                continue
            firm = field_to_firm.get(fid_i)
            if not firm:
                continue
            cov = 0.0
            hourly = item.get("covered_area_hourly") or []
            for entry in hourly:
                h = parse_dt(entry[0])
                if h and h < cutoff:
                    cov += float(entry[1] or 0)
            if cov <= 0 and covered_area_if_zero:
                cov = float(item.get("covered_area") or 0)
            if cov <= 0:
                continue
            if min_item_ha is not None and cov <= float(min_item_ha):
                continue

            crop = str(field_to_crop.get(fid_i) or "").strip()
            if crop and crop_label:
                crop = crop_label(crop) or crop
            machine_rec = machines_map.get(mid) if mid is not None else None
            if machine_rec is None and mid is not None:
                try:
                    machine_rec = machines_map.get(int(mid))
                except (TypeError, ValueError):
                    machine_rec = None
            machine_rec = machine_rec or {}
            own = ""
            if hired_group_id is not None:
                own = "hired" if machine_rec.get("machine_group_id") == hired_group_id else "own"
            row = {
                "report_id": str(report_id),
                "group": group,
                "date": date_iso,
                "firm": firm,
                "crop": crop,
                "field_id": fid_i,
                "field": field_names.get(fid_i, ""),
                "task_id": int(tid),
                "machine_id": int(mid) if mid is not None else None,
                "machine": str(machine_rec.get("name") or (f"machine_{mid}" if mid is not None else "")),
                "own": own,
                "season": str(t.get("season") or ""),
                "end": end_txt,
                "ha": 0.0,
            }
            per_key[(firm, mid)].append((cov, row))

    rows: list[dict[str, Any]] = []
    for entries in per_key.values():
        total = sum(c for c, _ in entries)
        rounded = round(total, 2)
        if machine_inclusive:
            if rounded < min_machine_ha:
                continue
        elif rounded <= min_machine_ha:
            continue  # такая пара «фирма × машина» не входит в area_today сводки
        for cov, row in entries:
            row["ha"] = round(cov, 2)
            rows.append(row)
    rows.sort(key=lambda r: (r["firm"], -r["ha"], r["task_id"], r["field_id"]))
    return rows


def reconcile_with_area(
    rows: Sequence[dict[str, Any]],
    area_today_by_firm: dict[str, float],
) -> dict[str, Any]:
    """Сверка Σha строк с area_today сводки по агрофирмам (допуск — на округление строк)."""
    sums: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        sums[r["firm"]] += float(r.get("ha") or 0)
        counts[r["firm"]] += 1
    worst = 0.0
    worst_firm = ""
    ok = True
    for firm in set(sums) | set(area_today_by_firm):
        want = float(area_today_by_firm.get(firm) or 0)
        diff = abs(sums.get(firm, 0.0) - want)
        tol = 0.02 + 0.005 * counts.get(firm, 0)  # округление каждой строки до 0.01
        if diff > tol:
            ok = False
        if diff > worst:
            worst, worst_firm = diff, firm
    return {
        "facts_ha": round(sum(sums.values()), 2),
        "area_today_ha": round(sum(float(v or 0) for v in area_today_by_firm.values()), 2),
        "max_firm_diff_ha": round(worst, 2),
        "max_diff_firm": worst_firm,
        "consistent": ok,
    }


# ─────────────────────────── запись / чтение ───────────────────────────


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def facts_file_name(report_date: date, report_id: str) -> str:
    return f"{_FILE_PREFIX}{report_date.isoformat()}__r{report_id}.tsv"


def write_task_facts(
    rows: Sequence[dict[str, Any]],
    *,
    report_id: str,
    report_date: date,
    out_dir: Path | str | None = None,
) -> Path | None:
    """Атомарно пишет TSV. None — если запись отключена или некуда писать."""
    if not facts_enabled():
        return None
    d = Path(out_dir) if out_dir else facts_dir()
    if d is None:
        return None
    d.mkdir(parents=True, exist_ok=True)
    path = d / facts_file_name(report_date, report_id)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow([_cell(r.get(c)) for c in COLUMNS])
    os.replace(tmp, path)
    return path


def facts_files(directory: Path | str, report_date: date | str) -> list[Path]:
    d = Path(directory)
    if not d.is_dir():
        return []
    iso = report_date.isoformat() if isinstance(report_date, date) else str(report_date)
    return sorted(d.glob(f"{_FILE_PREFIX}{iso}__r*.tsv"))


def has_task_facts(directory: Path | str, report_date: date | str) -> bool:
    return bool(facts_files(directory, report_date))


def _typed(row: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in COLUMNS:
        v = row.get(c, "")
        if c == "ha":
            try:
                out[c] = float(v)
            except (TypeError, ValueError):
                out[c] = 0.0
        elif c in _INT_COLUMNS:
            try:
                out[c] = int(v)
            except (TypeError, ValueError):
                out[c] = None
        else:
            out[c] = v or ""
    return out


def load_task_facts(
    directory: Path | str,
    report_date: date | str,
    *,
    report_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    want = {str(x) for x in report_ids} if report_ids else None
    rows: list[dict[str, Any]] = []
    for path in facts_files(directory, report_date):
        with path.open("r", encoding="utf-8", newline="") as fh:
            for raw in csv.DictReader(fh, delimiter="\t"):
                r = _typed(raw)
                if want is not None and r["report_id"] not in want:
                    continue
                rows.append(r)
    return dedupe_facts(rows)


def dedupe_facts(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Одна и та же работа из нескольких сводок — одна строка.

    Ключ: группа, дата, фирма, задание, поле, машина и га до сотых.
    Пустые культура, поле и машина добираются из копии, где они заполнены.
    Га, которые в файле уже округлены до 0.00, в список не попадают.
    """
    best: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    for r in rows:
        try:
            ha = round(float(r.get("ha") or 0), 2)
        except (TypeError, ValueError):
            continue
        if ha <= 0:
            continue
        key = (
            str(r.get("group") or ""),
            str(r.get("date") or ""),
            str(r.get("firm") or ""),
            r.get("task_id"),
            r.get("field_id"),
            r.get("machine_id"),
            ha,
        )
        prev = best.get(key)
        if prev is None:
            item = dict(r)
            item["ha"] = ha
            best[key] = item
            order.append(key)
            continue
        for col in ("crop", "field", "machine", "own", "season", "end"):
            if not str(prev.get(col) or "").strip() and str(r.get(col) or "").strip():
                prev[col] = r[col]
    return [best[k] for k in order]


# ─────────────────────────── фильтр / агрегаты ───────────────────────────


def _norm(s: Any) -> str:
    return " ".join(str(s or "").casefold().replace("ё", "е").split())


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v if str(x).strip()]
    s = str(v).strip()
    return [s] if s else []


def _match(value: Any, needles: list[str], exact: bool) -> bool:
    if not needles:
        return True
    hay = _norm(value)
    for n in needles:
        nn = _norm(n)
        if (hay == nn) if exact else (nn in hay):
            return True
    return False


def filter_facts(
    rows: Iterable[dict[str, Any]],
    *,
    report_id: str | Sequence[str] | None = None,
    group: str | Sequence[str] | None = None,
    crop: str | Sequence[str] | None = None,
    firm: str | Sequence[str] | None = None,
    machine: str | Sequence[str] | None = None,
    field: str | Sequence[str] | None = None,
    task_id: int | None = None,
    min_ha: float | None = None,
    exact: bool = False,
) -> list[dict[str, Any]]:
    """Условия объединяются по И; несколько значений одного поля — по ИЛИ.

    По умолчанию сравнение «содержит», без учёта регистра и ё/е:
    group="минеральных" найдёт «1.1 Внесение минеральных удобрений», crop="соя" — «Соя».
    """
    rid = _as_list(report_id)
    g, c, f, m, fl = (_as_list(x) for x in (group, crop, firm, machine, field))
    out: list[dict[str, Any]] = []
    for r in rows:
        if rid and str(r.get("report_id")) not in rid:
            continue
        if task_id is not None and r.get("task_id") != int(task_id):
            continue
        if min_ha is not None and float(r.get("ha") or 0) < float(min_ha):
            continue
        if not (
            _match(r.get("group"), g, exact)
            and _match(r.get("crop"), c, exact)
            and _match(r.get("firm"), f, exact)
            and _match(r.get("machine"), m, exact)
            and _match(r.get("field"), fl, exact)
        ):
            continue
        out.append(r)
    return out


def aggregate(
    rows: Iterable[dict[str, Any]],
    by: Sequence[str] = ("firm",),
) -> list[dict[str, Any]]:
    """Детерминированные итоги без модели: ha, число заданий / машин / полей."""
    acc: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        key = tuple(r.get(k) for k in by)
        a = acc.setdefault(
            key,
            {**{k: r.get(k) for k in by}, "ha": 0.0, "_t": set(), "_m": set(), "_f": set()},
        )
        a["ha"] += float(r.get("ha") or 0)
        a["_t"].add(r.get("task_id"))
        a["_m"].add(r.get("machine_id"))
        a["_f"].add(r.get("field_id"))
    out = []
    for a in acc.values():
        out.append({
            **{k: a[k] for k in by},
            "ha": round(a["ha"], 2),
            "tasks": len(a["_t"]),
            "machines": len(a["_m"]),
            "fields": len(a["_f"]),
        })
    out.sort(key=lambda x: -x["ha"])
    return out


def distinct_values(rows: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """Допустимые значения фильтров (для выпадающих списков и подсказки модели)."""
    sets: dict[str, set[str]] = {k: set() for k in ("report_id", "group", "crop", "firm")}
    for r in rows:
        for k in sets:
            v = str(r.get(k) or "").strip()
            if v:
                sets[k].add(v)
    return {k: sorted(v) for k, v in sets.items()}


# ─────────────────────────── компактный вывод для LLM ───────────────────────────


def estimate_tokens(text: str) -> int:
    """Грубая оценка (≈2.6 символа на токен для русского текста); не заменяет usage из API."""
    return int(len(text) / 2.6) + 1


def _fmt_ha(v: float) -> str:
    return f"{v:.2f}"


def _top_totals(rows: Sequence[dict[str, Any]], key: str, limit: int = 15) -> str:
    agg = aggregate(rows, (key,))
    parts = [f"{a[key] or '—'}={_fmt_ha(a['ha'])}" for a in agg[:limit]]
    if len(agg) > limit:
        rest = sum(a["ha"] for a in agg[limit:])
        parts.append(f"…ещё {len(agg) - limit}={_fmt_ha(rest)}")
    return "; ".join(parts)


def format_for_llm(
    rows: Sequence[dict[str, Any]],
    *,
    columns: Sequence[str] | None = None,
    max_rows: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """TSV-срез для модели: константные колонки в шапке, итоги по ВСЕМ строкам.

    Возвращает (текст, meta). meta: rows_total, rows_sent, ha_total, truncated, chars, est_tokens.
    """
    if max_rows is None:
        try:
            max_rows = int(os.environ.get("PROVERKA_FACTS_MAX_ROWS", "360"))
        except ValueError:
            max_rows = 120
    max_rows = max(1, int(max_rows))
    cols = [c for c in (columns or LLM_DEFAULT_COLUMNS) if c in COLUMNS and c != "ha"]
    cols.append("ha")

    rows = list(rows)
    n = len(rows)
    total_ha = round(sum(float(r.get("ha") or 0) for r in rows), 2)

    consts: dict[str, str] = {}
    if n:
        for c in dict.fromkeys([*_CONTEXT_COLUMNS, *cols]):
            if c == "ha":
                continue
            vals = {str(r.get(c) if r.get(c) is not None else "") for r in rows}
            if len(vals) == 1:
                v = next(iter(vals))
                if v and (c in cols or c in _CONTEXT_COLUMNS):
                    consts[c] = v
    table_cols = [c for c in cols if c not in consts]

    ordered = sorted(rows, key=lambda r: (-float(r.get("ha") or 0), str(r.get("firm")), r.get("task_id") or 0))
    shown = ordered[:max_rows]

    lines: list[str] = []
    if consts:
        lines.append("# " + "; ".join(f"{k}={v}" for k, v in consts.items()))
    lines.append("\t".join(table_cols))
    for r in shown:
        cells = []
        for c in table_cols:
            v = r.get(c)
            cells.append(_fmt_ha(float(v or 0)) if c == "ha" else _cell(v))
        lines.append("\t".join(cells))
    truncated = n > len(shown)
    if truncated:
        rest = ordered[len(shown):]
        rest_ha = round(sum(float(r.get("ha") or 0) for r in rest), 2)
        lines.append(f"… не показано {len(rest)} строк на {_fmt_ha(rest_ha)} га (мельче показанных)")
    lines.append(f"Итого: {n} строк, {_fmt_ha(total_ha)} га")
    if "firm" not in consts and n:
        lines.append("По фирмам: " + _top_totals(rows, "firm"))
    if "crop" in cols and "crop" not in consts and n:
        lines.append("По культурам: " + _top_totals(rows, "crop"))

    text = "\n".join(lines)
    meta = {
        "rows_total": n,
        "rows_sent": len(shown),
        "ha_total": total_ha,
        "truncated": truncated,
        "chars": len(text),
        "est_tokens": estimate_tokens(text),
    }
    return text, meta


# ─────────────────────────── съём во время расчёта ───────────────────────────
# Список живёт в contextvar: пока расчёт сводки его не включил, note_* — пустые
# вызовы и на гектары не влияют. Запись — отдельный TSV после успешного расчёта.

_SINK: ContextVar[list[dict[str, Any]] | None] = ContextVar("task_fact_sink", default=None)
_PENDING: ContextVar[list[tuple[tuple, dict[str, Any]]] | None] = ContextVar(
    "task_fact_pending", default=None,
)


def capture_on() -> bool:
    return _SINK.get() is not None


def begin_task_facts() -> tuple[Any, Any]:
    return _SINK.set([]), _PENDING.set([])


def take_task_facts(tokens: tuple[Any, Any]) -> list[dict[str, Any]]:
    rows = list(_SINK.get() or [])
    _SINK.reset(tokens[0])
    _PENDING.reset(tokens[1])
    return rows


def discard_task_facts(tokens: tuple[Any, Any]) -> None:
    _SINK.reset(tokens[0])
    _PENDING.reset(tokens[1])


def _clean_row(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        ha = float(row.get("ha") or 0)
    except (TypeError, ValueError):
        return None
    if ha <= 0:
        return None
    out = dict(row)
    out["ha"] = ha
    out["group"] = str(out.get("group") or "")
    out["firm"] = str(out.get("firm") or "")
    out["crop"] = str(out.get("crop") or "")
    return out


def note_fact(**row: Any) -> None:
    """Уже принятые гектары. Не вызывается, если съём выключен."""
    try:
        sink = _SINK.get()
        if sink is None:
            return
        clean = _clean_row(row)
        if clean is not None:
            sink.append(clean)
    except Exception:
        return


def add_rows(rows: Iterable[dict[str, Any]]) -> None:
    sink = _SINK.get()
    if sink is None:
        return
    for row in rows:
        clean = _clean_row(row)
        if clean is not None:
            sink.append(clean)


def note_pending(key: tuple, **row: Any) -> None:
    """Черновик до порога на машину. key = (group, firm, machine_id)."""
    try:
        pending = _PENDING.get()
        if pending is None:
            return
        clean = _clean_row(row)
        if clean is not None:
            pending.append((key, clean))
    except Exception:
        return


def commit_group(group: str, ok_pairs: set[tuple] | None) -> None:
    """Переносит черновик группы в факты.

    ok_pairs is None — все строки группы.
    ok_pairs — множество (firm, machine_id), остальные строки группы отбрасываются.
    """
    try:
        pending = _PENDING.get()
        sink = _SINK.get()
    except Exception:
        return
    if pending is None or sink is None:
        return
    try:
        left: list[tuple[tuple, dict[str, Any]]] = []
        for key, row in pending:
            if not key or key[0] != group:
                left.append((key, row))
                continue
            pair = (key[1], key[2]) if len(key) >= 3 else None
            if ok_pairs is None or (pair is not None and pair in ok_pairs):
                sink.append(row)
        pending.clear()
        pending.extend(left)
    except Exception:
        return


def fill_missing_crop(field_to_crop: dict[int, str] | None) -> None:
    sink = _SINK.get()
    if not sink or not field_to_crop:
        return
    for row in sink:
        if str(row.get("crop") or "").strip():
            continue
        fid = row.get("field_id")
        if fid is None:
            continue
        try:
            row["crop"] = str(field_to_crop.get(int(fid)) or "")
        except (TypeError, ValueError):
            continue


def bind_capture():
    """Локальные функции для горячего цикла: None, если съём выключен."""
    if not capture_on():
        return None
    return note_fact, note_pending, commit_group


def safe_store_task_facts(
    rows: Sequence[dict[str, Any]],
    *,
    report_id: str,
    report_date: date,
    operations: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Пишет TSV. Ошибка записи не выходит наружу."""
    if not facts_enabled():
        return None
    try:
        stamped: list[dict[str, Any]] = []
        iso = report_date.isoformat()
        for raw in rows:
            row = dict(raw)
            row["report_id"] = str(report_id)
            row["date"] = str(row.get("date") or iso)
            row["group"] = str(row.get("group") or "")
            row["firm"] = str(row.get("firm") or "")
            row["crop"] = str(row.get("crop") or "")
            row["field"] = str(row.get("field") or "")
            row["machine"] = str(row.get("machine") or "")
            row["own"] = str(row.get("own") or "")
            row["season"] = str(row.get("season") or "")
            row["end"] = str(row.get("end") or "")
            try:
                row["ha"] = float(row.get("ha") or 0)
            except (TypeError, ValueError):
                row["ha"] = 0.0
            if row["ha"] <= 0 or not row["group"]:
                continue
            stamped.append(row)
        path = write_task_facts(stamped, report_id=str(report_id), report_date=report_date)
        checks: dict[str, Any] = {}
        for group, op in (operations or {}).items():
            by_firm = (op or {}).get("area_today_by_firm") or {}
            if not isinstance(by_firm, dict) or not by_firm:
                continue
            grow = [r for r in stamped if r.get("group") == group]
            checks[str(group)] = reconcile_with_area(grow, {str(k): float(v or 0) for k, v in by_firm.items()})
        return {
            "file": path.name if path else None,
            "rows": len(stamped),
            "groups": checks,
        }
    except Exception as exc:  # noqa: BLE001 — файл фактов не должен ронять сводку
        return {"error": f"{type(exc).__name__}: {exc}"[:300], "rows": 0}
