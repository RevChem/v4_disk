import { useEffect, useMemo, useState } from "react";
import { Box, Link, Table, TableBody, TableCell, TableHead, TablePagination, TableRow, Tooltip, Typography } from "@mui/material";

import { pageFieldTable, pageWorkerTable } from "../utils/taskSlice";

function fmtHa2(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
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

function SliceChrome({ caption, count, page, pageSize, onPage, onPageSize, pageLabel, children, empty }) {
  if (!count) {
    return empty;
  }
  return (
    <>
      <Typography variant="body2">{caption}</Typography>
      <Box sx={{ flex: 1, minHeight: 0, overflow: "auto" }}>
        <Table size="small" stickyHeader>
          {children}
        </Table>
      </Box>
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
    </>
  );
}

export function WorkerSliceTable({ rows, showFirm, note, empty }) {
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(25);
  useEffect(() => {
    setPage(0);
  }, [rows, showFirm]);
  const view = useMemo(() => pageWorkerTable(rows, page, pageSize), [rows, page, pageSize]);
  const safePage = Math.min(page, Math.max(0, Math.ceil(view.count / pageSize) - 1));
  const shown = safePage === page ? view : pageWorkerTable(rows, safePage, pageSize);
  const columns = (showFirm ? 1 : 0) + 6;
  const caption = `Фамилий ${shown.count.toLocaleString("ru-RU")} · ${fmtHa2(shown.ha)} га${
    shown.track != null ? ` · по треку ${fmtHa2(shown.track)} га` : ""
  }${note ? `. ${note}` : ""}`;

  return (
    <SliceChrome
      caption={caption}
      count={shown.count}
      page={safePage}
      pageSize={pageSize}
      onPage={setPage}
      onPageSize={setPageSize}
      pageLabel="Фамилий"
      empty={empty}
    >
      <TableHead>
        <TableRow>
          {showFirm ? <TableCell>Агрофирма</TableCell> : null}
          <TableCell>Фамилия</TableCell>
          <TableCell>Машина</TableCell>
          <TableCell>Поле</TableCell>
          <TableCell align="right">Площадь, га</TableCell>
          <TableCell align="right">
            <Tooltip title="Площадь по треку: covered_area_by_track задания, как в отчёте «Обработанная площадь поля»">
              <span>Площадь по треку, га</span>
            </Tooltip>
          </TableCell>
          <TableCell>Задание</TableCell>
        </TableRow>
      </TableHead>
      <TableBody>
        {shown.table.map((row, index) => (
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
        ))}
        {shown.table.length ? (
          <TableRow>
            <TableCell colSpan={columns} sx={{ ...totalCell, borderTop: 3 }}>
              Всего {fmtHa2(shown.ha)} га
              {shown.track != null ? ` · по треку ${fmtHa2(shown.track)} га` : ""}
            </TableCell>
          </TableRow>
        ) : null}
      </TableBody>
    </SliceChrome>
  );
}

export function FieldSliceTable({ rows, showFirm, note, empty }) {
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(25);
  useEffect(() => {
    setPage(0);
  }, [rows, showFirm]);
  const view = useMemo(() => pageFieldTable(rows, page, pageSize), [rows, page, pageSize]);
  const safePage = Math.min(page, Math.max(0, Math.ceil(view.count / pageSize) - 1));
  const shown = safePage === page ? view : pageFieldTable(rows, safePage, pageSize);
  const columns = (showFirm ? 1 : 0) + 6;
  const caption = `Полей ${shown.count.toLocaleString("ru-RU")} · ${fmtHa2(shown.ha)} га${
    shown.track != null ? ` · по треку ${fmtHa2(shown.track)} га` : ""
  }${note ? `. ${note}` : ""}`;

  return (
    <SliceChrome
      caption={caption}
      count={shown.count}
      page={safePage}
      pageSize={pageSize}
      onPage={setPage}
      onPageSize={setPageSize}
      pageLabel="Полей"
      empty={empty}
    >
      <TableHead>
        <TableRow>
          {showFirm ? <TableCell>Агрофирма</TableCell> : null}
          <TableCell>Поле</TableCell>
          <TableCell>Фамилия</TableCell>
          <TableCell>Машина</TableCell>
          <TableCell align="right">Площадь, га</TableCell>
          <TableCell align="right">
            <Tooltip title="Площадь по треку: covered_area_by_track задания, как в отчёте «Обработанная площадь поля»">
              <span>Площадь по треку, га</span>
            </Tooltip>
          </TableCell>
          <TableCell>Задание</TableCell>
        </TableRow>
      </TableHead>
      <TableBody>
        {shown.table.map((row, index) => (
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
        ))}
        {shown.table.length ? (
          <TableRow>
            <TableCell colSpan={columns} sx={{ ...totalCell, borderTop: 3 }}>
              Всего {fmtHa2(shown.ha)} га
              {shown.track != null ? ` · по треку ${fmtHa2(shown.track)} га` : ""}
            </TableCell>
          </TableRow>
        ) : null}
      </TableBody>
    </SliceChrome>
  );
}
