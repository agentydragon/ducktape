import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";

function dataIs(object, key, value, writable, enumerable, configurable) {
  const descriptor = Object.getOwnPropertyDescriptor(object, key);
  return (
    descriptor?.value === value &&
    descriptor.writable === writable &&
    descriptor.enumerable === enumerable &&
    descriptor.configurable === configurable
  );
}
const method = Object.getOwnPropertyDescriptor(Error.prototype, "toString");
const node = [
  Error !== Error.prototype,
  Object.getPrototypeOf(Error) === Function.prototype,
  Object.getPrototypeOf(Error.prototype) === Object.prototype,
  dataIs(Error, "name", "Error", false, false, true),
  dataIs(Error, "length", 1, false, false, true),
  dataIs(Error, "prototype", Error.prototype, false, false, false),
  dataIs(Error.prototype, "constructor", Error, true, false, true),
  dataIs(Error.prototype, "name", "Error", true, false, true),
  dataIs(Error.prototype, "message", "", true, false, true),
  typeof method.value === "function" && method.writable && !method.enumerable && method.configurable,
].map(String);
const model = execFileSync(process.env.ERROR_REALM_TEST, { encoding: "utf8" }).trimEnd().split("\n");
assert.deepEqual(model, node);
assert(node.every((result) => result === "true"));
