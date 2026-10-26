"""Карточка одного задания Cropwise: читаемые поля и ссылка на страницу задания."""
from __future__ import annotations

import re

import httpx

from app.application.task_links import TASK_PAGE
from app.config import get_cropwise_token, settings

_STATUS = {
    "done": "Выполнено",
    "in_progress": "В работе",
    "planned": "Запланировано",
    "canceled": "Отменено",
    "confirmed": "Подтверждено",
}
_CUSTOM = {
    "x_custom_bunkera": "Бункера",
    "x_custom_glubina": "Глубина",
    "x_custom_norma_vyseva": "Норма высева",
    "x_custom_pricepshiki": "Прицепщики",
    "x_custom_prichina_prostoya_tekhniki": "Причина простоя",
    "x_custom_tonnazh": "Тоннаж",
}
_WHEN_RE = re.compile(r"\.\d+")
_TZ_RE = re.compile(r"[+-]\d{2}:\d{2}$")


def machine_task_card(task_id: int) -> dict:
    token = (get_cropwise_token() or "").strip()
    if not token:
        raise RuntimeError("Не задан токен Cropwise")
    base = (settings.cropwise_base_url or "https://operations.cropwise.com/api/v3").rstrip("/")
    try:
        with httpx.Client(headers={"X-User-Api-Token": token}, timeout=20) as client:
            task = _get(client, f"{base}/machine_tasks/{int(task_id)}")
            if not isinstance(task, dict):
                raise LookupError(task_id)
            machine_id = _int(task.get("machine_id"))
            machine = _optional(client, f"{base}/machines/{machine_id}") if machine_id else None
            work_type_id = _int(task.get("work_type_id"))
            work_type = _optional(client, f"{base}/work_types/{work_type_id}") if work_type_id else None
            driver_id = _int(task.get("driver_id"))
            driver = _optional(client, f"{base}/users/{driver_id}") if driver_id else None
            implement_id = _int(task.get("implement_id"))
            implement = _optional(client, f"{base}/implements/{implement_id}") if implement_id else None
            mapping = _optional(client, f"{base}/machine_task_field_mapping_items", {"machine_task_id": int(task_id)})
            fields = _fields(client, base, mapping if isinstance(mapping, list) else [])
    except httpx.HTTPError as exc:
        raise RuntimeError("Cropwise не ответил") from exc

    work_name = _text(work_type, "name") if isinstance(work_type, dict) else ""
    machine_name = _machine_label(machine if isinstance(machine, dict) else None)
    url = TASK_PAGE.format(machine_id=machine_id, task_id=int(task_id)) if machine_id else ""
    facts = _facts(task, machine_name, work_name, driver if isinstance(driver, dict) else None, implement if isinstance(implement, dict) else None)
    return {
        "id": int(task_id),
        "url": url,
        "title": work_name or f"Задание {int(task_id)}",
        "facts": facts,
        "fields": fields,
    }


def _facts(task: dict, machine_name: str, work_name: str, driver: dict | None, implement: dict | None) -> list[dict]:
    status = str(task.get("status") or "")
    rows = [
        ("Статус", _STATUS.get(status, status)),
        ("Сезон", task.get("season")),
        ("Тип работы", work_name),
        ("Начало", _when(task.get("start_time"))),
        ("Окончание", _when(task.get("end_time"))),
        ("Машина", machine_name),
        ("Исполнитель", _text(driver, "username") if driver else ""),
        ("Орудие", _text(implement, "name") if implement else ""),
        ("Обработано, га", _num(task.get("covered_area"), keep_zero=True)),
        ("В работе, га", _num(task.get("work_area"), keep_zero=True)),
        ("Площадь вручную, га", _num(task.get("manual_covered_area"))),
        ("Топливо, л", _num(task.get("fuel_consumption"), keep_zero=True)),
        ("Топливо на га, л", _num(task.get("fuel_consumption_per_ha"))),
        ("Топливо на дороге, л", _num(task.get("fuel_consumption_on_road"))),
        ("Описание", _text(task, "description")),
    ]
    for key, label in _CUSTOM.items():
        value = task.get(key)
        if value not in (None, "", [], {}):
            rows.append((label, value))
    return [{"label": label, "value": value} for label, value in rows if value not in (None, "", [])]


def _fields(client: httpx.Client, base: str, items: list) -> list[dict]:
    acc: dict[int, float] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        fid = _int(item.get("field_id"))
        if not fid:
            continue
        try:
            area = float(item.get("covered_area") or 0)
        except (TypeError, ValueError):
            area = 0.0
        acc[fid] = acc.get(fid, 0.0) + area
    out = []
    for fid, area in sorted(acc.items()):
        field = _optional(client, f"{base}/fields/{fid}")
        name = _text(field, "name") if isinstance(field, dict) else ""
        out.append({"id": fid, "name": name or str(fid), "covered_ha": round(area, 2)})
        if len(out) >= 40:
            break
    return out


def _machine_label(machine: dict | None) -> str:
    if not machine:
        return ""
    name = _text(machine, "name")
    plate = _text(machine, "registration_number")
    if name and plate and plate.casefold() not in name.casefold():
        return f"{name} · {plate}"
    return name or plate


def _optional(client: httpx.Client, url: str, params: dict | None = None):
    try:
        return _get(client, url, params)
    except httpx.HTTPError:
        return None


def _get(client: httpx.Client, url: str, params: dict | None = None):
    response = client.get(url, params=params)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    body = response.json()
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _text(obj: dict | None, key: str) -> str:
    if not isinstance(obj, dict):
        return ""
    return str(obj.get(key) or "").strip()


def _int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def _num(value, *, keep_zero: bool = False):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if number == 0 and not keep_zero:
        return None
    return round(number, 2)


def _when(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("T", " ")
    text = _WHEN_RE.sub("", text)
    return _TZ_RE.sub("", text).strip()
