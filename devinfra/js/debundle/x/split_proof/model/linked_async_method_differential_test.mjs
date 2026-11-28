import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { dump } from "../translation/parser.mjs";
import { first, second, last, payload, trace } from "./linked_async_method_fixture.mjs";

function model(source) {
  return JSON.parse(
    execFileSync(process.env.LINKED_ASYNC_METHOD_TEST, {
      input: JSON.stringify({ source: dump(source, { includeOpaqueCaptures: true }) }) + "\n",
      encoding: "utf8",
    })
  );
}

const beforeDrain = trace;
const [ret, threw, returnedAwait] = await Promise.allSettled([first, second, last]);
assert.equal(ret.status, "fulfilled");
assert.equal(threw.status, "rejected");
assert.equal(returnedAwait.status, "fulfilled");
const expected = {
  beforeDrain,
  afterDrain: trace,
  result: ret.value,
  sameRejection: threw.reason === payload,
  awaited: returnedAwait.value,
};
assert.deepEqual(expected, {
  beforeDrain: "ab",
  afterDrain: "abAB",
  result: 42,
  sameRejection: true,
  awaited: "at-call",
});
assert.deepEqual(model(readFileSync(new URL("./linked_async_method_fixture.mjs", import.meta.url), "utf8")), expected);

// The method name does not select a body; replacing the property with a
// different deferred method of the same name must not lend it a cut.
const replaced = model(`
  class Good { async run() { return await 1; } }
  class Other { async run() { return await Promise.resolve(2); } }
  const good = new Good();
  const other = new Other();
  good.run = other.run;
  const result = good.run();
`);
assert.match(replaced.unknown, /deferred function run: async invocation requires Promise semantics/);
assert.equal(replaced.jobs, 0);
assert.deepEqual(replaced.bodyRefusals, ["async method await operand: unsupported global Promise"]);

assert.deepEqual(model(`class C { async run() { return await { then() {} }; } } const c = new C(); c.run();`), {
  unknown: "await operand requires thenable semantics",
  jobs: 0,
  bodyRefusals: [],
});

// Replay the exact public input with the empty host profile. Property call
// dispatch reaches the deferred body; its ambient Promise lookup is the
// first body-lowering refusal, before any then or console participation.
const saved = model(readFileSync(process.env.SAVED_ASYNC_METHOD, "utf8"));
assert.match(saved.unknown, /deferred function run: async invocation requires Promise semantics/);
assert.equal(saved.jobs, 0);
assert.deepEqual(saved.bodyRefusals, ["async method await operand: unsupported global Promise"]);

// The ambient resolve property is observable in the unchanged program.
// Substituting its intrinsic by spelling would incorrectly hide this change.
const savedUrl = pathToFileURL(path.resolve(process.env.SAVED_ASYNC_METHOD)).href;
const runOriginal = (preamble) =>
  execFileSync(
    process.execPath,
    ["--input-type=module", "-e", `${preamble} await import(process.argv[1]);`, savedUrl],
    {
      encoding: "utf8",
    }
  );
assert.equal(runOriginal(""), "ok\n");
assert.equal(
  runOriginal('Promise.resolve = function () { return this === Promise ? "replacement" : "wrong receiver"; };'),
  "replacement\n"
);
