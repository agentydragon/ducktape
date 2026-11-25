import assert from "node:assert/strict";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { root, structuralFixture } from "./harness.mjs";

const arithmetic = structuralFixture(
  "nested-number-products",
  `var marker = 1; var limit = { bytes: 3 * 128 * 256, fraction: (5 + 2) % 4 / 2,
      invalid: 0 / 0, signedZero: 0 / (0 - 1) }; export { marker, limit };`,
  ["#1"],
  ["#0", "#2"]
);
const product = arithmetic.run();
assert.equal(product.status, "certified", JSON.stringify(product));
assert.deepEqual(arithmetic.run("--check-output"), product);
for (const file of [arithmetic.sourceFile, path.join(arithmetic.out, "entry.mjs")]) {
  const { limit } = await import(pathToFileURL(file));
  assert.equal(limit.bytes, 98304);
  assert.equal(limit.fraction, 1.5);
  assert(Number.isNaN(limit.invalid));
  assert(Object.is(limit.signedZero, -0));
}

const arithmeticProfile = path.join(root, "arithmetic-profile.json");
writeFileSync(arithmeticProfile, JSON.stringify({ profile: "browser-page" }));
for (const [name, before, operand] of [
  ["coercion", "Object.prototype.valueOf = () => 3", "({})"],
  ["replaced-math", "globalThis.Math = { PI: 12 }", "Math.PI"],
  ["shadowed-math", "export const Math = { get PI() { return 12; } }", "Math.PI"],
]) {
  const unsafe = structuralFixture(
    name,
    `${before}; var value = ${operand} / 12; export { value };`,
    ["#1"],
    ["#0", "#2"]
  );
  const denied = unsafe.run("--host-contract", arithmeticProfile);
  assert.equal(denied.status, "rejected", JSON.stringify(denied));
  assert.match(denied.reason, /unit 1 is not a local allocating declaration/, name);
  assert(!existsSync(unsafe.out));
}

const regexRecord = structuralFixture(
  "move-regexp-record",
  String.raw`const RegExp = () => { throw "shadowed constructor"; };
   var patterns = { first: /\s/gu, second: /\s/gu, flag: /\p{RI}\p{RI}/u };
   export { RegExp, patterns };`,
  ["#1"],
  ["#0", "#2"]
);
const record = regexRecord.run();
assert.equal(record.status, "certified", JSON.stringify(record));
assert.deepEqual(regexRecord.run("--check-output"), record);
for (const file of [regexRecord.sourceFile, path.join(regexRecord.out, "entry.mjs")]) {
  const { patterns } = await import(pathToFileURL(file));
  assert.equal(Object.getPrototypeOf(patterns.first), RegExp.prototype);
  assert.equal(patterns.first.flags, "gu");
  assert(patterns.flag.test("🇨🇿"));
  assert.notEqual(patterns.first, patterns.second);
  patterns.first.lastIndex = 4;
  assert.equal(patterns.second.lastIndex, 0);
}

const profile = path.join(root, "browser-page.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
// Moving these initializers ahead of the patch would change the exported result.
for (const [name, patch, value] of [
  ["constructor", 'globalThis.RegExp = class { constructor() { throw "patched"; } }', 'new RegExp("a")'],
  ["getter", 'Object.defineProperty(RegExp.prototype, "flags", { get() { return "patched"; } })', "/a/g.flags"],
  ["method", "RegExp.prototype.test = () => false", '/a/.test("a")'],
]) {
  const effect = structuralFixture(
    "regexp-record-" + name,
    `${patch}; var patterns = { literal: /a/, result: ${value} }; export { patterns };`,
    ["#1"],
    ["#0", "#2"]
  );
  const denied = effect.run("--host-contract", profile);
  assert.equal(denied.status, "rejected", JSON.stringify(denied));
  assert.match(denied.reason, /unit 1 is not a local allocating declaration/);
  assert(!existsSync(effect.out));
}
