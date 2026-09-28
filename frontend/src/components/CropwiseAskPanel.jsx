import { useEffect, useState } from "react";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Checkbox,
  Chip,
  FormControlLabel,
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

import { askCropwise, fetchCropwiseGroups, queryCropwise } from "../api";

const POPPER_PROPS = { popper: { sx: { zIndex: 1600 } } };

function shiftDate(iso, days) {
  const d = new Date(`${iso}T12:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  d.setDate(d.getDate() + days);
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

export default function CropwiseAskPanel({ summaryDate, open }) {
  const today = summaryDate || new Date().toISOString().slice(0, 10);
  const [cookie, setCookie] = useState("");
  const [envCookie, setEnvCookie] = useState(false);
  const [dateFrom, setDateFrom] = useState(shiftDate(today, -6));
  const [dateTo, setDateTo] = useState(today);
  const [groupOptions, setGroupOptions] = useState([]);
  const [groups, setGroups] = useState([]);
  const [season, setSeason] = useState("");
  const [firm, setFirm] = useState("");
  const [crop, setCrop] = useState("");
  const [firmOptions, setFirmOptions] = useState([]);
  const [cropOptions, setCropOptions] = useState([]);
  const [question, setQuestion] = useState("");
  const [useLlm, setUseLlm] = useState(true);

  const [result, setResult] = useState(null);
  const [queryLoading, setQueryLoading] = useState(false);
  const [queryError, setQueryError] = useState("");
  const [answer, setAnswer] = useState(null);
  const [askLoading, setAskLoading] = useState(false);
  const [askError, setAskError] = useState("");

  useEffect(() => {
    if (!summaryDate) return;
    setDateTo(summaryDate);
    setDateFrom(shiftDate(summaryDate, -6));
  }, [summaryDate]);

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    fetchCropwiseGroups()
      .then((data) => {
        if (cancelled) return;
        setGroupOptions((data?.groups || []).map((g) => g.name));
        const saved = String(data?.env_cookie || "").trim();
        setEnvCookie(Boolean(saved));
        if (saved) setCookie((prev) => (prev.trim() ? prev : saved));
      })
      .catch((e) => {
        if (!cancelled) setQueryError(String(e.message || e));
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const cookieReady = Boolean(cookie.trim()) || envCookie;

  function payload() {
    return {
      cookie: cookie.trim(),
      dateFrom,
      dateTo,
      groups,
      firm,
      crop,
      season,
    };
  }

  async function handleQuery() {
    setQueryLoading(true);
    setQueryError("");
    setResult(null);
    setAnswer(null);
    try {
      const data = await queryCropwise(payload());
      setResult(data);
      setFirmOptions(data.firms || []);
      setCropOptions(data.crops || []);
    } catch (e) {
      setQueryError(String(e.message || e));
    } finally {
      setQueryLoading(false);
    }
  }

  async function handleAsk() {
    if (askLoading || !question.trim()) return;
    setAskLoading(true);
    setAskError("");
    try {
      const data = await askCropwise({ ...payload(), question, useLlm });
      setAnswer(data);
      setResult(data);
      setFirmOptions(data.firms || []);
      setCropOptions(data.crops || []);
    } catch (e) {
      setAskError(String(e.message || e));
    } finally {
      setAskLoading(false);
    }
  }

  return (
    <Stack spacing={1.5}>
      <Typography variant="body2" color="text.secondary">
        Запрос в Cropwise по cookie, без сверки со сводкой. В модель уходят задания выбранного
        периода и карточка каждого: исполнитель, техника, орудие, поле и расход топлива.
      </Typography>
      <TextField
        label="Cookie Cropwise"
        placeholder={envCookie ? "Cookie из .env" : "_cropio_ses=…; crossdomain_auth=…"}
        value={cookie}
        onChange={(e) => setCookie(e.target.value)}
        fullWidth
        size="small"
        helperText={
          envCookie
            ? "Подставлен из .env. Можно заменить; пустое поле снова берёт cookie сервера."
            : "В .env нет CROPWISE_SESSION_COOKIE — вставьте cookie вручную."
        }
      />
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5}>
        <TextField
          label="С"
          type="date"
          size="small"
          value={dateFrom}
          onChange={(e) => setDateFrom(e.target.value)}
          InputLabelProps={{ shrink: true }}
          fullWidth
          helperText="смена с 06:00"
        />
        <TextField
          label="По"
          type="date"
          size="small"
          value={dateTo}
          onChange={(e) => setDateTo(e.target.value)}
          InputLabelProps={{ shrink: true }}
          fullWidth
          helperText="день включительно"
        />
        <TextField
          label="Сезон"
          size="small"
          value={season}
          onChange={(e) => setSeason(e.target.value.replace(/[^\d]/g, "").slice(0, 4))}
          placeholder="2027"
          sx={{ minWidth: 110 }}
          helperText="пусто — из cookie"
        />
      </Stack>
      <Autocomplete
        multiple
        size="small"
        options={groupOptions}
        value={groups}
        onChange={(_, v) => setGroups(v)}
        slotProps={POPPER_PROPS}
        renderTags={(value, getTagProps) =>
          value.map((option, index) => {
            const { key, ...tagProps } = getTagProps({ index });
            return <Chip key={key} size="small" label={option} {...tagProps} />;
          })
        }
        renderInput={(params) => <TextField {...params} label="Агрооперации" />}
      />
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5}>
        <Autocomplete
          size="small"
          options={firmOptions}
          value={firm || null}
          onChange={(_, v) => setFirm(v || "")}
          slotProps={POPPER_PROPS}
          renderInput={(params) => <TextField {...params} label="Агрофирма" />}
          fullWidth
        />
        <Autocomplete
          size="small"
          options={cropOptions}
          value={crop || null}
          onChange={(_, v) => setCrop(v || "")}
          slotProps={POPPER_PROPS}
          renderInput={(params) => <TextField {...params} label="Культура" />}
          fullWidth
        />
      </Stack>
      <Button
        variant="outlined"
        onClick={handleQuery}
        disabled={queryLoading || askLoading || !cookieReady || !groups.length || !dateFrom || !dateTo}
      >
        {queryLoading ? "Загрузка из Cropwise…" : "Загрузить задания"}
      </Button>
      {queryError && <Alert severity="warning">{queryError}</Alert>}
      {result && (
        <Box>
          <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 0.5 }}>
            {result.from_time} — {result.to_time}
            {result.session ? `, сезон сессии ${result.session}` : ""}. Заданий {result.rows_total},{" "}
            {result.ha_total} га
            {result.truncated ? ` (показаны первые ${result.rows?.length || 0})` : ""}.
            {result.details_note ? ` ${result.details_note}` : ""}
          </Typography>
          {result.details_error && (
            <Alert severity="warning" sx={{ mb: 0.5 }}>
              {result.details_error}
            </Alert>
          )}
          <Box sx={{ maxHeight: 280, overflow: "auto", border: 1, borderColor: "divider", borderRadius: 1 }}>
            <Table size="small" stickyHeader sx={{ minWidth: 1100 }}>
              <TableHead>
                <TableRow>
                  <TableCell>Задание</TableCell>
                  <TableCell>Фирма</TableCell>
                  <TableCell>Исполнитель</TableCell>
                  <TableCell>Техника</TableCell>
                  <TableCell>Орудие</TableCell>
                  <TableCell>Поле</TableCell>
                  <TableCell>Культура</TableCell>
                  <TableCell align="right">Га</TableCell>
                  <TableCell>Топливо</TableCell>
                  <TableCell>Подпись</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {(result.rows || []).map((r) => (
                  <TableRow key={r.task_id} hover>
                    <TableCell>#{r.task_id}</TableCell>
                    <TableCell>{r.firm}</TableCell>
                    <TableCell>{r.driver}</TableCell>
                    <TableCell>{r.machine}</TableCell>
                    <TableCell>{r.implement}</TableCell>
                    <TableCell>{r.field}</TableCell>
                    <TableCell>{r.crop}</TableCell>
                    <TableCell align="right">{r.ha}</TableCell>
                    <TableCell>{r.fuel}</TableCell>
                    <TableCell>{r.label}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Box>
        </Box>
      )}
      <TextField
        label="Вопрос для DeepSeek"
        placeholder="Например: кто выполнял работы, на какой технике, на каких полях и с каким расходом топлива?"
        multiline
        minRows={5}
        maxRows={14}
        fullWidth
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            handleAsk();
          }
        }}
        helperText="Ctrl+Enter — отправить"
      />
      <Stack direction="row" spacing={1.5} alignItems="center">
        <Button
          variant="contained"
          onClick={handleAsk}
          disabled={askLoading || queryLoading || !cookieReady || !groups.length || !question.trim()}
        >
          {askLoading ? "Спрашиваю…" : "Спросить"}
        </Button>
        <FormControlLabel
          control={<Checkbox size="small" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} />}
          label="Использовать модель"
        />
      </Stack>
      {askError && <Alert severity="warning">{askError}</Alert>}
      {answer?.answer && (
        <Box sx={{ p: 1.5, border: 1, borderColor: "divider", borderRadius: 1 }}>
          <Typography variant="caption" color="text.secondary">
            {answer.llm_used ? `DeepSeek · ${answer.model || ""}` : "Без модели"}
            {answer.usage?.input_tokens ? ` · ${answer.usage.input_tokens}+${answer.usage.output_tokens || 0} ток.` : ""}
          </Typography>
          <Typography variant="body2" sx={{ whiteSpace: "pre-wrap", mt: 0.5 }}>
            {answer.answer}
          </Typography>
          {answer.error && (
            <Alert severity="warning" sx={{ mt: 1 }}>
              {answer.error}
            </Alert>
          )}
        </Box>
      )}
      <Typography variant="caption" color="text.secondary">
        Повторный вопрос по тому же окну берёт уже загруженный ответ Cropwise.{" "}
        <Link href="https://operations.cropwise.com/machines/reports/work" target="_blank" rel="noopener noreferrer">
          Отчёт в Cropwise
        </Link>
      </Typography>
    </Stack>
  );
}
