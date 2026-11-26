import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, lstatSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { acorn, eslintScope } from "../../translation/parser.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { walk } from "../../translation/syntax_inventory.mjs";
import { writeDeployment } from "../../translation/testing/deployment_manifest.mjs";
import { dumpParsed } from "../../translation/annotated_estree.mjs";

const chunks = process.env.EXCALIDRAW_INPUTS.split(" ").find((file) => file.endsWith("/chunks"));
assert(chunks);
const authored = JSON.parse(readFileSync(process.env.EDITOR_LEAN_SPEC, "utf8"));
const scroll = "packages/excalidraw/scene/scroll.mjs";
// Keep the two exported helpers as the namespace-refusal witness.
assert.deepEqual(authored.modules[scroll], ["#1336", "#1337", "#1338"]);
const selected = ["#1337", "#1338"];
const selectors = Object.values(authored.modules).flat();
assert.equal(selectors.length, 1787);
const all = Array.from({ length: selectors.length }, (_, i) => "#" + i);
assert.deepEqual(new Set(selectors), new Set(all));
const { entry } = authored;
const placement = (moved, outputEntry = entry) => ({
  entry: outputEntry,
  modules: { [outputEntry]: all.filter((id) => !moved.includes(id)), [scroll]: moved },
  order: [scroll],
  sequential: true,
});
const code = readFileSync(path.join(chunks, "editor.js"), "utf8");
const parsed = parseModule(code, acorn.parse, eslintScope);
const program = dumpParsed(parsed);
const units = topLevelStatements(parsed.ast);
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
report.providerProbe = {
  local: firstInit.callee.name,
  provider,
  exported,
  callsOnCreation: 0,
  callsAfterTwoLoads: calls,
};

const ast = path.join(process.env.TEST_TMPDIR, "editor.json");
writeFileSync(ast, JSON.stringify(program));
// The authoring snapshot renames selected chunks. Resolve its editor alias back to
// esbuild's output tree, where the original relative imports resolve on disk.
const roster = process.env.EXCALIDRAW_INPUTS.split(" ").find((file) => file.endsWith("/js-files.txt"));
const bundle = process.env.EXCALIDRAW_OUTPUTS.split(" ").find((file) => file.endsWith("/bundle"));
const editor = readFileSync(roster, "utf8")
  .split("\n")
  .map((line) => line.split("\t"))
  .find(([alias]) => alias === "editor.js");
assert(bundle);
assert.equal(editor?.[1], "chunk-PN7KOLDJ.js");
const strictSource = path.join(bundle, editor[1]);
assert.equal(readFileSync(strictSource, "utf8"), code);
const strictSelection = process.argv.includes("--strict-single") ? selected.slice(0, 1) : selected;
const strictPlacement = placement(strictSelection, editor[1]);
const retained = new Set(strictPlacement.modules[strictPlacement.entry]);
const urlSites = [];
const sourceUrl = pathToFileURL(strictSource);
for (const [index, { node }] of units.entries()) {
  const id = all[index];
  walk(node, (site) => {
    if (site.type === "ImportExpression") {
      assert.equal(site.source.type, "Literal");
      const request = site.source.value;
      assert(typeof request === "string" && request.startsWith("./"));
      assert(statSync(new URL(request, sourceUrl)).isFile());
      urlSites.push([id, request]);
    } else if (site.type === "MetaProperty" && site.meta.name === "import") {
      urlSites.push([id, "import.meta"]);
    }
  });
}
assert.deepEqual(urlSites, [
  ["#354", "./file-open-002ab408-75HCAGST.js"],
  ["#354", "./file-open-7c801643-M7Y3IQ2R.js"],
  ["#356", "./directory-open-4ed118d0-ETS5I3GL.js"],
  ["#356", "./directory-open-01563666-Q3PKB2M7.js"],
  ["#357", "./file-save-745eba88-2D2PE256.js"],
  ["#357", "./file-save-3189631c-5CLXVCTQ.js"],
  ["#872", "import.meta"],
  ["#872", "import.meta"],
  ["#877", "./subset-worker.chunk-RPUG463Z.js"],
  ["#878", "./subset-shared.chunk-VM6EX75R.js"],
  ["#1751", "./image-GAAHSSAO-YDUOJAAS.js"],
  ["#1767", "./pica-RKQJY2SF.js"],
  ["#1767", "./image-blob-reduce.esm-ZI2TXCGP.js"],
]);
assert(
  urlSites.every(([unit]) => retained.has(unit)),
  "URL-sensitive units must stay in the entry"
);
assert(
  !lstatSync(path.join(bundle, scroll), { throwIfNoEntry: false }),
  "the scroll module must use a new public asset path"
);
report.urlContext = { entry: strictPlacement.entry, sites: urlSites };

