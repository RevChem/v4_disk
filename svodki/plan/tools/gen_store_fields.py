# -*- coding: utf-8 -*-
"""Генератор белого списка полей для svodki_task_store (utils/shared/svodki_task_store_fields.py).

Поле задания/строки mtfmi хранится, если его имя встречается строковым литералом в коде сводок
(`t.get("x")`, `t["x"]`, списки колонок и т.п.). Динамические обращения (имя поля собирается
в рантайме) так не видны — поэтому полный JSON всегда лежит в колонке raw, а при добавлении
поля список перегенерируется и база пересобирается без обращения к API:

    python tools/gen_store_fields.py && python utils/shared/svodki_task_store.py rebuild

Запуск из svodki/plan. ``--extra a,b`` — добавить поля вручную.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "utils" / "shared" / "svodki_task_store_fields.py"
SKIP_DIRS = {"__pycache__", "tests", ".venv", "node_modules"}
SKIP_FILES = {"svodki_task_store.py", "svodki_task_store_fields.py", "test_task_store.py", "gen_store_fields.py"}
IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extra", default="")
    args = ap.parse_args()
    names: set[str] = set()
    n_files = 0
    for p in ROOT.rglob("*.py"):
        if SKIP_DIRS & set(p.relative_to(ROOT).parts) or p.name in SKIP_FILES:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError):
            print("пропущен:", p, file=sys.stderr)
            continue
        n_files += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and IDENT.match(node.value):
                names.add(node.value)
    names |= {x.strip() for x in args.extra.split(",") if x.strip()}
    body = ",\n".join(f"    {n!r}" for n in sorted(names))
    OUT.write_text(
        "# -*- coding: utf-8 -*-\n"
        "# Сгенерировано tools/gen_store_fields.py — не править руками.\n"
        f"# Файлов просмотрено: {n_files}, имён: {len(names)}.\n"
        f"FIELDS = frozenset({{\n{body},\n}})\n",
        encoding="utf-8",
    )
    print(f"{OUT.relative_to(ROOT)}: {len(names)} имён из {n_files} файлов")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
