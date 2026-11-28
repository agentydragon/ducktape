import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { gzipSync } from "node:zlib";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";
import { spec } from "./gen_spec.mjs";
import { smoke } from "./smoke.mjs";

const tmp = process.env.TEST_TMPDIR;
const out = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const sha = (s) => createHash("sha256").update(s).digest("hex");
const parse = (code) => parseModule(code, acorn.parse, eslintScope);
const globals = new Set(["undefined", "Infinity", "NaN"]);
const profiles = {
  Object: "intrinsic-object",
  Array: "intrinsic-array",
  Symbol: "symbol-iterator",
  Map: "keyed-collections",
  Set: "keyed-collections",
  WeakMap: "keyed-collections",
  WeakSet: "keyed-collections",
  RegExp: "regexp",
  Promise: "promise",
  Function: "dynamic-code",
  eval: "dynamic-code",
  process: "node-host",
  require: "node-host",
  global: "node-host",
  setTimeout: "timers",
  clearTimeout: "timers",
  setInterval: "timers",
  clearInterval: "timers",
};
const cases = {
  regexp: "const x = /x/g;",
  generator: "function* g(){yield 1;}",
  await: "async function f(){return await 1;}",
  "labeled-control": "L: for(;;) break L;",
  "switch-nontrailing-default": "switch(0){default: break; case 1: break;}",
  "switch-lexical": "switch(0){case 0: let x = 1;}",
  "optional-chain": "const x = ({})?.a?.b;",
  "binding-pattern": "const [x] = [1];",
  spread: "const x = [...[1]];",
  template: "const x = `${1}`;",
  "class-static-block": "class C { static {} }",
  "class-computed-key": 'class C { ["x"](){} }',
  "array-hole": "const x = [, f()]; function f(){return 1;}",
  "dynamic-code": 'const x = new Function("return 1");',
  "intrinsic-object": "const x = Object.keys({});",
  "intrinsic-array": "const x = Array.isArray([]);",
  "symbol-iterator": "const x = Symbol.iterator;",
  "keyed-collections": "const x = new Map();",
  promise: "const x = Promise.resolve(1);",
  "node-host": "const x = process.env;",
  timers: "setTimeout(() => {}, 0);",
  "ambient-global": "const x = Math.max(1, 2);",
  "commonjs-wrapper": "const load = ((m) => () => m.exports)({ exports: 1 }); const x = load();",
  "module-import": 'import {x} from "./provider.mjs"; x;',
  "module-reexport": 'export * from "./provider.mjs";',
  "module-export-list": "const x = 1; export {x};",
  "module-default-expression": "export default 1 + 2;",
  "module-dynamic-import": 'const x = import("./provider.mjs");',
  "archive-AssignsHome": "let x = 0; x = 1; // request different owners for the definition and write",
  "archive-OrderOk": "const a = 1; const b = a; // request b before a",
  "archive-ExportOutsideEntry": "const a = 1; export {a}; // request the export row outside entry",
  "missing-construction": "var x = 1; x++; export {x}; // split across the initializer and update",
  "minified-readability":
    "const a=1,b=2;export{a,b}; // inspect selectors and emitted source, require no inferred refusal",
};

function probe(command, args, input) {
  const r = spawnSync(command, args, { input, encoding: "utf8", maxBuffer: 96 * 1024 * 1024, timeout: 90000 });
  if (r.error || r.status !== 0) return { error: r.error?.code ?? (r.stderr.trim().slice(0, 500) || "probe failed") };
  return JSON.parse(r.stdout);
}

