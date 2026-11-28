import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import sortedUniq from "lodash-es/sortedUniq.js";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

const resolve = createRequire(import.meta.url).resolve;
const manifest = resolve("lodash-es/package.json");
const packageText = readFileSync(manifest, "utf8");
const packageMetadata = JSON.parse(packageText);
const { name, version, type } = packageMetadata;
assert.deepEqual({ name, version, type }, { name: "lodash-es", version: "4.18.1", type: "module" });
const packageRoot = path.dirname(realpathSync(manifest));

const sources = new Map();
for (const [file, size, sha256] of [
  ["_baseSortedUniq.js", 756, "0cb97ac908e675c379134659f8814fd244017ef0c401913c3f6bb6282feb69ef"],
  ["eq.js", 797, "3a51729bde3af1d9bacf9b67e966d8c154e22b02f5e1cecc13f53f7f00889c13"],
  ["sortedUniq.js", 511, "0b23aa21cf887a1db228d20ecc388f5cc4ad6a0f40a3e8935d4aa44410a69c95"],
]) {
  const source = resolve("lodash-es/" + file);
  assert.equal(realpathSync(source), path.join(packageRoot, file));
  const bytes = readFileSync(source);
  assert.equal(bytes.length, size, file);
  assert.equal(createHash("sha256").update(bytes).digest("hex"), sha256, file);
  const parsed = parseModule(bytes.toString("utf8"), acorn.parse, eslintScope);
  assert.deepEqual(parsed.scopes.globalScope.through, [], file);
  sources.set(file, { source, parsed, text: bytes.toString("utf8") });
}

const entry = "_baseSortedUniq.js";
const target = sources.get(entry);
const rows = topLevelStatements(target.parsed.ast).map(({ node }) => node);
assert.deepEqual(
  rows.map(({ type }) => type),
  ["ImportDeclaration", "FunctionDeclaration", "ExportDefaultDeclaration"]
);
assert.equal(rows[0].source.value, "./eq.js");
assert.equal(rows[0].specifiers[0].type, "ImportDefaultSpecifier");
assert.equal(rows[1].id.name, "baseSortedUniq");
assert.equal(rows[2].declaration.name, "baseSortedUniq");

const a = { value: 1 };
const b = { value: 1 };
const input = [-0, 0, NaN, NaN, a, a, b, b];
const uniq = sortedUniq(input);
assert.deepEqual(uniq, [0, NaN, a, b]);
assert.equal(Object.is(uniq[0], +0), true);
assert.equal(uniq[2], a);
assert.equal(uniq[3], b);
assert.notEqual(uniq[2], uniq[3]);
assert.notEqual(uniq, input);
assert.deepEqual(input, [-0, 0, NaN, NaN, a, a, b, b]);
assert.notEqual(sortedUniq([]), sortedUniq([]));

const observed = JSON.parse(
  execFileSync(process.env.LINKED_INPUT_CHECKER, {
    encoding: "utf8",
    input:
      JSON.stringify(
        [...sources].slice(0, 2).map(([entry, { parsed }]) => ({
          kind: "sourceClassification",
          input: { entry, source: dumpParsed(parsed, { includeOpaqueCaptures: true }) },
        }))
      ) + "\n",
  })
);
const [targetShape, providerShape] = observed.map(({ output }) => output);
assert.deepEqual(targetShape.imports, [["eq.js", [["default", "eq"]]]]);
assert.deepEqual(targetShape.units, [0, 1, 2]);
assert.deepEqual(targetShape.exports, [["default", "default"]]);
assert.deepEqual(targetShape.body, [
  { id: 1, bindings: ["baseSortedUniq"], free: ["baseSortedUniq", "eq"], functionDeclaration: true },
  { id: 2, bindings: ["default"], free: ["default", "baseSortedUniq"], functionDeclaration: false },
]);
assert.deepEqual(providerShape.imports, []);
assert.deepEqual(providerShape.units, [0, 1]);
assert.deepEqual(providerShape.exports, [["default", "default"]]);

