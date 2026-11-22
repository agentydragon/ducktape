// The CoreJS model against Node: every program the front end accepts must print the same and end the same way in both.
//
//   node differential.mjs [--count N] [--seed S] [--splits K] [--show]
//
// Sources of programs, and the split checks:
//   - `cases.mjs`, written by hand;
//   - `module_cases.mjs`, programs of several modules, written by hand. The order in which the model runs the modules
//     must also be the one `evalorder` prints for the import graph (`Plan.evalOrder` of the proved checker);
//   - `generate.mjs`, bounded whole CoreJS programs, linked graphs and opaque closures. Every whole-program family
//     executes in a required accepted reordered split, independent of the random-plan budget below;
//   - the generator of `../translation/gen`, N programs for each set of families from seed S on. The generator leans on
//     the standard library, so nearly no whole program is in the fragment. A program is cut down to the units the
//     front end accepts alone and whose names are all declared by units that are kept, and one `console.log` is added
//     for each binding (and each field of an object) that the generator gave a primitive type. A unit in which the
//     model then reaches unknown code (a method of an array, say) is left out too, and the program is tried again.
//     The table counts the units of the generated programs (`units`), those the front end accepts alone
//     (`unitsAccepted`), those in the programs that ran (`unitsRun`), and the programs with no unit left (`empty`);
//   - splits by the real tool. K of the programs above that the model runs to the end (the hand-written ones first)
//     are split by `lowersplit` under three specs each: consecutive pieces, a random module for each statement with
//     the modules run in order (`sequential`), and the same without an order. For every split it accepts, four runs
//     must agree: the bundle in Node, the bundle in the model, the split in Node, the split in the model. The split's
//     statements must be the bundle's, token for token, and the model must run the modules in the order of the plan.
//     A split that runs the statements in the bundle's order must also end in the bundle's state in the model, every
//     object and binding at the same address, when it runs in the world the bundle recorded (`order_kept_preserves`
//     of `Split.lean` says it does).
//     Bundle and split differing in Node is a wrong accept by the tool, and is reported as that.
//     The tool's verdict is for a bundle whose evaluation completes (premise P4 of `../effects/README.md`). So a split
//     is compared with its bundle when the bundle ends normally in Node, or when the split runs the statements in the
//     bundle's order, which keeps every ending. The other accepted splits are counted apart; the model must still
//     agree with Node on each of them. `splitCases` of `cases.mjs` has one of those as a named case, and a split
//     that the tool must reject;
//   - the members of the host's prototypes: the model's lists must be Node's.
//
// The world. The model asks a world about the host functions that work on primitives (`worldFunction` of
// `Value.lean`; `corejs --host-names` prints them). The world of this test is Node itself, as a recording: every
// program runs in Node first, with those functions wrapped, and each call that has a primitive receiver and primitive
// arguments is written down with how it ended (a call from Node's own code, which runs while a program is loaded, is
// not: the caller has to be a file of the program). The model then runs in the world that answers exactly those calls
// in that order (`replay` of `Run.lean`). So the model has to make the calls Node made, the same function with the
// same receiver and arguments in the same order, and it gets Node's answers. A call of the model that is not the next
// recorded one fails the test, and so does a recorded call the model did not make before it ended. A listed function
// that returns an object fails it too: the list would be wrong.
//
// A run of the model ends "normal", "throw" (with the thrown value described as `describe` does below), "unknown" (it
// reached code the model does not define; what it printed before must be the start of what Node prints) or "fuel".
// Fails on any disagreement, on "fuel", and on an "unknown" the model blames on itself.
//
// `COREJS`, `LOWERSPLIT` and `EVALORDER` are the executables (default: Lake's build).
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { appendFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { format } from "node:util";
import { kindOf } from "../translation/gen/core.mjs";
import { generateProgram, renderProgram } from "../translation/gen/index.mjs";
import { coreOf, coreProgramOf, dumpOf } from "../translation/split_dump.mjs";
import { cases, inheritanceCases, inheritanceSplitCases, splitCases, bindingSplitCases } from "./cases.mjs";
import {
  generatedCases,
  generatedHeritageSplit,
  generatedInheritanceCases,
  generatedModuleCases,
  generatedOpaqueCase,
  generatedOpaqueClassCase,
  generatedBindingCases,
} from "./generate.mjs";
import { moduleCases } from "./module_cases.mjs";
import { opaqueCases, inheritanceOpaqueCases, scriptsOf } from "./opaque_cases.mjs";
import { bits, HOST_GLOBALS, Refused, toCore, toCoreProgram } from "./to_core.mjs";

const option = (name, fallback) => {
  const i = process.argv.lastIndexOf(`--${name}`);
  return i < 0 ? fallback : Number(process.argv[i + 1]);
};
const bindingOnly = process.argv.includes("--bindings-only");
const inheritanceOnly = process.argv.includes("--inheritance-only");
assert(!(bindingOnly && inheritanceOnly), "select one differential suite");
const broad = !bindingOnly && !inheritanceOnly;
const selectedCases = bindingOnly ? [] : inheritanceOnly ? inheritanceCases : cases;
const requestedCount = option("count", 40);
const shards = Number(process.env.TEST_TOTAL_SHARDS ?? 1);
const shard = Number(process.env.TEST_SHARD_INDEX ?? 0);
assert(Number.isInteger(shards) && shards > 0 && Number.isInteger(shard) && shard >= 0 && shard < shards);
assert(Number.isInteger(requestedCount) && requestedCount >= shards, "each shard needs at least one seed");
const seedOffset = Math.floor((requestedCount * shard) / shards);
const count = Math.floor((requestedCount * (shard + 1)) / shards) - seedOffset;
const firstSeed = option("seed", 1) + seedOffset;
if (process.env.TEST_SHARD_STATUS_FILE) writeFileSync(process.env.TEST_SHARD_STATUS_FILE, "");
const splitPrograms = option("splits", 60);
const show = process.argv.includes("--show");
const here = path.dirname(fileURLToPath(import.meta.url));
const built = (name) => path.join(here, "../../lean/.lake/build/bin", name);
const corejs = process.env.COREJS ?? built("corejs");
const lowersplit = process.env.LOWERSPLIT ?? built("lowersplit");
const lower = process.env.LOWER ?? built("lower");
const evalorder = process.env.EVALORDER ?? built("evalorder");
const work = mkdtempSync(path.join(process.env.TEST_TMPDIR ?? os.tmpdir(), "corejs_differential_"));

// Record the active case or batch if a subprocess or imported program wedges.
const progress = path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR ?? work, "progress.log");
const checkpoint = (label) => appendFileSync(progress, `${label}\n`);

