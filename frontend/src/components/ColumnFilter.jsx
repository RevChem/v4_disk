import { useMemo, useState } from "react";
import { Box, Button, Menu, MenuItem, TextField } from "@mui/material";
import ArrowDropDownIcon from "@mui/icons-material/ArrowDropDown";

export const EMPTY_COLUMNS = { plate: "", crop: "", operator: "", field: "" };

const COLUMN_KEYS = ["plate", "crop", "operator", "field"];

export function distinctColumnValues(rows) {
  const bags = { plate: new Set(), crop: new Set(), operator: new Set(), field: new Set() };
  for (const row of rows || []) {
    for (const key of COLUMN_KEYS) {
      const value = String(row?.[key] ?? "").trim();
      if (value && value !== "—") bags[key].add(value);
    }
  }
  const options = {};
  for (const key of COLUMN_KEYS) {
    options[key] = [...bags[key]].sort((a, b) => a.localeCompare(b, "ru"));
  }
  return options;
}

export function applyColumnFilters(rows, columns) {
  const active = COLUMN_KEYS.filter((key) => columns?.[key]);
  if (!active.length) return rows;
  return (rows || []).filter((row) =>
    active.every((key) => String(row?.[key] ?? "").trim() === columns[key]),
  );
}

/** Заголовок колонки: список значений текущего среза, клик оставляет только выбранное. */
export function ColumnFilter({ label, values, selected, onSelect, maxOptions = 0 }) {
  const [anchor, setAnchor] = useState(null);
  const [query, setQuery] = useState("");
  const options = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase("ru-RU");
    const list = Array.isArray(values) ? values : [];
    if (!needle) return list;
    return list.filter((value) => String(value).toLocaleLowerCase("ru-RU").includes(needle));
  }, [values, query]);
  const shown = maxOptions > 0 ? options.slice(0, maxOptions) : options;
  const hidden = options.length - shown.length;

  function close() {
    setAnchor(null);
    setQuery("");
  }

  function choose(value) {
    onSelect(selected === value ? "" : value);
    close();
  }

  return (
    <>
      <Button
        size="small"
        color={selected ? "primary" : "inherit"}
        onClick={(event) => setAnchor(event.currentTarget)}
        endIcon={<ArrowDropDownIcon />}
        title={selected ? `${label}: ${selected}` : label}
        sx={{
          textTransform: "none",
          fontWeight: 700,
          minWidth: 0,
          maxWidth: 200,
          px: 0.25,
          lineHeight: 1.2,
        }}
      >
        <Box component="span" sx={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {selected ? `${label}: ${selected}` : label}
        </Box>
      </Button>
      <Menu
        anchorEl={anchor}
        open={Boolean(anchor)}
        onClose={close}
        autoFocus={false}
        disableAutoFocusItem
        slotProps={{
          root: { sx: { zIndex: 2000 } },
          paper: { sx: { maxHeight: 360, minWidth: 220 } },
        }}
      >
        <Box
          sx={{ px: 1, pb: 1, position: "sticky", top: 0, bgcolor: "background.paper", zIndex: 1 }}
          onKeyDown={(event) => event.stopPropagation()}
        >
          <TextField
            size="small"
            fullWidth
            autoFocus
            placeholder="Поиск"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => event.stopPropagation()}
          />
        </Box>
        <MenuItem selected={!selected} onClick={() => choose("")}>
          Все
        </MenuItem>
        {shown.map((value) => (
          <MenuItem key={value} selected={selected === value} onClick={() => choose(value)}>
            {value}
          </MenuItem>
        ))}
        {hidden > 0 ? <MenuItem disabled>Ещё {hidden}. Уточните поиск.</MenuItem> : null}
        {!options.length ? <MenuItem disabled>Нет значений</MenuItem> : null}
      </Menu>
    </>
  );
}
