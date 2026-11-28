import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { walk } from "../../translation/syntax_inventory.mjs";
import { writeDeployment } from "../../translation/testing/deployment_manifest.mjs";
import { acorn, eslintScope } from "../../translation/parser.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

const bundle = process.env.EXCALIDRAW_OUTPUTS.split(" ").find((file) => file.endsWith("/bundle"));
assert(bundle);
const entry = "chunk-PN7KOLDJ.js";
const scroll = "packages/excalidraw/scene/scroll.mjs";
const source = path.join(bundle, entry);
const code = readFileSync(source, "utf8");
const sha256 = (value) => createHash("sha256").update(value).digest("hex");
const hashes = JSON.parse(readFileSync(new URL("./main_static_sha256.json", import.meta.url), "utf8"));
assert.equal(sha256(code), hashes[entry]);
const parsed = parseModule(code, acorn.parse, eslintScope);
const units = topLevelStatements(parsed.ast);
const scope = parsed.scopes.scopes.find((s) => s.type === "module");
const ordinal = (identifier) => {
  const i = units.findIndex(({ start, end }) => start <= identifier.start && identifier.end <= end);
  assert(i >= 0);
  return i;
};

// Close over module bindings used eagerly or captured, including their declaration
// sites and top-level writes. Preserve source order and original unit identities.
const edges = units.map(() => new Set());
for (const v of scope.variables) {
  const definitions = new Set(v.identifiers.map(ordinal));
  for (const ref of v.references) {
    if (ref.isWrite() && ref.from.variableScope === scope) definitions.add(ordinal(ref.identifier));
  }
  for (const ref of v.references) {
    for (const i of definitions) edges[ordinal(ref.identifier)].add(i);
  }
}
function closure(seeds) {
  const ids = new Set(seeds);
  for (const i of ids) for (const dependency of edges[i]) ids.add(dependency);
  return [...ids].sort((a, b) => a - b);
}
const root = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const tmp = process.env.TEST_TMPDIR;
function annotated(file, output) {
  execFileSync(process.env.MODULE_IO, ["--parse", file, output]);
  return readFileSync(output, "utf8");
}
// Use the production parse and indexed source decoder. This test runner also
// attempts a planner on supported inputs; these requests fail before that.
function runProbe(kind, input) {
  return JSON.parse(
    execFileSync(process.env.LINKED_INPUT_CHECKER, {
      encoding: "utf8",
      input: JSON.stringify([{ kind, input }]) + "\n",
    })
  )[0];
}
function probe(file, spec, ast) {
  return runProbe("indexedInlinePlan", { source: JSON.parse(ast), sourceText: readFileSync(file, "utf8"), spec });
}
const movedPair = [1337, 1338];
function placement(ids, moved) {
  const pick = (selected) => ids.flatMap((i, j) => (selected(i) ? ["#" + j] : []));
  return {
    entry,
    modules: { [entry]: pick((i) => !moved.includes(i)), [scroll]: pick((i) => moved.includes(i)) },
    order: [scroll],
    sequential: true,
  };
}
const fullAst = path.join(tmp, "full-ast.json");
const full = probe(
  source,
  placement(
    units.map((_, i) => i),
    movedPair
  ),
  annotated(source, fullAst)
);
assert.equal(full.error, "statement 41: unsupported global Uint8Array");
const census = JSON.parse(
  execFileSync(process.env.COREJS, ["--lowering-census", fullAst], {
    encoding: "utf8",
    maxBuffer: 8 * 1024 * 1024,
  })
);
const snippet = (i) => code.slice(units[i].start, units[i].end);
const firstOrdinals = [41, 376];
writeFileSync(path.join(root, "full-lowering.json"), JSON.stringify({ full, census }, null, 2));
console.log(
  JSON.stringify({
    full,
    snippets: Object.fromEntries(firstOrdinals.map((i) => [i, snippet(i)])),
    firstRefusals: census.lowering_refusals.slice(0, 25),
  })
);
function trial(name, moved, retainFirstRefusal) {
  const ids = closure([5, ...(retainFirstRefusal ? [41] : []), ...moved]);
  const kept = new Set(ids);
  const body = ids.map((i) => {
    const node = units[i].node;
    if (node.type !== "ImportDeclaration") return node;
    return {
      ...node,
      specifiers: node.specifiers.filter((sp) =>
        scope.set.get(sp.local.name).references.some((ref) => kept.has(ordinal(ref.identifier)))
      ),
    };
  });
  for (const [i, { node }] of units.entries()) {
    if (node.type !== "ExportNamedDeclaration") continue;
    assert(!node.declaration && !node.source);
    const specifiers = node.specifiers.filter((sp) =>
      scope.set.get(sp.local.name).identifiers.some((id) => kept.has(ordinal(id)))
    );
    if (!specifiers.length) continue;
    ids.push(i);
    body.push({ ...node, specifiers });
  }
  const dir = path.join(root, name);
  mkdirSync(dir);
  const manifest = path.join(tmp, name + "-print.json");
  writeFileSync(manifest, JSON.stringify({ [entry]: { type: "Program", sourceType: "module", body } }));
  execFileSync(process.env.MODULE_IO, ["--print", manifest, dir, source]);
  // Keep each static provider byte-for-byte; recurse through its imports.
  const providers = new Set();
  const urlSites = [];
  function copyProviders(moduleName, ast) {
    walk(ast, (node) => {
      if (node.type === "ImportExpression" || node.type === "MetaProperty") {
        urlSites.push([moduleName, node.source?.value ?? node.type]);
      }
      if (!(["ImportDeclaration", "ExportNamedDeclaration", "ExportAllDeclaration"].includes(node.type) && node.source))
        return;
      const request = node.source.value;
      assert(request.startsWith("./"));
      const target = path.join(path.dirname(moduleName), request);
      assert.notEqual(target, entry, "provider re-enters the projected entry");
      if (providers.has(target)) return;
      providers.add(target);
      const provider = readFileSync(path.join(bundle, target));
      assert.equal(sha256(provider), hashes[target]);
      copyFileSync(path.join(bundle, target), path.join(dir, target));
      copyProviders(target, parseModule(provider.toString("utf8"), acorn.parse).ast);
    });
  }
  const slice = path.join(dir, entry);
  const sliced = parseModule(readFileSync(slice, "utf8"), acorn.parse, eslintScope);
  assert.equal(topLevelStatements(sliced.ast).length, ids.length);
  copyProviders(entry, sliced.ast);
  const spec = path.join(dir, "spec.json");
  const plan = placement(ids, moved);
  writeFileSync(spec, JSON.stringify(plan, null, 2));
  const result = split({
    source: slice,
    spec,
    out: path.join(tmp, name),
    require: "normal-completion",
  });
  assert.equal(result.reasonCode, "no_constructed_rule", result.log);
  assert.equal(result.guarantee, null);
  const ast = path.join(tmp, name + ".json");
  const decoded = probe(slice, plan, annotated(slice, ast));
  if (moved.length === 1 && retainFirstRefusal) {
    const dest = path.join(tmp, "structural");
    const sidecar = path.join(dir, "structural.json");
    const diag = spawnSync(
      process.env.LOWERSPLIT,
      ["--diagnostic", writeDeployment(ast, spec, dest), dest, "--plan", sidecar, "--all-conflicts"],
      { encoding: "utf8" }
    );
    if (diag.error) throw diag.error;
    assert.equal(diag.status, 1, diag.stdout + diag.stderr);
    assert.match(diag.stderr, /no modules written/);
    assert(!existsSync(dest));
    const { orderConflicts } = JSON.parse(readFileSync(sidecar, "utf8"));
    assert(orderConflicts.pairs.some(([i, j]) => ids[i] === 5 && ids[j] === 1337));
  }
  const failure = /^statement (\d+):/.exec(decoded.error);
  const report = {
    ids,
    exitCode: 1,
    files: result.files,
    providers: [...providers],
    urlSites,
    verdict: result.verdict,
    decoded,
    originalFailure: failure ? ids[Number(failure[1])] : null,
  };
  writeFileSync(path.join(dir, "result.json"), JSON.stringify(report, null, 2));
  return report;
}
const single = trial("single", [1337], true);
const pair = trial("pair", movedPair, true);
const without41 = trial("crossing-only", [1337], false);
assert.deepEqual(single.ids, [4, 5, 41, 1337, 1786]);
assert.equal(single.originalFailure, 41);
assert.deepEqual(single.urlSites, []);
assert.deepEqual(pair.urlSites, []);
assert.equal(pair.originalFailure, 41);
assert(single.decoded.error.endsWith("unsupported global Uint8Array"));
assert(pair.decoded.error.endsWith("unsupported global Uint8Array"));
assert.equal(without41.decoded.error, "checked construction requires an import-free original");
for (const [name, { ids, ...report }] of Object.entries({ single, pair, without41 })) {
  console.log(JSON.stringify({ name, retained: ids.length, ...report }));
}

