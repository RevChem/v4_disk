"""Культура строки: кэш обновляется, пустая ссылка остаётся в сумме, сбой загрузки виден."""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

PLAN = Path(__file__).resolve().parents[2]
for p in (PLAN, PLAN / "utils" / "api", PLAN / "utils" / "shared", PLAN / "utils" / "season", PLAN / "proverka"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    import httpx  # noqa: F401
except ImportError:  # заглушка только если библиотеки нет
    httpx_stub = types.ModuleType("httpx")
    httpx_stub.AsyncClient = object
    sys.modules["httpx"] = httpx_stub


def test_stale_cache_is_replaced_and_blank_row_stays_in_total(monkeypatch) -> None:
    import mtfmi_batch_fetch as m

    payload = {"rows": [{"id": 7, "crop_id": 3, "year": 2026, "field_id": 4, "crop_id_present": True}]}

    async def mass_request_async(client, base_url, kind, ids, sem):
        row = payload["rows"][0]
        if not row.get("crop_id_present", True):
            return [{"id": row["id"], "year": row["year"], "field_id": row["field_id"]}]
        return [dict(row)]

    mtaomi = types.ModuleType("mtaomi_batch_fetch")
    mtaomi.mass_request_async = mass_request_async
    monkeypatch.setitem(sys.modules, "mtaomi_batch_fetch", mtaomi)

    async def crops_by_id(client, base_url, sem, *, refresh=False):
        return {3: "Соя"}

    monkeypatch.setattr(m, "_crops_by_id", crops_by_id)
    m._HISTORY_CROP.clear()
    m._HISTORY_FIELD.clear()
    m._HISTORY_ERRORS.clear()
    m._HISTORY_CROP[7] = "Пшеница"

    async def run() -> None:
        await m.remember_history_crops(
            None, "http://cropwise", {1: [{"history_item_id": 7, "field_id": 4}]}, None,
        )

    asyncio.run(run())
    assert m._HISTORY_CROP[7] == "Соя"
    assert m.crop_of_item({"history_item_id": 7, "field_id": 4}) == "Соя"
    assert m.crop_of_item({"history_item_id": 7, "field_id": 9}, "Пшеница с карты") == ""

    payload["rows"] = [{"id": 7, "year": 2026, "field_id": 4, "crop_id_present": False}]
    asyncio.run(run())
    assert m.crop_label({"history_item_id": 7, "field_id": 4}) == m.UNDEFINED_CROP
    m._HISTORY_CROP[8] = "Соя"
    m._HISTORY_FIELD[8] = 4
    named = m.crop_label({"history_item_id": 8, "field_id": 4})
    blank = m.crop_label({})
    hectares: dict[str, float] = {}
    for key, ha in ((blank, 3.0), (named, 5.0)):
        hectares[key] = hectares.get(key, 0.0) + ha
    assert blank == m.UNDEFINED_CROP
    assert named == "Соя"
    assert abs(sum(hectares.values()) - 8.0) < 1e-9

    m.note_history_load_error(RuntimeError("timeout"))
    assert any("timeout" in line for line in m.history_load_errors())

    from proverka.data_checks import collect_data_checks

    notes = collect_data_checks()
    assert any("timeout" in line for line in notes)


def test_empty_harvest_crop_is_not_other() -> None:
    from task_area_shared import crop_key_for_harvest_area
    import mtfmi_batch_fetch as m

    assert crop_key_for_harvest_area(1, "", set(), "Прочие") == m.UNDEFINED_CROP
    assert crop_key_for_harvest_area(1, "Горох", set(), "Прочие") == "Прочие"


def test_season_mismatch_is_listed_without_replacing_crop() -> None:
    from season_mismatch_log import log_row_season_mismatches, mismatch_lines

    log_row_season_mismatches(
        [{"id": 11, "season": "2026"}],
        {11: [{"field_id": 2, "history_item_id": 9}]},
        {9: 2027},
        {9: "Соя"},
    )
    lines = mismatch_lines()
    assert any("2026" in line and "2027" in line and "Соя" in line for line in lines)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
