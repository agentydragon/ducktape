import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { exportNamed, importNamed, observe, program, runSplit } from "./harness.mjs";

const source = `
import { publicValue } from "./provider.mjs";
const local = "ok";
export const result = publicValue === 31 ? local : (() => { throw 0; })();
export { publicValue as live };
`;
const spec = {
  entry: "entry.mjs",
  modules: { "entry.mjs": ["#0", "#2", "#3"], "local.mjs": ["#1"] },
  order: ["local.mjs"],
  sequential: true,
};

function split(name, provider, extras) {
  return runSplit(name, { source, provider, extras, spec });
}

function checkProvider(test, expected) {
  assert.deepEqual(observe(test.files.sourceFile, Object.keys(expected)), expected);
  assert.equal(test.verdict.reason_code, "no_constructed_rule", test.log);
  assert(!existsSync(test.out));
}

for (const length of [1, 3, 7]) {
  const aliases = Array.from({ length }, (_, i) =>
    program([
      importNamed(`./relay-${i + 1}.mjs`, [[`name${i + 1}`, `local${i}`]]),
      exportNamed([[`local${i}`, i === 0 ? "publicValue" : `name${i}`]]),
    ])
  );
  const extras = Object.fromEntries(aliases.slice(1).map((source, i) => [`relay-${i + 1}.mjs`, source]));
  extras[`relay-${length}.mjs`] = `export const name${length} = 31;`;
  checkProvider(split(`chain-${length}`, aliases[0], extras), { result: "ok", live: 31 });
}

// Both graphs use the same provider cell and schedule, and both throw.
// The ordered certificate still promises that initialization completes normally.
const throwing = split("same-order-throw", "export const publicValue = 0;", {});
assert.equal(throwing.verdict.status, "rejected", throwing.log);
assert.equal(throwing.verdict.reason_code, "no_constructed_rule", throwing.log);
assert(!existsSync(throwing.out));

// Resolving provider/publicValue visits provider again at a different export.
const cycleProvider = `
import { back as forwarded } from "./relay.mjs";
export { forwarded as publicValue };
export const leaf = 31;
`;
for (const name of ["leaf", "publicValue", "missing"]) {
  const fixture = split(`cycle-${name}`, cycleProvider, {
    "relay.mjs": `import { ${name} } from "./provider.mjs"; export { ${name} as back };`,
  });
  if (name === "leaf") {
    checkProvider(fixture, { result: "ok", live: 31 });
  } else {
    assert.deepEqual(observe(fixture.files.sourceFile, "result"), { error: "SyntaxError" });
    assert.equal(fixture.verdict.status, "rejected", fixture.log);
    assert.equal(fixture.verdict.reason_code, "no_constructed_rule", fixture.log);
    assert(!existsSync(fixture.out));
  }
}
