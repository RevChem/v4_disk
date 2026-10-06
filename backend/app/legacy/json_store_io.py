"""Надёжный ввод-вывод для JSON-хранилищ (правки планов/ячеек/комментариев, настройки).

Проблемы, которые закрывает модуль:
* запись через общий ``*.tmp`` из нескольких потоков портила файл (или падала на replace);
* прямой ``write_text`` отдавал читателю недописанный файл;
* повреждённый файл молча читался как ``{}``, и следующее сохранение затирало ВСЕ правки.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:  # pragma: no cover - на Windows flock нет
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - только Windows
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]

from app.config import settings
from app.infrastructure.logging import get_logger

log = get_logger(__name__)

_DEFAULT_LOCK_NAME = "default"
_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_tls = threading.local()

_READ_RETRIES = 3
_READ_RETRY_DELAY_SEC = 0.05


class JsonStoreReadError(OSError):
    """Файл существует, но прочитать его не удалось (сбой ввода-вывода, права доступа).

    Намеренно НЕ превращается в ``{}``: иначе следующее сохранение затёрло бы исправный файл.
    """


def _thread_lock(name: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(name)
        if lock is None:
            lock = _THREAD_LOCKS[name] = threading.RLock()
        return lock


def _safe_lock_name(name: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(name or ""))
    return cleaned or _DEFAULT_LOCK_NAME


def _lock_path(name: str) -> Path:
    base = Path(settings.upload_dir)
    if name == _DEFAULT_LOCK_NAME:
        return base / ".json_store.lock"
    return base / f".json_store.{name}.lock"


def _acquire_file_lock(fh) -> None:
    """Межпроцессная блокировка: flock (Linux/macOS) или msvcrt.locking (Windows)."""
    if fcntl is not None:
        fcntl.flock(fh, fcntl.LOCK_EX)
    elif msvcrt is not None:  # pragma: no cover - только Windows
        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)  # сам ждёт ~10 с и бросает OSError
                return
            except OSError:
                time.sleep(0.05)
    # иначе — только потоковая блокировка


def _release_file_lock(fh) -> None:
    if fcntl is not None:
        fcntl.flock(fh, fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - только Windows
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


def atomic_write_text(path: Path, text: str) -> Path:
    """Запись через уникальный временный файл + fsync + os.replace (читатель видит старое или новое целиком)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    return path


def atomic_write_json(path: Path, data: Any, **dump_kwargs: Any) -> Path:
    dump_kwargs.setdefault("ensure_ascii", False)
    return atomic_write_text(path, json.dumps(data, **dump_kwargs))


def _quarantine(path: Path, label: str, reason: str) -> Path:
    backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    with contextlib.suppress(OSError):
        os.replace(path, backup)
    log.error("json_store.corrupt", store=label or path.name, path=str(path),
              backup=str(backup), error=reason)
    return backup


def read_json_dict(path: Path, *, label: str = "") -> dict[str, Any]:
    """Читает JSON-объект.

    * Нет файла -> ``{}``.
    * Повреждённое содержимое (не JSON / не UTF-8 / не объект) -> файл сохраняется как
      ``*.corrupt-<ts>``, ошибка пишется в лог, возвращается ``{}``.
    * Сбой чтения (``OSError``: права, ввод-вывод) -> несколько повторов, затем
      ``JsonStoreReadError``. Исправный файл НЕ переименовывается и не подменяется ``{}``.
    """
    path = Path(path)
    if not path.is_file():
        return {}
    text = None
    last_exc: OSError | None = None
    for attempt in range(_READ_RETRIES):
        try:
            text = path.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            return {}  # файл исчез между проверкой и чтением
        except UnicodeDecodeError as exc:  # это ValueError, не OSError: содержимое повреждено
            _quarantine(path, label, str(exc))
            return {}
        except OSError as exc:
            last_exc = exc
            if attempt + 1 < _READ_RETRIES:
                time.sleep(_READ_RETRY_DELAY_SEC * (attempt + 1))
    if text is None:
        log.error("json_store.read_failed", store=label or path.name, path=str(path),
                  error=str(last_exc))
        raise JsonStoreReadError(f"Не удалось прочитать {path}: {last_exc}") from last_exc
    try:
        raw = json.loads(text)
    except ValueError as exc:  # JSONDecodeError
        _quarantine(path, label, str(exc))
        return {}
    if not isinstance(raw, dict):
        _quarantine(path, label, f"ожидался JSON-объект, получен {type(raw).__name__}")
        return {}
    return raw


@contextlib.contextmanager
def store_rmw_lock(name: str = _DEFAULT_LOCK_NAME) -> Iterator[None]:
    """Сериализует цикл «прочитать -> изменить -> записать» для хранилища ``name``.

    RLock между потоками + файловая блокировка между процессами (flock / msvcrt.locking).
    У каждого ``name`` свои RLock и lock-файл, поэтому независимые хранилища не ждут друг друга.
    Реентерабельна в пределах одного потока и одного ``name``.
    """
    name = _safe_lock_name(name)
    with _thread_lock(name):
        depths = getattr(_tls, "depths", None)
        if depths is None:
            depths = _tls.depths = {}
        depth = depths.get(name, 0)
        fh = None
        if depth == 0 and (fcntl is not None or msvcrt is not None):
            try:
                lock_path = _lock_path(name)
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fh = open(lock_path, "a+")
                _acquire_file_lock(fh)
            except OSError:
                if fh is not None:
                    with contextlib.suppress(OSError):
                        fh.close()
                fh = None
        depths[name] = depth + 1
        try:
            yield
        finally:
            depths[name] = depth
            if fh is not None:
                with contextlib.suppress(OSError):
                    _release_file_lock(fh)
                with contextlib.suppress(OSError):
                    fh.close()


def serialized_rmw(fn=None, *, name: str = _DEFAULT_LOCK_NAME):
    """Декоратор: тело функции выполняется под ``store_rmw_lock(name)``.

    Работает как ``@serialized_rmw`` и как ``@serialized_rmw(name="plans_edits")``.
    """
    import functools

    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with store_rmw_lock(name):
                return func(*args, **kwargs)

        return wrapper

    return decorate(fn) if fn is not None else decorate
