import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dump } from "../translation/parser.mjs";
import { atReturn, duringTurn, first, payload, second, trace } from "./linked_primitive_await_fixture.mjs";

function model(source) {
  return JSON.parse(
    execFileSync(process.env.LINKED_PRIMITIVE_AWAIT_TEST, {
      input: JSON.stringify({ source: dump(source, { includeOpaqueCaptures: true }) }) + "\n",
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
  atReturn: "a",
  duringTurn: "ab|",
  afterDrain: "ab|AB",
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
