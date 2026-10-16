import { useMemo, useState } from "react";
import {
  Box,
  Button,
  Chip,
  IconButton,
  InputAdornment,
  Link,
  LinearProgress,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TableSortLabel,
  TextField,
  Typography,
} from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import SearchRoundedIcon from "@mui/icons-material/SearchRounded";
import DownloadRoundedIcon from "@mui/icons-material/DownloadRounded";
import ChevronRightRoundedIcon from "@mui/icons-material/ChevronRightRounded";
import UnfoldMoreRoundedIcon from "@mui/icons-material/UnfoldMoreRounded";
import UnfoldLessRoundedIcon from "@mui/icons-material/UnfoldLessRounded";
import OpenInNewRoundedIcon from "@mui/icons-material/OpenInNewRounded";

const PAGE = 200;
const NUMERIC = new Set(["int", "num", "pct", "progress"]);

export const fmtNum = (v, d = 2) =>
  v === null || v === undefined || v === "" ? "—" : Number(v).toLocaleString("ru-RU", { maximumFractionDigits: d });

export const fmtDate = (iso) => {
  const [y, m, d] = String(iso || "").split("-");
  return y && m && d ? `${d}.${m}.${y}` : iso || "—";
};

const isHot = (label) => /нереальн|дубль/i.test(label);

