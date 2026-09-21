import json
import importlib.util
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from app.config import settings

UI_ANNOTATIONS_KEY = "__ui_annotations__"
SYNC_EXCLUSIONS_KEY = "__sync_exclusions__"
DATE_OVERRIDES_KEY = "__by_date__"


_MANUAL_OVERRIDES_DEFAULTS_FALLBACK: dict[str, dict[str, Any]] = {
    "1": {
        "plan_output_ha_per_day": 250.0,
        "plan_output_by_firm": {
            "АФ Чистопольская": 40263.0,
            "АгроРаздолье": 50764.0,
            "Аксу-Агро": 42366.0,
            "НурлатАгро": 57321.0,
            "Дрожжаное Рассвет Агро": 30134.0,
            "Актай": 2691.0,
            "Лениногорская Птицефабрика": 2741.0,
        },
        "forecast_alert_days": 45,
        # Если задана дата — для красной подсветки используем её как порог.
        # Формат: YYYY-MM-DD. Пусто = использовать forecast_alert_days.
        "forecast_alert_date": "",
        "fertilizer_own_count_by_firm": {
            "АФ Чистопольская": 10,
            "АгроРаздолье": 11,
            "Аксу-Агро": 12,
            "НурлатАгро": 7,
            "Дрожжаное Рассвет Агро": 10,
            "Актай": 1,
            "Лениногорская Птицефабрика": 2,
        },
        "spraying_own_count_by_firm": {
            "АФ Чистопольская": 9,
            "АгроРаздолье": 9,
            "Аксу-Агро": 8,
            "НурлатАгро": 4,
            "Дрожжаное Рассвет Агро": 8,
            "Актай": 1,
            "Лениногорская Птицефабрика": 2,
        },
        "boron_own_count_by_firm": {
            "АФ Чистопольская": 10,
            "АгроРаздолье": 15,
            "Аксу-Агро": 10,
            "НурлатАгро": 6,
            "Дрожжаное Рассвет Агро": 8,
            "Актай": 5,
            "Лениногорская Птицефабрика": 0,
        },
        "cultivation_own_count_by_firm": {
            "АФ Чистопольская": 6,
            "АгроРаздолье": 8,
            "Аксу-Агро": 4,
            "НурлатАгро": 5,
            "Дрожжаное Рассвет Агро": 4,
            "Актай": 0,
            "Лениногорская Птицефабрика": 0,
        },
        "seeding_own_count_by_firm": {
            "АФ Чистопольская": 5,
            "АгроРаздолье": 7,
            "Аксу-Агро": 7,
            "НурлатАгро": 10,
            "Дрожжаное Рассвет Агро": 5,
            "Актай": 0,
            "Лениногорская Птицефабрика": 1,
        },
        "veg_seeding_own_count_by_firm": {
            "АФ Чистопольская": 6,
            "АгроРаздолье": 0,
            "Аксу-Агро": 0,
            "НурлатАгро": 0,
            "Дрожжаное Рассвет Агро": 0,
            "Актай": 0,
            "Лениногорская Птицефабрика": 0,
        },
        "veg_spraying_own_count_by_firm": {
            "АФ Чистопольская": 2,
            "АгроРаздолье": 0,
            "Аксу-Агро": 0,
            "НурлатАгро": 0,
            "Дрожжаное Рассвет Агро": 0,
            "Актай": 0,
            "Лениногорская Птицефабрика": 0,
        },
        "frez_own_count_by_firm": {
            "АФ Чистопольская": 4,
            "АгроРаздолье": 0,
            "Аксу-Агро": 0,
            "НурлатАгро": 0,
            "Дрожжаное Рассвет Агро": 0,
            "Актай": 0,
            "Лениногорская Птицефабрика": 0,
        },
    },
    "2": {
        "plan_rate_by_group": {
            "1.1 Внесение минеральных удобрений": 250.0,
            "2.1 Боронование": 250.0,
            "2.9 Дискование": 250.0,
            "2.4 Вспашка": 250.0,
            "2.11 Культивация": 250.0,
            "4.0 Посев": 250.0,
            "4.1 Посев (овощи)": 250.0,
            "3.1 Опрыскивание": 250.0,
            "2.12 Прикатывание": 250.0,
            "2.7 Гребнеобразование": 250.0,
            "2.8 Глубокорыхление": 250.0,
            "5.0 Уборка": 250.0,
            "5.4 Уборка картофеля, лука": 250.0,
        },
        "available_own_by_group_firm": {},
    },
    "3": {
        "plan_rate_by_firm": {
            "АФ Чистопольская": 250.0,
            "АгроРаздолье": 250.0,
            "Аксу-Агро": 250.0,
            "НурлатАгро": 250.0,
            "Дрожжаное Рассвет Агро": 250.0,
            "Актай": 250.0,
            "Лениногорская Птицефабрика": 250.0,
        },
        "available_self_propelled_by_firm": {
            "АФ Чистопольская": 10,
            "АгроРаздолье": 11,
            "Аксу-Агро": 12,
            "НурлатАгро": 7,
            "Дрожжаное Рассвет Агро": 10,
            "Актай": 1,
            "Лениногорская Птицефабрика": 2,
        },
    },
    "4": {
        "plan_rate_by_firm": {
            "АФ Чистопольская": 200.0,
            "АгроРаздолье": 200.0,
            "Аксу-Агро": 200.0,
            "НурлатАгро": 200.0,
            "Дрожжаное Рассвет Агро": 200.0,
            "Актай": 200.0,
            "Лениногорская Птицефабрика": 200.0,
        },
        "own_count_by_firm": {
            "АФ Чистопольская": 10,
            "АгроРаздолье": 15,
            "Аксу-Агро": 10,
            "НурлатАгро": 6,
            "Дрожжаное Рассвет Агро": 8,
            "Актай": 5,
            "Лениногорская Птицефабрика": 0,
        },
    },
    "5": {
        "seeding_plan_rate_by_firm": {
            "АФ Чистопольская": 200.0,
            "АгроРаздолье": 200.0,
            "Аксу-Агро": 200.0,
            "НурлатАгро": 200.0,
            "Дрожжаное Рассвет Агро": 200.0,
            "Актай": 200.0,
            "Лениногорская Птицефабрика": 200.0,
        },
        "seeding_own_count_by_firm": {
            "АФ Чистопольская": 5,
            "АгроРаздолье": 7,
            "Аксу-Агро": 7,
            "НурлатАгро": 10,
            "Дрожжаное Рассвет Агро": 5,
            "Актай": 0,
            "Лениногорская Птицефабрика": 1,
        },
        "veg_plan_rate_by_firm": {
            "АФ Чистопольская": 100.0,
            "АгроРаздолье": 100.0,
            "Аксу-Агро": 100.0,
            "НурлатАгро": 100.0,
            "Дрожжаное Рассвет Агро": 100.0,
            "Актай": 100.0,
            "Лениногорская Птицефабрика": 100.0,
        },
        "veg_own_count_by_firm": {
            "АФ Чистопольская": 4,
            "АгроРаздолье": 2,
            "Аксу-Агро": 2,
            "НурлатАгро": 2,
            "Дрожжаное Рассвет Агро": 2,
            "Актай": 0,
            "Лениногорская Птицефабрика": 1,
        },
    },
    "6": {
        "plan_rate_by_firm": {
            "АФ Чистопольская": 200.0,
            "АгроРаздолье": 200.0,
            "Аксу-Агро": 200.0,
            "НурлатАгро": 200.0,
            "Дрожжаное Рассвет Агро": 200.0,
            "Актай": 200.0,
            "Лениногорская Птицефабрика": 200.0,
        },
        "own_count_by_firm": {
            "АФ Чистопольская": 9,
            "АгроРаздолье": 9,
            "Аксу-Агро": 8,
            "НурлатАгро": 4,
            "Дрожжаное Рассвет Агро": 8,
            "Актай": 1,
            "Лениногорская Птицефабрика": 2,
        },
        "veg_spray_plan_rate_by_firm": {
            "АФ Чистопольская": 200.0,
        },
        "veg_spray_own_count_by_firm": {
            "АФ Чистопольская": 2,
        },
    },
    "7": {
        "plan_rate_by_firm": {
            "АФ Чистопольская": 200.0,
            "АгроРаздолье": 200.0,
            "Аксу-Агро": 200.0,
            "НурлатАгро": 200.0,
            "Дрожжаное Рассвет Агро": 200.0,
            "Актай": 200.0,
            "Лениногорская Птицефабрика": 200.0,
        },
        "own_count_by_firm": {
            "АФ Чистопольская": 4,
            "АгроРаздолье": 8,
            "Аксу-Агро": 4,
            "НурлатАгро": 6,
            "Дрожжаное Рассвет Агро": 3,
            "Актай": 0,
            "Лениногорская Птицефабрика": 1,
        },
    },
    "8": {
        "plan_rate_by_firm": {
            "АФ Чистопольская": 250.0,
            "АгроРаздолье": 250.0,
            "Аксу-Агро": 250.0,
            "НурлатАгро": 250.0,
            "Дрожжаное Рассвет Агро": 250.0,
            "Актай": 250.0,
            "Лениногорская Птицефабрика": 250.0,
        },
        "own_count_by_firm": {
            "АФ Чистопольская": 6,
            "АгроРаздолье": 8,
            "Аксу-Агро": 4,
            "НурлатАгро": 5,
            "Дрожжаное Рассвет Агро": 4,
            "Актай": 0,
            "Лениногорская Птицефабрика": 0,
        },
        "planned_output": 250.0,
        "aggregates_by_firm": {
            "АФ Чистопольская": 6,
            "АгроРаздолье": 8,
            "Аксу-Агро": 4,
            "НурлатАгро": 5,
            "Дрожжаное Рассвет Агро": 4,
            "Актай": 0,
            "Лениногорская Птицефабрика": 0,
        },
    },
    "9": {
        "plan_rate_by_firm": {
            "АФ Чистопольская": 40.0,
            "АгроРаздолье": 40.0,
            "Аксу-Агро": 40.0,
            "НурлатАгро": 40.0,
            "Дрожжаное Рассвет Агро": 40.0,
            "Актай": 40.0,
            "Лениногорская Птицефабрика": 40.0,
        },
        "own_count_by_firm": {
            "АФ Чистопольская": 3,
            "АгроРаздолье": 13,
            "Аксу-Агро": 9,
            "НурлатАгро": 14,
            "Дрожжаное Рассвет Агро": 2,
            "Актай": 0,
            "Лениногорская Птицефабрика": 0,
        },
    },
    "23": {
        # Весь текстовый блок «по работам» в аналитике (docx): непустое значение заменяет автогенерацию.
        "analytics_document_body": "",
    },
    "26": {
        # Начало периода сводки комбайнов (YYYY-MM-DD). Пусто — 10 июня сезона уборки.
        "combine_season_start_date": "",
    },
    "27": {
        # Начало периода рейтинга механизаторов (YYYY-MM-DD). Пусто — 1-е число месяца DATE_TO.
        "rating_period_from_date": "",
    },
}

