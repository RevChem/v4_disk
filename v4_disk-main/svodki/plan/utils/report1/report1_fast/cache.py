"""Кэш machine_tasks / mtfmi / agro_operations для сводки 1 v2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Report1DataCache:
    tasks_by_wt: dict[int, list[dict]] = field(default_factory=dict)
    mtfmi_by_task: dict[int, list[dict]] = field(default_factory=dict)
    agro_ops_wt55: list[dict] | None = None
    parse_dt: Callable[[Any], Any] | None = None

    def set_parse_dt(self, fn: Callable[[Any], Any]) -> None:
        self.parse_dt = fn

    def _filter_tasks(self, tasks: list[dict], params: dict) -> list[dict]:
        parse = self.parse_dt
        if parse is None:
            return list(tasks)
        lo_raw = params.get("end_time_gt_eq")
        hi_raw = params.get("end_time_lt")
        if not lo_raw and not hi_raw:
            return list(tasks)
        out: list[dict] = []
        for t in tasks:
            end_dt = parse(t.get("end_time"))
            if end_dt is None:
                continue
            if lo_raw:
                lo_dt = parse(lo_raw)
                if lo_dt is not None and end_dt < lo_dt:
                    continue
            if hi_raw:
                hi_dt = parse(hi_raw)
                if hi_dt is not None and end_dt >= hi_dt:
                    continue
            out.append(t)
        return out

    def get_machine_tasks(self, params: dict) -> list[dict] | None:
        wt_raw = params.get("work_type_id")
        if wt_raw is None:
            return None
        try:
            wt = int(wt_raw)
        except (TypeError, ValueError):
            return None
        chunk = self.tasks_by_wt.get(wt)
        if chunk is None:
            return None
        return self._filter_tasks(chunk, params)

    def get_mtfmi(self, task_id: int) -> list[dict] | None:
        rows = self.mtfmi_by_task.get(int(task_id))
        if rows is None:
            return None
        return list(rows)

    def get_agro_operations(self, params: dict) -> list[dict] | None:
        if self.agro_ops_wt55 is None:
            return None
        wt_raw = params.get("work_type_id")
        season = str(params.get("season") or "")
        if wt_raw is None:
            return None
        try:
            wt = int(wt_raw)
        except (TypeError, ValueError):
            return None
        if wt != 55:
            return None
        if season and self.agro_ops_wt55:
            filtered = [
                op
                for op in self.agro_ops_wt55
                if str(op.get("season") or "") == season or not op.get("season")
            ]
            return filtered if filtered else list(self.agro_ops_wt55)
        return list(self.agro_ops_wt55)


CACHE = Report1DataCache()
