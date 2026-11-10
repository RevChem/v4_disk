/** Группировка среза заданий: агрофирма → фамилия → машина → поле, и срез по полям. */

function round2(value) {
  return Math.round(value * 100) / 100;
}

function compareText(a, b) {
  return String(a).localeCompare(String(b), "ru", { numeric: true, sensitivity: "base" });
}

function amountOf(row) {
  const value = Number(row?.ha ?? row?.tonnes);
  return Number.isFinite(value) ? value : 0;
}

function trackOf(row) {
  if (row?.track_ha == null || row.track_ha === "") return null;
  const value = Number(row.track_ha);
  return Number.isFinite(value) ? value : null;
}

function bump(node, ha, track) {
  node.ha = round2(node.ha + ha);
  if (track != null) node.track = round2((node.track ?? 0) + track);
}

function take(map, key, create) {
  let node = map.get(key);
  if (!node) {
    node = create();
    map.set(key, node);
  }
  return node;
}

function personName(row) {
  return String(row?.surname || row?.operator || "").trim() || "без фамилии";
}

function personKey(name) {
  return name.toLocaleLowerCase("ru");
}

function preferPersonName(current, next) {
  const hasLower = (value) => /[a-zа-яё]/.test(value);
  if (hasLower(next) && !hasLower(current)) return next;
  return current;
}

function machineLabel(row) {
  const plate = String(row?.plate || "").trim();
  if (plate) return plate;
  if (row?.machine_id) return `машина ${row.machine_id}`;
  return "без номера";
}

function firmName(row) {
  return String(row?.firm || "").trim() || "без агрофирмы";
}

export function groupWorkers(rows) {
  const firms = new Map();
  let ha = 0;
  let track = null;
  for (const row of Array.isArray(rows) ? rows : []) {
    const haValue = amountOf(row);
    const trackValue = trackOf(row);
    const firm = take(firms, firmName(row), () => ({ firm: firmName(row), people: new Map(), ha: 0, track: null }));
    const namedPerson = personName(row);
    const person = take(firm.people, personKey(namedPerson), () => ({ name: namedPerson, machines: new Map(), ha: 0, track: null }));
    person.name = preferPersonName(person.name, namedPerson);
    const plate = machineLabel(row);
    const machine = take(person.machines, plate, () => ({ plate, lines: new Map(), ha: 0, track: null }));
    const field = String(row?.field || "").trim() || "без поля";
    const fieldId = row?.field_id ?? field;
    const taskId = row?.task_id ?? "";
    const line = take(machine.lines, `${fieldId}|${taskId}`, () => ({
      field,
      taskId: row?.task_id ?? null,
      taskUrl: row?.task_url || "",
      ha: 0,
      track: null,
    }));
    bump(line, haValue, trackValue);
    bump(machine, haValue, trackValue);
    bump(person, haValue, trackValue);
    bump(firm, haValue, trackValue);
    ha = round2(ha + haValue);
    if (trackValue != null) track = round2((track ?? 0) + trackValue);
  }

  let people = 0;
  const firmList = [...firms.values()].sort((a, b) => compareText(a.firm, b.firm));
  for (const firm of firmList) {
    firm.people = [...firm.people.values()].sort((a, b) => compareText(a.name, b.name));
    people += firm.people.length;
    for (const person of firm.people) {
      person.machines = [...person.machines.values()].sort((a, b) => compareText(a.plate, b.plate));
      for (const machine of person.machines) {
        machine.lines = [...machine.lines.values()].sort(
          (a, b) => compareText(a.field, b.field) || (Number(a.taskId) || 0) - (Number(b.taskId) || 0),
        );
      }
    }
  }
  return { firms: firmList, people, ha, track };
}