_MANUAL_OVERRIDES_DEFAULTS_FALLBACK["1.18"] = deepcopy(_MANUAL_OVERRIDES_DEFAULTS_FALLBACK["1"])


def _safe_float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(raw: Any, default: int = 0) -> int:
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return int(default)


def _load_plans_defaults() -> dict[str, dict[str, Any]] | None:
    try:
        from .plans_editor import load_effective_plans_module

        module = load_effective_plans_module()
    except Exception:
        root = Path(settings.svodki_root).expanduser()
        if root.is_absolute():
            svodki_root = root
        else:
            svodki_root = (Path.cwd() / root).resolve()
        plans_path = svodki_root / "plan" / "plans.py"
        if not plans_path.is_file():
            return None
        try:
            spec = importlib.util.spec_from_file_location("svodki_plan_plans_defaults", plans_path)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            return None

    firm_to_folder = getattr(module, "FIRM_TO_FOLDER", {}) or {}
    firms = [str(x) for x in firm_to_folder.keys()] if isinstance(firm_to_folder, dict) else []
    if not firms:
        firms = list(_MANUAL_OVERRIDES_DEFAULTS_FALLBACK.get("1", {}).get("plan_output_by_firm", {}).keys())

    available_own = getattr(module, "AVAILABLE_OWN", {}) or {}
    plan_rate_per_group = getattr(module, "PLAN_RATE_PER_GROUP", {}) or {}
    hardcoded_flat = getattr(module, "HARDCODED_PLAN_FLAT", {}) or {}

    plan_output_by_firm: dict[str, float] = {firm: 0.0 for firm in firms}
    if isinstance(hardcoded_flat, dict):
        for key, val in hardcoded_flat.items():
            if not isinstance(key, tuple) or len(key) < 2:
                continue
            group_name = str(key[0])
            firm_name = str(key[1])
            if group_name != "1.1 Внесение минеральных удобрений":
                continue
            if firm_name not in plan_output_by_firm:
                continue
            plan_output_by_firm[firm_name] += _safe_float(val, 0.0)
    elif isinstance(getattr(module, "HARDCODED_PLAN", None), dict):
        fert = module.HARDCODED_PLAN.get("1.1 Внесение минеральных удобрений", {})
        if isinstance(fert, dict):
            for firm_name, crops in fert.items():
                if firm_name not in plan_output_by_firm or not isinstance(crops, dict):
                    continue
                plan_output_by_firm[firm_name] = sum(_safe_float(v, 0.0) for v in crops.values())
    plan_output_by_firm = {k: round(v, 1) for k, v in plan_output_by_firm.items()}

    def _group_own(name: str) -> dict[str, int]:
        src = available_own.get(name, {}) if isinstance(available_own, dict) else {}
        out = {firm: _safe_int(src.get(firm, 0), 0) for firm in firms}
        return out

    def _firm_rate_from_group(name: str) -> dict[str, float]:
        rate = _safe_float((plan_rate_per_group or {}).get(name, 0.0), 0.0)
        return {firm: rate for firm in firms}

    defaults: dict[str, dict[str, Any]] = {
        "1": {
            "plan_output_ha_per_day": _safe_float((plan_rate_per_group or {}).get("1.1 Внесение минеральных удобрений", 250.0), 250.0),
            "plan_output_by_firm": plan_output_by_firm,
            "forecast_alert_days": 45,
            "forecast_alert_date": "",
            "fertilizer_own_count_by_firm": _group_own("1.1 Внесение минеральных удобрений"),
            "spraying_own_count_by_firm": _group_own("3.1 Опрыскивание"),
            "boron_own_count_by_firm": _group_own("2.1 Боронование"),
            "cultivation_own_count_by_firm": _group_own("2.11 Культивация"),
            "seeding_own_count_by_firm": _group_own("4.0 Посев"),
            "veg_seeding_own_count_by_firm": _group_own("4.1 Посев (овощи)"),
            "veg_spraying_own_count_by_firm": _group_own("3.2 Опрыскивание (овощи)"),
            "frez_own_count_by_firm": _group_own("2.6 Фрезерование"),
            # Совместимость для plan-скрипта отчёта 1.
            "own_count_by_firm": _group_own("1.1 Внесение минеральных удобрений"),
        },
        "2": {
            "plan_rate_by_group": {str(k): _safe_float(v, 0.0) for k, v in (plan_rate_per_group or {}).items()},
            "available_own_by_group_firm": {
                f"{group_name} | {firm_name}": _safe_int(cnt, 0)
                for group_name, firm_map in (available_own.items() if isinstance(available_own, dict) else [])
                if isinstance(firm_map, dict)
                for firm_name, cnt in firm_map.items()
            },
        },
        "3": {
            "plan_rate_by_firm": _firm_rate_from_group("1.1 Внесение минеральных удобрений"),
            "available_self_propelled_by_firm": _group_own("1.1 Внесение минеральных удобрений"),
        },
        "4": {
            "plan_rate_by_firm": _firm_rate_from_group("2.1 Боронование"),
            "own_count_by_firm": _group_own("2.1 Боронование"),
        },
        "5": {
            "seeding_plan_rate_by_firm": _firm_rate_from_group("4.0 Посев"),
            "seeding_own_count_by_firm": _group_own("4.0 Посев"),
            "veg_plan_rate_by_firm": _firm_rate_from_group("4.1 Посев (овощи)"),
            "veg_own_count_by_firm": _group_own("4.1 Посев (овощи)"),
        },
        "6": {
            "plan_rate_by_firm": _firm_rate_from_group("3.1 Опрыскивание"),
            "own_count_by_firm": _group_own("3.1 Опрыскивание"),
            "veg_spray_plan_rate_by_firm": _firm_rate_from_group("3.2 Опрыскивание (овощи)"),
            "veg_spray_own_count_by_firm": _group_own("3.2 Опрыскивание (овощи)"),
        },
        "7": {
            "plan_rate_by_firm": _firm_rate_from_group("2.9 Дискование"),
            "own_count_by_firm": _group_own("2.9 Дискование"),
        },
        "8": {
            "plan_rate_by_firm": _firm_rate_from_group("2.11 Культивация"),
            "own_count_by_firm": _group_own("2.11 Культивация"),
            "planned_output": _safe_float((plan_rate_per_group or {}).get("2.11 Культивация", 0.0), 0.0),
            "aggregates_by_firm": _group_own("2.11 Культивация"),
        },
        "9": {
            "plan_rate_by_firm": _firm_rate_from_group("2.4 Вспашка"),
            "own_count_by_firm": _group_own("2.4 Вспашка"),
        },
        "11": {
            "plan_rate_by_firm": _firm_rate_from_group("5.0 Уборка"),
            "own_count_by_firm": _group_own("5.0 Уборка"),
        },
    }
    defaults["1.18"] = deepcopy(defaults["1"])
    return defaults