const spec = fileURLToPath(new URL("./lodash_sorted_uniq/split.json", import.meta.url));
const request = {
  source: target.source,
  spec,
  out: path.join(process.env.TEST_TMPDIR, "split"),
  require: "all-outcomes",
};
const result = split(request);
assert.equal(result.status, "refused", result.log);
assert.equal(result.reasonCode, "no_constructed_rule", result.log);
assert.equal(result.reason, "static provider: expected a raw .mjs source and a root-level entry route");

// No certified tree exists. Check an existing directory containing only
// original package bytes; split verifies refusal leaves it unchanged.
const prior = path.join(process.env.TEST_TMPDIR, "prior");
mkdirSync(prior);
copyFileSync(manifest, path.join(prior, "package.json"));
for (const [file, { source }] of sources) copyFileSync(source, path.join(prior, file));
const recheck = split({ ...request, out: prior, checkOutput: true });
assert.deepEqual(recheck.verdict, result.verdict);

// Test the two file sets through the actual staging frontend on an unchanged
// graph. The explicit premise carries package.json as fixed bytes beside the
// three .js files. The production split has no way to request that premise.
const checked = path.join(process.env.TEST_TMPDIR, "checked.json");
const layout = path.join(process.env.TEST_TMPDIR, "layout.json");
const fixedModules = Object.fromEntries(
  [...sources].filter(([file]) => file !== entry).map(([file, { text }]) => [file, text])
);
const parts = [{ programs: { [entry]: dumpParsed(target.parsed) }, source: target.text }];
function checkArtifact(providers, expectedError, dir = prior) {
  writeFileSync(layout, JSON.stringify({ parts, providers }));
  const check = spawnSync(process.env.MODULE_IO, ["--check-output", layout, dir, checked], { encoding: "utf8" });
  if (check.error) throw check.error;
  assert.equal(check.status, expectedError ? 1 : 0, check.stderr);
  if (expectedError) assert.match(check.stderr, expectedError);
}

const withPackage = { ...fixedModules, "package.json": packageText };
checkArtifact(withPackage);
const published = path.join(process.env.TEST_TMPDIR, "published");
renameSync(prior, published);
checkArtifact(withPackage, undefined, published);
const staticCall = spawnSync(
  process.execPath,
  [
    "--input-type=module",
    "--eval",
    "import sortedUniq from './sortedUniq.js'; if (sortedUniq([NaN, NaN]).length !== 1) process.exitCode = 1;",
  ],
  { cwd: published, encoding: "utf8" }
);
assert.equal(staticCall.status, 0, staticCall.stderr);
renameSync(published, prior);
checkArtifact(fixedModules, /unexpected: package\.json/);
const outputPackage = path.join(prior, "package.json");
writeFileSync(outputPackage, JSON.stringify({ ...packageMetadata, type: "commonjs" }));
checkArtifact(withPackage, /provider package\.json differs from the source graph/);
const commonjsCheck = spawnSync(process.execPath, ["--check", path.join(prior, "sortedUniq.js")], { encoding: "utf8" });
assert.equal(commonjsCheck.status, 1, commonjsCheck.stderr);
assert.match(commonjsCheck.stderr, /Cannot use import statement outside a module/);

const withoutType = { ...packageMetadata };
delete withoutType.type;
writeFileSync(outputPackage, JSON.stringify(withoutType));
checkArtifact(withPackage, /provider package\.json differs from the source graph/);
rmSync(outputPackage);
checkArtifact(withPackage, /missing: package\.json/);
checkArtifact(fixedModules);

copyFileSync(manifest, outputPackage);
const moved = path.join(prior, "moved");
mkdirSync(moved);
renameSync(path.join(prior, entry), path.join(moved, entry));
checkArtifact(withPackage, /missing: _baseSortedUniq\.js; unexpected: moved\/_baseSortedUniq\.js/);