function csvEscape(value) {
  const s = value === null || value === undefined ? "" : String(value);
  return /[";\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function tableToCsv(columns, rows) {
  const head = columns.map((c) => csvEscape(c.label)).join(";");
  const lines = rows.map((r) =>
    columns
      .map((c) => {
        const v = c.type === "link" && c.href ? r[c.href] || r[c.key] : r[c.key];
        return csvEscape(c.type === "date" ? fmtDate(v) : v);
      })
      .join(";"),
  );
  return `\uFEFF${[head, ...lines].join("\r\n")}`;
}

function Cell({ column, row, onDrill }) {
  const theme = useTheme();
  const v = row[column.key];
  // кликабельное значение: исполнитель / машина / поле открывают свой подробный срез
  if (column.drill && onDrill && v && row[`${column.drill}_id`]) {
    return (
      <Link
        component="button"
        type="button"
        underline="hover"
        title="Открыть подробности"
        onClick={(e) => {
          e.stopPropagation();
          onDrill(column.drill, row);
        }}
        sx={{ textAlign: "left", color: "primary.main", cursor: "pointer", font: "inherit", fontWeight: 600 }}
      >
        {String(v)}
      </Link>
    );
  }
  switch (column.type) {
    case "int":
      return fmtNum(v, 0);
    case "num":
      return fmtNum(v, 2);
    case "pct":
      return v === null || v === undefined ? "—" : `${fmtNum(v, 1)} %`;
    case "date":
      return fmtDate(v);
    case "link": {
      const href = column.href ? row[column.href] : "";
      if (!v && v !== 0) return "—";
      return href ? (
        <Link href={href} target="_blank" rel="noreferrer" underline="hover" sx={{ display: "inline-flex", alignItems: "center", gap: 0.4 }}>
          {v}
          <OpenInNewRoundedIcon sx={{ fontSize: 13, opacity: 0.7 }} />
        </Link>
      ) : (
        v
      );
    }
    case "tag": {
      if (!v) return null;
      const hot = isHot(String(v));
      const tone = hot ? theme.palette.error.main : theme.palette.warning.main;
      return (
        <Chip
          size="small"
          label={v}
          sx={{
            height: 22,
            fontWeight: 600,
            color: tone,
            bgcolor: alpha(tone, theme.palette.mode === "dark" ? 0.18 : 0.12),
            border: `1px solid ${alpha(tone, 0.35)}`,
          }}
        />
      );
    }
    case "progress": {
      if (v === null || v === undefined) return "—";
      const val = Math.max(0, Math.min(100, Number(v)));
      const color = Number(v) >= 100 ? "success" : "primary";
      return (
        <Stack direction="row" spacing={1} alignItems="center" sx={{ minWidth: 150 }}>
          <LinearProgress
            variant="determinate"
            value={val}
            color={color}
            sx={{ flex: 1, height: 7, borderRadius: 4, bgcolor: alpha(theme.palette.text.primary, 0.08) }}
          />
          <Typography variant="caption" sx={{ width: 50, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
            {fmtNum(v, 1)} %
          </Typography>
        </Stack>
      );
    }
    default:
      return v === null || v === undefined || v === "" ? "—" : String(v);
  }
}

function compare(a, b, numeric) {
  const aEmpty = a === null || a === undefined || a === "";
  const bEmpty = b === null || b === undefined || b === "";
  if (aEmpty || bEmpty) return aEmpty === bEmpty ? 0 : aEmpty ? 1 : -1;
  return numeric ? Number(a) - Number(b) : String(a).localeCompare(String(b), "ru");
}

export default function HubTable({ columns, rows, tree = false, filename = "analytics", truncated = false, totalRows, onDrill }) {
  const theme = useTheme();
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState(null); // {key, dir}
  const [limit, setLimit] = useState(PAGE);
  const [open, setOpen] = useState(() => new Set());

  const childIds = useMemo(() => {
    const s = new Set();
    if (tree) rows.forEach((r) => r._parent && s.add(r._parent));
    return s;
  }, [rows, tree]);

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (needle) {
      return rows.filter((r) => columns.some((c) => String(r[c.key] ?? "").toLowerCase().includes(needle)));
    }
    if (!tree) return rows;
    const byId = new Map(rows.map((r) => [r._id, r]));
    return rows.filter((r) => {
      let p = r._parent;
      while (p) {
        if (!open.has(p)) return false;
        p = byId.get(p)?._parent;
      }
      return true;
    });
  }, [rows, columns, search, tree, open]);

  const sorted = useMemo(() => {
    if (!sort || (tree && !search.trim())) return visible;
    const col = columns.find((c) => c.key === sort.key);
    const numeric = NUMERIC.has(col?.type);
    const out = [...visible].sort((a, b) => compare(a[sort.key], b[sort.key], numeric));
    return sort.dir === "desc" ? out.reverse() : out;
  }, [visible, sort, columns, tree, search]);

  const shown = sorted.slice(0, limit);

  function toggleSort(key) {
    setSort((s) => (!s || s.key !== key ? { key, dir: "asc" } : s.dir === "asc" ? { key, dir: "desc" } : null));
  }

  function toggle(id) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function download() {
    const blob = new Blob([tableToCsv(columns, sorted)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${filename}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1.25 }}>
        <TextField
          size="small"
          placeholder="Поиск по таблице"
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setLimit(PAGE);
          }}
          sx={{ width: { xs: "100%", sm: 280 } }}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <SearchRoundedIcon fontSize="small" />
              </InputAdornment>
            ),
          }}
        />
        {tree && (
          <>
            <Button size="small" startIcon={<UnfoldMoreRoundedIcon />} onClick={() => setOpen(new Set(childIds))}>
              Развернуть
            </Button>
            <Button size="small" startIcon={<UnfoldLessRoundedIcon />} onClick={() => setOpen(new Set())}>
              Свернуть
            </Button>
          </>
        )}
        <Box sx={{ flex: 1 }} />
        {onDrill && columns.some((c) => c.drill) && (
          <Typography variant="caption" color="primary" sx={{ display: { xs: "none", md: "block" }, whiteSpace: "nowrap" }}>
            Синие значения кликабельны
          </Typography>
        )}
        <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: "nowrap" }}>
          {fmtNum(sorted.length, 0)} из {fmtNum(totalRows ?? rows.length, 0)} строк
        </Typography>
        <Button size="small" variant="outlined" startIcon={<DownloadRoundedIcon />} onClick={download} disabled={!sorted.length}>
          CSV
        </Button>
      </Stack>

      <Box
        sx={{
          maxHeight: "min(52vh, 560px)",
          overflow: "auto",
          border: 1,
          borderColor: "divider",
          borderRadius: 2,
          bgcolor: "background.paper",
        }}
      >
        <Table size="small" stickyHeader>
          <TableHead>
            <TableRow>
              {columns.map((c) => (
                <TableCell
                  key={c.key}
                  align={NUMERIC.has(c.type) && c.type !== "progress" ? "right" : "left"}
                  sortDirection={sort?.key === c.key ? sort.dir : false}
                  sx={{ fontWeight: 700, whiteSpace: "nowrap", bgcolor: alpha(theme.palette.primary.main, 0.08), backdropFilter: "blur(6px)" }}
                >
                  <TableSortLabel
                    active={sort?.key === c.key}
                    direction={sort?.key === c.key ? sort.dir : "asc"}
                    onClick={() => toggleSort(c.key)}
                    disabled={tree && !search.trim()}
                  >
                    {c.label}
                  </TableSortLabel>
                </TableCell>
              ))}
            </TableRow>
          </TableHead>
          <TableBody>
            {shown.map((row, i) => {
              const depth = tree && !search.trim() ? row._depth || 0 : 0;
              const hasKids = tree && !search.trim() && childIds.has(row._id);
              return (
                <TableRow
                  key={row._id ?? i}
                  hover
                  sx={{
                    "&:nth-of-type(even)": { bgcolor: alpha(theme.palette.text.primary, 0.025) },
                    ...(tree && depth === 0 ? { "& td": { fontWeight: 700 } } : null),
                  }}
                >
                  {columns.map((c, ci) => (
                    <TableCell
                      key={c.key}
                      align={NUMERIC.has(c.type) && c.type !== "progress" ? "right" : "left"}
                      sx={{ fontVariantNumeric: "tabular-nums", py: 0.9 }}
                    >
                      {ci === 0 && tree ? (
                        <Stack direction="row" alignItems="center" sx={{ pl: depth * 3 }}>
                          {hasKids ? (
                            <IconButton size="small" onClick={() => toggle(row._id)} sx={{ mr: 0.5, p: 0.25 }}>
                              <ChevronRightRoundedIcon
                                fontSize="small"
                                sx={{ transform: open.has(row._id) ? "rotate(90deg)" : "none", transition: "transform .15s" }}
                              />
                            </IconButton>
                          ) : (
                            <Box sx={{ width: 26 }} />
                          )}
                          <Cell column={c} row={row} onDrill={onDrill} />
                        </Stack>
                      ) : (
                        <Cell column={c} row={row} onDrill={onDrill} />
                      )}
                    </TableCell>
                  ))}
                </TableRow>
              );
            })}
            {!shown.length && (
              <TableRow>
                <TableCell colSpan={columns.length} align="center" sx={{ py: 4, color: "text.secondary" }}>
                  {search ? "Ничего не найдено" : "Нет данных за выбранный период"}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </Box>

      {sorted.length > limit && (
        <Box sx={{ textAlign: "center", mt: 1 }}>
          <Button size="small" onClick={() => setLimit((l) => l + PAGE)}>
            Показать ещё {Math.min(PAGE, sorted.length - limit)}
          </Button>
        </Box>
      )}
      {truncated && (
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
          Сервер отдал первые 3000 строк из {fmtNum(totalRows, 0)}. Сузьте период или выберите группу работ.
        </Typography>
      )}
    </Box>
  );
}