export function groupFields(rows) {
  const firms = new Map();
  let ha = 0;
  let track = null;
  let fields = 0;
  for (const row of Array.isArray(rows) ? rows : []) {
    const haValue = amountOf(row);
    const trackValue = trackOf(row);
    const named = firmName(row);
    const firm = take(firms, named, () => ({ firm: named, fields: new Map(), ha: 0, track: null }));
    const fieldLabel = String(row?.field || "").trim() || "без поля";
    const fieldId = row?.field_id ?? fieldLabel;
    const field = take(firm.fields, String(fieldId), () => ({ name: fieldLabel, people: new Map(), ha: 0, track: null }));
    const namedPerson = personName(row);
    const person = take(field.people, personKey(namedPerson), () => ({ name: namedPerson, machines: new Map(), ha: 0, track: null }));
    person.name = preferPersonName(person.name, namedPerson);
    const plate = machineLabel(row);
    const machine = take(person.machines, plate, () => ({ plate, lines: new Map(), ha: 0, track: null }));
    const taskId = row?.task_id ?? "";
    const line = take(machine.lines, String(taskId), () => ({
      taskId: row?.task_id ?? null,
      taskUrl: row?.task_url || "",
      ha: 0,
      track: null,
    }));
    bump(line, haValue, trackValue);
    bump(machine, haValue, trackValue);
    bump(person, haValue, trackValue);
    bump(field, haValue, trackValue);
    bump(firm, haValue, trackValue);
    ha = round2(ha + haValue);
    if (trackValue != null) track = round2((track ?? 0) + trackValue);
  }

  const firmList = [...firms.values()].sort((a, b) => compareText(a.firm, b.firm));
  for (const firm of firmList) {
    firm.fields = [...firm.fields.values()].sort((a, b) => compareText(a.name, b.name));
    fields += firm.fields.length;
    for (const field of firm.fields) {
      field.people = [...field.people.values()].sort((a, b) => compareText(a.name, b.name));
      for (const person of field.people) {
        person.machines = [...person.machines.values()].sort((a, b) => compareText(a.plate, b.plate));
        for (const machine of person.machines) {
          machine.lines = [...machine.lines.values()].sort((a, b) => (Number(a.taskId) || 0) - (Number(b.taskId) || 0));
        }
      }
    }
  }
  return { firms: firmList, fields, ha, track };
}

export function flattenWorkerGroups(firms) {
  const table = [];
  for (const firm of firms) {
    const firmStart = table.length;
    for (const person of firm.people) {
      const personStart = table.length;
      for (const machine of person.machines) {
        const plateStart = table.length;
        for (const line of machine.lines) {
          table.push({
            kind: "line",
            firmSpan: 0,
            firm: firm.firm,
            personSpan: 0,
            person: person.name,
            plateSpan: 0,
            plate: machine.plate,
            field: line.field,
            ha: line.ha,
            track: line.track,
            taskId: line.taskId,
            taskUrl: line.taskUrl,
          });
        }
        if (table.length > plateStart) table[plateStart].plateSpan = table.length - plateStart;
      }
      table.push({
        kind: "total",
        firmSpan: 0,
        firm: firm.firm,
        personSpan: 0,
        person: person.name,
        ha: person.ha,
        track: person.track,
      });
      if (table.length > personStart) table[personStart].personSpan = table.length - personStart;
    }
    if (table.length > firmStart) table[firmStart].firmSpan = table.length - firmStart;
  }
  return table;
}

export function flattenFieldGroups(firms) {
  const table = [];
  for (const firm of firms) {
    const firmStart = table.length;
    for (const field of firm.fields) {
      const fieldStart = table.length;
      for (const person of field.people) {
        const personStart = table.length;
        let lines = 0;
        for (const machine of person.machines) {
          const plateStart = table.length;
          for (const line of machine.lines) {
            lines += 1;
            table.push({
              kind: "line",
              firmSpan: 0,
              firm: firm.firm,
              fieldSpan: 0,
              field: field.name,
              personSpan: 0,
              person: person.name,
              plateSpan: 0,
              plate: machine.plate,
              ha: line.ha,
              track: line.track,
              taskId: line.taskId,
              taskUrl: line.taskUrl,
            });
          }
          if (table.length > plateStart) table[plateStart].plateSpan = table.length - plateStart;
        }
        if (lines > 1) {
          table.push({
            kind: "person",
            firmSpan: 0,
            firm: firm.firm,
            fieldSpan: 0,
            field: field.name,
            personSpan: 0,
            person: person.name,
            ha: person.ha,
            track: person.track,
          });
        }
        if (table.length > personStart) table[personStart].personSpan = table.length - personStart;
      }
      table.push({
        kind: "field",
        firmSpan: 0,
        firm: firm.firm,
        fieldSpan: 0,
        field: field.name,
        ha: field.ha,
        track: field.track,
      });
      if (table.length > fieldStart) table[fieldStart].fieldSpan = table.length - fieldStart;
    }
    if (table.length > firmStart) table[firmStart].firmSpan = table.length - firmStart;
  }
  return table;
}

function fieldLabel(row) {
  return String(row?.field || "").trim() || "без поля";
}

function sliceLabel(row, key) {
  if (key === "firm") return firmName(row);
  if (key === "person") return personName(row);
  if (key === "plate") return machineLabel(row);
  return fieldLabel(row);
}

export const EMPTY_SLICE_COLUMNS = { firm: "", person: "", plate: "", field: "" };

export function filterSliceRows(rows, filters) {
  const list = Array.isArray(rows) ? rows : [];
  const firm = filters?.firm || "";
  const person = filters?.person || "";
  const personFold = person ? personKey(person) : "";
  const plate = filters?.plate || "";
  const field = filters?.field || "";
  if (!firm && !person && !plate && !field) return list;
  return list.filter((row) => {
    if (firm && sliceLabel(row, "firm") !== firm) return false;
    if (personFold && personKey(personName(row)) !== personFold) return false;
    if (plate && sliceLabel(row, "plate") !== plate) return false;
    if (field && sliceLabel(row, "field") !== field) return false;
    return true;
  });
}

