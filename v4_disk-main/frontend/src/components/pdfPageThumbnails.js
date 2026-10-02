import { GlobalWorkerOptions, getDocument } from "pdfjs-dist/legacy/build/pdf";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.js?url";

if (!GlobalWorkerOptions.workerSrc) {
  GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
}

const AGROFIRM_MARKERS = [
  "АФ Чистопольская",
  "АгроРаздолье",
  "Аксу-Агро",
  "НурлатАгро",
  "Дрожжаное Рассвет Агро",
  "Актай",
  "Лениногорская Птицефабрика"
];

function normalizeText(text) {
  return String(text || "")
    .replace(/\s+/g, " ")
    .trim();
}

function extractLabelFromFullText(rawText) {
  const text = normalizeText(rawText);
  const lower = text.toLowerCase();
  if (!text) return "";

  if (lower.includes("общий намолот")) return "Общий намолот";
  if (lower.includes("сводка овощ")) return "Сводка овощи";
  if (lower.includes("общая уборка овощ")) return "Овощи (уборка)";
  if (lower.includes("посев — сводно") || lower.includes("сводно по всем агрофирм")) {
    return "Посев — итого (все АФ)";
  }
  if (lower.includes("выработка техники") && lower.includes("4.0") && lower.includes("посев")) {
    return "Выраб. — 4.0 Посев";
  }
  if (lower.includes("сводка по культурам")) return "Сводка по культурам";
  if (lower.includes("свод по агрофирмам")) return "Свод по агрофирмам";
  if (lower.includes("свод по удобрениям")) return "Свод по удобрениям (ХК)";

  const yarsevMatch = text.match(/яр[с]?ев\s*блок\s*(\d)/i);
  if (yarsevMatch) return `ЯрСев блок ${yarsevMatch[1]}`;

  const fertMatch = text.match(/удобрения\s*[—-]\s*([^\n|]{3,40})/i);
  if (fertMatch) return `Удобрения — ${normalizeText(fertMatch[1])}`;

  for (const firm of AGROFIRM_MARKERS) {
    if (lower.includes(firm.toLowerCase())) {
      if (lower.includes("выработка техники") || lower.includes("1.1") || lower.includes("внесение минеральных")) {
        return `1.1 МУ — ${firm}`;
      }
      return firm;
    }
  }

  const workMatch = text.match(/вид\s+работ[:\s]+([0-9.]+\s+[^\n|]{2,48})/i);
  if (workMatch) return `Выраб. — ${normalizeText(workMatch[1])}`;

  if (lower.includes("боронован")) return "Выраб. — 2.1 Боронование";
  if (lower.includes("дискован")) return "Выраб. — 2.9 Дискование";
  if (lower.includes("фрезерован")) return "Выраб. — 2.6 Фрезерование";
  if (lower.includes("гребнеобраз")) return "Выраб. — 2.7 Гребнеобразование";
  if (lower.includes("культив")) return "Выраб. — 2.11 Культивация";
  if (lower.includes("опрыскив")) return "Выраб. — 3.1 Опрыскивание";
  if (lower.includes("вспашк") || lower.includes("глубокорыхл")) return "Вспашка / глубокорыхление";
  if (lower.includes("выработка техники")) return "Выработка техники";

  return "";
}

function extractFallbackSnippet(textContent) {
  const items = (textContent.items || [])
    .map((item) => ({
      str: normalizeText(item?.str),
      x: Number(item?.transform?.[4] || 0),
      y: Number(item?.transform?.[5] || 0)
    }))
    .filter((item) => item.str.length >= 4 && !/^\d+$/.test(item.str));

  items.sort((a, b) => {
    if (Math.abs(b.y - a.y) > 6) return b.y - a.y;
    return a.x - b.x;
  });

  const lines = [];
  for (const item of items) {
    if (lines.some((line) => line.toLowerCase() === item.str.toLowerCase())) continue;
    lines.push(item.str);
    if (lines.length >= 2) break;
  }
  return lines.join(" · ").slice(0, 72);
}

async function extractPageLabel(page) {
  const textContent = await page.getTextContent();
  const fullText = (textContent.items || []).map((item) => String(item?.str || "")).join(" ");
  const label = extractLabelFromFullText(fullText);
  if (label) return label;
  const snippet = extractFallbackSnippet(textContent);
  return snippet || "";
}

function yieldToBrowser() {
  return new Promise((resolve) => {
    setTimeout(resolve, 0);
  });
}

async function openPdf(fileUrl) {
  const loadingTask = getDocument({
    url: fileUrl,
    withCredentials: true
  });
  return loadingTask.promise;
}

async function renderPageThumbnail(page, pageNumber, { scale = 0.35, withLabels = true } = {}) {
  const viewport = page.getViewport({ scale });
  const canvas = document.createElement("canvas");
  canvas.width = Math.ceil(viewport.width);
  canvas.height = Math.ceil(viewport.height);
  const ctx = canvas.getContext("2d", { alpha: false });
  await page.render({
    canvasContext: ctx,
    viewport
  }).promise;
  const label = withLabels ? await extractPageLabel(page) : `Страница ${pageNumber}`;
  return {
    page: pageNumber,
    imageDataUrl: canvas.toDataURL("image/jpeg", 0.82),
    width: viewport.width,
    height: viewport.height,
    label: label || `Страница ${pageNumber}`
  };
}

export async function loadSinglePdfPageThumbnail(fileUrl, pageNumber, { scale = 0.35, withLabels = true } = {}) {
  const pdf = await openPdf(fileUrl);
  const pageNum = Math.max(1, Math.min(Number(pageNumber) || 1, pdf.numPages));
  const page = await pdf.getPage(pageNum);
  return renderPageThumbnail(page, pageNum, { scale, withLabels });
}

/**
 * Рендер миниатюр страниц PDF для редактора порядка.
 * Между страницами отдаёт управление браузеру, чтобы UI не зависал.
 */
export async function loadPdfPageThumbnails(
  fileUrl,
  { scale = 0.35, withLabels = true, onPage, shouldCancel } = {}
) {
  const pdf = await openPdf(fileUrl);
  const pages = [];
  for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
    if (shouldCancel?.()) break;
    const page = await pdf.getPage(pageNumber);
    const thumb = await renderPageThumbnail(page, pageNumber, { scale, withLabels });
    pages.push(thumb);
    onPage?.(thumb, pageNumber, pdf.numPages);
    if (pageNumber < pdf.numPages) {
      await yieldToBrowser();
    }
  }
  return pages;
}

export async function getPdfPageCount(fileUrl) {
  const pdf = await openPdf(fileUrl);
  return pdf.numPages;
}
