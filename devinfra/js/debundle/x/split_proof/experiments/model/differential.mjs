// The CoreJS model against Node: every program the front end accepts must print the same and end the same way in both.
//
//   node differential.mjs [--count N] [--seed S] [--splits K] [--show]
//
// Four sources of programs, and one more check:
//   - `cases.mjs`, written by hand;
//   - `module_cases.mjs`, programs of several modules, written by hand. The order in which the model runs the modules
//     must also be the one `evalorder` prints for the import graph (`Plan.evalOrder` of the proved checker);
//   - the generator of `../translation/gen`, N programs for each set of families from seed S on. The generator leans on
//     the standard library, so nearly no whole program is in the fragment. A program is cut down to the units the
//     front end accepts alone and whose names are all declared by units that are kept, and one `console.log` is added
//     for each binding (and each field of an object) that the generator gave a primitive type. A unit in which the
//     model then reaches unknown code (a method of a string, say) is left out too, and the program is tried again.
//     The table counts the units of the generated programs (`units`), those the front end accepts alone
//     (`unitsAccepted`), those in the programs that ran (`unitsRun`), and the programs with no unit left (`empty`);
//   - splits by the real tool. K of the programs above that the model runs to the end (the hand-written ones first)
//     are split by `lowersplit` under three specs each: consecutive pieces, a random module for each statement with
//     the modules run in order (`sequential`), and the same without an order. For every split it accepts, four runs
//     must agree: the bundle in Node, the bundle in the model, the split in Node, the split in the model. The split's
//     statements must be the bundle's, token for token, and the model must run the modules in the order of the plan.
//     Bundle and split differing in Node is a wrong accept by the tool, and is reported as that.
//     The tool's verdict is for a bundle whose evaluation completes (premise P4 of `../effects/README.md`). So a split
//     is compared with its bundle when the bundle ends normally in Node, or when the split runs the statements in the
//     bundle's order, which keeps every ending. The other accepted splits are counted apart; the model must still
//     agree with Node on each of them. `splitCases` of `cases.mjs` has one of those as a named case;
//   - the members of the host's prototypes: the model's lists must be Node's.
//
// A run of the model ends "normal", "throw" (with the thrown value described as `describe` does below), "unknown" (it
// reached code the model does not define; what it printed before must be the start of what Node prints) or "fuel".
// Fails on any disagreement, on "fuel", and on an "unknown" the model blames on itself.
//
// `COREJS`, `LOWERSPLIT` and `EVALORDER` are the executables (default: Lake's build).
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { format } from "node:util";
import { kindOf } from "../translation/gen/core.mjs";
import { generateProgram, renderProgram } from "../translation/gen/index.mjs";
import { dumpOf } from "../translation/split_dump.mjs";
import { cases, splitCases } from "./cases.mjs";
import { moduleCases } from "./module_cases.mjs";
import { Refused, toCore, toCoreProgram } from "./to_core.mjs";

const option = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i < 0 ? fallback : Number(process.argv[i + 1]);
};
const count = option("count", 40);
const firstSeed = option("seed", 1);
const splitPrograms = option("splits", 60);
const show = process.argv.includes("--show");
const here = path.dirname(fileURLToPath(import.meta.url));
const built = (name) => path.join(here, "../../lean/.lake/build/bin", name);
const corejs = process.env.COREJS ?? built("corejs");
const lowersplit = process.env.LOWERSPLIT ?? built("lowersplit");
const evalorder = process.env.EVALORDER ?? built("evalorder");
const work = mkdtempSync(path.join(process.env.TEST_TMPDIR ?? os.tmpdir(), "corejs_differential_"));

// The sets of `gen_validity_test.mjs`: each family with the ones before it.
const SETS = [
  ["expressions", "statements"],
  ["expressions", "statements", "functions"],
  ["expressions", "statements", "functions", "classes"],
  ["expressions", "statements", "functions", "classes", "patterns"],
  ["expressions", "statements", "functions", "classes", "patterns", "protocols"],
  ["expressions", "statements", "functions", "classes", "patterns", "protocols", "modules", "unmodeled"],
];
// How often a generated program is tried again without the unit in which the model reached unknown code.
const ROUNDS = 8;

/** `{ tokens, units, … }` for `source`, or `null` if the front end refuses it. */
function accepted(source, options) {
  try {
    return toCore(source, options);
  } catch (e) {
    if (e instanceof Refused || e instanceof SyntaxError) return null;
    throw e;
  }
}

