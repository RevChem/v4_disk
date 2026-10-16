/**
 * Чистая логика редактора правок ячеек (без React): черновик, пакет для сохранения,
 * навигация по ячейкам и масштаб. Вынесена отдельно, чтобы её можно было тестировать.
 *
 * Черновик: { [cellKey]: { cell, replacement, editId } }
 *  - cell        — ячейка страницы (cellText уже исходный, из сохранённой правки, если она была);
 *  - replacement — новое значение;
 *  - editId      — id уже сохранённой правки для этой ячейки ("" — правки ещё нет).
 */

/** Очень слабый зелёный для подсветки изменённых ячеек. */
export const CHANGED_TINT = "rgba(34, 197, 94, 0.14)";

export const ZOOM_MIN = 0.5;
export const ZOOM_MAX = 3;
export const ZOOM_STEPS = [0.5, 0.65, 0.8, 1, 1.25, 1.5, 2, 2.5, 3];

const norm = (value) => String(value ?? "").trim();

/** Правка, относящаяся к ячейке: по ключу или по тому же месту на странице
 * (после замены в PDF у ячейки уже другой текст и другой ключ). */
export function findEditForCell(edits, cell) {
  if (!cell) return null;
  const list = Array.isArray(edits) ? edits : [];
  const byKey = list.find((row) => String(row.cell_key) === String(cell.cellKey));
  if (byKey) return byKey;
  const cx = Number(cell.leftPct) + Number(cell.widthPct) / 2;
  const cy = Number(cell.topPct) + Number(cell.heightPct) / 2;
  return (
    list.find((row) => {
      if (Number(row.page) !== Number(cell.page)) return false;
      const l = Number(row.left);
      const t = Number(row.top);
      const w = Number(row.width);
      const h = Number(row.height);
      const tx = Math.max(0.003, w * 0.5);
      const ty = Math.max(0.002, h * 0.5);
      return cx >= l - tx && cx <= l + w + tx && cy >= t - ty && cy <= t + h + ty;
    }) || null
  );
}

/** Ячейка страницы, соответствующая сохранённой правке (обратный поиск). */
export function findCellForEdit(pages, row) {
  if (!row) return null;
  const pageInfo = (pages || []).find((p) => Number(p.page) === Number(row.page));
  const cells = pageInfo?.cells || [];
  const byKey = cells.find((cell) => String(cell.cellKey) === String(row.cell_key));
  const matched = byKey || cells.find((cell) => findEditForCell([row], cell) === row);
  if (matched) {
    return {
      ...matched,
      fillColor: matched.fillColor || String(row.fill_color || ""),
      textColor: matched.textColor || String(row.text_color || "")
    };
  }
  return {
    cellKey: String(row.cell_key || ""),
    cellText: String(row.cell_text || ""),
    page: Number(row.page || 1),
    leftPct: Number(row.left || 0),
    topPct: Number(row.top || 0),
    widthPct: Number(row.width || 0.01),
    heightPct: Number(row.height || 0.02),
    fillColor: String(row.fill_color || ""),
    textColor: String(row.text_color || "")
  };
}

/** Исходное значение ячейки (до любых правок). */
export function originalText(cell, existing) {
  return norm(existing?.cell_text || cell?.cellText);
}

/** Значение ячейки в данный момент (с учётом черновика). */
export function currentValue(cell, existing, draftEntry) {
  if (draftEntry) return norm(draftEntry.replacement);
  if (existing) return norm(existing.replacement);
  return norm(cell?.cellText);
}

/** Изменена ли ячейка относительно исходного документа (сохранённо или в черновике). */
export function isCellChanged(cell, existing, draftEntry) {
  if (!existing && !draftEntry) return false;
  return currentValue(cell, existing, draftEntry) !== originalText(cell, existing);
}

/**
 * Записать значение ячейки в черновик. Возвращает НОВЫЙ объект черновика.
 * Если значение совпало с уже сохранённым — записи в черновике нет (нечего сохранять).
 */
export function applyDraftValue(draft, edits, cell, text) {
  const value = norm(text);
  const existing = findEditForCell(edits, cell);
  const saved = norm(existing ? existing.replacement : cell.cellText);
  let key = String(cell.cellKey);
  if (existing) {
    // Одна сохранённая правка — одна запись черновика, даже если ячейку открыли с другим ключом.
    for (const [draftKey, entry] of Object.entries(draft || {})) {
      if (entry.editId && String(entry.editId) === String(existing.id)) {
        key = draftKey;
        break;
      }
    }
  }
  const next = { ...draft };
  if (value === saved) {
    delete next[key];
    return next;
  }
  next[key] = {
    cell: existing ? { ...cell, cellText: existing.cell_text || cell.cellText } : cell,
    replacement: value,
    editId: existing ? String(existing.id) : ""
  };
  return next;
}

export function draftCount(draft) {
  return Object.keys(draft || {}).length;
}

/**
 * Пакет для сохранения. Если в черновике для уже сохранённой правки стоит исходное
 * значение — это отмена правки, её нужно удалить, а не сохранять.
 */
