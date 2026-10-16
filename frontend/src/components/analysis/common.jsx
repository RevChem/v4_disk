import { useMemo, useState } from "react";
import {
  Box,
  Button,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TableSortLabel,
  Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";

export const POPPER_PROPS = { popper: { sx: { zIndex: 1600 } } };

export const fmt = (v, d = 1) =>
  v == null || v === "" ? "—" : Number(v).toLocaleString("ru-RU", { maximumFractionDigits: d, minimumFractionDigits: 0 });

export function fmtDate(iso) {
  const [y, m, d] = String(iso || "").split("-");
  return y && m && d ? `${d}.${m}.${y}` : iso || "";
}

export function isoDay(date) {
  const z = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${z(date.getMonth() + 1)}-${z(date.getDate())}`;
}

export function monthBounds(iso) {
  const [y, m] = String(iso || "").split("-").map(Number);
  if (!y || !m) {
    const now = new Date();
    return monthBounds(isoDay(now));
  }
  const last = new Date(y, m, 0).getDate();
  const mm = String(m).padStart(2, "0");
  return { from: `${y}-${mm}-01`, to: `${y}-${mm}-${String(last).padStart(2, "0")}` };
}

export function csvEscape(value) {
  const s = value == null ? "" : String(value);
  return /["; \n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function downloadCsv(filename, columns, rows) {
  const head = columns.map((c) => csvEscape(c.label)).join(";");
  const body = rows.map((r) =>
    columns.map((c) => csvEscape(c.csv ? c.csv(r) : r[c.key])).join(";"),
  );
  const blob = new Blob([`﻿${[head, ...body].join("\r\n")}`], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** Плитка с показателем. tone: default | good | warn | bad */
export function KpiCard({ label, value, sub, tone = "default", sx }) {
  return (
    <Paper
      variant="outlined"
      sx={(theme) => {
        const color =
          tone === "bad" ? theme.palette.error.main
            : tone === "warn" ? theme.palette.warning.main
              : tone === "good" ? theme.palette.success.main
                : theme.palette.primary.main;
        return {
          p: 1.5,
          minWidth: 0,
          borderLeft: `4px solid ${color}`,
          background: alpha(color, theme.palette.mode === "dark" ? 0.08 : 0.04),
          ...sx,
        };
      }}
    >
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", lineHeight: 1.2 }}>
        {label}
      </Typography>
      <Typography variant="h6" fontWeight={700} sx={{ lineHeight: 1.3 }} noWrap>
        {value}
      </Typography>
      {sub && (
        <Typography variant="caption" color="text.secondary" sx={{ display: "block" }} noWrap>
          {sub}
        </Typography>
      )}
    </Paper>
  );
}

/**
 * Таблица с сортировкой по клику на заголовок и «показать ещё».
 * columns: [{ key, label, align, render(row), sort(row) -> значение, csv(row), width, tone(row) -> "bad"|"warn"|undefined }]
 */
export function SortableTable({
  columns,
  rows,
  rowKey,
  defaultSort,
  defaultDir = "desc",
  emptyText = "Нет данных",
  pageSize = 400,
  maxHeight,
  fill = false,
  onRowClick,
}) {
  const [sortKey, setSortKey] = useState(defaultSort || null);
  const [dir, setDir] = useState(defaultDir);
  const [limit, setLimit] = useState(pageSize);

  const sorted = useMemo(() => {
    if (!sortKey) return rows;
    const col = columns.find((c) => c.key === sortKey);
    if (!col) return rows;
    const val = (r) => (col.sort ? col.sort(r) : r[col.key]);
    const out = [...rows];
    out.sort((a, b) => {
      const x = val(a);
      const y = val(b);
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      const cmp = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), "ru");
      return dir === "asc" ? cmp : -cmp;
    });
    return out;
  }, [rows, columns, sortKey, dir]);

  function toggle(key) {
    if (sortKey === key) setDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(key);
      setDir("desc");
    }
  }

  const shown = sorted.slice(0, limit);

  return (
    <Box sx={fill ? { display: "flex", flexDirection: "column", height: "100%", minHeight: 0 } : undefined}>
      <Box
        sx={{
          overflow: "auto",
          maxHeight,
          ...(fill ? { flex: 1, minHeight: 0 } : null),
          border: 1,
          borderColor: "divider",
          borderRadius: 1.5,
          bgcolor: "background.paper",
        }}
      >
        <Table size="small" stickyHeader sx={{ "& .MuiTableCell-root": { px: 1.25 } }}>
          <TableHead>
            <TableRow>
              {columns.map((c) => (
                <TableCell
                  key={c.key}
                  align={c.align || "left"}
                  sx={{ whiteSpace: "nowrap", fontWeight: 700, width: c.width, minWidth: c.minWidth, bgcolor: "background.paper" }}
                  sortDirection={sortKey === c.key ? dir : false}
                >
                  <TableSortLabel
                    active={sortKey === c.key}
                    direction={sortKey === c.key ? dir : "desc"}
                    onClick={() => toggle(c.key)}
                  >
                    {c.label}
                  </TableSortLabel>
                </TableCell>
              ))}
            </TableRow>
          </TableHead>
          <TableBody>
            {shown.map((row, i) => (
              <TableRow
                key={rowKey ? rowKey(row, i) : i}
                hover
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                sx={onRowClick ? { cursor: "pointer" } : undefined}
              >
                {columns.map((c) => {
                  const tone = c.tone ? c.tone(row) : undefined;
                  return (
                    <TableCell
                      key={c.key}
                      align={c.align || "left"}
                      sx={(theme) => ({
                        ...(c.align === "right" ? { fontVariantNumeric: "tabular-nums" } : null),
                        ...(c.wrap ? null : { whiteSpace: "nowrap" }),
                        ...(c.minWidth ? { minWidth: c.minWidth } : null),
                        ...(tone === "bad"
                          ? { color: theme.palette.error.main, fontWeight: 700 }
                          : tone === "warn"
                            ? { color: theme.palette.warning.main, fontWeight: 600 }
                            : null),
                      })}
                    >
                      {c.render ? c.render(row) : row[c.key] ?? "—"}
                    </TableCell>
                  );
                })}
              </TableRow>
            ))}
            {!shown.length && (
              <TableRow>
                <TableCell colSpan={columns.length}>
                  <Typography variant="body2" color="text.secondary" sx={{ py: 3, textAlign: "center" }}>
                    {emptyText}
                  </Typography>
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </Box>
      {sorted.length > shown.length && (
        <Stack direction="row" alignItems="center" spacing={1.5} sx={{ mt: 1 }}>
          <Typography variant="caption" color="text.secondary">
            Показано {shown.length} из {sorted.length}
          </Typography>
          <Button size="small" onClick={() => setLimit((n) => n + pageSize)}>
            Показать ещё
          </Button>
        </Stack>
      )}
    </Box>
  );
}
