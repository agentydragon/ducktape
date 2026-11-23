import assert from "node:assert/strict";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  arrow,
  call,
  exportNamed,
  id,
  literal,
  lower,
  prepare,
  program,
  root,
  variable,
} from "./linked_programs_test_support.mjs";

const profile = path.join(root, "page.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
const entry = "entry.mjs";
function fixture(name, source) {
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source,
    provider: "",
    spec: { entry, modules: { "worker.mjs": ["#0"], [entry]: ["#1", "#2"] }, order: ["worker.mjs"], sequential: true },
  });
  const out = path.join(dir, "out");
  return { ...files, out, run: (...opts) => lower(files.sourceFile, files.specFile, out, opts).verdict };
}

for (const binding of ["ambient", "local"]) {
  const local = binding === "local";
  const read = arrow(local ? [id("window")] : [], {
    type: "UnaryExpression",
    prefix: true,
    operator: "typeof",
    argument: id("window"),
  });
  const test = fixture(
    binding,
    program([
      variable("const", "read", read),
      variable("const", "result", call(id("read"), local ? [literal(42)] : [])),
      exportNamed([["result", "result"]]),
    ])
  );
  if (!local) {
    assert.match(test.run().reason, /unsupported global window/);
    assert(!existsSync(test.out));
  }
  const verdict = test.run("--host-contract", profile);
  assert.equal(verdict.status, "certified", JSON.stringify(verdict));
  assert.equal(verdict.hostProfile, "browser-page");
  assert.equal(verdict.assumptions.length, local ? 0 : 1);
  assert.deepEqual(test.run("--host-contract", profile, "--check-output"), verdict);
  // Supply the single observation promised by the profile in this Node realm.
  globalThis.window = {};
  try {
    for (const file of [test.sourceFile, path.join(test.out, entry)]) {
      assert.equal((await import(pathToFileURL(file).href)).result, local ? "number" : "object");
    }
  } finally {
    delete globalThis.window;
  }
}

for (const [name, body, reason] of [
  ["bare", "return window;", /unsupported global window/],
  ["property", "return typeof window.document;", /unsupported global window/],
  ["other-global", "return typeof document;", /unsupported global document/],
  ["tdz", "return typeof window; let window;", /threw during linked initialization/],
]) {
  const test = fixture(name, `const read = () => { ${body} }; const result = read(); export { result };`);
  assert.match(test.run("--host-contract", profile).reason, reason);
  assert(!existsSync(test.out));
}
