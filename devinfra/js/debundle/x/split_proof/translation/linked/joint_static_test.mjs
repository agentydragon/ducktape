import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { acorn, dump } from "../parser.mjs";
import { root } from "./harness.mjs";
import { writeDeployment } from "../testing/deployment_manifest.mjs";

const app = `import { value as external, bump as change } from "./provider.mjs";
var value = 10;
function bump() { value = value + 1; }
export { value, bump, external, change };`;
const provider = `var value = 20;
function bump() { value = value + 1; }
export { value, bump };`;
const dir = path.join(root, "original");
mkdirSync(dir);
writeFileSync(path.join(dir, "app.mjs"), app);
writeFileSync(path.join(dir, "provider.mjs"), provider);

function invokePublic(manifest, out, args = []) {
  const result = spawnSync(process.env.LOWERSPLIT, [manifest, out, ...args], { encoding: "utf8" });
  assert.ifError(result.error);
  assert([0, 1].includes(result.status), result.stdout + result.stderr);
  const verdict = JSON.parse(result.stdout.trim());
  return { result, verdict };
}

function assertRefused(manifest, out, args = [], reasonCode = null) {
  const { result, verdict } = invokePublic(manifest, out, args);
  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.equal(verdict.status, "rejected", result.stdout + result.stderr);
  assert.equal(verdict.guarantee, null, result.stdout + result.stderr);
  if (reasonCode !== null) assert.equal(verdict.reason_code, reasonCode, result.stdout + result.stderr);
  assert(!existsSync(out), `refusal created ${out}`);
  return verdict;
}

function diagnosticPreview(manifest, out) {
  const result = spawnSync(process.env.SPLIT_PREVIEW, [manifest, out, "--force"], { encoding: "utf8" });
  assert.ifError(result.error);
  assert([0, 1].includes(result.status), result.stdout + result.stderr);
  assert.match(result.stdout, /model diagnostic: /, result.stdout + result.stderr);
  assert(existsSync(out), `preview was not written to ${out}`);
  return result.stdout;
}

function snapshotFiles(directory, prefix = "") {
  return Object.fromEntries(
    readdirSync(directory, { withFileTypes: true })
      .sort((left, right) => left.name.localeCompare(right.name))
      .flatMap((entry) => {
        const relative = path.join(prefix, entry.name);
        const full = path.join(directory, entry.name);
        if (entry.isDirectory()) return Object.entries(snapshotFiles(full, relative));
        assert(entry.isFile(), `unexpected output entry ${full}`);
        return [[relative, readFileSync(full)]];
      })
  );
}

function previewSpec(source, spec, out) {
  return diagnosticPreview(writeDeployment(source, spec, out), out);
}

function split(entry, owner, owners, inputDir = dir) {
  const file = path.join(inputDir, entry);
  const bytes = readFileSync(file);
  const chunk = path.basename(entry, ".mjs");
  const placement = {
    chunk_id: chunk,
    source_sha256: createHash("sha256").update(bytes).digest("hex"),
    import_order: [owner],
    units: owners.map((v, ordinal) => ({ ordinal, owner: v ? owner : null })),
  };
  const spec = path.join(root, chunk + "-" + owner + ".json");
  writeFileSync(spec, JSON.stringify(placement));
  const out = spec + ".out";
  const manifest = writeDeployment(file, spec, out);
  assertRefused(manifest, out, ["--structural", "--require", "all-outcomes"]);
  previewSpec(file, spec, out);
  return { entry, owner: owner + ".mjs", chunk, placement, out, inputDir };
}

function assemble(name, ...parts) {
  const dest = path.join(root, name);
  mkdirSync(dest);
  for (const entry of ["app.mjs", "provider.mjs"])
    writeFileSync(path.join(dest, entry), readFileSync(path.join(parts[0].inputDir, entry)));
  for (const part of parts)
    for (const f of [part.entry, part.owner]) writeFileSync(path.join(dest, f), readFileSync(path.join(part.out, f)));
  return dest;
}

function fragment(part, dest) {
  const bytes = readFileSync(path.join(part.inputDir, part.entry));
  assert.equal(createHash("sha256").update(bytes).digest("hex"), part.placement.source_sha256);
  const parse = (text) => dump(text, { includeOpaqueCaptures: true });
  const files = Object.fromEntries(
    [part.entry, part.owner].map((f) => [f, parse(readFileSync(path.join(dest, f), "utf8"))])
  );
  return {
    entry: part.entry,
    chunk: part.chunk,
    placement: part.placement,
    source: parse(bytes.toString("utf8")),
    files,
  };
}

