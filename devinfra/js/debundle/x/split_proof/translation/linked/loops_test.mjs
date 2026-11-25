import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  arrow,
  block,
  call,
  caseShard,
  assertRejectedOrOrderKeptSlice,
  exportNamed,
  expression,
  forcePreview,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  program,
  returns,
  sequentialSplit,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const runCase = caseShard();

if (runCase("module-for")) {
  const source = `
    const before = j;
    var total = 0;
    const reads = {};
    for (let i = 0; i < 3; i++) {
      reads[i] = () => i;
    }
    for (var j = 0; j < 2; j++) {
      total += j;
    }
    export const result = [before === undefined, reads[0](), reads[1](), reads[2](), j, total];
  `;
  const { files, out, verdict, log } = sequentialSplit("module-for", source, {
    "worker.mjs": ["#0", "#1", "#2", "#3", "#4"],
    [entry]: ["#5"],
  });
  assert.equal(verdict.status, "certified", log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: [true, 0, 1, 2, 2, 1] });
  }
}

if (runCase("module-for-many-vars")) {
  const source = `
    let trace = "";
    for (var outer = 0, limit = (trace += "init:", 2); outer < limit; ++outer) {
      for (var row = outer, inner = 0; inner < 2; ++inner) {
        trace += \`\${row}\${inner} \`;
      }
    }
    export const result = [trace, outer, limit, row, inner];
  `;
  const { files, out, verdict, log } = sequentialSplit("module-for-many-vars", source, {
    "worker.mjs": ["#0", "#1"],
    [entry]: ["#2"],
  });
  assert.equal(verdict.status, "certified", log);
  assert.equal(lower(files.sourceFile, files.specFile, out, ["--check-output"]).verdict.status, "certified");
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: ["init:00 01 10 11 ", 2, 2, 1, 2] });
  }
}

if (runCase("for-many-lexical-cells")) {
  const source = `
    const read = () => {
      let first, second, tdz = false;
      for (let i = 0, j = i + 10; i < 2; i++, j++) {
        if (i === 0) first = () => [i, j];
        else second = () => [i, j];
      }
      try { for (let early = later, later = 1; false;) {} }
      catch { tdz = true; }
      return [first(), second(), tdz];
    };
    export const result = read();
  `;
  const { files, out, verdict, log } = sequentialSplit("for-many-lexical-cells", source, {
    "worker.mjs": ["#0"],
    [entry]: ["#1"],
  });
  assert.equal(verdict.status, "certified", log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: [[0, 10], [1, 11], true] });
  }
}

if (runCase("guarded-for-in")) {
  const source = `
    class Grandparent {}
    const addEnumerable = () => { Grandparent.prototype.hidden = 1; };
    const initialized = addEnumerable();
    class Parent extends Grandparent { hidden() {} }
    const inherited = { __proto__: Parent.prototype, duplicate: 0, inherited: 1 };
    const keys = { __proto__: inherited, second: 0, 2: 0, duplicate: 1, 1: 0 };
    const reads = { __proto__: null };
    let last;
    for (const key in keys) {
      reads[key] = { read: () => key };
      last = key;
    }
    for (var first in keys) break;
    let caught;
    try { for (const key in keys) throw key; } catch (key) { caught = key; }
    for (const key in null) throw key;
    export const result = [
      reads[1].read(), reads[2].read(), reads.second.read(), reads.duplicate.read(),
      reads.inherited.read(), reads.hidden === undefined, reads.constructor === undefined, first, last, caught
    ];
  `;
  const { files, out, verdict, log } = sequentialSplit("guarded-for-in", source, {
    "worker.mjs": ["#0", "#1", "#2", "#3", "#4", "#5", "#6", "#7", "#8", "#9", "#10", "#11", "#12"],
    [entry]: ["#13"],
  });
  assert.equal(verdict.status, "certified", log);
  assert.equal(lower(files.sourceFile, files.specFile, out, ["--check-output"]).verdict.status, "certified");
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), {
      result: ["1", "2", "second", "duplicate", "inherited", true, true, "1", "inherited", "1"],
    });
  }
}

if (runCase("for-in-head-tdz")) {
  const source = `
    const key = {};
    let caught = false;
    try { for (let key in key) {} } catch { caught = true; }
    export const result = caught;
  `;
  const { files, out, verdict, log } = sequentialSplit("for-in-head-tdz", source, {
    "worker.mjs": ["#0", "#1", "#2"],
    [entry]: ["#3"],
  });
  assert.equal(verdict.status, "certified", log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: true });
  }
}

