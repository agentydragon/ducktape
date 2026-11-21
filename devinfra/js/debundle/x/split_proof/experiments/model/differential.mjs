// The CoreJS model against Node: every program the front end accepts must print the same and end the same way in both.
//
//   node differential.mjs [--count N] [--seed S] [--show]
//
// Three sources of programs, and one more check:
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
//   - the members of the host's prototypes: the model's lists must be Node's.
//
// A run of the model ends "normal", "throw" (with the thrown value described as `describe` does below), "unknown" (it
// reached code the model does not define; what it printed before must be the start of what Node prints) or "fuel".
// Fails on any disagreement, on "fuel", and on an "unknown" the model blames on itself.
//
// `COREJS` and `EVALORDER` are the executables (default: Lake's build).
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { format } from "node:util";
import { kindOf } from "../translation/gen/core.mjs";
import { generateProgram, renderProgram } from "../translation/gen/index.mjs";
import { cases } from "./cases.mjs";
import { moduleCases } from "./module_cases.mjs";
import { Refused, toCore, toCoreProgram } from "./to_core.mjs";

const option = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i < 0 ? fallback : Number(process.argv[i + 1]);
};
const count = option("count", 40);
const firstSeed = option("seed", 1);
const show = process.argv.includes("--show");
const here = path.dirname(fileURLToPath(import.meta.url));
const built = (name) => path.join(here, "../../lean/.lake/build/bin", name);
const corejs = process.env.COREJS ?? built("corejs");
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
if (failures.length > 0) {
  console.error(failures.slice(0, 20).join("\n"));
  console.error(`${failures.length} failures`);
}
if (failures.length > 0) process.exit(1);
assert(
  summary.every((s) => s.compared > 0),
  "a set of families has no program to compare"
);
console.log("the model agrees with Node on every program it runs to the end");
