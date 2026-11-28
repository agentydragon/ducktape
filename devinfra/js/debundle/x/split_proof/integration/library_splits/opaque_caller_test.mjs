import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parse } from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

const pinnedPath = process.argv[2];
const code = readFileSync(pinnedPath, "utf8");
assert.equal(
  createHash("sha256").update(code).digest("hex"),
  "9a1ab71f40d915f55f3985c314a844d7686f936b1a097dd78ded2c92cf84ba44"
);
const parseText = (text) => parseModule(text, parse, eslintScope);
const dump = (text) => dumpParsed(parseText(text), { includeOpaqueCaptures: true });
const ordinal = 6;
const units = topLevelStatements(parseText(code).ast).map(({ node }) => node);
const row = units[ordinal];
assert.equal(row.declarations[0].id.name, "__commonJS");
const factory = code.slice(row.start, row.end);
const tmp = process.env.TEST_TMPDIR;

const spec = path.join(tmp, "placement.json");
writeFileSync(
  spec,
  JSON.stringify({
    entry: "entry.mjs",
    modules: { "private.mjs": ["#6"], "entry.mjs": units.flatMap((_, i) => (i === ordinal ? [] : [`#${i}`])) },
    order: ["private.mjs"],
    sequential: true,
  })
);
const ordinary = split({ source: pinnedPath, spec, out: path.join(tmp, "certified") });
assert.equal(ordinary.status, "refused", ordinary.log);
assert.equal(ordinary.reasonCode, "no_constructed_rule");

const fromCapture = 'import { source as __getOwnPropNames } from "./capture.mjs";\n';
const publicExports = "\nexport { __commonJS as factory, __commonJS as sameFactory };";
function stage(name, moved) {
  const dir = path.join(tmp, name);
  mkdirSync(dir);
  writeFileSync(
    path.join(dir, "capture.mjs"),
    "export let source = Reflect.ownKeys; export let alternate = Reflect.ownKeys;"
  );
  const owner = path.join(dir, moved ? "private.mjs" : "public.mjs");
  writeFileSync(owner, fromCapture + factory + (moved ? "\nexport { __commonJS };" : publicExports));
  const entry = path.join(dir, "public.mjs");
  if (moved) writeFileSync(entry, 'import { __commonJS } from "./private.mjs";' + publicExports);
  const bytes = readFileSync(owner, "utf8");
  const [request, , exports] = parseText(bytes).ast.body;
  assert.equal(request.source.value, "./capture.mjs");
  assert.deepEqual(
    request.specifiers.map((sp) => [sp.imported.name, sp.local.name]),
    [["source", "__getOwnPropNames"]]
  );
  assert.deepEqual(
    exports.specifiers.map((sp) => [sp.exported.name, sp.local.name]),
    moved
      ? [["__commonJS", "__commonJS"]]
      : [
          ["factory", "__commonJS"],
          ["sameFactory", "__commonJS"],
        ]
  );
  return {
    source: bytes,
    load: () => import(pathToFileURL(entry).href),
  };
}

const before = stage("original", false);
const after = stage("reread", true);
const checked = JSON.parse(
  execFileSync(process.env.OPAQUE_CALLER, [], {
    encoding: "utf8",
    maxBuffer: 32 * 1024 * 1024,
    input:
      JSON.stringify({
        pinned: dump(code),
        ordinal,
        providers: { "capture.mjs": dump("export let source; export let alternate;") },
        source: dump(before.source),
        reread: dump(after.source),
        badBody: dump(after.source.replace("mod = 0", "mod = 1")),
      }) + "\n",
  })
);
assert.deepEqual(checked, {
  fullSource: "statement 0: unsupported global Object",
  trial: ["normal", "throw", "unknown-after-effects", "fuel"],
  changedCode: "reread deferred source differs from the original slice",
});

// Node executes the real factory in this small context; the Lean world is a
// separate protocol test and does not implement the factory or Reflect.ownKeys.
for (const tree of [before, after]) {
  const api = await tree.load();
  assert.deepEqual(Object.keys(api), ["factory", "sameFactory"]);
  assert.equal(api.factory, api.sameFactory);
  let calls = 0;
  const loader = api.factory({
    unit(exports) {
      calls++;
      exports.name = "cached";
    },
  });
  const initial = loader();
  assert.equal(loader(), initial);
  assert.equal(initial.name, "cached");
  assert.equal(calls, 1);
}
console.log(JSON.stringify({ ...checked, ordinary: ordinary.verdict }));
