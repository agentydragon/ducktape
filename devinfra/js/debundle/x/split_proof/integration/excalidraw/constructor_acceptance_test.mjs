import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { acorn, eslintScope } from "../../translation/parser.mjs";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

// Use the same two-helper placement, pinned input, and real paths as scroll.mjs.
const inputs = process.env.EXCALIDRAW_INPUTS.split(" ");
const chunks = inputs.find((file) => file.endsWith("/chunks"));
const roster = inputs.find((file) => file.endsWith("/js-files.txt"));
const bundle = process.env.EXCALIDRAW_OUTPUTS.split(" ").find((file) => file.endsWith("/bundle"));
assert(chunks && roster && bundle);
const authored = JSON.parse(readFileSync(process.env.EDITOR_LEAN_SPEC, "utf8"));
const scroll = "packages/excalidraw/scene/scroll.mjs";
assert.deepEqual(authored.modules[scroll], ["#1336", "#1337", "#1338"]);
const selected = ["#1337", "#1338"];
const all = Array.from({ length: Object.values(authored.modules).flat().length }, (_, i) => `#${i}`);
const editor = readFileSync(roster, "utf8")
  .split("\n")
  .map((line) => line.split("\t"))
  .find(([alias]) => alias === "editor.js");
assert.equal(editor?.[1], "chunk-PN7KOLDJ.js");
const source = path.join(bundle, editor[1]);
const code = readFileSync(source, "utf8");
assert.equal(code, readFileSync(path.join(chunks, "editor.js"), "utf8"));
const parsed = parseModule(code, acorn.parse, eslintScope);
assert.equal(topLevelStatements(parsed.ast).length, all.length);
const placement = {
  entry: editor[1],
  modules: { [editor[1]]: all.filter((id) => !selected.includes(id)), [scroll]: selected },
  order: [scroll],
  sequential: true,
};
const root = process.env.TEST_TMPDIR;
const spec = path.join(root, "scroll-spec.json");
const out = path.join(root, "scroll-out");
writeFileSync(spec, JSON.stringify(placement));
const request = [
  {
    kind: "inlineConstructor",
    input: { source: dumpParsed(parsed, { includeOpaqueCaptures: true }), sourceText: code, spec: placement },
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
let production;
try {
  const result = split({ source, spec, out, require: "all-outcomes", timeoutMs: 90_000 });
  production = {
    status: result.status,
    scope: result.scope,
    strategy: result.verdict.strategy ?? null,
    reason: result.reason,
  };
} catch (error) {
  if (error.code !== "ETIMEDOUT") throw error;
  production = { status: "blocked", reason: "CLI exceeded 90 seconds" };
}
const exactStrategy = production.status === "certified" && production.strategy === "compiled-constructor";
if (exactStrategy) {
  assert(!constructor.error && !constructor.blocked);
  assert.equal(split({ source, spec, out, require: "all-outcomes", checkOutput: true }).status, "certified");
} else if (production.status === "refused") assert(!existsSync(out));
console.log(
  JSON.stringify({
    fixture: "excalidraw/scroll.mjs:1337,1338",
    source: editor[1],
    constructor,
    production,
    outcome: exactStrategy ? "green" : constructor.blocked || production.status === "blocked" ? "blocked" : "refused",
  })
);
