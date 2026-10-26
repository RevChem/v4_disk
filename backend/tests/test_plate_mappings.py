"""Сопоставления госномеров: файл сайта перекрывает список по умолчанию."""
from __future__ import annotations

import json

import pytest

from app.application import plate_mappings
from app.config import settings


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    seed_dir = tmp_path / "plan"
    seed_dir.mkdir()
    seed = seed_dir / "plate_mappings.json"
    seed.write_text(
        json.dumps({"items": [{"namolot": "АЕ2119 16", "cropwise": "2119АЕ"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "upload"))
    monkeypatch.setattr(settings, "svodki_root", str(tmp_path))
    return tmp_path


def test_load_uses_seed_until_site_file_exists():
    assert plate_mappings.load_items() == [{"namolot": "АЕ2119 16", "cropwise": "2119АЕ"}]
    saved = plate_mappings.save_items([
        {"namolot": "АЕ 2121 16", "cropwise": "2121АЕ"},
    ])
    assert saved == [{"namolot": "АЕ 2121 16", "cropwise": "2121АЕ"}]
    assert plate_mappings.load_items() == saved
    assert plate_mappings.override_path().is_file()


def test_duplicate_namolot_plate_is_rejected():
    with pytest.raises(ValueError, match="повторяется"):
        plate_mappings.save_items([
            {"namolot": "АЕ 2121 16", "cropwise": "2121АЕ"},
            {"namolot": "ае  2121   16", "cropwise": "другой"},
        ])


def test_empty_side_is_rejected():
    with pytest.raises(ValueError, match="оба госномера"):
        plate_mappings.save_items([{"namolot": "АЕ2119 16", "cropwise": "  "}])
