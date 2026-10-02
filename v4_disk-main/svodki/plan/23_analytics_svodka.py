"""
Запуск отчёта 23 (аналитика по сводке за сутки).
Реализация: ranis_svodka (9).py (основная), иначе ranis_svodka (3).py / ranis_svodka.py.

Цифры по работам берутся из снимка сводки 2 (bundle JSON) — быстро, за секунды.
Если bundle нет: SVODKI_R23_ALLOW_LIVE_CROPWISE=1 (медленно) или SVODKI_R23_PREFETCH=1
(сбор prefetch сводки 2 через svodki_prefetch.py).

Дата суток в отчёте:
  REPORT_DATE=YYYY-MM-DD            — TODAY сводки 2 (дата платформы); сутки в 23 = REPORT_DATE − 1
  SVODKI_ANALYTICS_DATE=YYYY-MM-DD  — явное переопределение суток (ручной запуск)

Веб кладёт число в cwd `.r23_fertilizer_transport_machines` и в env.
Через `runpy.run_path(..., init_globals=…)` передаём `_SVODKI_R23_MACHINES` в глобалы ranis.
"""
from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path


def _install_svodki_hooks_from_sidecar() -> None:
    """Тот же механизм, что sitecustomize на PYTHONPATH: prefetch/cache до import ranis."""
    new_dir = Path(__file__).resolve().parent
    sc_path = new_dir / "sitecustomize.py"
    if not sc_path.is_file():
        return
    import importlib.util

    mod_name = "_svodki_sitecustomize_hooks"
    mod = sys.modules.get(mod_name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(mod_name, sc_path)
        if spec is None or spec.loader is None:
            return
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    install = getattr(mod, "install_svodki_api_hooks", None)
    if callable(install):
        install()


_R23_REQUIRED_PACKAGES = (
    ("docx", "python-docx"),
    ("httpx", "httpx"),
    ("requests", "requests"),
)


def _ensure_r23_dependencies() -> None:
    missing: list[str] = []
    for module_name, pip_name in _R23_REQUIRED_PACKAGES:
        try:
            __import__(module_name)
        except ModuleNotFoundError:
            missing.append(pip_name)
    if not missing:
        return
    parent = Path(__file__).resolve().parent
    print(
        "Не установлены зависимости для сводки 23:\n"
        + "\n".join(f"  - {name}" for name in missing)
        + "\n\nУстановите в venv этой папки:\n"
        f"  cd {parent}\n"
        "  .venv/bin/pip install -r requirements.txt\n"
        "или:\n"
        f"  .venv/bin/pip install {' '.join(missing)}",
        file=sys.stderr,
    )
    sys.exit(1)


def _resolve_analytics_day() -> date:
    raw = (os.environ.get("SVODKI_ANALYTICS_DATE") or "").strip()
    if raw:
        return date.fromisoformat(raw)
    raw = (os.environ.get("REPORT_DATE") or "").strip()
    if raw:
        return date.fromisoformat(raw) - timedelta(days=1)
    return date.today() - timedelta(days=1)


def _prefetch_matches_day(path: Path, report_day: date) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("report_date") or "").strip() == report_day.isoformat()
    except (OSError, ValueError, TypeError):
        return False


def _try_set_bundle_env(parent: Path, report_day: date) -> bool:
    """Подставляет SVODKI_R23_WORK_DATA_BUNDLE_IN, если снимок сводки 2 уже есть на диске."""
    if (os.environ.get("SVODKI_R23_WORK_DATA_BUNDLE_IN") or "").strip():
        return True
    candidates: list[Path] = []
    upload_raw = (os.environ.get("UPLOAD_DIR") or "").strip()
    if upload_raw:
        candidates.append(
            Path(upload_raw) / "_r23_donor" / f"report2_{report_day.isoformat()}.json"
        )
    for rel in (
        f"uploads/_r23_donor/report2_{report_day.isoformat()}.json",
        f".r23_bundle_{report_day.isoformat()}.json",
    ):
        candidates.append(parent / rel)
    for p in candidates:
        if p.is_file():
            os.environ["SVODKI_R23_WORK_DATA_BUNDLE_IN"] = str(p.resolve())
            print(f"[r23] Bundle сводки 2: {p}", flush=True)
            return True
    return False


