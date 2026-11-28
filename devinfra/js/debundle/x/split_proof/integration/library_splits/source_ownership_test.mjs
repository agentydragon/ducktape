import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { gunzipSync, gzipSync } from "node:zlib";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import ts from "typescript";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";
import { attribute, composeAcorn, sha256 } from "./source_ownership.mjs";
import { smoke } from "./smoke.mjs";

const json = (file) => JSON.parse(readFileSync(file));
const compressed = (file) => JSON.parse(gunzipSync(readFileSync(new URL(file, import.meta.url))));
const pins = json(new URL("./source_pins.json", import.meta.url));
const census = compressed("./results/full-blocker-census.json.gz");
const acornBuild = compressed("./results/acorn-8.19.0-rollup-map.json.gz");

function sourceHome(library, map) {
  if (library === "acorn") {
    const compose = composeAcorn(map.sourcesContent[0], acornBuild);
    return (_, point) => compose(point);
  }
  // Rebuild each candidate only once. Same-named TS/JS is not enough.
  const zod = new Map();
  if (library === "zod") {
    const root = path.dirname(createRequire(import.meta.url).resolve("zod/package.json"));
    for (const [i, file] of map.sources.entries()) {
      const relative = /\/node_modules\/zod\/(.*)\.js$/.exec(file)?.[1];
      if (!relative) continue;
      const authored = relative + ".ts";
      const rebuilt = ts.transpileModule(readFileSync(path.join(root, "src", authored), "utf8"), {
        fileName: authored,
        compilerOptions: {
          target: ts.ScriptTarget.ES2020,
          module: ts.ModuleKind.ESNext,
          experimentalDecorators: true,
          emitDecoratorMetadata: true,
          jsx: ts.JsxEmit.React,
        },
      }).outputText;
      assert.equal(sha256(rebuilt), sha256(map.sourcesContent[i]), `Zod authoring replay differs: ${authored}`);
      zod.set("zod/" + relative + ".js", "zod/src/" + authored);
    }
  }
  return (input) => zod.get(input.file) ?? null;
}

function summarize(rows, ownerFor) {
  const totals = new Map();
  for (const row of rows) {
    const owner = ownerFor(row);
    if (owner === undefined) continue;
    if (!totals.has(owner)) totals.set(owner, { owner, units: 0, inputBytes: 0, attribution: {} });
    const total = totals.get(owner);
    const bytes = row.endByte - row.startByte;
    const classification = row.classification === "source_file" ? row.originals[0].file : row.classification;
    const counts = (total.attribution[classification] ??= { units: 0, inputBytes: 0, mappedBytes: 0 });
    counts.units++;
    counts.inputBytes += bytes;
    counts.mappedBytes += row.mappedBytes;
    total.units++;
    total.inputBytes += bytes;
  }
  return [...totals.values()];
}