// Trace the actual call through the named import and export to the defining
// cell. These are parser facts; they do not authorize dropping the provider's
// other units or replacing a live cell with a snapshot.
const [declaration] = units[5].node.declarations;
const call = declaration.init;
assert.equal(call.type, "CallExpression");
const alias = scope.set.get(call.callee.name);
assert(alias.references.some((r) => r.identifier === call.callee && r.isRead()));
const [imported] = alias.defs;
assert.equal(imported.type, "ImportBinding");
assert.equal(imported.parent.source.value, "./chunk-BLPUKQJE.js");
const providerName = path.basename(imported.parent.source.value);
const exported = imported.node.imported.name;
assert.deepEqual([declaration.id.name, alias.name, providerName, exported], ["po", "Ve", "chunk-BLPUKQJE.js", "b"]);
const providerPath = path.join(bundle, providerName);
const providerCode = readFileSync(providerPath, "utf8");
assert.equal(sha256(providerCode), hashes[providerName]);
const providerParse = parseModule(providerCode, acorn.parse, eslintScope);
const providerUnits = topLevelStatements(providerParse.ast);
const providerScope = providerParse.scopes.scopes.find((s) => s.type === "module");
const exportRow = providerParse.ast.body.find((node) => node.type === "ExportNamedDeclaration");
const exp = exportRow.specifiers.find((sp) => sp.exported.name === exported);
const factory = providerScope.set.get(exp.local.name);
assert.equal(factory.defs.length, 1);
assert.equal(factory.references.filter((r) => r.isWrite() && !r.init).length, 0);
const factoryDeclaration = factory.defs[0].node;
const factoryOrdinal = providerUnits.findIndex(({ node }) => node.declarations?.includes(factoryDeclaration));
assert.deepEqual([factory.name, factoryOrdinal], ["n", 7]);
const creator = factoryDeclaration.init;
assert.equal(creator.type, "ArrowFunctionExpression");
const loader = creator.body;
assert.equal(loader.type, "ArrowFunctionExpression");
const creatorScope = providerParse.scopes.acquire(creator);
assert.deepEqual(
  creatorScope.through.map((r) => r.identifier.name),
  []
);
const loaderScope = providerParse.scopes.acquire(loader);
const captures = [...new Set(loaderScope.through.map((r) => r.resolved))];
assert.deepEqual(captures.map((binding) => binding.name).sort(), ["a", "b"]);
assert(captures.every((binding) => binding.scope === creatorScope && binding.defs[0].type === "Parameter"));
const callbackScope = parsed.scopes.acquire(call.arguments[0]);
const callbackCaptures = [
  ...new Set(callbackScope.through.filter((r) => r.resolved).map((r) => r.resolved.name)),
].sort();
assert.deepEqual(callbackCaptures, []);
const providerAst = JSON.parse(annotated(providerPath, path.join(tmp, "wrapper-provider.json")));
assert.deepEqual(runProbe("wrapperFactoryShape", { source: providerAst, ordinal: factoryOrdinal }), { output: true });
const editorAst = JSON.parse(readFileSync(fullAst, "utf8"));
const crossing = runProbe("wrapperCrossingShape", { source: editorAst, factoryAlias: alias.name });
assert.deepEqual(crossing, { output: { wrapper: "po", arrow: "Jb" } });
assert.deepEqual(runProbe("wrapperCrossingShape", { source: editorAst, factoryAlias: "notTheFactory" }), {
  error: "unlinked imported name Ve",
});
const providerDecoded = runProbe("sourceClassification", { entry: providerName, source: providerAst });
assert.equal(providerDecoded.error, "statement 0: unsupported global Object");