function check(appSplit, providerSplit, dest) {
  const result = spawnSync(process.env.JOINT_CHECKER, [], {
    encoding: "utf8",
    input: JSON.stringify({ app: fragment(appSplit, dest), provider: fragment(providerSplit, dest) }) + "\n",
  });
  assert.ifError(result.error);
  return result;
}

async function observe(dest) {
  const a = await import(pathToFileURL(path.join(dest, "app.mjs")));
  const b = await import(pathToFileURL(path.join(dest, "provider.mjs")));
  assert.equal(a.change, b.bump);
  const seen = [a.value, b.value];
  a.bump();
  b.bump();
  return [...seen, a.value, a.external, b.value];
}

const left = split("app.mjs", "private-state", [false, true, true, false]);
const right = split("provider.mjs", "provider-state", [true, true, false]);
const shared = split("provider.mjs", "private-state", [true, true, false]);
const expected = [10, 20, 11, 21, 21];
assert.deepEqual(await observe(dir), expected);
assert.deepEqual(await observe(assemble("app-only", left)), expected);
assert.deepEqual(await observe(assemble("provider-only", shared)), expected);
const joint = assemble("joint", left, right);
const ok = check(left, right, joint);
assert.equal(ok.status, 0, ok.stderr);
assert.equal(ok.stdout.trim(), "certified");
assert.deepEqual(await observe(joint), expected);

// Swap the cells exposed by two public names.
const retargeted = assemble("retargeted-exports", left, right);
const retargetedEntry = path.join(retargeted, "app.mjs");
const appText = readFileSync(retargetedEntry, "utf8");
const imports = acorn
  .parse(appText, { ecmaVersion: "latest", sourceType: "module" })
  .body.filter((node) => node.type === "ImportDeclaration")
  .map((node) => appText.slice(node.start, node.end));
