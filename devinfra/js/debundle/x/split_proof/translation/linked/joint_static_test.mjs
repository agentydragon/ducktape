import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { acorn, dump } from "../parser.mjs";
import { forcePreview, lower, root } from "./harness.mjs";

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

function split(entry, owner, owners, inputDir = dir, preview = false) {
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
  const opts = ["--structural", "--require", "all-outcomes"];
  if (preview) {
    const modules = { [entry]: [], [owner + ".mjs"]: [] };
    for (const row of placement.units) modules[row.owner === null ? entry : owner + ".mjs"].push("#" + row.ordinal);
    const specFile = spec + ".preview";
    writeFileSync(specFile, JSON.stringify({ entry, modules, order: [owner + ".mjs"] }));
    forcePreview({ sourceFile: file, specFile }, out);
  } else {
    assert.equal(lower(file, spec, out, opts, "linked-structural-order").verdict.status, "certified");
    assert.equal(
      lower(file, spec, out, [...opts, "--check-output"], "linked-structural-order").verdict.status,
      "certified"
    );
  }
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

// These fixtures exercise the joint CoreJS checker, whose source examples use
// imports and callables outside the strict CLI observation fragment. Their
// proposed layouts are untrusted checker inputs, not certified CLI output.
const left = split("app.mjs", "private-state", [false, true, true, false], dir, true);
const right = split("provider.mjs", "provider-state", [true, true, false], dir, true);
const shared = split("provider.mjs", "private-state", [true, true, false], dir, true);
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
const ca = split("app.mjs", "app-tag", [false, false, true, false], cyclicDir, true);
const cp = split("provider.mjs", "seed", [false, false, true, false], cyclicDir, true);
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
const leak = split("provider.mjs", "leaked-state", [true, false, true, false], leakDir, true);
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
const tagApp = split("app.mjs", "app-template", [false, true, false], tagDir, true);
const tagProvider = split("provider.mjs", "provider-template", [true, true, false], tagDir, true);
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
  const out = path.join(root, "three-providers-out");
  writeFileSync(manifest, JSON.stringify({ entry, chunks }));
  const run = (target, ...args) =>
    spawnSync(
      process.env.LOWERSPLIT,
      [manifest, target, "--structural", "--deployment-origin", "https://deployment.test", ...args],
      { encoding: "utf8" }
    );
  const emitted = run(out);
  assert.equal(emitted.status, 1, emitted.stdout + emitted.stderr);
  const strictRefusal = JSON.parse(emitted.stdout);
  assert.equal(strictRefusal.status, "rejected");
  assert.equal(strictRefusal.reason_code, "strict_observation_unsupported");
  assert(!existsSync(out));

  writeFileSync(path.join(original, "second-body.mjs"), "export const existing = 20;");
  const collisionPath = path.join(root, "collision-out");
  const collision = run(collisionPath);
  assert.equal(collision.status, 1, collision.stdout + collision.stderr);
  assert.match(JSON.parse(collision.stdout).reason, /output path already exists: second-body\.mjs/);
  assert(!existsSync(collisionPath));
}

// An ordered deployment preserves abrupt initialization and the state at the
// throw. A valid reorder of the independent declarations has a weaker proof.
{
  const original = path.join(root, "throwing-deployment");
  mkdirSync(original);
  const entry = "app.mjs";
  const route = "provider.mjs";
  const message = "startup failure";
  writeFileSync(path.join(original, entry), `import { label } from "./${route}"; export { label };`);
  const provider =
    'const label = "startup"; const detail = " failure"; throw label + detail; export { label, detail };';
  writeFileSync(path.join(original, route), provider);
  const manifest = path.join(original, "deployment.json");
  const setOrder = (order) =>
    writeFileSync(
      manifest,
      JSON.stringify({
        entry,
        chunks: [
          { path: entry, source: entry },
          {
            path: route,
            source: route,
            placement: {
              chunk_id: "provider",
              source_sha256: createHash("sha256").update(provider).digest("hex"),
              import_order: order,
              units: ["label", "detail", null, null].map((owner, ordinal) => ({ ordinal, owner })),
            },
          },
        ],
      })
    );
  const run = (dest, ...args) => {
    const result = spawnSync(process.env.LOWERSPLIT, [manifest, dest, "--structural", ...args], { encoding: "utf8" });
    assert.ifError(result.error);
    assert(result.stdout, result.stderr);
    return { exit: result.status, verdict: JSON.parse(result.stdout) };
  };

  const out = path.join(root, "throwing-deployment-out");
  setOrder(["label", "detail"]);
  const required = ["--require", "all-outcomes"];
  await assert.rejects(import(pathToFileURL(path.join(original, entry))), (thrown) => thrown === message);
  const emitted = run(out, ...required);
  assert.equal(emitted.exit, 1, JSON.stringify(emitted.verdict));
  assert.equal(emitted.verdict.status, "rejected");
  assert.equal(emitted.verdict.reason_code, "strict_observation_unsupported");
  assert(!existsSync(out));

  setOrder(["detail", "label"]);
  const reordered = path.join(root, "throwing-deployment-reordered");
  const refused = run(reordered, ...required);
  assert.equal(refused.exit, 1, JSON.stringify(refused.verdict));
  assert.match(refused.verdict.reason, /proves only normal-completion/);
  assert(!existsSync(reordered));
  const normal = run(reordered);
  assert.equal(normal.exit, 1, JSON.stringify(normal.verdict));
  assert.equal(normal.verdict.reason_code, "strict_observation_unsupported");
  assert(!existsSync(reordered));
}