const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const spec = path.join(artifacts, "scroll-spec.json");
writeFileSync(spec, JSON.stringify(strictPlacement, null, 2));
const profile = path.join(artifacts, "host.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
const out = path.join(process.env.TEST_TMPDIR, "modules");
// Moving calculateScrollCenter as well as centerScrollOn would expose private
// entry bindings through glue exports. Check the actual linked editor graph.
if (strictSelection.length === 2) {
  const pair = spawnSync(
    process.env.LOWERSPLIT,
    [
      writeDeployment(strictSource, spec, out),
      out,
      "--structural",
      "--host-contract",
      profile,
      "--deployment-origin",
      "https://example.test",
    ],
    { encoding: "utf8" }
  );
  if (pair.error) throw pair.error;
  assert.equal(pair.status, 1, pair.stdout + pair.stderr);
  const refused = JSON.parse(pair.stdout.trimEnd().split("\n").at(-1));
  assert.equal(refused.scope, "linked-structural");
  assert.match(refused.reason, /; extra 3: .*; missing 0; retargeted 0$/);
  report.entryMismatch = refused.reason;
  assert(!existsSync(out));
}

const run = spawnSync(
  process.env.LOWERSPLIT,
  [writeDeployment(strictSource, spec, out), out, "--require", "normal-completion", "--host-contract", profile],
  { encoding: "utf8" }
);
if (run.error) throw run.error;
assert.equal(run.signal, null);
assert.equal(run.status, 1, run.stdout + run.stderr);
const verdict = JSON.parse(run.stdout.trimEnd().split("\n").at(-1));
const first = program.body[0];
writeFileSync(
  path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "result.json"),
  JSON.stringify(
    {
      input: editor[1],
      selected: strictSelection,
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
assert.equal(verdict.requested, "normal-completion");
assert.equal(verdict.status, "rejected");
assert.equal(verdict.guarantee, null);
assert.equal(verdict.scope, "linked-programs", JSON.stringify(verdict));
assert.equal(verdict.reason, "statement 354: unsupported linked expression ImportExpression");
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
  const args = [writeDeployment(ast, candidate, diagnostic), diagnostic, "--force", "--plan", plan];
  if (moved.length === 1) args.push("--all-conflicts");
  const render = spawnSync(process.env.SPLIT_PREVIEW, args, { encoding: "utf8" });
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
const [pair, single] = surfaces;
assert.deepEqual(pair.extra, ["Eb$1", "Lm", "qb$1"]);
assert.deepEqual(pair.missing, []);
if (report.entryMismatch) {
  const witnesses = [...report.entryMismatch.matchAll(/"([^"]+)" \(candidate cell @\d+\.\d+\)/g)];
  assert.deepEqual(witnesses.map((m) => m[1]).sort(), pair.extra);
}
assert.deepEqual(single.extra, []);
assert.deepEqual(single.missing, []);
const conflicts = single.orderConflicts.pairs;
assert(Array.isArray(conflicts), "single-arrow plan must include all structural conflicts");
assert.equal(conflicts.length, single.orderConflicts.count);
assert.equal(conflicts.length, 259, "reinspect the single-arrow conflict set after an Excalidraw bundle update");
assert(
  conflicts.every((pair) => pair[1] === 1337),
  "every inversion should cross moved statement #1337"
);
assert(conflicts.some(([earlier, later]) => earlier === firstUnit && later === 1337));

// Keep the ordinary move-plan evidence. This replay records the exact source
// order inversions created by moving unit #1337; linked-provider certification
// now lives in the small source-file E2E cases rather than a replay-only CLI.
assert(conflicts.every(([, later]) => later === 1337));
const moveReport = {
  moved: ["#1337"],
  structuralConflictPairs: conflicts.length,
  firstConflict: conflicts[0],
  includesImportBoundaryConflict: conflicts.some(([earlier, later]) => earlier === firstUnit && later === 1337),
};
assert.equal(moveReport.structuralConflictPairs, 259);
assert(moveReport.includesImportBoundaryConflict);
report.moveReport = moveReport;
writeFileSync(
  path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "scroll-move-report.json"),
  JSON.stringify({ summary: moveReport, conflicts }, null, 2)
);
writeFileSync(
  path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "import-boundary.json"),
  JSON.stringify(report, null, 2)
);
console.log(
  JSON.stringify({
    firstBody: report.firstBody,
    selectedUnits: report.selectedUnits,
    providerProbe: report.providerProbe,
    moveReport: report.moveReport,
    surfaces,
  })
);