// Exercise the resolved export with synthetic observable callbacks. A later
// call publishes the cache before callback reentry, even if the callback throws.
const provider = await import("data:text/javascript," + encodeURIComponent(providerCode));
const makeWrapper = provider[exported];
const failure = new Error("callback failed after publishing exports");
let calls = 0;
let partial;
const reenter = makeWrapper((exports, module) => {
  calls++;
  partial = exports;
  assert.equal(module.exports, exports);
  exports.seen = true;
  assert.equal(reenter(), exports);
  throw failure;
});
assert.equal(calls, 0);
assert.throws(reenter, (error) => error === failure);
assert.equal(reenter(), partial);
assert.equal(partial.seen, true);
assert.equal(calls, 1);

// Contrast lazy creation with an eager call at the initializer.
function observe(movedFirst, eager) {
  let captured;
  let calls = 0;
  const arrow = () => 42;
  const make = () => {
    const load = makeWrapper((exports) => {
      calls++;
      exports.captured = captured;
    });
    assert.equal(calls, 0);
    if (eager) load();
    return load;
  };
  if (movedFirst) captured = arrow;
  const load = make();
  if (!movedFirst) captured = arrow;
  const exports = load();
  assert.equal(load(), exports);
  assert.equal(calls, 1);
  return exports.captured === arrow;
}
const wrapperControl = {
  lazy: [observe(false, false), observe(true, false)],
  eager: [observe(false, true), observe(true, true)],
};
assert.deepEqual(wrapperControl, { lazy: [true, true], eager: [false, true] });
const wrapperReport = {
  consumer: { owner: entry, ordinal: 5, binding: declaration.id.name, alias: alias.name, callbackCaptures },
  provider: {
    owner: providerName,
    sha256: hashes[providerName],
    ordinal: factoryOrdinal,
    binding: factory.name,
    exported,
    captures: captures.map((binding) => binding.name).sort(),
    decoded: providerDecoded,
  },
  moved: { ordinal: 1337, from: entry, to: scroll },
  crossing,
  wrapperControl,
  reentry: { calls, retainedPartialExportsAfterThrow: true },
};
writeFileSync(path.join(root, "wrapper-boundary.json"), JSON.stringify(wrapperReport, null, 2));
console.log(JSON.stringify(wrapperReport));
