import assert from "node:assert/strict";
import { mkdirSync, readFileSync, renameSync, symlinkSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { split } from "../testing/split.mjs";
import { observe, root } from "./harness.mjs";

const source = path.join(root, "source");
mkdirSync(path.join(source, "lib"), { recursive: true });
const entry = "entry.mjs";
const provider = "lib/provider.mjs";
const seed = "seed.mjs";
const helper = "moved.mjs";
const entryText = `import { value } from "./lib/provider.mjs";
const local = value === 31 ? "ok" : "wrong";
const retained = 10;
const result = retained === 10 ? local : "wrong";
export { value as live, result };`;
const providerText = `import { seed } from "../seed.mjs";
export const value = seed;
export const decoy = seed;`;
const seedText = "export const seed = 31;";
const sources = { [entry]: entryText, [provider]: providerText, [seed]: seedText };
for (const [name, text] of Object.entries(sources)) writeFileSync(path.join(source, name), text);

const placement = path.join(root, "placement.json");
const manifest = path.join(root, "deployment.json");
const chunks = Object.keys(sources).map((name) => ({
  path: name,
  source: path.join(source, name),
  ...(name === entry ? { spec: placement } : {}),
}));
function declare(rows = chunks) {
  writeFileSync(manifest, JSON.stringify({ entry, chunks: rows }));
}
function place(moved = 1) {
  writeFileSync(
    placement,
    JSON.stringify({
      entry,
      modules: { [entry]: [0, 1, 2, 3, 4].filter((i) => i !== moved).map((i) => `#${i}`), [helper]: [`#${moved}`] },
      order: [helper],
      sequential: true,
    })
  );
}
const out = path.join(root, "out");
const run = (options = {}) => split({ manifest, out, require: "all-outcomes", ...options });
const check = () => run({ checkOutput: true });
function refused(result, reason) {
  assert.equal(result.status, "refused", result.log);
  assert.match(result.reason, reason);
  assert(!Object.hasOwn(result.verdict, "receipt"));
}

declare();
place();
const published = run();
assert.equal(published.status, "certified", published.log);
assert.equal(published.scope, "finite-static-graph");
assert.deepEqual(published.files, [entry, provider, helper, seed]);
const receipt = published.verdict.receipt;
assert.equal(receipt.operation, "published");
assert.deepEqual(receipt.graph.source.evaluation_order, [seed, provider, entry]);
assert.deepEqual(receipt.graph.output.evaluation_order, [seed, provider, helper, entry]);
assert.deepEqual(receipt.graph.public_entry, {
  route: entry,
  exports: ["live", "result"],
  live_cells: "same-linked-cells",
});
assert.equal(receipt.graph.run_result, "equal-complete-modeled-result-at-equal-fuel-for-every-world");
assert.deepEqual(receipt.deployment.fixed_files, [provider, seed]);
assert.equal(receipt.client.coverage, "unchecked");
assert.equal(receipt.deployment.claim, "none");

const publicRead = (dir) => observe(path.join(dir, entry), ["live", "result"]);
assert.deepEqual(publicRead(source), { live: 31, result: "ok" });
assert.deepEqual(publicRead(out), publicRead(source));
assert.deepEqual(check().verdict.receipt, { ...receipt, operation: "checked-output" });

const movedFile = path.join(out, helper);
const movedText = readFileSync(movedFile, "utf8");
assert.match(movedText, /const local/);
assert.doesNotMatch(readFileSync(path.join(out, entry), "utf8"), /const local/);
const wrongCell = movedText.replace(/import \{\s*value\s*\}/, "import { decoy as value }");
assert.notEqual(wrongCell, movedText);
writeFileSync(movedFile, wrongCell);
assert.deepEqual(publicRead(out), publicRead(source));
refused(check(), /differs from its generated ESTree/);
writeFileSync(movedFile, movedText);

const fixedFile = path.join(out, provider);
writeFileSync(fixedFile, providerText + "\n// equal runtime behavior");
refused(check(), /provider lib\/provider\.mjs differs from the source graph/);
writeFileSync(fixedFile, providerText);
assert.equal(check().status, "certified");

const sentinel = path.join(out, "unrelated");
writeFileSync(sentinel, "keep");
place(2); // Same dependency graph, but the helper now executes a later source unit first.
refused(run({ allowExistingOut: true }), /different ordered linked units/);
assert.equal(readFileSync(sentinel, "utf8"), "keep");
place();

const refusedOut = path.join(root, "refused");
const seedFile = path.join(source, seed);
writeFileSync(seedFile, `import { value } from "./lib/provider.mjs";\n${seedText}`);
refused(run({ out: refusedOut }), /static cycle/);
writeFileSync(seedFile, seedText);

const aliasFile = path.join(source, "alias.mjs");
renameSync(seedFile, aliasFile);
symlinkSync("alias.mjs", seedFile);
refused(run({ out: refusedOut }), /does not have the deployment path|unsupported module alias/);
unlinkSync(seedFile);
renameSync(aliasFile, seedFile);

const extra = "unreached.mjs";
writeFileSync(path.join(source, extra), "export const extra = true;");
declare([...chunks, { path: extra, source: path.join(source, extra) }]);
refused(run({ out: refusedOut }), /every declared module must be reached/);
declare();

const providerFile = path.join(source, provider);
writeFileSync(providerFile, providerText.replace("../seed.mjs", "../unreached.mjs"));
refused(run({ out: refusedOut }), /every import must be declared/);
writeFileSync(providerFile, providerText.replace("value = seed", "value = seed + 0"));
refused(run({ out: refusedOut }), /primitive synchronous module syntax/);
writeFileSync(providerFile, providerText);

writeFileSync(path.join(source, helper), "export const unrelated = true;");
refused(run({ out: refusedOut }), /output path already exists/);
