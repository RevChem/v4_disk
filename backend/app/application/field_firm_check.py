"""Проверка: пометка фирмы в названии поля Namolot («(АФ3)») против папки Cropwise.

Сводка 10 и кнопка Namolot определяют фирму по ТЕКУЩЕЙ папке поля в Cropwise.
Если поле передали другой агрофирме посреди сезона, тонны до передачи уходят
новому владельцу. Пометка в названии поля стоит в самой записи Намолота, поэтому
расхождение «пометка ≠ папка» ловит такие случаи.

Запускается Celery раз в FIELD_FIRM_CHECK_INTERVAL_SEC (по умолчанию 3 часа).
Результат — JSON в upload_dir; фронтенд опрашивает его и показывает окно.
Поле, для которого передача уже записана в plans.FIELD_FIRM_TRANSFERS,
или поле из списка «не ошибка», расхождением не считается.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.config import settings
from app.infrastructure.logging import get_logger
from app.legacy.json_store_io import atomic_write_json, read_json_dict

log = get_logger(__name__)

TZ_MSK = timezone(timedelta(hours=3))
_TAG_RE = re.compile(r"\(([^)]*)\)")
_PREFIX_RE = re.compile(r"^аф\s*(\d+)\s*[-\u2010-\u2015\u2212]")
_AF_RE = re.compile(r"(?:^|[^0-9а-яa-z])аф\s*[- ]?\s*(\d+)")
_LATIN = str.maketrans({"A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К",
                        "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У"})
MAX_ITEMS = 50


def _store_dir() -> Path:
    path = Path(settings.upload_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path() -> Path:
    return _store_dir() / ".field_firm_check.json"


def ignore_path() -> Path:
    return _store_dir() / ".field_firm_ignore.json"


def item_key(code: str, tag_firm: str, cw_firm: str) -> str:
    return f"{code}|{tag_firm}|{cw_firm}"


def _norm(text: str) -> str:
    return text.strip().upper().translate(_LATIN).casefold().replace("ё", "е")


def firm_from_tags(spp: object, number_to_firm: dict[int, str]) -> str | None:
    """Фирма по пометке в названии поля. Пометка бывает в двух местах:

    * в скобках после кода: «АИ-01-09(АФ4) Пшеница…»;
    * в самом коде как префикс: «АФ5-02-27 Пшеница…», «АФ2-01-02 …».

    Без пометки (код вроде «РВ-01-61» без скобок) возвращается None. Если места
    указывают на разные фирмы или номер неизвестен, тоже None: сравнивать не с чем.
    """
    text = str(spp or "").strip().lstrip("*").strip()
    numbers: set[int] = set()
    for tag in _TAG_RE.findall(text):
        match = _AF_RE.search(_norm(tag))
        if match:
            numbers.add(int(match.group(1)))
    head = _norm(re.split(r"[\s(]", text, maxsplit=1)[0]) if text else ""
    prefix = _PREFIX_RE.match(head)
    if prefix:
        numbers.add(int(prefix.group(1)))
    firms = {number_to_firm[n] for n in numbers if n in number_to_firm}
    if len(numbers) != 1 or len(firms) != 1:
        return None
    return next(iter(firms))


def find_mismatches(
    records: list[dict],
    code_to_firm: dict[str, str],
    *,
    field_code: Callable[[object], str],
    record_time: Callable[[object], datetime | None],
    number_to_firm: dict[int, str],
    transfer_firm: Callable[[str, datetime | None], str | None] | None = None,
    ignored: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Поля, у которых пометка Namolot указывает на одну фирму, а привязка — на другую.

    transfer_firm(code, время) — фирма из таблицы передач (None, если поля там нет).
    Если таблица знает поле, фирмой записи считается она, а не папка Cropwise.
    """
    ignored = ignored or set()
    buckets: dict[str, dict[str, Any]] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        code = field_code(rec.get("spp"))
        if not code:
            continue
        tag_firm = firm_from_tags(rec.get("spp"), number_to_firm)
        if not tag_firm:
            continue
        when = record_time(rec.get("date"))
        firm = (transfer_firm(code, when) if transfer_firm else None) or code_to_firm.get(code)
        if not firm or firm == tag_firm:
            continue
        key = item_key(code, tag_firm, firm)
        if key in ignored:
            continue
        try:
            tonnes = float(rec.get("quantity") or 0) / 1000.0
        except (TypeError, ValueError):
            tonnes = 0.0
        b = buckets.setdefault(key, {
            "key": key, "code": code, "tag_firm": tag_firm, "cropwise_firm": firm,
            "tonnes": 0.0, "records": 0, "first_at": None, "last_at": None,
            "crops": set(), "example": str(rec.get("spp") or "").strip(),
        })
        b["tonnes"] += tonnes
        b["records"] += 1
        b["crops"].add(re.sub(r"\s*\d{2,4}\s*г?\.?\s*$", "", str(rec.get("crop") or "")).strip())
        if when is not None:
            iso = when.strftime("%Y-%m-%dT%H:%M:%S")
            b["first_at"] = iso if b["first_at"] is None else min(b["first_at"], iso)
            b["last_at"] = iso if b["last_at"] is None else max(b["last_at"], iso)
    out = []
    for b in buckets.values():
        b["tonnes"] = round(b["tonnes"], 3)
        b["crops"] = sorted(c for c in b["crops"] if c)
        out.append(b)
    out.sort(key=lambda x: -x["tonnes"])
    return out


