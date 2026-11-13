/**
 * Какая сохранённая приписка относится к ячейке.
 * Ключ ячейки включает текст и координаты на момент сохранения. После сдвига
 * таблицы (сводка 13) ключ уже не совпадает, хотя звёздочка стоит на этой цифре.
 */

export function findCommentForCell(cell, rows) {
  if (!cell) return null;
  const list = Array.isArray(rows) ? rows : [];
  const byKey = list.find((row) => String(row.cell_key) === String(cell.cellKey));
  if (byKey) return byKey;
  const page = Number(cell.page);
  const anchorX = Number(cell.leftPct) + Number(cell.widthPct) * 0.96;
  const anchorY = Number(cell.topPct) + Number(cell.heightPct) * 0.52;
  let best = null;
  let bestDist = Infinity;
  for (const row of list) {
    if (Number(row.page) !== page) continue;
    const x = Number(row.x);
    const y = Number(row.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    const dx = Math.abs(x - anchorX);
    const dy = Math.abs(y - anchorY);
    const tolX = Math.max(0.04, Number(cell.widthPct) * 1.5);
    const tolY = Math.max(0.03, Number(cell.heightPct) * 2);
    if (dx > tolX || dy > tolY) continue;
    const dist = dx + dy;
    if (dist < bestDist) {
      best = row;
      bestDist = dist;
    }
  }
  return best;
}

/** Приписки, чей текст напечатан в этом фрагменте (строка «* комментарий» под таблицей). */
export function commentsPrintedInCell(cell, rows) {
  if (!cell) return [];
  const raw = String(cell.cellText || "").trim();
  if (!raw.includes("*")) return [];
  const stripped = raw.replace(/^\*+\s*/, "").trim();
  if (!stripped) return [];
  const found = [];
  const seen = new Set();
  for (const row of Array.isArray(rows) ? rows : []) {
    if (Number(row.page) !== Number(cell.page)) continue;
    const note = String(row.comment || "").trim();
    if (!note || (stripped !== note && !stripped.startsWith(`${note} `))) continue;
    const id = String(row.id || "");
    if (seen.has(id)) continue;
    seen.add(id);
    found.push(row);
  }
  return found;
}
