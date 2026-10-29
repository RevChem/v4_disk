import { useState } from "react";
import { Box, Dialog, DialogContent, DialogTitle, IconButton, Typography } from "@mui/material";
import ListAltRoundedIcon from "@mui/icons-material/ListAltRounded";

import { TaskText } from "./TaskText";

const CODE_RE = /сводка=(\S+)\s+([A-Z][A-Z0-9_]+):/;
const COUNT_RE = /(\d+)\s*шт/;

export function errorPlacesKey(text) {
  const match = String(text || "").match(CODE_RE);
  return match ? `${match[1]}:${match[2]}` : "";
}

/** Полный список строк. Если его ещё нет в файле — примеры из абзаца. */
export function paragraphPlaces(message, places) {
  if (Array.isArray(places) && places.length) return places;
  const raw = String(message || "");
  const cut = raw.indexOf(" | ");
  if (cut < 0) return [];
  return raw
    .slice(cut + 3)
    .split("; ")
    .map((part) => part.trim())
    .filter(Boolean);
}

/** Одно поле — одна строка: второе задание на том же поле чинится так же. */
export function uniqueByField(lines) {
  const seen = new Set();
  const out = [];
  for (const line of lines || []) {
    const match = String(line).match(/поле=(\d+)/);
    const key = match ? match[1] : `row:${line}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(line);
  }
  return out;
}

/** Абзац ошибки и значок, который открывает все задания с этой ошибкой. */
export function ErrorParagraph({ message, places, placesByKey, urls }) {
  const listed = paragraphPlaces(message, places || placesByKey?.[errorPlacesKey(message)]);
  const all = uniqueByField(listed);
  const raw = String(message || "");
  const cut = raw.indexOf(" | ");
  const summary = cut < 0 ? raw : raw.slice(0, cut);
  const countMatch = summary.match(COUNT_RE);
  const stated = countMatch ? Number(countMatch[1]) : all.length;
  const complete = listed.length >= stated;
  const count = complete ? all.length : stated;
  const noTask = /нет задания|без задания/i.test(summary);
  const summaryText = complete ? summary.replace(COUNT_RE, `${count} шт`) : summary;
  const preview = all.slice(0, 3);
  const shown = preview.length ? `${summaryText} | ${preview.join("; ")}` : summaryText;
  const [open, setOpen] = useState(false);

  return (
    <>
      {noTask ? shown : <TaskText text={shown} urls={urls} />}
      {!noTask && all.length > 0 ? (
        <IconButton
          size="small"
          aria-label="Все задания с этой ошибкой"
          title={`Все задания с этой ошибкой (${count})`}
          onClick={() => setOpen(true)}
          sx={{ p: 0.25, ml: 0.5, verticalAlign: "text-bottom" }}
        >
          <ListAltRoundedIcon sx={{ fontSize: 18 }} />
        </IconButton>
      ) : null}
      <Dialog open={open} onClose={() => setOpen(false)} fullWidth maxWidth="md">
        <DialogTitle>
          Задания с ошибкой · {count} шт
        </DialogTitle>
        <DialogContent dividers>
          <Box sx={{ display: "flex", flexDirection: "column", gap: 0.5 }}>
            {all.map((line, index) => (
              <Typography
                key={`${index}-${line.slice(0, 40)}`}
                variant="body2"
                sx={{ fontFamily: "monospace", fontSize: "0.8rem" }}
              >
                <TaskText text={line} urls={urls} />
              </Typography>
            ))}
          </Box>
          {count > all.length ? (
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>
              В этом запуске сохранены {all.length} из {count}. Полный список записывается при следующей генерации сводки.
            </Typography>
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  );
}
