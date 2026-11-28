import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { parseModule } from "../../translation/scope_metadata.mjs";

const packageRoot = process.argv[2];
const manifest = JSON.parse(readFileSync(path.join(packageRoot, "package.json"), "utf8"));
assert.deepEqual(
  [manifest.name, manifest.version, manifest.exports.import.default],
  ["@humanwhocodes/retry", "0.4.3", "./dist/retrier.js"]
);
const code = readFileSync(path.join(packageRoot, manifest.exports.import.default), "utf8");
assert.equal(
  createHash("sha256").update(code).digest("hex"),
  "86a1ccc63d7cce261886d404d4e3aa38dd40bbf802d9c3e9340ca047e5e86e66"
);

const parse = (text) => parseModule(text, acorn.parse, eslintScope);
const cls = parse(code).ast.body.find((row) => row.type === "ClassDeclaration" && row.id.name === "Retrier");
assert(cls);
const member = (name, type, kind) => {
  const found = cls.body.body.find((row) => row.key?.name === name && row.type === type && row.kind === kind);
  assert(found, name);
  return found;
};
const text = ({ start, end }) => code.slice(start, end);
const processPending = member("processPending", "MethodDefinition", "method");
assert.equal(processPending.key.type, "PrivateIdentifier");
const call = member("call", "MethodDefinition", "method");
const workingUpdate = call.value.body.body.find(({ expression }) => expression?.type === "UpdateExpression");
assert.equal(text(workingUpdate), "this.#working++;");

const shared = `
let debugCalls = 0;
function debug(message) { debugCalls++; }
const Math = { min(a, b) { return a < b ? a : b; } };

class Retrier {
  #pending;
  ${text(member("working", "PropertyDefinition"))}
  ${text(member("concurrency", "PropertyDefinition"))}
  constructor(queue, concurrency) {
    this.#pending = queue;
    this.#concurrency = concurrency;
  }
  ${text(member("pending", "MethodDefinition", "get"))}
  ${text(member("working", "MethodDefinition", "get"))}
  ${text(processPending)}
  start() { return this.#processPending(); }
  bind(callback) { return () => this.#startJob(callback); }
  #startJob(callback) {
    ${text(workingUpdate)}
    return callback(this);
  }
}

`;

const source =
  shared +
  `
function makeQueue() {
  return {
    first: undefined, second: undefined, length: 2,
    shift() {
      const task = this.first;
      this.first = this.second;
      this.second = undefined;
      this.length--;
      return task;
    }
  };
}

// The detached pending arrow retains the Retrier, rather than the queue or
// the undefined receiver of the optional call. Both getters read its slots.
const queue = makeQueue();
const ordinary = new Retrier(queue, 2);
let receiverMatches = false;
let firstWorking = 0;
queue.first = ordinary.bind((receiver) => {
  receiverMatches = receiver === ordinary;
  firstWorking = receiver.working;
});
queue.second = null;
ordinary.start();
const normalDebugCalls = debugCalls;

// The first job throws after the write. The second job must still be queued.
const stoppedQueue = makeQueue();
const stopped = new Retrier(stoppedQueue, 2);
const sentinel = {};
let exactThrow = false;
let afterWrite = 0;
let secondRan = false;
stoppedQueue.first = stopped.bind((receiver) => {
  afterWrite = receiver.working;
  throw sentinel;
});
stoppedQueue.second = stopped.bind(() => { secondRan = true; });
try { stopped.start(); } catch (error) { exactThrow = error === sentinel; }
const abruptDebugCalls = debugCalls - normalDebugCalls;

export const observations = [
  receiverMatches, firstWorking, ordinary.working, ordinary.pending, normalDebugCalls,
  exactThrow, afterWrite, stopped.working, stopped.pending, secondRan, abruptDebugCalls
];
`;
assert(source.includes(text(processPending)));
assert.deepEqual(
  parse(source).scopes.globalScope.through.map(({ identifier }) => identifier.name),
  ["undefined", "undefined", "undefined"]
);
function fixture(name, source) {
  const file = path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, name + ".mjs");
  writeFileSync(file, source);
  return file;
}
function runModel(file) {
  return JSON.parse(execFileSync(process.env.RETRIER_PENDING, [file], { encoding: "utf8" }));
}
const file = fixture("retrier-pending", source);
const expected = [true, 1, 1, 0, 2, true, 1, 1, 1, false, 1];
const { observations } = await import(pathToFileURL(file).href);
assert.deepEqual(observations, expected);
const model = runModel(file);
assert.deepEqual(model, expected);

// Observe the engine throw directly rather than reading properties of the
// model's opaque error value.
const brands = [];
for (const [name, expression] of [
  ["foreign-private-method", "const fake = { start: Retrier.prototype.start }; fake.start();"],
  ["foreign-private-getter", "Retrier.prototype.working;"],
]) {
  const brand = fixture(name, shared + expression);
  await assert.rejects(import(pathToFileURL(brand).href), TypeError);
  const result = runModel(brand);
  assert.deepEqual(result, { throw: "TypeError", detail: "receiver does not have this private element" });
  brands.push(result);
}

// Keep the class and observer scope, but supply its actual kind of queue.
// This measures the next boundary; it makes no intrinsic-integrity claim.
const native = fixture(
  "native-pending",
  shared +
    `
const pending = [];
const retrier = new Retrier(pending, 1);
pending[0] = retrier.bind(() => {});
retrier.start();
export const observations = [retrier.working, retrier.pending];
`
);
assert.deepEqual((await import(pathToFileURL(native).href)).observations, [1, 0]);
const nativeModel = runModel(native);
assert.deepEqual(nativeModel, { unknown: "host function Array.prototype.shift is called" });
console.log(JSON.stringify({ observations, model, brands, nativeModel }));