if (runCase("for-in-head-captured-tdz")) {
  const source = `
    const key = "outer";
    let saved;
    let current;
    for (const key in (saved = () => key, { first: 1, last: 2 })) {
      current = key;
      continue;
    }
    let caught = false;
    try { saved(); } catch { caught = true; }
    export const result = [current, caught];
  `;
  const { files, out, verdict, log } = sequentialSplit("for-in-head-captured-tdz", source, {
    "worker.mjs": ["#0", "#1", "#2", "#3", "#4", "#5"],
    [entry]: ["#6"],
  });
  assert.equal(verdict.status, "certified", log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: ["last", true] });
  }
}

for (const [name, target, body, reason] of [
  ["source", "keys", "target[key] = 1", /writes to an enumerated object/],
  ["prototype", "prototype", "target[key] = 1", /writes to an enumerated object/],
  ["setter", "{ set a(value) { keys.b = value; } }", "target[key] = 1", /writes through an accessor/],
  ["call", "{}", "mutate(key)", /for-in body may run code/],
]) {
  if (!runCase(`unsafe-for-in-${name}`)) continue;
  const source = `
    const prototype = { b: 0 };
    const keys = { __proto__: prototype, a: 0 };
    const target = ${target};
    const mutate = key => { keys[key] = 1; };
    for (const key in keys) ${body};
    export const result = 1;
  `;
  const { files, verdict, log, out } = sequentialSplit(`unsafe-for-in-${name}`, source, {
    "worker.mjs": ["#0", "#1", "#2", "#3", "#4"],
    [entry]: ["#5"],
  });
  const orderKept = assertRejectedOrOrderKeptSlice({ verdict, log, out }, reason);
  if (orderKept) {
    assert.deepEqual(observe(path.join(out, entry), "result"), observe(files.sourceFile, "result"));
  }
}

if (runCase("iteration-capture")) {
  // The update gets a fresh lexical cell. A closure made before continue must still
  // see the first iteration's value, even after the update and the second iteration.
  const capture = sequentialSplit(
    "iteration-capture",
    `const capture = () => {
    let first;
    for (let more = true; ; more = false) {
      if (more) { first = () => more; continue; }
      break;
    }
    return first();
  }; export const result = capture();`,
    { "capture.mjs": ["#0"], [entry]: ["#1"] },
    ["capture.mjs"]
  );
  assert.equal(capture.verdict.status, "certified", capture.log);
  assert.equal(
    lower(capture.files.sourceFile, capture.files.specFile, capture.out, ["--check-output"]).verdict.status,
    "certified"
  );
  for (const file of [capture.files.sourceFile, path.join(capture.out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: true });
  }
}

const write = (name, right) => ({ type: "AssignmentExpression", operator: "=", left: id(name), right });
const jump = (type) => ({ type, label: null });
for (const kind of ["ForStatement", "WhileStatement"]) {
  for (const dependent of [false, true]) {
    if (!runCase(`${kind}-${dependent}`)) continue;
    const turn = id("turn");
    const advance = write("turn", literal(false));
    const loop = {
      type: kind,
      ...(kind === "ForStatement" ? { init: write("turn", literal(true)), update: advance } : {}),
      test: literal(true),
      body: block([
        {
          type: "IfStatement",
          test: turn,
          consequent: block([...(kind === "WhileStatement" ? [expression(advance)] : []), jump("ContinueStatement")]),
          alternate: null,
        },
        jump("BreakStatement"),
      ]),
    };
    const source = program([
      variable("const", "ready", literal(kind)),
      variable(
        "const",
        "result",
        call(
          arrow(
            [],
            block([variable("let", "turn", literal(true)), loop, returns(dependent ? id("ready") : literal(kind))])
          )
        )
      ),
      exportNamed([["result", "result"]]),
    ]);
    const order = ["result.mjs", "ready.mjs"];
    const test = sequentialSplit(
      kind + dependent,
      source,
      { "ready.mjs": ["#0"], "result.mjs": ["#1"], [entry]: ["#2"] },
      order
    );
    assert.deepEqual(observe(test.files.sourceFile, "result"), { result: kind });
    let candidate = test.out;
    if (dependent) {
      assert.equal(test.verdict.status, "rejected", test.log);
      assert(!existsSync(test.out));
      candidate = path.join(test.dir, "preview");
      forcePreview(test.files, candidate);
    } else {
      assert.equal(test.verdict.status, "certified", test.log);
    }
    assert.deepEqual(moduleEvaluationOrder(candidate, entry), [...order, entry]);
    assert.deepEqual(
      observe(path.join(candidate, entry), "result"),
      dependent ? { error: "ReferenceError" } : { result: kind }
    );
  }
}
