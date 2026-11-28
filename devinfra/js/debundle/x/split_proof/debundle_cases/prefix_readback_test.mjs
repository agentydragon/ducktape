import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFileSync, cpSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "yaml";
import { acorn } from "../translation/parser.mjs";

const id = "emission/lowering/split_declarator_fragments/spec";
const fixture = new URL("../../../e2e/cases/emission/lowering/split_declarator_fragments/", import.meta.url);
const root = process.env.TEST_TMPDIR;
const diagnostic = path.join(root, "diagnostic");
const report = path.join(root, "report.jsonl");

function run(cmd, args, cwd = process.cwd()) {
  const result = spawnSync(cmd, args, { cwd, encoding: "utf8" });
  assert.ifError(result.error);
  return result;
}

// Get the original ordinals, catchall assignment and authored priority from
// the same resolver and translation as the corpus, not from the emitted files.
const runner = fileURLToPath(new URL("./run_cases.mjs", import.meta.url));
const adapted = run(process.execPath, [runner, "--filter", id, "--keep", diagnostic, "--out", report]);
assert.equal(adapted.status, 0, adapted.stdout + adapted.stderr);
assert.equal(JSON.parse(readFileSync(report, "utf8")).id, id);
const plan = JSON.parse(readFileSync(path.join(diagnostic, id, "lean_spec.json"), "utf8"));
const routes = new Map([
  ["entry.mjs", "static/app/entry.js"],
  ["x.mjs", "static/app/modules/x.js"],
  ["residual.mjs", "static/app/modules/residual/unhandled.js"],
]);
function route(file) {
  assert(routes.has(file), `unmapped emitted file: ${file}`);
  return routes.get(file);
}
const request = path.join(root, "plan.json");
writeFileSync(
  request,
  JSON.stringify({
    entry: route(plan.entry),
    modules: Object.fromEntries(Object.entries(plan.modules).map(([name, units]) => [route(name), units])),
    order: plan.order.map(route),
  })
);

// Stage a fresh Rust emission from the unchanged saved source and spec.
const stage = path.join(root, "source");
const original = path.join(stage, "static/app.js");
const tree = path.join(root, "output");
mkdirSync(path.dirname(original), { recursive: true });
copyFileSync(new URL("static/app.js", fixture), original);
writeFileSync(path.join(stage, "package.json"), '{"type":"module"}');
writeFileSync(path.join(stage, "js-files.txt"), "static/app.js\n");
const authored = parse(readFileSync(new URL("spec.yaml", fixture), "utf8"));
writeFileSync(
  path.join(stage, "spec.json"),
  JSON.stringify({
    ...authored,
    inputs: { input_root: ".", js_list_path: "js-files.txt" },
    materialize_logical_modules: { target_dir: "modules" },
    write_js_tree: { out_dir: tree },
  })
);
const emitted = run(path.resolve(process.env.DEBUNDLE), ["run", "--spec", "spec.json"], stage);
assert.equal(emitted.status, 0, emitted.stdout + emitted.stderr);

function check(tree) {
  return run(process.env.PREFIX_READBACK, ["static/app.js", original, request, tree]);
}
writeFileSync(path.join(tree, "package.json"), '{"type":"module"}');
const staged = path.join(tree, "app");
const goodRuntime = run(process.execPath, [path.join(staged, route(plan.entry))], stage);
assert.equal(goodRuntime.status, 0, goodRuntime.stderr);
assert.equal(goodRuntime.stdout, "3\n");
const passing = check(staged);
assert.equal(passing.status, 0, passing.stdout + passing.stderr);
assert.match(passing.stdout, /modeled prefix and retained suffix checked; same live export cells/);

// Retarget the import of c while keeping the console operation and public
// export row intact. The mutated code is well-formed and binds c to z's cell.
const wrong = path.join(tree, "wrong-binding");
cpSync(path.join(tree, "app"), wrong, { recursive: true });
const wrongEntry = path.join(wrong, route(plan.entry));
const goodText = readFileSync(wrongEntry, "utf8");
const ast = acorn.parse(goodText, { ecmaVersion: "latest", sourceType: "module" });
const entryImport = ast.body.find(
  (node) => node.type === "ImportDeclaration" && node.specifiers.some((part) => part.local.name === "c")
);
assert(entryImport);
writeFileSync(
  wrongEntry,
  goodText.slice(0, entryImport.start) +
    'import { z as c } from "./modules/residual/unhandled.js";' +
    goodText.slice(entryImport.end)
);
const wrongRuntime = run(process.execPath, [wrongEntry], stage);
assert.equal(wrongRuntime.status, 0, wrongRuntime.stderr);
assert.equal(wrongRuntime.stdout, "z\n");
const failing = check(wrong);
assert.equal(failing.status, 1, failing.stdout + failing.stderr);
assert.match(failing.stderr, /glue imports differ from the plan/);

const ordinary = run(process.execPath, [runner, "--certify", "--filter", id, "--out", report]);
assert.equal(ordinary.status, 0, ordinary.stdout + ordinary.stderr);
const full = JSON.parse(readFileSync(report, "utf8"));
assert.equal(full.id, id);
assert.equal(full.outcome, "certified");
assert.deepEqual(full.emission, { guarantee: "all-outcomes", scope: "retained-tail" });
assert.deepEqual(full.disk, full.emission);
assert.equal(full.behavior, "pass");
assert(full.expectations.every(({ status }) => status === "pass" || status === "inapplicable"));