function features(node, parent, unresolved) {
  const f = [];
  const add = (id) => f.push(id);
  const t = node.type;
  if (t === "Identifier" && unresolved.has(node) && !globals.has(node.name)) {
    // Linked lowering treats guarded typeof separately; preserve that distinction.
    if (!(parent?.type === "UnaryExpression" && parent.operator === "typeof"))
      add(profiles[node.name] ?? "ambient-global");
  }
  if (node.regex) add("regexp");
  if (node.generator || t === "YieldExpression") add("generator");
  if (t === "AwaitExpression" || (t === "ForOfStatement" && node.await) || node.async) add("await");
  if (t === "LabeledStatement" || (["BreakStatement", "ContinueStatement"].includes(t) && node.label))
    add("labeled-control");
  if (t === "SwitchStatement" && node.cases.slice(0, -1).some((c) => !c.test)) add("switch-nontrailing-default");
  if (
    t === "SwitchCase" &&
    node.consequent.some((s) => (s.type === "VariableDeclaration" && s.kind !== "var") || s.type === "ClassDeclaration")
  )
    add("switch-lexical");
  if (t === "ChainExpression" || node.optional === true) add("optional-chain");
  if (["ObjectPattern", "ArrayPattern", "AssignmentPattern", "RestElement"].includes(t)) add("binding-pattern");
  if (t === "SpreadElement") add("spread");
  if (t === "TemplateLiteral" || t === "TaggedTemplateExpression") add("template");
  if (t === "StaticBlock") add("class-static-block");
  if (["MethodDefinition", "PropertyDefinition"].includes(t) && node.computed) add("class-computed-key");
  if (t === "ArrayExpression" && node.elements.includes(null)) add("array-hole");
  if (t === "ImportDeclaration") add("module-import");
  if (t === "ExportAllDeclaration" || (t === "ExportNamedDeclaration" && node.source)) add("module-reexport");
  if (t === "ExportNamedDeclaration" && !node.source && !node.declaration) add("module-export-list");
  if (t === "ExportDefaultDeclaration" && !node.declaration.type.endsWith("Declaration"))
    add("module-default-expression");
  if (t === "ImportExpression") add("module-dynamic-import");
  if (
    (t === "Identifier" && /^(?:__commonJS|__require)(?:\d+)?$/.test(node.name)) ||
    (t === "MemberExpression" &&
      node.object.type === "Identifier" &&
      node.object.name === "module" &&
      node.property.name === "exports")
  )
    add("commonjs-wrapper");
  return f;
}

function ordinalsFor(units) {
  // The existing parser supplies half-open ESTree UTF-16 ranges, even for split declarators.
  return (node) => {
    let lo = 0;
    let hi = units.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (units[mid].start <= node.start) lo = mid + 1;
      else hi = mid;
    }
    const unit = units[lo - 1];
    return unit && node.end <= unit.end ? lo - 1 : null;
  };
}

function ownership(parsed, units) {
  const ordinalOf = ordinalsFor(units);
  const records = [];
  const seen = new Set();
  for (const scope of parsed.scopes.scopes) {
    for (const ref of scope.references) {
      const binding = ref.resolved;
      if (binding?.scope.type !== "module") continue;
      const use = ordinalOf(ref.identifier);
      const definitions = [...new Set(binding.identifiers.map(ordinalOf).filter((n) => n !== null))];
      if (use === null || definitions.length === 0) continue;
      const start = ref.identifier.start;
      if (seen.has(start)) continue;
      seen.add(start);
      let withinFunctionScope = false;
      for (let at = scope; at && at.type !== "module"; at = at.upper)
        if (at.type === "function" || at.type === "function-expression-name") withinFunctionScope = true;
      records.push({
        use,
        definitions,
        start,
        end: ref.identifier.end,
        name: binding.name,
        read: ref.isRead(),
        write: ref.isWrite(),
        withinFunctionScope,
      });
    }
  }
  return records.sort((a, b) => a.start - b.start);
}

function placementOrigins(placement, references, n) {
  const owner = Array(n).fill(null);
  for (const [name, selectors] of Object.entries(placement.modules)) {
    for (const selector of selectors) {
      const ordinal = Number(selector.slice(1));
      assert.equal(owner[ordinal], null);
      owner[ordinal] = name;
    }
  }
  assert(owner.every((name) => typeof name === "string"));
  const schedule = [...placement.order, placement.entry].flatMap((name) =>
    placement.modules[name].map((s) => Number(s.slice(1)))
  );
  const position = new Map(schedule.map((ordinal, i) => [ordinal, i]));
  const crossings = [];
  let crossingWrites = 0;
  let eagerOrderCandidates = 0;
  for (const [index, ref] of references.entries()) {
    const otherOwners = [...new Set(ref.definitions.map((id) => owner[id]).filter((name) => name !== owner[ref.use]))];
    if (!otherOwners.length) continue;
    crossings.push(index);
    if (ref.write) crossingWrites++;
    // A function may be hoisted, or invoked immediately. These are never proof verdicts.
    if (
      ref.read &&
      !ref.withinFunctionScope &&
      ref.definitions.some((id) => owner[id] !== owner[ref.use] && position.get(id) > position.get(ref.use))
    )
      eagerOrderCandidates++;
  }
  const scheduleInversions = schedule.reduce(
    (n, id, i) => n + schedule.slice(0, i).filter((other) => other > id).length,
    0
  );
  return {
    crossings,
    crossingWrites,
    eagerOrderCandidates,
    scheduleInversions,
    orderEvidence: "static_scope_and_requested_schedule",
  };
}

