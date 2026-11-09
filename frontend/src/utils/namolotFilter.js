/** Клиентский фильтр строк Namolot. Пустой список культур или агрофирм — без ограничения. */

export function normPlate(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[\s-]+/g, "");
}

export function normText(value) {
  return String(value || "")
    .toLocaleLowerCase("ru-RU")
    .replace(/ё/g, "е")
    .trim();
}

function rangeStart(dateFrom, timeFrom) {
  if (!dateFrom) return "";
  return `${dateFrom}T${timeFrom || "00:00"}`;
}

function rangeEnd(dateTo, timeTo) {
  if (!dateTo) return "";
  return `${dateTo}T${timeTo || "23:59"}`;
}

export function filterRows(rows, filters) {
  const list = Array.isArray(rows) ? rows : [];
  const firms = filters?.firms?.length ? new Set(filters.firms) : null;
  const crops = filters?.crops?.length ? new Set(filters.crops) : null;
  const plateQ = normPlate(filters?.plate);
  const nameQ = normText(filters?.surname);
  const crew = filters?.crew === "hired" || filters?.crew === "own" ? filters.crew : "";
  const start = rangeStart(filters?.dateFrom, filters?.timeFrom);
  const end = rangeEnd(filters?.dateTo, filters?.timeTo);

  return list.filter((row) => {
    if (firms && !firms.has(row.firm)) return false;
    if (crew && (row.crew === "hired" ? "hired" : "own") !== crew) return false;
    if (crops && !crops.has(row.crop)) return false;
    const stamp = String(row.at || "").slice(0, 16);
    if ((start || end) && !stamp) return false;
    if (start && stamp < start) return false;
    if (end && stamp > end) return false;
    if (plateQ && !normPlate(row.plate).includes(plateQ)) return false;
    if (nameQ && !normText(`${row.surname || ""} ${row.operator || ""}`).includes(nameQ)) return false;
    return true;
  });
}

function tonneSort(dir) {
  const sign = dir === "asc" ? 1 : -1;
  return (a, b) => sign * ((Number(a.tonnes) || 0) - (Number(b.tonnes) || 0));
}

export function rankRows(rows, mode, dir = "desc") {
  const list = Array.isArray(rows) ? rows : [];
  if (mode === "plates") return groupRank(list, (row) => row.plate || "без номера", dir);
  if (mode === "surnames") return groupRank(list, (row) => row.surname || row.operator || "без фамилии", dir);
  const sign = dir === "asc" ? 1 : -1;
  return [...list].sort((a, b) => {
    const byTonnes = (Number(a.tonnes) || 0) - (Number(b.tonnes) || 0);
    if (byTonnes) return sign * byTonnes;
    return sign * String(b.at || "").localeCompare(String(a.at || ""));
  });
}

export function sortRecordRows(rows, sortKey, dir) {
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    if (sortKey === "at") {
      return sign * String(a.at || "").localeCompare(String(b.at || ""));
    }
    const byTonnes = (Number(a.tonnes) || 0) - (Number(b.tonnes) || 0);
    if (byTonnes) return sign * byTonnes;
    return String(a.plate || "").localeCompare(String(b.plate || ""), "ru");
  });
}

function groupRank(rows, keyOf, dir) {
  const map = new Map();
  for (const row of rows) {
    const key = keyOf(row) || "—";
    let group = map.get(key);
    if (!group) {
      group = {
        key,
        tonnes: 0,
        records: 0,
        crops: new Set(),
        firms: new Set(),
        plates: new Set(),
        names: new Set(),
      };
      map.set(key, group);
    }
    group.tonnes += Number(row.tonnes) || 0;
    group.records += 1;
    if (row.crop) group.crops.add(row.crop);
    if (row.firm) group.firms.add(row.firm);
    if (row.plate) group.plates.add(row.plate);
    if (row.operator) group.names.add(row.operator);
  }
  return [...map.values()]
    .map((group) => ({
      key: group.key,
      tonnes: Math.round(group.tonnes * 1000) / 1000,
      records: group.records,
      crops: [...group.crops].sort((a, b) => a.localeCompare(b, "ru")),
      firms: [...group.firms].sort((a, b) => a.localeCompare(b, "ru")),
      plates: [...group.plates].sort((a, b) => a.localeCompare(b, "ru")),
      names: [...group.names].sort((a, b) => a.localeCompare(b, "ru")),
    }))
    .sort(tonneSort(dir));
}