export function sliceColumnOptions(rows, filters) {
  const options = { firm: new Set(), person: new Map(), plate: new Set(), field: new Set() };
  for (const key of ["firm", "plate", "field"]) {
    for (const row of filterSliceRows(rows, { ...filters, [key]: "" })) options[key].add(sliceLabel(row, key));
  }
  for (const row of filterSliceRows(rows, { ...filters, person: "" })) {
    const name = personName(row);
    const key = personKey(name);
    options.person.set(key, preferPersonName(options.person.get(key) || name, name));
  }
  return {
    firm: [...options.firm].sort(compareText),
    person: [...options.person.values()].sort(compareText),
    plate: [...options.plate].sort(compareText),
    field: [...options.field].sort(compareText),
  };
}

export function sliceStats(rows) {
  const list = Array.isArray(rows) ? rows : [];
  const people = new Set();
  const plates = new Set();
  const fields = new Set();
  const tasks = new Set();
  const firms = new Set();
  const byPerson = new Map();
  const byField = new Map();
  let ha = 0;
  let track = null;
  for (const row of list) {
    const person = sliceLabel(row, "person");
    const field = sliceLabel(row, "field");
    const amount = amountOf(row);
    const trackValue = trackOf(row);
    people.add(person);
    plates.add(sliceLabel(row, "plate"));
    fields.add(field);
    firms.add(sliceLabel(row, "firm"));
    if (row?.task_id != null && row.task_id !== "") tasks.add(String(row.task_id));
    ha = round2(ha + amount);
    if (trackValue != null) track = round2((track ?? 0) + trackValue);
    byPerson.set(person, round2((byPerson.get(person) || 0) + amount));
    byField.set(field, round2((byField.get(field) || 0) + amount));
  }
  return {
    lines: list.length,
    people: people.size,
    plates: plates.size,
    fields: fields.size,
    tasks: tasks.size,
    firms: firms.size,
    ha,
    track,
    topPerson: topEntry(byPerson),
    topField: topEntry(byField),
  };
}

function topEntry(map) {
  let best = null;
  for (const [name, ha] of map) {
    if (!best || ha > best.ha || (ha === best.ha && compareText(name, best.name) < 0)) best = { name, ha };
  }
  return best;
}

function taskIdsOf(node) {
  if (node?.taskId != null && node.taskId !== "") {
    const value = Number(node.taskId);
    return Number.isFinite(value) ? [value] : [];
  }
  const kids = node?.lines || node?.machines || node?.people || node?.fields || [];
  const ids = [];
  for (const kid of kids) ids.push(...taskIdsOf(kid));
  return ids;
}

function metricValue(node, sort) {
  if (sort.key === "ha") return Number.isFinite(node.ha) ? node.ha : null;
  if (sort.key === "track") return node.track == null ? null : Number(node.track);
  const ids = taskIdsOf(node);
  if (!ids.length) return null;
  return sort.dir === "asc" ? Math.min(...ids) : Math.max(...ids);
}

function metricCmp(sort, labelOf) {
  const sign = sort.dir === "asc" ? 1 : -1;
  return (a, b) => {
    const av = metricValue(a, sort);
    const bv = metricValue(b, sort);
    if (av == null || bv == null) {
      if (av != null || bv != null) return av == null ? 1 : -1;
    } else if (av !== bv) {
      return sign * (av - bv);
    }
    return compareText(labelOf(a), labelOf(b));
  };
}

function orderWorkerModel(model, sort) {
  if (!sort?.key) return model;
  model.firms.sort(metricCmp(sort, (node) => node.firm));
  for (const firm of model.firms) {
    firm.people.sort(metricCmp(sort, (node) => node.name));
    for (const person of firm.people) {
      person.machines.sort(metricCmp(sort, (node) => node.plate));
      for (const machine of person.machines) machine.lines.sort(metricCmp(sort, (node) => node.field));
    }
  }
  return model;
}

function orderFieldModel(model, sort) {
  if (!sort?.key) return model;
  model.firms.sort(metricCmp(sort, (node) => node.firm));
  for (const firm of model.firms) {
    firm.fields.sort(metricCmp(sort, (node) => node.name));
    for (const field of firm.fields) {
      field.people.sort(metricCmp(sort, (node) => node.name));
      for (const person of field.people) {
        person.machines.sort(metricCmp(sort, (node) => node.plate));
        for (const machine of person.machines) machine.lines.sort(metricCmp(sort, (node) => String(node.taskId ?? "")));
      }
    }
  }
  return model;
}

