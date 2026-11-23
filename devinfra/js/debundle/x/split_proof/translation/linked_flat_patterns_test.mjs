import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  arrow,
  block,
  call,
  exportNamed,
  forcePreview,
  functionExpression,
  id,
  literal,
  observe,
  program,
  property,
  returns,
  root,
  sequentialSplit,
  throws,
  variable,
} from "./testing/linked_programs.mjs";

const entry = "entry.mjs";
// Calling make() twice would bind two different objects. A null argument to the
// exported function must also throw before any body statements execute.
{
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

for (const [name, param] of [
  ["empty", "{}"],
  ["computed", "{[key]:value}"],
  ["nested", "{a:{b}}"],
  ["default", "{a=1}"],
  ["rest", "{...props}"],
]) {
  const source = `function read(${param}) {} export {read};`;
  const result = sequentialSplit(name, source, { [entry]: ["#0", "#1"] }, []);
  assert.equal(result.verdict.status, "rejected", result.log);
  assert(!existsSync(result.out));
}