// Replay the original graph through the typed artifact contract. No split is
// claimed: the program callback here checks transport, not behavior.
function ownedPackage(source, out, mode, expectedError) {
  const run = spawnSync(process.env.OWNED_NODE_PACKAGE_CHECKER, [source, out, mode], {
    encoding: "utf8",
    env: { ...process.env, SPLIT_PROOF_MODULE_IO: path.resolve(process.env.MODULE_IO) },
  });
  if (run.error) throw run.error;
  assert.equal(run.status, expectedError ? 1 : 0, run.stderr);
  if (expectedError) assert.match(run.stderr, expectedError);
}

const owned = path.join(process.env.TEST_TMPDIR, "owned");
ownedPackage(packageRoot, owned, "publish");
for (const [file, text] of Object.entries(withPackage)) {
  assert.deepEqual(readFileSync(path.join(owned, file)), Buffer.from(text));
}
ownedPackage(packageRoot, owned, "reread");
ownedPackage(packageRoot, owned, "publish", /owned package destination must not exist/);
ownedPackage(packageRoot, owned, "reread");

const failed = path.join(process.env.TEST_TMPDIR, "failed");
ownedPackage(packageRoot, failed, "reject", /deliberate check refusal after staging/);
assert.equal(existsSync(failed), false);

const wrongOutput = path.join(process.env.TEST_TMPDIR, "wrong-output");
cpSync(owned, wrongOutput, { recursive: true });
const wrongBytes = JSON.stringify({ ...packageMetadata, type: "commonjs" });
writeFileSync(path.join(wrongOutput, "package.json"), wrongBytes);
ownedPackage(packageRoot, wrongOutput, "reread", /provider package\.json differs from the source graph/);
assert.equal(readFileSync(path.join(wrongOutput, "package.json"), "utf8"), wrongBytes);
assert.deepEqual(readFileSync(path.join(wrongOutput, "sortedUniq.js")), Buffer.from(fixedModules["sortedUniq.js"]));
renameSync(path.join(wrongOutput, "sortedUniq.js"), path.join(wrongOutput, "relocated.js"));
ownedPackage(packageRoot, wrongOutput, "reread", /missing: sortedUniq\.js; unexpected: relocated\.js/);
assert.deepEqual(readFileSync(path.join(wrongOutput, "relocated.js")), Buffer.from(fixedModules["sortedUniq.js"]));

const unverified = path.join(process.env.TEST_TMPDIR, "unverified");
cpSync(owned, unverified, { recursive: true });
const unverifiedManifest = path.join(unverified, "package.json");
writeFileSync(unverifiedManifest, wrongBytes);
ownedPackage(unverified, failed, "publish", /package\.json must explicitly declare "type": "module"/);
rmSync(unverifiedManifest);
ownedPackage(unverified, failed, "publish", /expected a regular package file: package\.json/);
symlinkSync(manifest, unverifiedManifest);
ownedPackage(unverified, failed, "publish", /expected a regular package file: package\.json/);
assert.equal(existsSync(failed), false);

// Keep the pinned entry and provider unchanged. Only the positive control
// uses a different caller, whose whole body is a checked default snapshot.
const caller = "sortedUniq.js";
const aliasRoot = path.join(process.env.TEST_TMPDIR, "alias-source");
cpSync(owned, aliasRoot, { recursive: true });
copyFileSync(target.source, path.join(aliasRoot, entry));
copyFileSync(resolve("lodash-es/identity.js"), path.join(aliasRoot, "identity.js"));
const aliasText = `import uniq from "./${entry}";\nexport default uniq;\n`;
writeFileSync(path.join(aliasRoot, caller), aliasText);