MANUAL_OVERRIDES_DEFAULTS: dict[str, dict[str, Any]] = _load_plans_defaults() or deepcopy(_MANUAL_OVERRIDES_DEFAULTS_FALLBACK)
# Сводка 23 задаётся только в fallback (не строится из plans.py).
MANUAL_OVERRIDES_DEFAULTS.setdefault("23", deepcopy(_MANUAL_OVERRIDES_DEFAULTS_FALLBACK["23"]))
MANUAL_OVERRIDES_DEFAULTS["23"].setdefault("analytics_document_body", "")
MANUAL_OVERRIDES_DEFAULTS.setdefault("26", deepcopy(_MANUAL_OVERRIDES_DEFAULTS_FALLBACK["26"]))
MANUAL_OVERRIDES_DEFAULTS["26"].setdefault("combine_season_start_date", "")
MANUAL_OVERRIDES_DEFAULTS.setdefault("27", deepcopy(_MANUAL_OVERRIDES_DEFAULTS_FALLBACK["27"]))
MANUAL_OVERRIDES_DEFAULTS["27"].setdefault("rating_period_from_date", "")


def get_manual_overrides_defaults() -> dict[str, dict[str, Any]]:
    """Актуальные дефолты: plans + правки из редактора планов на сайте."""
    loaded = _load_plans_defaults()
    if not loaded:
        out = deepcopy(_MANUAL_OVERRIDES_DEFAULTS_FALLBACK)
    else:
        out = deepcopy(loaded)
    out.setdefault("23", deepcopy(_MANUAL_OVERRIDES_DEFAULTS_FALLBACK["23"]))
    out["23"].setdefault("analytics_document_body", "")
    out.setdefault("26", deepcopy(_MANUAL_OVERRIDES_DEFAULTS_FALLBACK["26"]))
    out["26"].setdefault("combine_season_start_date", "")
    out.setdefault("27", deepcopy(_MANUAL_OVERRIDES_DEFAULTS_FALLBACK["27"]))
    out["27"].setdefault("rating_period_from_date", "")
    out.setdefault("1.18", deepcopy(out.get("1", _MANUAL_OVERRIDES_DEFAULTS_FALLBACK["1"])))
    return out


