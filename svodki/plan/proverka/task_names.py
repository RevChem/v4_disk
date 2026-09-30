"""Точные имена задач Cropwise, которые вообще могут встретиться в work.json.

Совпадение только по равенству имени (после нормализации пробелов и «6.9.»).
Подстрока вроде «Вспаш» или «га» чужую задачу не захватывает.
Хвост трёх имён в исходном списке обрезан — для них сравнение по уникальному
префиксу, код задачи при этом входит в префикс целиком.
"""
from __future__ import annotations

import re

# Имена, которые известны полностью.
EXACT_TASK_NAMES: tuple[str, ...] = (
    "1.1 Внесение минеральных удобрений",
    "1.2 Известкование",
    "1.3 Внесение органических удобрений",
    "2.1 Боронование зяби",
    "2.2 Боронование многолетних трав и стерни",
    "2.4 Вспашка",
    "2.6 Фрезерование",
    "2.7 Гребнеобразование",
    "2.8 Глубокорыхление",
    "2.9 Дискование",
    "2.11 Культивация",
    "2.12 Прикатывание",
    "3.1 Опрыскивание",
    "3.2 Опрыскивание на овощах",
    "4.1 Посадка картофеля",
    "4.2 Посев зерновой сеялкой без удобрений",
    "4.5 Посев зерновой сеялкой с удобрениями",
    "4.8 Посев морковь, свекла",
    "5.1 Уборка подсолнечника",
    "5.2 Уборка зерновых и зернобобовых",
    "5.3 Уборка кукурузы на зерно",
    "5.4 Уборка картофеля, лука",
    "5.5 Уборка моркови и свеклы",
    "5.6 Скашивание ботвы",
    "5.8 Уборка рапса, горчицы, гречихи и проса",
    "6.1 Скашивание с измельчением трав",
    "6.2 Подбор валков с измельчением трав",
    "6.3 Скашивание с измельчением кукурузы",
    "6.4 Ворошение",
    "6.5 Валкообразование",
    "6.6 Подбор валков с прессованием",
    "6.8 Скашивание в валок",
    "6.9 Скашивание на сено",
)

# Уникальные начала имён, у которых в списке обрезан хвост.
TASK_NAME_PREFIXES: tuple[str, ...] = (
    "2.5 Вспашка по многолетним травам и зале",
    "4.3 Посев пропашной сеялкой без удобрени",
    "4.6 Посев пропашной сеялкой с удобрениям",
    "4.10 Пересев зерновой сеялкой без удобре",
)

# Группа сводки → задачи Cropwise, которые в неё входят.
# Имя группы и имя задачи часто различаются («2.1 Боронование» / «2.1 Боронование зяби»).
GROUP_TASKS: dict[str, tuple[str, ...]] = {
    "1.1 Внесение минеральных удобрений": ("1.1 Внесение минеральных удобрений",),
    "2.1 Боронование": (
        "2.1 Боронование зяби",
        "2.2 Боронование многолетних трав и стерни",
    ),
    "2.4 Вспашка": ("2.4 Вспашка",),
    "2.6 Фрезерование": ("2.6 Фрезерование",),
    "2.7 Гребнеобразование": ("2.7 Гребнеобразование",),
    "2.8 Глубокорыхление": ("2.8 Глубокорыхление",),
    "2.9 Дискование": ("2.9 Дискование",),
    "2.11 Культивация": ("2.11 Культивация",),
    "2.12 Прикатывание": ("2.12 Прикатывание",),
    "3.1 Опрыскивание": ("3.1 Опрыскивание",),
    "3.2 Опрыскивание (овощи)": ("3.2 Опрыскивание на овощах",),
    "4.0 Посев": (
        "4.2 Посев зерновой сеялкой без удобрений",
        "4.3 Посев пропашной сеялкой без удобрени",
        "4.5 Посев зерновой сеялкой с удобрениями",
        "4.6 Посев пропашной сеялкой с удобрениям",
        "4.10 Пересев зерновой сеялкой без удобре",
    ),
    "4.1 Посев (овощи)": (
        "4.1 Посадка картофеля",
        "4.8 Посев морковь, свекла",
    ),
    "5.0 Уборка": (
        "5.1 Уборка подсолнечника",
        "5.2 Уборка зерновых и зернобобовых",
        "5.3 Уборка кукурузы на зерно",
        "5.4 Уборка картофеля, лука",
        "5.8 Уборка рапса, горчицы, гречихи и проса",
        "6.3 Скашивание с измельчением кукурузы",
    ),
    "5.4 Уборка картофеля, лука": ("5.4 Уборка картофеля, лука",),
}

_CODE_DOT_RE = re.compile(r"^(\d+\.\d+)\.(?=\S)")


def normalize_task_name(name: str) -> str:
    """Пробелы, ё и «6.9.Скашивание» → «6.9 скашивание»."""
    text = " ".join(str(name or "").replace("ё", "е").replace("Ё", "Е").split())
    text = _CODE_DOT_RE.sub(r"\1 ", text)
    return text.casefold()


_EXACT_NORM = {normalize_task_name(name): name for name in EXACT_TASK_NAMES}
_PREFIX_NORM = tuple(normalize_task_name(name) for name in TASK_NAME_PREFIXES)


def names_match(actual: str, expected: str) -> bool:
    """Точное равенство. Для обрезанного хвоста — только если префикс уникален и целиком содержит код."""
    left = normalize_task_name(actual)
    right = normalize_task_name(expected)
    if not left or not right:
        return False
    if left == right:
        return True
    if right in _PREFIX_NORM and left.startswith(right):
        return True
    if left in _PREFIX_NORM and right.startswith(left):
        return True
    return False


def is_known_task(name: str) -> bool:
    key = normalize_task_name(name)
    if not key:
        return False
    if key in _EXACT_NORM:
        return True
    return any(key.startswith(prefix) for prefix in _PREFIX_NORM)


def canonical_task_name(name: str) -> str | None:
    """Имя из списка, если строка — эта задача. Иначе None."""
    key = normalize_task_name(name)
    exact = _EXACT_NORM.get(key)
    if exact:
        return exact
    for prefix, original in zip(_PREFIX_NORM, TASK_NAME_PREFIXES):
        if key.startswith(prefix) or prefix.startswith(key) and key == prefix:
            return original
    return None


def tasks_for_group(group_name: str) -> tuple[str, ...]:
    mapped = GROUP_TASKS.get(group_name)
    if mapped:
        return mapped
    canon = canonical_task_name(group_name)
    if canon:
        return (canon,)
    return ()


def tasks_for_groups(group_names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for group_name in group_names:
        for task in tasks_for_group(group_name):
            key = normalize_task_name(task)
            if key in seen:
                continue
            seen.add(key)
            out.append(task)
    return tuple(out)