def _ensure_report2_prefetch(parent: Path, report_day: date) -> None:
    """Снимок Cropwise как у сводки 2 на выбранные сутки."""
    existing = (os.environ.get("SVODKI_PREFETCH_DATA_PATH") or "").strip()
    if existing:
        p = Path(existing)
        if p.is_file() and _prefetch_matches_day(p, report_day):
            return
        print(
            f"[r23] Prefetch {existing} не за {report_day} — пересборка под сводку 2.",
            flush=True,
        )

    out = parent / f".r23_prefetch_{report_day.isoformat()}.json"
    if out.is_file() and _prefetch_matches_day(out, report_day):
        os.environ["SVODKI_PREFETCH_DATA_PATH"] = str(out.resolve())
        os.environ["SVODKI_PREFETCH_STRICT"] = "1"
        print(f"[r23] Используем кэш prefetch: {out.name}", flush=True)
        return

    prefetch_script = parent / "svodki_prefetch.py"
    if not prefetch_script.is_file():
        print(
            "[r23] svodki_prefetch.py не найден — сводка 23 пойдёт в Cropwise напрямую.",
            flush=True,
        )
        return

    env = os.environ.copy()
    env["REPORT_DATE"] = report_day.isoformat()
    env["SVODKI_PREFETCH_REPORT_IDS"] = "2"
    env.pop("SVODKI_PREFETCH_DATA_PATH", None)
    env.pop("SVODKI_PREFETCH_STRICT", None)
    env.pop("SVODKI_API_CACHE_PATH", None)
    print(f"[r23] Собираем prefetch сводки 2 за {report_day}...", flush=True)
    rc = subprocess.call(
        [sys.executable, str(prefetch_script), str(out)],
        cwd=parent,
        env=env,
    )
    if rc != 0 or not out.is_file():
        print(
            f"[r23] Prefetch завершился с кодом {rc} — сводка 23 пойдёт в Cropwise напрямую.",
            flush=True,
        )
        return
    os.environ["SVODKI_PREFETCH_DATA_PATH"] = str(out.resolve())
    os.environ["SVODKI_PREFETCH_STRICT"] = "1"
    print(f"[r23] Prefetch готов: {out.name}", flush=True)


def _r23_init_globals_for_runpy() -> dict[str, object]:
    """Число: файл в cwd → env; возвращаем init_globals для runpy."""
    raw: str | None = None
    p = Path.cwd() / ".r23_fertilizer_transport_machines"
    if p.is_file():
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except OSError:
            raw = None
    if not raw or not raw.isdigit():
        s = (os.environ.get("SVODKI_R23_FERTILIZER_TRANSPORT_MACHINES") or "").strip()
        raw = s if s.isdigit() else None
    if not raw or not raw.isdigit():
        return {}
    n = int(raw)
    if n < 0 or n > 999_999:
        return {}
    os.environ["SVODKI_R23_FERTILIZER_TRANSPORT_MACHINES"] = str(n)
    return {"_SVODKI_R23_MACHINES": n}


def main() -> None:
    _ensure_r23_dependencies()
    parent = Path(__file__).resolve().parent
    report_day = _resolve_analytics_day()
    # Никогда не подставлять штамп платформы как «сутки» — иначе в тексте +1 день (9 июня вместо 8).
    os.environ.pop("SVODKI_ANALYTICS_DATE", None)
    rebuild = (os.environ.get("SVODKI_R23_DOC_REBUILD_ONLY") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if not rebuild:
        has_bundle = _try_set_bundle_env(parent, report_day)
        allow_live = (os.environ.get("SVODKI_R23_ALLOW_LIVE_CROPWISE") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        want_prefetch = (os.environ.get("SVODKI_R23_PREFETCH") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if not has_bundle and (want_prefetch or allow_live):
            _ensure_report2_prefetch(parent, report_day)
    _install_svodki_hooks_from_sidecar()
    init = _r23_init_globals_for_runpy()
    for name in ("ranis_svodka (9).py", "ranis_svodka (3).py", "ranis_svodka.py"):
        src = parent / name
        if src.is_file():
            if init:
                runpy.run_path(str(src), run_name="__main__", init_globals=init)
            else:
                runpy.run_path(str(src), run_name="__main__")
            return
    print(
        f"Не найден ни ranis_svodka (9).py, ни ranis_svodka (3).py, ни ranis_svodka.py в {parent}",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
