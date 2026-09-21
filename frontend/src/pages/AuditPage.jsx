import { useEffect, useState } from "react";
import { Alert, Box, Card, CardContent, Stack, Typography } from "@mui/material";

import { fetchAuditLogs } from "../api";

export default function AuditPage() {
  const [rows, setRows] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const data = await fetchAuditLogs();
        setRows(Array.isArray(data) ? data : []);
      } catch (e) {
        setError(String(e.message || e));
      }
    })();
  }, []);

  return (
    <Card className="hero-card" elevation={0}>
      <CardContent sx={{ p: 3 }}>
        <Typography variant="h6" sx={{ mb: 2 }}>
          Журнал аудита
        </Typography>
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
        <Stack spacing={1}>
          {rows.map((row) => (
            <Box key={row.id} className="summary-row" sx={{ p: 1.5, borderRadius: 2 }}>
              <Typography variant="body2" color="text.secondary">
                {new Date(row.created_at).toLocaleString("ru-RU")} · user #{row.user_id ?? "—"} · {row.action}
              </Typography>
              <Typography variant="caption" component="pre" sx={{ m: 0, whiteSpace: "pre-wrap" }}>
                {JSON.stringify(row.details, null, 2)}
              </Typography>
            </Box>
          ))}
        </Stack>
      </CardContent>
    </Card>
  );
}
