import assert from "node:assert/strict";
import { lstatSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { acorn, eslintScope } from "../../translation/parser.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { walk } from "../../translation/syntax_inventory.mjs";

const chunks = process.env.EXCALIDRAW_INPUTS.split(" ").find((file) => file.endsWith("/chunks"));
assert(chunks);
const authored = JSON.parse(readFileSync(process.env.EDITOR_LEAN_SPEC, "utf8"));
const scroll = "packages/excalidraw/scene/scroll.mjs";
// The proposed moves select either the first exported helper or both.
assert.deepEqual(authored.modules[scroll], ["#1336", "#1337", "#1338"]);
const selected = ["#1337", "#1338"];
const selectors = Object.values(authored.modules).flat();
assert.equal(selectors.length, 1787);
const all = Array.from({ length: selectors.length }, (_, i) => "#" + i);
assert.deepEqual(new Set(selectors), new Set(all));
const code = readFileSync(path.join(chunks, "editor.js"), "utf8");
const parsed = parseModule(code, acorn.parse, eslintScope);
const units = topLevelStatements(parsed.ast);
assert.equal(units.length, all.length);
assert.equal(parsed.ast.body[0].type, "ImportDeclaration");
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
const originalSource = path.join(bundle, editor[1]);
assert.equal(readFileSync(originalSource, "utf8"), code);
const urlSites = [];
const sourceUrl = pathToFileURL(originalSource);
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
  urlSites.every(([unit]) => !selected.includes(unit)),
  "URL-sensitive units must stay in the entry"
);
assert(
  !lstatSync(path.join(bundle, scroll), { throwIfNoEntry: false }),
  "the scroll module must use a new public asset path"
);
report.urlContext = { entry: editor[1], sites: urlSites };

// The pinned editor exposes its public names through an export list.
function publicNames(ast) {
  const names = new Set();
  for (const s of ast.body.filter((node) => node.type.startsWith("Export"))) {
    assert.equal(s.type, "ExportNamedDeclaration");
    assert.equal(s.declaration, null);
    for (const sp of s.specifiers) names.add(sp.exported.name ?? sp.exported.value);
  }
  return names;
}
report.originalExportCount = publicNames(parsed.ast).size;
assert.equal(report.originalExportCount, 611);
writeFileSync(
  path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "import-boundary.json"),
  JSON.stringify(report, null, 2)
);