function requestedOwners(placement, rows) {
  const owners = new Map();
  for (const [owner, selectors] of Object.entries(placement.modules)) {
    for (const selector of selectors) {
      assert.match(selector, /^#\d+$/);
      const ordinal = Number(selector.slice(1));
      assert(rows[ordinal] && !owners.has(ordinal));
      owners.set(ordinal, owner);
    }
  }
  assert.equal(owners.size, rows.length);
  return owners;
}

function sourceFilePlacement(rows) {
  const entry = "entry.mjs";
  const modules = {};
  for (const row of rows) {
    const owner = row.classification === "source_file" ? row.originals[0].file + ".mjs" : entry;
    (modules[owner] ??= []).push("#" + row.ordinal);
  }
  return { entry, modules, order: Object.keys(modules).filter((key) => key !== entry), sequential: true };
}

function flatSpec(parsed, units, placement, owners, work) {
  const variables = parsed.scopes.acquire(parsed.ast, true).variables;
  const names = units.map(({ start, end }) =>
    variables
      .filter(({ defs }) => defs.some(({ name }) => name.start >= start && name.start < end))
      .map(({ name }) => name)
  );
  const logical = {};
  const order = placement.order.map((owner, i) => {
    // Use stable logical paths, with no case folding or extension ambiguity.
    const route = "owners/" + String(i);
    const indexes = units.flatMap((_, j) => (owners.get(j) === owner ? [j] : []));
    logical[route] = {
      members: [...new Set(indexes.flatMap((j) => names[j]))].map((name) => ({
        name,
        selector: { binding: { name } },
      })),
      anonymous_statements: indexes
        .filter((j) => !names[j].length)
        .map((j) => ({
          match: parsed.code.slice(units[j].start, units[j].end),
        })),
    };
    return route;
  });
  return {
    inputs: { input_root: work, js_list_path: path.join(work, "files.txt") },
    logical_modules: { main: logical },
    import_order: { main: order },
    unassigned_mode: { main: { kind: "inline_in_entry" } },
    write_js_tree: { out_dir: path.join(work, "rust") },
    materialize_logical_modules: { report_out_dir: path.join(work, "reports") },
  };
}

function run(program, args) {
  assert(program);
  const result = spawnSync(program, args, { encoding: "utf8", timeout: 90_000, maxBuffer: 32 * 1024 * 1024 });
  if (result.error && result.error.code !== "ETIMEDOUT") throw result.error;
  return result;
}

function files(root) {
  if (!existsSync(root)) return [];
  return readdirSync(root, { recursive: true, withFileTypes: true })
    .filter((file) => file.isFile() && /\.(?:mjs|js)$/.test(file.name))
    .map((file) => path.relative(root, path.join(file.parentPath, file.name)))
    .sort();
}

// No output source map is available. Match a top-level AST only when it is
// unique on both sides, across files. Import/export glue or renamed statements
// do not supply evidence for the location of an original unit.
function fingerprint(node) {
  if (node.type.startsWith("Import") || node.type.startsWith("Export")) return null;
  return JSON.stringify(node, (key, value) => {
    if (["start", "end", "loc", "range", "raw"].includes(key)) return undefined;
    return typeof value === "bigint" ? { bigint: String(value) } : value;
  });
}

function resultFiles(out, paths, originals, rows) {
  if (!paths.length) return { files: [], recovered: [], matches: [], unmatched: null };
  const index = new Map();
  const existing = new Map();
  const insert = (map, key, value) => {
    if (key !== null) map.set(key, map.has(key) ? null : value);
  };
  originals.forEach(({ node }, ordinal) => insert(index, fingerprint(node), ordinal));
  const emitted = [];
  for (const owner of paths) {
    const code = readFileSync(path.join(out, owner), "utf8");
    const units = topLevelStatements(acorn.parse(code, { ecmaVersion: "latest", sourceType: "module" }));
    emitted.push({ owner, emittedBytes: Buffer.byteLength(code), emittedUnits: units.length });
    for (const { node } of units) insert(existing, fingerprint(node), owner);
  }
  const matched = new Map();
  for (const [key, ordinal] of index) {
    const owner = existing.get(key);
    if (ordinal !== null && owner != null) matched.set(ordinal, owner);
  }
  const unmatched = summarize(rows, ({ ordinal }) => (matched.has(ordinal) ? undefined : null))[0] ?? null;
  return {
    files: emitted,
    matches: [...matched].map(([ordinal, owner]) => ({ ordinal, owner })),
    recovered: summarize(rows, ({ ordinal }) => matched.get(ordinal)),
    unmatched,
  };
}

async function replayRust(source, parsed, units, rows, placement, owners, work, library, baseline) {
  const rustSpec = flatSpec(parsed, units, placement, owners, work);
  copyFileSync(source, path.join(work, "main.js"));
  writeFileSync(path.join(work, "package.json"), '{"type":"module"}\n');
  writeFileSync(path.join(work, "files.txt"), "main.js\n");
  const spec = path.join(work, "rust.json");
  writeFileSync(spec, JSON.stringify(rustSpec, null, 2));
  const resolvedFile = path.join(work, "resolved.json");
  const resolver = run(process.env.DEBUNDLE_RESOLVER, ["--spec", spec, "--chunk", "main", "--out", resolvedFile]);
  if (resolver.error) return { status: "timeout", phase: "resolution", output: null };
  if (resolver.status !== 0) {
    return {
      status: "unresolved",
      diagnostic: resolver.stderr.trim().replaceAll(work, "<work>").slice(0, 2000),
      output: null,
    };
  }
  const resolved = json(resolvedFile);
  assert.equal(resolved.source_sha256, pins[library].sha256);
  assert.deepEqual(
    resolved.units.map(({ ordinal }) => ordinal),
    rows.map(({ ordinal }) => ordinal)
  );
  const expected = placement.order.map((owner, index) => [owner, rustSpec.import_order.main[index]]);
  const routes = new Map(expected);
  const mismatches = resolved.units.flatMap(({ ordinal, owner }) =>
    owner === (routes.get(owners.get(ordinal)) ?? null) ? [] : [{ ordinal, resolved: owner }]
  );
  if (mismatches.length) return { status: "different_assignment", mismatches, output: null };
  const result = run(process.env.DEBUNDLE, ["run", "--spec", spec]);
  if (result.error) return { status: "timeout", phase: "run", routes: Object.fromEntries(expected), output: null };
  const out = rustSpec.write_js_tree.out_dir;
  const paths = files(out);
  const refused =
    result.status !== 0 &&
    ["cycles.json", "atomic_group_conflicts.json"].some((file) => existsSync(path.join(work, "reports", "main", file)));
  assert(result.status === 0 || refused, result.stderr);
  assert.equal(paths.length, result.status === 0 ? placement.order.length + 1 : 0);
  const output = resultFiles(out, paths, units, rows);
  const entry = "app/main/entry.js";
  if (result.status === 0) assert(paths.includes(entry), JSON.stringify(paths));
  const behavior = result.status === 0 ? await smoke(library, path.join(out, entry)) : null;
  return {
    status: result.status === 0 ? "emitted" : "refused",
    exitCode: result.status,
    ...(result.status !== 0 && { diagnostic: result.stderr.trim().replaceAll(work, "<work>").slice(0, 2000) }),
    routes: Object.fromEntries([
      [placement.entry, entry],
      ...expected.map(([requested, logical]) => [requested, "app/main/" + logical + ".js"]),
    ]),
    output,
    smokeMatches: behavior === null ? null : JSON.stringify(behavior) === JSON.stringify(baseline),
  };
}

function grouping(rows, ownerFor, residual) {
  const groups = new Map();
  const owners = new Map();
  const pairs = (n) => (n * (n - 1)) / 2;
  for (const row of rows) {
    if (row.classification !== "source_file") continue;
    const home = row.originals[0].file;
    if (!groups.has(home)) groups.set(home, { file: home, surviving: 0, destinations: {} });
    const group = groups.get(home);
    group.surviving++;
    const owner = ownerFor(row);
    if (owner === undefined) continue;
    group.destinations[owner] = (group.destinations[owner] ?? 0) + 1;
    owners.set(owner, (owners.get(owner) ?? 0) + 1);
  }
  const perFile = [...groups.values()].map((group) => {
    const counts = Object.entries(group.destinations);
    const matched = counts.reduce((sum, [, count]) => sum + count, 0);
    const largest = Math.max(0, ...counts.map(([, count]) => count));
    const best = counts
      .filter(([, count]) => count === largest)
      .map(([owner, count]) => ({
        owner,
        together: count,
        unrelatedKnown: owners.get(owner) - count,
      }));
    return { ...group, best, unknownOutput: group.surviving - matched, inResidual: group.destinations[residual] ?? 0 };
  });
  return {
    perFile,
    sameFileTogether: perFile.reduce(
      (sum, row) => sum + Object.values(row.destinations).reduce((n, count) => n + pairs(count), 0),
      0
    ),
    sameInputFilePairs: perFile.reduce((sum, row) => sum + pairs(row.surviving), 0),
    knownOutputPairs: [...owners.values()].reduce((sum, count) => sum + pairs(count), 0),
  };
}

const sources = [];
const placements = [];
for (const sourceArg of process.argv.slice(2)) {
  const source = path.resolve(sourceArg);
  const library = path.basename(source, ".mjs");
  const prefix = source.slice(0, -4);
  const code = readFileSync(source, "utf8");
  const parsed = parseModule(code, acorn.parse, eslintScope);
  const units = topLevelStatements(parsed.ast);
  const { bytes, units: n, sha256: pin } = pins[library];
  assert.equal(sha256(code), pin);
  assert.equal(Buffer.byteLength(code), bytes);
  assert.equal(units.length, n);
  const censusSource = census.sources.find(({ variant, sha256 }) => variant === library && sha256 === pin);
  assert.deepEqual(
    censusSource.unitOrigins,
    units.map(({ node, start, end }, ordinal) => ({ ordinal, start, end, type: node.type }))
  );
  const map = json(prefix + ".map");
  const { rows, inputs } = attribute(library, code, units, map, json(prefix + ".meta.json"), sourceHome(library, map));
  const [total] = summarize(rows, () => "bundle");
  const baseline = await smoke(library, source);
  assert(baseline.ok, JSON.stringify(baseline));
  sources.push({
    library,
    sha256: pin,
    bytes,
    outsideUnitBytes: bytes - total.inputBytes,
    total,
    inputs,
    rows,
    baseline,
  });
  const halves = census.placements.find(({ variant, id }) => variant === library && id === "halves");
  for (const [id, placement] of [
    ["halves", halves.placement],
    ["source_files", sourceFilePlacement(rows)],
  ]) {
    const work = path.join(process.env.TEST_TMPDIR, library + "-" + id);
    mkdirSync(work);
    const specText = JSON.stringify(placement, null, 2);
    const spec = path.join(work, "strict.json");
    writeFileSync(spec, specText);
    const specSha256 = sha256(specText);
    if (id === "halves") assert.equal(specSha256, halves.specSha256);
    const owners = requestedOwners(placement, rows);
    const strictOut = path.join(work, "strict");
    const request = { source, spec, out: strictOut, require: "all-outcomes", timeoutMs: 90_000 };
    const strict = split(request);
    const accepted = strict.status === "certified";
    const checked = accepted ? split({ ...request, checkOutput: true }) : null;
    if (accepted) assert.deepEqual(checked.verdict, strict.verdict);
    const behavior = accepted ? await smoke(library, path.join(strictOut, placement.entry)) : null;
    const rust = await replayRust(source, parsed, units, rows, placement, owners, work, library, baseline);
    const outputGrouping = (output, residual) => {
      if (!output?.files.length) return null;
      const matched = new Map(output.matches.map(({ ordinal, owner }) => [ordinal, owner]));
      return grouping(rows, ({ ordinal }) => matched.get(ordinal), residual);
    };
    const strictOutput = resultFiles(strictOut, strict.files, units, rows);
    const row = {
      library,
      sourceSha256: pin,
      id,
      specSha256,
      placement,
      requested: summarize(rows, ({ ordinal }) => owners.get(ordinal)),
      requestedGrouping: grouping(rows, ({ ordinal }) => owners.get(ordinal), placement.entry),
      rust: { ...rust, grouping: outputGrouping(rust.output, rust.routes?.[placement.entry]) },
      strict: {
        verdict: strict.verdict,
        output: strictOutput,
        grouping: outputGrouping(strictOutput, placement.entry),
        diskChecked: accepted,
        smokeMatches: behavior === null ? null : JSON.stringify(behavior) === JSON.stringify(baseline),
      },
    };
    if (rust.status === "emitted") assert(rust.smokeMatches, library + "/" + id);
    if (accepted) assert(row.strict.smokeMatches, library + "/" + id);
    placements.push(row);
    console.log(
      JSON.stringify({
        library,
        id,
        rust: rust.status,
        rustSmoke: rust.smokeMatches,
        rustFiles: rust.output?.files.length,
        rustError: rust.diagnostic?.slice(0, 350),
        strict: strict.status,
        strictFiles: strict.files.length,
        requestedOwners: row.requested.length,
        rustRecovered: rust.output?.matches.length,
        rustMatchedBytes: rust.output?.recovered.reduce((sum, owner) => sum + owner.inputBytes, 0),
        rustEmittedBytes: rust.output?.files.reduce((sum, owner) => sum + owner.emittedBytes, 0),
        rustGrouping: row.rust.grouping && {
          correctlyTogether: row.rust.grouping.sameFileTogether,
          inputPairs: row.rust.grouping.sameInputFilePairs,
          outputPairs: row.rust.grouping.knownOutputPairs,
        },
        strictReason: strict.reasonCode,
      })
    );
  }
}
assert.equal(sources.length, Object.keys(pins).length);
writeFileSync(
  path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "source-ownership.json.gz"),
  gzipSync(JSON.stringify({ sources, placements }) + "\n", { level: 9 })
);