writeFileSync(
  retargetedEntry,
  [...imports, "export { external as value, bump, value as external, change };"].join("\n")
);
const swapped = await import(pathToFileURL(retargetedEntry));
assert.deepEqual([swapped.value, swapped.external], [20, 10]);
const retargetedVerdict = check(left, right, retargeted);
assert.equal(retargetedVerdict.status, 1, retargetedVerdict.stdout);
assert.match(retargetedVerdict.stderr, /module's named exports differ from the plan/);

// Individually valid placements request the same private URL. Both public
// bindings now reach one cell and both bumps update it.
const collision = assemble("collision", left, shared);
assert.deepEqual(await observe(collision), [20, 20, 22, 22, 22]);
const refused = check(left, shared, collision);
assert.equal(refused.status, 1, refused.stdout);
assert.match(refused.stderr, /joint output path collision/);

// A disjoint-path cycle distinguishes equivalence at the provider root from
// equivalence at the application root. The provider split initializes seed
// before observation. Entering through the provider hides the change: the
// app already initialized ready. Entering through the app exposes it.
const cyclicDir = path.join(root, "cyclic-original");
mkdirSync(cyclicDir);
writeFileSync(
  path.join(cyclicDir, "app.mjs"),
  `import { observation } from "./provider.mjs";
  var ready = 1;
  var tag = 2;
  export { ready, tag, observation };`
);
writeFileSync(
  path.join(cyclicDir, "provider.mjs"),
  `import { ready } from "./app.mjs";
  const observation = ready ? "ready" : typeof seed;
  var seed = () => 3;
  export { observation, seed };`
);
const ca = split("app.mjs", "app-tag", [false, false, true, false], cyclicDir);
const cp = split("provider.mjs", "seed", [false, false, true, false], cyclicDir);
const both = assemble("cycle-joint", ca, cp);
// Load each root from a separate copy, so Node's module cache cannot erase
// the difference in initialization order.
async function at(directory, entry) {
  return (await import(pathToFileURL(path.join(directory, entry)))).observation;
}
assert.equal(await at(cyclicDir, "app.mjs"), "undefined");
assert.equal(await at(assemble("cycle-app-only", ca), "app.mjs"), "undefined");
const originalB = path.join(root, "cycle-provider-root");
mkdirSync(originalB);
for (const entry of ["app.mjs", "provider.mjs"])
  writeFileSync(path.join(originalB, entry), readFileSync(path.join(cyclicDir, entry)));
assert.equal(await at(originalB, "provider.mjs"), "ready");
assert.equal(await at(assemble("cycle-provider-only", cp), "provider.mjs"), "ready");
assert.equal(await at(both, "app.mjs"), "function");
const cycle = check(ca, cp, both);
assert.equal(cycle.status, 1, cycle.stdout);
assert.match(cycle.stderr, /has access to the moved region/);

// A provider can grow a public namespace via legitimate glue even when the
// app never imports that name. Check the provider's public cells too.
const leakDir = path.join(root, "provider-leak");
mkdirSync(leakDir);
writeFileSync(path.join(leakDir, "app.mjs"), app);
writeFileSync(
  path.join(leakDir, "provider.mjs"),
  `var value = 20;
   var step = 1;
   function bump() { value = value + step; }
   export { value, bump };`
);
const leak = split("provider.mjs", "leaked-state", [true, false, true, false], leakDir);
const leaked = assemble("leaked", left, leak);
const extra = await import(pathToFileURL(path.join(leaked, "provider.mjs")));
assert.deepEqual(Object.keys(extra), ["bump", "step", "value"]);
const ns = check(left, leak, leaked);
assert.equal(ns.status, 1, ns.stdout);
assert.match(ns.stderr, /provider\.mjs: entry exports resolve to different linked cells; extra 1: "step"/);

// Both fragments put a tag at local ordinal one. Calls to the same site
// share its frozen object; the provider and application have distinct sites.
const tagDir = path.join(root, "joint-template-original");
mkdirSync(tagDir);
writeFileSync(
  path.join(tagDir, "app.mjs"),
  'import { providerSite } from "./provider.mjs";\nfunction appSite() { return (x => x)`same`; }\nexport { appSite, providerSite };'
);
writeFileSync(
  path.join(tagDir, "provider.mjs"),
  "const tag = x => x;\nfunction providerSite() { return tag`same`; }\nexport { providerSite };"
);
const tagApp = split("app.mjs", "app-template", [false, true, false], tagDir);
const tagProvider = split("provider.mjs", "provider-template", [true, true, false], tagDir);
const tagOutput = assemble("joint-template-output", tagApp, tagProvider);
for (const directory of [tagDir, tagOutput]) {
  const { appSite, providerSite } = await import(pathToFileURL(path.join(directory, "app.mjs")));
  assert.equal(appSite(), appSite());
  assert.equal(providerSite(), providerSite());
  assert.notEqual(appSite(), providerSite());
}
const tagged = check(tagApp, tagProvider, tagOutput);
assert.equal(tagged.status, 0, tagged.stderr);
assert.equal(tagged.stdout.trim(), "certified");

// An unchanged root observes all three changed providers in one initialization.
{
  const original = path.join(root, "three-providers");
  mkdirSync(original);
  const entry = "app.mjs";
  const providers = ["first", "second", "third"];
  const source = providers
    .map((name) => `import { value as ${name} } from "./${name}.mjs";`)
    .concat("export const total = first + second + third;")
    .join("\n");
  writeFileSync(path.join(original, entry), source);
  const chunks = [{ path: entry, source: entry }];
  for (const [i, name] of providers.entries()) {
    const route = name + ".mjs";
    const owner = name + "-body";
    const bytes = `const value = ${i + 1}; export { value };`;
    writeFileSync(path.join(original, route), bytes);
    chunks.push({
      path: route,
      source: route,
      placement: {
        chunk_id: name,
        source_sha256: createHash("sha256").update(bytes).digest("hex"),
        import_order: [owner],
        units: [
          { ordinal: 0, owner },
          { ordinal: 1, owner: null },
        ],
      },
    });
  }
  const manifest = path.join(original, "deployment.json");
  writeFileSync(manifest, JSON.stringify({ entry, chunks }));
  const options = ["--structural", "--deployment-origin", "https://deployment.test"];
  assertRefused(manifest, path.join(root, "three-providers-public"), [], "no_constructed_rule");
  assertRefused(manifest, path.join(root, "three-providers-former-mode"), options);

  // Preserve the former case's Node evidence with a separate Lean diagnostic
  // preview. Each changed source chunk is previewed independently, then joined
  // with the unchanged application entry just as the deployment did.
  const preview = path.join(root, "three-providers-node-preview");
  mkdirSync(preview);
  copyFileSync(path.join(original, entry), path.join(preview, entry));
  for (const chunk of chunks.slice(1)) {
    const placementFile = path.join(original, `${chunk.path}.placement.json`);
    writeFileSync(placementFile, JSON.stringify(chunk.placement));
    const chunkPreview = path.join(root, `${chunk.placement.chunk_id}-deployment-preview`);
    const singleChunk = writeDeployment(path.join(original, chunk.source), placementFile, chunkPreview);
    const model = diagnosticPreview(singleChunk, chunkPreview);
    assert.match(model, /OrderKept/);
    for (const file of readdirSync(chunkPreview)) copyFileSync(path.join(chunkPreview, file), path.join(preview, file));
  }
  assert.equal((await import(pathToFileURL(path.join(original, entry)))).total, 6);
  assert.equal((await import(pathToFileURL(path.join(preview, entry)))).total, 6);

  const before = snapshotFiles(preview);
  const checked = invokePublic(manifest, preview, [...options, "--check-output"]);
  assert.equal(checked.result.status, 1, checked.result.stdout + checked.result.stderr);
  assert.equal(checked.verdict.status, "rejected", checked.result.stdout + checked.result.stderr);
  assert.equal(checked.verdict.guarantee, null, checked.result.stdout + checked.result.stderr);
  assert.deepEqual(snapshotFiles(preview), before, "multi-chunk check-output changed existing bytes");
}

// If two moved initializers throw different values, changing which owner runs
// first changes observable abrupt completion. Normal-completion equivalence
// cannot justify that rewrite, and the public path must refuse both requests.
{
  const original = path.join(root, "throwing-deployment");
  mkdirSync(original);
  const route = "provider.mjs";
  const provider = `const first = (() => { throw "first"; })();
    const second = (() => { throw "second"; })();
    export { first, second };`;
  writeFileSync(path.join(original, route), provider);
  const sourceFile = path.join(original, route);
  const makePlacement = (order) => {
    const placement = {
      chunk_id: "provider",
      source_sha256: createHash("sha256").update(provider).digest("hex"),
      import_order: order,
      units: ["first", "second", null].map((owner, ordinal) => ({ ordinal, owner })),
    };
    const spec = path.join(original, `${order.join("-")}.placement.json`);
    writeFileSync(spec, JSON.stringify(placement));
    return spec;
  };

  const orderedSpec = makePlacement(["first", "second"]);
  const orderedPreview = path.join(root, "throwing-deployment-ordered-preview");
  const orderedManifest = writeDeployment(sourceFile, orderedSpec, orderedPreview);
  assertRefused(orderedManifest, path.join(root, "throwing-deployment-ordered-public"), [
    "--structural",
    "--require",
    "all-outcomes",
  ]);
  const orderedModel = diagnosticPreview(orderedManifest, orderedPreview);
  assert.match(orderedModel, /for every world/);

  const reorderedSpec = makePlacement(["second", "first"]);
  const reorderedPreview = path.join(root, "throwing-deployment-reordered-preview");
  const reorderedManifest = writeDeployment(sourceFile, reorderedSpec, reorderedPreview);
  assertRefused(reorderedManifest, path.join(root, "throwing-deployment-reordered-default"), [], "no_constructed_rule");
  assertRefused(reorderedManifest, path.join(root, "throwing-deployment-reordered-normal"), [
    "--structural",
    "--require",
    "normal-completion",
  ]);
  assertRefused(reorderedManifest, path.join(root, "throwing-deployment-reordered-all"), [
    "--structural",
    "--require",
    "all-outcomes",
  ]);
  const reorderedModel = diagnosticPreview(reorderedManifest, reorderedPreview);
  assert.match(reorderedModel, /not covered: a run that throws/);

  const observeAbrupt = async (file) => {
    try {
      await import(pathToFileURL(file));
      return { completion: "normal" };
    } catch (value) {
      return { completion: "throw", value };
    }
  };
  assert.deepEqual(await observeAbrupt(sourceFile), { completion: "throw", value: "first" });
  assert.deepEqual(await observeAbrupt(path.join(orderedPreview, route)), { completion: "throw", value: "first" });
  assert.deepEqual(await observeAbrupt(path.join(reorderedPreview, route)), { completion: "throw", value: "second" });
}