def section_defs_by_report() -> dict[str, list[dict[str, str]]]:
    report1_sections: list[dict[str, str]] = [
        {
            "key": "plan_output_ha_per_day",
            "label": "Плановая выработка га/сут (порог для красной подсветки столбца на 1 агрегат)",
        },
        {
            "key": "plan_output_by_firm",
            "label": "1.1 Внесение: плановая выработка по фирмам (га)",
        },
        {
            "key": "forecast_alert_days",
            "label": "Прогнозная дата: красная, если дальше от даты отчёта (дней)",
        },
        {
            "key": "forecast_alert_date",
            "label": "Прогнозная дата: красная, если позже этой даты (календарь)",
            "field_type": "date",
        },
        {
            "key": "fertilizer_own_count_by_firm",
            "label": "1.1 Внесение: агрегаты (кол-во) по фирмам",
        },
        {
            "key": "spraying_own_count_by_firm",
            "label": "3.1 Опрыскивание: агрегаты (кол-во) по фирмам",
        },
        {
            "key": "boron_own_count_by_firm",
            "label": "2.1 Боронование: агрегаты (кол-во) по фирмам",
        },
        {
            "key": "cultivation_own_count_by_firm",
            "label": "2.11 Культивация: агрегаты (кол-во) по фирмам",
        },
        {
            "key": "seeding_own_count_by_firm",
            "label": "4.0 Посев: агрегаты (кол-во) по фирмам",
        },
        {
            "key": "veg_seeding_own_count_by_firm",
            "label": "4.1 Посев (овощи): агрегаты (кол-во) по фирмам",
        },
        {
            "key": "veg_spraying_own_count_by_firm",
            "label": "3.2 Опрыскивание (овощи): агрегаты (кол-во) по фирмам",
        },
        {
            "key": "frez_own_count_by_firm",
            "label": "2.6 Фрезерование: агрегаты (кол-во) по фирмам",
        },
        {
            "key": "own_count_by_firm",
            "label": "Агрегаты (общее правило): собственные по фирмам",
        },
    ]
    return {
        "1": report1_sections,
        "1.18": deepcopy(report1_sections),
        "2": [
            {
                "key": "plan_rate_by_group",
                "label": "Плановая выработка по видам работ (га/сутки)",
            },
            {
                "key": "available_own_by_group_firm",
                "label": "Расширенная выборка: собственные агрегаты по виду работ и фирме",
            },
        ],
        "3": [
            {"key": "plan_rate_by_firm", "label": "Плановая выработка по фирмам (га/сутки)"},
            {
                "key": "available_self_propelled_by_firm",
                "label": "Доступно к работе самоходных/прицепных (план, шт.)",
            },
        ],
        "4": [
            {"key": "plan_rate_by_firm", "label": "Плановая выработка по фирмам (га/сутки)"},
            {"key": "own_count_by_firm", "label": "Собственные агрегаты (кол-во) по фирмам"},
        ],
        "5": [
            {
                "key": "seeding_plan_rate_by_firm",
                "label": "Посев: плановая выработка по фирмам (га/сутки)",
            },
            {
                "key": "seeding_own_count_by_firm",
                "label": "Посев: собственные агрегаты (кол-во) по фирмам",
            },
            {
                "key": "veg_plan_rate_by_firm",
                "label": "Овощи: плановая выработка по фирмам (га/сутки)",
            },
            {
                "key": "veg_own_count_by_firm",
                "label": "Овощи: собственные агрегаты (кол-во) по фирмам",
            },
        ],
        "6": [
            {"key": "plan_rate_by_firm", "label": "3.1 Опрыскивание: плановая выработка по фирмам (га/сутки)"},
            {"key": "own_count_by_firm", "label": "3.1 Опрыскивание: собственные агрегаты (кол-во) по фирмам"},
            {
                "key": "veg_spray_plan_rate_by_firm",
                "label": "3.2 Опрыскивание (овощи): плановая выработка по фирмам (га/сутки)",
            },
            {
                "key": "veg_spray_own_count_by_firm",
                "label": "3.2 Опрыскивание (овощи): собственные агрегаты (кол-во) по фирмам",
            },
        ],
        "7": [
            {"key": "plan_rate_by_firm", "label": "Плановая выработка по фирмам (га/сутки)"},
            {"key": "own_count_by_firm", "label": "Собственные агрегаты (кол-во) по фирмам"},
        ],
        "8": [
            {"key": "plan_rate_by_firm", "label": "Плановая выработка по фирмам (га/сутки)"},
            {"key": "own_count_by_firm", "label": "Собственные агрегаты (кол-во) по фирмам"},
            {"key": "planned_output", "label": "Плановая выработка (единое значение, совместимость)"},
            {"key": "aggregates_by_firm", "label": "Собственные агрегаты по фирмам (совместимость)"},
        ],
        "9": [
            {"key": "plan_rate_by_firm", "label": "Плановая выработка по фирмам (га/сутки)"},
            {"key": "own_count_by_firm", "label": "Собственные агрегаты (кол-во) по фирмам"},
        ],
        "11": [
            {"key": "plan_rate_by_firm", "label": "Плановая выработка по фирмам (га/сутки)"},
            {"key": "own_count_by_firm", "label": "Собственные комбайны (кол-во) по фирмам"},
        ],
        "23": [
            {
                "key": "analytics_document_body",
                "label": "Текст сводки по работам (полностью вместо автотекста; блок простоев в конце — из системы)",
            },
        ],
        "26": [
            {
                "key": "combine_season_start_date",
                "label": "Дата начала отсчёта сезона уборки (начало периода отчёта)",
                "field_type": "date",
            },
        ],
        "27": [
            {
                "key": "rating_period_from_date",
                "label": "Дата начала периода рейтинга механизаторов",
                "field_type": "date",
            },
        ],
    }