// The sets of `gen_validity_test.mjs`: each family with the ones before it.
const SETS = !broad
  ? []
  : [
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

const failures = [];
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const sameRun = (a, b) => a.end === b.end && a.detail === b.detail && same(a.out, b.out);

// The world: the host functions the model asks about, wrapped while a program runs in Node.
checkpoint("reading host names");
const hostNames = spawnSync(corejs, ["--host-names"], { encoding: "utf8" });
assert.equal(hostNames.status, 0, `corejs --host-names failed: ${hostNames.stderr}`);
const host = JSON.parse(hostNames.stdout);
const isPrimitive = (v) => v === null || ["undefined", "boolean", "number", "string"].includes(typeof v);
/** A primitive as `Decode.primitive` of `Run.lean` reads it. */
const encode = (v) => {
  if (v === undefined) return ["u"];
  if (v === null) return ["n"];
  if (typeof v === "boolean") return ["b", v];
  if (typeof v === "number") return ["d", bits(v)];
  return /[\ud800-\udfff]/.test(v) ? ["x"] : ["s", v];
};
let recording = null; // the calls of the program that is running
let inHost = false; // inside a recorded call, or inside this harness
/**
 * Is the function that called a wrapped host function a function of the program? Node's own code runs while a
 * program is loaded and calls host functions too; a program is a file of the work directory.
 */
function calledByProgram() {
  const prepare = Error.prepareStackTrace;
  Error.prepareStackTrace = (_, sites) => sites;
  const holder = {};
  Error.captureStackTrace(holder, calledByProgram);
  const sites = holder.stack;
  Error.prepareStackTrace = prepare;
  // sites[0] is the wrapper, sites[1] its caller
  return (sites[1]?.getFileName() ?? "").includes(path.basename(work));
}
const wrapped = [...host.methods.map((name) => [name, true]), ...host.functions.map((name) => [name, false])].map(
  ([name, method]) => {
    const keys = name.split(".");
    const key = keys.pop();
    const owner = keys.reduce((o, k) => o[k], globalThis);
    const descriptor = Object.getOwnPropertyDescriptor(owner, key);
    assert.equal(typeof descriptor?.value, "function", `${name} is not a function of this Node`);
    const proxy = new Proxy(descriptor.value, {
      apply(target, receiver, args) {
        if (recording === null || inHost) return Reflect.apply(target, receiver, args);
        inHost = true;
        try {
          const self = method ? receiver : undefined;
          if (!isPrimitive(self) || !args.every(isPrimitive) || !calledByProgram())
            return Reflect.apply(target, receiver, args);
          const call = { fn: name, this: encode(self), args: args.map(encode) };
          recording.push(call);
          try {
            const result = Reflect.apply(target, receiver, args);
            if (isPrimitive(result)) call.ret = encode(result);
            else
              failures.push(`${name} returned an object for primitives: the model's list of host functions is wrong`);
            return result;
          } catch (e) {
            if (isPrimitive(e)) call.throw = encode(e);
            else call.error = e.constructor.name;
            throw e;
          }
        } finally {
          inHost = false;
        }
      },
    });
    return { owner, key, descriptor, proxy };
  }
);

/** What the module `file` does in Node: `{ out, end, detail, world }`, `world` being the host calls it made. */
async function runFileInNode(file) {
  const out = [];
  const log = console.log;
  console.log = (...args) => {
    const before = inHost;
    inHost = true;
    out.push(format(...args));
    inHost = before;
  };
  recording = [];
  for (const w of wrapped) Object.defineProperty(w.owner, w.key, { ...w.descriptor, value: w.proxy });
  let ending;
  try {
    await import(pathToFileURL(file));
    ending = { end: "normal", detail: "" };
  } catch (e) {
    ending = { end: "throw", detail: null, thrown: e };
  }
  for (const w of wrapped) Object.defineProperty(w.owner, w.key, w.descriptor);
  const world = recording;
  recording = null;
  console.log = log;
  if (ending.thrown !== undefined || ending.detail === null) ending.detail = describe(ending.thrown);
  return { out, end: ending.end, detail: ending.detail, world };
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

/** The host calls of a run in Node without their endings: what the program asked of the world, in order. */
const asked = (node) => node.world.map(({ fn, this: receiver, args }) => [fn, receiver, args]);

/** Sets `job.node` to what the program of `job` does in Node, for all `jobs`. */
async function runNode(jobs) {
  for (const job of jobs) {
    checkpoint(`Node: ${job.label}`);
    job.node = job.entry ? await runFileInNode(job.entry) : await runInNode(job.source);
  }
}

/** A translated module as a program of its own, statement `j` with the identity `j`. */
const single = (t) => ({
  entry: "main",
  modules: [{ name: "main", imports: [], exports: [], ids: t.ids.map((_, j) => j), tokens: t.tokens }],
});

let modelSeconds = 0;
/**
 * Sets `job.result` to what the model does with `job.program` in the world `job.node` recorded, for all `jobs` in one
 * call of `corejs`.
 */
function runModel(jobs) {
  if (jobs.length === 0) return;
  const input = path.join(work, "programs.jsonl");
  writeFileSync(input, jobs.map((j) => JSON.stringify({ ...j.program, world: j.node.world })).join("\n") + "\n");
  checkpoint(`CoreJS batch: ${jobs.map((j) => j.label).join("; ")}`);
  const started = Date.now();
  const model = spawnSync(corejs, [input], { encoding: "utf8", maxBuffer: 1 << 28 });
  modelSeconds += (Date.now() - started) / 1000;
  checkpoint(`CoreJS returned: ${model.status}`);
  assert.equal(model.status, 0, `corejs failed: ${model.stderr}`);
  const lines = model.stdout.trimEnd().split("\n");
  assert.equal(lines.length, jobs.length, "corejs did not answer every program");
  jobs.forEach((job, i) => {
    job.result = JSON.parse(lines[i]);
    const { world, left, end } = job.result;
    if (world !== "") failures.push(`${job.label}: ${world}\n${show ? job.source : ""}`);
    else if (left !== 0 && (end === "normal" || end === "throw"))
      failures.push(`${job.label}: the model ended ${left} host calls before Node's last one`);
  });
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

// { label, source, entry, program, node, result, expectEnd, set }: `program` is what `corejs` reads
const jobs = [];
for (const c of selectedCases) {
  const translated = accepted(c.source, { allowHost: c.allowHost ?? false });
  assert(translated !== null, `case ${c.name}: the front end refuses it`);
  jobs.push({
    label: `case ${c.name}`,
    source: c.source,
    program: single(translated),
    expectEnd: c.end ?? null,
    set: null,
  });
}
const bindingCases = bindingOnly
  ? Array.from({ length: count }, (_, i) => generatedBindingCases(firstSeed + i)).flat()
  : [];
const heritageCases = inheritanceOnly
  ? Array.from({ length: count }, (_, i) => generatedHeritageSplit(firstSeed + i))
  : [];
const namedSplits = bindingOnly
  ? [...bindingSplitCases, ...bindingCases]
  : inheritanceOnly
    ? [...inheritanceSplitCases, ...heritageCases]
    : splitCases;
for (const c of namedSplits) {
  const translated = accepted(c.source);
  assert(translated !== null, `split case ${c.name}: the front end refuses the bundle`);
  jobs.push({
    label: `split case ${c.name}`,
    source: c.source,
    program: single(translated),
    expectEnd: inheritanceOnly ? "normal" : null,
    set: null,
    splitCase: c,
  });
}
const focused = bindingOnly
  ? []
  : Array.from({ length: count }, (_, i) =>
      (inheritanceOnly ? generatedInheritanceCases : generatedCases)(firstSeed + i)
    ).flat();
for (const c of focused) {
  const translated = toCore(c.source);
  jobs.push({
    label: c.name,
    source: c.source,
    program: single(translated),
    expectEnd: "normal",
    set: null,
    focused: c,
  });
}
const linked = !broad ? [] : Array.from({ length: count }, (_, i) => generatedModuleCases(firstSeed + i)).flat();
for (const c of !broad ? [] : [...moduleCases, ...linked]) {
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
    linked: linked.includes(c),
  });
}
// Body fallback must not hide eager class work or unsupported invocation/home-object semantics.
if (!bindingOnly) {
  for (const source of [
    "class Box extends Base { constructor() { super(); } }",
    "class Box extends Base { field = 1; }",
    "class Box extends Base { static field = 1; }",
    "class Box { constructor() { this.target = new.target; } }",
    "class Box { [name]() { const [x] = []; } }",
    "class Box { static { sideEffect(); } method() { const [x] = []; } }",
    "class Box { method() { return super.value; } }",
    "class Box { async method() { await value; } }",
    "class Box { *method() { yield value; } }",
  ]) {
    const translated = coreOf(source);
    assert.equal(translated.opaqueBodies.length, 0, `unsupported class gained body-only fallback: ${source}`);
    assert.equal(translated.opaque.length, 1, `unsupported class must stay opaque: ${source}`);
  }
  for (const source of [
    'class Box { method() { eval("value"); } }',
    'class Box { method() { return import("./module.mjs"); } }',
    "class Box { method() { return import.meta.url; } }",
  ])
    assert.throws(() => coreOf(source), Refused, `module-identity restriction disappeared: ${source}`);
}

const allOpaqueCases = bindingOnly
  ? []
  : inheritanceOnly
    ? inheritanceOpaqueCases
    : [
        ...opaqueCases,
        ...Array.from({ length: count }, (_, i) => generatedOpaqueCase(firstSeed + i)),
        ...Array.from({ length: count }, (_, i) => generatedOpaqueClassCase(firstSeed + i)),
      ];
for (const c of allOpaqueCases) {
  const translated = coreOf(c.source);
  const bodies = translated.opaqueBodies ?? [];
  assert(translated.opaque.length > 0 || bodies.length > 0, `opaque case ${c.name}: no statement or body is opaque`);
  if (c.bodyParts) {
    assert.equal(translated.opaque.length, 0, `opaque class case ${c.name}: class creation became an opaque statement`);
    assert.deepEqual(
      bodies.map(({ id, part }) => `${id}/${part}`).sort(),
      [...c.bodyParts].sort(),
      `opaque class case ${c.name}: deferred bodies must retain their lexical part identities`
    );
  } else {
    assert(translated.opaque.length > 0, `opaque statement case ${c.name}: no statement is opaque`);
  }
  jobs.push({
    label: `opaque: ${c.name}`,
    source: c.source,
    program: { ...single(translated), scripts: scriptsOf(c) },
    expectEnd: c.end ?? "normal",
    set: null,
    opaqueCase: c,
  });
}
await runNode(jobs);
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
    job.program = single(translated);
    tried.push(job);
  }
  await runNode(tried);
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
  checkpoint(`module order: ${program.entry}`);
  const run = spawnSync(evalorder, [file], { encoding: "utf8" });
  checkpoint(`module order returned: ${run.status}`);
  assert.equal(run.status, 0, `evalorder failed: ${run.stderr}`);
  return run.stdout.trimEnd().split("\n");
}

