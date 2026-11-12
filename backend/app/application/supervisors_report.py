"""Отчёт по заданиям машин: листы agri и transport.

Строка — поле задания. Путь в рабочей области берётся из
machine_task_field_mapping_items.work_distance, путь за задание — из
machine_tasks.total_distance. Тип работ — имя группы work_type_groups.
Колонка «Общий расход топлива, л» есть в файле, значение в каждой строке — 0.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any

import httpx
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from app.config import settings

TZ = timezone(timedelta(hours=3))
PAGE_LIMIT = 1000
PAGE_SIZE = 100
MAX_PERIOD = timedelta(days=93)

HEADERS = (
    "№",
    "Машина",
    "Госномер",
    "Тип работ",
    "Пройденный путь в рабочей области, км",
    "Пройденный путь за время задания, км",
    "Общий расход топлива, л",
)

_SHEET_ORDER = ("agri", "transport")


class SupervisorsReportError(Exception):
    pass


def parse_period(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start_at = _as_moscow(start)
    end_at = _as_moscow(end)
    if end_at <= start_at:
        raise SupervisorsReportError("Конец периода должен быть позже начала.")
    if end_at - start_at > MAX_PERIOD:
        raise SupervisorsReportError("Период длиннее 93 суток.")
    return start_at, end_at


def list_work_groups() -> list[dict[str, Any]]:
    with _client() as client:
        rows = _pages(client, "work_type_groups")
    groups = []
    for row in rows:
        try:
            gid = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(row.get("name") or "").strip()
        if name:
            groups.append({"id": gid, "name": name})
    groups.sort(key=lambda item: item["name"].casefold())
    return groups


def build_excel(start: datetime, end: datetime, work_group_ids: list[int] | None) -> tuple[bytes, str]:
    start_at, end_at = parse_period(start, end)
    selected = {int(item) for item in (work_group_ids or [])}
    with _client() as client:
        groups = {int(row["id"]): str(row.get("name") or "").strip() for row in _pages(client, "work_type_groups")}
        work_types = _pages(client, "work_types")
        type_group = {}
        ids_by_group: dict[int, list[int]] = defaultdict(list)
        for row in work_types:
            try:
                wid = int(row["id"])
                gid = int(row["work_type_group_id"])
            except (KeyError, TypeError, ValueError):
                continue
            type_group[wid] = gid
            ids_by_group[gid].append(wid)
        unknown = [gid for gid in selected if gid not in groups]
        if unknown:
            raise SupervisorsReportError("Неизвестный тип работ.")
        work_ids: list[int] | None = None
        if selected:
            work_ids = []
            for gid in selected:
                work_ids.extend(ids_by_group.get(gid, []))
            if not work_ids:
                rows: list[dict[str, Any]] = []
                return _workbook(rows), _filename(start_at, end_at)
        tasks = _tasks(client, start_at, end_at, work_ids)
        tasks = [task for task in tasks if _overlaps(task, start_at, end_at)]
        if selected:
            tasks = [task for task in tasks if type_group.get(_int(task.get("work_type_id"))) in selected]
        task_ids = [_int(task.get("id")) for task in tasks]
        task_ids = [item for item in task_ids if item]
        machine_ids = {_int(task.get("machine_id")) for task in tasks}
        machine_ids.discard(0)
        machines = _by_ids(client, "machines", sorted(machine_ids))
        fields = _by_task_ids(client, "machine_task_field_mapping_items", task_ids)
    rows = assemble_rows(tasks, machines, type_group, groups, fields)
    return _workbook(rows), _filename(start_at, end_at)


def assemble_rows(
    tasks: list[dict],
    machines: dict[int, dict],
    type_group: dict[int, int],
    groups: dict[int, str],
    fields: list[dict],
) -> list[dict[str, Any]]:
    fields_by_task: dict[int, list[dict]] = defaultdict(list)
    for item in fields:
        tid = _int(item.get("machine_task_id"))
        if tid:
            fields_by_task[tid].append(item)
    for bucket in fields_by_task.values():
        bucket.sort(key=lambda item: _int(item.get("id")))

    rows: list[dict[str, Any]] = []
    ordered = sorted(tasks, key=lambda task: (_int(task.get("id"))))
    for task in ordered:
        tid = _int(task.get("id"))
        if not tid:
            continue
        machine = machines.get(_int(task.get("machine_id"))) or {}
        kind = str(machine.get("machine_type") or "").strip().lower() or "прочие"
        group_name = groups.get(type_group.get(_int(task.get("work_type_id")), 0), "")
        task_km = _km(task.get("total_distance"))
        pieces = fields_by_task.get(tid) or [None]
        for piece in pieces:
            area_km = _km(piece.get("work_distance")) if piece else 0.0
            rows.append(
                {
                    "sheet": kind,
                    "machine": _machine_label(machine),
                    "task_id": tid,
                    "field_id": _int(piece.get("id")) if piece else 0,
                    "values": (
                        tid,
                        _machine_label(machine),
                        str(machine.get("registration_number") or "").strip(),
                        group_name,
                        area_km,
                        task_km,
                        0,
                    ),
                }
            )
    rows.sort(key=lambda row: (row["sheet"], row["machine"].casefold(), row["task_id"], row["field_id"]))
    return rows


def _workbook(rows: list[dict[str, Any]]) -> bytes:
    book = Workbook()
    sheets: dict[str, Any] = {}
    first = book.active
    names = list(_SHEET_ORDER)
    for row in rows:
        if row["sheet"] not in names:
            names.append(row["sheet"])
    first.title = names[0]
    sheets[names[0]] = first
    for name in names[1:]:
        sheets[name] = book.create_sheet(name[:31])
    header_font = Font(bold=True)
    for name, sheet in sheets.items():
        sheet.append(list(HEADERS))
        for cell in sheet[1]:
            cell.font = header_font
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        for row in rows:
            if row["sheet"] == name:
                sheet.append(list(row["values"]))
        last_col = get_column_letter(len(HEADERS))
        sheet.auto_filter.ref = f"A1:{last_col}{max(sheet.max_row, 1)}"
        sheet.freeze_panes = "A2"
        sheet.row_dimensions[1].height = 32
        for index, width in enumerate((12, 62, 16, 24, 22, 22, 22), start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _filename(start: datetime, end: datetime) -> str:
    left = start.strftime("%d.%m.%Y_%H-%M")
    right = end.strftime("%d.%m.%Y_%H-%M")
    return f"Отчет_{left}_{right}.xlsx"


def _machine_label(machine: dict) -> str:
    name = str(machine.get("name") or "").strip()
    plate = str(machine.get("registration_number") or "").strip()
    inventory = str(machine.get("inventory_number") or "").strip()
    return " / ".join(part for part in (name, plate, inventory) if part)


def _km(meters: Any) -> float:
    try:
        value = float(meters or 0)
    except (TypeError, ValueError):
        value = 0.0
    return round(value / 1000, 2)


def _overlaps(task: dict, start: datetime, end: datetime) -> bool:
    task_start = _parse_dt(task.get("start_time"))
    task_end = _parse_dt(task.get("end_time"))
    if task_start is None or task_end is None:
        return False
    return task_start <= end and task_end >= start


def _as_moscow(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=TZ)
    return value.astimezone(TZ)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _client() -> httpx.Client:
    from app.config import get_cropwise_token

    token = (get_cropwise_token() or "").strip()
    if not token:
        raise SupervisorsReportError("Не задан токен Cropwise.")
    base = (settings.cropwise_base_url or "https://operations.cropwise.com/api/v3").rstrip("/")
    return httpx.Client(
        base_url=base,
        headers={"X-User-Api-Token": token},
        timeout=180,
    )


def _pages(client: httpx.Client, endpoint: str, params: dict | None = None) -> list[dict]:
    out: list[dict] = []
    from_id = 0
    base = dict(params or {})
    while True:
        query = {**base, "from_id": from_id, "limit": PAGE_LIMIT}
        response = client.get(f"/{endpoint}", params=query)
        response.raise_for_status()
        body = response.json()
        chunk = body.get("data") or []
        if not isinstance(chunk, list) or not chunk:
            break
        out.extend(item for item in chunk if isinstance(item, dict))
        meta = ((body.get("meta") or {}).get("response") or {})
        last = meta.get("last_record_id")
        obtained = int(meta.get("obtained_records") or len(chunk))
        if not last or obtained < PAGE_SIZE:
            break
        from_id = int(last) + 1
    return out


def _tasks(client: httpx.Client, start: datetime, end: datetime, work_ids: list[int] | None) -> list[dict]:
    query = {
        "start_time_lt": (end + timedelta(seconds=1)).isoformat(),
        "end_time_gt_eq": start.isoformat(),
    }
    found: dict[int, dict] = {}
    batches = [work_ids[index:index + 20] for index in range(0, len(work_ids), 20)] if work_ids else [None]
    for batch in batches:
        params = dict(query)
        if batch:
            params["work_type_id"] = ",".join(str(item) for item in batch)
        for row in _pages(client, "machine_tasks", params):
            tid = _int(row.get("id"))
            if tid:
                found[tid] = row
    return list(found.values())


def _by_task_ids(client: httpx.Client, endpoint: str, task_ids: list[int]) -> list[dict]:
    out: list[dict] = []
    for index in range(0, len(task_ids), 80):
        chunk = task_ids[index:index + 80]
        out.extend(_pages(client, endpoint, {"machine_task_id": ",".join(str(item) for item in chunk)}))
    return out


def _by_ids(client: httpx.Client, endpoint: str, ids: list[int]) -> dict[int, dict]:
    found: dict[int, dict] = {}
    for index in range(0, len(ids), 80):
        chunk = ids[index:index + 80]
        for row in _pages(client, endpoint, {"ids": ",".join(str(item) for item in chunk)}):
            item_id = _int(row.get("id"))
            if item_id:
                found[item_id] = row
    return found

