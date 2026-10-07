import { GlobalWorkerOptions, getDocument } from "pdfjs-dist/legacy/build/pdf";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.js?url";

if (!GlobalWorkerOptions.workerSrc) {
  GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
}

function sampleCellFillColor(ctx, left, top, width, height) {
  const x0 = Math.max(0, Math.floor(left));
  const y0 = Math.max(0, Math.floor(top));
  const w = Math.max(1, Math.ceil(width));
  const h = Math.max(1, Math.ceil(height));
  let data;
  try {
    data = ctx.getImageData(x0, y0, w, h).data;
  } catch {
    return "#ffffff";
  }
  const lumOf = (r, g, b) => (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  const buckets = new Map();
  const step = Math.max(1, Math.floor((w * h) / 1200));
  for (let i = 0; i < data.length; i += 4 * step) {
    const r = data[i];
    const g = data[i + 1];
    const b = data[i + 2];
    const lum = lumOf(r, g, b);
    if (lum < 0.18) continue;
    const chroma = Math.max(r, g, b) - Math.min(r, g, b);
    if (lum < 0.48 && chroma < 28) continue;
    const key = `${r >> 4},${g >> 4},${b >> 4}`;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push([r, g, b]);
  }
  if (!buckets.size) return "#ffffff";
  const ranked = [...buckets.entries()].sort((a, b) => b[1].length - a[1].length).slice(0, 4);
  let best = null;
  let bestScore = -1;
  for (const [, pixels] of ranked) {
    const ordered = [...pixels].sort((a, b) => lumOf(...a) - lumOf(...b));
    const cut = Math.floor(ordered.length * 0.35);
    const keep = ordered.slice(cut).length ? ordered.slice(cut) : ordered;
    const rr = keep.reduce((s, p) => s + p[0], 0) / keep.length;
    const gg = keep.reduce((s, p) => s + p[1], 0) / keep.length;
    const bb = keep.reduce((s, p) => s + p[2], 0) / keep.length;
    const lum = lumOf(rr, gg, bb);
    const score = keep.length * (1 + lum * 1.6);
    if (score > bestScore) {
      bestScore = score;
      best = [
        Math.min(255, Math.round(rr * 1.03 + 2)),
        Math.min(255, Math.round(gg * 1.03 + 2)),
        Math.min(255, Math.round(bb * 1.03 + 2))
      ];
    }
  }
  if (!best) return "#ffffff";
  const hex = (n) => n.toString(16).padStart(2, "0");
  return `#${hex(best[0])}${hex(best[1])}${hex(best[2])}`;
}

function contrastTextColor(fillHex) {
  const raw = String(fillHex || "#ffffff").replace("#", "");
  if (!/^[0-9a-fA-F]{6}$/.test(raw)) return "#111827";
  const r = parseInt(raw.slice(0, 2), 16);
  const g = parseInt(raw.slice(2, 4), 16);
  const b = parseInt(raw.slice(4, 6), 16);
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.55 ? "#111827" : "#ffffff";
}

export async function parsePdfCellsFromUrl(fileUrl) {
  const loadingTask = getDocument({
    url: fileUrl,
    withCredentials: true
  });
  const pdf = await loadingTask.promise;
  const pages = [];
  for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
    const page = await pdf.getPage(pageNumber);
    const viewport = page.getViewport({ scale: 1.2 });
    const canvas = document.createElement("canvas");
    canvas.width = Math.ceil(viewport.width);
    canvas.height = Math.ceil(viewport.height);
    const ctx = canvas.getContext("2d", { alpha: false });
    await page.render({
      canvasContext: ctx,
      viewport
    }).promise;
    const textContent = await page.getTextContent();
    const cells = [];
    for (const item of textContent.items || []) {
      const str = String(item?.str || "").trim();
      if (!str) continue;
      const transform = Array.isArray(item.transform) ? item.transform : [1, 0, 0, 1, 0, 0];
      const pdfX = Number(transform[4] || 0);
      const pdfY = Number(transform[5] || 0);
      const pdfWidth = Math.max(1, Number(item.width || 0));
      const pdfHeight = Math.max(
        1,
        Number(item.height || 0) || Math.abs(Number(transform[3] || 0)) || 8
      );
      const rect = viewport.convertToViewportRectangle([
        pdfX,
        pdfY,
        pdfX + pdfWidth,
        pdfY + pdfHeight
      ]);
      const left = Math.max(0, Math.min(rect[0], rect[2]));
      const top = Math.max(0, Math.min(rect[1], rect[3]));
      const width = Math.max(3, Math.abs(rect[2] - rect[0]));
      const height = Math.max(8, Math.abs(rect[3] - rect[1]));
      const fillColor = sampleCellFillColor(ctx, left, top, width, height);
      cells.push({
        cellKey: `${pageNumber}:${Math.round(left)}:${Math.round(top)}:${str.slice(0, 40)}`,
        cellText: str,
        page: pageNumber,
        leftPct: viewport.width > 0 ? left / viewport.width : 0,
        topPct: viewport.height > 0 ? top / viewport.height : 0,
        widthPct: viewport.width > 0 ? width / viewport.width : 0.01,
        heightPct: viewport.height > 0 ? height / viewport.height : 0.02,
        fillColor,
        textColor: contrastTextColor(fillColor)
      });
    }
    pages.push({
      page: pageNumber,
      width: viewport.width,
      height: viewport.height,
      imageDataUrl: canvas.toDataURL("image/png"),
      cells
    });
  }
  return pages;
}