const ends = {};
let orders = 0;
let hostCalls = 0;
for (const job of jobs) {
  const got = job.result;
  ends[got.end] = (ends[got.end] ?? 0) + 1;
  hostCalls += job.node.world.length - got.left;
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
function numbered(source, scripts) {
  const t = scripts ? coreOf(source) : toCore(source);
  if (scripts)
    return { program: { entry: "main", modules: [{ name: "main", imports: [], ...t }], scripts }, units: unitsOf(t) };
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
  opaque: [0, 0],
  opaqueCases: 0,
  unsupported: 0,
  agree: 0,
  orderKept: 0,
  sliceNamed: 0,
  movesNamed: 0,
  sameState: 0,
  otherCalls: 0,
};
const apart = { splits: 0, differ: 0 }; // accepted splits that move code, of a bundle that does not end normally
const wrongAccepts = [];
const splitJobs = [];

/** Split the bundle of `job` by `spec`. An accepted split becomes a job; `named` is its entry of `splitCases`. */
function split(job, bundle, dump, kind, spec, named) {
  checkpoint(`split: ${job.label}, ${kind}`);
  const dir = writeFiles({
    "ast.json": JSON.stringify(dump),
    "spec.json": JSON.stringify(spec),
    "program.json": JSON.stringify(bundle.program),
  });
  const out = path.join(dir, "out");
  const plan = path.join(dir, "plan.json");
  const run = spawnSync(
    lowersplit,
    [
      path.join(dir, "ast.json"),
      path.join(dir, "spec.json"),
      out,
      "--plan",
      plan,
      "--corejs",
      path.join(dir, "program.json"),
    ],
    { encoding: "utf8" }
  );
  checkpoint(`split returned: ${run.status}`);
  const label = `${job.label}, split ${kind} ${JSON.stringify(spec)}`;
  if (named?.refused) {
    assert.equal(run.status, 1, `${label}: lowersplit does not reject it\n${run.stdout}\n${run.stderr}`);
    return tally.named[1]++;
  }
  if (named) assert.equal(run.status, 0, `${label}: lowersplit does not accept it\n${run.stdout}\n${run.stderr}`);
  if (run.status === 3) return tally.unsupported++;
  if (run.status === 1) return tally[kind][1]++;
  assert.equal(run.status, 0, `${label}: lowersplit failed\n${run.stdout}\n${run.stderr}`);
  tally[kind][0]++;
  const { order, modules } = JSON.parse(readFileSync(plan, "utf8"));
  const entry = path.join(out, "entry.mjs");
  const statements = order.flatMap((m) => modules[m]);
  const orderKept = statements.every((id, k) => k === 0 || statements[k - 1] < id);
  // The verdict names the order-kept slice exactly when the split keeps the order: its other hypotheses hold here.
  const slice = run.stdout.match(/^ {2}proved slice: (.*)$/m)?.[1];
  if (orderKept !== Boolean(slice?.startsWith("order kept")))
    failures.push(
      `${label}: the plan ${orderKept ? "keeps" : "does not keep"} the order, and the verdict says ${slice}`
    );
  else if (orderKept) tally.sliceNamed++;
  else if (slice?.startsWith("moves")) tally.movesNamed++;
  if (named?.certified) {
    assert(!orderKept, `${label}: the binding certificate regression must reorder statements`);
    assert(slice?.startsWith("moves (certified bindings)"), `${label}: missing certified binding slice: ${slice}`);
  }
  if (named?.certifiedHeritage) {
    const { pivot, crossed } = named.certifiedHeritage;
    assert.deepEqual(
      statements.filter((id) => id === pivot || crossed.includes(id)),
      [...crossed, pivot],
      `${label}: local declarations must run before the subclass`
    );
    assert.match(
      slice ?? "",
      /^moves \(certified class heritage\).*: in the model, the split ends normally in a state exactly when the bundle does, for every world$/,
      `${label}: missing class heritage certification and normal equivalence`
    );
  }
  if (named?.uncertified) {
    assert(!orderKept, `${label}: the uncertified control must reorder statements`);
    assert.match(slice, /^none\./, `${label}: unsupported syntax must gain no permission`);
  }
  if (named?.uncertifiedHeritage) {
    assert(!orderKept, `${label}: the uncertified heritage control must reorder statements`);
    assert.match(slice ?? "", /^none\..*\(OrderKept\)/, `${label}: the heritage guard must reject the proposed moves`);
  }
  if (named?.poisonWrite !== undefined) {
    const analysis = spawnSync(lower, [path.join(dir, "ast.json")], { encoding: "utf8" });
    assert.equal(analysis.status, 0, `${label}: cannot obtain actual Effects summaries\n${analysis.stderr}`);
    const footprints = analysis.stdout
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line));
    const footprintPath = path.join(dir, "footprints.jsonl");
    const replay = () => {
      writeFileSync(footprintPath, footprints.map((fp) => JSON.stringify(fp)).join("\n") + "\n");
      return spawnSync(
        lowersplit,
        [
          path.join(dir, "ast.json"),
          path.join(dir, "spec.json"),
          path.join(dir, "replay"),
          "--corejs",
          path.join(dir, "program.json"),
          "--footprints",
          footprintPath,
        ],
        { encoding: "utf8" }
      );
    };
    const pristine = replay();
    assert.equal(pristine.status, 0, `${label}: actual summaries must reload\n${pristine.stderr}`);
    assert.match(
      pristine.stdout,
      /proved slice: moves \(certified bindings\)/,
      `${label}: actual serialized summaries must certify`
    );
    assert(footprints[named.poisonWrite].writes.length > 0, `${label}: mutation must remove a real write`);
    footprints[named.poisonWrite].writes = [];
    const poisoned = replay();
    assert.equal(poisoned.status, 0, `${label}: missing-write control must reach the semantic certificate check`);
    assert.match(poisoned.stdout, /proved slice: none\./, `${label}: missing write must not certify`);
  }
  if ((job.focused || job.opaqueCase?.move) && kind === "named") {
    assert(!orderKept, `${label}: the required move kept the order`);
    assert(slice?.startsWith("moves"), `${label}: the required move is outside the proved moves slice: ${slice}`);
  }
  try {
    const scripts = job.program.scripts;
    const program = scripts
      ? { ...coreProgramOf(entry, { ids: modules }), scripts }
      : toCoreProgram(entry, { ids: modules });
    splitJobs.push({
      label,
      source: job.source,
      entry,
      order,
      orderKept,
      moves: slice?.startsWith("moves"),
      of: job,
      bundle,
      program,
      named,
    });
  } catch (e) {
    failures.push(`${label}: the front end takes the bundle and not the split: ${e.message}`);
  }
}

