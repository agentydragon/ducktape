import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { acorn, eslintScope } from "../../translation/parser.mjs";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

// Keep the historic two-helper request beside the full upstream owner.
const inputs = process.env.EXCALIDRAW_INPUTS.split(" ");
const chunks = inputs.find((file) => file.endsWith("/chunks"));
const roster = inputs.find((file) => file.endsWith("/js-files.txt"));
const bundle = process.env.EXCALIDRAW_OUTPUTS.split(" ").find((file) => file.endsWith("/bundle"));
assert(chunks && roster && bundle);
const authored = JSON.parse(readFileSync(process.env.EDITOR_LEAN_SPEC, "utf8"));
const owner = "packages/excalidraw/scene/scroll";
const scroll = owner + ".mjs";
assert.deepEqual(authored.modules[scroll], ["#1336", "#1337", "#1338"]);
const selected = ["#1337", "#1338"];
const editor = readFileSync(roster, "utf8")
  .split("\n")
  .map((line) => line.split("\t"))
  .find(([alias]) => alias === "editor.js");
assert.equal(editor?.[1], "chunk-PN7KOLDJ.js");
const source = path.join(bundle, editor[1]);
const bytes = readFileSync(source);
assert(bytes.equals(readFileSync(path.join(chunks, "editor.js"))));
const sha256 = createHash("sha256").update(bytes).digest("hex");
assert.equal(sha256, "fa1cfc7930fe3c0e2b4d95e6cb14ebde21068014cd92db303622509d9a0d455c");
const code = bytes.toString("utf8");
const parsed = parseModule(code, acorn.parse, eslintScope);
const units = topLevelStatements(parsed.ast);
const { placements } = JSON.parse(readFileSync(process.env.CANONICAL_PLACEMENT, "utf8"));
const canonical = placements.find((p) => p.chunk_id === "editor");
assert(canonical);
assert.equal(canonical.source_sha256, sha256);
assert.equal(canonical.units.length, 1787);
assert.equal(units.length, canonical.units.length);
const all = canonical.units.map(({ ordinal }, i) => {
  assert.equal(ordinal, i);
  return `#${ordinal}`;
});
const scrollUnits = canonical.units.filter((unit) => unit.owner === owner);
const members = scrollUnits.map(({ ordinal, members }) => {
  const [only] = Object.entries(members);
  assert.equal(Object.keys(members).length, 1);
  const [binding, upstream] = only;
  assert.equal(units[ordinal].node.declarations[0].id.name, binding);
  return { ordinal, binding, upstream };
});
assert.deepEqual(
  members.map(({ ordinal, upstream }) => [ordinal, upstream]),
  [
    [1336, "isOutsideViewPort"],
    [1337, "centerScrollOn"],
    [1338, "calculateScrollCenter"],
  ]
);
const priority = canonical.import_order.filter((name) => name === owner).map((name) => name + ".mjs");
assert.deepEqual(priority, [scroll]);
const placement = (ids) => ({
  entry: editor[1],
  modules: { [editor[1]]: all.filter((id) => !ids.includes(id)), [scroll]: ids },
  order: priority,
  sequential: true,
});
const root = process.env.TEST_TMPDIR;
const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const request = [
  {
    kind: "inlineConstructor",
    input: { source: dumpParsed(parsed, { includeOpaqueCaptures: true }), sourceText: code, spec: placement(selected) },
  },
];
const probe = spawnSync(process.env.LINKED_INPUT_CHECKER, [], {
  encoding: "utf8",
  input: JSON.stringify(request) + "\n",
  maxBuffer: 64 * 1024 * 1024,
  timeout: 90_000,
});
let constructor;
if (probe.error?.code === "ETIMEDOUT") constructor = { blocked: "constructor probe exceeded 90 seconds" };
else {
  if (probe.error) throw probe.error;
  assert.equal(probe.status, 0, probe.stderr);
  constructor = JSON.parse(probe.stdout)[0];
}
function specFile(name, value) {
  const spec = path.join(artifacts, name + ".json");
  writeFileSync(spec, JSON.stringify(value, null, 2) + "\n");
  return spec;
}

function run(spec, out, options = {}) {
  return split({ source, spec, out, require: "all-outcomes", timeoutMs: 90_000, ...options });
}

function publication(name, value) {
  const spec = specFile(name, value);
  const out = path.join(root, name);
  let emit;
  try {
    emit = run(spec, out);
  } catch (error) {
    if (error.code !== "ETIMEDOUT") throw error;
    assert(!existsSync(out), "timed-out publication left an output tree");
    return { status: "blocked", reason: "CLI exceeded 90 seconds" };
  }
  if (emit.status === "refused") return { status: emit.status, verdict: emit.verdict };
  assert.equal(emit.verdict.strategy, "compiled-constructor", emit.log);
  assert.equal(emit.guarantee, "all-outcomes", emit.log);
  assert.deepEqual(emit.files, [editor[1], scroll]);
  const disk = run(spec, out, { checkOutput: true });
  assert.equal(disk.status, "certified", disk.log);
  assert.equal(disk.guarantee, "all-outcomes", disk.log);
  assert.equal(disk.scope, emit.scope, disk.log);
  const tampered = path.join(root, name + "-tampered");
  cpSync(out, tampered, { recursive: true });
  const file = path.join(tampered, scroll);
  writeFileSync(file, Buffer.concat([readFileSync(file), Buffer.from("\nthrow 0;\n")]));
  const damaged = run(spec, tampered, { checkOutput: true });
  assert.equal(damaged.status, "refused", damaged.log);
  return {
    status: emit.status,
    verdict: emit.verdict,
    files: emit.files,
    disk: disk.verdict,
    tampered: damaged.verdict,
  };
}

const pair = publication("scroll-pair", placement(selected));
const fullPlacement = placement(members.map(({ ordinal }) => `#${ordinal}`));
const full = publication("scroll-owner", fullPlacement);

const old = path.join(root, "old-output");
mkdirSync(path.join(old, path.dirname(scroll)), { recursive: true });
writeFileSync(path.join(old, editor[1]), "old entry\n");
writeFileSync(path.join(old, scroll), "old owner\n");
writeFileSync(path.join(old, "unrelated.bin"), Buffer.from([0, 255]));
// Reassign one selected unit to two owners. Its authored assignment cannot
// correspond to any valid split. The same gate currently refuses both specs.
const duplicate = structuredClone(fullPlacement);
duplicate.modules[editor[1]].push("#1338");
const duplicateSpec = specFile("duplicate-owner", duplicate);
const adversary = run(duplicateSpec, old, { allowExistingOut: true });
assert.equal(adversary.status, "refused", adversary.log);
let oldChecks = null;
if (full.status === "refused") {
  const fullSpec = path.join(artifacts, "scroll-owner.json");
  const emit = run(fullSpec, old, { allowExistingOut: true });
  const readback = run(fullSpec, old, { checkOutput: true });
  assert.deepEqual(emit.verdict, full.verdict);
  assert.deepEqual(readback.verdict, full.verdict);
  oldChecks = { emit: emit.verdict, readback: readback.verdict };
}
const report = {
  source: editor[1],
  sha256,
  units: units.length,
  owner,
  members,
  priority,
  pair: { constructor, ...pair },
  full,
  adversary: adversary.verdict,
  old_destination: oldChecks,
  chromium: "not run by this CLI probe",
};
writeFileSync(path.join(artifacts, "acceptance.json"), JSON.stringify(report, null, 2) + "\n");
console.log(JSON.stringify(report));