const packageDeclaration = (root) => ({
  entry,
  chunks: [{ path: entry, source: path.join(root, entry), spec }],
  owned_node_package: { root, provider: "eq.js", caller },
});
const declaration = packageDeclaration(aliasRoot);
function deployment(manifest, caseName = "package") {
  const file = path.join(process.env.TEST_TMPDIR, caseName + ".deployment.json");
  writeFileSync(file, JSON.stringify(manifest));
  return file;
}
const cli = {
  manifest: deployment(declaration),
  out: path.join(process.env.TEST_TMPDIR, "certified"),
  require: "all-outcomes",
};
const certified = split(cli);
assert.equal(certified.status, "certified", certified.log);
assert.equal(certified.scope, "owned-node-callable-provider");
assert.equal(certified.guarantee, "all-outcomes");
assert.equal(certified.verdict.every_ending_matches, true);
assert.equal(certified.verdict.proof_scope, "linked-entry-provider");
assert.equal(certified.verdict.caller_context, "default-import-snapshot");
assert.deepEqual(certified.files, [entry, "eq.js", "package.json", "sorted-uniq.mjs", "sortedUniq.js"]);
assert.deepEqual(split({ ...cli, checkOutput: true }).verdict, certified.verdict);
for (const [file, text] of Object.entries({ ...withPackage, [caller]: aliasText })) {
  assert.deepEqual(readFileSync(path.join(cli.out, file)), Buffer.from(text));
}

execFileSync(
  process.execPath,
  [
    "--input-type=module",
    "--eval",
    `
      import assert from "node:assert/strict";
      import sortedUniq from "./sortedUniq.js";
      import callee from "./_baseSortedUniq.js";
      import { baseSortedUniq } from "./sorted-uniq.mjs";
      assert.equal(callee, baseSortedUniq);
      assert.equal(sortedUniq, callee);
      const a = { value: 1 }, b = { value: 1 };
      const input = [-0, 0, NaN, NaN, a, a, b, b];
      const output = sortedUniq(input);
      assert.deepEqual(output, [0, NaN, a, b]);
      assert.equal(Object.is(output[0], +0), true);
      assert.equal(output[2], a);
      assert.equal(output[3], b);
      assert.notEqual(output, input);
      assert.deepEqual(input, [-0, 0, NaN, NaN, a, a, b, b]);
      assert.notEqual(sortedUniq([]), sortedUniq([]));
    `,
  ],
  { cwd: cli.out }
);

function expectRefusal(options, reason) {
  const result = split({ ...cli, ...options });
  assert.equal(result.status, "refused", result.log);
  assert.match(result.reason, reason);
}
const callerRefusal = /expected the fixed caller to contain only a default import and a snapshot of that binding/;
const originalCallerManifest = deployment(packageDeclaration(packageRoot), "original-caller");
expectRefusal({ manifest: originalCallerManifest, out: failed }, callerRefusal);
expectRefusal({ manifest: originalCallerManifest, out: owned, checkOutput: true }, callerRefusal);

// Reproduce the reviewed context difference using the same caller bytes in
// both trees. The split reproduced here is the accepted entry/provider
// output; only its copied caller is exchanged for the discriminator.
const observerRoot = path.join(process.env.TEST_TMPDIR, "observer-source");
const observerOut = path.join(process.env.TEST_TMPDIR, "observer-output");
cpSync(aliasRoot, observerRoot, { recursive: true });
cpSync(cli.out, observerOut, { recursive: true });
const observerText =
  aliasText + 'export const seesHelper = await import("./sorted-uniq.mjs").then(() => true, () => false);\n';