for (const job of jobs.filter((j) => j.opaqueCase))
  if (!job.node || !sameRun(job.result, job.node))
    failures.push(
      `${job.label}: the model differs from Node: ${JSON.stringify(job.result)} ${JSON.stringify(job.node)}`
    );
  else tally.opaqueCases++;
const candidates = jobs.filter((j) => !j.entry && j.node && j.result.end !== "unknown" && sameRun(j.result, j.node));
for (const job of candidates.filter((j) => j.splitCase))
  split(job, numbered(job.source), dumpOf(job.source), "named", job.splitCase.spec, job.splitCase);
// Every focused program gets a nontrivial split, independently of the manual-first random-plan budget.
const swapPrefix = (dump) => ({
  entry: "entry.mjs",
  modules: { "m0.mjs": ["#1"], "entry.mjs": dump.statements.flatMap((_, i) => (i === 1 ? [] : [`#${i}`])) },
});
for (const job of candidates.filter((j) => j.focused || j.opaqueCase?.move)) {
  const dump = dumpOf(job.source);
  split(job, numbered(job.source, job.program.scripts), dump, "named", swapPrefix(dump), {});
}
// a program with opaque statements, in two consecutive pieces that run in order
for (const job of candidates.filter((j) => j.opaqueCase)) {
  const dump = dumpOf(job.source);
  const n = dump.statements.length;
  const cut = Math.floor(n / 2);
  const modules = { "m0.mjs": [], "entry.mjs": [] };
  dump.statements.forEach((_, i) => modules[i < cut ? "m0.mjs" : "entry.mjs"].push(`#${i}`));
  split(
    job,
    numbered(job.source, job.program.scripts),
    dump,
    "opaque",
    { entry: "entry.mjs", modules, sequential: true },
    null
  );
}
for (const [index, job] of candidates
  .filter((j) => !j.opaqueCase)
  .slice(0, splitPrograms)
  .entries()) {
  const dump = dumpOf(job.source);
  if (dump.statements.length < 2) continue;
  tally.programs++;
  const bundle = numbered(job.source);
  const next = random(firstSeed * 7919 + index);
  for (const [kind, spec] of Object.entries(specsFor(dump, next))) split(job, bundle, dump, kind, spec, null);
}
const bundleJobs = [...new Set(splitJobs.map((s) => s.of))];
for (const job of bundleJobs)
  job.numbered = { label: job.label, node: job.node, program: splitJobs.find((s) => s.of === job).bundle.program };
