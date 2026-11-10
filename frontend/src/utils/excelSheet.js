/** Книга Excel в формате SpreadsheetML. Открывается в Excel без дополнительных библиотек. */

function escapeXml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function cellXml(cell) {
  if (cell == null || cell === "") return "<Cell/>";
  const linked = typeof cell === "object" && cell.href;
  const value = linked ? cell.v : cell;
  const type = typeof value === "number" && Number.isFinite(value) ? "Number" : "String";
  const data = type === "Number" ? value : escapeXml(value ?? "");
  const href = linked ? ` ss:HRef="${escapeXml(cell.href)}"` : "";
  return `<Cell${href}><Data ss:Type="${type}">${data}</Data></Cell>`;
}

function sheetXml(sheet) {
  const name = escapeXml(String(sheet.name || "Лист").slice(0, 31));
  const rows = (sheet.rows || []).map((row) => `<Row>${(row || []).map(cellXml).join("")}</Row>`).join("");
  return `<Worksheet ss:Name="${name}"><Table>${rows}</Table></Worksheet>`;
}

export function workbookXml(sheets) {
  const body = (Array.isArray(sheets) ? sheets : []).map(sheetXml).join("");
  return `<?xml version="1.0" encoding="UTF-8"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">${body}</Workbook>`;
}

export function downloadWorkbook(filename, sheets) {
  const blob = new Blob([workbookXml(sheets)], { type: "application/vnd.ms-excel;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename.endsWith(".xls") ? filename : `${filename}.xls`;
  link.click();
  URL.revokeObjectURL(url);
}