/** A thrown value, as `describe` of `Run.lean` writes it. */
function describe(v) {
  if (v instanceof Error) return v.constructor.name;
  if (v === null || v === undefined) return String(v);
  if (typeof v === "object" || typeof v === "function") return typeof v;
  return `${typeof v}:${String(v)}`;
}

/** What the module `file` does in Node: `{ out, end, detail }`. */
async function runFileInNode(file) {
  const out = [];
  const log = console.log;
  console.log = (...args) => out.push(format(...args));
  try {
    await import(pathToFileURL(file));
    return { out, end: "normal", detail: "" };
  } catch (e) {
    return { out, end: "throw", detail: describe(e) };
  } finally {
    console.log = log;
  }
}

let directories = 0;
/** A new directory with `files`. */
function writeFiles(files) {
  const dir = path.join(work, `d${directories++}`);
  for (const [name, text] of Object.entries(files)) {
    mkdirSync(path.dirname(path.join(dir, name)), { recursive: true });
    writeFileSync(path.join(dir, name), text);
  }
  return dir;
}

const runInNode = (source) => runFileInNode(path.join(writeFiles({ "main.mjs": source }), "main.mjs"));

let modelSeconds = 0;
/** Sets `job.result` to what the model does with `job.program`, for all `jobs` in one call of `corejs`. */
function runModel(jobs) {
  if (jobs.length === 0) return;
  const input = path.join(work, "programs.jsonl");
  writeFileSync(input, jobs.map((j) => JSON.stringify(j.program)).join("\n") + "\n");
  const started = Date.now();
  const model = spawnSync(corejs, [input], { encoding: "utf8", maxBuffer: 1 << 28 });
  modelSeconds += (Date.now() - started) / 1000;
  assert.equal(model.status, 0, `corejs failed: ${model.stderr}`);
  const lines = model.stdout.trimEnd().split("\n");
  assert.equal(lines.length, jobs.length, "corejs did not answer every program");
  jobs.forEach((job, i) => (job.result = JSON.parse(lines[i])));
}

/**
 * A generated program cut down to the fragment, with observers of its primitive state. `banned` are units to leave out
 * although the front end accepts them. `owner[j]` is the unit that statement `j` of the result came from.
 */
function slice(program, banned) {
  const one = (unit) => renderProgram({ ...program, units: [unit], exports: [], imports: [] });
  const primitive = (type) => ["num", "str", "bool"].includes(kindOf(type));
  const declared = new Set();
  const kept = [];
  const owner = [];
  const observers = [];
  let acceptedUnits = 0;
  for (const unit of program.units) {
    const alone = accepted(one(unit));
    if (alone === null) continue;
    acceptedUnits++;
    if (banned.has(unit) || !unit.uses.every((name) => declared.has(name))) continue;
    kept.push(unit);
    for (let i = 0; i < alone.units; i++) owner.push(unit);
    for (const b of unit.bindings) {
      declared.add(b.name);
      if (primitive(b.type)) observers.push(`console.log("=${b.name}", ${b.name});`);
      else if (kindOf(b.type) === "obj")
        for (const [field, type] of Object.entries(b.type.fields))
          if (primitive(type))
            observers.push(
              `console.log(${JSON.stringify(`=${b.name}.${field}`)}, ${b.name}[${JSON.stringify(field)}]);`
            );
    }
  }
  const source = renderProgram({ ...program, units: kept, exports: [], imports: [] }) + observers.join("\n") + "\n";
  return { source, owner, units: program.units.length, acceptedUnits, keptUnits: kept.length };
}

const failures = [];
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const sameRun = (a, b) => a.end === b.end && a.detail === b.detail && same(a.out, b.out);

// { label, source | files, program, result, expectEnd, set }: `program` is what `corejs` reads
const jobs = [];
for (const c of cases) {
  const translated = accepted(c.source, { allowHost: c.allowHost ?? false });
  assert(translated !== null, `case ${c.name}: the front end refuses it`);
  jobs.push({
    label: `case ${c.name}`,
    source: c.source,
    program: translated.tokens,
    expectEnd: c.end ?? null,
    set: null,
  });
}
for (const c of splitCases) {
  const translated = accepted(c.source);
  assert(translated !== null, `split case ${c.name}: the front end refuses the bundle`);
  jobs.push({
    label: `split case ${c.name}`,
    source: c.source,
    program: translated.tokens,
    expectEnd: null,
    set: null,
    splitCase: c,
  });
}
for (const c of moduleCases) {
  const entry = path.join(writeFiles(c.files), "main.mjs");
  jobs.push({
    label: `modules: ${c.name}`,
    source: Object.entries(c.files)
      .map(([name, text]) => `// ${name}${text}`)
      .join("\n"),
    entry,
    program: toCoreProgram(entry),
    expectEnd: c.end ?? null,
    set: null,
  });
}
runModel(jobs);

