import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dump } from "../translation/parser.mjs";
import { atReturn, duringTurn, first, payload, second, trace } from "./linked_primitive_await_fixture.mjs";

function model(source, options = {}) {
  return JSON.parse(
    execFileSync(process.env.LINKED_PRIMITIVE_AWAIT_TEST, {
      input: JSON.stringify({ source: dump(source, { includeOpaqueCaptures: true }), ...options }) + "\n",
      encoding: "utf8",
    })
  );
}

const [rejected, fulfilled] = await Promise.allSettled([first, second]);
assert.equal(rejected.status, "rejected");
assert.equal(fulfilled.status, "fulfilled");
const expected = {
  atReturn,
  duringTurn,
  afterDrain: trace,
  fresh: first !== second,
  rejectionIdentity: rejected.reason === payload,
  value: fulfilled.value,
};
assert.deepEqual(expected, {
  atReturn: "ag",
  duringTurn: "agb|",
  afterDrain: "agb|AB",
  fresh: true,
  rejectionIdentity: true,
  value: 23,
});
assert.deepEqual(
  model(readFileSync(new URL("./linked_primitive_await_fixture.mjs", import.meta.url), "utf8")),
  expected
);

for (const source of [
  "async function f() { let x = await 0; } f();",
  "async function f() { await 0; await 1; } f();",
  "async function f() { await 0; } const alias = f; alias();",
  "async function f() { await 0; } const p = [f()];",
]) {
  const result = model(source);
  assert.match(result.unknown, /deferred function f: async invocation requires Promise semantics/);
  assert.equal(result.jobs, 0);
}
assert.deepEqual(model("async function f() { await { then() {} }; } f();"), {
  unknown: "await operand requires thenable semantics",
  jobs: 0,
});
assert.deepEqual(model('async function f() { await 0; } f(); throw "stop";'), { throw: "stop", jobs: 1 });

// A reassigned binding must not lend its original cut to another callee.
assert.deepEqual(model("async function f() { await 0; } async function g() { await 1; } f = g; f();"), {
  unknown: "deferred function g: async invocation requires Promise semantics",
  jobs: 0,
});
assert.deepEqual(
  model("async function f() { await 0; } function g() { return 1; } f = g; f();", { statusOnly: true }),
  { end: "normal", jobs: 0 }
);

// Compare against an ordinary statement at the same position. The await
// operand must not run on fuel that can't reach the ordinary expression.
for (const fuel of [4, 5, 6]) {
  const options = { fuel, statusOnly: true };
  const awaited = model("async function f() { await 0; } f();", options);
  const ordinary = model("async function f() { 0; } f();", options);
  assert.equal(awaited.end, ordinary.end);
  assert.deepEqual(awaited, fuel < 6 ? { end: "fuel" } : { end: "normal", jobs: 1 });
}