_SYNC_GROUPS: tuple[set[tuple[str, str]], ...] = (
    {
        ("3", "plan_rate_by_firm"),
        ("4", "plan_rate_by_firm"),
        ("6", "plan_rate_by_firm"),
        ("7", "plan_rate_by_firm"),
        ("8", "plan_rate_by_firm"),
        ("9", "plan_rate_by_firm"),
        ("11", "plan_rate_by_firm"),
    },
    {
        ("3", "available_self_propelled_by_firm"),
        ("4", "own_count_by_firm"),
        ("6", "own_count_by_firm"),
        ("7", "own_count_by_firm"),
        ("8", "own_count_by_firm"),
        ("9", "own_count_by_firm"),
        ("11", "own_count_by_firm"),
    },
)


def linked_sections_by_report() -> dict[str, dict[str, list[dict[str, str]]]]:
    defs = section_defs_by_report()
    label_by_key: dict[tuple[str, str], str] = {}
    for rid, rows in defs.items():
        for row in rows:
            key = str(row.get("key") or "")
            if key:
                label_by_key[(rid, key)] = str(row.get("label") or key)

    out: dict[str, dict[str, list[dict[str, str]]]] = {}
    for group in _SYNC_GROUPS:
        for report_id, section_key in group:
            others = sorted(
                [(r, s) for (r, s) in group if not (r == report_id and s == section_key)],
                key=lambda x: (x[0], x[1]),
            )
            if not others:
                continue
            out.setdefault(report_id, {})[section_key] = [
                {
                    "report_id": r,
                    "section_key": s,
                    "label": label_by_key.get((r, s), s),
                }
                for (r, s) in others
            ]
    return out


