import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Box, Button, ButtonBase, Stack, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import ArrowBackRoundedIcon from "@mui/icons-material/ArrowBackRounded";

import { fetchAnalytics } from "../../api";
import { CHECK_FOLDERS, findCheck } from "./catalog";
import HubResult from "./HubResult";

const errText = (e) => {
  const d = e?.response?.data?.detail;
  return typeof d === "string" ? d : e?.message || "Не удалось загрузить данные";
};

function FolderCard({ folder, onOpen }) {
  const Icon = folder.icon;
  return (
    <ButtonBase
      onClick={() => onOpen(folder.id)}
      sx={(theme) => ({
        display: "flex",
        alignItems: "center",
        justifyContent: "flex-start",
        gap: 1.5,
        textAlign: "left",
        p: 1.75,
        borderRadius: 2,
        border: 1,
        borderColor: "divider",
        "&:hover": {
          borderColor: `${folder.color}.main`,
          bgcolor: alpha(theme.palette[folder.color]?.main || theme.palette.primary.main, 0.06),
        },
      })}
    >
      <Box
        sx={(theme) => ({
          display: "grid",
          placeItems: "center",
          width: 40,
          height: 40,
          flexShrink: 0,
          borderRadius: 1.5,
          color: `${folder.color}.main`,
          bgcolor: alpha(theme.palette[folder.color]?.main || theme.palette.primary.main, 0.12),
        })}
      >
        <Icon />
      </Box>
      <Box sx={{ minWidth: 0 }}>
        <Typography variant="subtitle2" fontWeight={700}>
          {folder.title}
        </Typography>
        <Typography variant="caption" color="text.secondary" noWrap>
          {folder.tagline} · {folder.items.length}
        </Typography>
      </Box>
    </ButtonBase>
  );
}

function CheckCard({ item, onOpen }) {
  const Icon = item.icon;
  return (
    <ButtonBase
      onClick={() => onOpen(item.id)}
      sx={{
        display: "flex",
        alignItems: "flex-start",
        gap: 1.5,
        textAlign: "left",
        p: 1.75,
        borderRadius: 2,
        border: 1,
        borderColor: "divider",
        "&:hover": { borderColor: "primary.main" },
      }}
    >
      <Icon color="primary" sx={{ mt: 0.25 }} />
      <Box sx={{ minWidth: 0 }}>
        <Typography variant="subtitle2" fontWeight={700}>
          {item.title}
        </Typography>
        <Typography variant="body2" color="text.secondary">
          {item.desc}
        </Typography>
      </Box>
    </ButtonBase>
  );
}

export default function ChecksPanel({ active = true, summaryDate = "", onOpenDriver }) {
  const [folderId, setFolderId] = useState(null);
  const [itemId, setItemId] = useState(null);
  const [stack, setStack] = useState([]);
  const [resultKey, setResultKey] = useState(0);
  const [groups, setGroups] = useState([]);
  const [groupsErr, setGroupsErr] = useState("");
  const savedFilters = useRef({});
  const optionsCache = useRef({});

  useEffect(() => {
    if (!active || groups.length) return undefined;
    let alive = true;
    fetchAnalytics("overview")
      .then((data) => {
        if (!alive) return;
        setGroups(Array.isArray(data?.groups) ? data.groups : []);
      })
      .catch((e) => alive && setGroupsErr(errText(e)));
    return () => {
      alive = false;
    };
  }, [active, groups.length]);

  const folder = CHECK_FOLDERS.find((f) => f.id === folderId) || null;
  const item = useMemo(() => (itemId ? findCheck(itemId) : null), [itemId]);

  function openFolder(id) {
    setStack([]);
    setFolderId(id);
    setItemId(null);
  }

  function openCheck(id, extra) {
    if (extra) {
      savedFilters.current[id] = extra;
      setResultKey((n) => n + 1);
    }
    setItemId(id);
    const next = findCheck(id);
    if (next) setFolderId(next.folderId);
  }

  function drill(kind, row, data) {
    const from = data.date_from;
    const to = data.date_to;
    if (kind === "driver") {
      onOpenDriver?.({
        dateFrom: from,
        dateTo: to,
        person: { id: row.driver_id, name: row.driver },
      });
      return;
    }
    const target = kind === "machine" ? "machine_work" : "field_history";
    const base = { group: "", machine: null, field: null };
    const saved =
      kind === "machine"
        ? { ...base, from, to, machine: { id: row.machine_id, label: row.machine } }
        : { ...base, from: `${String(to).slice(0, 4)}-01-01`, to, field: { id: row.field_id, label: row.field } };
    setStack((prev) => [...prev, { folderId, itemId }]);
    openCheck(target, saved);
  }

  function goBack() {
    const prev = stack[stack.length - 1];
    if (prev) {
      setStack((s) => s.slice(0, -1));
      setFolderId(prev.folderId);
      setItemId(prev.itemId);
      return;
    }
    if (itemId) {
      setItemId(null);
      return;
    }
    setFolderId(null);
  }

  const backLabel = stack.length
    ? "Назад"
    : item
      ? folder?.title || "К списку"
      : folder
        ? "Все проверки"
        : "";

  return (
    <Stack spacing={2}>
      {(folder || item) && (
        <Stack direction="row" spacing={1} alignItems="center">
          <Button size="small" startIcon={<ArrowBackRoundedIcon />} onClick={goBack}>
            {backLabel}
          </Button>
          <Typography variant="body2" color="text.secondary">
            {folder?.title}
            {item ? ` · ${item.title}` : ""}
          </Typography>
        </Stack>
      )}

      {groupsErr && !item && <Alert severity="warning">{groupsErr}</Alert>}

      {!folder && (
        <Box
          sx={{
            display: "grid",
            gap: 1.25,
            gridTemplateColumns: { xs: "1fr", sm: "repeat(2, minmax(0, 1fr))", lg: "repeat(3, minmax(0, 1fr))" },
          }}
        >
          {CHECK_FOLDERS.map((f) => (
            <FolderCard key={f.id} folder={f} onOpen={openFolder} />
          ))}
        </Box>
      )}

      {folder && !item && (
        <Stack spacing={1.25}>
          {folder.items.map((check) => (
            <CheckCard key={check.id} item={check} onOpen={openCheck} />
          ))}
        </Stack>
      )}

      {item && (
        <HubResult
          key={`${item.id}-${resultKey}`}
          item={item}
          anchorDate={summaryDate || undefined}
          groups={groups}
          saved={savedFilters.current[item.id]}
          onSave={(draft) => {
            savedFilters.current[item.id] = draft;
          }}
          optionsCache={optionsCache}
          onDrill={drill}
        />
      )}
    </Stack>
  );
}
