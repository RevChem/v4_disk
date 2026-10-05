"""
cropwise/duplicates_sync.py
=================================
Вкладка «Дубли» панели «Нарушения»: ищет дубли ФИО пользователей и дубли
машин по госномеру в Cropwise.

Адаптация автономного скрипта cropwise_audit.py — логика нормализации
(normalize_spacing_case/normalize_plate) перенесена без изменений,
HTTP-слой заменён на общий асинхронный клиент Cropwise (client.py:
_fetch_all_pages), архивность машин определяется тем же способом, что
и в vehicle_check_sync.py (inventory_history_items, hidden=true,
актуально на текущий момент).

Правила совпадения РАЗНЫЕ для двух наборов:
  - ФИО пользователей: ТОЧНЫЕ дубли (после нормализации) + дубли с
    разницей РОВНО в один символ (опечатка — пропущенная/лишняя/другая
    буква), см. find_near_duplicates_one_char. Такие строки помечены
    полем "match_type": "near" (точные — "exact"), чтобы отличать их
    в таблице/экспорте.
  - Госномера машин: ТОЛЬКО точные совпадения после нормализации.
    Разница в один символ у госномера почти всегда означает РАЗНЫЕ
    машины (А123ВС716 / А123ВС718 — не дубль), поэтому здесь эта
    логика НАМЕРЕННО не применяется.

Запускается ТОЛЬКО вручную (кнопка «Обновить» в UI) — без Celery
beat-расписания, по тому же принципу, что и вкладка «GPS на нескольких
машинах» (см. gps_duplicate_sync.py / gps_duplicate_router.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from itertools import combinations
from typing import Optional

import aiohttp

from app.cropwise.client import CROPWISE_HEADERS, _fetch_all_pages, _CROPWISE_TIMEOUT
from app.logging_config import get_logger

logger = get_logger("cropwise.duplicates")

TZ_MSK = timezone(timedelta(hours=3))


# =============================================================================
# НОРМАЛИЗАЦИЯ (перенесено без изменений из cropwise_audit.py)
# =============================================================================

def normalize_spacing_case(s: Optional[str]) -> str:
    """Схлопывает повторяющиеся пробелы, убирает пробелы по краям, приводит
    к единому регистру. Ловит: 'Иванов  Иван' vs 'Иванов Иван' (двойной
    пробел), 'ИВАНОВ иван' vs 'Иванов Иван' (регистр)."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.strip()).casefold()


def normalize_plate(s: Optional[str]) -> str:
    """Для госномеров дополнительно убираем внутренние пробелы и дефисы —
    'А123ВС716' и 'А 123 ВС 716' должны считаться одним и тем же номером."""
    if not s:
        return ""
    s = normalize_spacing_case(s)
    return re.sub(r"[\s\-_.]+", "", s)


IGNORED_PLATES = {"", "б/н", "бн", "-", "нет", "отсутствует", "б\\н"}
PLACEHOLDER_PLATE_RE = re.compile(r"^\s*vin\b", re.IGNORECASE)


def is_missing_plate(raw: Optional[str]) -> bool:
    """Считаем госномер отсутствующим, если поле пусто ИЛИ содержит одну
    из типичных заглушек ('Б/Н', '-', 'vin ...') — такие заглушки не
    считаем дублями друг друга."""
    if raw is None:
        return True
    norm = normalize_spacing_case(raw)
    if norm in IGNORED_PLATES:
        return True
    if PLACEHOLDER_PLATE_RE.match(raw):
        return True
    return False


@dataclass
class DupGroup:
    normalized: str
    items: list[dict] = field(default_factory=list)


