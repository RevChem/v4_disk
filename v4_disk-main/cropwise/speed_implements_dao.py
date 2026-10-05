"""
cropwise/speed_implements_dao.py
================================
Справочник «Жатки» — оборудование под контролем скорости и его допустимая
скорость. Хранится в JSON-файле (без БД и миграций): список, добавление,
изменение, удаление, импорт из optim.xlsx и выдача норм для проверки скорости.

Где лежит файл
--------------
  /app/data/speed_implements.json   (в Docker это том backend_data, общий для
                                     backend / celery-worker / celery-beat и
                                     НЕ перезаписываемый при выкладке кода)
  либо путь из переменной окружения SPEED_IMPLEMENTS_PATH.
Вне Docker (нет /app/data) — backend/data/speed_implements.json.
НЕ кладите файл рядом с кодом (backend/app/...): при выкладке обновлений
архивом он был бы перезаписан.

Если файла нет, он создаётся автоматически из speed_implements_seed.py
(содержимое бывшего optim.xlsx). Файл можно править и руками — формат
читаемый; главное, чтобы оставался корректным JSON (иначе проверка скорости и
модалка вернут ошибку, а не затрут файл).

Надёжность записи
-----------------
  - чтение-изменение-запись выполняется под межпроцессной блокировкой
    (fcntl.flock на speed_implements.json.lock) — два диспетчера, нажавших
    «Сохранить» одновременно, не потеряют правки друг друга;
  - файл пишется во временный и подменяется целиком (os.replace) — читатель
    (Celery-проверка скорости) никогда не увидит полузаписанный файл;
  - перед каждой записью прошлая версия копируется в speed_implements.json.bak.

Используется:
  - speed_violations_router.py — эндпоинты /api/speed-violations/implements*
  - speed_violations_sync.py   — get_norms_map(): {implement_id: {...speed_kmh}}
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

try:
    import fcntl  # Linux/macOS; на Windows (локальная разработка) — только потоковый lock
except ImportError:  # pragma: no cover
    fcntl = None

from app.logging_config import get_logger

logger = get_logger("cropwise.speed_implements")

TZ_MSK = timezone(timedelta(hours=3))
FILE_VERSION = 1

_thread_lock = threading.Lock()


class ImplementExistsError(Exception):
    """Оборудование с таким implement_id уже есть в справочнике."""

    def __init__(self, implement_id: int):
        super().__init__(f"Оборудование с ID {implement_id} уже есть в списке")
        self.implement_id = implement_id


class ImplementNotFoundError(Exception):
    """Записи с таким id нет в справочнике."""


class ImplementsStoreError(Exception):
    """Файл справочника не читается / повреждён."""


# ----------------------------------------------------------------------------
# Файл: путь, блокировка, чтение, атомарная запись
# ----------------------------------------------------------------------------

def _path() -> str:
    env = os.environ.get("SPEED_IMPLEMENTS_PATH")
    if env:
        return env
    if os.path.isdir("/app/data"):
        return "/app/data/speed_implements.json"
    backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(backend_dir, "data", "speed_implements.json")


def _now_iso() -> str:
    """naive ISO в МСК — так же, как отдаёт даты остальная панель «Нарушения»
    (фронтенд formatStatusChangedAt трактует строку без зоны как МСК)."""
    return datetime.now(TZ_MSK).replace(tzinfo=None).isoformat(timespec="seconds")


def _round1(v: Optional[float]) -> Optional[float]:
    return round(float(v), 1) if v is not None else None


@contextmanager
def _write_lock(path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with _thread_lock:
        if fcntl is None:
            yield
            return
        with open(path + ".lock", "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


def _read_file(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError('ожидался объект с массивом "items"')
        for it in data["items"]:
            if not isinstance(it, dict) or "id" not in it or "implement_id" not in it or "speed_max" not in it:
                raise ValueError(f"некорректная запись: {it!r}")
        data.setdefault("next_id", max((it["id"] for it in data["items"]), default=0) + 1)
        return data
    except Exception as e:
        logger.error(f"speed_implements: не удалось прочитать {path}: {e}")
        raise ImplementsStoreError(f"Файл справочника «Жатки» повреждён или не читается ({path}): {e}") from e


def _write_file(path: str, data: dict) -> None:
    """Атомарная запись: временный файл → fsync → подмена. Прошлая версия → .bak."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".speed_implements_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _seed_data() -> dict:
    from app.cropwise.speed_implements_seed import SEED

    now = _now_iso()
    items = [
        {
            "id": i, "implement_id": impl_id, "name": name, "inventory_number": inv,
            "speed_min": smin, "speed_max": smax,
            "updated_by": None, "updated_by_fio": None, "updated_at": now,
        }
        for i, (impl_id, name, inv, smin, smax) in enumerate(SEED, start=1)
    ]
    return {"version": FILE_VERSION, "next_id": len(items) + 1, "items": items}


