import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { parseModule, topLevelStatements } from "../../scope_metadata.mjs";
import { dumpParsed } from "../../../lowering/dump_core.mjs";

const chunks = process.env.EXCALIDRAW_INPUTS.split(" ").find((file) => file.endsWith("/chunks"));
assert(chunks);
const authored = JSON.parse(readFileSync(process.env.EDITOR_LEAN_SPEC, "utf8"));
const scroll = "packages/excalidraw/scene/scroll.mjs";
const selected = authored.modules[scroll];
assert.deepEqual(selected, ["#1337", "#1338"]);
const selectors = Object.values(authored.modules).flat();
assert.equal(selectors.length, 1787);
const all = Array.from({ length: selectors.length }, (_, i) => "#" + i);
assert.deepEqual(new Set(selectors), new Set(all));
const { entry } = authored;
const placement = (moved) => ({
  entry,
  modules: { [entry]: all.filter((id) => !moved.includes(id)), [scroll]: moved },
  order: [scroll],
  sequential: true,
});
const trial = placement(selected);
const code = readFileSync(path.join(chunks, "editor.js"), "utf8");
const parsed = parseModule(code, acorn.parse, eslintScope);
const program = dumpParsed(parsed);
const units = topLevelStatements(parsed.ast, code);
assert.equal(units.length, all.length);
const moduleScope = parsed.scopes.scopes.find((s) => s.type === "module");
const imports = parsed.ast.body.filter((s) => s.type === "ImportDeclaration");
const firstBody = parsed.ast.body.findIndex((s) => s.type !== "ImportDeclaration");
const firstUnit = units.findIndex((u) => u.node.type !== "ImportDeclaration");
const describe = (node) => ({
  type: node.type,
  declarations: node.declarations?.map((d) => ({
    binding: d.id.name,
    initializer: d.init?.type,
    callee: d.init?.callee?.name,
  })),
});
const report = {
  requests: imports.map((s) => ({
    specifier: s.source.value,
    bindings: s.specifiers.map((sp) => ({ kind: sp.type, local: sp.local.name, imported: sp.imported?.name })),
    attributes: s.attributes ?? [],
  })),
  firstBody: { statement: firstBody, unit: firstUnit, ...describe(units[firstUnit].node) },
  selectedUnits: selected.map((id) => {
    const unit = units[Number(id.slice(1))];
    const reads = moduleScope.variables.filter((v) =>
      v.references.some((r) => r.isRead() && r.identifier.start >= unit.start && r.identifier.end <= unit.end)
    );
    return {
      id,
      ...describe(unit.node),
      reads: reads.map((v) => ({
        binding: v.name,
        source: v.defs.find((d) => d.type === "ImportBinding")?.parent.source.value,
      })),
    };
  }),
};
// Exercise the resolved esbuild helper from the pinned provider. Replacing the factory
// argument with an observable callback distinguishes wrapper creation from invoking it.
const firstInit = units[firstUnit].node.declarations[0].init;
assert.equal(firstInit.type, "CallExpression");
const imported = moduleScope.set.get(firstInit.callee.name).defs[0];
assert.equal(imported.type, "ImportBinding");
const provider = imported.parent.source.value;
const providerCode = readFileSync(path.join(chunks, provider), "utf8");
const providerModule = await import("data:text/javascript," + encodeURIComponent(providerCode));
const exported = imported.node.imported.name;
let calls = 0;
const load = providerModule[exported]((exports) => {
  exports.count = ++calls;
});
assert.equal(calls, 0);
const loaded = load();
assert.equal(loaded.count, 1);
assert.equal(load(), loaded);
assert.equal(calls, 1);
report.wrapper = { local: firstInit.callee.name, provider, exported, callsOnCreation: 0, callsAfterTwoLoads: calls };
writeFileSync(
  path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "import-boundary.json"),
  JSON.stringify(report, null, 2)
);
const ast = path.join(process.env.TEST_TMPDIR, "editor.json");
writeFileSync(ast, JSON.stringify(program));
const spec = path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "scroll-spec.json");
writeFileSync(spec, JSON.stringify(trial, null, 2));
const out = path.join(process.env.TEST_TMPDIR, "modules");
const run = spawnSync(process.env.LOWERSPLIT, [ast, spec, out, "--require", "all-outcomes"], { encoding: "utf8" });
if (run.error) throw run.error;
assert.equal(run.signal, null);
assert.equal(run.status, 1, run.stdout + run.stderr);
const verdict = JSON.parse(run.stdout.trimEnd().split("\n").at(-1));
const first = program.body[0];
writeFileSync(
  path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "result.json"),
  JSON.stringify(
    {
      selected,
      units: selectors.length,
      firstStatement: first.type,
      source: first.source?.lit,
      verdict,
    },
    null,
    2
  )
);
assert.equal(first.type, "ImportDeclaration");
assert.deepEqual(verdict, {
  requested: "all-outcomes",
  scope: "closed-declarations",
  status: "rejected",
  guarantee: null,
  reason: "statement 0: expected a variable declaration, got ImportDeclaration",
});
assert(!existsSync(out), "a rejected trial published modules");

// The pinned editor exposes its public names through an export list. Read the actual
// rendered entry independently of the planner; reject unhandled export syntax in the census.
function publicNames(ast) {
  const names = new Set();
  for (const s of ast.body.filter((node) => node.type.startsWith("Export"))) {
    assert.equal(s.type, "ExportNamedDeclaration");
    assert.equal(s.declaration, null);
    for (const sp of s.specifiers) names.add(sp.exported.name ?? sp.exported.value);
  }
  return names;
}
const originalNames = publicNames(parsed.ast);
const surfaces = [];
for (const moved of [selected, selected.slice(0, 1)]) {
  const tag = moved.join("_");
  const diagnostic = path.join(process.env.TEST_TMPDIR, "render-" + tag);
  mkdirSync(path.join(diagnostic, path.dirname(scroll)), { recursive: true });
  const candidate = path.join(process.env.TEST_TMPDIR, "candidate-" + tag + ".json");
  writeFileSync(candidate, JSON.stringify(placement(moved)));
  const plan = path.join(process.env.TEST_TMPDIR, "plan-" + tag + ".json");
  const render = spawnSync(process.env.LOWERSPLIT, [ast, candidate, diagnostic, "--force", "--plan", plan], {
    encoding: "utf8",
  });
  if (render.error) throw render.error;
  assert([0, 1].includes(render.status), render.stdout + render.stderr);
  const output = parseModule(readFileSync(path.join(diagnostic, entry), "utf8"), acorn.parse);
  const names = publicNames(output.ast);
  surfaces.push({
    moved,
    structuralStatus: render.status,
    orderConflicts: JSON.parse(readFileSync(plan, "utf8")).orderConflicts,
    failedChecks: render.stdout
      .split("\n")
      .map((line) => /^\s*FAIL\s+(\S+)/.exec(line)?.[1])
      .filter(Boolean),
    originalCount: originalNames.size,
    extra: [...names].filter((n) => !originalNames.has(n)).sort(),
    missing: [...originalNames].filter((n) => !names.has(n)).sort(),
  });
}
writeFileSync(
  path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "entry-exports.json"),
  JSON.stringify(surfaces, null, 2)
);
const single = surfaces[1];
assert.deepEqual(single.extra, []);
assert.deepEqual(single.missing, []);
console.log(JSON.stringify({ firstBody: report.firstBody, selectedUnits: report.selectedUnits, surfaces }));
