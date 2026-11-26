import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  arrow,
  assertCertified,
  assertRejected,
  assertRejectedOrOrderKeptSlice,
  block,
  call,
  caseShard,
  exportNamed,
  forcePreview,
  functionExpression,
  id,
  literal,
  lower,
  observe,
  program,
  property,
  returns,
  root,
  sequentialSplit,
  throws,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const runCase = caseShard();

if (runCase("module-var")) {
  const source = `
    var calls = 0, order = "";
    function make() {
      calls += 1;
      return {
        get left() { order += "L"; return 37; },
        get right() { order += "R"; return first; },
      };
    }
    var before = first, { left: first, "right": second } = make(), after = second;
    export { first as left, second as right, calls, order, before, after };
  `;
  const { files, out, verdict, log } = sequentialSplit("module-var", source, {
    "worker.mjs": ["#0", "#1", "#2", "#3", "#4", "#5"],
    [entry]: ["#6"],
  });
  assert.equal(verdict.status, "certified", log);
  const expected = { left: 37, right: 37, calls: 1, order: "LR", before: undefined, after: 37 };
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    const mod = await import(pathToFileURL(file).href);
    assert.deepEqual({ ...mod }, expected);
  }
}

for (const kind of ["var", "const"]) {
  if (!runCase(`inline-module-${kind}`)) continue;
  const source = `
    const input = { x: 5, y: 7 };
    export ${kind} { x: right, "y": left } = input;
  `;
  const { files, out, verdict, log } = sequentialSplit(`inline-module-${kind}`, source, {
    "worker.mjs": ["#0"],
    [entry]: ["#1"],
  });
  assertCertified(verdict, log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    const mod = await import(pathToFileURL(file).href);
    assert.deepEqual({ ...mod }, { left: 7, right: 5 });
  }
}

if (runCase("module-lexical")) {
  const source = `
    let calls = 0, order = "";
    function make() {
      calls += 1;
      const receiver = {
        get left() { if (this !== receiver) throw 1; order += "L"; return 37; },
        get right() { order += "R"; return first; },
        get nested() { order += "N"; return { 0x10: null }; },
      };
      return receiver;
    }
    function key() { order += "K"; return "right"; }
    let { left: first, [key()]: second, nested: { 0x10: third = 7 }, absent: read = () => first } = make();
    function change() { first += 1; }
    const atInit = [read(), read.name];
    export { first, second, third, calls, order, read, change, atInit };
  `;
  const { files, out, verdict, log } = sequentialSplit("module-lexical", source, {
    "worker.mjs": ["#0", "#1", "#2", "#3", "#4", "#5"],
    [entry]: ["#6", "#7"],
  });
  assertCertified(verdict, log);
  assert.deepEqual(lower(files.sourceFile, files.specFile, out, ["--check-output"]).verdict, verdict);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    const { atInit, calls, order, second, third, read, change, ...initial } = await import(pathToFileURL(file).href);
    assert.deepEqual(
      { atInit, calls, order, second, third, initial },
      {
        atInit: [37, "read"],
        calls: 1,
        order: "LKRN",
        second: 37,
        third: null,
        initial: { first: 37 },
      }
    );
    change();
    const mod = await import(pathToFileURL(file).href);
    assert.equal(mod.first, 38);
    assert.equal(read(), 38);
  }
}

if (runCase("lexical-before-read")) {
  const source = `
    const { value = make() } = {};
    const result = read();
    function read() { return value; }
    function make() { return 7; }
    export { result };
  `;
  const modules = { "pattern.mjs": ["#0"], "call.mjs": ["#1", "#2", "#3"], [entry]: ["#4"] };
  const correct = sequentialSplit("lexical-before-read", source, modules, ["pattern.mjs", "call.mjs"]);
  assertCertified(correct.verdict, correct.log);
  for (const file of [correct.files.sourceFile, path.join(correct.out, entry)])
    assert.deepEqual(observe(file, "result"), { result: 7 });

  const early = sequentialSplit("lexical-read-before-init", source, modules, ["call.mjs", "pattern.mjs"]);
  assertRejected(early, /split threw during linked initialization/);
  const preview = path.join(root, "lexical-read-before-init-preview");
  forcePreview(early.files, preview);
  assert.deepEqual(observe(path.join(preview, entry), "result"), { error: "ReferenceError" });
}

