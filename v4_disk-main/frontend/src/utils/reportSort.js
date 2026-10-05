/** Сортировка id сводок по убыванию (24, 14, …, 1, 0.1). */
export function compareReportIdsDesc(a, b) {
  const sa = String(a);
  const sb = String(b);
  const na = parseFloat(sa);
  const nb = parseFloat(sb);
  if (Number.isFinite(na) && Number.isFinite(nb) && sa === String(na) && sb === String(nb)) {
    return nb - na;
  }
  return sb.localeCompare(sa, undefined, { numeric: true });
}

export function sortCatalogDesc(items) {
  return [...(items || [])].sort((a, b) => compareReportIdsDesc(a.id, b.id));
}

/** Сортировка id сводок по возрастанию (1, 2, …, 24). */
export function compareReportIdsAsc(a, b) {
  return compareReportIdsDesc(b, a);
}

export function sortCatalogAsc(items) {
  return [...(items || [])].sort((a, b) => compareReportIdsAsc(a.id, b.id));
}
