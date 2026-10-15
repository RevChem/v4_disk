import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import { fetchCropwiseSeasons, formatError } from "../api";

function formatHa(value) {
  if (value == null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatStamp(iso) {
  const text = String(iso || "");
  const [day, time] = text.split("T");
  if (!day) return text;
  const [y, m, d] = day.split("-");
  return time ? `${d}.${m}.${y} ${time.slice(0, 5)}` : `${d}.${m}.${y}`;
}

function GroupTable({ group }) {
  const seasons = group.seasons || [];
  const firms = [];
  for (const season of seasons) {
    for (const row of season.by_firm || []) {
      if (row.firm && !firms.includes(row.firm)) firms.push(row.firm);
    }
  }
  const column = seasons.find((season) => season.column)?.column;
  return (
    <Stack spacing={0.5}>
      <Typography variant="subtitle2">{group.group_name}</Typography>
      <Typography variant="caption" color="text.secondary">
        Сезоны {(group.years || []).join(" и ") || "—"}, окно {formatStamp(group.window_from)} —{" "}
        {formatStamp(group.window_to)}
        {column ? `, «${column}»` : ""}
      </Typography>
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Агрофирма</TableCell>
            {seasons.map((season) => (
              <TableCell key={season.year} align="right">
                {season.year}
              </TableCell>
            ))}
          </TableRow>
        </TableHead>
        <TableBody>
          {firms.map((firm) => (
            <TableRow key={firm}>
              <TableCell>{firm}</TableCell>
              {seasons.map((season) => {
                const row = (season.by_firm || []).find((item) => item.firm === firm);
                return (
                  <TableCell key={season.year} align="right">
                    {formatHa(row?.ha)}
                  </TableCell>
                );
              })}
            </TableRow>
          ))}
          <TableRow>
            <TableCell sx={{ fontWeight: 700 }}>Всего</TableCell>
            {seasons.map((season) => (
              <TableCell key={season.year} align="right" sx={{ fontWeight: 700 }}>
                {season.status === "loaded" ? formatHa(season.total_ha) : season.note || "—"}
              </TableCell>
            ))}
          </TableRow>
        </TableBody>
      </Table>
    </Stack>
  );
}

export default function CropwiseSeasonDialog({ open, onClose, summaryDate }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [data, setData] = useState(null);

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    setLoading(true);
    setError("");
    setData(null);
    fetchCropwiseSeasons(summaryDate)
      .then((payload) => {
        if (!cancelled) setData(payload);
      })
      .catch((e) => {
        if (!cancelled) setError(formatError(e, "Не удалось посчитать сезоны Cropwise"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, summaryDate]);

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle>Cropwise сезон</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ pt: 0.5 }}>
          <Typography variant="body2" color="text.secondary">
            Живой work.json по сезонам из редактора планов. Число — сумма проходов, не уникальная площадь поля.
          </Typography>
          {loading ? (
            <Stack direction="row" spacing={1} alignItems="center">
              <CircularProgress size={18} />
              <Typography variant="body2">Считаем сезоны…</Typography>
            </Stack>
          ) : null}
          {error ? <Alert severity="error">{error}</Alert> : null}
          {(data?.groups || []).map((group) => (
            <GroupTable key={group.group_name} group={group} />
          ))}
        </Stack>
      </DialogContent>
    </Dialog>
  );
}
