import assert from "node:assert/strict";
import path from "node:path";
import {
  arrow,
  assertCertifiedResult,
  assertRejected,
  block,
  call,
  exportNamed,
  expression,
  forcePreview,
  id,
  literal,
  lower,
  observe,
  ownerReaderSplit,
  program,
  returns,
  throws,
  variable,
} from "./testing/linked_programs.mjs";

const entry = "entry.mjs";
const owner = "owner.mjs";
const reader = "reader.mjs";
const state = "state.mjs";

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
const ordered = ownerReaderSplit("switch-ordered", source, placement, [owner, state, reader]);
assertCertifiedResult(ordered, "selected");
const disk = lower(ordered.files.sourceFile, ordered.files.specFile, ordered.out, ["--check-output"]);
assert.equal(disk.verdict.status, "certified", disk.log);

const early = ownerReaderSplit("switch-early", source, placement, [owner, reader, state]);
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
assertCertifiedResult(ownerReaderSplit("switch-generated-evaluation-order", generated), "dnsb");

for (const [name, cases, reason] of [
  ["fallthrough", "case 0: 1; case 1: return 2;", /unsupported switch fallthrough/],
  ["default-order", "default: return 0; case 1: return 1;", /unsupported nontrailing switch default/],
  ["shared-lexical", "case 0: let shared = 1; break; case 1: return shared;", /unsupported switch lexical declaration/],
  ["nested-break", "case 0: if (n === 0) break; break; default: return 1;", /unsupported nested switch break/],
]) {
  const refused = ownerReaderSplit(
    `switch-${name}`,
    `function choose(n) { switch (n) { ${cases} } return 0; }
     const result = choose(0); export { result };`
  );
  assertRejected(refused, reason);
}
