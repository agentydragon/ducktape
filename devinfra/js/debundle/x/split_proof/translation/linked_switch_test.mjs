import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  arrow,
  block,
  call,
  exportNamed,
  expression,
  forcePreview,
  id,
  literal,
  lower,
  observe,
  prepare,
  program,
  returns,
  root,
  throws,
  variable,
} from "./linked_programs_test_support.mjs";

const entry = "entry.mjs";
const owner = "owner.mjs";
const reader = "reader.mjs";
const state = "state.mjs";

function split(
  name,
  source,
  modules = { [owner]: ["#0"], [reader]: ["#1"], [entry]: ["#2"] },
  order = [owner, reader]
) {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider: "", spec: { entry, modules, order, sequential: true } });
  const out = path.join(dir, "out");
  return { files, dir, out, ...lower(files.sourceFile, files.specFile, out) };
}

function assertCertified({ files, verdict, log, out }, result) {
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "normal-completion", log);
  assert.deepEqual(observe(files.sourceFile, "result"), { result });
  assert.deepEqual(observe(path.join(out, entry), "result"), { result });
}

function assertRejected({ verdict, log, out }, reason) {
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, reason, log);
  assert(!existsSync(out));
}

const source = `function choose() {
  switch (mode) {
    case "active": return "selected";
    default: return "empty";
  }
}
var mode;
mode = "active";
const result = choose();
export { result };`;
const placement = { [owner]: ["#0"], [state]: ["#1", "#2"], [reader]: ["#3"], [entry]: ["#4"] };
const ordered = split("switch-ordered", source, placement, [owner, state, reader]);
assertCertified(ordered, "selected");
const disk = lower(ordered.files.sourceFile, ordered.files.specFile, ordered.out, ["--check-output"]);
assert.equal(disk.verdict.status, "certified", disk.log);

const early = split("switch-early", source, placement, [owner, reader, state]);
assertRejected(early, /probe executions completed normally with different final states/);
const preview = path.join(early.dir, "preview");
forcePreview(early.files, preview);
assert.deepEqual(observe(path.join(preview, entry), "result"), { result: "empty" });

const append = (suffix) => ({
  type: "AssignmentExpression",
  operator: "=",
  left: id("trace"),
  right: { type: "BinaryExpression", operator: "+", left: id("trace"), right: literal(suffix) },
});
const mark = (suffix, value) => ({ type: "SequenceExpression", expressions: [append(suffix), literal(value)] });
const clause = (test, ...consequent) => ({ type: "SwitchCase", test, consequent });
const generated = program([
  variable(
    "const",
    "choose",
    arrow(
      [],
      block([
        variable("let", "trace", literal("")),
        {
          type: "SwitchStatement",
          discriminant: mark("d", "1"),
          cases: [
            clause(mark("n", 1), throws(literal("coerced case test"))),
            clause(mark("s", "1"), expression(append("b")), { type: "BreakStatement", label: null }),
            clause(mark("x", "1"), throws(literal("evaluated a later test"))),
            clause(null, throws(literal("entered default"))),
          ],
        },
        returns(id("trace")),
      ])
    )
  ),
  variable("const", "result", call(id("choose"))),
  exportNamed([["result", "result"]]),
]);
assertCertified(split("switch-generated-evaluation-order", generated), "dnsb");

for (const [name, cases, reason] of [
  ["fallthrough", "case 0: 1; case 1: return 2;", /unsupported switch fallthrough/],
  ["default-order", "default: return 0; case 1: return 1;", /unsupported nontrailing switch default/],
  ["shared-lexical", "case 0: let shared = 1; break; case 1: return shared;", /unsupported switch lexical declaration/],
  ["nested-break", "case 0: if (n === 0) break; break; default: return 1;", /unsupported nested switch break/],
]) {
  const refused = split(
    `switch-${name}`,
    `function choose(n) { switch (n) { ${cases} } return 0; }
     const result = choose(0); export { result };`
  );
  assertRejected(refused, reason);
}