function spans(parsed, units) {
  const unresolved = new Set(parsed.scopes.globalScope.through.map((r) => r.identifier));
  const seen = new Set();
  const hits = [];
  // Scan the original AST: synthetic declarator unit wrappers may clone identifiers.
  function visit(node, parent, candidates) {
    if (!node || typeof node.type !== "string") return;
    const narrow = candidates.filter(({ range }) => range[0] <= node.start && node.end <= range[1]);
    const matched = narrow.length ? narrow : candidates;
    for (const { ordinal } of matched) {
      for (const feature of features(node, parent, unresolved)) {
        const key = [ordinal, feature, node.start, node.end].join(":");
        if (seen.has(key)) continue;
        seen.add(key);
        hits.push({
          ordinal,
          feature,
          start: node.start,
          end: node.end,
          kind: node.type,
          ...(node.type === "Identifier" && { name: node.name }),
          ...(matched.length > 1 && { spanSharedByUnits: true }),
          evidence: "static_presence",
        });
      }
    }
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) for (const child of value) visit(child, node, matched);
      else if (value?.type) visit(value, node, matched);
    }
  }
  const indexed = units.map(({ node, declarator }, ordinal) => ({
    ordinal,
    range: [
      declarator?.start ?? node.declarations?.[0]?.start ?? node.start,
      declarator?.end ?? node.declarations?.[0]?.end ?? node.end,
    ],
  }));
  // Retain original ordinals even for multiple split declarators in one statement.
  for (const node of parsed.ast.body) {
    const candidates = indexed.filter(({ range }) => node.start <= range[0] && range[1] <= node.end);
    visit(node, null, candidates);
  }
  return hits;
}

function diagnostic(parsed, variant) {
  const source = dumpParsed(parsed, { includeOpaqueCaptures: true });
  const file = path.join(tmp, variant + "-annotated.json");
  writeFileSync(file, JSON.stringify(source));
  return { source, result: probe(process.env.COREJS, ["--moves-ast", file]) };
}

function requestSpec(base) {
  return { ...base, sequential: true, order: Object.keys(base.modules).filter((owner) => owner !== base.entry) };
}

function extract(n, unit) {
  return {
    entry: "entry.mjs",
    modules: {
      "entry.mjs": Array.from({ length: n }, (_, i) => i)
        .filter((i) => i !== unit)
        .map((i) => `#${i}`),
      "moved.mjs": [`#${unit}`],
    },
  };
}

const sources = [];
const placements = [];
const archive = new URL("./results/", import.meta.url);
const archiveRows = readdirSync(archive)
  .filter((f) => f.endsWith(".jsonl"))
  .sort()
  .flatMap((f) =>
    readFileSync(new URL(f, archive), "utf8")
      .trim()
      .split("\n")
      .map((line) => ({ file: f, ...JSON.parse(line) }))
  );
