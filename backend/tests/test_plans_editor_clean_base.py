"""Регрессия: «база» редактора планов не должна впитывать JSON-правки.

В docker-compose у api задан SVODKI_PLANS_EDITS_PATH, а plans.py при импорте сам накладывает
этот JSON. Раньше редактор брал такую «базу», сравнивал с ней и при следующем сохранении
стирал из JSON всё, что было сохранено ранее.
"""
import json

import pytest

from app.config import settings
from app.legacy import plans_editor as pe

G = "2.4 Вспашка"
F = "АФ Чистопольская"


def _reset_cache() -> None:
    pe._BASE_CACHE["key"] = None
    pe._BASE_CACHE["value"] = None


@pytest.fixture
def edits_file(tmp_path, monkeypatch):
    path = tmp_path / "plans_edits.json"
    monkeypatch.setattr(settings, "svodki_plans_edits_path", str(path))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    # как в docker-compose: переменная окружения указывает на тот же файл правок
    monkeypatch.setenv("SVODKI_PLANS_EDITS_PATH", str(path))
    # Иначе set_plan_value переписал бы рабочий svodki/plan/plans.py.
    monkeypatch.setattr(pe, "_persist_editor_to_plans_py", lambda _effective: "тест: файл не пишем")
    _reset_cache()
    yield path
    _reset_cache()


def test_base_ignores_edits_from_env(edits_file):
    clean = pe.extract_base_plans()["base"]["PLAN_RATE_PER_GROUP"][G]
    edits_file.write_text(json.dumps({"PLAN_RATE_PER_GROUP": {G: clean + 123.0}}), encoding="utf-8")
    _reset_cache()  # как после рестарта процесса

    assert pe.extract_base_plans()["base"]["PLAN_RATE_PER_GROUP"][G] == clean
    # а итоговые (эффективные) значения правку, наоборот, учитывают — ровно один раз
    effective = pe.load_effective_plans_module()
    assert effective.PLAN_RATE_PER_GROUP[G] == clean + 123.0


def test_second_edit_keeps_first_after_restart(edits_file):
    pe.set_plan_value("PLAN_RATE_PER_GROUP", [G], 111)
    _reset_cache()  # рестарт процесса: база перечитывается при заданной переменной окружения
    pe.set_plan_value("AVAILABLE_OWN", [G, F], 7)
    _reset_cache()
    pe.set_plan_value("CROP_DISPLAY_RANK", [G, "Пшеница озимая"], 2)

    saved = json.loads(edits_file.read_text(encoding="utf-8"))
    assert saved["PLAN_RATE_PER_GROUP"][G] == 111
    assert saved["AVAILABLE_OWN"][G][F] == 7
    assert saved["CROP_DISPLAY_RANK"][G]["Пшеница озимая"] == 2


def test_work_groups_ids_can_change_names_cannot(edits_file):
    base = pe.extract_base_plans()["base"]["WORK_GROUPS"]
    renamed = [{**g, "name": "другое имя" if g["name"] == "4.0 Посев" else g["name"]} for g in base]
    with pytest.raises(ValueError, match="4.0 Посев"):
        pe.set_work_groups(renamed)

    deleted = [g for g in base if g["name"] != "5.0 Уборка"]
    with pytest.raises(ValueError, match="5.0 Уборка"):
        pe.set_work_groups(deleted)

    changed = []
    for g in base:
        row = {"name": g["name"], "ids": list(g["ids"])}
        if row["name"] == "4.0 Посев":
            row["ids"] = [*row["ids"], 999]
        changed.append(row)
    changed.append({"name": "9.9 Своя группа", "ids": [1]})
    pe.set_work_groups(changed)

    saved = json.loads(edits_file.read_text(encoding="utf-8"))
    sowing = next(g for g in saved["WORK_GROUPS"] if g["name"] == "4.0 Посев")
    assert 999 in sowing["ids"]
    assert any(g["name"] == "9.9 Своя группа" for g in saved["WORK_GROUPS"])


def test_other_layout_keeps_saved_plan_numbers(edits_file):
    pe.set_plan_value("HARDCODED_PLAN", [G, F, "Пшеница озимая"], 9876)
    _reset_cache()
    pe.set_plan_value("OTHER_CROPS_LAYOUT", [G], 1)

    saved = json.loads(edits_file.read_text(encoding="utf-8"))
    assert saved["HARDCODED_PLAN"][G][F]["Пшеница озимая"] == 9876
    assert saved["OTHER_CROPS_LAYOUT"][G] == 1

    pe.set_plan_value("OTHER_CROPS_LAYOUT", [G], 0)
    saved = json.loads(edits_file.read_text(encoding="utf-8"))
    assert saved["HARDCODED_PLAN"][G][F]["Пшеница озимая"] == 9876
    assert G not in saved.get("OTHER_CROPS_LAYOUT", {})
