import { Fragment, useEffect, useMemo, useState } from "react";
import {
  Box,
  Button,
  Link,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TablePagination,
  TableRow,
  TableSortLabel,
  Tooltip,
  Typography,
} from "@mui/material";
import DownloadRoundedIcon from "@mui/icons-material/DownloadRounded";

import { ColumnFilter } from "./ColumnFilter";
import { downloadWorkbook } from "../utils/excelSheet";
import {
  EMPTY_SLICE_COLUMNS,
  fieldSheetRows,
  filterSliceRows,
  pageFieldTable,
  pageWorkerTable,
  sliceColumnOptions,
  sliceStats,
  statsSheetRows,
  workerSheetRows,
} from "../utils/taskSlice";

function fmtHa2(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtCount(value) {
  return Number(value || 0).toLocaleString("ru-RU");
}

function taskLink(row) {
  if (!row.taskId && !row.taskUrl) return "—";
  if (!row.taskUrl) return row.taskId;
  return (
    <Link href={row.taskUrl} target="_blank" rel="noopener noreferrer">
      {row.taskId}
    </Link>
  );
}

const groupCell = { verticalAlign: "top", fontWeight: 600 };
const totalCell = { fontWeight: 700, borderTop: 2, borderColor: "divider", bgcolor: "action.hover" };
const TRACK_HINT = "Площадь по треку: covered_area_by_track задания, как в отчёте «Обработанная площадь поля»";

function statPairs(stats) {
  const pairs = [
    ["Фамилий", fmtCount(stats.people)],
    ["Машин", fmtCount(stats.plates)],
    ["Полей", fmtCount(stats.fields)],
    ["Заданий", fmtCount(stats.tasks)],
    ["Площадь, га", fmtHa2(stats.ha)],
    ["По треку, га", fmtHa2(stats.track)],
  ];
  if (stats.track != null) {
    pairs.push(["Разница, га", fmtHa2(stats.ha - stats.track)]);
    pairs.push(["Доля трека", stats.ha ? `${fmtHa2((stats.track / stats.ha) * 100)} %` : "—"]);
  }
  pairs.push(["На фамилию, га", stats.people ? fmtHa2(stats.ha / stats.people) : "—"]);
  pairs.push(["На поле, га", stats.fields ? fmtHa2(stats.ha / stats.fields) : "—"]);
  pairs.push(["Больше всех", stats.topPerson ? `${stats.topPerson.name} · ${fmtHa2(stats.topPerson.ha)}` : "—"]);
  pairs.push(["Крупнейшее поле", stats.topField ? `${stats.topField.name} · ${fmtHa2(stats.topField.ha)}` : "—"]);
  return pairs;
}

function SliceStats({ rows }) {
  const stats = useMemo(() => sliceStats(rows), [rows]);
  const lines = [];
  const pairs = statPairs(stats);
  for (let index = 0; index < pairs.length; index += 3) lines.push(pairs.slice(index, index + 3));
  return (
    <Box>
      <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
        Статистика
      </Typography>
      <Table size="small" sx={{ width: "max-content", maxWidth: "100%" }}>
        <TableBody>
          {lines.map((line, index) => (
            <TableRow key={index}>
              {line.map(([label, value]) => (
                <Fragment key={label}>
                  <TableCell sx={{ color: "text.secondary", border: 0, py: 0.35, pr: 1 }}>{label}</TableCell>
                  <TableCell sx={{ fontWeight: 700, border: 0, py: 0.35, pr: 2.5 }}>{value}</TableCell>
                </Fragment>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Box>
  );
}

function useSliceView(rows) {
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(25);
  const [filters, setFilters] = useState(EMPTY_SLICE_COLUMNS);
  const [sort, setSort] = useState(null);

  useEffect(() => {
    setPage(0);
    setFilters(EMPTY_SLICE_COLUMNS);
    setSort(null);
  }, [rows]);

  const filtered = useMemo(() => filterSliceRows(rows, filters), [rows, filters]);
  const options = useMemo(() => sliceColumnOptions(rows, filters), [rows, filters]);
  const narrowed = Boolean(filters.firm || filters.person || filters.plate || filters.field);

  function pick(key, value) {
    setPage(0);
    setFilters((current) => ({ ...current, [key]: value }));
  }

  function toggleSort(key) {
    setPage(0);
    setSort((current) =>
      !current || current.key !== key ? { key, dir: "desc" } : { key, dir: current.dir === "desc" ? "asc" : "desc" },
    );
  }

  function resetColumns() {
    setPage(0);
    setFilters(EMPTY_SLICE_COLUMNS);
    setSort(null);
  }

  return {
    page,
    setPage,
    pageSize,
    setPageSize,
    filters,
    filtered,
    options,
    pick,
    sort,
    toggleSort,
    resetColumns,
    narrowed: narrowed || Boolean(sort),
  };
}

function FilterHead({ label, values, selected, onSelect, search = false }) {
  return (
    <TableCell>
      <ColumnFilter
        label={label}
        values={values}
        selected={selected}
        onSelect={onSelect}
        maxOptions={search ? 80 : 0}
      />
    </TableCell>
  );
}

function SortHead({ label, sortKey, sort, onSort, hint, align = "right" }) {
  const active = sort?.key === sortKey;
  const direction = active && sort.dir === "asc" ? "asc" : "desc";
  const control = (
    <TableSortLabel active={active} direction={active ? direction : "desc"} onClick={() => onSort(sortKey)}>
      {label}
    </TableSortLabel>
  );
  return (
    <TableCell align={align} sortDirection={active ? direction : false}>
      {hint ? (
        <Tooltip title={hint}>
          <span>{control}</span>
        </Tooltip>
      ) : (
        control
      )}
    </TableCell>
  );
}

function SliceFrame({ caption, count, page, pageSize, onPage, onPageSize, pageLabel, toolbar, stats, children }) {
  return (
    <Box sx={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", gap: 1 }}>
      {stats}
      <Stack direction="row" spacing={1} alignItems="center" useFlexGap flexWrap="wrap">
        <Typography variant="body2" sx={{ flex: 1 }}>
          {caption}
        </Typography>
        {toolbar}
      </Stack>
      <Box sx={{ flex: 1, minHeight: 0, overflow: "auto" }}>
        <Table size="small" stickyHeader>
          {children}
        </Table>
      </Box>
      {count ? (
        <TablePagination
          component="div"
          count={count}
          page={page}
          onPageChange={(_event, next) => onPage(next)}
          rowsPerPage={pageSize}
          onRowsPerPageChange={(event) => {
            onPageSize(Number(event.target.value));
            onPage(0);
          }}
          rowsPerPageOptions={[25, 50, 100]}
          labelRowsPerPage={pageLabel}
          labelDisplayedRows={({ from, to, count: total }) => `${from}–${to} из ${total}`}
        />
      ) : null}
    </Box>
  );
}

function columnToolbar(onDownload, onReset, narrowed) {
  return (
    <>
      {narrowed ? (
        <Button size="small" onClick={onReset}>
          Сбросить колонки
        </Button>
      ) : null}
      <Button size="small" variant="outlined" startIcon={<DownloadRoundedIcon />} onClick={onDownload}>
        Скачать Excel
      </Button>
    </>
  );
}

export function WorkerSliceTable({ rows, showFirm, note }) {
  const viewState = useSliceView(rows);
  const { filtered, options, filters, pick, sort, toggleSort, resetColumns, narrowed, page, setPage, pageSize } = viewState;
  const built = useMemo(
    () => pageWorkerTable(filtered, page, pageSize, sort),
    [filtered, page, pageSize, sort],
  );
  const safePage = Math.min(page, Math.max(0, Math.ceil(built.count / pageSize) - 1));
  const shown = safePage === page ? built : pageWorkerTable(filtered, safePage, pageSize, sort);
  const columns = (showFirm ? 1 : 0) + 6;
  const caption = `Фамилий ${shown.count.toLocaleString("ru-RU")} · ${fmtHa2(shown.ha)} га${
    shown.track != null ? ` · по треку ${fmtHa2(shown.track)} га` : ""
  }${filtered.length !== rows.length ? ` · строк ${filtered.length.toLocaleString("ru-RU")} из ${rows.length.toLocaleString("ru-RU")}` : ""}${
    note ? `. ${note}` : ""
  }`;

  function download() {
    downloadWorkbook("Срез.xls", [
      { name: "Срез", rows: workerSheetRows(filtered, { showFirm, sort }) },
      { name: "Статистика", rows: statsSheetRows(sliceStats(filtered)) },
    ]);
  }

  return (
    <SliceFrame
      caption={caption}
      count={shown.count}
      page={safePage}
      pageSize={pageSize}
      onPage={setPage}
      onPageSize={viewState.setPageSize}
      pageLabel="Фамилий"
      stats={<SliceStats rows={filtered} />}
      toolbar={columnToolbar(download, resetColumns, narrowed)}
    >
      <TableHead>
        <TableRow>
          {showFirm ? (
            <FilterHead label="Агрофирма" values={options.firm} selected={filters.firm} onSelect={(value) => pick("firm", value)} />
          ) : null}
          <FilterHead label="Фамилия" values={options.person} selected={filters.person} onSelect={(value) => pick("person", value)} search />
          <FilterHead label="Машина" values={options.plate} selected={filters.plate} onSelect={(value) => pick("plate", value)} search />
          <FilterHead label="Поле" values={options.field} selected={filters.field} onSelect={(value) => pick("field", value)} search />
          <SortHead label="Площадь, га" sortKey="ha" sort={sort} onSort={toggleSort} />
          <SortHead label="Площадь по треку, га" sortKey="track" sort={sort} onSort={toggleSort} hint={TRACK_HINT} />
          <SortHead label="Задание" sortKey="task" sort={sort} onSort={toggleSort} align="left" />
        </TableRow>
      </TableHead>
      <TableBody>
        {shown.table.length ? (
          shown.table.map((row, index) => (
            <TableRow key={`${row.kind}-${row.person}-${row.plate || ""}-${row.taskId || ""}-${index}`}>
              {showFirm && row.firmSpan > 0 ? (
                <TableCell rowSpan={row.firmSpan} sx={groupCell}>
                  {row.firm}
                </TableCell>
              ) : null}
              {row.personSpan > 0 ? (
                <TableCell rowSpan={row.personSpan} sx={groupCell}>
                  {row.person}
                </TableCell>
              ) : null}
              {row.kind === "total" ? (
                <>
                  <TableCell colSpan={2} sx={totalCell}>
                    Итого
                  </TableCell>
                  <TableCell align="right" sx={totalCell}>
                    {fmtHa2(row.ha)}
                  </TableCell>
                  <TableCell align="right" sx={totalCell}>
                    {fmtHa2(row.track)}
                  </TableCell>
                  <TableCell sx={totalCell} />
                </>
              ) : (
                <>
                  {row.plateSpan > 0 ? (
                    <TableCell rowSpan={row.plateSpan} sx={{ verticalAlign: "top", whiteSpace: "nowrap" }}>
                      {row.plate}
                    </TableCell>
                  ) : null}
                  <TableCell>{row.field}</TableCell>
                  <TableCell align="right">{fmtHa2(row.ha)}</TableCell>
                  <TableCell align="right">{fmtHa2(row.track)}</TableCell>
                  <TableCell sx={{ whiteSpace: "nowrap" }}>{taskLink(row)}</TableCell>
                </>
              )}
            </TableRow>
          ))
        ) : (
          <TableRow>
            <TableCell colSpan={columns}>
              <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
                Ничего не найдено по выбранным колонкам.
              </Typography>
            </TableCell>
          </TableRow>
        )}
        {shown.table.length ? (
          <TableRow>
            <TableCell colSpan={columns} sx={{ ...totalCell, borderTop: 3 }}>
              Всего {fmtHa2(shown.ha)} га
              {shown.track != null ? ` · по треку ${fmtHa2(shown.track)} га` : ""}
            </TableCell>
          </TableRow>
        ) : null}
      </TableBody>
    </SliceFrame>
  );
}

export function FieldSliceTable({ rows, showFirm, note }) {
  const viewState = useSliceView(rows);
  const { filtered, options, filters, pick, sort, toggleSort, resetColumns, narrowed } = viewState;
  const page = viewState.page;
  const setPage = viewState.setPage;
  const pageSize = viewState.pageSize;
  const built = useMemo(() => pageFieldTable(filtered, page, pageSize, sort), [filtered, page, pageSize, sort]);
  const safePage = Math.min(page, Math.max(0, Math.ceil(built.count / pageSize) - 1));
  const shown = safePage === page ? built : pageFieldTable(filtered, safePage, pageSize, sort);
  const columns = (showFirm ? 1 : 0) + 6;
  const caption = `Полей ${shown.count.toLocaleString("ru-RU")} · ${fmtHa2(shown.ha)} га${
    shown.track != null ? ` · по треку ${fmtHa2(shown.track)} га` : ""
  }${filtered.length !== rows.length ? ` · строк ${filtered.length.toLocaleString("ru-RU")} из ${rows.length.toLocaleString("ru-RU")}` : ""}${
    note ? `. ${note}` : ""
  }`;

  function download() {
    downloadWorkbook("Срезы, поля.xls", [
      { name: "Срезы, поля", rows: fieldSheetRows(filtered, { showFirm, sort }) },
      { name: "Статистика", rows: statsSheetRows(sliceStats(filtered)) },
    ]);
  }

  return (
    <SliceFrame
      caption={caption}
      count={shown.count}
      page={safePage}
      pageSize={pageSize}
      onPage={setPage}
      onPageSize={viewState.setPageSize}
      pageLabel="Полей"
      stats={<SliceStats rows={filtered} />}
      toolbar={columnToolbar(download, resetColumns, narrowed)}
    >
      <TableHead>
        <TableRow>
          {showFirm ? (
            <FilterHead label="Агрофирма" values={options.firm} selected={filters.firm} onSelect={(value) => pick("firm", value)} />
          ) : null}
          <FilterHead label="Поле" values={options.field} selected={filters.field} onSelect={(value) => pick("field", value)} search />
          <FilterHead label="Фамилия" values={options.person} selected={filters.person} onSelect={(value) => pick("person", value)} search />
          <FilterHead label="Машина" values={options.plate} selected={filters.plate} onSelect={(value) => pick("plate", value)} search />
          <SortHead label="Площадь, га" sortKey="ha" sort={sort} onSort={toggleSort} />
          <SortHead label="Площадь по треку, га" sortKey="track" sort={sort} onSort={toggleSort} hint={TRACK_HINT} />
          <SortHead label="Задание" sortKey="task" sort={sort} onSort={toggleSort} align="left" />
        </TableRow>
      </TableHead>
      <TableBody>
        {shown.table.length ? (
          shown.table.map((row, index) => (
            <TableRow key={`${row.kind}-${row.field}-${row.person || ""}-${row.plate || ""}-${row.taskId || ""}-${index}`}>
              {showFirm && row.firmSpan > 0 ? (
                <TableCell rowSpan={row.firmSpan} sx={groupCell}>
                  {row.firm}
                </TableCell>
              ) : null}
              {row.fieldSpan > 0 ? (
                <TableCell rowSpan={row.fieldSpan} sx={groupCell}>
                  {row.field}
                </TableCell>
              ) : null}
              {row.kind === "field" ? (
                <>
                  <TableCell colSpan={2} sx={totalCell}>
                    Итого по полю
                  </TableCell>
                  <TableCell align="right" sx={totalCell}>
                    {fmtHa2(row.ha)}
                  </TableCell>
                  <TableCell align="right" sx={totalCell}>
                    {fmtHa2(row.track)}
                  </TableCell>
                  <TableCell sx={totalCell} />
                </>
              ) : row.kind === "person" ? (
                <>
                  <TableCell sx={totalCell}>Итого</TableCell>
                  <TableCell align="right" sx={totalCell}>
                    {fmtHa2(row.ha)}
                  </TableCell>
                  <TableCell align="right" sx={totalCell}>
                    {fmtHa2(row.track)}
                  </TableCell>
                  <TableCell sx={totalCell} />
                </>
              ) : (
                <>
                  {row.personSpan > 0 ? (
                    <TableCell rowSpan={row.personSpan} sx={groupCell}>
                      {row.person}
                    </TableCell>
                  ) : null}
                  {row.plateSpan > 0 ? (
                    <TableCell rowSpan={row.plateSpan} sx={{ verticalAlign: "top", whiteSpace: "nowrap" }}>
                      {row.plate}
                    </TableCell>
                  ) : null}
                  <TableCell align="right">{fmtHa2(row.ha)}</TableCell>
                  <TableCell align="right">{fmtHa2(row.track)}</TableCell>
                  <TableCell sx={{ whiteSpace: "nowrap" }}>{taskLink(row)}</TableCell>
                </>
              )}
            </TableRow>
          ))
        ) : (
          <TableRow>
            <TableCell colSpan={columns}>
              <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
                Ничего не найдено по выбранным колонкам.
              </Typography>
            </TableCell>
          </TableRow>
        )}
        {shown.table.length ? (
          <TableRow>
            <TableCell colSpan={columns} sx={{ ...totalCell, borderTop: 3 }}>
              Всего {fmtHa2(shown.ha)} га
              {shown.track != null ? ` · по треку ${fmtHa2(shown.track)} га` : ""}
            </TableCell>
          </TableRow>
        ) : null}
      </TableBody>
    </SliceFrame>
  );
}
