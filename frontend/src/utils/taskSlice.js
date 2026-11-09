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
            fieldSpan: 0,
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

function pageOf(flat, page, pageSize) {
  const size = Math.max(1, Number(pageSize) || 1);
  const start = Math.max(0, Number(page) || 0) * size;
  return { slice: flat.slice(start, start + size), count: flat.length };
}

export function pageWorkerTable(rows, page, pageSize) {
  const model = groupWorkers(rows);
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

export function pageFieldTable(rows, page, pageSize) {
  const model = groupFields(rows);
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