def resolve_linked_targets(report_id: str, section_key: str) -> list[tuple[str, str]]:
    current = (str(report_id), str(section_key))
    targets: list[tuple[str, str]] = []
    for group in _SYNC_GROUPS:
        if current not in group:
            continue
        targets.extend(sorted([x for x in group if x != current], key=lambda x: (x[0], x[1])))
    return targets


def reset_manual_overrides_to_defaults(keep_annotations: bool = True) -> Path:
    raw = load_manual_store()
    preserved_annotations = raw.get(UI_ANNOTATIONS_KEY) if keep_annotations else None
    preserved_sync_exclusions = raw.get(SYNC_EXCLUSIONS_KEY, {})
    preserved_by_date = raw.get(DATE_OVERRIDES_KEY)
    new_data: dict[str, Any] = {}
    if keep_annotations and isinstance(preserved_annotations, dict):
        new_data[UI_ANNOTATIONS_KEY] = preserved_annotations
    if isinstance(preserved_sync_exclusions, dict):
        new_data[SYNC_EXCLUSIONS_KEY] = preserved_sync_exclusions
    if isinstance(preserved_by_date, dict) and preserved_by_date:
        new_data[DATE_OVERRIDES_KEY] = deepcopy(preserved_by_date)
    return save_manual_store(new_data)


def load_sync_exclusions() -> dict[str, dict[str, bool]]:
    raw = load_manual_store().get(SYNC_EXCLUSIONS_KEY, {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, bool]] = {}
    for report_id, section_map in raw.items():
        if not isinstance(section_map, dict):
            continue
        rid = str(report_id)
        out[rid] = {}
        for section_key, flag in section_map.items():
            out[rid][str(section_key)] = bool(flag)
    return out