runModel(bundleJobs.map((j) => j.numbered));
await runNode(splitJobs);
runModel(splitJobs);
// `order_kept_preserves` of `Split.lean` is about one world: a split that keeps the order also runs in the world its
// bundle recorded, and must end there in the bundle's state.
for (const job of splitJobs) job.inBundleWorld = { label: job.label, node: job.of.node, program: job.program };
runModel(
  splitJobs
    .filter((job) => job.orderKept || (job.moves && job.of.node.end === "normal"))
    .map((job) => job.inBundleWorld)
);
for (const job of splitJobs) {
  const files = () =>
    job.program.modules.map((m) => `// ${m.name}\n${readFileSync(path.join(path.dirname(job.entry), m.name), "utf8")}`);
  const text = () => `--- bundle\n${job.source}\n--- split\n${files().join("\n")}`;
  const node = job.node;
  if (!same(asked(node), asked(job.of.node))) {
    tally.otherCalls++;
    if (show) console.log(`other host calls than the bundle: ${job.label}`);
  }
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
  if (job.orderKept) {
    tally.orderKept++;
    const there = job.inBundleWorld.result;
    const bundle = job.of.numbered.result;
    if (there.state === bundle.state && sameRun(there, bundle)) tally.sameState++;
    else failures.push(`${job.label}: the split keeps the order and ends in another state than its bundle`);
  }
  if (job.moves && job.of.node.end === "normal") {
    const there = job.inBundleWorld.result;
    const bundle = job.of.numbered.result;
    if (there.state !== bundle.state || !sameRun(there, bundle))
      failures.push(`${job.label}: the proved move ends in another state in the bundle's world`);
  }
  if (!same(job.result.order, job.order))
    failures.push(`${job.label}: the model runs the modules as ${job.result.order}, the plan as ${job.order}`);
  for (const m of job.program.modules)
    for (const [id, tokens] of unitsOf(m))
      if (!same(tokens, job.bundle.units.get(id)))
        failures.push(`${job.label}: statement ${id} of ${m.name} is not the bundle's statement ${id}`);
  tally.agree++;
}