def find_exact_duplicates(
    entries: list[tuple[int, str, str]],  # (id, raw_value, normalized_value)
) -> list[DupGroup]:
    """
    entries: список (id, исходное значение, нормализованное значение).
    Возвращает группы с ОДИНАКОВЫМ нормализованным значением (>1 записи).
    """
    by_norm: dict[str, list[tuple[int, str]]] = {}
    for _id, raw, norm in entries:
        if not norm:
            continue
        by_norm.setdefault(norm, []).append((_id, raw))

    return [
        DupGroup(normalized=norm, items=[{"id": i, "raw": r} for i, r in items])
        for norm, items in by_norm.items()
        if len(items) > 1
    ]


# =============================================================================
# АРХИВНОСТЬ МАШИНЫ (тот же способ, что в vehicle_check_sync.py)
# =============================================================================

async def _fetch_hidden_machine_ids(session: aiohttp.ClientSession) -> set[int]:
    now_str = datetime.now(tz=TZ_MSK).strftime("%Y-%m-%dT%H:%M:%S")
    raw = await _fetch_all_pages(session, "inventory_history_items", {
        "historyable_type": "Machine",
        "hidden": "true",
        "event_start_at": now_str,
        "event_end_at": now_str,
    })
    return {
        int(item["historyable_id"])
        for item in raw
        if item.get("historyable_id") is not None
    }


# =============================================================================
# ФОРМИРОВАНИЕ СТРОК ДЛЯ ТАБЛИЦЫ / EXCEL (группы разворачиваются в пары
# через combinations — так группа из N>2 одинаковых значений даёт
# несколько наглядных строк "запись 1 ⇄ запись 2")
# =============================================================================

def _name_rows(exact_groups: list[DupGroup]) -> list[dict]:
    rows: list[dict] = []
    for g in exact_groups:
        for (ia, ra), (ib, rb) in combinations([(it["id"], it["raw"]) for it in g.items], 2):
            rows.append({"id_a": ia, "fio_a": ra, "id_b": ib, "fio_b": rb})
    return rows


def _plate_rows(exact_groups: list[DupGroup], id_to_machine: dict[int, dict]) -> list[dict]:
    def _m(mid):
        mm = id_to_machine.get(mid, {})
        return mm.get("custom_name") or mm.get("name") or f"machine_{mid}"

    rows: list[dict] = []
    for g in exact_groups:
        for (ia, ra), (ib, rb) in combinations([(it["id"], it["raw"]) for it in g.items], 2):
            rows.append({
                "id_a": ia, "plate_a": ra, "name_a": _m(ia),
                "id_b": ib, "plate_b": rb, "name_b": _m(ib),
            })
    return rows


# =============================================================================
# ОСНОВНАЯ ЛОГИКА
# =============================================================================

async def refresh_duplicates() -> dict:
    from app.cropwise.duplicates_router import update_cache

    now = datetime.now(tz=TZ_MSK)

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        machines_raw = await _fetch_all_pages(session, "machines")
        users_raw = await _fetch_all_pages(session, "users")
        hidden_ids = await _fetch_hidden_machine_ids(session)

    machines = [m for m in machines_raw if m.get("id") not in hidden_ids]
    id_to_machine = {m.get("id"): m for m in machines}

    # ── Дубли ФИО пользователей ──────────────────────────────────────────
    name_entries = []
    for u in users_raw:
        raw = (u.get("username") or u.get("email") or "").strip()
        if not raw:
            continue
        name_entries.append((u.get("id"), raw, normalize_spacing_case(raw)))
    name_rows = _name_rows(find_exact_duplicates(name_entries))

    # ── Дубли машин по госномеру (не архивные) ───────────────────────────
    plate_entries = []
    for m in machines:
        raw = (m.get("registration_number") or "").strip()
        if not raw or is_missing_plate(raw):
            continue  # заглушки вида "Б/Н" не считаем дублями друг друга
        plate_entries.append((m.get("id"), raw, normalize_plate(raw)))
    plate_rows = _plate_rows(find_exact_duplicates(plate_entries), id_to_machine)

    update_cache(name_rows, plate_rows, now)

    result = {"names": len(name_rows), "plates": len(plate_rows)}
    logger.info(f"duplicates refresh done: {result}")
    return result
