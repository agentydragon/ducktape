import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";

let trace = "";
async function singleJob() {
  trace += "s";
  await 0;
  trace += "a";
  return 11;
}
const promise = singleJob();
const atReturn = trace;
trace += "e";
const singleDuringTurn = trace;
const singleValue = await promise;
const single = { atReturn, duringTurn: singleDuringTurn, afterDrain: trace, value: singleValue };
assert.deepEqual(single, { atReturn: "s", duringTurn: "se", afterDrain: "sea", value: 11 });

trace = "";
let marker = 0;
const payload = {};
async function firstJob() {
  trace += "a";
  await 0;
  trace += "A";
  throw payload;
}
async function secondJob() {
  trace += "b";
  await 0;
  trace += "B";
  return marker;
}
const left = firstJob();
const right = secondJob();
marker = 23;
trace += "|";
const duringTurn = trace;
const [first, second] = await Promise.allSettled([left, right]);
assert.equal(first.status, "rejected");
assert.equal(second.status, "fulfilled");
const pair = {
  duringTurn,
  afterDrain: trace,
  fresh: left !== right,
  rejectionIdentity: first.reason === payload,
  value: second.value,
};
assert.deepEqual(pair, { duringTurn: "ab|", afterDrain: "ab|AB", fresh: true, rejectionIdentity: true, value: 23 });

const model = JSON.parse(execFileSync(process.env.PRIMITIVE_AWAIT_TEST, { encoding: "utf8" }));
assert.deepEqual(model, { single, pair });
