"""Сверка уборки (5.0): Excel сводок 1, 2, 10, 11, 13 между собой и с Cropwise UI.

Сводки заново не считаются: читаются уже сохранённые .xlsx в uploads/<дата>/ — ровно
те цифры, что видит пользователь. Cropwise — work.json по cookie за смену и за сезон.

Каждое значение сводится к ключу (агрофирма, культура, показатель), где показатель —
``day`` (за сутки) или ``season`` (с начала уборки). Агрофирма или культура может быть
``ALL`` — итог по холдингу или по всем культурам фирмы. Если сводка даёт только
разбивку, итоги досчитываются суммой и помечаются ``derived``.

Подчинённые юрлица Cropwise (ООО «Агрофирма 5» в папке Дрожжаного и т.п.) уже
сложены в родителя: в Excel — генерацией сводок, в work.json — разбором дерева
папок в ``cropwise_ui``.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from openpyxl import load_workbook

from plans import FIRM_ORDER
from proverka.file_values import find_report_xlsx

OPERATION = "5.0 Уборка"
ALL = "ALL"
DAY = "day"
SEASON = "season"
REPORT_IDS = ("1", "2", "10", "11", "13")
CROPWISE = "cropwise"
DEFAULT_TOLERANCE_HA = 0.5

CORN_PARENT = "Кукуруза, всего"
CORN_CHILDREN = ("Кукуруза на зерно", "Кукуруза на силос")
CROP_ORDER = (
    "Рапс озимый", "Пшеница озимая", "Тритикале озимая", "Рапс яровой", "Соя",
    "Подсолнечник", CORN_PARENT, *CORN_CHILDREN,
)

REPORT_TITLES = {
    "1": "1. ХК Крупно",
    "2": "2. ХК Детальная",
    "10": "10. Уборка культур",
    "11": "11. Уборка",
    "13": "13. Обзор ХК",
}

_MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
_TOTAL_LABELS = frozenset({"итого", "всего"})
_FIRM_HEADERS = ("Агрофирма", "Агрофирмы")
_CROP_HEADERS = ("Культура",)


# ----------------------------------------------------------------------------- имена
def _key(text: Any) -> str:
    return "".join(ch for ch in str(text or "").casefold().replace("ё", "е") if ch.isalnum())


def _flat(cell: Any) -> str:
    return " ".join(str(cell or "").replace("\n", " ").split())


def _firm_aliases() -> dict[str, str]:
    try:
        from firm_sheet_names import FIRM_DISPLAY_NAMES, FIRM_LABELS_TABLE
    except ImportError:
        FIRM_DISPLAY_NAMES, FIRM_LABELS_TABLE = {}, {}
    out: dict[str, str] = {}
    for firm in FIRM_ORDER:
        out[_key(firm)] = firm
        for table in (FIRM_DISPLAY_NAMES, FIRM_LABELS_TABLE):
            if table.get(firm):
                out[_key(table[firm])] = firm
                out[_key(str(table[firm]).replace("ООО", "", 1))] = firm
    return out


_FIRMS = _firm_aliases()


def canonical_firm(label: Any) -> str | None:
    """«Нурлат Агро», «ООО "НУРЛАТ АГРО"», «Лениногорская Птицефаб» → имя из FIRM_ORDER."""
    k = _key(label)
    if not k:
        return None
    if k in _FIRMS:
        return _FIRMS[k]
    hits = {firm for alias, firm in _FIRMS.items() if len(k) >= 6 and (alias.startswith(k) or k.startswith(alias))}
    return hits.pop() if len(hits) == 1 else None


def canonical_crop(label: Any) -> str:
    text = " ".join(str(label or "").replace("ё", "е").split()).rstrip(":").strip()
    low = text.casefold()
    if low.startswith("кукуруза, из них") or low in ("кукуруза всего", "кукуруза, всего"):
        return CORN_PARENT
    for name in CROP_ORDER:
        if low == name.casefold().replace("ё", "е"):
            return name
    return text[:1].upper() + text[1:] if text else text


def _crop_sort_key(crop: str) -> tuple[int, str]:
    if crop == ALL:
        return (-1, "")
    try:
        return (CROP_ORDER.index(crop), crop)
    except ValueError:
        return (len(CROP_ORDER), crop)


def _firm_sort_key(firm: str) -> tuple[int, str]:
    if firm == ALL:
        return (-1, "")
    try:
        return (FIRM_ORDER.index(firm), firm)
    except ValueError:
        return (len(FIRM_ORDER), firm)


def shift_date(report_date: date) -> date:
    return report_date - timedelta(days=1)


def _day_label(d: date) -> str:
    return f"{d.day} {_MONTHS_GEN[d.month - 1]}"


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# ----------------------------------------------------------------------------- чтение Excel
@dataclass(frozen=True)
class TableSpec:
    """Как читать таблицу уборки на листе.

    sheet — имя листа или предикат по имени. Колонки ищутся по тексту шапки.
    ``firm_from_sheet`` / ``crop_from_sheet`` — второе измерение берётся из имени листа
    (сводка 13: лист на фирму; сводка 10: лист на культуру). ``day_is_date`` — колонка
    суток подписана датой смены («15 октября»), как в сводке 2.
    """

    sheet: str | Callable[[str], bool]
    day: tuple[str, ...] = ()
    season: tuple[str, ...] = ()
    day_is_date: bool = False
    firm_from_sheet: bool = False
    crop_from_sheet: bool = False

    def matches(self, title: str) -> bool:
        if callable(self.sheet):
            return bool(self.sheet(title))
        return _flat(title) == _flat(self.sheet)


def _is_firm_sheet(title: str) -> bool:
    return canonical_firm(title) is not None


def _is_crop_sheet_10(title: str) -> bool:
    return _flat(title) not in ("Общий намолот", "Овощи")


SPECS: dict[str, tuple[TableSpec, ...]] = {
    "1": (
        TableSpec("Выраб. — 5.0 Уборка", day=("Обработано за сутки, га",)),
        TableSpec("Общий намолот", season=("Факт общий, га",), day=("Факт за сутки, га",)),
    ),
    "2": (
        TableSpec("5.0 Уборка", season=("Выполнено работ в Га", "Выполнено, га"), day_is_date=True),
    ),
    "10": (
        TableSpec("Общий намолот", season=("Факт общий, га",), day=("Факт за сутки, га",)),
        TableSpec(_is_crop_sheet_10, season=("Факт общий, га",), day=("Факт за сутки, га",), crop_from_sheet=True),
    ),
    "11": (
        TableSpec("Сводка", day=("Убрано за сутки, га",)),
    ),
    "13": (
        TableSpec("Зерновые культуры", season=("Убранная площадь, га",), day=("Убрано за сутки, га",)),
        TableSpec(_is_firm_sheet, season=("Факт общий, га",), day=("Факт за сутки, га",), firm_from_sheet=True),
    ),
}


@dataclass
class Fact:
    firm: str
    crop: str
    metric: str
    value: float
    where: str
    derived: bool = False


def _find(values: list[Any], names: tuple[str, ...]) -> int | None:
    wanted = {_flat(n).casefold() for n in names}
    for i, cell in enumerate(values):
        if _flat(cell).casefold() in wanted:
            return i
    return None


def _header(values: list[Any], spec: TableSpec, day_label: str) -> dict[str, int] | None:
    firm_i = _find(values, _FIRM_HEADERS)
    crop_i = _find(values, _CROP_HEADERS)
    if firm_i is None and crop_i is None:
        return None
    season_i = _find(values, spec.season) if spec.season else None
    if spec.day_is_date:
        # В таблицах машин и выработки сводки 2 тоже есть колонки-даты; берём только ту,
        # где рядом колонка «Выполнено», то есть таблицу гектаров.
        day_i = _find(values, (day_label,)) if season_i is not None else None
    else:
        day_i = _find(values, spec.day) if spec.day else None
    if season_i is None and day_i is None:
        return None
    cols = {}
    if firm_i is not None:
        cols["firm"] = firm_i
    if crop_i is not None:
        cols["crop"] = crop_i
    if season_i is not None:
        cols[SEASON] = season_i
    if day_i is not None:
        cols[DAY] = day_i
    return cols


def _read_sheet(ws: Any, spec: TableSpec, day_label: str) -> tuple[list[Fact], list[str]]:
    facts: list[Fact] = []
    unknown: list[str] = []
    sheet_firm = canonical_firm(ws.title) if spec.firm_from_sheet else None
    sheet_crop = canonical_crop(ws.title) if spec.crop_from_sheet else None
    cols: dict[str, int] | None = None
    for r, row in enumerate(ws.iter_rows(values_only=True), start=1):
        values = list(row)
        head = _header(values, spec, day_label)
        if head:
            cols = head
            continue
        if cols is None:
            continue
        if not any(v not in (None, "") for v in values):
            cols = None
            continue
        if _find(values, _FIRM_HEADERS) is not None or _find(values, _CROP_HEADERS) is not None:
            cols = None  # шапка соседней таблицы без нужных колонок
            continue

        def cell(name: str) -> Any:
            i = cols.get(name)
            return values[i] if i is not None and i < len(values) else None

        labels = [str(v).strip() for v in values if isinstance(v, str) and v.strip()]
        is_total = any(_key(v) in _TOTAL_LABELS for v in labels[:2])
        firm = sheet_firm
        crop = sheet_crop
        if "firm" in cols:
            raw = cell("firm")
            if is_total:
                firm = ALL
            elif raw not in (None, ""):
                firm = canonical_firm(raw)
                if firm is None:
                    unknown.append(f"{ws.title}: «{raw}»")
                    continue
        if "crop" in cols:
            raw = cell("crop")
            if is_total:
                crop = ALL
            elif raw not in (None, ""):
                crop = canonical_crop(raw)
            elif "firm" in cols:
                continue  # строка-заголовок группы фирмы в сводке 2
        if is_total and firm is None:
            firm = ALL
        if is_total and crop is None:
            crop = ALL
        firm = firm or ALL
        crop = crop or ALL
        for metric in (DAY, SEASON):
            if metric not in cols:
                continue
            v = _num(cell(metric))
            if v is None:
                continue
            facts.append(Fact(firm, crop, metric, round(v, 2), f"лист «{ws.title}», строка {r}"))
    return facts, unknown


def read_report(path: Path, report_id: str, report_date: date) -> dict[str, Any]:
    """Факты уборки из одного .xlsx. Ошибки не прячутся: {found, error, facts, warnings}."""
    specs = SPECS[report_id]
    day_label = _day_label(shift_date(report_date))
    try:
        wb = load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        return {"found": False, "error": f"{path.name}: {type(exc).__name__}: {exc}", "facts": [], "warnings": []}
    facts: list[Fact] = []
    warnings: list[str] = []
    try:
        for spec in specs:
            sheets = [ws for ws in wb.worksheets if spec.matches(ws.title)]
            if not sheets:
                label = spec.sheet if isinstance(spec.sheet, str) else "листы по фирмам/культурам"
                warnings.append(f"нет листа «{label}»")
                continue
            for ws in sheets:
                got, unknown = _read_sheet(ws, spec, day_label)
                facts.extend(got)
                warnings.extend(f"не узнали агрофирму {u}" for u in unknown)
    finally:
        wb.close()
    if not facts:
        warnings.append("в файле не найдено ни одного значения уборки")
    return {"found": True, "error": None, "facts": facts, "warnings": warnings}


# ----------------------------------------------------------------------------- итоги
def _rollup(facts: Iterable[Fact]) -> dict[tuple[str, str, str], Fact]:
    """Явные значения + досчитанные итоги. При дубле ключа побеждает первое прочитанное."""
    out: dict[tuple[str, str, str], Fact] = {}
    for f in facts:
        out.setdefault((f.firm, f.crop, f.metric), f)

    def add_derived(key: tuple[str, str, str], parts: list[Fact], what: str) -> None:
        if key in out or not parts:
            return
        out[key] = Fact(*key, round(sum(p.value for p in parts), 2), f"сумма {what}", derived=True)

    for metric in (DAY, SEASON):
        firms = {k[0] for k in out if k[0] != ALL}
        for firm in [*firms, ALL]:
            children = [out[(firm, c, metric)] for c in CORN_CHILDREN if (firm, c, metric) in out]
            add_derived((firm, CORN_PARENT, metric), children, "кукурузы на зерно и силос")
        crops = {k[1] for k in out if k[1] != ALL}
        leaf_crops = [c for c in crops if c != CORN_PARENT]
        for crop in crops:
            add_derived((ALL, crop, metric), [out[(f, crop, metric)] for f in firms if (f, crop, metric) in out], "по агрофирмам")
        for firm in firms:
            add_derived((firm, ALL, metric), [out[(firm, c, metric)] for c in leaf_crops if (firm, c, metric) in out], "по культурам")
        add_derived((ALL, ALL, metric), [out[(f, ALL, metric)] for f in firms if (f, ALL, metric) in out], "по агрофирмам")
    return out


# ----------------------------------------------------------------------------- Cropwise
def _season_start(report_date: date) -> date:
    try:
        from season_unified import combine_season_start_for_date, editor_period_bounds

        saved = editor_period_bounds(OPERATION, report_date)
        return saved[0] if saved else combine_season_start_for_date(report_date)
    except Exception:  # noqa: BLE001 — без настроек как в сводке 10: 10 июня
        start = date(report_date.year, 6, 10)
        return start if report_date >= start else date(report_date.year - 1, 6, 10)


def _iso(d: date, hour: int = 6) -> str:
    return datetime(d.year, d.month, d.day, hour).isoformat()


def cropwise_windows(report_date: date) -> dict[str, tuple[str, str]]:
    return {
        DAY: (_iso(shift_date(report_date)), _iso(report_date)),
        SEASON: (_iso(_season_start(report_date)), _iso(report_date)),
    }


def _cropwise_crop(crop: str) -> str:
    c = canonical_crop(crop)
    return "Кукуруза на зерно" if c.casefold() == "кукуруза" else c


def load_cropwise(cookie: str, report_date: date) -> dict[str, Any]:
    """work.json за смену и за сезон (оба сезона сессии, как в основной проверке)."""
    from proverka.crop_compare import cropwise_area_by_firm_crop
    from proverka.cropwise_ui import WorkLoadJob, load_works_for_seasons, parse_work_report_body, ui_check_years
    from proverka.task_names import tasks_for_group
    from proverka.work_groups import work_type_ids_for_group

    windows = cropwise_windows(report_date)
    wt_ids = work_type_ids_for_group(OPERATION)
    hints = tuple(tasks_for_group(OPERATION))
    years = ui_check_years(report_date)
    jobs = [WorkLoadJob(metric, t0, t1, wt_ids, hints) for metric, (t0, t1) in windows.items()]
    loaded = load_works_for_seasons(cookie, jobs, years=years)

    facts: list[Fact] = []
    status: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    firms = list(FIRM_ORDER)
    for metric in windows:
        by_year = loaded.get(metric) or {}
        per_year: dict[str, Any] = {}
        by_firm: dict[str, float] = defaultdict(float)
        by_firm_crop: dict[tuple[str, str], float] = defaultdict(float)
        total = 0.0
        for year in years:
            slot = by_year.get(year) or {}
            st = slot.get("load_status") or ("loaded" if slot.get("ok") else "not_loaded")
            per_year[str(year)] = {"status": st, "error": slot.get("error")}
            if st != "loaded":
                if st != "empty":
                    warnings.append(f"{'сутки' if metric == DAY else 'сезон'}: сезон {year} не загружен ({slot.get('error') or st})")
                continue
            body = slot.get("body")
            parsed = parse_work_report_body(body, firms, list(hints))
            if parsed.get("fact_column_error"):
                warnings.append(f"сезон {year}: {parsed['fact_column_error']}")
            total += float(parsed.get("fact_total_ha") or 0)
            for firm, ha in (parsed.get("fact_by_firm") or {}).items():
                by_firm[firm] += float(ha or 0)
            unmatched = float(parsed.get("unmatched_firm_ha") or 0)
            if unmatched > 0:
                warnings.append(f"сезон {year}: {unmatched:.2f} га не привязаны к агрофирме")
            for crop, per_firm in cropwise_area_by_firm_crop(body, firms, list(hints)).items():
                for firm, ha in per_firm.items():
                    by_firm_crop[(firm, _cropwise_crop(crop))] += float(ha or 0)
        where = f"work.json {windows[metric][0][:10]} — {windows[metric][1][:10]}"
        if any(v["status"] == "loaded" for v in per_year.values()):
            facts.append(Fact(ALL, ALL, metric, round(total, 2), where))
            facts.extend(Fact(f, ALL, metric, round(v, 2), where) for f, v in by_firm.items())
            facts.extend(Fact(f, c, metric, round(v, 2), where) for (f, c), v in by_firm_crop.items())
        status[metric] = {"window": windows[metric], "years": per_year}
    error = None
    if not facts:
        errors = " ".join(str(y.get("error") or "") for s in status.values() for y in s["years"].values())
        if "401" in errors or "403" in errors:
            error = "Cookie Cropwise недействительна (401) — скопируйте свежую из браузера"
        else:
            error = "Cropwise не отдал данные ни за один сезон"
    return {"facts": facts, "status": status, "warnings": warnings, "error": error}


# ----------------------------------------------------------------------------- сверка
def _status(values: dict[str, float], cw: float | None, tol: float) -> tuple[float | None, float | None, str]:
    nums = list(values.values())
    spread = round(max(nums) - min(nums), 2) if len(nums) >= 2 else None
    cw_diff = None
    if cw is not None and nums:
        cw_diff = round(max(abs(v - cw) for v in nums), 2)
    bad_svodki = spread is not None and spread > tol
    bad_cw = cw_diff is not None and cw_diff > tol
    if bad_svodki and bad_cw:
        return spread, cw_diff, "both"
    if bad_svodki:
        return spread, cw_diff, "svodki"
    if bad_cw:
        return spread, cw_diff, "cropwise"
    if len(nums) + (cw is not None) < 2:
        return spread, cw_diff, "single"
    return spread, cw_diff, "ok"


def run_check(
    report_date: date,
    *,
    cookie: str = "",
    tolerance: float = DEFAULT_TOLERANCE_HA,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    prog = progress or (lambda _msg: None)
    sources: list[dict[str, Any]] = []
    by_source: dict[str, dict[tuple[str, str, str], Fact]] = {}

    for rid in REPORT_IDS:
        prog(f"Читаем Excel сводки {rid}…")
        path = find_report_xlsx(report_date, rid)
        src = {"id": rid, "title": REPORT_TITLES[rid], "file": path.name if path else None}
        if path is None:
            src.update(found=False, error="файл не найден — сгенерируйте сводку за эту дату", warnings=[])
            sources.append(src)
            continue
        got = read_report(path, rid, report_date)
        src.update(found=got["found"], error=got["error"], warnings=got["warnings"], values=len(got["facts"]))
        sources.append(src)
        if got["facts"]:
            by_source[rid] = _rollup(got["facts"])

    cropwise: dict[str, Any] = {"enabled": bool(cookie)}
    if cookie:
        prog("Загружаем Cropwise (work.json за сутки и за сезон)…")
        try:
            cw = load_cropwise(cookie, report_date)
            cropwise.update(status=cw["status"], warnings=cw["warnings"], error=cw["error"])
            if cw["facts"]:
                by_source[CROPWISE] = _rollup(cw["facts"])
        except Exception as exc:  # noqa: BLE001 — сверка Excel между собой остаётся
            cropwise.update(error=f"{type(exc).__name__}: {exc}", warnings=[])

    prog("Сводим таблицу…")
    keys = set()
    for facts in by_source.values():
        keys.update(facts)
    rows: list[dict[str, Any]] = []
    for firm, crop, metric in keys:
        values = {}
        for rid in REPORT_IDS:
            f = by_source.get(rid, {}).get((firm, crop, metric))
            if f is not None:
                values[rid] = {"value": f.value, "where": f.where, "derived": f.derived}
        cwf = by_source.get(CROPWISE, {}).get((firm, crop, metric))
        cw_val = {"value": cwf.value, "where": cwf.where, "derived": cwf.derived} if cwf else None
        if cw_val is None and not values:
            continue
        spread, cw_diff, st = _status(
            {k: v["value"] for k, v in values.items()}, cw_val["value"] if cw_val else None, tolerance,
        )
        rows.append({
            "firm": firm, "crop": crop, "metric": metric,
            "values": values, "cropwise": cw_val,
            "spread": spread, "cropwise_diff": cw_diff, "status": st,
        })
    rows.sort(key=lambda r: (_firm_sort_key(r["firm"]), _crop_sort_key(r["crop"]), r["metric"] != DAY))

    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1
    return {
        "operation": OPERATION,
        "report_date": report_date.isoformat(),
        "shift_date": shift_date(report_date).isoformat(),
        "season_start": _season_start(report_date).isoformat(),
        "tolerance": tolerance,
        "report_ids": list(REPORT_IDS),
        "sources": sources,
        "cropwise": cropwise,
        "rows": rows,
        "summary": dict(counts),
    }


if __name__ == "__main__":  # pragma: no cover — ручной прогон
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", required=True)
    ap.add_argument("--cookie", default="")
    args = ap.parse_args()
    res = run_check(date.fromisoformat(args.date), cookie=args.cookie, progress=print)
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, ensure_ascii=False, indent=2))
    for r in res["rows"]:
        if r["status"] not in ("ok", "single"):
            vals = {k: v["value"] for k, v in r["values"].items()}
            print(r["firm"], "|", r["crop"], "|", r["metric"], vals, "cw:", (r["cropwise"] or {}).get("value"), r["status"])
