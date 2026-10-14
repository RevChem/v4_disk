import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import { fetchPlansCropwiseSeason, formatError } from "../api";

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

export default function CropwiseSeasonHa({ groupName, summaryDate, disabled }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [data, setData] = useState(null);

  useEffect(() => {
    setData(null);
    setError("");
  }, [groupName, summaryDate]);

  async function load() {
    if (!groupName) return;
    setLoading(true);
    setError("");
    try {
      setData(await fetchPlansCropwiseSeason(groupName, summaryDate));
    } catch (e) {
      setData(null);
      setError(formatError(e, "Не удалось запросить Cropwise"));
    } finally {
      setLoading(false);
    }
  }

  const seasons = data?.seasons || [];
  const firms = [];
  for (const season of seasons) {
    for (const row of season.by_firm || []) {
      if (row.firm && !firms.includes(row.firm)) firms.push(row.firm);
    }
  }
  const column = seasons.find((season) => season.column)?.column;

  return (
    <Stack spacing={1}>
      <Stack direction="row" spacing={1} alignItems="center" useFlexGap flexWrap="wrap">
        <Button
          variant="outlined"
          size="small"
          disabled={disabled || loading || !groupName}
          onClick={load}
        >
          {loading ? <CircularProgress size={16} sx={{ mr: 1 }} /> : null}
          Гектары Cropwise
        </Button>
        <Typography variant="caption" color="text.secondary">
          Живой work.json по сохранённым сезонам этой операции. Сумма проходов, не уникальная площадь.
        </Typography>
      </Stack>
      {error ? <Alert severity="error">{error}</Alert> : null}
      {data ? (
        <Stack spacing={0.5}>
          <Typography variant="caption" color="text.secondary">
            {data.group_name}: сезоны {(data.years || []).join(" и ") || "—"}, окно{" "}
            {formatStamp(data.window_from)} — {formatStamp(data.window_to)}
            {column ? `, колонка «${column}»` : ""}
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
      ) : null}
    </Stack>
  );
}
