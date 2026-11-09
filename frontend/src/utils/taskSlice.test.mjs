import assert from "node:assert/strict";

import { filterRows } from "./namolotFilter.js";
import { flattenFieldGroups, flattenWorkerGroups, groupFields, groupWorkers, pageFieldTable, pageWorkerTable } from "./taskSlice.js";

const rows = [
  { firm: "Актай", surname: "Пискеев", plate: "2824", field: "П2", field_id: 2, task_id: 20, task_url: "http://t/20", ha: 4, track_ha: 3.5, machine_id: 1, crew: "own" },
  { firm: "Актай", surname: "Пискеев", plate: "2824", field: "П1", field_id: 1, task_id: 10, task_url: "http://t/10", ha: 10, track_ha: 9, machine_id: 1, crew: "own" },
  { firm: "Актай", surname: "Пискеев", plate: "2824", field: "П1", field_id: 1, task_id: 10, task_url: "http://t/10", ha: 1, track_ha: 0.5, machine_id: 1, crew: "own" },
  { firm: "Актай", surname: "Пискеев", plate: "1111", field: "П1", field_id: 1, task_id: 11, task_url: "http://t/11", ha: 2, track_ha: null, machine_id: 2, crew: "hired" },
  { firm: "НурлатАгро", surname: "Иванов", plate: "9", field: "П9", field_id: 9, task_id: 30, task_url: "http://t/30", ha: 1, track_ha: 1, machine_id: 3, crew: "hired" },
];

const workers = groupWorkers(rows);
assert.equal(workers.people, 2);
assert.equal(workers.ha, 18);
assert.equal(workers.track, 14);
assert.deepEqual(workers.firms.map((firm) => firm.firm), ["Актай", "НурлатАгро"]);
const piskeev = workers.firms[0].people[0];
assert.equal(piskeev.name, "Пискеев");
assert.equal(piskeev.ha, 17);
assert.equal(piskeev.track, 13);
assert.deepEqual(piskeev.machines.map((machine) => machine.plate), ["1111", "2824"]);
const sameTask = piskeev.machines[1].lines[0];
assert.equal(sameTask.field, "П1");
assert.equal(sameTask.ha, 11);
assert.equal(sameTask.track, 9.5);
assert.equal(sameTask.taskUrl, "http://t/10");

const flat = flattenWorkerGroups(workers.firms);
assert.equal(flat[0].firm, "Актай");
assert.equal(flat[0].firmSpan, flat.filter((row) => row.firm === "Актай").length);
assert.equal(flat.find((row) => row.kind === "total" && row.person === "Пискеев").ha, 17);
const page = pageWorkerTable(rows, 0, 1);
assert.equal(page.count, 2);
assert.equal(page.table[0].person, "Пискеев");
assert.equal(page.table.some((row) => row.person === "Иванов"), false);

const fields = groupFields(rows);
const field1 = fields.firms[0].fields.find((field) => field.name === "П1");
assert.equal(field1.ha, 13);
assert.equal(field1.track, 9.5);
const fieldFlat = flattenFieldGroups(fields.firms);
assert.equal(fieldFlat.find((row) => row.kind === "field" && row.field === "П1").ha, 13);
assert.equal(fieldFlat.some((row) => row.kind === "person" && row.person === "Пискеев" && row.ha === 13), true);
const mixedCase = groupWorkers([
  { firm: "Актай", surname: "МАЧУКОВ", plate: "1", field: "П", field_id: 1, task_id: 1, ha: 1, track_ha: 1 },
  { firm: "Актай", surname: "Мачуков", plate: "2", field: "П", field_id: 1, task_id: 2, ha: 2, track_ha: 2 },
]);
assert.equal(mixedCase.people, 1);
assert.equal(mixedCase.firms[0].people[0].name, "Мачуков");
assert.equal(mixedCase.firms[0].people[0].ha, 3);
assert.equal(groupFields([
  { firm: "Актай", surname: "МАЧУКОВ", plate: "1", field: "П", field_id: 1, task_id: 1, ha: 1, track_ha: 1 },
  { firm: "Актай", surname: "Мачуков", plate: "2", field: "П", field_id: 1, task_id: 2, ha: 2, track_ha: 2 },
]).firms[0].fields[0].people.length, 1);

const oneLine = pageFieldTable(
  [{ firm: "Актай", surname: "Иванов", plate: "9", field: "П9", field_id: 9, task_id: 30, ha: 1, track_ha: 1 }],
  0,
  10,
);
assert.equal(oneLine.table.filter((row) => row.kind === "person").length, 0);
assert.equal(oneLine.table.find((row) => row.kind === "field").ha, 1);

function audit(table, showFirm, mode) {
  const columns = (showFirm ? 1 : 0) + 6;
  let firmLeft = 0;
  let personLeft = 0;
  let plateLeft = 0;
  let fieldLeft = 0;
  table.forEach((row, index) => {
    let cells = 0;
    const occupy = (left) => {
      cells += 1;
      return left - 1;
    };
    if (showFirm) {
      if (firmLeft > 0) firmLeft = occupy(firmLeft);
      else if (row.firmSpan > 0) {
        cells += 1;
        firmLeft = row.firmSpan - 1;
      }
    }
    if (mode === "field") {
      if (fieldLeft > 0) fieldLeft = occupy(fieldLeft);
      else if (row.fieldSpan > 0) {
        cells += 1;
        fieldLeft = row.fieldSpan - 1;
      }
    }
    if (mode === "worker") {
      if (personLeft > 0) personLeft = occupy(personLeft);
      else if (row.personSpan > 0) {
        cells += 1;
        personLeft = row.personSpan - 1;
      }
      if (row.kind === "total") cells += 5;
      else {
        if (plateLeft > 0) plateLeft = occupy(plateLeft);
        else if (row.plateSpan > 0) {
          cells += 1;
          plateLeft = row.plateSpan - 1;
        }
        cells += 4;
      }
    } else if (row.kind === "field") {
      cells += 5;
    } else if (row.kind === "person") {
      if (personLeft > 0) personLeft = occupy(personLeft);
      cells += 4;
    } else {
      if (personLeft > 0) personLeft = occupy(personLeft);
      else if (row.personSpan > 0) {
        cells += 1;
        personLeft = row.personSpan - 1;
      }
      if (plateLeft > 0) plateLeft = occupy(plateLeft);
      else if (row.plateSpan > 0) {
        cells += 1;
        plateLeft = row.plateSpan - 1;
      }
      cells += 3;
    }
    assert.equal(cells, columns, `row ${index} ${row.kind} ${mode} showFirm=${showFirm}: ${cells}`);
  });
}

audit(flat, true, "worker");
audit(flat, false, "worker");
audit(fieldFlat, true, "field");
audit(fieldFlat, false, "field");

const hired = filterRows(rows, { crew: "hired" });
assert.deepEqual(hired.map((row) => row.task_id), [11, 30]);
const own = filterRows(rows, { crew: "own" });
assert.deepEqual(own.map((row) => row.task_id), [20, 10, 10]);
assert.equal(filterRows(rows, { crew: "all" }).length, rows.length);
assert.equal(filterRows(rows, { firms: ["Актай"] }).length, 4);

console.log("task slice ok");
