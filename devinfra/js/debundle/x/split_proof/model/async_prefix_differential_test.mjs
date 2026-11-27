import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";

const core = JSON.parse(execFileSync(process.env.ASYNC_PREFIX_TEST, { encoding: "utf8" }));

let trace = "";
async function completed() {
  trace = "s";
  return 7;
}
async function abrupt() {
  trace = "x";
  throw "err";
}
const first = completed();
const atReturn = trace;
const second = completed();
const rejected = abrupt();
const afterReject = trace;
const payload = {};
async function objectRejection() {
  throw payload;
}
assert.deepEqual(core, {
  atReturn,
  afterReject,
  value: await first,
  reason: await rejected.catch((reason) => reason),
  fresh: first !== second,
  objectRejectionIdentity: (await objectRejection().catch((reason) => reason)) === payload,
  ordinaryProperties: Reflect.ownKeys(first).length,
});

trace = "";
async function suspended() {
  trace += "s";
  await 0;
  trace += "a";
}
const pending = suspended();
assert.equal(trace, "s", "the prefix must run before the call returns");
trace += "e";
assert.equal(trace, "se", "the await continuation must not run inside the caller's turn");
await pending;
assert.equal(trace, "sea");
