import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dump } from "../translation/parser.mjs";
import {
  afterCaller,
  afterFirst,
  afterRejection,
  afterSecond,
  first,
  payload,
  rejected,
  second,
} from "./linked_async_fixture.mjs";

function model(source, mode) {
  return JSON.parse(
    execFileSync(process.env.LINKED_ASYNC_TEST, {
      input: JSON.stringify({ source: dump(source, { includeOpaqueCaptures: true }), mode }) + "\n",
      encoding: "utf8",
    })
  );
}

const reason = rejected.catch((error) => error);
const promises = [first, second, rejected];
const source = readFileSync(new URL("./linked_async_fixture.mjs", import.meta.url), "utf8");
assert.deepEqual(model(source, "normal"), {
  afterFirst,
  afterSecond,
  afterRejection,
  afterCaller,
  firstValue: await first,
  secondValue: await second,
  fresh: new Set(promises).size === promises.length,
  rejectionIdentity: (await reason) === payload,
  noOwnKeys: promises.every((p) => Reflect.ownKeys(p).length === 0),
  sharedPrototype: promises.every((p) => Object.getPrototypeOf(p) === Promise.prototype),
});

for (const source of [
  "async function run() { await 0; } run();",
  "async function run() { for await (const item of []) {} } run();",
  "async function run(x = 1) { return x; } run();",
  "const run = async () => 1; run();",
]) {
  assert.match(model(source, "unknown").unknown, /deferred function run: async invocation requires Promise semantics/);
}
assert.match(
  model("async function run() { return { then() {} }; } run();", "unknown").unknown,
  /async object resolution requires thenable semantics/
);
