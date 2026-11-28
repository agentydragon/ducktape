import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [root] = process.argv.slice(2);
const load = (file) => import(pathToFileURL(path.join(root, file)).href);
const originalMethod = Array.prototype.join;
const descriptor = Object.getOwnPropertyDescriptor(Array.prototype, "join");
const caller = await load("caller.mjs");
const entry = await load("join.js");
if (existsSync(path.join(root, "join-implementation.mjs"))) {
  const implementation = await load("join-implementation.mjs");
  assert.equal(implementation.join, entry.default);
}
assert.equal(Object.getOwnPropertyDescriptor(originalMethod, "call"), undefined);
const normal = caller.tryJoin(["a", "b", "c"], "~");
const defaultSeparator = caller.tryJoin(["a", "b"]);
const events = [];
let replacements = 0;
const thrown = {};
const array = caller.pair(events, thrown);
let receiver = false;
let functionReceiver = false;
let separatorPassed = false;
let nullish;
let abrupt;
try {
  Object.defineProperty(Array.prototype, "join", {
    ...descriptor,
    value() {
      replacements += 1;
      return "replacement";
    },
  });
  Object.defineProperty(originalMethod, "call", {
    configurable: true,
    get() {
      events.push("call:get");
      return function (arrayArg, sep) {
        events.push("call:invoke");
        receiver = arrayArg === array;
        functionReceiver = this === originalMethod;
        separatorPassed = sep === "|";
        return Reflect.apply(originalMethod, arrayArg, [sep]);
      };
    },
  });
  // Neither of these should read .call. The nonnullish call must read it
  // after import, even though .join itself was saved during import.
  nullish = [caller.tryJoin(null, "|"), caller.tryJoin(undefined, "|")];
  abrupt = caller.tryJoin(array, "|");
} finally {
  delete originalMethod.call;
  Object.defineProperty(Array.prototype, "join", descriptor);
}
const replay = {
  exports: Object.keys(entry),
  publicIdentity: caller.join === entry.default,
  name: entry.default.name,
  arity: entry.default.length,
  normal,
  defaultSeparator,
  nullish,
  ending: abrupt.ending,
  result: abrupt.ending === "returned" ? abrupt.value : null,
  identicalThrown: abrupt.value === thrown,
  writes: caller.writes,
  events,
  replacements,
  receiver,
  functionReceiver,
  separatorPassed,
};
process.stdout.write(JSON.stringify(replay) + "\n");
