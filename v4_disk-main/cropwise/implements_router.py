"""
cropwise/implements_router.py
==============================
Эндпоинт: «Навесное оборудование» — вкладка модального окна «Доп. отчёты».
POST /api/implements-query — возвращает Excel-файл.

Логика перенесена из автономного скрипта implements.py, адаптирована под
асинхронный клиент проекта (app.cropwise.client) вместо синхронного httpx.

Тело запроса — пустое (без фильтров, как и в исходном скрипте): выгружается
весь справочник оборудования Cropwise целиком, разложенный на 3 листа:
  «Фикс. ширина»  — активное оборудование с фиксированной шириной захвата
  «Перем. ширина» — активное оборудование с переменной шириной захвата
  «Архив»         — оборудование, помеченное как архивное/скрытое

Архивность определяется по последней (по event_start_at) записи
inventory_history_items для каждого implement_id: available=False или
hidden=True → архив.

Регион оборудования — из implement_region_mapping_items: берутся все
маппинги, активные на сегодняшний день (или, если таких нет, маппинг(и)
с самой поздней date_start), названия регионов склеиваются через ", ".
"""
from __future__ import annotations

import asyncio
import io
from datetime import date
from typing import Optional

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth.dependencies import get_current_admin_user
from app.cropwise.client import CROPWISE_HEADERS, _fetch_all_pages, _CROPWISE_TIMEOUT
from app.logging_config import get_logger
from app.users.models import User

logger = get_logger("cropwise.implements_router")

router = APIRouter(prefix="/api/implements-query", tags=["Навесное оборудование"])

_OPS_SEM = asyncio.Semaphore(20)

COLUMNS_BASE = [
    "Регион",
    "Тип оборудования",
    "Имя",
    "Производитель",
    "Модель",
    "Год производства",
    "Регистрационный номер",
    "Инвентарный номер",
    "Серийный номер шасси",
    "Описание",
    "Юридическая компания",
    "Реальная ширина захвата",
    "Официальная ширина захвата",
    "RFID",
    "Дополнительная информация",
    "Внешний идентификатор",
    "ID",
]

# Для листов с переменной шириной захвата (и архива) добавляем мин-макс. ширину
COLUMNS_VARIABLE = COLUMNS_BASE + ["Мин-Макс ширина захвата"]

# Словарь перевода implement_type (английское значение из API -> русское название)
IMPLEMENT_TYPE_TRANSLATIONS = {
    "bags_extractor": "Экстрактор из рукава",
    "bags_packer": "Упаковщик в рукав",
    "baler": "Пресс-подборщик",
    "barrel": "Бочка",
    "bunker": "Бункер",
    "cultivator": "Культиватор",
    "cutter": "Фреза",
    "disc_harrow": "Дисковая борона",
    "disk": "Дискатор",
    "feeder_wagon": "Кормораздатчик",
    "fertilizer_application_equipment": "Оборудование для внесения удобрений",
    "fodder_mixer": "Кормосмеситель",
    "graider": "Грейдер",
    "grain_seeder": "Сеялка зерновая",
    "harrow": "Борона",
    "hindcarriage": "Прицеп",
    "lifter": "Погрузчик",
    "milling_cultivator": "Фрезерный культиватор",
    "mower": "Косилка",
    "other": "Другой",
    "planter": "Сеялка",
    "plow": "Плуг",
    "rakes_rollformers": "Грабли-валкообразователи",
    "reaper": "Жатка",
    "ripper": "Рыхлитель",
    "roller": "Каток",
    "row_crop_drill": "Сеялка пропашная",
    "seed_treater": "Протравитель семян",
    "self_propelled_sprayer": "Самоходный опрыскиватель",
    "semi_trailer": "Полуприцеп",
    "shredders": "Измельчитель",
    "sprayer": "Опрыскиватель",
    "spreader": "Разбрасыватель",
    "subsoiler": "Глубокорыхлитель",
    "topping_for_removing_tops": "Ботвоудалитель",
    "trailed_harvester": "Прицепной комбайн",
    "trailed_potato_harvester": "Картофелеуборочный комбайн (прицепной)",
    "trailed_sprayer": "Прицепной опрыскиватель",
    "universal_planter": "Универсальная сеялка",
}


def _translate_implement_type(value: str) -> str:
    return IMPLEMENT_TYPE_TRANSLATIONS.get(value, value)


class ImplementsRequest(BaseModel):
    """Тело запроса пустое — как и в исходном скрипте, отчёт без фильтров."""
    pass


# ---------------------------------------------------------------------------
# Регион оборудования
# ---------------------------------------------------------------------------

def _build_implement_regions(regions: list[dict], mapping_items: list[dict]) -> dict[int, str]:
    """
    Возвращает {implement_id: region_names_joined}. У одного implement может
    быть НЕСКОЛЬКО одновременно активных маппингов на разные регионы —
    берём ВСЕ маппинги, активные на сегодняшний день, и склеиваем названия
    регионов через ", ". Если на сегодня активных маппингов нет вообще,
    в качестве fallback берём маппинг(и) с самой поздней date_start.
    """
    region_name_by_id = {r.get("id"): r.get("name", "") for r in regions}

    today = date.today().isoformat()

    by_implement: dict[int, list[dict]] = {}
    for mi in mapping_items:
        impl_id = mi.get("implement_id")
        if impl_id is None:
            continue
        by_implement.setdefault(impl_id, []).append(mi)

    implement_region_name: dict[int, str] = {}
    for impl_id, items in by_implement.items():
        def is_active(mi: dict) -> bool:
            date_start = mi.get("date_start") or ""
            date_end = mi.get("date_end")
            no_date_end = mi.get("no_date_end", False)
            if date_start and date_start > today:
                return False
            if no_date_end or not date_end:
                return True
            return date_end >= today

        active = [mi for mi in items if is_active(mi)]
        if not active:
            latest_start = max((mi.get("date_start") or "") for mi in items)
            active = [mi for mi in items if (mi.get("date_start") or "") == latest_start]

        seen = set()
        names = []
        for mi in sorted(active, key=lambda mi: mi.get("date_start") or ""):
            region_id = mi.get("machine_region_id")
            name = region_name_by_id.get(region_id, "")
            if name and name not in seen:
                seen.add(name)
                names.append(name)

        implement_region_name[impl_id] = ", ".join(names)

    return implement_region_name


