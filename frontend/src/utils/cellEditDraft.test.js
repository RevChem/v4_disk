import test from "node:test";
import assert from "node:assert/strict";
import {
  applyDraftValue,
  buildBatchPayload,
  clampZoom,
  draftCount,
  findCellForEdit,
  findEditForCell,
  isCellChanged,
  mergeEditRows,
  readingNeighbor,
  revertSavedEdit,
  stepZoom,
  verticalNeighbor
} from "./cellEditDraft.js";

function cell(key, text, page, left, top, width = 0.08, height = 0.012) {
  return {
    cellKey: key,
    cellText: text,
    page,
    leftPct: left,
    topPct: top,
    widthPct: width,
    heightPct: height,
    fillColor: "#ffffff",
    textColor: ""
  };
}

// Сетка 3 столбца × 3 строки на странице 1.
const grid = [];
for (let r = 0; r < 3; r += 1) {
  for (let c = 0; c < 3; c += 1) {
    grid.push(cell(`1:${c}:${r}:v`, `${r}${c}`, 1, 0.1 + c * 0.2, 0.1 + r * 0.05));
  }
}
const pages = [{ page: 1, cells: grid }];

const savedEdit = {
  id: "ed1",
  page: 1,
  left: 0.1,
  top: 0.1,
  width: 0.08,
  height: 0.012,
  cell_key: "1:0:0:v",
  cell_text: "00",
  replacement: "77"
};

test("новое значение попадает в черновик, совпадение с текущим — убирает запись", () => {
  const c = grid[0];
  let draft = applyDraftValue({}, [], c, "123");
  assert.equal(draftCount(draft), 1);
  assert.equal(draft[c.cellKey].replacement, "123");
  assert.equal(draft[c.cellKey].editId, "");
  draft = applyDraftValue(draft, [], c, "  00 ");
  assert.equal(draftCount(draft), 0);
});

test("повторная правка уже сохранённой ячейки помнит исходный текст и id правки", () => {
  // В PDF после сохранения у ячейки уже новый текст и другой ключ — ищем по месту.
  const shown = cell("1:0:0:77", "77", 1, 0.1, 0.1);
  assert.equal(findEditForCell([savedEdit], shown), savedEdit);
  const draft = applyDraftValue({}, [savedEdit], shown, "88");
  const entry = draft[shown.cellKey];
  assert.equal(entry.editId, "ed1");
  assert.equal(entry.cell.cellText, "00");
  const payload = buildBatchPayload(draft, [savedEdit]);
  assert.deepEqual(payload.deleteIds, []);
  assert.equal(payload.edits.length, 1);
  assert.equal(payload.edits[0].cellText, "00");
  assert.equal(payload.edits[0].replacement, "88");
});

test("возврат к исходному значению превращается в удаление правки", () => {
  const shown = cell("1:0:0:77", "77", 1, 0.1, 0.1);
  const draft = applyDraftValue({}, [savedEdit], shown, "00");
  const payload = buildBatchPayload(draft, [savedEdit]);
  assert.deepEqual(payload.deleteIds, ["ed1"]);
  assert.deepEqual(payload.edits, []);
  assert.equal(isCellChanged(shown, savedEdit, draft[shown.cellKey]), false);
});

test("отмена правки удаляет именно её, даже если по месту находится соседняя", () => {
  // Сводка 13: много нулей рядом. Центр ячейки попадает в более раннюю правку,
  // у которой замена как раз равна исходному тексту удаляемой — черновик пропадал.
  const editA = {
    ...savedEdit,
    id: "a",
    cell_text: "100",
    replacement: "0",
    left: 0.1,
    top: 0.1,
    width: 0.2,
    height: 0.05
  };
  const editB = {
    ...savedEdit,
    id: "b",
    cell_key: "1:1:1:0",
    cell_text: "0",
    replacement: "примечание",
    left: 0.12,
    top: 0.11,
    width: 0.08,
    height: 0.02
  };
  const shown = cell("1:1:1:примечание", "примечание", 1, 0.12, 0.11, 0.08, 0.02);
  assert.equal(draftCount(applyDraftValue({}, [editA, editB], shown, "0")), 0);
  const draft = revertSavedEdit({}, editB, shown);
  const payload = buildBatchPayload(draft, [editA, editB]);
  assert.deepEqual(payload.deleteIds, ["b"]);
  assert.deepEqual(payload.edits, []);
  const rows = mergeEditRows([editA, editB], draft);
  assert.ok(rows.some((row) => row.status === "revert" && row.edit.id === "b"));
});

test("значение, равное сохранённому, не создаёт черновик", () => {
  const shown = cell("1:0:0:77", "77", 1, 0.1, 0.1);
  assert.equal(draftCount(applyDraftValue({}, [savedEdit], shown, "77")), 0);
});