for (const file of process.argv.slice(2)) {
  const variant = path.basename(file, ".mjs");
  const library = variant.replace(/\.min$/, "");
  const code = readFileSync(file, "utf8");
  const parsed = parse(code);
  const units = topLevelStatements(parsed.ast);
  const n = units.length;
  const inventory = spans(parsed, units);
  const references = ownership(parsed, units);
  const { source, result } = diagnostic(parsed, variant);
  assert(!result.error, result.error);
  // A first concrete refusal has a known exact unit; matching candidate spans are
  // useful for navigation but may not be the precise subexpression that failed.
  const byUnit = new Map();
  for (const span of inventory) {
    if (!byUnit.has(span.ordinal)) byUnit.set(span.ordinal, []);
    byUnit.get(span.ordinal).push(span);
  }
  const lowering = result.unit_details.map((unit) => ({
    ...unit,
    evidence: "independently_observed_per_unit",
    ...(unit.concrete_refusal && {
      candidateSpans: (byUnit.get(unit.unit) ?? [])
        .filter((span) => span.name && unit.concrete_refusal === `unsupported global ${span.name}`)
        .map(({ start, end }) => [start, end]),
    }),
  }));
  const baseline = await smoke(library, file);
  assert(baseline.ok, JSON.stringify(baseline));
  sources.push({
    variant,
    bytes: Buffer.byteLength(code),
    sha256: sha(code),
    units: n,
    baseline,
    unitOrigins: units.map(({ node, start, end }, ordinal) => ({ ordinal, start, end, type: node.type })),
    spans: inventory,
    references,
    syntaxKinds: [...new Set(units.map(({ node }) => node.type))].sort(),
    concreteLowering: { ...result, unit_details: lowering },
    readability: {
      minified: variant.endsWith(".min"),
      lines: code.split("\n").length,
      maxLineUtf16: Math.max(...code.split("\n").map((s) => s.length)),
      evidence: "static_presence",
    },
  });
  const queue = [["halves", spec(n, "halves"), "canonical"]];
  if (variant === "acorn" || variant === "semver") {
    queue.push(["advance-second", extract(n, 1), "ladder"]);
    const firstFunction = units.findIndex(({ node }, i) => i > 0 && node.type === "FunctionDeclaration");
    if (firstFunction !== -1) queue.push(["advance-function", extract(n, firstFunction), "ladder"]);
  }
  for (const [id, base, family] of queue) {
    const placement = requestSpec(base);
    const specText = JSON.stringify(placement, null, 2);
    const specFile = path.join(tmp, variant + "-" + id + ".json");
    writeFileSync(specFile, specText);
    const request = {
      source: file,
      spec: specFile,
      out: path.join(tmp, variant + "-" + id),
      require: "all-outcomes",
      timeoutMs: 90000,
    };
    let observed;
    try {
      const result = split(request);
      let replay = null;
      if (result.status === "certified") {
        const disk = split({ ...request, checkOutput: true });
        replay = {
          sameVerdict: JSON.stringify(disk.verdict) === JSON.stringify(result.verdict),
          smoke: await smoke(library, path.join(request.out, placement.entry)),
        };
        replay.matchesBaseline = JSON.stringify(replay.smoke) === JSON.stringify(baseline);
      }
      observed = {
        evidence: "observed",
        verdict: result.verdict,
        files: result.files,
        replay,
        accepted:
          result.status === "certified" &&
          result.verdict.strategy === "compiled-constructor" &&
          replay.sameVerdict &&
          replay.matchesBaseline,
      };
    } catch (error) {
      observed = { evidence: "unknown", error: String(error).slice(0, 500), accepted: false };
    }
    const input = { source, sourceText: code, spec: placement, entry: placement.entry };
    const probes = probe(
      process.env.LINKED_INPUT_CHECKER,
      [],
      JSON.stringify(["singletonSourceShape", "indexedInlinePlan"].map((kind) => ({ kind, input }))) + "\n"
    );
    placements.push({
      variant,
      id,
      family,
      placement,
      specSha256: sha(specText),
      observed,
      probes,
      origins: placementOrigins(placement, references, n),
    });
    console.log(
      JSON.stringify({
        variant,
        id,
        units: n,
        bytes: Buffer.byteLength(code),
        sourceSha256: sha(code),
        specSha256: sha(specText),
        ownerCounts: Object.fromEntries(Object.entries(placement.modules).map(([k, v]) => [k, v.length])),
        entry: placement.entry,
        order: placement.order,
        originalSmoke: baseline.ok,
        firstLowering: result.lowering_refusals?.[0] ?? null,
        sourceShape: Array.isArray(probes) ? (probes[0].error ?? "decoded") : probes.error,
        opaque: result.opaque,
        unitRefused: result.refused,
        verdict: observed.verdict,
        accepted: observed.accepted,
        loweringRefusals: result.lowering_refusals?.length,
        independentSpans: inventory.length,
      })
    );
  }
}

