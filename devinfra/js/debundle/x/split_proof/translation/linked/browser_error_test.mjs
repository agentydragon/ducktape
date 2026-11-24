import assert from "node:assert/strict";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { forcePreview, root, threeUnitFixture as fixture } from "./harness.mjs";

const profile = path.join(root, "page.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
const entry = "entry.mjs";

globalThis.window = globalThis;
globalThis.self = globalThis;
try {
  const normal = fixture(
    "construct-native-error",
    `const read = () => {
      const page = window;
      const temporary = { first: new page.Error("message") };
      const second = new Error("message");
      try { throw temporary.first; } catch (caught) {
        return [temporary.first === caught, temporary.first === second, caught.message, second.message, typeof new self.Error()];
      }
    };
    const result = read();
    export { result };`
  );
  assert.match(normal.run().reason, /unsupported global window/);
  const verdict = normal.run("--host-contract", profile);
  assert.equal(verdict.status, "certified", JSON.stringify(verdict));
  assert(
    verdict.assumptions.includes(
      "Error returns the stable host reference window.Error without observable effects throughout initialization"
    )
  );
  assert(
    verdict.assumptions.includes(
      "window.Error returns the stable host reference window.Error without observable effects throughout initialization"
    )
  );
  assert.equal(verdict.assumptions.filter((premise) => premise.includes("the intrinsic Error constructor")).length, 1);
  assert.deepEqual(normal.run("--host-contract", profile, "--check-output"), verdict);
  for (const file of [normal.sourceFile, path.join(normal.out, entry)]) {
    assert.deepEqual((await import(pathToFileURL(file).href)).result, [true, false, "message", "message", "object"]);
  }

  const shadowed = fixture(
    "construct-shadowed-error",
    `const read = () => {
      const Error = function () { return { message: "local" }; };
      return new Error().message;
    };
    const result = read();
    export { result };`
  );
  const shadowedVerdict = shadowed.run("--host-contract", profile);
  assert.equal(shadowedVerdict.status, "certified", JSON.stringify(shadowedVerdict));
  assert.deepEqual(shadowedVerdict.assumptions, []);
  for (const file of [shadowed.sourceFile, path.join(shadowed.out, entry)]) {
    assert.equal((await import(pathToFileURL(file).href)).result, "local");
  }

  const throwingArgument = fixture(
    "error-argument-before-construction",
    `const read = () => {
      const fail = () => { throw 17; };
      try { return new window.Error(fail()); } catch (reason) { return reason; }
    };
    const result = read();
    export { result };`
  );
  const throwingVerdict = throwingArgument.run("--host-contract", profile);
  assert.equal(throwingVerdict.status, "certified", JSON.stringify(throwingVerdict));
  assert.deepEqual(throwingVerdict.assumptions, [
    "window returns the same page object without observable effects throughout initialization",
    "window.Error returns the stable host reference window.Error without observable effects throughout initialization",
  ]);
  for (const file of [throwingArgument.sourceFile, path.join(throwingArgument.out, entry)]) {
    assert.equal((await import(pathToFileURL(file).href)).result, 17);
  }

  const stack = fixture(
    "read-native-error-stack",
    `const read = () => {
      const reason = new window.Error("message");
      try { return reason["stack"]; } catch { return "caught"; }
    };
    const result = read();
    export { result };`
  );
  assert.match(
    stack.run("--host-contract", profile).reason,
    /native Error.stack depends on engine behavior and source locations/
  );
  assert(!existsSync(stack.out));
  const preview = path.join(path.dirname(stack.sourceFile), "preview");
  forcePreview(stack, preview);
  const originalStack = (await import(pathToFileURL(stack.sourceFile).href)).result;
  const reorderedStack = (await import(pathToFileURL(path.join(preview, entry)).href)).result;
  assert.match(originalStack, /source\.mjs/);
  assert.match(reorderedStack, /worker\.mjs/);
  assert.notEqual(originalStack, reorderedStack);
} finally {
  delete globalThis.self;
  delete globalThis.window;
}

for (const [name, body, reason] of [
  [
    "escaped-error",
    'return { nested: { error: new self.Error("captured") } };',
    /native Error escapes linked initialization with an unmodeled stack/,
  ],
  [
    "closed-over-error",
    'const error = new Error("captured"); return () => error;',
    /native Error escapes linked initialization with an unmodeled stack/,
  ],
  [
    "error-options",
    'return new self.Error("message", { cause: 17 }).message;',
    /native Error construction requires an omitted, undefined, or string message and no options/,
  ],
  ["inherited-error-message", "return new Error().message;", /inherited Error.message is inspected/],
  [
    "error-write",
    'const error = new Error("message"); error.message = "changed"; return error.message;',
    /a property of an engine error/,
  ],
]) {
  const test = fixture(name, `const read = () => { ${body} }; const result = read(); export { result };`);
  const verdict = test.run("--host-contract", profile);
  assert.equal(verdict.status, "rejected", JSON.stringify(verdict));
  assert.equal(verdict.reason_code, "probe_unknown", JSON.stringify(verdict));
  assert.match(verdict.reason, reason);
  assert(!existsSync(test.out));
}