// The members the model gives the host's prototypes are the ones this Node has, and the front end lets through the
// globals the model has a rule for.
if (!same([...host.globals].sort(), [...HOST_GLOBALS].sort()))
  failures.push(`the front end lets through ${HOST_GLOBALS}, the model has rules for ${host.globals}`);
for (const [owner, listed] of Object.entries(host.prototypes)) {
  const real = Object.getOwnPropertyNames(owner.split(".").reduce((o, k) => o[k], globalThis));
  if (!same([...listed].sort(), real.sort()))
    failures.push(`${owner}: the model lists ${listed.sort()}, Node has ${real}`);
}

rmSync(work, { recursive: true, force: true });
console.log(
  `mode: ${bindingOnly ? "binding certificates" : inheritanceOnly ? "default inheritance" : "CoreJS interpreter"}; cases: ${selectedCases.length}; module cases: ${broad ? moduleCases.length : 0} (${orders} orders compared with Plan.evalOrder); ` +
    `generated: ${count} programs for each set from seed ${firstSeed}`
);
console.table(summary);
console.log("unknown code that units of generated programs were left out for:", reasons);
console.log(`model endings: ${JSON.stringify(ends)}; corejs took ${modelSeconds.toFixed(1)} s`);
const verdicts = (kind) => `${kind}: ${tally[kind][0]} accepted, ${tally[kind][1]} rejected`;
console.log(
  `opaque cases: ${tally.opaqueCases} of ${allOpaqueCases.length} agree with Node; splits: ${tally.named[0]} named accepted, ${tally.named[1]} named rejected; ${tally.programs} programs; ${["cut", "ordered", "free", "opaque"].map(verdicts).join("; ")}; ` +
    `${tally.unsupported} unsupported; ${tally.sliceNamed} accepted splits whose verdict names the order-kept slice, ${tally.movesNamed} the moves slice; ${tally.agree} accepted splits where the four runs agree, ` +
    `${tally.orderKept} of them with the statements in the bundle's order, ${tally.sameState} of those ending in ` +
    `the bundle's state; ${apart.splits} accepted splits move code ` +
    `of a bundle that does not end normally and are not compared with it (premise P4), ${apart.differ} of them ` +
    `differ; ${tally.otherCalls} accepted splits make other host calls than their bundle, or in another order`
);
const total = (key) => summary.reduce((sum, s) => sum + s[key], 0);
const focusedCoverage = {};
for (const job of jobs.filter((j) => j.focused)) {
  const covered = (focusedCoverage[job.focused.family] ??= { generated: 0, completed: 0, reordered: 0 });
  covered.generated++;
  if (job.result.end === "normal" && sameRun(job.result, job.node)) covered.completed++;
  if (splitJobs.some((s) => s.of === job && !s.orderKept && sameRun(s.result, job.result) && sameRun(s.node, job.node)))
    covered.reordered++;
}
if (bindingOnly) {
  const certifiedGenerated = splitJobs.filter((j) => j.named?.generatedBinding);
  assert.equal(certifiedGenerated.length, 4 * count, "every generated binding case must be split");
  for (const family of ["literal", "binary", "unary", "branch"]) {
    assert.equal(
      certifiedGenerated.filter((j) => j.named.bindingFamily === family).length,
      count,
      `every seed must execute the ${family} binding-certificate family`
    );
  }
  assert(
    certifiedGenerated.every((j) => j.moves && sameRun(j.result, j.of.result) && sameRun(j.node, j.of.node)),
    "every generated binding case must execute equivalently through the certified moves slice"
  );
  console.log(`certified binding moves: ${certifiedGenerated.length}, four runs and final state compared`);
}
if (inheritanceOnly) {
  const certified = splitJobs.filter((j) => j.named?.certifiedHeritage);
  assert.equal(certified.length, count + 1, "the handwritten and every generated heritage split must execute");
  assert(
    certified.every(
      (j) => j.moves && j.result.end === "normal" && sameRun(j.result, j.of.result) && sameRun(j.node, j.of.node)
    ),
    "class heritage moves must preserve normal execution in the model and in Node"
  );
  console.log(`certified class heritage moves: ${certified.length}, four runs and final state compared`);
}
console.log("whole CoreJS generator (no filtering):");
console.table(focusedCoverage);
assert.deepEqual(
  Object.keys(focusedCoverage).sort(),
  bindingOnly
    ? []
    : inheritanceOnly
      ? ["inheritance"]
      : ["arrays", "classes", "closures", "control", "exceptions", "expressions", "host", "objects"],
  "the whole-program generator must execute each promised family"
);
assert(
  Object.values(focusedCoverage).every((c) => c.generated === count && c.completed === count && c.reordered === count),
  "every focused family must complete and execute in a matching accepted reordered split for every seed"
);
assert(
  jobs.filter((j) => j.linked).every((j) => sameRun(j.result, j.node)),
  "every generated linked program must match Node"
);
assert.equal(
  jobs.filter((j) => j.linked).length,
  broad ? count * 3 : 0,
  "each seed must run the diamond, cycle and abrupt module graphs"
);
console.log(`generated linked programs: ${linked.length}, compared with Node and Plan.evalOrder`);
const opaqueMoved = splitJobs.filter((j) => j.of.opaqueCase?.move && !j.orderKept);
assert.equal(
  opaqueMoved.length,
  allOpaqueCases.filter((c) => c.move).length,
  "every opaque closure move must be accepted and executed"
);
const generatedClassJobs = jobs.filter((j) => j.opaqueCase?.generatedClass);
assert.equal(generatedClassJobs.length, broad ? count : 0, "every seed must generate an opaque class case");
assert.equal(
  opaqueMoved.filter((j) => j.of.opaqueCase?.generatedClass).length,
  generatedClassJobs.length,
  "every generated opaque class must execute in an accepted reordered split"
);
console.log(`opaque closure and class moves: ${opaqueMoved.length}, four runs and final state compared`);
console.log(
  `generated units: ${total("units")}; accepted alone: ${total("unitsAccepted")}; in the programs that ran: ` +
    `${total("unitsRun")} (${((100 * total("unitsRun")) / total("units")).toFixed(1)}%); ` +
    `host calls recorded: ${hostCalls}`
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
