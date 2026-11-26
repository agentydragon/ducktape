import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";

const root = process.env.TEST_TMPDIR;
const dir = path.join(root, "cases", "routing");
mkdirSync(dir, { recursive: true });
writeFileSync(
  path.join(dir, "main.js"),
  `const a = 1, other = 2, b = 3;
if (true) { var hoisted = 4; }
if (true) { let blockLocal = 0; }
console.log(a + other + b + hoisted);`
);
writeFileSync(path.join(dir, "package.json"), '{"type":"module"}\n');
const source = readFileSync(path.join(dir, "main.js"), "utf8");
const fixture = path.join(dir, ".fixture");
mkdirSync(fixture, { recursive: true });
writeFileSync(path.join(fixture, "js-files.txt"), "main.js\n");
const spec = {
  inputs: { input_root: ".", js_list_path: ".fixture/js-files.txt" },
  logical_modules: {
    main: {
      "scene/Shape": { members: [{ selector: { binding: { name: "a" } } }] },
      tail: { members: [{ selector: { binding: { name: "b" } } }] },
    },
  },
  unassigned_mode: { main: { kind: "catchall_file", target: "Rest" } },
  import_order: { main: ["SCENE/Shape", "REST", "tail"] },
};
const specPath = path.join(fixture, "spec.json");
const placementPath = path.join(fixture, "placement.json");
writeFileSync(specPath, JSON.stringify(spec));
function resolve(out) {
  return spawnSync(
    path.resolve(process.env.DEBUNDLE_RESOLVER),
    ["--spec", ".fixture/spec.json", "--chunk", "main", "--out", path.relative(dir, out)],
    { cwd: dir, encoding: "utf8" }
  );
}
const resolved = resolve(placementPath);
assert.equal(resolved.status, 0, resolved.stderr);
const placement = JSON.parse(readFileSync(placementPath, "utf8"));
assert.equal(placement.chunk_id, "main");
assert.equal(placement.source_sha256, createHash("sha256").update(source).digest("hex"));
// The report lists explicit modules; the catchall Rest owner is implicit.
assert.deepEqual(placement.import_order, ["scene/shape", "tail"]);
assert.deepEqual(
  placement.units.map(({ ordinal, owner }) => [ordinal, owner]),
  [
    [0, "scene/shape"],
    [1, null],
    [2, "tail"],
    [3, null],
    [4, null],
    [5, null],
  ]
);

const original = spawnSync(process.execPath, [path.join(dir, "main.js")], { encoding: "utf8" });
assert.equal(original.status, 0, original.stderr);
assert.equal(original.stdout, "10\n");
