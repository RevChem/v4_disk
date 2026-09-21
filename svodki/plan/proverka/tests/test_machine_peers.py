"""Peer-сводки для сверки кол-ва машин."""
from __future__ import annotations

from proverka.operation_registry import (
    peer_reports_for_group,
    peer_reports_for_machine_group,
)


def test_harvest_machine_peers_use_report14_not_10_13() -> None:
    area = peer_reports_for_group("5.0 Уборка")
    machines = peer_reports_for_machine_group("5.0 Уборка")
    assert "10" in area
    assert "13" in area
    assert "10" not in machines
    assert "13" not in machines
    assert machines == ("1", "2", "11", "14")


def test_other_ops_share_area_and_machine_peers() -> None:
    for group in ("2.1 Боронование", "4.0 Посев", "5.4 Уборка картофеля, лука"):
        assert peer_reports_for_machine_group(group) == peer_reports_for_group(group)