def _get_archived_implement_ids(history: list[dict]) -> set[int]:
    """
    Определяет архивное оборудование через InventoryHistoryItem: берём
    последнюю по event_start_at запись для каждого implement_id, и если
    available=False или hidden=True — считаем архивным.
    """
    latest_by_implement: dict[int, dict] = {}
    for h in history:
        impl_id = h.get("historyable_id")
        if impl_id is None:
            continue
        current = latest_by_implement.get(impl_id)
        if current is None or h.get("event_start_at", "") > current.get("event_start_at", ""):
            latest_by_implement[impl_id] = h

    archived_ids: set[int] = set()
    for impl_id, h in latest_by_implement.items():
        available = h.get("available", True)
        hidden = h.get("hidden", False)
        if available is False or hidden is True:
            archived_ids.add(impl_id)
    return archived_ids


# ---------------------------------------------------------------------------
# Excel builder
# ---------------------------------------------------------------------------

def _build_row(item: dict, region_by_implement: dict, include_min_max: bool = False) -> list:
    row = [
        region_by_implement.get(item.get("id"), ""),
        _translate_implement_type(item.get("implement_type", "")),
        item.get("name", ""),
        item.get("manufacturer", ""),
        item.get("model", ""),
        item.get("year", ""),
        item.get("registration_number", ""),
        item.get("inventory_number", ""),
        item.get("chassis_serial_number", ""),
        item.get("description", ""),
        item.get("legal_company", ""),
        item.get("width", ""),
        item.get("official_width", ""),
        "",  # RFID — пока не заполняется
        item.get("additional_info", ""),
        item.get("external_id", ""),
        item.get("id", ""),
    ]
    if include_min_max:
        min_w = item.get("min_width", "")
        max_w = item.get("max_width", "")
        min_max = "" if (min_w == "" and max_w == "") else f"{min_w}-{max_w}"
        row.append(min_max)
    return row


def _write_sheet(ws, items: list[dict], columns: list[str],
                  region_by_implement: dict, include_min_max: bool = False) -> None:
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    ws.append(columns)
    for c in ws[1]:
        c.font = Font(name="Calibri", size=11, bold=True)
    for item in items:
        ws.append(_build_row(item, region_by_implement, include_min_max=include_min_max))
    for ci, _ in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = 22
    ws.freeze_panes = "A2"


def _build_excel(implements_active: list[dict], implements_archived: list[dict],
                  region_by_implement: dict) -> bytes:
    import openpyxl

    fixed_width    = [i for i in implements_active if not i.get("variable_width")]
    variable_width = [i for i in implements_active if i.get("variable_width")]

    wb = openpyxl.Workbook()
    ws_fixed = wb.active
    ws_fixed.title = "Фикс. ширина"
    _write_sheet(ws_fixed, fixed_width, COLUMNS_BASE, region_by_implement, include_min_max=False)

    ws_var = wb.create_sheet("Перем. ширина")
    _write_sheet(ws_var, variable_width, COLUMNS_VARIABLE, region_by_implement, include_min_max=True)

    ws_archived = wb.create_sheet("Архив")
    _write_sheet(ws_archived, implements_archived, COLUMNS_VARIABLE, region_by_implement, include_min_max=True)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Сбор данных
# ---------------------------------------------------------------------------

async def _fetch_implements_report() -> bytes:
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        implements_all, regions, mapping_items, history = await asyncio.gather(
            _fetch_all_pages(session, "implements", sem=_OPS_SEM),
            _fetch_all_pages(session, "machine_regions", sem=_OPS_SEM),
            _fetch_all_pages(session, "implement_region_mapping_items", sem=_OPS_SEM),
            _fetch_all_pages(session, "inventory_history_items",
                              {"historyable_type": "Implement"}, sem=_OPS_SEM),
        )

    archived_ids = _get_archived_implement_ids(history)
    region_by_implement = _build_implement_regions(regions, mapping_items)

    implements_active   = [i for i in implements_all if i.get("id") not in archived_ids]
    implements_archived = [i for i in implements_all if i.get("id") in archived_ids]

    logger.info(
        f"implements: всего {len(implements_all)}, "
        f"активных {len(implements_active)}, архивных {len(implements_archived)}"
    )

    return _build_excel(implements_active, implements_archived, region_by_implement)


# ---------------------------------------------------------------------------
# Эндпоинт
# ---------------------------------------------------------------------------

@router.post(
    "",
    summary="Навесное оборудование — Excel",
    description=(
        "Полный реестр навесного оборудования (implements) из Cropwise: "
        "«Фикс. ширина» / «Перем. ширина» / «Архив». Без фильтров."
    ),
)
async def post_implements_query(
    body: Optional[ImplementsRequest] = None,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        xlsx_bytes = await _fetch_implements_report()
    except aiohttp.ClientError as e:
        logger.error(f"implements_query Cropwise error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка получения данных из Cropwise: {e}")
    except Exception as e:
        logger.error(f"implements_query error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка формирования отчёта: {e}")

    fname = f"implements_{date.today().isoformat()}.xlsx"
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )
