import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { gunzipSync } from "node:zlib";
import { split } from "../../translation/testing/split.mjs";
import { writeDeployment } from "../../translation/testing/deployment_manifest.mjs";
import { acorn, eslintScope } from "../../translation/parser.mjs";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { walk } from "../../translation/syntax_inventory.mjs";

const chunks = process.env.EXCALIDRAW_INPUTS.split(" ").find((file) => file.endsWith("/chunks"));
assert(chunks);
const bundle = process.env.EXCALIDRAW_OUTPUTS.split(" ").find((file) => file.endsWith("/bundle"));
assert(bundle);
const load = (file) => JSON.parse(readFileSync(file, "utf8"));
const selected = load(process.env.SELECTED_PLACEMENTS).placements;
const canonical = load(process.env.CANONICAL_PLACEMENTS).placements;
const joint = [];
const summaries = [];
function ordinalAt(units, { start }) {
  let lo = 0;
  let hi = units.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (units[mid].end <= start) lo = mid + 1;
    else hi = mid;
  }
  assert(lo < units.length && units[lo].start <= start);
  return lo;
}

// One edge per distinct lexical binding and using owner. This reports where
// a link must be justified, independent of the diagnostic order decision.
function linkedInventory(parsed, units, layout, entry) {
  const moduleScope = parsed.scopes.scopes.find(({ type }) => type === "module");
  const publicLocals = new Set();
  for (const row of parsed.ast.body) {
    if (row.type === "ExportNamedDeclaration" && !row.source) {
      for (const sp of row.specifiers) publicLocals.add(sp.local.name);
      if (row.declaration) {
        for (const variable of moduleScope.variables)
          if (variable.identifiers.some(({ start }) => row.declaration.start <= start && start < row.declaration.end))
            publicLocals.add(variable.name);
      }
    }
    if (row.type === "ExportDefaultDeclaration" && row.declaration.id) publicLocals.add(row.declaration.id.name);
  }
  const crossings = [];
  for (const { name, identifiers, references } of moduleScope.variables) {
    if (!identifiers.length) continue;
    const defs = [...new Set(identifiers.map((id) => ordinalAt(units, id)))];
    if (defs.some((id) => units[id].node.type === "ImportDeclaration")) continue;
    const from = [...new Set(defs.map((id) => layout[id]))];
    for (const to of new Set(
      references
        .filter((r) => r.isRead())
        .map((r) => ordinalAt(units, r.identifier))
        .filter((id) => !(units[id].node.type === "ExportNamedDeclaration" && !units[id].node.declaration))
        .map((id) => layout[id])
    )) {
      if (from.length !== 1 || from[0] !== to) crossings.push({ name, from, to, publicAlias: publicLocals.has(name) });
    }
  }
  const location = [];
  walk(parsed.ast, (node) => {
    if (node.type === "ImportExpression" || (node.type === "MetaProperty" && node.meta.name === "import")) {
      const unit = ordinalAt(units, node);
      location.push({ unit, type: node.type, owner: layout[unit] });
    }
  });
  const perDefOwner = {};
  for (const cross of crossings)
    for (const owner of cross.from) {
      const row = (perDefOwner[owner] ??= { usesWithAlias: 0, privateUses: 0 });
      row[cross.publicAlias ? "usesWithAlias" : "privateUses"]++;
    }
  return {
    crossings,
    perDefOwner,
    location,
    counts: {
      publicCrossings: crossings.filter((edge) => edge.publicAlias).length,
      privateCrossings: crossings.filter((edge) => !edge.publicAlias).length,
      dynamicImports: location.filter((site) => site.type === "ImportExpression").length,
      metadataSites: location.filter((site) => site.type === "MetaProperty").length,
      movedSites: location.filter((site) => site.owner !== entry).length,
    },
  };
}
for (const [chunk, spec] of [
  ["editor", process.env.EDITOR_LEAN_SPEC],
  ["main", process.env.MAIN_LEAN_SPEC],
]) {
  const dir = path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, chunk);
  mkdirSync(dir, { recursive: true });
  const reports = path.join(process.env.SPEC_GRAPHS, "reports/tree", chunk);
  const graph = JSON.parse(gunzipSync(readFileSync(path.join(reports, "dependency_graph.json.gz"))));
  assert.equal(graph.chunk_id, chunk);
  const validation = JSON.parse(readFileSync(path.join(reports, "translation_validation.json"), "utf8"));
  assert(validation.gate_accepts && validation.accepts);
  const filename = chunk === "editor" ? "chunk-PN7KOLDJ.js" : "main.js";
  const raw = path.join(bundle, filename);
  const source = readFileSync(raw, "utf8");
  assert.equal(source, readFileSync(path.join(chunks, chunk + ".js"), "utf8"));
  const digest = createHash("sha256").update(source).digest("hex");
  const selection = selected.find(({ chunk_id }) => chunk_id === chunk);
  const ownerView = canonical.find(({ chunk_id }) => chunk_id === chunk);
  assert.equal(selection.source_sha256, digest);
  assert.equal(ownerView.source_sha256, digest);
  joint.push({ path: filename, source: path.resolve(raw), chunk_id: chunk, placement: selection });
  const parsed = parseModule(source, acorn.parse, eslintScope);
  const dump = dumpParsed(parsed, { includeOpaqueCaptures: true });
  const units = topLevelStatements(parsed.ast);
  const nodes = graph.nodes.sort((a, b) => a.statement_ordinal - b.statement_ordinal);
  assert.equal(nodes.length, units.length);
  const entry = "entry.mjs";
  const owners = new Map(graph.module_graph.nodes.map((m) => [m.key, m.kind === "entry" ? entry : m.path + ".mjs"]));
  for (const [i, node] of nodes.entries()) {
    assert.equal(node.statement_ordinal, i);
    const ast = units[i].node;
    const binding = ast.declarations?.[0]?.id ?? ast.id;
    if (binding?.type === "Identifier")
      assert(
        node.declared_bindings.some((b) => b.binding === binding.name),
        chunk + " unit " + i
      );
  }
  // Rust's priority orders imports. The emitter's depth-first traversal determines execution order.
  const effects = JSON.parse(readFileSync(path.join(reports, "effect_translation_validation.json"), "utf8"));
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const schedule = effects.emitted_order.map((id) => byId.get("unit:" + id).statement_ordinal);
  assert.deepEqual(
    schedule.toSorted((a, b) => a - b),
    nodes.map((node) => node.statement_ordinal)
  );
  const write = (name, value) => {
    const file = path.join(dir, name + ".json");
    writeFileSync(file, JSON.stringify(value));
    return file;
  };
  const ast = write("ast", dump);
  const census = JSON.parse(
    execFileSync(process.env.COREJS, ["--moves-ast", ast], {
      encoding: "utf8",
      maxBuffer: 32 * 1024 * 1024,
    })
  );
  write("census", census);
  const classification = JSON.parse(
    execFileSync(process.env.LINKED_INPUT_CHECKER, {
      input:
        JSON.stringify([
          { kind: "sourceClassification", input: { source: dump, sourceText: source, entry: filename } },
        ]) + "\n",
      encoding: "utf8",
      maxBuffer: 32 * 1024 * 1024,
    })
  )[0];
  const declarations = new Map(census.unit_details.map((d) => [d.unit, d]));
  const byOwner = (group) => {
    const out = {};
    for (const [id, owner] of group) {
      const metadata = units[id].node.type;
      const d = declarations.get(id);
      const counter = (out[owner] ??= {
        rows: 0,
        executable: 0,
        imports: 0,
        exports: 0,
        excluded: 0,
        concrete: 0,
        opaqueFallback: 0,
        refused: 0,
        candidate: 0,
        other: 0,
      });
      counter.rows++;
      if (!d) {
        if (metadata === "ImportDeclaration") counter.imports++;
        else if (metadata.startsWith("Export")) counter.exports++;
        else counter.excluded++;
        continue;
      }
      counter.executable++;
      if (d.concrete_refusal === null) counter.concrete++;
      else if (d.kind === "refused") counter.refused++;
      else counter.opaqueFallback++;
      if (["inert", "allocate"].includes(d.kind)) counter.candidate++;
      else if (d.kind === "other") counter.other++;
    }
    return out;
  };
  const fullOwners = byOwner(nodes.map((n) => [n.statement_ordinal, owners.get(n.destination)]));
  assert.equal(
    Object.values(fullOwners).reduce((n, c) => n + c.executable, 0),
    census.units
  );
  const selectedRows = selection.units.filter(({ owner }) => owner !== null);
  assert.equal(selection.units.length, nodes.length);
  for (const { ordinal, owner, members } of selectedRows) {
    const original = ownerView.units[ordinal];
    assert.equal(original.ordinal, ordinal);
    assert.equal(original.owner, owner);
    for (const [from, to] of Object.entries(members ?? {})) assert.equal(original.members[from], to);
  }
  const selectedOwners = byOwner(selectedRows.map(({ ordinal, owner }) => [ordinal, owner]));
  write("owners", { fullOwners, selectedOwners });
  write("placements", { classification, selection, canonical: ownerView });
  function diagnostic(name, layout, input) {
    const destination = path.join(process.env.TEST_TMPDIR, chunk + "-" + name);
    const planFile = path.join(dir, name + "-plan.json");
    const moveFile = path.join(dir, name + "-moves.json");
    const result = spawnSync(
      process.env.LOWERSPLIT,
      [
        "--diagnostic",
        writeDeployment(input, layout, destination),
        destination,
        "--plan",
        planFile,
        "--move-report",
        moveFile,
      ],
      { encoding: "utf8", maxBuffer: 64 * 1024 * 1024, timeout: 120_000 }
    );
    const log = result.stdout + result.stderr;
    writeFileSync(path.join(dir, name + "-checker.log"), log);
    if (result.error) throw result.error;
    assert([0, 1].includes(result.status), log);
    assert(!existsSync(destination), "diagnostic published output");
    return { plan: load(planFile), moves: load(moveFile), log, status: result.status };
  }
  const { plan, moves, log, status } = diagnostic("full", spec, raw);
  const subset = diagnostic("selected", write("selection", selection), raw);
  const ordinary = split({
    source: raw,
    spec,
    out: path.join(process.env.TEST_TMPDIR, chunk + "-ordinary"),
    require: "all-outcomes",
  });
  assert.equal(ordinary.status, "refused", ordinary.log);
  assert.deepEqual(
    plan.order.flatMap((name) => plan.modules[name]),
    schedule
  );
  assert.deepEqual(Object.keys(plan.modules).sort(), [...owners.values()].sort());
  const planOwners = Object.entries(plan.modules).flatMap(([name, ordinals]) =>
    ordinals.map((ordinal) => [ordinal, name])
  );
  assert.deepEqual(
    planOwners.toSorted(([a], [b]) => a - b),
    nodes.map((node) => [node.statement_ordinal, owners.get(node.destination)])
  );
  const mappedSubset = Object.entries(subset.plan.modules).flatMap(([name, ids]) => ids.map((id) => [id, name]));
  assert.deepEqual(
    mappedSubset.toSorted(([a], [b]) => a - b),
    selection.units.map(({ ordinal, owner }) => [ordinal, owner === null ? filename : owner + ".mjs"])
  );
  const fullLink = linkedInventory(
    parsed,
    units,
    nodes.map((node) => owners.get(node.destination)),
    entry
  );
  const subsetLink = linkedInventory(
    parsed,
    units,
    selection.units.map(({ owner }) => owner ?? filename),
    filename
  );
  write("links", { fullLink, subsetLink });
  const totals = (groups) => {
    const result = {};
    for (const row of Object.values(groups)) for (const [k, v] of Object.entries(row)) result[k] = (result[k] ?? 0) + v;
    return result;
  };
  const summary = {
    chunk,
    digest,
    originalBytes: Buffer.byteLength(source),
    units: nodes.length,
    nonentryModules: owners.size - 1,
    moved: nodes.filter((node) => owners.get(node.destination) !== entry).length,
    rustInversions: validation.swapped_pairs,
    structural: status === 0,
    failures: log.split("\n").filter((line) => line.includes("FAIL")),
    fullTotals: totals(fullOwners),
    entry: fullOwners[entry],
    selectedTotals: totals(selectedOwners),
    selectedBindings: selectedRows.reduce((n, { members }) => n + Object.keys(members ?? {}).length, 0),
    fullLinks: fullLink.counts,
    selectedLinks: subsetLink.counts,
    selectionOrder: subset.plan.order,
    selectionOrderConflicts: subset.plan.orderConflicts,
    fullOrderConflicts: plan.orderConflicts,
    moveReportError: moves.error,
    selectedMoveReportError: subset.moves.error,
    sourceClassification: classification.error ?? "decoded",
    ordinary: ordinary.verdict,
  };
  write("result", summary);
  summaries.push(summary);
  console.log(JSON.stringify(summary));
}
const artifactRoot = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const jointManifest = path.join(artifactRoot, "selected-deployment.json");
writeFileSync(jointManifest, JSON.stringify({ entry: "main.js", chunks: joint }));
const combined = split({
  manifest: jointManifest,
  out: path.join(process.env.TEST_TMPDIR, "selected-joint"),
  require: "all-outcomes",
});
assert.equal(combined.status, "refused", combined.log);
assert.match(combined.reason, /no constructor supports multi-chunk deployments/);
writeFileSync(path.join(artifactRoot, "summary.json"), JSON.stringify({ summaries, joint: combined.verdict }, null, 2));
console.log(JSON.stringify({ joint: combined.verdict }));
