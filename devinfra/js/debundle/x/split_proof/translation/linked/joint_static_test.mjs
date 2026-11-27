import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { acorn, dump } from "../parser.mjs";
import { root } from "./harness.mjs";

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

function fragmentFixture(entry, owner, owners, bodyImports = "", entryExports = "") {
  // Supply the two candidate files independently of any CLI publisher.
  // This test exercises the joint checker on both legitimate and altered trees.
  const inputDir = dir;
  const bytes = readFileSync(path.join(inputDir, entry));
  const chunk = path.basename(entry, ".mjs");
  const placement = {
    chunk_id: chunk,
    source_sha256: createHash("sha256").update(bytes).digest("hex"),
    import_order: [owner],
    units: owners.map((moved, ordinal) => ({ ordinal, owner: moved ? owner : null })),
  };
  const out = path.join(root, chunk + "-" + owner);
  mkdirSync(out);
  const lines = bytes.toString().trim().split("\n");
  writeFileSync(
    path.join(out, owner + ".mjs"),
    [...lines.filter((_, i) => owners[i]), "export { value, bump };"].join("\n")
  );
  writeFileSync(
    path.join(out, entry),
    [bodyImports, `import { value, bump } from "./${owner}.mjs";`, entryExports].join("\n")
  );
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

const left = fragmentFixture(
  "app.mjs",
  "private-state",
  [false, true, true, false],
  'import { value as external, bump as change } from "./provider.mjs";',
  "export { value, bump, external, change };"
);
const right = fragmentFixture("provider.mjs", "provider-state", [true, true, false], "", "export { value, bump };");
const shared = fragmentFixture("provider.mjs", "private-state", [true, true, false], "", "export { value, bump };");
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
