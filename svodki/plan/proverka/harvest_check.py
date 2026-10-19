"""Сверка агроопераций: Excel сводок между собой и с Cropwise UI.

Сводки заново не считаются: читаются уже сохранённые .xlsx в uploads/<дата>/ — ровно
те цифры, что видит пользователь. Cropwise — work.json по cookie за смену и за сезон.

Каждое значение сводится к ключу (операция, агрофирма, культура, показатель), где
показатель — ``day`` (за сутки) или ``season`` (с начала сезона операции). Агрофирма
или культура может быть ``ALL`` — итог по холдингу или по всем культурам фирмы. Если
сводка даёт только разбивку, итоги досчитываются суммой и помечаются ``derived``.

Как читать листы каждой операции, задано в ``SPECS``: новая операция добавляется
тем же словарём (сводка → лист, колонки суток и сезона, при необходимости секция).

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

ALL = "ALL"
DAY = "day"
SEASON = "season"
CROPWISE = "cropwise"
DEFAULT_TOLERANCE_HA = 0.5  # допуск для суточных значений, га
# Допуск для сезонных значений: за сезон копится округление, поэтому он шире суточного
# (как красная точка «Cropwise за сезон»).
DEFAULT_TOLERANCE_SEASON_HA = 10.0
_DAY_PERF = ("Обработано за сутки, га",)
_DAY_VOLUME = ("Общий объем выполненной работы за сутки, га",)
_SEASON_R2 = ("Выполнено работ в Га", "Выполнено, га")
_FACT = ("Факт общий, га",)
_FACT_DAY = ("Факт за сутки, га",)

CORN_PARENT = "Кукуруза, всего"
CORN_CHILDREN = ("Кукуруза на зерно", "Кукуруза на силос")
CROP_ORDER = (
    "Рапс озимый", "Пшеница озимая", "Тритикале озимая", "Рапс яровой", "Соя",
    "Подсолнечник", CORN_PARENT, *CORN_CHILDREN,
)

REPORT_TITLES = {
    "1": "1. ХК Крупно",
    "2": "2. ХК Детальная",
    "3": "3. Внесение МУ",
    "4": "4. Боронование",
    "5": "5. Посев",
    "6": "6. Опрыскивание",
    "7": "7. Дискование",
    "8": "8. Культивация",
    "9": "9. Вспашка и глубокорыхление",
    "10": "10. Уборка культур",
    "11": "11. Уборка",
    "13": "13. Обзор ХК",
}
_REPORT_ORDER = tuple(REPORT_TITLES)

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
    """Как читать одну таблицу на листе.

    sheet — имя листа или предикат по имени. Excel режет имя до 31 символа:
    полное имя в спецификации совпадает с обрезанным листом по префиксу.
    ``section`` — заголовок блока на общем листе («2.4 Вспашка», «КУЛЬТУРЫ»):
    читается только этот блок, до пустой строки или следующего заголовка.
    ``firm_from_sheet`` / ``crop_from_sheet`` — второе измерение берётся из имени листа.
    ``crop_prefix`` снимается с имени листа перед названием культуры («Вспашка », «Посев »).
    ``day_is_date`` — колонка суток подписана датой смены («15 октября»), как в сводке 2.
    """

    sheet: str | Callable[[str], bool]
    day: tuple[str, ...] = ()
    season: tuple[str, ...] = ()
    day_is_date: bool = False
    firm_from_sheet: bool = False
    crop_from_sheet: bool = False
    section: str | None = None
    crop_prefix: str = ""

    def matches(self, title: str) -> bool:
        if callable(self.sheet):
            return bool(self.sheet(title))
        want = _flat(self.sheet)
        got = _flat(title)
        if want == got:
            return True
        return len(str(title)) == 31 and len(got) >= 12 and want.startswith(got)


def _r2(sheet: str) -> TableSpec:
    return TableSpec(sheet, season=_SEASON_R2, day_is_date=True)


def _perf(sheet: str, section: str | None = None) -> TableSpec:
    return TableSpec(sheet, day=_DAY_PERF, section=section)


def _summary(day: tuple[str, ...], section: str | None = None, sheet: str = "Сводка") -> TableSpec:
    return TableSpec(sheet, day=day, section=section)


def _is_firm_sheet(title: str) -> bool:
    return canonical_firm(title) is not None


def _is_crop_sheet_10(title: str) -> bool:
    return _flat(title) not in ("Общий намолот", "Овощи")


def _is_plow_crop_sheet(title: str) -> bool:
    return str(title or "").startswith("Вспашка ")


def _is_seeding_crop_sheet(title: str) -> bool:
    return str(title or "").startswith("Посев ")


# Операция → сводка → как читать её листы. Порядок операций — порядок в таблице.
SPECS: dict[str, dict[str, tuple[TableSpec, ...]]] = {
    "1.1 Внесение минеральных удобрений": {
        "1": (_perf("Выработка техники"),),
        "2": (_r2("1.1 Внесение минеральных удобрений"),),
        "3": (_summary(_DAY_PERF),),
    },
    "2.1 Боронование": {
        "1": (_perf("Выраб. — 2.1 Боронование"),),
        "2": (_r2("2.1 Боронование"),),
        "4": (_summary(_DAY_PERF),),
    },
    "2.9 Дискование": {
        "1": (_perf("Выраб. — 2.9 Дискование"),),
        "2": (_r2("2.9 Дискование"),),
        "7": (_summary(_DAY_VOLUME),),
    },
    "2.4 Вспашка": {
        "1": (_perf("Вспашка + Глубокорыхление", section="2.4 Вспашка"),),
        "2": (_r2("2.4 Вспашка"),),
        "9": (
            _summary(_DAY_PERF, section="2.4 Вспашка"),
            TableSpec("Итого (все АФ)", season=_FACT, day=_FACT_DAY),
            TableSpec(
                _is_plow_crop_sheet, season=_FACT, day=_FACT_DAY,
                crop_from_sheet=True, crop_prefix="Вспашка ",
            ),
        ),
    },
    "2.8 Глубокорыхление": {
        "1": (_perf("Вспашка + Глубокорыхление", section="2.8 Глубокорыхление"),),
        "2": (_r2("2.8 Глубокорыхление"),),
        "9": (
            _summary(_DAY_PERF, section="2.8 Глубокорыхление"),
            TableSpec("Глубокорыхление", season=_FACT, day=_FACT_DAY),
        ),
    },
    "2.11 Культивация": {
        "1": (_perf("Выраб. — 2.11 Культивация"),),
        "2": (_r2("2.11 Культивация"),),
        "8": (_summary(_DAY_VOLUME),),
    },
    "4.0 Посев": {
        "1": (_perf("Выраб. — 4.0 Посев"),),
        "2": (_r2("4.0 Посев"),),
        "5": (
            _summary(_DAY_VOLUME, section="КУЛЬТУРЫ"),
            TableSpec(
                _is_seeding_crop_sheet, season=_FACT, day=_FACT_DAY,
                crop_from_sheet=True, crop_prefix="Посев ",
            ),
        ),
    },
    "3.1 Опрыскивание": {
        "1": (_perf("Выраб. — 3.1 Опрыскивание"),),
        "2": (_r2("3.1 Опрыскивание"),),
        "6": (_summary(_DAY_VOLUME, section="3.1 Опрыскивание"),),
    },
    "3.2 Опрыскивание (овощи)": {
        "1": (_perf("Выраб. — 3.2 Опрыскивание (овощи)"),),
        "2": (_r2("3.2 Опрыскивание (овощи)"),),
        "6": (_summary(_DAY_VOLUME, section="Опрыскивание овощных культур"),),
    },
    "5.0 Уборка": {
        "1": (
            TableSpec("Выраб. — 5.0 Уборка", day=_DAY_PERF),
            TableSpec("Общий намолот", season=_FACT, day=_FACT_DAY),
        ),
        "2": (_r2("5.0 Уборка"),),
        "10": (
            TableSpec("Общий намолот", season=_FACT, day=_FACT_DAY),
            TableSpec(_is_crop_sheet_10, season=_FACT, day=_FACT_DAY, crop_from_sheet=True),
        ),
        "11": (_summary(("Убрано за сутки, га",)),),
        "13": (
            TableSpec("Зерновые культуры", season=("Убранная площадь, га",), day=("Убрано за сутки, га",)),
            TableSpec(_is_firm_sheet, season=_FACT, day=_FACT_DAY, firm_from_sheet=True),
        ),
    },
    "4.1 Посев (овощи)": {
        "2": (_r2("4.1 Посев (овощи)"),),
        "5": (_summary(_DAY_VOLUME, section="Посев / посадка овощных культур"),),
    },
    "2.12 Прикатывание": {
        "1": (_perf("Выраб. — 2.12 Прикатывание"),),
        "2": (_r2("2.12 Прикатывание"),),
    },
    "2.7 Гребнеобразование": {
        "1": (_perf("Выраб. — 2.7 Гребнеобразование"),),
        "2": (_r2("2.7 Гребнеобразование"),),
    },
    "2.6 Фрезерование": {
        "1": (_perf("Выраб. — 2.6 Фрезерование"),),
        "2": (_r2("2.6 Фрезерование"),),
    },
    "5.4 Уборка картофеля, лука": {
        "1": (
            TableSpec("Сводка овощи", day=("Убрано за сутки, га",)),
            TableSpec("Овощи", season=_FACT, day=_FACT_DAY),
        ),
        "2": (_r2("5.4 Уборка овощей"),),
        "10": (TableSpec("Овощи", season=_FACT, day=_FACT_DAY),),
    },
}


def all_report_ids() -> list[str]:
    used = {rid for specs in SPECS.values() for rid in specs}
    return [rid for rid in _REPORT_ORDER if rid in used]


REPORT_IDS = tuple(all_report_ids())


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


def _lone_text(values: list[Any]) -> str | None:
    texts = [_flat(v) for v in values if v not in (None, "")]
    return texts[0] if len(texts) == 1 else None


def _same_text(a: str, b: str) -> bool:
    return _flat(a).casefold() == _flat(b).casefold()


def _sheet_crop(title: str, spec: TableSpec) -> str | None:
    if not spec.crop_from_sheet:
        return None
    raw = str(title or "")
    if spec.crop_prefix and raw.startswith(spec.crop_prefix):
        raw = raw[len(spec.crop_prefix):]
    return canonical_crop(raw)


def _read_sheet(ws: Any, spec: TableSpec, day_label: str) -> tuple[list[Fact], list[str]]:
    facts: list[Fact] = []
    unknown: list[str] = []
    sheet_firm = canonical_firm(ws.title) if spec.firm_from_sheet else None
    sheet_crop = _sheet_crop(ws.title, spec)
    cols: dict[str, int] | None = None
    in_section = spec.section is None
    for r, row in enumerate(ws.iter_rows(values_only=True), start=1):
        values = list(row)
        lone = _lone_text(values)
        if spec.section:
            if lone is not None and _same_text(lone, spec.section):
                in_section = True
                cols = None
                continue
            if not in_section:
                continue
            if lone is not None:
                in_section = False
                cols = None
                continue
        head = _header(values, spec, day_label)
        if head:
            cols = head
            continue
        if cols is None:
            continue
        if not any(v not in (None, "") for v in values):
            cols = None
            if spec.section:
                in_section = False
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


def _spec_label(spec: TableSpec) -> str:
    if isinstance(spec.sheet, str):
        label = spec.sheet
    else:
        label = "листы по фирмам/культурам"
    if spec.section:
        return f"{label} / {spec.section}"
    return label


def read_specs(wb: Any, specs: tuple[TableSpec, ...], report_date: date) -> tuple[list[Fact], list[str]]:
    """Факты операции из уже открытой книги."""
    day_label = _day_label(shift_date(report_date))
    facts: list[Fact] = []
    warnings: list[str] = []
    for spec in specs:
        sheets = [ws for ws in wb.worksheets if spec.matches(ws.title)]
        if not sheets:
            warnings.append(f"нет листа «{_spec_label(spec)}»")
            continue
        for ws in sheets:
            got, unknown = _read_sheet(ws, spec, day_label)
            facts.extend(got)
            warnings.extend(f"не узнали агрофирму {u}" for u in unknown)
    if not facts:
        warnings.append("в файле не найдено ни одного значения")
    return facts, warnings


def read_report(path: Path, report_id: str, report_date: date, operation: str = "5.0 Уборка") -> dict[str, Any]:
    """Факты одной операции из одного .xlsx. Ошибки не прячутся: {found, error, facts, warnings}."""
    specs = SPECS[operation][report_id]
    try:
        wb = load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        return {"found": False, "error": f"{path.name}: {type(exc).__name__}: {exc}", "facts": [], "warnings": []}
    try:
        facts, warnings = read_specs(wb, specs, report_date)
    finally:
        wb.close()
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
def season_scope(report_date: date, operation: str) -> dict[str, Any]:
    """Сезон операции так же, как его берут сводки: начало окна и сезоны Cropwise.

    years=None — сводка не режет machine_task.season, берутся все сезоны сессии.
    """
    from proverka.cropwise_ui import ui_check_years
    from season_unified import applies_task_season_filter, resolve_group_season_config

    cfg = resolve_group_season_config(operation, report_date)
    start = min(date.fromisoformat(cfg["period_start"]), report_date)
    task_years = [int(y) for y in (cfg.get("task_season_years") or [])]
    years = sorted(set(task_years)) if task_years and applies_task_season_filter(operation) else None
    return {
        "start": start,
        "years": years,
        "load_years": years or list(ui_check_years(report_date)),
        "harvest": operation.startswith("5.0 Уборка"),
    }


def _season_start_iso(report_date: date, operation: str) -> str | None:
    try:
        return season_scope(report_date, operation)["start"].isoformat()
    except Exception:  # noqa: BLE001 — подпись в UI не должна ронять сверку
        return None


def _iso(d: date, hour: int = 6) -> str:
    return datetime(d.year, d.month, d.day, hour).isoformat()


def cropwise_windows(report_date: date, operation: str, scope: dict[str, Any] | None = None) -> dict[str, tuple[str, str]]:
    scope = scope or season_scope(report_date, operation)
    # Уборка считает сезон от начала смены (сводка 10), остальные сводки — с полуночи.
    start_hour = 6 if scope["harvest"] else 0
    return {
        DAY: (_iso(shift_date(report_date)), _iso(report_date)),
        SEASON: (_iso(scope["start"], start_hour), _iso(report_date)),
    }


def _cropwise_crop(crop: str) -> str:
    c = canonical_crop(crop)
    return "Кукуруза на зерно" if c.casefold() == "кукуруза" else c


def load_cropwise(cookie: str, report_date: date, operation: str) -> dict[str, Any]:
    """work.json одной операции за смену и за сезон — только сезоны, которые учитывает сводка."""
    from proverka.crop_compare import cropwise_area_by_firm_crop
    from proverka.cropwise_ui import WorkLoadJob, load_works_for_seasons, parse_work_report_body
    from proverka.task_names import tasks_for_group
    from proverka.work_groups import work_type_ids_for_group

    scope = season_scope(report_date, operation)
    windows = cropwise_windows(report_date, operation, scope)
    wt_ids = work_type_ids_for_group(operation)
    hints = tuple(tasks_for_group(operation))
    years = scope["load_years"]
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
    return {"facts": facts, "status": status, "warnings": warnings, "error": error, "years": years}


# ----------------------------------------------------------------------------- сверка
def tolerance_for_metric(metric: str, day: float, season: float) -> float:
    """Допуск расхождения, га: для сезонных значений свой, шире суточного."""
    return season if metric == SEASON else day


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
    tolerance: float | None = None,
    tolerance_season: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    prog = progress or (lambda _msg: None)
    if tolerance is None:
        tolerance = DEFAULT_TOLERANCE_HA
    if tolerance_season is None:
        tolerance_season = DEFAULT_TOLERANCE_SEASON_HA
    operations = list(SPECS)
    sources: list[dict[str, Any]] = []
    opened: dict[str, Any] = {}
    file_error: dict[str, str] = {}

    for rid in REPORT_IDS:
        prog(f"Читаем Excel сводки {rid}…")
        path = find_report_xlsx(report_date, rid)
        src = {"id": rid, "title": REPORT_TITLES[rid], "file": path.name if path else None, "warnings": []}
        if path is None:
            src.update(found=False, error="файл не найден — сгенерируйте сводку за эту дату")
            file_error[rid] = src["error"]
            sources.append(src)
            continue
        try:
            opened[rid] = load_workbook(path, data_only=True, read_only=True)
        except Exception as exc:  # noqa: BLE001
            src.update(found=False, error=f"{path.name}: {type(exc).__name__}: {exc}")
            file_error[rid] = src["error"]
            sources.append(src)
            continue
        src.update(found=True, error=None)
        sources.append(src)

    by_source: dict[tuple[str, str], dict[tuple[str, str, str], Fact]] = {}
    op_warnings: dict[str, list[str]] = {op: [] for op in operations}
    try:
        for op in operations:
            for rid, specs in SPECS[op].items():
                if rid in file_error:
                    op_warnings[op].append(f"сводка {rid}: {file_error[rid]}")
                    continue
                facts, warnings = read_specs(opened[rid], specs, report_date)
                op_warnings[op].extend(f"сводка {rid}: {w}" for w in warnings)
                if facts:
                    by_source[(op, rid)] = _rollup(facts)
    finally:
        for wb in opened.values():
            wb.close()

    cropwise: dict[str, Any] = {"enabled": bool(cookie)}
    cw_by_op: dict[str, dict[str, Any]] = {}
    if cookie:
        for op in operations:
            prog(f"Cropwise: {op}…")
            try:
                cw = load_cropwise(cookie, report_date, op)
            except Exception as exc:  # noqa: BLE001 — сверка Excel между собой остаётся
                cw = {"facts": [], "status": {}, "warnings": [], "error": f"{type(exc).__name__}: {exc}"}
            cw_by_op[op] = cw
            if cw["facts"]:
                by_source[(op, CROPWISE)] = _rollup(cw["facts"])

    prog("Сводим таблицу…")
    rows: list[dict[str, Any]] = []
    by_operation: dict[str, Any] = {}
    for op in operations:
        ids = list(SPECS[op])
        keys: set[tuple[str, str, str]] = set()
        for rid in (*ids, CROPWISE):
            keys.update(by_source.get((op, rid), {}))
        op_rows: list[dict[str, Any]] = []
        for firm, crop, metric in keys:
            values = {}
            for rid in ids:
                f = by_source.get((op, rid), {}).get((firm, crop, metric))
                if f is not None:
                    values[rid] = {"value": f.value, "where": f.where, "derived": f.derived}
            cwf = by_source.get((op, CROPWISE), {}).get((firm, crop, metric))
            cw_val = {"value": cwf.value, "where": cwf.where, "derived": cwf.derived} if cwf else None
            if cw_val is None and not values:
                continue
            spread, cw_diff, st = _status(
                {k: v["value"] for k, v in values.items()}, cw_val["value"] if cw_val else None,
                tolerance_for_metric(metric, tolerance, tolerance_season),
            )
            op_rows.append({
                "operation": op, "firm": firm, "crop": crop, "metric": metric,
                "values": values, "cropwise": cw_val,
                "spread": spread, "cropwise_diff": cw_diff, "status": st,
            })
        op_rows.sort(key=lambda r: (_firm_sort_key(r["firm"]), _crop_sort_key(r["crop"]), r["metric"] != DAY))
        rows.extend(op_rows)
        counts: dict[str, int] = defaultdict(int)
        for r in op_rows:
            counts[r["status"]] += 1
        cw = cw_by_op.get(op) or {}
        by_operation[op] = {
            "report_ids": ids,
            "season_start": _season_start_iso(report_date, op),
            "warnings": op_warnings[op],
            "cropwise": {
                "error": cw.get("error"),
                "years": cw.get("years"),
                "warnings": cw.get("warnings") or [],
                "status": cw.get("status"),
            },
            "summary": dict(counts),
        }

    summary: dict[str, int] = defaultdict(int)
    for r in rows:
        summary[r["status"]] += 1
    return {
        "operations": operations,
        "peers": {op: list(SPECS[op]) for op in operations},
        "report_date": report_date.isoformat(),
        "shift_date": shift_date(report_date).isoformat(),
        "tolerance": tolerance,  # суточный допуск (имя сохранено для совместимости)
        "tolerance_day": tolerance,
        "tolerance_season": tolerance_season,
        "report_ids": list(REPORT_IDS),
        "sources": sources,
        "cropwise": cropwise,
        "by_operation": by_operation,
        "rows": rows,
        "summary": dict(summary),
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
            print(r["operation"], "|", r["firm"], "|", r["crop"], "|", r["metric"], vals, "cw:", (r["cropwise"] or {}).get("value"), r["status"])