def is_sync_excluded(report_id: str, section_key: str, exclusions: dict[str, dict[str, bool]] | None = None) -> bool:
    ex = exclusions if exclusions is not None else load_sync_exclusions()
    return bool(ex.get(str(report_id), {}).get(str(section_key), False))


def set_sync_exclusion(report_id: str, section_key: str, excluded: bool) -> Path:
    raw = load_manual_store()
    ex = raw.get(SYNC_EXCLUSIONS_KEY, {})
    if not isinstance(ex, dict):
        ex = {}
    rid = str(report_id)
    skey = str(section_key)
    if not isinstance(ex.get(rid), dict):
        ex[rid] = {}
    if excluded:
        ex[rid][skey] = True
    else:
        ex[rid].pop(skey, None)
        if not ex[rid]:
            ex.pop(rid, None)
    if ex:
        raw[SYNC_EXCLUSIONS_KEY] = ex
    else:
        raw.pop(SYNC_EXCLUSIONS_KEY, None)
    return save_manual_store(raw)


def _svodki_root() -> Path:
    root = Path(settings.svodki_root).expanduser()
    if root.is_absolute():
        return root
    return (Path.cwd() / root).resolve()


def _resolve_manual_overrides_path() -> Path:
    svodki_root = _svodki_root()
    configured = (settings.svodki_manual_overrides_path or "").strip()
    if configured:
        p = Path(configured).expanduser()
        if not p.is_absolute():
            p = (svodki_root / p).resolve()
        return p

    # In Docker `svodki` is mounted read-only, so runtime edits must live in upload storage.
    root = Path(settings.upload_dir).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    return root / "manual_overrides.json"


def manual_overrides_path() -> Path:
    return _resolve_manual_overrides_path()


def _upload_root() -> Path:
    root = Path(settings.upload_dir).expanduser()
    if not root.is_absolute():
        return (Path.cwd() / root).resolve()
    return root.resolve()


def r23_work_narrative_prefill_path(summary_date: date) -> Path:
    """Файл с последним автотекстом блока «работы» аналитики 23 для даты платформы."""
    return _upload_root() / f"r23_work_narrative_prefill_{summary_date.isoformat()}.txt"


def r23_work_data_from_report2_bundle_path(platform_date: date) -> Path:
    """
    Снимок work_data для сводки 23, записанный при последней генерации сводки 2
    на дату платформы (REPORT_DATE скриптов).
    """
    return _upload_root() / "_r23_donor" / f"report2_{platform_date.isoformat()}.json"


def load_r23_work_narrative_prefill(summary_date: date) -> str:
    path = r23_work_narrative_prefill_path(summary_date)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def r23_analytics_body_override_for_date(summary_date: date) -> str:
    saved = load_manual_store()
    by_date = saved.get(DATE_OVERRIDES_KEY)
    if not isinstance(by_date, dict):
        return ""
    day_bucket = by_date.get(summary_date.isoformat())
    if not isinstance(day_bucket, dict):
        return ""
    block = day_bucket.get("23")
    if not isinstance(block, dict):
        return ""
    return str(block.get("analytics_document_body") or "").strip()


def resolve_r23_editor_body_for_generation(summary_date: date, *, rebuild_only: bool) -> str:
    per_date = r23_analytics_body_override_for_date(summary_date)
    if per_date:
        return per_date
    if not rebuild_only:
        return ""
    merged = merged_manual_store_for_date(summary_date)
    return str((merged.get("23") or {}).get("analytics_document_body") or "").strip()


def load_manual_store() -> dict[str, Any]:
    path = _resolve_manual_overrides_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_manual_store(data: dict[str, Any]) -> Path:
    path = _resolve_manual_overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def merged_manual_store() -> dict[str, Any]:
    saved = load_manual_store()
    merged = deepcopy(get_manual_overrides_defaults())

    for report_id, report_data in saved.items():
        if str(report_id).startswith("__"):
            continue
        if report_id not in merged:
            merged[report_id] = report_data if isinstance(report_data, dict) else {}
            continue
        if isinstance(report_data, dict):
            for key, value in report_data.items():
                merged[report_id][key] = value
    return merged


def merged_manual_store_for_date(summary_date: date) -> dict[str, Any]:
    """Эффективные значения для генерации на календарную дату платформы (глобальные + слой __by_date__)."""
    merged = merged_manual_store()
    saved = load_manual_store()
    by_date = saved.get(DATE_OVERRIDES_KEY)
    if not isinstance(by_date, dict):
        return merged
    overlay_reports = by_date.get(summary_date.isoformat())
    if not isinstance(overlay_reports, dict):
        return merged
    for report_id, report_data in overlay_reports.items():
        if str(report_id).startswith("__"):
            continue
        if not isinstance(report_data, dict):
            continue
        merged.setdefault(report_id, {})
        for key, value in report_data.items():
            merged[report_id][key] = deepcopy(value)
    return merged