const examples = Object.fromEntries(
  Object.entries(cases).map(([feature, code]) => [
    feature,
    {
      code,
      independentLowering: diagnostic(parse(code), "example-" + feature).result,
      evidenceScope: "standalone_empty_profile_lowering_only",
    },
  ])
);
// A full cut sweep establishes the historical unit count. Matching counts do
// not authenticate the archived bytes, and a differing count blocks comparison.
const archivedUnits = new Map();
for (const { bundle, strategy, args } of archiveRows) {
  if (strategy === "cut") archivedUnits.set(bundle, Math.max(archivedUnits.get(bundle) ?? 0, args[0] + 1));
}
for (const row of archiveRows) {
  const source = sources.find(({ variant }) => variant === row.bundle);
  row.archivedUnits = archivedUnits.get(row.bundle);
  row.currentOrdinaryVerdict = "not_replayed";
  if (!source || row.archivedUnits !== source.units) {
    row.comparability = source ? "different_unit_count" : "unknown_source";
    continue;
  }
  row.comparability = "same_unit_count_source_identity_unknown";
  const { entry, modules } = spec(source.units, row.strategy === "cut_entry_first" ? "cut" : row.strategy, row.args);
  row.regeneratedSelection = {
    entry,
    ownerCounts: Object.fromEntries(Object.entries(modules).map(([name, selectors]) => [name, selectors.length])),
    selectionSha256: sha(JSON.stringify({ entry, modules })),
    matchesArchivedOwnerCount: Object.keys(modules).length === row.modules,
  };
}
const featuresFor = (s) => [...new Set(s.spans.map((span) => span.feature))].sort();
const intersections = new Map();
for (const row of placements.filter(({ observed }) => !observed.accepted)) {
  const features = [...featuresFor(sources.find((s) => s.variant === row.variant)), "missing-construction"];
  if (row.origins.crossingWrites) features.push("archive-AssignsHome");
  if (row.origins.eagerOrderCandidates) features.push("archive-OrderOk");
  features.sort();
  const key = features.join(",");
  if (!intersections.has(key)) intersections.set(key, { features, placements: [], verifiedFinishCount: null });
  intersections.get(key).placements.push(row.variant + "/" + row.id);
}
const ranking = [...intersections.values()]
  .map((row) => ({ ...row, inventoryCoverCount: row.placements.length, verifiedFinishCount: null }))
  .sort((a, b) => b.inventoryCoverCount - a.inventoryCoverCount);
const report = {
  schema: 1,
  evidence: {
    observed: "fresh ordinary CLI, diagnostic decoder, and recorded counterexample runs are separate fields",
    static_presence: "syntax and scope evidence only; nesting does not establish execution or lack of support",
    unknown: "not tested or failed to produce usable evidence",
    archived: "historical structural/Node matrix; no ordinary CLI verdict implied",
    ranking:
      "exact complete static feature-set groups for unresolved current placements; verified finish counts are unknown",
  },
  sources,
  placements,
  archiveRows,
  examples,
  ranking,
};
writeFileSync(path.join(out, "full-blocker-census.json"), JSON.stringify(report));
const compressed = gzipSync(JSON.stringify(report));
writeFileSync(path.join(out, "full-blocker-census.json.gz"), compressed);
if (process.env.CENSUS_PRINT_ARCHIVE === "1") {
  const encoded = compressed.toString("base64");
  for (let i = 0; i < encoded.length; i += 8000) console.log("census-archive:" + encoded.slice(i, i + 8000));
}
assert.equal(sources.length, 8);
assert.equal(new Set(placements.map(({ variant, specSha256 }) => variant + "/" + specSha256)).size, placements.length);
assert(
  placements.every(({ observed }) => observed.evidence === "observed"),
  "ordinary replay did not produce every verdict"
);
console.log(
  JSON.stringify({
    sources: sources.length,
    placements: placements.length,
    archived: archiveRows.length,
    certified: placements.filter(({ observed }) => observed.accepted).length,
    ranking,
  })
);