def _snapshot() -> dict:
    """Текущее содержимое файла (при первом обращении файл создаётся из seed)."""
    path = _path()
    if not os.path.exists(path):
        with _write_lock(path):
            if not os.path.exists(path):
                _write_file(path, _seed_data())
                logger.info(f"speed_implements: создан {path} из начального списка")
    return _read_file(path)


def _mutate(fn: Callable[[dict], object]):
    """Чтение → fn(data) → запись, всё под блокировкой. Если fn бросает
    исключение, файл не меняется."""
    path = _path()
    with _write_lock(path):
        data = _read_file(path) if os.path.exists(path) else _seed_data()
        result = fn(data)
        _write_file(path, data)
    return result


# ----------------------------------------------------------------------------
# Операции (синхронное ядро + async-обёртки для роутера / Celery)
# ----------------------------------------------------------------------------

def _public(it: dict) -> dict:
    return {
        "id":               it["id"],
        "implement_id":     it["implement_id"],
        "name":             it.get("name"),
        "inventory_number": it.get("inventory_number"),
        "speed_min":        it.get("speed_min"),
        "speed_max":        it["speed_max"],
        "updated_by_fio":   it.get("updated_by_fio"),
        "updated_at":       it.get("updated_at"),
    }


def _list() -> list[dict]:
    items = [_public(it) for it in _snapshot()["items"]]
    items.sort(key=lambda x: ((x["name"] or "").lower(), x["implement_id"]))
    return items


def _create(data: dict, user_id: Optional[int], user_fio: Optional[str]) -> dict:
    def op(store: dict) -> dict:
        if any(it["implement_id"] == data["implement_id"] for it in store["items"]):
            raise ImplementExistsError(data["implement_id"])
        item = {
            "id": store["next_id"],
            "implement_id": data["implement_id"],
            "name": data.get("name"),
            "inventory_number": data.get("inventory_number"),
            "speed_min": _round1(data.get("speed_min")),
            "speed_max": _round1(data["speed_max"]),
            "updated_by": user_id, "updated_by_fio": user_fio, "updated_at": _now_iso(),
        }
        store["next_id"] += 1
        store["items"].append(item)
        return _public(item)
    return _mutate(op)


def _update(item_id: int, data: dict, user_id: Optional[int], user_fio: Optional[str]) -> dict:
    def op(store: dict) -> dict:
        item = next((it for it in store["items"] if it["id"] == item_id), None)
        if item is None:
            raise ImplementNotFoundError()
        if any(it["implement_id"] == data["implement_id"] and it["id"] != item_id for it in store["items"]):
            raise ImplementExistsError(data["implement_id"])
        item.update(
            implement_id=data["implement_id"],
            name=data.get("name"),
            inventory_number=data.get("inventory_number"),
            speed_min=_round1(data.get("speed_min")),
            speed_max=_round1(data["speed_max"]),
            updated_by=user_id, updated_by_fio=user_fio, updated_at=_now_iso(),
        )
        return _public(item)
    return _mutate(op)