for (const dir of [observerRoot, observerOut]) writeFileSync(path.join(dir, caller), observerText);
function seesHelper(cwd) {
  return execFileSync(
    process.execPath,
    ["--input-type=module", "--eval", `import { seesHelper } from "./${caller}"; console.log(seesHelper);`],
    { cwd, encoding: "utf8" }
  ).trim();
}
assert.equal(seesHelper(observerRoot), "false");
assert.equal(seesHelper(observerOut), "true");
const observerManifest = deployment(packageDeclaration(observerRoot), "observer");
expectRefusal({ manifest: observerManifest, out: failed }, callerRefusal);
expectRefusal({ manifest: observerManifest, out: observerOut, checkOutput: true }, callerRefusal);
expectRefusal({ manifest: observerManifest, out: observerOut, allowExistingOut: true }, callerRefusal);

// A caller can run unverified code without any dynamic import. Neither
// property reads nor top-level calls belong to the admitted context.
for (const [name, extra] of [
  ["property", "export const read = globalThis.value;"],
  ["call", "export const run = uniq([]);"],
]) {
  const root = path.join(process.env.TEST_TMPDIR, name + "-source");
  cpSync(aliasRoot, root, { recursive: true });
  writeFileSync(path.join(root, caller), aliasText + extra);
  expectRefusal({ manifest: deployment(packageDeclaration(root), name), out: failed }, callerRefusal);
}

expectRefusal({ allowExistingOut: true }, /owned package destination must not exist/);
expectRefusal({ require: "normal-completion", out: failed }, /owned_node_package requires --require all-outcomes/);
expectRefusal(
  {
    manifest: deployment(
      { ...declaration, chunks: [{ ...declaration.chunks[0], source: sources.get("eq.js").source }] },
      "wrong-source"
    ),
    out: failed,
  },
  /chunk source must be the declared package entry route/
);
for (const [field, reason] of [
  ["provider", /expected the unchanged closed provider for the default import/],
  ["caller", /expected the fixed caller to import only the original entry default/],
]) {
  expectRefusal(
    {
      manifest: deployment(
        { ...declaration, owned_node_package: { ...declaration.owned_node_package, [field]: "identity.js" } },
        "wrong-" + field
      ),
      out: failed,
    },
    reason
  );
}

const wrongSpec = path.join(process.env.TEST_TMPDIR, "misplaced-snapshot.json");
const authored = JSON.parse(readFileSync(spec, "utf8"));
writeFileSync(
  wrongSpec,
  JSON.stringify({ ...authored, modules: { [entry]: ["#0"], "sorted-uniq.mjs": ["#1", "#2"] } })
);
const wrongOwner = deployment(
  { ...declaration, chunks: [{ ...declaration.chunks[0], spec: wrongSpec }] },
  "wrong-owner"
);
expectRefusal({ manifest: wrongOwner, out: failed }, /only the original function declaration may move/);
expectRefusal({ manifest: wrongOwner, allowExistingOut: true }, /only the original function declaration may move/);

for (const [file, tamper, reason] of [
  ["eq.js", (text) => text + "\n", /provider eq\.js differs from the source graph/],
  ["package.json", () => wrongBytes, /provider package\.json differs from the source graph/],
  ["sortedUniq.js", (text) => text + "\n", /provider sortedUniq\.js differs from the source graph/],
  [
    entry,
    (text) => text.replace("export default baseSortedUniq;", "export { baseSortedUniq as default };"),
    /_baseSortedUniq\.js differs from its generated ESTree/,
  ],
]) {
  const out = path.join(process.env.TEST_TMPDIR, "tampered-" + file);
  cpSync(cli.out, out, { recursive: true });
  const dest = path.join(out, file);
  const before = readFileSync(dest, "utf8");
  const after = tamper(before);
  assert.notEqual(after, before);
  writeFileSync(dest, after);
  expectRefusal({ out, checkOutput: true }, reason);
}
assert.deepEqual(split({ ...cli, checkOutput: true }).verdict, certified.verdict);

console.log(
  JSON.stringify({
    workload: "lodash-es/_baseSortedUniq.js",
    decoded: observed,
    split: result.verdict,
    recheck: recheck.verdict,
    ownedPackage: certified.verdict,
  })
);
