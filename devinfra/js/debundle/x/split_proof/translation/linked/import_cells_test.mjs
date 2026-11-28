import assert from "node:assert/strict";
import { existsSync, readFileSync, renameSync, symlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { split as construct } from "../testing/split.mjs";
import {
  exportNamed,
  importNamed,
  lower,
  moduleEvaluationOrder,
  observe,
  prepare,
  program,
  runSplit,
} from "./harness.mjs";

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

const primitiveSource = `
import { publicValue } from "./provider.mjs";
const local = "ok";
export const result = publicValue === 31 ? local : "not-ok";
export { publicValue as live };
`;
const primitiveProvider = "export const publicValue = 31;";

function primitiveFixture(name, options = {}) {
  const dir = path.join(process.env.TEST_TMPDIR, name);
  const files = prepare(dir, { source: primitiveSource, provider: primitiveProvider, spec, ...options });
  const out = path.join(dir, "out");
  const run = (options = {}) =>
    construct({ source: files.sourceFile, spec: files.specFile, out, require: "all-outcomes", ...options });
  return { ...files, dir, out, run };
}

function assertProviderConstruction(fixture, localFile = "local.mjs") {
  const result = fixture.run();
  assert.equal(result.status, "certified", result.log);
  assert.equal(result.guarantee, "all-outcomes");
  assert.equal(result.scope, "shared-static-provider");
  assert.equal(result.verdict.strategy, "compiled-constructor");
  assert.deepEqual(result.files, ["entry.mjs", localFile, "provider.mjs"]);
  assert.deepEqual(moduleEvaluationOrder(fixture.out, spec.entry), ["provider.mjs", localFile, "entry.mjs"]);
  assert.deepEqual(readFileSync(path.join(fixture.out, "provider.mjs")), readFileSync(fixture.providerFile));
  assert.deepEqual(observe(fixture.sourceFile, ["result", "live"]), { result: "ok", live: 31 });
  assert.deepEqual(observe(path.join(fixture.out, spec.entry), ["result", "live"]), { result: "ok", live: 31 });
  const reread = fixture.run({ checkOutput: true });
  assert.equal(reread.status, "certified", reread.log);
  const { receipt } = result.verdict;
  assert.equal(receipt.operation, "published");
  assert.equal(receipt.graph.public_entry.route, spec.entry);
  assert.deepEqual(receipt.graph.public_entry.exports, ["result", "live"]);
  assert.equal(receipt.graph.public_entry.live_cells, "same-linked-cells");
  assert.deepEqual(receipt.graph.source.evaluation_order, ["provider.mjs", "entry.mjs"]);
  assert.deepEqual(receipt.graph.output.evaluation_order, ["provider.mjs", localFile, "entry.mjs"]);
  assert.equal(receipt.client.coverage, "unchecked");
  assert.equal(receipt.deployment.claim, "none");
  assert.deepEqual(receipt.deployment.fixed_files, ["provider.mjs"]);
  assert.deepEqual(reread.verdict, { ...result.verdict, receipt: { ...receipt, operation: "checked-output" } });
}

function assertRefused(fixture, message, options) {
  const refusal = fixture.run(options);
  assert.equal(refusal.status, "refused", refusal.log);
  assert.match(refusal.reason, message);
  assert.equal(refusal.verdict.receipt, undefined);
}

const oneProvider = primitiveFixture("one-static-provider");
assertProviderConstruction(oneProvider);
// Moving the public initializer ahead of its local input would make an
// ordinary client of the entry encounter the uninitialized binding.
const wrongOrder = path.join(oneProvider.dir, "wrong-order.json");
writeFileSync(
  wrongOrder,
  JSON.stringify({ ...spec, modules: { "entry.mjs": ["#0", "#1", "#3"], "local.mjs": ["#2"] } })
);
assertRefused(oneProvider, /static provider:/, {
  spec: wrongOrder,
  out: path.join(oneProvider.dir, "wrong-order"),
});
assertRefused(oneProvider, /static provider:/, { spec: wrongOrder, allowExistingOut: true });
const stagedProvider = path.join(oneProvider.out, "provider.mjs");
writeFileSync(stagedProvider, primitiveProvider + "\n// changed bytes, same syntax\n");
assertRefused(oneProvider, /provider provider\.mjs differs/, { checkOutput: true });
writeFileSync(stagedProvider, primitiveProvider);
renameSync(stagedProvider, path.join(oneProvider.out, "unexpected.mjs"));
assertRefused(oneProvider, /output module set differs.*missing: provider\.mjs.*unexpected: unexpected\.mjs/, {
  checkOutput: true,
});
renameSync(path.join(oneProvider.out, "unexpected.mjs"), stagedProvider);

const equalCells = primitiveFixture("equal-valued-cells", {
  provider: primitiveProvider + "\nexport const decoy = 31;\n",
});
assertProviderConstruction(equalCells);
const outputEntry = path.join(equalCells.out, spec.entry);
const entryText = readFileSync(outputEntry, "utf8");
const wrongCell = entryText.replace(/import\s*\{\s*publicValue\s*\}/, "import { decoy as publicValue }");
assert.notEqual(wrongCell, entryText);
writeFileSync(outputEntry, wrongCell);
assert.deepEqual(observe(outputEntry, ["result", "live"]), { result: "ok", live: 31 });
assertRefused(equalCells, /output module entry\.mjs differs/, { checkOutput: true });

// A moved read and the entry's public alias share the unchanged provider.
const nested = "nested/local.mjs";
const nestedRead = primitiveFixture("nested-provider-read", {
  source: primitiveSource.replace('const local = "ok";', 'const local = publicValue === 31 ? "ok" : "not-ok";'),
  spec: { ...spec, modules: { "entry.mjs": spec.modules["entry.mjs"], [nested]: ["#1"] }, order: [nested] },
});
assertProviderConstruction(nestedRead, nested);
assert.match(readFileSync(path.join(nestedRead.out, nested), "utf8"), /from "\.\.\/provider\.mjs"/);

for (const [name, modules] of [
  ["wrong-import-owner", { "entry.mjs": ["#2", "#3"], "local.mjs": ["#0", "#1"] }],
  ["duplicate-selector", { ...spec.modules, "local.mjs": ["#1", "local"] }],
  ["provider-collision", { "entry.mjs": ["#0", "#2", "#3"], "provider.mjs": ["#1"] }],
]) {
  const invalidSpec = path.join(oneProvider.dir, name + ".json");
  writeFileSync(invalidSpec, JSON.stringify({ ...spec, modules }));
  assertRefused(oneProvider, /static provider:/, { spec: invalidSpec, out: path.join(oneProvider.dir, name) });
  assertRefused(oneProvider, /static provider:/, { spec: invalidSpec, allowExistingOut: true });
}

const aliasedProvider = primitiveFixture("aliased-provider");
const otherProvider = path.join(aliasedProvider.dir, "other.mjs");
renameSync(aliasedProvider.providerFile, otherProvider);
symlinkSync(otherProvider, aliasedProvider.providerFile);
assertRefused(aliasedProvider, /unsupported module alias: provider\.mjs/);

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

// A call and explicit throw are outside the primitive provider constructor.
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

// Two local imports of the same provider export remain the same live cell.
const auditSource = `import { publicValue as first, publicValue as second } from "./provider.mjs";
const local = first === second;
export const result = local;
export { first as live };`;
const auditSpec = {
  entry: "entry.mjs",
  modules: { "entry.mjs": ["#0", "#2", "#3"], "local.mjs": ["#1"] },
  order: ["local.mjs"],
  sequential: true,
};
const audit = runSplit("same-provider-alias-audit", {
  source: auditSource,
  provider: "export const publicValue = 31;",
  spec: auditSpec,
});
assert.equal(audit.verdict.status, "certified", audit.log);
assert.equal(audit.verdict.guarantee, "all-outcomes", audit.log);
assert.deepEqual(observe(audit.files.sourceFile, ["result", "live"]), { result: true, live: 31 });
assert.deepEqual(observe(path.join(audit.out, "entry.mjs"), ["result", "live"]), { result: true, live: 31 });
assert.equal(
  readFileSync(path.join(audit.out, "provider.mjs"), "utf8"),
  readFileSync(audit.files.providerFile, "utf8")
);
assert.equal(
  lower(audit.files.sourceFile, audit.files.specFile, audit.out, ["--check-output"]).verdict.status,
  "certified"
);
assert.deepEqual(moduleEvaluationOrder(audit.out, "entry.mjs"), ["provider.mjs", "local.mjs", "entry.mjs"]);

// Separate declarations from the same provider still select the same cell.
const separateImports = auditSource.replace(
  'import { publicValue as first, publicValue as second } from "./provider.mjs";',
  'import { publicValue as first } from "./provider.mjs"; import { publicValue as second } from "./provider.mjs";'
);
const separateSpec = { ...auditSpec, modules: { "entry.mjs": ["#0", "#1", "#3", "#4"], "local.mjs": ["#2"] } };
const separate = runSplit("same-provider-separate-imports", {
  source: separateImports,
  provider: "export const publicValue = 31;",
  spec: separateSpec,
});
assert.equal(separate.verdict.status, "certified", separate.log);
assert.equal(separate.verdict.guarantee, "all-outcomes", separate.log);
assert.deepEqual(observe(separate.files.sourceFile, ["result", "live"]), { result: true, live: 31 });
assert.deepEqual(observe(path.join(separate.out, "entry.mjs"), ["result", "live"]), { result: true, live: 31 });
assert.equal(
  lower(separate.files.sourceFile, separate.files.specFile, separate.out, ["--check-output"]).verdict.status,
  "certified"
);

// Equal values in different provider cells do not justify treating aliases as one cell.
const different = primitiveFixture("different-provider-cells", {
  source: auditSource.replace("publicValue as second", "decoy as second"),
  provider: "export const publicValue = 31; export const decoy = 31;",
  spec: auditSpec,
});
assert.deepEqual(observe(different.sourceFile, ["result", "live"]), { result: true, live: 31 });
assertRefused(different, /static provider:/);
assert(!existsSync(different.out));
const auditBefore = ["entry.mjs", "local.mjs", "provider.mjs"].map((name) => readFileSync(path.join(audit.out, name)));
const existingRefusal = construct({
  source: different.sourceFile,
  spec: different.specFile,
  out: audit.out,
  require: "all-outcomes",
  allowExistingOut: true,
});
assert.equal(existingRefusal.status, "refused", existingRefusal.log);
assert.deepEqual(
  ["entry.mjs", "local.mjs", "provider.mjs"].map((name) => readFileSync(path.join(audit.out, name))),
  auditBefore
);
