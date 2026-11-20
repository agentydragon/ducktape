import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { inspectCalls } from "./call_facts.mjs";

const source = `
class Result {
  own;
  constructor(arg) { this.own = arg.value; this.extra = arg.missing; }
}
function factory(arg) { return new Result(arg); }
const a = factory({value: 1});
const b = factory({value: obj.value, missing: 2});
`;
const report = inspectCalls(source, "factory");
assert.equal(report.calls.length, 2);
assert.deepEqual(report.constructors[0].argument_reads, ["missing", "value"]);
assert.deepEqual(report.constructors[0].receiver_writes_without_field, ["extra"]);
assert.deepEqual(report.calls[0].missing_constructor_reads, ["missing"]);
assert(report.calls[0].uncertainties.includes("argument_may_read_inherited_property"));
assert(report.calls[0].uncertainties.includes("receiver_write_may_use_prototype"));
assert(report.calls[1].uncertainties.includes("argument_operations_unanalyzed"));
assert(report.calls[1].uncertainties.includes("reordered_execution_unanalyzed"));
const quoted = inspectCalls(source.replace("{value: 1}", "{value: 1, 'missing': 3}"), "factory");
assert.deepEqual(quoted.calls[0].missing_constructor_reads, []);
const spread = inspectCalls(source.replace("{value: 1}", "{value: 1, ...more}"), "factory");
assert.equal(spread.calls[0].missing_constructor_reads, null);
assert(spread.calls[0].uncertainties.includes("argument_shape_unknown"));
assert(spread.calls[0].uncertainties.includes("argument_may_read_inherited_property"));
// A shadowed reference is not a call to the selected helper.
assert.equal(inspectCalls(source + "function x(factory) { return factory({}); }", "factory").calls.length, 2);
assert.equal(inspectCalls(source + "factory = other;", "factory").helper_binding_writes, 1);
assert.throws(() => inspectCalls("import {factory} from 'x'; const a = factory({});", "factory"));
assert.throws(() => inspectCalls("const factory = () => 1; const a = factory();", "factory"));
assert.throws(() => inspectCalls("function factory() {} function factory() {}", "factory"));
const tdz = inspectCalls(
  "const a = factory({}); class Result {} function factory(x) { return new Result(x); }",
  "factory"
);
assert(tdz.calls[0].uncertainties.includes("constructor_initialization_unproven"));
const mutatedClass = inspectCalls(source + "Result = other;", "factory");
assert(mutatedClass.calls[0].uncertainties.includes("constructor_binding_written"));
assert(inspectCalls(source, "factory").limitations.some((x) => x.includes("aliases")));

// An inherited getter changes a real run even though a fresh object is passed.
const prelude = `class Result { constructor(arg) { this.extra = arg.missing; } }
function factory(arg) { return new Result(arg); }`;
const mutation = `Object.defineProperty(Object.prototype, "missing", {get() { console.log("getter"); }, configurable:true});`;
const call = "const a = factory({});";
function run(code) {
  const result = spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout;
}
assert.equal(run(`${prelude}\n${mutation}\n${call}`), "getter\n");
assert.equal(run(`${prelude}\n${call}\n${mutation}`), "");