def _delete(item_id: int) -> None:
    def op(store: dict) -> None:
        before = len(store["items"])
        store["items"] = [it for it in store["items"] if it["id"] != item_id]
        if len(store["items"]) == before:
            raise ImplementNotFoundError()
    _mutate(op)


def _import_excel(excel_path: str, user_id: Optional[int], user_fio: Optional[str]) -> dict:
    from app.cropwise.speed_norms_excel import load_excel_speed_table

    rows, broken = load_excel_speed_table(excel_path)  # RuntimeError, если не тот формат файла
    broken_list = [{"row": r, "reason": why} for r, why in broken]

    def op(store: dict) -> dict:
        existing = {it["implement_id"] for it in store["items"]}
        added = skipped = 0
        now = _now_iso()
        for r in rows:
            if r["speed_max"] is None:
                continue  # уже учтено в broken
            if r["id"] in existing:
                skipped += 1
                continue
            existing.add(r["id"])
            store["items"].append({
                "id": store["next_id"],
                "implement_id": r["id"],
                "name": str(r["name"]).strip() if r.get("name") else None,
                "inventory_number": r.get("inventory_number"),
                "speed_min": _round1(r["speed_min"]),
                "speed_max": _round1(r["speed_max"]),
                "updated_by": user_id, "updated_by_fio": user_fio, "updated_at": now,
            })
            store["next_id"] += 1
            added += 1
        return {"added": added, "skipped_existing": skipped, "broken": broken_list}
    return _mutate(op)


def _norms_map() -> dict[int, dict]:
    return {
        it["implement_id"]: {
            "id": it["implement_id"], "name": it.get("name"),
            "inventory_number": it.get("inventory_number"),
            "speed_min": it.get("speed_min"), "speed_kmh": it["speed_max"],
        }
        for it in _snapshot()["items"]
    }


def _status() -> dict:
    items = _snapshot()["items"]
    last = max((it.get("updated_at") or "" for it in items), default="")
    uploaded_at = None
    if last:
        try:
            uploaded_at = datetime.fromisoformat(last).strftime("%d.%m.%Y %H:%M:%S")
        except ValueError:
            uploaded_at = last
    return {"uploaded": len(items) > 0, "uploaded_at": uploaded_at, "rows_count": len(items), "broken_rows": 0}


async def list_implements() -> list[dict]:
    return await asyncio.to_thread(_list)


async def create_implement(data: dict, user_id: Optional[int], user_fio: Optional[str]) -> dict:
    """data: implement_id, name, inventory_number, speed_min, speed_max."""
    return await asyncio.to_thread(_create, data, user_id, user_fio)


async def update_implement(item_id: int, data: dict, user_id: Optional[int], user_fio: Optional[str]) -> dict:
    return await asyncio.to_thread(_update, item_id, data, user_id, user_fio)


async def delete_implement(item_id: int) -> None:
    await asyncio.to_thread(_delete, item_id)


async def import_from_excel(path: str, user_id: Optional[int], user_fio: Optional[str]) -> dict:
    """Добавляет в справочник оборудование из optim.xlsx, которого в нём ещё нет
    (по implement_id). Существующие записи НЕ перезаписываются — ручные правки
    в модалке важнее файла. Безопасно вызывать повторно.
    Возвращает {added, skipped_existing, broken: [{row, reason}]}."""
    return await asyncio.to_thread(_import_excel, path, user_id, user_fio)


async def get_norms_map() -> dict[int, dict]:
    """{implement_id: {"id", "name", "inventory_number", "speed_min", "speed_kmh"}}.
    speed_kmh — порог превышения (верхняя граница диапазона)."""
    return await asyncio.to_thread(_norms_map)


async def get_status() -> dict:
    """Для строки статуса на вкладке: сколько записей и когда менялись."""
    return await asyncio.to_thread(_status)
