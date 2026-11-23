import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  arrow,
  block,
  call,
  catchClause,
  exportNamed,
  forcePreview,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  prepare,
  program,
  returns,
  root,
  throws,
  tryStatement,
  variable,
} from "./linked_programs_test_support.mjs";

const array = (elements) => ({ type: "ArrayExpression", elements });

function workerSplit(name, source) {
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source,
    provider: "",
    spec: { entry: "entry.mjs", modules: { "worker.mjs": ["#0"], "entry.mjs": ["#1", "#2"] }, order: ["worker.mjs"] },
  });
  const out = path.join(dir, "out");
  return { files, out, ...lower(files.sourceFile, files.specFile, out) };
}

function assertWorkerResult(name, source, expected) {
  const { files, out, verdict, log } = workerSplit(name, source);
  assert.equal(verdict.status, "certified", `${name}: ${log}`);
  assert.equal(verdict.guarantee, "normal-completion", log);
  for (const file of [files.sourceFile, path.join(out, "entry.mjs")]) {
    assert.deepEqual(observe(file, "result"), { result: expected }, name);
  }
}

function observedProgram(initializer, result) {
  return program([
    variable("const", "worker", initializer),
    variable("const", "value", result),
    exportNamed([["value", "result"]]),
  ]);
}

// An explicit undefined element is an own property. The linked array must also
// preserve nested values, ordinary Array.prototype and the length descriptor.
{
  const { files, out, verdict, log } = workerSplit(
    "dense-array",
    "const worker = () => [undefined, [1], null]; const value = worker; export { value as result };"
  );
  assert.equal(verdict.status, "certified", log);
  assert.deepEqual(lower(files.sourceFile, files.specFile, out, ["--check-output"]).verdict, verdict);
  for (const file of [files.sourceFile, path.join(out, "entry.mjs")]) {
    const namespace = await import(pathToFileURL(file).href);
    assert.deepEqual(Object.keys(namespace), ["result"]);
    const result = namespace.result();
    assert.equal(Object.getPrototypeOf(result), Array.prototype);
    assert.deepEqual(
      Object.getOwnPropertyDescriptors(result),
      Object.getOwnPropertyDescriptors([undefined, [1], null])
    );
  }
}

// Vary array lengths and compose literals, linked reads and calls in element positions.
for (const width of [0, 1, 2, 5]) {
  const variants = [
    [id("worker"), width],
    [call(arrow([], literal(false))), false],
    [array([literal(null), id("worker")]), [null, width]],
  ];
  const elements = Array.from({ length: width }, (_, i) => variants[(width + i) % variants.length]);
  const value = array(elements.map(([syntax]) => syntax));
  // Some arrays are first built only after initialization, by the exported closure.
  const expression = width % 2 === 0 ? arrow([], value) : value;
  assertWorkerResult(
    `generated-array-${width}`,
    observedProgram(literal(width), expression),
    elements.map(([, value]) => value)
  );
}

// If the first element throws, the elements that follow it cannot replace its value.
assertWorkerResult(
  "array-abrupt-order",
  `const worker = () => {
    try {
      return [(() => { throw "first"; })(), (() => { throw "later"; })()];
    } catch (error) {
      return error;
    }
  };
  const value = worker(); export { value as result };`,
  "first"
);

