import test from "node:test";
import assert from "node:assert/strict";
import { commentsPrintedInCell, findCommentForCell } from "./commentCellMatch.js";

const comment = {
  id: "c1",
  page: 1,
  x: 0.2,
  y: 0.3,
  cell_key: "1:10:20:56 367",
  cell_text: "56 367",
  comment: "дождь"
};

test("комментарий находится и по старому ключу, и по точке звёздочки", () => {
  const sameKey = {
    cellKey: "1:10:20:56 367",
    page: 1,
    leftPct: 0.05,
    topPct: 0.05,
    widthPct: 0.04,
    heightPct: 0.02
  };
  assert.equal(findCommentForCell(sameKey, [comment]).id, "c1");

  const moved = {
    cellKey: "1:40:20:56 372",
    page: 1,
    leftPct: 0.16,
    topPct: 0.29,
    widthPct: 0.04,
    heightPct: 0.02
  };
  assert.equal(findCommentForCell(moved, [comment]).id, "c1");
  assert.equal(
    findCommentForCell({ ...moved, page: 2 }, [comment]),
    null
  );
});

test("строка приписки под таблицей узнаётся по тексту комментария", () => {
  const cell = { page: 1, cellText: "* дождь" };
  assert.deepEqual(commentsPrintedInCell(cell, [comment]).map((row) => row.id), ["c1"]);
  assert.deepEqual(commentsPrintedInCell({ page: 1, cellText: "0" }, [comment]), []);
});
