import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  assertCertified,
  assertRejectedOrOrderKeptSlice,
  block,
  call,
  conditional,
  exportNamed,
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

// A call before the function declaration evaluates against the hoisted module
// cell. A default parameter can read that same public function object.
{
  const source = `
    const first = identify();
    function identify(value = identify) { return value; }
    export { first, identify };
  `;
  const order = ["call.mjs", "declaration.mjs"];
  const { files, out, verdict, log } = sequentialSplit(
    "hoisted-self",
    source,
    { "call.mjs": ["#0"], "declaration.mjs": ["#1"], [entry]: ["#2"] },
    order
  );
  assertCertified(verdict, log);
  assert.deepEqual(moduleEvaluationOrder(out, entry), [...order, entry]);
  const original = await import(pathToFileURL(files.sourceFile).href);
  const emitted = await import(pathToFileURL(path.join(out, entry)).href);
  for (const namespace of [original, emitted]) {
    assert.deepEqual(Object.keys(namespace), ["first", "identify"]);
    assert.equal(namespace.first, namespace.identify);
  }
  assert.equal(String(original.identify), String(emitted.identify));
  assert.equal(lower(files.sourceFile, files.specFile, out, ["--check-output"]).verdict.status, "certified");
}

// A copied provider uses the same parser and may declare its public function inline.
{
  const source = 'import { read as load } from "./provider.mjs"; export const result = load();';
  const provider = 'export function read() { return "provided"; }';
  const { files, out, verdict, log } = sequentialSplit(
    "inline-provider",
    source,
    { [entry]: ["#0", "#1"] },
    [],
    provider
  );
  assert.equal(verdict.status, "certified", log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: "provided" });
  }
}

function declaration(name, params, body) {
  return { type: "FunctionDeclaration", id: id(name), params, body: block(body), async: false, generator: false };
}

// Names, captured values and the branch selecting a second declaration vary.
// A function is available before its declaration runs, but the captured
// lexical binding is uninitialized if the entire call moves before it.
for (const [seed, value] of ["initialized", 42].entries()) {
  const captured = `captured${seed}`;
  const dispatch = `dispatch${seed}`;
  const read = `read${seed}`;
  const take = call(id(read));
  const fallback = literal(null);
  const arms = seed === 0 ? [take, fallback] : [fallback, take];
  const source = program([
    variable("const", captured, literal(value)),
    variable("const", "result", call(id(dispatch), [literal(seed === 0)])),
    declaration(dispatch, [id("mode")], [returns(conditional(id("mode"), ...arms))]),
    declaration(read, [], [returns(id(captured))]),
    exportNamed([["result", "result"]]),
  ]);
  const modules = {
    "value.mjs": ["#0"],
    "call.mjs": ["#1"],
    "dispatch.mjs": ["#2"],
    "read.mjs": ["#3"],
    [entry]: ["#4"],
  };
  for (const movedTooSoon of [false, true]) {
    const order = movedTooSoon ? ["call.mjs", "value.mjs"] : ["value.mjs", "call.mjs"];
    order.push("dispatch.mjs", "read.mjs");
    const { dir, files, out, verdict, log } = sequentialSplit(
      `generated-${seed}-${movedTooSoon}`,
      source,
      modules,
      order
    );
    assert.deepEqual(observe(files.sourceFile, "result"), { result: value });
    let candidate = out;
    if (movedTooSoon) {
      assert.equal(verdict.status, "rejected", log);
      assert.equal(verdict.reason, "split threw during linked initialization", log);
      assert(!existsSync(out));
      candidate = path.join(dir, "preview");
      forcePreview(files, candidate);
    } else {
      assertCertified(verdict, log);
    }
    assert.deepEqual(moduleEvaluationOrder(candidate, entry), [...order, entry]);
    assert.deepEqual(
      observe(path.join(candidate, entry), "result"),
      movedTooSoon ? { error: "ReferenceError" } : { result: value }
    );
  }
}

{
  const source = "function read() { return 3; } read = () => 4; export { read };";
  const { files, out, verdict, log } = sequentialSplit(
    "rebind",
    source,
    { "read.mjs": ["#0", "#1"], [entry]: ["#2"] },
    ["read.mjs"]
  );
  assertCertified(verdict, log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "read"), { read: 4 });
  }
}

{
  const source = "function read() { { function nested() { return 3; } return nested(); } } read(); export { read };";
  const { out, verdict, log } = sequentialSplit("block-local", source, { [entry]: ["#0", "#1", "#2"] }, []);
  assertRejectedOrOrderKeptSlice({ verdict, log, out }, /unsupported statement FunctionDeclaration/);
}