def combine_season_start_for_date(report_date: date) -> date:
    """
    Начало периода сводки 26: combine_season_start_date из настроек,
    иначе 10 июня года сезона уборки (до 10.06 — прошлый год).
    """
    merged = merged_manual_store_for_date(report_date)
    raw = str((merged.get("26") or {}).get("combine_season_start_date") or "").strip()
    if not raw:
        raw = (settings.svodki_combine_season_start_date or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw.split("T")[0])
        except ValueError:
            pass
    harvest = date(report_date.year, 6, 10)
    if report_date < harvest:
        return date(report_date.year - 1, 6, 10)
    return harvest


def rating_period_from_for_date(report_date: date) -> date:
    """
    Начало периода сводки 27: rating_period_from_date из настроек,
    иначе 1-е число месяца даты отчёта.
    """
    merged = merged_manual_store_for_date(report_date)
    raw = str((merged.get("27") or {}).get("rating_period_from_date") or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw.split("T")[0])
        except ValueError:
            pass
    return report_date.replace(day=1)


def write_effective_manual_store_for_date(summary_date: date, dest_path: Path) -> None:
    """Записывает JSON в том виде, как его читают svodki-скрипты (без служебных ключей)."""
    data = merged_manual_store_for_date(summary_date)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_manual_section_to_store(
    raw: dict[str, Any],
    report_id: str,
    section_key: str,
    value: Any,
    *,
    summary_date: date | None,
) -> None:
    rid = str(report_id).strip()
    sk = str(section_key).strip()
    if summary_date is None:
        block = raw.setdefault(rid, {})
        if not isinstance(block, dict):
            block = {}
            raw[rid] = block
        block[sk] = value
        return
    iso = summary_date.isoformat()
    by_date = raw.setdefault(DATE_OVERRIDES_KEY, {})
    if not isinstance(by_date, dict):
        by_date = {}
        raw[DATE_OVERRIDES_KEY] = by_date
    day_bucket = by_date.setdefault(iso, {})
    if not isinstance(day_bucket, dict):
        day_bucket = {}
        by_date[iso] = day_bucket
    block = day_bucket.setdefault(rid, {})
    if not isinstance(block, dict):
        block = {}
        day_bucket[rid] = block
    block[sk] = value


def load_annotations_store() -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    saved = load_manual_store()
    raw = saved.get(UI_ANNOTATIONS_KEY, {})
    if not isinstance(raw, dict):
        return {}

    out: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for report_id, section_map in raw.items():
        if not isinstance(section_map, dict):
            continue
        out[report_id] = {}
        for section_key, item_map in section_map.items():
            if not isinstance(item_map, dict):
                continue
            out[report_id][section_key] = {}
            for item_key, note in item_map.items():
                if not isinstance(note, dict):
                    continue
                out[report_id][section_key][item_key] = {
                    "starred": bool(note.get("starred")),
                    "comment": str(note.get("comment") or "").strip(),
                }
    return out


def normalize_annotation(starred: Any, comment: Any) -> dict[str, Any]:
    return {
        "starred": bool(starred),
        "comment": str(comment or "").strip(),
    }


def _coerce_number(raw: Any, sample: Any) -> float | int:
    value = float(raw)
    if isinstance(sample, int) and not isinstance(sample, bool):
        return int(round(value))
    return float(value)


def normalize_section_value(
    report_id: str, section_key: str, value: Any, *, summary_date: date | None = None
) -> Any:
    defaults = get_manual_overrides_defaults().get(report_id, {})
    sample = defaults.get(section_key)

    if isinstance(sample, dict):
        if not isinstance(value, dict):
            raise ValueError("Ожидается объект вида {название_фирмы: значение}.")
        normalized: dict[str, Any] = {}
        merged_source = merged_manual_store_for_date(summary_date) if summary_date else merged_manual_store()
        merged_current = merged_source.get(report_id, {})
        current_map = merged_current.get(section_key, {})
        known_keys: list[str] = []
        if isinstance(current_map, dict):
            known_keys.extend([str(k) for k in current_map.keys()])
        if isinstance(sample, dict):
            for k in sample.keys():
                if str(k) not in known_keys:
                    known_keys.append(str(k))
        keys = list(known_keys) if known_keys else []
        for k in value.keys():
            sk = str(k)
            if sk not in keys:
                keys.append(sk)
        for key in keys:
            raw = value.get(key, 0)
            sample_num = sample.get(key, 0)
            normalized[key] = _coerce_number(raw, sample_num)
        return normalized

    if isinstance(sample, (int, float)):
        return _coerce_number(value, sample)

    if section_key in {"forecast_alert_date", "combine_season_start_date"}:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("Ожидается дата в формате YYYY-MM-DD.") from exc
        return raw

    if isinstance(sample, str):
        return str(value if value is not None else "")

    if isinstance(value, (int, float)):
        return value
    raise ValueError("Ожидается числовое значение.")