# ------------------------------------------------------------------ хранилище

def load_ignored() -> dict[str, dict]:
    try:
        data = read_json_dict(ignore_path(), label="field_firm_ignore")
    except OSError:
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def add_ignored(key: str, user_email: str, note: str = "") -> dict[str, dict]:
    data = load_ignored()
    data[key] = {"by": user_email, "at": datetime.now(TZ_MSK).isoformat(timespec="seconds"),
                 "note": note[:300]}
    atomic_write_json(ignore_path(), data, ensure_ascii=False, indent=2)
    return data


def read_state() -> dict[str, Any]:
    """Последний результат проверки; ручные «не ошибка» применяются сразу."""
    try:
        state = read_json_dict(state_path(), label="field_firm_check")
    except OSError:
        state = {}
    ignored = set(load_ignored())
    items = [i for i in state.get("items", []) if i.get("key") not in ignored]
    return {
        "run_id": state.get("run_id"),
        "checked_at": state.get("checked_at"),
        "status": state.get("status", "never"),
        "error": state.get("error"),
        "items": items,
    }


def _save_state(state: dict[str, Any]) -> None:
    atomic_write_json(state_path(), state, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------ запуск

def _transfer_helpers():
    """(fn фирма-по-таблице-передач) из svodki/plan/plans.py; без него — без таблицы."""
    try:
        from plans import FIELD_FIRM_TRANSFERS, firm_override_for_field
    except Exception:  # pragma: no cover - plans нет в PYTHONPATH
        return None

    def fn(code: str, when):
        if code not in FIELD_FIRM_TRANSFERS:
            return None
        return firm_override_for_field(code, when)

    return fn


def run_check(today: date | None = None) -> dict[str, Any]:
    """Один прогон. Ошибка источника не стирает прошлый результат и не создаёт новое окно."""
    from app.application import namolot_view as nv

    now = datetime.now(TZ_MSK)
    today = today or now.date()
    previous = read_state()
    try:
        loaded = nv._load_field_codes()          # свежий справочник, а не кэш на 6 часов
        if loaded:
            nv._write_field_codes(loaded)
        index = loaded or nv.field_code_index()
        if not index:
            raise RuntimeError("справочник полей Cropwise пуст или недоступен")
        start = nv.harvest_season_start(today)
        records = nv.fetch_live(start, today, max_days=nv.BRIEF_MAX_DAYS)
        items = find_mismatches(
            records, index,
            field_code=nv.field_base_code,
            record_time=nv.msk_wall,
            number_to_firm=nv._NUMBER_TO_FIRM,
            transfer_firm=_transfer_helpers(),
            ignored=set(load_ignored()),
        )[:MAX_ITEMS]
    except Exception as exc:
        log.exception("field_firm_check.failed", error=str(exc))
        state = {"run_id": previous.get("run_id"), "checked_at": previous.get("checked_at"),
                 "status": "error", "error": f"{exc.__class__.__name__}: {exc}",
                 "items": previous.get("items", [])}
        _save_state(state)
        return state
    state = {"run_id": now.isoformat(timespec="seconds"), "checked_at": now.isoformat(timespec="seconds"),
             "status": "ok", "error": None, "items": items, "records": len(records)}
    _save_state(state)
    log.info("field_firm_check.done", items=len(items), records=len(records))
    return state