function pageOf(flat, page, pageSize) {
  const size = Math.max(1, Number(pageSize) || 1);
  const start = Math.max(0, Number(page) || 0) * size;
  return { slice: flat.slice(start, start + size), count: flat.length };
}

function taskCell(row) {
  if (row.taskId == null || row.taskId === "") return "";
  const value = Number(row.taskId);
  const shown = Number.isFinite(value) ? value : row.taskId;
  return row.taskUrl ? { v: shown, href: row.taskUrl } : shown;
}

export function workerSheetRows(rows, { showFirm = false, sort = null } = {}) {
  const model = orderWorkerModel(groupWorkers(rows), sort);
  const header = [...(showFirm ? ["Агрофирма"] : []), "Фамилия", "Машина", "Поле", "Площадь, га", "Площадь по треку, га", "Задание"];
  const body = flattenWorkerGroups(model.firms).map((row) => {
    const firm = showFirm ? [row.firm] : [];
    if (row.kind === "total") return [...firm, row.person, "Итого", "", row.ha, row.track ?? "", ""];
    return [...firm, row.person, row.plate, row.field, row.ha, row.track ?? "", taskCell(row)];
  });
  body.push([...(showFirm ? [""] : []), "Всего", "", "", model.ha, model.track ?? "", ""]);
  return [header, ...body];
}

export function fieldSheetRows(rows, { showFirm = false, sort = null } = {}) {
  const model = orderFieldModel(groupFields(rows), sort);
  const header = [...(showFirm ? ["Агрофирма"] : []), "Поле", "Фамилия", "Машина", "Площадь, га", "Площадь по треку, га", "Задание"];
  const body = flattenFieldGroups(model.firms).map((row) => {
    const firm = showFirm ? [row.firm || ""] : [];
    if (row.kind === "field") return [...firm, row.field, "Итого по полю", "", row.ha, row.track ?? "", ""];
    if (row.kind === "person") return [...firm, row.field || "", row.person, "Итого", row.ha, row.track ?? "", ""];
    return [...firm, row.field, row.person, row.plate, row.ha, row.track ?? "", taskCell(row)];
  });
  body.push([...(showFirm ? [""] : []), "Всего", "", "", model.ha, model.track ?? "", ""]);
  return [header, ...body];
}

export function statsSheetRows(stats) {
  const diff = stats.track == null ? "" : round2(stats.ha - stats.track);
  const share = stats.track == null || !stats.ha ? "" : round2((stats.track / stats.ha) * 100);
  const average = (count) => (count ? round2(stats.ha / count) : "");
  return [
    ["Показатель", "Значение"],
    ["Строк", stats.lines],
    ["Фамилий", stats.people],
    ["Машин", stats.plates],
    ["Полей", stats.fields],
    ["Заданий", stats.tasks],
    ["Агрофирм", stats.firms],
    ["Площадь, га", stats.ha],
    ["Площадь по треку, га", stats.track ?? ""],
    ["Разница, га", diff],
    ["Доля трека, %", share],
    ["Среднее на фамилию, га", average(stats.people)],
    ["Среднее на поле, га", average(stats.fields)],
    ["Больше всех", stats.topPerson ? `${stats.topPerson.name} · ${stats.topPerson.ha}` : ""],
    ["Крупнейшее поле", stats.topField ? `${stats.topField.name} · ${stats.topField.ha}` : ""],
  ];
}

export function pageWorkerTable(rows, page, pageSize, sort = null) {
  const model = orderWorkerModel(groupWorkers(rows), sort);
  const flat = [];
  for (const firm of model.firms) {
    for (const person of firm.people) flat.push({ firmName: firm.firm, person });
  }
  const { slice, count } = pageOf(flat, page, pageSize);
  const firms = [];
  const index = new Map();
  for (const item of slice) {
    let firm = index.get(item.firmName);
    if (!firm) {
      firm = { firm: item.firmName, people: [] };
      index.set(item.firmName, firm);
      firms.push(firm);
    }
    firm.people.push(item.person);
  }
  return { table: flattenWorkerGroups(firms), count, ha: model.ha, track: model.track };
}

export function pageFieldTable(rows, page, pageSize, sort = null) {
  const model = orderFieldModel(groupFields(rows), sort);
  const flat = [];
  for (const firm of model.firms) {
    for (const field of firm.fields) flat.push({ firmName: firm.firm, field });
  }
  const { slice, count } = pageOf(flat, page, pageSize);
  const firms = [];
  const index = new Map();
  for (const item of slice) {
    let firm = index.get(item.firmName);
    if (!firm) {
      firm = { firm: item.firmName, fields: [] };
      index.set(item.firmName, firm);
      firms.push(firm);
    }
    firm.fields.push(item.field);
  }
  return { table: flattenFieldGroups(firms), count, ha: model.ha, track: model.track };
}