const summary = SETS.map((families) => ({
  families: families.join("+"),
  programs: 0,
  wholeAccepted: 0,
  units: 0,
  unitsAccepted: 0,
  unitsRun: 0,
  empty: 0,
  compared: 0,
  agree: 0,
  unknown: 0,
}));
const reasons = {}; // the unknown code that units of generated programs were left out for
let pending = [];
SETS.forEach((families, set) => {
  for (let seed = firstSeed; seed < firstSeed + count; seed++) {
    const program = generateProgram(seed, { families });
    summary[set].programs++;
    if (accepted(renderProgram(program)) !== null) summary[set].wholeAccepted++;
    pending.push({
      label: `seed ${seed} of ${families.join("+")}`,
      generated: program,
      banned: new Set(),
      expectEnd: null,
      set,
    });
  }
});
for (let round = 0; pending.length > 0; round++) {
  const tried = [];
  for (const job of pending) {
    job.cut = slice(job.generated, job.banned);
    job.source = job.cut.source;
    const translated = accepted(job.source);
    assert(translated !== null, `${job.label}: the kept units are refused together\n${job.source}`);
    job.program = translated.tokens;
    tried.push(job);
  }
  runModel(tried);
  pending = [];
  for (const job of tried) {
    const unit = job.cut.owner[job.result.region - 1];
    if (
      job.result.end === "unknown" &&
      !job.result.detail.startsWith("model:") &&
      unit !== undefined &&
      round < ROUNDS
    ) {
      reasons[job.result.detail] = (reasons[job.result.detail] ?? 0) + 1;
      job.banned.add(unit);
      pending.push(job);
      continue;
    }
    const s = summary[job.set];
    s.units += job.cut.units;
    s.unitsAccepted += job.cut.acceptedUnits;
    s.unitsRun += job.cut.keptUnits;
    if (job.cut.keptUnits === 0) s.empty++;
    else jobs.push(job);
  }
}

/** The order `Plan.evalOrder` gives the import graph of `program`. */
function planOrder(program) {
  const graph = {
    entry: program.entry,
    modules: Object.fromEntries(program.modules.map((m) => [m.name, m.imports.map((i) => i.source)])),
  };
  const file = path.join(work, "graph.json");
  writeFileSync(file, JSON.stringify(graph));
  const run = spawnSync(evalorder, [file], { encoding: "utf8" });
  assert.equal(run.status, 0, `evalorder failed: ${run.stderr}`);
  return run.stdout.trimEnd().split("\n");
}

const ends = {};
let orders = 0;
for (const job of jobs) {
  const got = job.result;
  ends[got.end] = (ends[got.end] ?? 0) + 1;
  const fail = (why, node) =>
    failures.push(
      `${job.label}: ${why}\n  model: ${JSON.stringify(got)}\n  node:  ${JSON.stringify(node)}\n${show ? job.source : ""}`
    );
  if (job.expectEnd !== null && got.end !== job.expectEnd) {
    fail(`the model should end ${job.expectEnd}`, null);
    continue;
  }
  // a program that uses a host value prints in Node what the host does; only the model's ending is checked
  if (job.expectEnd === "unknown") continue;
  job.node = job.entry ? await runFileInNode(job.entry) : await runInNode(job.source);
  const node = job.node;
  const s = job.set === null ? null : summary[job.set];
  if (got.end === "unknown") {
    if (got.detail.startsWith("model:")) fail("the model reached a state its rules exclude", node);
    else if (!same(node.out.slice(0, got.out.length), got.out)) fail("differs from Node before the unknown code", node);
    else if (s) s.unknown++;
    continue;
  }
  if (s) s.compared++;
  if (!sameRun(got, node)) fail("differs from Node", node);
  else if (s) s.agree++;
  if (job.entry) {
    orders++;
    const plan = planOrder(job.program);
    if (!same(plan, got.order)) fail(`the modules run in another order than Plan.evalOrder gives: ${plan}`, node);
  }
}

// Splits by the real tool.