// Generate a pair with the same reversed schedule. A read in the first array
// element makes a caught TDZ take precedence over the second element's throw.
for (const dependent of [false, true]) {
  const first = dependent ? id("captured") : literal(19);
  const second = call(arrow([], block([throws(literal("later"))])));
  const kind = { type: "UnaryExpression", operator: "typeof", argument: id("error"), prefix: true };
  const worker = arrow(
    [],
    block([tryStatement(block([returns(array([first, second]))]), catchClause(id("error"), block([returns(kind)])))])
  );
  const spec = {
    entry: "entry.mjs",
    modules: { "captured.mjs": ["#0"], "worker.mjs": ["#1", "#2"], "entry.mjs": ["#3"] },
    order: ["worker.mjs", "captured.mjs"],
    sequential: true,
  };
  const dir = path.join(root, `array-reorder-${dependent}`);
  const files = prepare(dir, {
    source: program([
      variable("const", "captured", literal(19)),
      variable("const", "worker", worker),
      variable("const", "value", call(id("worker"))),
      exportNamed([["value", "result"]]),
    ]),
    provider: "",
    spec,
  });
  assert.deepEqual(observe(files.sourceFile, "result"), { result: "string" });
  const out = path.join(dir, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  let candidate = out;
  if (dependent) {
    assert.equal(verdict.status, "rejected", log);
    assert.match(verdict.reason, /could not prove equivalent linked executions/);
    assert(!existsSync(out));
    candidate = path.join(dir, "preview");
    forcePreview(files, candidate);
  } else {
    assert.equal(verdict.status, "certified", log);
    assert.equal(verdict.guarantee, "normal-completion", log);
  }
  assert.deepEqual(moduleEvaluationOrder(candidate, spec.entry), [...spec.order, spec.entry]);
  assert.deepEqual(observe(path.join(candidate, spec.entry), "result"), { result: dependent ? "object" : "string" });
}

for (const [kind, syntax] of [
  ["hole", "[worker, , 2]"],
  ["spread", "[0, ...[worker]]"],
]) {
  const { verdict, out } = workerSplit(
    `array-${kind}`,
    `const worker = 1; const value = ${syntax}; export { value as result };`
  );
  assert.equal(verdict.status, "rejected");
  assert.equal(verdict.reason, `statement 1: unsupported array ${kind}`);
  assert(!existsSync(out));
}

// Both modules allocate arrays with identical contents. Only reference identity
// reveals which array the facade's third public binding denotes.
for (const elements of [[], [literal(2), array([literal(null)])]]) {
  for (const dependsOnOrder of [false, true]) {
    const dir = path.join(root, `array-identity-${elements.length}-${dependsOnOrder}`);
    const spec = {
      entry: "entry.mjs",
      modules: { "left.mjs": ["#0", "#1"], "right.mjs": ["#2"], "entry.mjs": ["#3"] },
      order: ["right.mjs", "left.mjs"],
      sequential: true,
    };
    const alias = dependsOnOrder
      ? call(
          arrow(
            [],
            block([tryStatement(block([returns(id("right"))]), catchClause(null, block([returns(id("left"))])))])
          )
        )
      : id("left");
    const files = prepare(dir, {
      source: program([
        variable("const", "left", array(elements)),
        variable("const", "alias", alias),
        variable("const", "right", array(elements)),
        exportNamed(["left", "alias", "right"].map((name) => [name, name])),
      ]),
      provider: "",
      spec,
    });
    const out = path.join(dir, "out");
    const { verdict, log } = lower(files.sourceFile, files.specFile, out);
    let candidate = out;
    if (dependsOnOrder) {
      assert.equal(verdict.status, "rejected", log);
      assert.match(verdict.reason, /could not prove equivalent linked executions/);
      assert(!existsSync(out));
      candidate = path.join(dir, "preview");
      forcePreview(files, candidate);
    } else {
      assert.equal(verdict.status, "certified", log);
      assert.equal(verdict.guarantee, "normal-completion", log);
    }
    assert.deepEqual(moduleEvaluationOrder(candidate, spec.entry), [...spec.order, spec.entry]);
    for (const [file, target] of [
      [files.sourceFile, "left"],
      [path.join(candidate, spec.entry), dependsOnOrder ? "right" : "left"],
    ]) {
      const ns = await import(pathToFileURL(file).href);
      assert.deepEqual(Object.keys(ns), ["alias", "left", "right"]);
      assert.deepEqual(ns.left, ns.right);
      assert.notEqual(ns.left, ns.right);
      assert.equal(ns.alias, ns[target]);
    }
  }
}
