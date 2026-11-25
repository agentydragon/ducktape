import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  assertCertified,
  assertRejectedOrOrderKeptSlice,
  block,
  call,
  exportNamed,
  forcePreview,
  id,
  literal,
  moduleEvaluationOrder,
  observe,
  program,
  returns,
  sequentialSplit,
  throws,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";

{
  const source = `
    var seen = "", outer = 7;
    const before = captured;
    try {
      seen += typeof shadow;
      let shadow = 10;
    } catch {
      seen += "tdz";
    }
    try {
      var captured = 41;
      throw 1;
    } catch (caught) {
      let outer = caught;
      captured += outer;
    } finally {
      seen += "!";
    }
    export { before, captured, outer, seen };
  `;
  const { files, out, verdict, log } = sequentialSplit("module-try", source, {
    "worker.mjs": ["#0", "#1", "#2", "#3", "#4"],
    [entry]: ["#5"],
  });
  assert.equal(verdict.status, "certified", log);
  const exports = ["before", "captured", "outer", "seen"];
  const expected = { before: undefined, captured: 42, outer: 7, seen: "tdz!" };
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, exports), expected);
  }
}

{
  const source = `
    const before = selected;
    let outer = 2;
    if (outer) {
      const outer = 7;
      var selected = () => outer;
    } else throw "wrong branch";
    if (false) var skipped = selected();
    export const result = [before === undefined, selected(), outer, skipped === undefined];
  `;
  const { files, out, verdict, log } = sequentialSplit("module-if", source, {
    "worker.mjs": ["#0", "#1", "#2", "#3"],
    [entry]: ["#4"],
  });
  assertCertified(verdict, log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: [true, 7, 2, true] });
  }
}

for (const [name, body, expected] of [
  [
    "var-hoist-and-lexical-scope",
    `
      const before = chosen;
      const label = "outer";
      if ("choose") var chosen = 41, copy = chosen;
      if (0) var dead = (() => { throw "unexpected initializer"; })();
      if (true) {
        const label = "inner";
        if (label !== "inner") throw label;
      }
      if (true);
      return [before === undefined, copy, dead === undefined, label];
    `,
    [true, 41, true, "outer"],
  ],
  [
    "abrupt-branch",
    `
      const choose = (mode) => {
        try {
          if (mode) return 5;
          else throw 7;
        } catch (value) {
          return value;
        }
      };
      return [choose(true), choose(false)];
    `,
    [5, 7],
  ],
]) {
  const source = `const run = () => { ${body} }; export const result = run();`;
  const { files, out, verdict, log } = sequentialSplit(name, source, { "run.mjs": ["#0"], [entry]: ["#1"] }, [
    "run.mjs",
  ]);
  assertCertified(verdict, log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: expected });
  }
}

for (const [seed, value] of ["value", 42].entries()) {
  const captured = `captured${seed}`;
  const choose = `choose${seed}`;
  const branch = returns(id(captured));
  const dead = throws(literal("unexpected branch"));
  const source = program([
    variable("const", captured, literal(value)),
    variable("const", "result", call(id(choose), [literal(seed === 0)])),
    {
      type: "FunctionDeclaration",
      id: id(choose),
      params: [id("mode")],
      body: block([
        {
          type: "IfStatement",
          test: id("mode"),
          consequent: seed === 0 ? branch : dead,
          alternate: seed === 0 ? dead : branch,
        },
      ]),
      async: false,
      generator: false,
    },
    exportNamed([["result", "result"]]),
  ]);
  const modules = { "value.mjs": ["#0"], "call.mjs": ["#1"], "choose.mjs": ["#2"], [entry]: ["#3"] };
  for (const early of [false, true]) {
    const order = early ? ["call.mjs", "value.mjs", "choose.mjs"] : ["value.mjs", "call.mjs", "choose.mjs"];
    const { dir, files, out, verdict, log } = sequentialSplit(`reordered-${seed}-${early}`, source, modules, order);
    assert.deepEqual(observe(files.sourceFile, "result"), { result: value });
    let candidate = out;
    if (early) {
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
      early ? { error: "ReferenceError" } : { result: value }
    );
  }
}

const { out, verdict, log } = sequentialSplit(
  "unsupported-dead-branch",
  "const run = () => { if (false) do {} while (false); return 1; }; export const result = run();",
  { "run.mjs": ["#0"], [entry]: ["#1"] },
  ["run.mjs"]
);
assertRejectedOrOrderKeptSlice({ verdict, log, out }, /unsupported statement DoWhileStatement/);
