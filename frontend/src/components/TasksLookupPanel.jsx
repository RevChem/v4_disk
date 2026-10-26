import { useState } from "react";
import {
  Alert,
  Box,
  Button,
  Link,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from "@mui/material";
import OpenInNewRoundedIcon from "@mui/icons-material/OpenInNewRounded";

import { fetchMachineTask } from "../api";

export default function TasksLookupPanel() {
  const [taskId, setTaskId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [card, setCard] = useState(null);

  async function search() {
    const id = taskId.trim();
    if (!/^\d+$/.test(id)) {
      setError("Введите номер задания, например 463820");
      setCard(null);
      return;
    }
    setLoading(true);
    setError("");
    try {
      setCard(await fetchMachineTask(id));
    } catch (err) {
      setCard(null);
      setError(String(err.message || err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Stack spacing={2} sx={{ maxWidth: 920 }}>
      <Typography variant="body2" color="text.secondary">
        Номер задания Cropwise. Сразу открывается карточка и ссылка на страницу этого задания.
      </Typography>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1}>
        <TextField
          label="Номер задания"
          size="small"
          value={taskId}
          onChange={(event) => setTaskId(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void search();
          }}
          disabled={loading}
          sx={{ maxWidth: 280 }}
        />
        <Button variant="contained" onClick={() => void search()} disabled={loading}>
          {loading ? "Поиск…" : "Найти"}
        </Button>
      </Stack>
      {error ? <Alert severity="error">{error}</Alert> : null}
      {card ? (
        <Box>
          <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 1.5 }} flexWrap="wrap" useFlexGap>
            <Typography variant="h6" fontWeight={700}>
              {card.id}. {card.title}
            </Typography>
            {card.url ? (
              <Link href={card.url} target="_blank" rel="noopener noreferrer" sx={{ display: "inline-flex", alignItems: "center", gap: 0.5 }}>
                Открыть задание
                <OpenInNewRoundedIcon sx={{ fontSize: 16 }} />
              </Link>
            ) : null}
          </Stack>
          <Table size="small" sx={{ mb: 2 }}>
            <TableBody>
              {(card.facts || []).map((fact) => (
                <TableRow key={fact.label}>
                  <TableCell sx={{ width: 240, color: "text.secondary" }}>{fact.label}</TableCell>
                  <TableCell>{String(fact.value)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {card.fields?.length ? (
            <>
              <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
                Поля
              </Typography>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Поле</TableCell>
                    <TableCell align="right">Обработано, га</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {card.fields.map((field) => (
                    <TableRow key={field.id}>
                      <TableCell>{field.name}</TableCell>
                      <TableCell align="right">{field.covered_ha}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </>
          ) : (
            <Typography variant="body2" color="text.secondary">
              Поля в задании не указаны.
            </Typography>
          )}
        </Box>
      ) : null}
    </Stack>
  );
}
