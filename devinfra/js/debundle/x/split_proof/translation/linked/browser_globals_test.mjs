import assert from "node:assert/strict";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { assertRefusedOrOrderKeptSlice, caseShard, root, threeUnitFixture } from "./harness.mjs";

const profile = path.join(root, "page.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
const entry = "entry.mjs";
const includesCase = caseShard();
const fixture = (name, source) => threeUnitFixture(name, source, "entry.mjs", true);

if (includesCase("page-reference")) {
  const test = fixture(
    "page-reference",
    "const read = () => { const pass = (window) => window; return [pass(window), window, pass(17)]; }; const result = read(); export { result };"
  );
  assertRefusedOrOrderKeptSlice(test.run(), test.out, /unsupported global window/);
  const verdict = test.run("--host-contract", profile);
  assert.equal(verdict.status, "certified", JSON.stringify(verdict));
  assert.deepEqual(
    verdict.assumptions,
    verdict.scope === "linked-slice-order-kept"
      ? []
      : ["window returns the same page object without observable effects throughout initialization"]
  );
  assert.deepEqual(test.run("--host-contract", profile, "--check-output"), verdict);

  const page = {};
  globalThis.window = page;
  try {
    for (const file of [test.sourceFile, path.join(test.out, entry)]) {
      const [passed, direct, lexical] = (await import(pathToFileURL(file).href)).result;
      assert.equal(passed, page);
      assert.equal(direct, page);
      assert.equal(lexical, 17);
    }
  } finally {
    delete globalThis.window;
  }
}

if (includesCase("page-aliases")) {
  const test = fixture(
    "page-aliases",
    "const read = () => [typeof self, typeof globalThis, self, globalThis, ((self) => self)(17)]; const result = read(); export { result };"
  );
  assertRefusedOrOrderKeptSlice(test.run(), test.out, /unsupported global self/);
  const verdict = test.run("--host-contract", profile);
  assert.equal(verdict.status, "certified", JSON.stringify(verdict));
  assert.deepEqual(
    verdict.assumptions,
    verdict.scope === "linked-slice-order-kept"
      ? []
      : [
          "typeof globalThis is object without observable effects throughout initialization",
          "typeof self is object without observable effects throughout initialization",
          "self returns the same page object without observable effects throughout initialization",
          "globalThis returns the same page object without observable effects throughout initialization",
        ]
  );
  assert.deepEqual(test.run("--host-contract", profile, "--check-output"), verdict);
  globalThis.self = globalThis;
  try {
    for (const file of [test.sourceFile, path.join(test.out, entry)]) {
      const [selfType, pageType, worker, page, lexical] = (await import(pathToFileURL(file).href)).result;
      assert.deepEqual([selfType, pageType, lexical], ["object", "object", 17]);
      assert.equal(worker, globalThis);
      assert.equal(page, globalThis);
    }
  } finally {
    delete globalThis.self;
  }
}

if (includesCase("guarded-global")) {
  const test = fixture(
    "guarded-global",
    'const read = () => typeof global !== "undefined" ? global : 17; const result = read(); export { result };'
  );
  assertRefusedOrOrderKeptSlice(test.run(), test.out, /unsupported global global/);
  const verdict = test.run("--host-contract", profile);
  assert.equal(verdict.status, "certified", JSON.stringify(verdict));
  assert.deepEqual(
    verdict.assumptions,
    verdict.scope === "linked-slice-order-kept"
      ? []
      : ["typeof global is undefined without observable effects throughout initialization"]
  );
  assert.deepEqual(test.run("--host-contract", profile, "--check-output"), verdict);
  // Node supplies a global shim and demonstrates why the browser premise is explicit.
  for (const file of [test.sourceFile, path.join(test.out, entry)]) {
    assert.equal((await import(pathToFileURL(file).href)).result, globalThis);
  }
}

for (const deferred of [false, true]) {
  if (!includesCase(deferred ? "deferred-page-error" : "read-page-error")) continue;
  const test = fixture(
    deferred ? "deferred-page-error" : "read-page-error",
    `const read = (page) => page.Error; const result = ${deferred ? "() => read(globalThis)" : "read(globalThis)"}; export { result };`
  );
  const verdict = test.run("--host-contract", profile);
  assert.equal(verdict.status, "certified", JSON.stringify(verdict));
  const assumptions =
    deferred || verdict.scope === "linked-slice-order-kept"
      ? []
      : [
          "globalThis returns the same page object without observable effects throughout initialization",
          "window.Error returns the stable host reference window.Error without observable effects throughout initialization",
        ];
  assert.deepEqual(verdict.assumptions, assumptions);
  assert.deepEqual(test.run("--host-contract", profile, "--check-output"), verdict);
  for (const file of [test.sourceFile, path.join(test.out, entry)]) {
    const { result } = await import(pathToFileURL(file).href);
    assert.equal(deferred ? result() : result, Error);
  }
}

for (const [name, body, reason] of [
  [
    "construct-page-error",
    "try { return new self.Error(); } catch { return 17; }",
    /host value window.Error is constructed/,
  ],
  ["reached-global", "try { return global; } catch { return 17; }", /ambient binding global is reached/],
  ["alias-property", "return self.document.title;", /unsupported host reference window.document.title/],
  ["volatile", "return innerWidth;", /ambient binding innerWidth is reached/],
  ["other-global", "return typeof applicationSpecific;", /unsupported global applicationSpecific/],
  ["tdz", "return typeof window; let window;", /threw during linked initialization/],
]) {
  if (!includesCase(name)) continue;
  const test = fixture(name, `const read = () => { ${body} }; const result = read(); export { result };`);
  assertRefusedOrOrderKeptSlice(test.run("--host-contract", profile), test.out, reason);
}

if (includesCase("module-try-typed-array")) {
  const test = fixture(
    "module-try-typed-array",
    "try { new Uint8Array(1); } catch {} const result = 1; export { result };"
  );
  const verdict = test.run("--host-contract", profile);
  if (verdict.status === "certified" && verdict.scope === "linked-slice-order-kept") {
    assert.equal(verdict.guarantee, "all-outcomes");
  } else {
    assert.equal(verdict.status, "rejected");
    assert.match(verdict.reason, /host value window\.Uint8Array is constructed/);
    assert(!existsSync(test.out));
  }
}

if (includesCase("bulk-browser-bindings")) {
  const test = fixture(
    "bulk-browser-bindings",
    "const read = () => [typeof document, typeof fetch, typeof process, Object.defineProperty, globalThis.Object.defineProperty, URL, (() => FileReader)]; const result = read(); export { result };"
  );
  const verdict = test.run("--host-contract", profile);
  assert.equal(verdict.status, "certified", JSON.stringify(verdict));
  assert.deepEqual(verdict.assumptions, [
    "typeof document is object without observable effects throughout initialization",
    "typeof fetch is function without observable effects throughout initialization",
    "typeof process is undefined without observable effects throughout initialization",
    "Object returns the stable host reference window.Object without observable effects throughout initialization",
    "window.Object.defineProperty returns the stable host reference window.Object.defineProperty without observable effects throughout initialization",
    "globalThis returns the same page object without observable effects throughout initialization",
    "window.Object returns the stable host reference window.Object without observable effects throughout initialization",
    "URL returns the stable host reference window.URL without observable effects throughout initialization",
  ]);
  assert.deepEqual(test.run("--host-contract", profile, "--check-output"), verdict);
  for (const file of [test.sourceFile, path.join(test.out, entry)]) {
    const [, , shim, direct, qualified, url] = (await import(pathToFileURL(file).href)).result;
    assert.deepEqual([direct, qualified, url], [Object.defineProperty, Object.defineProperty, URL]);
    assert.equal(shim, "object");
  }
}

// A method can retain the clock lookup without promising a time or a host effect.
for (const reached of [false, true]) {
  if (!includesCase(`clock-method-${reached}`)) continue;
  const test = fixture(
    `clock-method-${reached}`,
    `class Clock { read() { return Date.now(); } } const result = ${reached ? "new Clock().read()" : "42"}; export { result };`
  );
  const verdict = test.run("--host-contract", profile);
  if (reached) {
    assert.match(verdict.reason, /host function window.Date.now is called/);
    assert(!existsSync(test.out));
  } else {
    assert.equal(verdict.status, "certified", JSON.stringify(verdict));
    assert.deepEqual(verdict.assumptions, []);
    assert.deepEqual(test.run("--host-contract", profile, "--check-output"), verdict);
  }
}