for (const [name, declaration, reason, error] of [
  [
    "module-default-tdz",
    "const { missing: earlier = value, other: value = 9 } = {};",
    /original threw during linked initialization/,
    "ReferenceError",
  ],
  ["module-null-before-key", "const { [value]: value } = null;", /original threw/, "TypeError"],
  ["module-rest", "const { ...value } = {};", /unsupported object binding property/, null],
]) {
  if (!runCase(name)) continue;
  const source = `${declaration} export { value };`;
  const result = sequentialSplit(name, source, { [entry]: ["#0", "#1"] }, []);
  assertRejectedOrOrderKeptSlice(result, reason);
  if (error) assert.deepEqual(observe(result.files.sourceFile, "value"), { error });
}

// Calling make() twice would bind two different objects. A null argument to the
// exported function must also throw before any body statements execute.
if (runCase("once")) {
  const source = `
    function make() { const unique = {}; return {a: unique, b: unique}; }
    function read({a, "b": alias}) { var a; return a === alias; }
    function check() { const {a, b} = make(); if (a !== b) throw 1; return read({a, b}); }
    const answer = check(); export {answer, read};
  `;
  const { files, out, verdict, log } = sequentialSplit(
    "once",
    source,
    { "worker.mjs": ["#0", "#1", "#2"], [entry]: ["#3", "#4"] },
    ["worker.mjs"]
  );
  assert.equal(verdict.status, "certified", log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    const mod = await import(pathToFileURL(file).href);
    assert.equal(mod.answer, true);
    assert.equal(mod.read.length, 1);
    assert.throws(() => mod.read(null), TypeError);
    const reads = [];
    const receiver = {};
    for (const key of ["a", "b"])
      Object.defineProperty(receiver, key, {
        get() {
          assert.equal(this, receiver);
          reads.push(key);
          return 42;
        },
      });
    assert.equal(mod.read(receiver), true);
    assert.deepEqual(reads, ["a", "b"]);
  }
}

