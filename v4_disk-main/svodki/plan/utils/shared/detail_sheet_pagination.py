"""Общая логика разрывов страниц для листов детализации (сводки 3–9).

Правила:
- «СОБСТВЕННЫЕ» и «НАЁМНЫЕ» одной фирмы на одной странице, если помещаются;
- построчный перенос (без переноса целого «хвоста»);
- заголовок секции не остаётся без ≥1 строки данных.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from openpyxl.worksheet.pagebreak import Break


@dataclass(frozen=True)
class DetailPaginationConfig:
    page_body_height_pt: float = 505.0
    page_safe_reserve_pt: float = 36.0
    section_title_height_pt: float = 26.0
    header_height_pt: float = 52.0
    data_row_height_pt: float = 24.0
    data_row_max_height_pt: float = 36.0
    total_row_height_pt: float = 24.0
    section_gap_height_pt: float = 12.0
    atomic_section_extra_buffer_pt: float = 80.0
    single_page_slack_pt: float = 32.0
    wrap_line_height_pt: float = 14.0
    note_height_pt: float = 34.0
    wrap_chars_per_unit: float = 0.65


DEFAULT_DETAIL_PAGINATION = DetailPaginationConfig()


def estimate_wrapped_lines(value: object, col_width: float | None, *, chars_per_unit: float) -> int:
    text = str(value or "").strip()
    if not text:
        return 1
    width = float(col_width or 10)
    chars_per_line = max(1, int(width * chars_per_unit))
    parts = text.splitlines() or [text]
    lines = 0
    for part in parts:
        p = part.strip()
        if not p:
            lines += 1
            continue
        lines += max(1, math.ceil(len(p) / chars_per_line))
    return max(1, lines)


def estimate_row_height_by_values(
    values: list[object],
    col_widths: list[float],
    *,
    min_height: float,
    max_height: float,
    line_height: float,
    chars_per_unit: float,
) -> float:
    max_lines = 1
    for idx, value in enumerate(values):
        if value in (None, ""):
            continue
        width = col_widths[idx] if idx < len(col_widths) else 10.0
        max_lines = max(
            max_lines,
            estimate_wrapped_lines(value, width, chars_per_unit=chars_per_unit),
        )
    return min(max_height, max(min_height, max_lines * line_height + 4))


class DetailPageContext:
    """Отслеживание высоты страницы и явные row breaks в Excel."""

    def __init__(
        self,
        ws,
        n_cols: int,
        col_widths: list[float],
        cfg: DetailPaginationConfig = DEFAULT_DETAIL_PAGINATION,
        *,
        start_row: int = 1,
        on_new_page: Callable[[], None] | None = None,
    ) -> None:
        self.ws = ws
        self.n_cols = n_cols
        self.col_widths = col_widths
        self.cfg = cfg
        self.row = start_row
        self.page_used_pt = 0.0
        self.on_new_page = on_new_page
        self.page_limit = max(1.0, cfg.page_body_height_pt - cfg.page_safe_reserve_pt)
        self.page_limit_relaxed = self.page_limit + cfg.single_page_slack_pt

    def row_height(
        self,
        values: list[object],
        *,
        min_height: float | None = None,
        max_height: float | None = None,
    ) -> float:
        return estimate_row_height_by_values(
            values,
            self.col_widths,
            min_height=min_height or self.cfg.data_row_height_pt,
            max_height=max_height or self.cfg.data_row_max_height_pt,
            line_height=self.cfg.wrap_line_height_pt,
            chars_per_unit=self.cfg.wrap_chars_per_unit,
        )

    def section_block_height(
        self,
        rows: list[dict],
        detail_vals: Callable[[int, dict], list[object]],
    ) -> float:
        if not rows:
            return 0.0
        block_h = self.cfg.section_title_height_pt + self.cfg.header_height_pt
        for idx, rd in enumerate(rows, start=1):
            block_h += self.row_height(detail_vals(idx, rd))
        block_h += self.cfg.total_row_height_pt
        return block_h

    def firm_sheet_block_height(
        self,
        detail_own: list[dict],
        detail_hired: list[dict],
        detail_vals: Callable[[int, dict], list[object]],
        *,
        include_note: bool = True,
    ) -> float:
        block_h = self.cfg.note_height_pt if include_note else 0.0
        if detail_own:
            block_h += self.section_block_height(detail_own, detail_vals)
        if detail_own and detail_hired:
            block_h += self.cfg.section_gap_height_pt
        if detail_hired:
            block_h += self.section_block_height(detail_hired, detail_vals)
        return block_h

    def ensure_space(self, required_height_pt: float, *, relaxed: bool = False) -> bool:
        if required_height_pt <= 0:
            return False
        limit = self.page_limit_relaxed if relaxed else self.page_limit
        if self.page_used_pt > 0 and self.page_used_pt + required_height_pt > limit:
            try:
                self.ws.row_breaks.append(Break(id=max(1, self.row - 1)))
            except Exception:
                pass
            self.page_used_pt = 0.0
            if self.on_new_page:
                self.on_new_page()
            return True
        return False

    def add_section_gap(self, *, firm_single_page: bool) -> None:
        if firm_single_page:
            self.ws.row_dimensions[self.row].height = self.cfg.section_gap_height_pt
            self.row += 1
            self.page_used_pt += self.cfg.section_gap_height_pt
            return
        if self.ensure_space(self.cfg.section_gap_height_pt):
            return
        self.ws.row_dimensions[self.row].height = self.cfg.section_gap_height_pt
        self.row += 1
        self.page_used_pt += self.cfg.section_gap_height_pt

    def min_section_start_height(
        self,
        rows: list[dict],
        detail_vals: Callable[[int, dict], list[object]],
    ) -> float:
        need = self.cfg.section_title_height_pt + self.cfg.header_height_pt
        need += self.row_height(detail_vals(1, rows[0]))
        if len(rows) == 1:
            need += self.cfg.total_row_height_pt
        return need

    def before_section_header(
        self,
        rows: list[dict],
        detail_vals: Callable[[int, dict], list[object]],
    ) -> None:
        if rows and self.page_used_pt > 0:
            self.ensure_space(self.min_section_start_height(rows, detail_vals), relaxed=False)

    def add_used(self, height_pt: float) -> None:
        self.page_used_pt += height_pt


def write_paginated_section(
    ctx: DetailPageContext,
    *,
    sec_title: str,
    rows: list[dict],
    detail_vals: Callable[[int, dict], list[object]],
    write_caption_and_header: Callable[[str, bool], None],
    write_data_row: Callable[[int, list[object]], float],
    write_total_row: Callable[[], float],
    continue_on_page: bool = False,
    firm_single_page: bool = False,
    same_firm_both_sections: bool = False,
) -> None:
    """Записывает одну секцию (СОБСТВЕННЫЕ / НАЁМНЫЕ) с построчной пагинацией."""
    if not rows:
        return

    cfg = ctx.cfg
    atomic_section = len(rows) <= 1
    continuing_same_page = ctx.page_used_pt > 0
    relaxed_header = continue_on_page or (
        same_firm_both_sections and continuing_same_page
    )

    section_block_h = ctx.section_block_height(rows, detail_vals)
    single_page_section = firm_single_page or (
        not continuing_same_page
        and len(rows) > 1
        and section_block_h <= ctx.page_limit_relaxed
        and (ctx.page_used_pt + section_block_h) <= ctx.page_limit_relaxed
    )

    def _min_section_start_height() -> float:
        return ctx.min_section_start_height(rows, detail_vals)

    if atomic_section and len(rows) == 1:
        one_vals = detail_vals(1, rows[0])
        one_h = ctx.row_height(one_vals)
        one_h = max(one_h, cfg.data_row_height_pt)
        atomic_buffer = (
            0.0
            if (firm_single_page or continuing_same_page or continue_on_page)
            else cfg.atomic_section_extra_buffer_pt
        )
        full_section_need = (
            cfg.section_title_height_pt
            + cfg.header_height_pt
            + one_h
            + cfg.total_row_height_pt
            + atomic_buffer
        )
        if not (firm_single_page or continue_on_page):
            ctx.ensure_space(full_section_need, relaxed=relaxed_header)
    elif len(rows) > 1:
        if single_page_section:
            if not (firm_single_page or continue_on_page):
                ctx.ensure_space(section_block_h, relaxed=relaxed_header)
        elif not continue_on_page:
            first_vals = detail_vals(1, rows[0])
            first_h = ctx.row_height(first_vals)
            ctx.ensure_space(
                cfg.section_title_height_pt + cfg.header_height_pt + first_h,
                relaxed=relaxed_header,
            )

    if ctx.page_used_pt > 0:
        ctx.ensure_space(_min_section_start_height(), relaxed=False)

    write_caption_and_header(sec_title, relaxed_header)

    use_row_pagination = len(rows) > 1 and not single_page_section

    for idx, rd in enumerate(rows, start=1):
        vals = detail_vals(idx, rd)
        data_row_height = ctx.row_height(vals)
        rows_left = len(rows) - idx

        if use_row_pagination and ctx.page_used_pt > 0:
            next_need = data_row_height
            if rows_left == 0:
                next_need += cfg.total_row_height_pt
            if ctx.page_used_pt + next_need > ctx.page_limit:
                try:
                    ctx.ws.row_breaks.append(Break(id=max(1, ctx.row - 1)))
                except Exception:
                    pass
                ctx.page_used_pt = 0.0
                if ctx.on_new_page:
                    ctx.on_new_page()
                ctx.ensure_space(
                    cfg.section_title_height_pt + cfg.header_height_pt,
                    relaxed=relaxed_header,
                )
                write_caption_and_header(sec_title, relaxed_header)

        actual_h = write_data_row(ctx.row, vals)
        ctx.row += 1
        ctx.page_used_pt += actual_h

    if not atomic_section and not (firm_single_page or continue_on_page):
        ctx.ensure_space(cfg.total_row_height_pt, relaxed=relaxed_header)
    total_h = write_total_row()
    ctx.row += 1
    ctx.page_used_pt += total_h


def compute_firm_single_page(
    ctx: DetailPageContext,
    detail_own: list[dict],
    detail_hired: list[dict],
    detail_vals: Callable[[int, dict], list[object]],
    *,
    include_note: bool = True,
) -> bool:
    block_h = ctx.firm_sheet_block_height(
        detail_own,
        detail_hired,
        detail_vals,
        include_note=include_note,
    )
    return block_h <= ctx.page_limit_relaxed