export function buildBatchPayload(draft, edits) {
  const upserts = [];
  const deleteIds = [];
  for (const entry of Object.values(draft || {})) {
    const cell = entry.cell;
    const existing = entry.editId
      ? (edits || []).find((row) => String(row.id) === String(entry.editId))
      : findEditForCell(edits, cell);
    if (existing && norm(entry.replacement) === norm(existing.cell_text)) {
      if (!deleteIds.includes(String(existing.id))) deleteIds.push(String(existing.id));
      continue;
    }
    upserts.push({
      page: cell.page,
      left: cell.leftPct,
      top: cell.topPct,
      width: cell.widthPct,
      height: cell.heightPct,
      cellKey: cell.cellKey,
      cellText: cell.cellText,
      replacement: norm(entry.replacement),
      fillColor: cell.fillColor || "",
      textColor: cell.textColor || ""
    });
  }
  return { edits: upserts, deleteIds };
}

/**
 * Строки списка правок: сохранённые + новые из черновика.
 * status: saved | modified (изменена после сохранения) | revert (будет удалена) | new.
 */
export function mergeEditRows(edits, draft) {
  const entries = Object.entries(draft || {});
  const draftByEditId = new Map();
  for (const [key, entry] of entries) {
    if (entry.editId) draftByEditId.set(String(entry.editId), { key, entry });
  }
  const rows = [];
  for (const edit of edits || []) {
    const found = draftByEditId.get(String(edit.id));
    if (!found) {
      rows.push({
        id: `e:${edit.id}`,
        status: "saved",
        page: Number(edit.page),
        top: Number(edit.top),
        left: Number(edit.left),
        from: String(edit.cell_text || ""),
        to: String(edit.replacement || ""),
        edit,
        draftKey: ""
      });
      continue;
    }
    const revert = norm(found.entry.replacement) === norm(edit.cell_text);
    rows.push({
      id: `e:${edit.id}`,
      status: revert ? "revert" : "modified",
      page: Number(edit.page),
      top: Number(edit.top),
      left: Number(edit.left),
      from: String(edit.cell_text || ""),
      to: String(found.entry.replacement || ""),
      edit,
      draftKey: found.key
    });
  }
  const knownIds = new Set((edits || []).map((edit) => String(edit.id)));
  for (const [key, entry] of entries) {
    if (entry.editId && knownIds.has(String(entry.editId))) continue;
    rows.push({
      id: `d:${key}`,
      status: "new",
      page: Number(entry.cell.page),
      top: Number(entry.cell.topPct),
      left: Number(entry.cell.leftPct),
      from: String(entry.cell.cellText || ""),
      to: String(entry.replacement || ""),
      edit: null,
      draftKey: key
    });
  }
  rows.sort((a, b) => a.page - b.page || a.top - b.top || a.left - b.left);
  return rows;
}

/** Соседняя ячейка в порядке чтения (по строкам, слева направо), в т.ч. на другой странице. */
export function readingNeighbor(pages, cell, dir) {
  const flat = [];
  for (const page of pages || []) {
    const sorted = [...(page.cells || [])].sort(
      (a, b) => Math.round(a.topPct * 250) - Math.round(b.topPct * 250) || a.leftPct - b.leftPct
    );
    flat.push(...sorted);
  }
  const idx = flat.findIndex((c) => String(c.cellKey) === String(cell.cellKey));
  if (idx < 0) return null;
  return flat[idx + dir] || null;
}

/** Ячейка строго под (dir = 1) или над (dir = -1) текущей в том же столбце — как Enter в Excel. */
export function verticalNeighbor(pages, cell, dir) {
  const pageInfo = (pages || []).find((p) => Number(p.page) === Number(cell.page));
  if (!pageInfo) return null;
  const left = Number(cell.leftPct);
  const right = left + Number(cell.widthPct);
  const top = Number(cell.topPct);
  const half = Number(cell.heightPct) / 2;
  let best = null;
  let bestScore = Infinity;
  for (const other of pageInfo.cells || []) {
    if (String(other.cellKey) === String(cell.cellKey)) continue;
    const dy = (Number(other.topPct) - top) * dir;
    if (dy <= half) continue; // не на той же строке и не в обратную сторону
    const oLeft = Number(other.leftPct);
    const oRight = oLeft + Number(other.widthPct);
    const overlap = Math.min(right, oRight) - Math.max(left, oLeft);
    const minWidth = Math.min(right - left, oRight - oLeft);
    if (overlap < Math.max(0.0005, minWidth * 0.4)) continue; // другой столбец
    const score = dy + Math.abs(oLeft + (oRight - oLeft) / 2 - (left + (right - left) / 2)) * 0.5;
    if (score < bestScore) {
      best = other;
      bestScore = score;
    }
  }
  return best;
}

export function clampZoom(value) {
  const v = Number(value);
  if (!Number.isFinite(v)) return 1;
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(v * 1000) / 1000));
}

/** Следующий/предыдущий шаг масштаба по кнопкам +/−. */
export function stepZoom(current, dir) {
  const z = clampZoom(current);
  if (dir > 0) return ZOOM_STEPS.find((s) => s > z + 0.001) ?? ZOOM_MAX;
  const smaller = ZOOM_STEPS.filter((s) => s < z - 0.001);
  return smaller.length ? smaller[smaller.length - 1] : ZOOM_MIN;
}

/** Размер шрифта поля/черновика в cqw (1% ширины контейнера страницы) для ячейки. */
export function cellFontCqw(cell, pageAspect) {
  const aspect = Number(pageAspect) > 0 ? Number(pageAspect) : 1.414;
  return Number(cell.heightPct) * aspect * 100;
}