test("isCellChanged: сохранённая и черновая правки подсвечиваются, нетронутые — нет", () => {
  const shown = cell("1:0:0:77", "77", 1, 0.1, 0.1);
  assert.equal(isCellChanged(shown, savedEdit, undefined), true);
  assert.equal(isCellChanged(grid[4], null, undefined), false);
  const draft = applyDraftValue({}, [], grid[4], "5");
  assert.equal(isCellChanged(grid[4], null, draft[grid[4].cellKey]), true);
});

test("пакет содержит все правки черновика", () => {
  let draft = {};
  draft = applyDraftValue(draft, [], grid[1], "a");
  draft = applyDraftValue(draft, [], grid[5], "b");
  draft = applyDraftValue(draft, [], grid[8], "c");
  const payload = buildBatchPayload(draft, []);
  assert.deepEqual(payload.edits.map((e) => e.replacement).sort(), ["a", "b", "c"]);
  assert.deepEqual(payload.deleteIds, []);
});

test("список правок: статусы saved / modified / revert / new", () => {
  const edit2 = { ...savedEdit, id: "ed2", left: 0.5, top: 0.2, cell_key: "1:2:2:v", cell_text: "22", replacement: "9" };
  const edits = [savedEdit, edit2];
  const shown1 = cell("1:0:0:77", "77", 1, 0.1, 0.1);
  let draft = applyDraftValue({}, edits, shown1, "88"); // modified
  draft = applyDraftValue(draft, edits, grid[4], "new"); // new
  let rows = mergeEditRows(edits, draft);
  const byStatus = Object.fromEntries(rows.map((r) => [r.status, r]));
  assert.equal(rows.length, 3);
  assert.ok(byStatus.modified && byStatus.new && byStatus.saved);
  assert.equal(byStatus.modified.to, "88");
  draft = applyDraftValue(draft, edits, shown1, "00"); // revert
  rows = mergeEditRows(edits, draft);
  assert.ok(rows.some((r) => r.status === "revert" && r.edit.id === "ed1"));
});

test("findCellForEdit находит ячейку и по ключу, и по месту", () => {
  const shown = cell("1:0:0:77", "77", 1, 0.1, 0.1);
  const found = findCellForEdit([{ page: 1, cells: [shown] }], savedEdit);
  assert.equal(found.cellKey, "1:0:0:77");
  const fallback = findCellForEdit([], savedEdit);
  assert.equal(fallback.cellText, "00");
});

test("verticalNeighbor: вниз/вверх по столбцу, на краю — null", () => {
  const down = verticalNeighbor(pages, grid[1], 1); // r0 c1 -> r1 c1
  assert.equal(down.cellText, "11");
  const up = verticalNeighbor(pages, grid[4], -1);
  assert.equal(up.cellText, "01");
  assert.equal(verticalNeighbor(pages, grid[7], 1), null);
  assert.equal(verticalNeighbor(pages, grid[1], -1), null);
});

test("readingNeighbor: по строкам слева направо и на следующую страницу", () => {
  assert.equal(readingNeighbor(pages, grid[0], 1).cellText, "01");
  assert.equal(readingNeighbor(pages, grid[2], 1).cellText, "10");
  assert.equal(readingNeighbor(pages, grid[0], -1), null);
  const two = [pages[0], { page: 2, cells: [cell("2:0:0:z", "zz", 2, 0.1, 0.1)] }];
  assert.equal(readingNeighbor(two, grid[8], 1).cellText, "zz");
});

test("масштаб: шаги ограничены, кнопки не выходят за границы", () => {
  assert.equal(stepZoom(1, 1), 1.25);
  assert.equal(stepZoom(1, -1), 0.8);
  assert.equal(stepZoom(3, 1), 3);
  assert.equal(stepZoom(0.5, -1), 0.5);
  assert.equal(stepZoom(1.1, 1), 1.25);
  assert.equal(stepZoom(1.1, -1), 1);
  assert.equal(clampZoom(10), 3);
  assert.equal(clampZoom(0.1), 0.5);
  assert.equal(clampZoom("abc"), 1);
});

test("одна сохранённая правка — одна запись черновика, даже при другом ключе ячейки", () => {
  const viaRowFallback = cell("1:0:0:old-key", "00", 1, 0.1, 0.1); // открыта из списка
  const viaPage = cell("1:0:0:77", "77", 1, 0.1, 0.1); // открыта кликом по странице
  let draft = applyDraftValue({}, [savedEdit], viaRowFallback, "88");
  draft = applyDraftValue(draft, [savedEdit], viaPage, "99");
  assert.equal(draftCount(draft), 1);
  assert.equal(Object.values(draft)[0].replacement, "99");
  // возврат к сохранённому значению убирает именно эту запись
  draft = applyDraftValue(draft, [savedEdit], viaPage, "77");
  assert.equal(draftCount(draft), 0);
});