// Vary property order, aliases, quoted keys, declaration kind and the position of
// the pattern. A premature call in the same graph destructures an undefined var.
for (let seed = 0; seed < 6; seed++) {
  if (!runCase(`seed-${seed}`)) continue;
  const parameter = seed % 2 === 0;
  const keys = ["left", "right", "tail"];
  const fields = Array.from({ length: 1 + (seed % 3) }, (_, i) => {
    const key = keys[(seed + i) % keys.length];
    const shorthand = (seed + i) % 3 === 0;
    const target = shorthand ? key : `field${i}`;
    const prop = property(key, id(target));
    if (!shorthand && (seed + i) % 2 === 1) prop.key = literal(key);
    return { prop: { ...prop, shorthand }, key, target, value: seed * 10 + i };
  });
  const pattern = { type: "ObjectPattern", properties: fields.map((f) => f.prop) };
  const body = [];
  if (!parameter)
    body.push({
      type: "VariableDeclaration",
      kind: ["var", "let", "const"][seed % 3],
      declarations: [{ type: "VariableDeclarator", id: pattern, init: id("arg") }],
    });
  for (const { target, value } of fields)
    body.push({
      type: "IfStatement",
      test: { type: "BinaryExpression", operator: "!==", left: id(target), right: literal(value) },
      consequent: throws(literal("wrong binding")),
      alternate: null,
    });
  body.push(returns(literal(true)));
  const params = [parameter ? pattern : id("arg")];
  const inner = seed < 2 ? null : (seed < 4 ? arrow : functionExpression)(params, block(body));
  const source = program([
    variable("var", "input", {
      type: "ObjectExpression",
      properties: fields.map((f) => property(f.key, literal(f.value))),
    }),
    variable("const", "answer", call(id("read"), [id("input")])),
    {
      type: "FunctionDeclaration",
      id: id("read"),
      params: inner ? [id("inputArg")] : params,
      body: inner ? block([returns(call(inner, [id("inputArg")]))]) : block(body),
      async: false,
      generator: false,
    },
    exportNamed([["answer", "answer"]]),
  ]);
  const modules = { "input.mjs": ["#0"], "call.mjs": ["#1"], "read.mjs": ["#2"], [entry]: ["#3"] };
  const correct = ["input.mjs", "call.mjs", "read.mjs"];
  const { files, out, verdict, log } = sequentialSplit(`seed-${seed}`, source, modules, correct);
  assert.equal(verdict.status, "certified", log);
  assert.deepEqual(observe(files.sourceFile, "answer"), { answer: true });
  assert.deepEqual(observe(path.join(out, entry), "answer"), { answer: true });
  if (seed < 2) {
    const late = sequentialSplit(`unsafe-${seed}`, source, modules, ["call.mjs", "input.mjs", "read.mjs"]);
    assert.equal(late.verdict.status, "rejected", late.log);
    assert(!existsSync(late.out));
    const preview = path.join(root, `preview-${seed}`);
    forcePreview(late.files, preview);
    assert.deepEqual(observe(path.join(preview, entry), "answer"), { error: "TypeError" });
  }
}

if (runCase("shorthand-parameter-default")) {
  const source = "function read({a=1}) { return a; } const answer = [read({}), read({a: 0})]; export {answer};";
  const { files, out, verdict, log } = sequentialSplit("shorthand-parameter-default", source);
  assert.equal(verdict.status, "certified", log);
  assert.deepEqual(observe(files.sourceFile, "answer"), { answer: [1, 0] });
  assert.deepEqual(observe(path.join(out, entry), "answer"), { answer: [1, 0] });
}

if (runCase("rest-only-parameter")) {
  const source = "function read({...props}) { return props.a; } const answer = read({a: 3}); export {answer};";
  const result = sequentialSplit("rest-only-parameter", source, { [entry]: ["#0", "#1", "#2"] }, []);
  assertCertified(result.verdict, result.log);
  assert.deepEqual(observe(result.files.sourceFile, "answer"), { answer: 3 });
  assert.deepEqual(observe(path.join(result.out, entry), "answer"), { answer: 3 });
}

for (const [name, param] of [
  ["empty", "{}"],
  ["computed", "{[key]:value}"],
  ["nested", "{a:{b}}"],
]) {
  if (!runCase(`unsupported-parameter-${name}`)) continue;
  const source = `function read(${param}) {} read({}); export {read};`;
  const result = sequentialSplit(name, source, { [entry]: ["#0", "#1", "#2"] }, []);
  assertRejectedOrOrderKeptSlice(result, /deferred function read:/);
}

if (runCase("local-defaults")) {
  const source = `
    const run = () => {
      let calls = 0;
      const input = { get a() { calls += 1; return undefined; }, b: null };
      const { a: first = 12, b: second = 17, c: third = first + 1, create: make = () => third } = input;
      let sawTDZ = false;
      try {
        const { missing: earlier = later, other: later = 9 } = input;
      } catch (error) {
        sawTDZ = typeof error === "object";
      }
      return [calls, first, second, third, make(), make.name, sawTDZ];
    };
    const result = run();
    export { result };
  `;
  const { files, out, verdict, log } = sequentialSplit("local-defaults", source);
  assert.equal(verdict.status, "certified", log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: [1, 12, null, 13, 13, "make", true] });
  }
}