/** A random number generator with a seed (mulberry32): `next()` is in [0, 1). */
function random(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Three specs for a bundle of `dump.statements`: `cut` (consecutive pieces, run in order), `ordered` (a random module
 * for each statement, the modules run in the order of their first statements) and `free` (the same, with the order
 * left to the imports). An `export` statement stays in the entry, as `lowersplit` requires.
 */
function specsFor(dump, next) {
  const n = dump.statements.length;
  const pinned = dump.statements.map((s) => /^(Import|Export)/.test(s.ast.type));
  const pieces = 2 + Math.floor(next() * 3);
  const name = (k) => (k === pieces - 1 ? "entry.mjs" : `m${k}.mjs`);
  const toSpec = (moduleOf, extra) => {
    const modules = { "entry.mjs": [] };
    moduleOf.forEach((k, i) => (modules[name(pinned[i] ? pieces - 1 : k)] ??= []).push(`#${i}`));
    return { entry: "entry.mjs", modules, ...extra };
  };
  const cuts = Array.from({ length: pieces - 1 }, () => Math.floor(next() * (n + 1))).sort((a, b) => a - b);
  const scatter = () => Array.from({ length: n }, () => Math.floor(next() * pieces));
  return {
    cut: toSpec(
      Array.from({ length: n }, (_, i) => cuts.filter((c) => c <= i).length),
      { sequential: true }
    ),
    ordered: toSpec(scatter(), { sequential: true }),
    free: toSpec(scatter(), {}),
  };
}

/** `source` as a one-module program whose statements have the numbers `lowersplit` gives them. */
function numbered(source) {
  const t = toCore(source);
  return { program: { entry: "main", modules: [{ name: "main", imports: [], ...t }] }, units: unitsOf(t) };
}

/** The tokens of each statement of a translated module, by its identity. */
const unitsOf = (t) => new Map(t.ids.map((id, k) => [id, t.tokens.slice(...t.spans[k])]));

const tally = {
  programs: 0,
  named: [0, 0],
  cut: [0, 0],
  ordered: [0, 0],
  free: [0, 0],
  unsupported: 0,
  agree: 0,
  orderKept: 0,
};
const apart = { splits: 0, differ: 0 }; // accepted splits that move code, of a bundle that does not end normally
const wrongAccepts = [];
const splitJobs = [];

/** Split the bundle of `job` by `spec`. An accepted split becomes a job; `named` is its entry of `splitCases`. */
function split(job, bundle, dump, kind, spec, named) {
  const dir = writeFiles({ "ast.json": JSON.stringify(dump), "spec.json": JSON.stringify(spec) });
  const out = path.join(dir, "out");
  const plan = path.join(dir, "plan.json");
  const run = spawnSync(lowersplit, [path.join(dir, "ast.json"), path.join(dir, "spec.json"), out, "--plan", plan], {
    encoding: "utf8",
  });
  const label = `${job.label}, split ${kind} ${JSON.stringify(spec)}`;
  if (named) assert.equal(run.status, 0, `${label}: lowersplit does not accept it\n${run.stdout}\n${run.stderr}`);
  if (run.status === 3) return tally.unsupported++;
  if (run.status === 1) return tally[kind][1]++;
  assert.equal(run.status, 0, `${label}: lowersplit failed\n${run.stdout}\n${run.stderr}`);
  tally[kind][0]++;
  const { order, modules } = JSON.parse(readFileSync(plan, "utf8"));
  const entry = path.join(out, "entry.mjs");
  const statements = order.flatMap((m) => modules[m]);
  const orderKept = statements.every((id, k) => k === 0 || statements[k - 1] < id);
  try {
    const program = toCoreProgram(entry, { ids: modules });
    splitJobs.push({ label, source: job.source, entry, order, orderKept, of: job, bundle, program, named });
  } catch (e) {
    failures.push(`${label}: the front end takes the bundle and not the split: ${e.message}`);
  }
}

const candidates = jobs.filter((j) => !j.entry && j.node && j.result.end !== "unknown" && sameRun(j.result, j.node));
for (const job of candidates.filter((j) => j.splitCase))
  split(job, numbered(job.source), dumpOf(job.source), "named", job.splitCase.spec, job.splitCase);
for (const [index, job] of candidates.slice(0, splitPrograms).entries()) {
  const dump = dumpOf(job.source);
  if (dump.statements.length < 2) continue;
  tally.programs++;
  const bundle = numbered(job.source);
  const next = random(firstSeed * 7919 + index);
  for (const [kind, spec] of Object.entries(specsFor(dump, next))) split(job, bundle, dump, kind, spec, null);
}
const bundleJobs = [...new Set(splitJobs.map((s) => s.of))];
for (const job of bundleJobs) job.numbered = { program: splitJobs.find((s) => s.of === job).bundle.program };
runModel(bundleJobs.map((j) => j.numbered));
runModel(splitJobs);
for (const job of splitJobs) {
  const files = () =>
    job.program.modules.map((m) => `// ${m.name}\n${readFileSync(path.join(path.dirname(job.entry), m.name), "utf8")}`);
  const text = () => `--- bundle\n${job.source}\n--- split\n${files().join("\n")}`;
  const node = await runFileInNode(job.entry);
  const runs = {
    "bundle in Node": job.of.node,
    "bundle in the model": job.of.numbered.result,
    "split in Node": node,
    "split in the model": job.result,
  };
  const table = Object.entries(runs)
    .map(([who, r]) => `  ${who}: ${JSON.stringify({ out: r.out, end: r.end, detail: r.detail })}`)
    .join("\n");
  if (!sameRun(job.result, node) || !sameRun(job.of.numbered.result, job.of.node)) {
    failures.push(`${job.label}: the model differs from Node\n${table}\n${show ? text() : ""}`);
    continue;
  }
  const differs = !sameRun(job.of.node, node);
  if (job.of.node.end !== "normal" && !job.orderKept) {
    // outside premise P4: the tool promises nothing, and the split is not compared with its bundle
    apart.splits++;
    if (differs) apart.differ++;
    if (job.named && !differs) failures.push(`${job.label}: expected to differ from its bundle\n${table}`);
    if (show && differs) console.log(`outside premise P4: ${job.label}\n${table}\n${text()}`);
    continue;
  }
  if (job.named?.differs) failures.push(`${job.label}: expected to be outside premise P4\n${table}`);
  if (differs) {
    wrongAccepts.push(`${job.label}\n${table}\n${text()}`);
    continue;
  }
  if (job.orderKept) tally.orderKept++;
  if (!same(job.result.order, job.order))
    failures.push(`${job.label}: the model runs the modules as ${job.result.order}, the plan as ${job.order}`);
  for (const m of job.program.modules)
    for (const [id, tokens] of unitsOf(m))
      if (!same(tokens, job.bundle.units.get(id)))
        failures.push(`${job.label}: statement ${id} of ${m.name} is not the bundle's statement ${id}`);
  tally.agree++;
}

// The members the model gives the host's prototypes are the ones this Node has.
const names = spawnSync(corejs, ["--host-names"], { encoding: "utf8" });
assert.equal(names.status, 0, `corejs --host-names failed: ${names.stderr}`);
for (const [owner, listed] of Object.entries(JSON.parse(names.stdout))) {
  const real = Object.getOwnPropertyNames(owner.split(".").reduce((o, k) => o[k], globalThis));
  if (!same([...listed].sort(), real.sort()))
    failures.push(`${owner}: the model lists ${listed.sort()}, Node has ${real}`);
}

rmSync(work, { recursive: true, force: true });
console.log(
  `cases: ${cases.length}; module cases: ${moduleCases.length} (${orders} orders compared with Plan.evalOrder); ` +
    `generated: ${count} programs for each set from seed ${firstSeed}`
);
console.table(summary);
console.log("unknown code that units of generated programs were left out for:", reasons);
console.log(`model endings: ${JSON.stringify(ends)}; corejs took ${modelSeconds.toFixed(1)} s`);
const verdicts = (kind) => `${kind}: ${tally[kind][0]} accepted, ${tally[kind][1]} rejected`;
console.log(
  `splits: ${tally.named[0]} named; ${tally.programs} programs; ${["cut", "ordered", "free"].map(verdicts).join("; ")}; ` +
    `${tally.unsupported} unsupported; ${tally.agree} accepted splits where the four runs agree, ` +
    `${tally.orderKept} of them with the statements in the bundle's order; ${apart.splits} accepted splits move code ` +
    `of a bundle that does not end normally and are not compared with it (premise P4), ${apart.differ} of them differ`
);
if (wrongAccepts.length > 0) {
  console.error(
    `WRONG ACCEPT by lowersplit: the split does not behave like the bundle in Node (${wrongAccepts.length})`
  );
  console.error(wrongAccepts.join("\n\n"));
}
if (failures.length > 0) {
  console.error(failures.slice(0, 20).join("\n"));
  console.error(`${failures.length} failures`);
}
if (failures.length > 0 || wrongAccepts.length > 0) process.exit(1);
assert(
  summary.every((s) => s.compared > 0),
  "a set of families has no program to compare"
);
console.log("the model agrees with Node on every program it runs to the end");
