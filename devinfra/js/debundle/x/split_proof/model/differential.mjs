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
//   - the consecutive opaque splits require an all-outcomes certificate on the emitted files. Node compares the
//     original and emitted programs, and --check-output checks the published files again.
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
// The executable paths default to Lake's build.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { format } from "node:util";
import { kindOf } from "../translation/testing/gen/core.mjs";
import { FAMILY_SAMPLES, generateProgram, renderProgram } from "../translation/testing/gen/index.mjs";
import { coreOf, coreProgramOf, splitInputs } from "../translation/split_dump.mjs";
import { cases, inheritanceCases, inheritanceSplitCases, splitCases, bindingSplitCases } from "./cases.mjs";
import {
  generatedCases,
  generatedHeritageSplit,
  generatedInheritanceCases,
  generatedModuleCases,
  generatedOpaqueCase,
  generatedObjectRequestCase,
  generatedOpaqueClassCase,
  generatedBindingCases,
} from "./generate.mjs";
import { moduleCases } from "./module_cases.mjs";
import { opaqueCases, inheritanceOpaqueCases, scriptsOf } from "./opaque_cases.mjs";
import { BINARY, LOGICAL, UNARY, bits, HOST_GLOBALS, Refused, toCore, toCoreProgram } from "./to_core.mjs";
import { writeDeployment } from "../translation/testing/deployment_manifest.mjs";
import { mulberry32 } from "../seeded_random.mjs";
import { acorn, dump } from "../translation/parser.mjs";

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
const built = (name) => path.join(here, "../lean/.lake/build/bin", name);
const corejs = process.env.COREJS ?? built("corejs");
const lowersplit = process.env.LOWERSPLIT ?? built("lowersplit");
const splitPreview = process.env.SPLIT_PREVIEW ?? built("split_preview");
const lower = process.env.LOWER ?? built("lower");
const evalorder = process.env.EVALORDER ?? built("evalorder");
const work = mkdtempSync(path.join(process.env.TEST_TMPDIR ?? os.tmpdir(), "corejs_differential_"));

// Record the active case or batch if a subprocess or imported program wedges.
const progress = path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR ?? work, "progress.log");
const checkpoint = (label) => appendFileSync(progress, `${label}\n`);

const familySamples = broad ? FAMILY_SAMPLES : [];
// How often a generated program is tried again without the unit in which the model reached unknown code.
const ROUNDS = 8;

/** The annotated module that `corejs --ast` lowers with the linked certification rules. */
const astOf = (source) => dump(source, { includeOpaqueCaptures: true });
/** A module graph for `corejs --ast`: the entry `main.mjs` and its providers by filename. */
const astModules = (files) => ({
  ...astOf(files["main.mjs"]),
  entry: "main.mjs",
  providers: Object.fromEntries(
    Object.entries(files)
      .filter(([name]) => name !== "main.mjs")
      .map(([name, text]) => [name, astOf(text)])
  ),
});
const parseModule = (source) => acorn.parse(source, { sourceType: "module", ecmaVersion: "latest" });
/** The import graph of `files`, for `Plan.evalOrder`. */
const moduleGraph = (files) => ({
  entry: "main.mjs",
  modules: Object.fromEntries(
    Object.entries(files).map(([name, text]) => [
      name,
      parseModule(text)
        .body.filter((node) => node.type === "ImportDeclaration")
        .map((node) => path.posix.normalize(path.posix.join(path.posix.dirname(name), node.source.value))),
    ])
  ),
});
/** Source units as `ESTreeUnits.nodes` numbers them: one per declarator of a multi-declarator declaration. */
const unitCount = (source) =>
  parseModule(source).body.reduce((n, statement) => {
    const declaration = statement.type === "ExportNamedDeclaration" ? statement.declaration : statement;
    const declarators = declaration?.type === "VariableDeclaration" ? declaration.declarations.length : 1;
    return n + Math.max(declarators, 1);
  }, 0);
/**
 * Programs the linked rules refuse, by job label, with the reason. A refusal not listed here fails, and so does a
 * listed program that lowers.
 */
const linkedRefusals = new Map([
  [
    "modules: var destructuring exports each binding and retains the original receiver",
    /repeated local or imported binding/,
  ],
  ["modules: an import of a name the module does not export", /does not export absent/],
  ["modules: an import cannot be assigned", /does not export late/],
]);
const refusedByLowering = [];

/** A thrown value, as `describe` of `Run.lean` writes it. */
function describe(v) {
  if (v instanceof Error) return v.constructor.name;
  if (v === null || v === undefined) return String(v);
  if (typeof v === "object" || typeof v === "function") return typeof v;
  if (typeof v === "symbol") {
    if (v === iteratorSymbol) return "symbol:Symbol(Symbol.iterator)";
    if (v === hasInstanceSymbol) return "symbol:Symbol(Symbol.hasInstance)";
    if (v === toStringTagSymbol) return "symbol:Symbol(Symbol.toStringTag)";
    return "symbol";
  }
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
const isWellFormed = String.prototype.isWellFormed;
const symbolConstructor = Symbol;
const iteratorSymbol = Symbol.iterator;
const hasInstanceSymbol = Symbol.hasInstance;
const toStringTagSymbol = Symbol.toStringTag;

function symbolObservations() {
  const binding = Object.getOwnPropertyDescriptor(globalThis, "Symbol");
  const stableSymbol = (key, expected) => {
    const member = Object.getOwnPropertyDescriptor(symbolConstructor, key);
    return stableConstructor && member?.value === expected && !member.writable && !member.configurable;
  };
  const stableConstructor = binding?.value === symbolConstructor;
  return {
    symbolConstructor: stableConstructor,
    iteratorSymbol: stableSymbol("iterator", iteratorSymbol),
    hasInstanceSymbol: stableSymbol("hasInstance", hasInstanceSymbol),
    toStringTagSymbol: stableSymbol("toStringTag", toStringTagSymbol),
  };
}
/** A primitive as `Decode.recordedPrimitive` of `Run.lean` reads it. */
const encode = (v) => {
  if (v === undefined) return ["u"];
  if (v === null) return ["n"];
  if (typeof v === "boolean") return ["b", v];
  if (typeof v === "number") return ["d", bits(v)];
  return Reflect.apply(isWellFormed, v, []) ? ["s", v] : ["x"];
};
let recording = null; // the directory and host calls of the program that is running
let inHost = false; // inside a recorded call, or inside this harness
/**
 * Loading a program can also run Node's own code or callbacks queued by an earlier program.
 * Attribute observations only to callers in the active program's directory.
 */
function calledByProgram(directory) {
  const prepare = Error.prepareStackTrace;
  Error.prepareStackTrace = (_, sites) => sites;
  const holder = {};
  Error.captureStackTrace(holder, calledByProgram);
  const sites = holder.stack;
  Error.prepareStackTrace = prepare;
  // sites[0] is the wrapper, sites[1] its caller
  const filename = sites[1]?.getFileName() ?? "";
  const caller = filename.startsWith("file:") ? fileURLToPath(filename) : filename;
  return caller.startsWith(directory + path.sep);
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
          if (!isPrimitive(self) || !args.every(isPrimitive) || !calledByProgram(recording.directory))
            return Reflect.apply(target, receiver, args);
          const call = { fn: name, this: encode(self), args: args.map(encode) };
          recording.calls.push(call);
          try {
            const result = Reflect.apply(target, receiver, args);
            if (isPrimitive(result)) call.ret = encode(result);
            else if (typeof result === "symbol") call.unknown = "the host returns an unsupported symbol";
            else
              failures.push(`${name} returned an object for primitives: the model's list of host functions is wrong`);
            return result;
          } catch (e) {
            if (isPrimitive(e)) call.throw = encode(e);
            else if (typeof e === "symbol") call.unknown = "the host throws an unsupported symbol";
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

/** Run a Node module and record its host calls. */
async function runFileInNode(file) {
  const directory = path.dirname(file);
  const activeRecording = { directory, calls: [] };
  const out = [];
  const log = console.log;
  console.log = (...args) => {
    const before = inHost;
    inHost = true;
    try {
      if (recording === activeRecording && calledByProgram(directory)) out.push(format(...args));
    } finally {
      inHost = before;
    }
  };
  const initialSymbols = symbolObservations();
  recording = activeRecording;
  for (const w of wrapped) Object.defineProperty(w.owner, w.key, { ...w.descriptor, value: w.proxy });
  let ending;
  try {
    await import(pathToFileURL(file));
    ending = { end: "normal", detail: "" };
  } catch (e) {
    ending = { end: "throw", detail: null, thrown: e };
  }
  for (const w of wrapped) Object.defineProperty(w.owner, w.key, w.descriptor);
  recording = null;
  console.log = log;
  if (ending.thrown !== undefined || ending.detail === null) ending.detail = describe(ending.thrown);
  const finalSymbols = symbolObservations();
  return {
    out,
    end: ending.end,
    detail: ending.detail,
    world: activeRecording.calls,
    iteratorSymbol: initialSymbols.iteratorSymbol && finalSymbols.iteratorSymbol,
    hasInstanceSymbol: initialSymbols.hasInstanceSymbol && finalSymbols.hasInstanceSymbol,
    toStringTagSymbol: initialSymbols.toStringTagSymbol && finalSymbols.toStringTagSymbol,
    symbolConstructor: initialSymbols.symbolConstructor && finalSymbols.symbolConstructor,
  };
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

let modelSeconds = 0;
/** What `corejs` (with `args`) answers for each of `lines`, one call. */
function runCorejs(args, lines, label) {
  if (lines.length === 0) return [];
  const input = path.join(work, "programs.jsonl");
  writeFileSync(input, lines.map((line) => JSON.stringify(line)).join("\n") + "\n");
  checkpoint(`CoreJS batch: ${label}`);
  const started = Date.now();
  const model = spawnSync(corejs, [...args, input], { encoding: "utf8", maxBuffer: 1 << 28 });
  modelSeconds += (Date.now() - started) / 1000;
  checkpoint(`CoreJS returned: ${model.status}`);
  assert.equal(model.status, 0, `corejs failed: ${model.stderr}`);
  const answers = model.stdout.trimEnd().split("\n");
  assert.equal(answers.length, lines.length, "corejs did not answer every program");
  return answers.map((answer) => JSON.parse(answer));
}
/**
 * Sets `job.result` to what the model does in the world `job.node` recorded, for all `jobs`. A job with `ast` lowers
 * through the linked rules (`corejs --ast`); one with `program` is already CoreJS.
 */
function runModel(jobs) {
  const recorded = (input, { world, iteratorSymbol, hasInstanceSymbol, toStringTagSymbol, symbolConstructor }) => ({
    ...input,
    world,
    iteratorSymbol,
    hasInstanceSymbol,
    toStringTagSymbol,
    symbolConstructor,
  });
  const lowered = jobs.filter((job) => job.ast);
  const decoded = jobs.filter((job) => !job.ast);
  const label = (batch) => batch.map((j) => j.label).join("; ");
  const results = new Map();
  runCorejs(
    ["--ast"],
    lowered.map((job) => recorded(job.ast, job.node)),
    label(lowered)
  ).forEach((r, i) => results.set(lowered[i], r));
  runCorejs(
    [],
    decoded.map((job) => recorded(job.program, job.node)),
    label(decoded)
  ).forEach((r, i) => results.set(decoded[i], r));
  jobs.forEach((job) => {
    job.result = results.get(job);
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
function slice(program, banned, acceptance) {
  const primitive = (type) => ["num", "str", "bool"].includes(kindOf(type));
  const declared = new Set();
  const kept = [];
  const owner = [];
  const observers = [];
  let acceptedUnits = 0;
  for (const unit of program.units) {
    const alone = acceptance.get(unit);
    if (!alone.accepted) continue;
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

/** A generated unit alone, as `slice` tries it. */
const unitSource = (program, unit) => renderProgram({ ...program, units: [unit], exports: [], imports: [] });

/** Whether the linked rules lower each unit of the generated jobs alone, and each whole program, and its unit count. */
function acceptUnits(generatedJobs) {
  const entries = generatedJobs.flatMap(({ generated }) => [
    { key: generated, source: renderProgram(generated) },
    ...generated.units.map((unit) => ({ key: unit, source: unitSource(generated, unit) })),
  ]);
  const answers = runCorejs(
    ["--ast"],
    entries.map(({ source }) => astOf(source)),
    "generated units alone"
  );
  return new Map(
    entries.map(({ key, source }, i) => [key, { accepted: answers[i].end !== "malformed", units: unitCount(source) }])
  );
}

// { label, source, entry, ast or program, node, result, expectEnd, set }: `ast` or `program` is what `corejs` reads
const jobs = [];
if (broad) {
  // Release the previous program's callback while the next program is being recorded.
  const releaseKey = "corejsDifferentialRelease";
  const release = `globalThis[${JSON.stringify(releaseKey)}]`;
  try {
    const previous = await runInNode(`
      const savedLog = console.log;
      new Promise((resolve) => { ${release} = resolve; }).then(() => {
        console.log("previous", Math.abs(-7));
        savedLog("previous via saved console");
        ${release} = true;
      });
    `);
    const dir = writeFiles({
      "main.mjs": `import { observe } from "./worker.mjs"; ${release}(); await 0; observe();`,
      "worker.mjs": `export function observe() { console.log("active", Math.abs(-11)); }`,
    });
    const active = await runFileInNode(path.join(dir, "main.mjs"));
    assert.equal(globalThis[releaseKey], true, "the previous program's callback must run");
    assert.deepEqual(previous.out, []);
    assert.equal(active.end, "normal", active.detail);
    assert.deepEqual(active.out, ["active 11"]);
    assert.deepEqual(asked(active), [["Math.abs", ["u"], [["d", bits(-11)]]]]);
  } finally {
    delete globalThis[releaseKey];
  }

  for (const source of [String.raw`"\ud800";`, String.raw`({ "\udfff": 1 });`, "`\udfff`;"]) {
    assert.throws(
      () => toCore(source),
      (e) => e instanceof Refused && e.message.includes("isolated UTF-16 surrogate"),
      JSON.stringify(source)
    );
  }
}
for (const c of selectedCases) {
  jobs.push({
    label: `case ${c.name}`,
    source: c.source,
    ast: astOf(c.source),
    expectEnd: c.end ?? null,
    expectedStop: c.expectedStop,
    set: null,
  });
}
const bindingCases = bindingOnly
  ? Array.from({ length: count }, (_, i) => generatedBindingCases(firstSeed + i)).flat()
  : [];
const heritageCases = inheritanceOnly
  ? Array.from({ length: count }, (_, i) => generatedHeritageSplit(firstSeed + i)).flat()
  : [];
const namedSplits = bindingOnly
  ? [...bindingSplitCases, ...bindingCases]
  : inheritanceOnly
    ? [...inheritanceSplitCases, ...heritageCases]
    : splitCases;
for (const c of namedSplits) {
  jobs.push({
    label: `split case ${c.name}`,
    source: c.source,
    ast: astOf(c.source),
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
  jobs.push({
    label: c.name,
    source: c.source,
    ast: astOf(c.source),
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
    ast: astModules(c.files),
    graph: moduleGraph(c.files),
    expectEnd: c.end ?? null,
    set: null,
    linked: linked.includes(c),
    expectedOutput: c.expectedOutput,
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
        ...Array.from({ length: count }, (_, i) => generatedObjectRequestCase(firstSeed + i)),
        ...Array.from({ length: count }, (_, i) => generatedOpaqueCase(firstSeed + i)),
        ...Array.from({ length: count }, (_, i) => generatedOpaqueClassCase(firstSeed + i)),
      ];
for (const c of allOpaqueCases) {
  jobs.push({
    label: `opaque: ${c.name}`,
    source: c.source,
    ast: { ...astOf(c.source), scripts: scriptsOf(c, { includeUncalledBodies: true }) },
    expectEnd: c.end ?? "normal",
    set: null,
    opaqueCase: c,
  });
}
await runNode(jobs);
runModel(jobs);

const summary = familySamples.map((families) => ({
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
familySamples.forEach((families, set) => {
  for (let seed = firstSeed; seed < firstSeed + count; seed++) {
    const program = generateProgram(seed, { families });
    summary[set].programs++;
    pending.push({
      label: `seed ${seed} of ${families.join("+")}`,
      generated: program,
      banned: new Set(),
      expectEnd: null,
      set,
    });
  }
});
const acceptance = acceptUnits(pending);
for (const job of pending) if (acceptance.get(job.generated).accepted) summary[job.set].wholeAccepted++;
for (let round = 0; pending.length > 0; round++) {
  const tried = [];
  for (const job of pending) {
    job.cut = slice(job.generated, job.banned, acceptance);
    job.source = job.cut.source;
    job.ast = astOf(job.source);
    tried.push(job);
  }
  await runNode(tried);
  runModel(tried);
  pending = [];
  for (const job of tried) {
    // A unit the linked rules refuse together with the kept units is left out like one that reaches unknown code.
    const refused = job.result.end === "malformed" ? /^statement (\d+): (.*)$/.exec(job.result.detail) : null;
    const unit = refused ? job.cut.owner[Number(refused[1])] : job.cut.owner[job.result.region - 1];
    const reason = refused ? `lowering: ${refused[2]}` : job.result.detail;
    if (
      ((job.result.end === "unknown" && !job.result.detail.startsWith("model:")) || refused) &&
      unit !== undefined &&
      round < ROUNDS
    ) {
      reasons[reason] = (reasons[reason] ?? 0) + 1;
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

/** The order `Plan.evalOrder` gives an import graph. */
function planOrder(graph) {
  const file = path.join(work, "graph.json");
  writeFileSync(file, JSON.stringify(graph));
  checkpoint(`module order: ${graph.entry}`);
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
  const refusal = linkedRefusals.get(job.label);
  if (got.end === "malformed") {
    if (refusal?.test(got.detail)) refusedByLowering.push(job.label);
    else fail(`the linked rules refuse it: ${got.detail}`, null);
    continue;
  }
  if (refusal) failures.push(`${job.label}: the linked rules lower it now; remove it from linkedRefusals`);
  if (job.expectEnd !== null && got.end !== job.expectEnd) {
    fail(`the model should end ${job.expectEnd}`, null);
    continue;
  }
  if (job.expectedStop) {
    const { detail, left, out } = job.expectedStop;
    if (
      got.detail !== detail ||
      got.left !== left ||
      !same(got.out, out) ||
      !same(job.node.out.slice(0, out.length), out)
    )
      fail("replay stopped at the wrong boundary", job.node);
  }
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
    const plan = planOrder(job.graph);
    if (!same(plan, got.order)) fail(`the modules run in another order than Plan.evalOrder gives: ${plan}`, node);
  }
}

// Splits by the real tool.

/**
 * Three specs for a bundle's units: `cut` (consecutive pieces, run in order), `ordered` (a random module
 * for each statement, the modules run in the order of their first statements) and `free` (the same, with the order
 * left to the imports). An `export` statement stays in the entry, as `lowersplit` requires.
 */
function specsFor(units, next) {
  const n = units.length;
  const pinned = units.map(({ node }) => /^(Import|Export)/.test(node.type));
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
function splitInputFor(job) {
  if (job.splitInput) return job.splitInput;
  const scripts = job.opaqueCase && scriptsOf(job.opaqueCase);
  const { dump, core, units } = splitInputs(job.source, { opaque: Boolean(scripts) });
  const program = { entry: "main", modules: [{ name: "main", imports: [], ...core }] };
  if (scripts) {
    const { name, bodyParts } = job.opaqueCase;
    const bodies = core.opaqueBodies.map(({ id, part }) => `${id}/${part}`);
    if (bodyParts) {
      assert.equal(core.opaque.length, 0, `opaque class case ${name}: class creation became an opaque statement`);
      assert.deepEqual(bodies.sort(), [...bodyParts].sort(), `opaque class case ${name}: callable identities changed`);
    } else {
      assert(core.opaque.length > 0, `opaque statement case ${name}: no statement is opaque`);
    }
    program.scripts = scripts;
  }
  job.splitInput = { dump, units, bundle: { program, units: unitsOf(core) } };
  return job.splitInput;
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
  const heritageReport = path.join(dir, "heritage-report.json");
  const hasHeritageExpectation = named?.certifiedHeritage || named?.uncertifiedHeritage;
  const run = spawnSync(
    splitPreview,
    [
      writeDeployment(path.join(dir, "ast.json"), path.join(dir, "spec.json"), out),
      out,
      "--plan",
      plan,
      "--corejs",
      path.join(dir, "program.json"),
      ...(hasHeritageExpectation ? ["--move-report", heritageReport] : []),
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
  const slice = run.stdout.match(/^ {2}model diagnostic: (.*)$/m)?.[1];
  if (orderKept !== Boolean(slice?.startsWith("order kept")))
    failures.push(
      `${label}: the plan ${orderKept ? "keeps" : "does not keep"} the order, and the verdict says ${slice}`
    );
  else if (orderKept) tally.sliceNamed++;
  else if (slice?.startsWith("moves")) tally.movesNamed++;
  if (named?.localNegation) {
    assert.deepEqual(statements, [0, 2, 1, 3], label);
    assert.match(slice ?? "", /^moves \(`CoreJS\.moves_preserve`\)/, label);
  }
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
  if (hasHeritageExpectation) {
    const report = JSON.parse(readFileSync(heritageReport, "utf8"));
    const certified = Boolean(named.certifiedHeritage);
    assert.equal(report.footprintsOfSyntax, true, `${label}: must reach the heritage guard with validated footprints`);
    assert(report.pairwiseFailedPairs > 0, `${label}: the existing pairwise rules must leave a move unproved`);
    assert.equal(report.certifiedClassHeritage, certified, `${label}: unexpected heritage guard result`);
    assert.equal(report.failedPairs, certified ? 0 : report.pairwiseFailedPairs, `${label}: inconsistent move report`);
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
        splitPreview,
        [
          writeDeployment(path.join(dir, "ast.json"), path.join(dir, "spec.json"), path.join(dir, "replay")),
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
      /model diagnostic: moves \(certified bindings\)/,
      `${label}: actual serialized summaries must certify`
    );
    assert(footprints[named.poisonWrite].writes.length > 0, `${label}: mutation must remove a real write`);
    footprints[named.poisonWrite].writes = [];
    const poisoned = replay();
    assert.equal(poisoned.status, 0, `${label}: missing-write control must reach the semantic certificate check`);
    assert.match(poisoned.stdout, /model diagnostic: none\./, `${label}: missing write must not certify`);
  }
  if ((job.focused || job.opaqueCase?.move) && kind === "named") {
    assert(!orderKept, `${label}: the required move kept the order`);
    assert(slice?.startsWith("moves"), `${label}: the required move is outside the proved moves slice: ${slice}`);
  }
  try {
    const scripts = bundle.program.scripts;
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

// The certified path takes the original JavaScript and numbers its own units.
async function certifyOpaqueSplit(job) {
  const label = `${job.label}, consecutive split`;
  const expectedRefusal = job.opaqueCase.consecutiveSplitRefusal;
  const entry = "entry.mjs";
  const n = unitCount(job.source);
  assert(n >= 2, label);
  const cut = Math.floor(n / 2);
  const units = Array.from({ length: n }, (_, i) => `#${i}`);
  const spec = {
    entry,
    modules: { "m0.mjs": units.slice(0, cut), [entry]: units.slice(cut) },
    sequential: true,
    privateEntryBody: true,
  };
  const dir = writeFiles({ [entry]: job.source, "spec.json": JSON.stringify(spec) });
  const out = path.join(dir, "out");
  const manifest = writeDeployment(path.join(dir, entry), path.join(dir, "spec.json"), out);
  const level = "all-outcomes";
  const run = (...extra) => {
    checkpoint(`${label}: ${extra.length ? "check-output" : "emit"}`);
    const result = spawnSync(lowersplit, [manifest, out, "--structural", "--require", level, ...extra], {
      encoding: "utf8",
    });
    assert.ifError(result.error);
    assert([0, 1].includes(result.status), `${label}: ${result.stdout}${result.stderr}`);
    const verdict = JSON.parse(result.stdout);
    assert.equal(verdict.requested, level, label);
    assert.equal(verdict.status, result.status === 0 ? "certified" : "rejected", label);
    return verdict;
  };

  const emitted = run();
  if (emitted.status === "rejected") {
    assert.equal(emitted.guarantee, null, label);
    assert(!existsSync(out), `${label}: a refused split published output`);
    assert.equal(typeof emitted.reason, "string", label);
    assert(emitted.reason.length > 0, label);
    tally.opaque[1]++;
    if (emitted.reason !== expectedRefusal) {
      failures.push(
        expectedRefusal === undefined
          ? `${label}: unexpected refusal: ${emitted.reason}`
          : `${label}: refused with "${emitted.reason}", expected "${expectedRefusal}"`
      );
    }
    return;
  }
  if (expectedRefusal !== undefined)
    failures.push(`${label}: certified a split with expected refusal "${expectedRefusal}"`);
  assert.equal(emitted.guarantee, level, label);
  assert.deepEqual(run("--check-output"), emitted, label);
  const splitNode = await runFileInNode(path.join(out, entry));
  if (!sameRun(job.node, splitNode) || !same(asked(job.node), asked(splitNode))) {
    const observations = ({ out, end, detail, world }) => ({ out, end, detail, world });
    wrongAccepts.push(
      `${label}
  original: ${JSON.stringify(observations(job.node))}
  emitted: ${JSON.stringify(observations(splitNode))}`
    );
  }
  tally.opaque[0]++;
}

for (const job of jobs.filter((j) => j.opaqueCase))
  if (!job.node || !sameRun(job.result, job.node))
    failures.push(
      `${job.label}: the model differs from Node: ${JSON.stringify(job.result)} ${JSON.stringify(job.node)}`
    );
  else tally.opaqueCases++;
const candidates = jobs.filter((j) => !j.entry && j.node && j.result.end !== "unknown" && sameRun(j.result, j.node));
for (const job of candidates.filter((j) => j.splitCase)) {
  const { bundle, dump } = splitInputFor(job);
  split(job, bundle, dump, "named", job.splitCase.spec, job.splitCase);
}
// Every focused program gets a nontrivial split, independently of the manual-first random-plan budget.
const swapPrefix = (units) => ({
  entry: "entry.mjs",
  modules: { "m0.mjs": ["#1"], "entry.mjs": units.flatMap((_, i) => (i === 1 ? [] : [`#${i}`])) },
});
for (const job of candidates.filter((j) => j.focused || j.opaqueCase?.move)) {
  const { dump, bundle, units } = splitInputFor(job);
  split(job, bundle, dump, "named", swapPrefix(units), {});
}
for (const job of jobs.filter((j) => j.opaqueCase)) await certifyOpaqueSplit(job);
for (const [index, job] of candidates
  .filter((j) => !j.opaqueCase)
  .slice(0, splitPrograms)
  .entries()) {
  const { dump, bundle, units } = splitInputFor(job);
  if (units.length < 2) continue;
  tally.programs++;
  const next = mulberry32(firstSeed * 7919 + index);
  for (const [kind, spec] of Object.entries(specsFor(units, next))) split(job, bundle, dump, kind, spec, null);
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
const comparesNormalState = (job) => (job.moves || job.named?.heritageMutation) && job.of.node.end === "normal";
runModel(splitJobs.filter((job) => job.orderKept || comparesNormalState(job)).map((job) => job.inBundleWorld));
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
  if (comparesNormalState(job)) {
    const there = job.inBundleWorld.result;
    const bundle = job.of.numbered.result;
    if (there.state !== bundle.state || !sameRun(there, bundle))
      failures.push(`${job.label}: the move ends in another state in the bundle's world`);
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
const generator = function* () {};
const asyncGenerator = async function* () {};
const intrinsicPrototypes = {
  "AsyncGeneratorFunction.prototype": Object.getPrototypeOf(asyncGenerator),
  "AsyncGenerator.prototype": Object.getPrototypeOf(asyncGenerator.prototype),
  "ArrayIterator.prototype": Object.getPrototypeOf([][Symbol.iterator]()),
  "AsyncIterator.prototype": Object.getPrototypeOf(Object.getPrototypeOf(asyncGenerator.prototype)),
  "AsyncFunction.prototype": Object.getPrototypeOf(async () => {}),
  "GeneratorFunction.prototype": Object.getPrototypeOf(generator),
  "Generator.prototype": Object.getPrototypeOf(generator.prototype),
};
for (const [owner, listed] of Object.entries(host.prototypes)) {
  const prototype = intrinsicPrototypes[owner] ?? owner.split(".").reduce((o, k) => o[k], globalThis);
  const real = Object.getOwnPropertyNames(prototype);
  if (!same([...listed].sort(), real.sort()))
    failures.push(`${owner}: the model lists ${listed.sort()}, Node has ${real}`);
}

rmSync(work, { recursive: true, force: true });
console.log(
  `mode: ${bindingOnly ? "binding certificates" : inheritanceOnly ? "default inheritance" : "CoreJS interpreter"}; cases: ${selectedCases.length}; module cases: ${broad ? moduleCases.length : 0} (${orders} orders compared with Plan.evalOrder); ` +
    `generated: ${count} programs for each set from seed ${firstSeed}`
);
console.table(summary);
console.log(`refused by the linked rules, as expected: ${refusedByLowering.length}`);
console.log("unknown code that units of generated programs were left out for:", reasons);
console.log(`model endings: ${JSON.stringify(ends)}; corejs took ${modelSeconds.toFixed(1)} s`);
const verdicts = (kind) => `${kind}: ${tally[kind][0]} accepted, ${tally[kind][1]} rejected`;
console.log(
  `opaque cases: ${tally.opaqueCases} of ${allOpaqueCases.length} agree with Node; splits: ${tally.named[0]} named accepted, ${tally.named[1]} named rejected; ${tally.programs} programs; ${["cut", "ordered", "free"].map(verdicts).join("; ")}; ` +
    `${tally.unsupported} unsupported; ${tally.sliceNamed} accepted splits whose verdict names the order-kept slice, ${tally.movesNamed} the moves slice; ${tally.agree} accepted splits where the four runs agree, ` +
    `${tally.orderKept} of them with the statements in the bundle's order, ${tally.sameState} of those ending in ` +
    `the bundle's state; ${apart.splits} accepted splits move code ` +
    `of a bundle that does not end normally and are not compared with it (premise P4), ${apart.differ} of them ` +
    `differ; ${tally.otherCalls} accepted splits make other host calls than their bundle, or in another order`
);
console.log(
  `consecutive opaque certificates: ${tally.opaque[0]} accepted and checked on disk, ${tally.opaque[1]} refused`
);
const total = (key) => summary.reduce((sum, s) => sum + s[key], 0);
const focusedCoverage = {};
for (const job of jobs.filter((j) => j.focused)) {
  const covered = (focusedCoverage[job.focused.family] ??= { generated: 0, completed: 0, reordered: 0 });
  covered.generated++;
  if (job.focused.family === "objects")
    assert(
      job.result.out.some((line) => line.startsWith("destructured ")),
      job.label
    );
  if (job.focused.family === "expressions") {
    for (const [kind, table] of Object.entries({ unary: UNARY, binary: BINARY, logical: LOGICAL }))
      for (const op of Object.values(table))
        assert(
          job.result.out.some((line) => line.startsWith(`${kind} ${op} `)),
          `${job.label}: missing ${kind} ${op}`
        );
  }
  if (job.result.end === "normal" && sameRun(job.result, job.node)) covered.completed++;
  if (splitJobs.some((s) => s.of === job && !s.orderKept && sameRun(s.result, job.result) && sameRun(s.node, job.node)))
    covered.reordered++;
}
if (bindingOnly) {
  const negations = splitJobs.filter((j) => j.named?.localNegation);
  assert.equal(negations.length, count);
  assert(negations.every((j) => j.moves && j.result.end === "normal" && sameRun(j.node, j.of.node)));
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
  const always = [
    "forwarded arguments",
    "constructor links",
    "prototype links",
    "construction receiver",
    "method dispatch",
  ];
  const outcomes = ["retargeted constructor", "returned object", "thrown value"];
  const coverage = new Map();
  for (const job of jobs.filter((j) => j.focused?.family === "inheritance")) {
    const features = job.focused.witnesses.map(({ feature }) => feature);
    assert.equal(features.length, always.length + 1, `${job.label}: expected one additional constructor outcome`);
    assert(
      always.every((feature) => features.includes(feature)) && outcomes.some((outcome) => features.includes(outcome)),
      `${job.label}: missing generated inheritance observations`
    );
    for (const { feature, line } of job.focused.witnesses) {
      const prefix = `${feature} `;
      const observed = job.result.out.filter((out) => out.startsWith(prefix));
      assert.deepEqual(observed, [line], `${job.label}: CoreJS witness for ${feature}`);
      assert.deepEqual(
        job.node.out.filter((out) => out.startsWith(prefix)),
        observed,
        `${job.label}: Node witness for ${feature}`
      );
      const entry = coverage.get(feature) ?? { programs: 0, widths: new Set() };
      entry.programs++;
      entry.widths.add(observed[0].slice(prefix.length).split(" ").length);
      coverage.set(feature, entry);
    }
  }
  for (const feature of always)
    assert.equal(coverage.get(feature)?.programs, count, `every seed must execute ${feature}`);
  assert.equal(coverage.get("forwarded arguments").widths.size, Math.min(count, 3), "distinct argument counts");
  for (const feature of ["constructor links", "prototype links"])
    assert.equal(coverage.get(feature).widths.size, Math.min(count, 4), `${feature}: distinct chain lengths`);
  if (count >= 12)
    assert(
      outcomes.every((outcome) => coverage.has(outcome)),
      "the generated sample must execute every outcome"
    );
  console.log("generated inheritance programs with matching runtime witnesses:");
  console.table(Object.fromEntries([...coverage].map(([feature, { programs }]) => [feature, { programs }])));
}
if (inheritanceOnly) {
  const certified = splitJobs.filter((j) => j.named?.certifiedHeritage);
  assert.equal(certified.length, count + 1, "the handwritten and every generated heritage split must execute");
  const compositions = certified.flatMap((j) => (j.named.heritageCoverage ? [j.named.heritageCoverage] : []));
  assert.equal(compositions.length, count, "every seed must execute its composed heritage split");
  const rejected = splitJobs.filter((j) => j.named?.heritageMutation);
  assert.equal(rejected.length, count, "every seeded mutation must reach the guard");
  assert(
    [...certified, ...rejected].every(
      (j) => j.result.end === "normal" && sameRun(j.result, j.of.result) && sameRun(j.node, j.of.node)
    ),
    "composed heritage plans and their mutations must preserve normal execution in the model and in Node"
  );
  for (const job of [...certified, ...rejected]) {
    const observed = job.named.heritageObservedPrefix;
    if (observed)
      assert(
        job.node.out[0]?.startsWith(`${observed} `),
        `${job.label}: expected constructor and prototype observations ${observed}`
      );
  }
  const mutations = new Set(rejected.map((j) => j.named.heritageMutation));
  assert.equal(
    mutations.size,
    Math.min(count, 5),
    "the seeded mutations must exercise the different guard restrictions"
  );
  if (count >= 12) {
    for (const [axis, minimum] of Object.entries({
      baseMembers: 3,
      pivotMembers: 4,
      prefix: 3,
      crossed: 5,
      block: 2,
      width: 4,
    })) {
      const shapes = new Set(compositions.flatMap((c) => c[axis]));
      assert(shapes.size >= minimum, `heritage compositions must exercise ${minimum} ${axis} shapes: ${[...shapes]}`);
    }
    assert(
      new Set(compositions.map((shape) => JSON.stringify(shape))).size >= 8,
      "heritage builds must vary in shape across seeds"
    );
  }
  console.log(`certified class heritage moves: ${certified.length}, four runs and final state compared`);
  console.log(`guard-rejected heritage mutations: ${rejected.length}, covering ${[...mutations].sort().join(", ")}`);
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
  "every focused family must complete and execute in a matching accepted reordered split for every seed\n" +
    failures.filter((f) => jobs.some((j) => j.focused && f.startsWith(`${j.label}:`))).join("\n")
);
assert(
  jobs.filter((j) => j.linked).every((j) => sameRun(j.result, j.node)),
  "every generated linked program must match Node"
);
assert.equal(
  jobs.filter((j) => j.linked).length,
  broad ? count * 4 : 0,
  "each seed must run the default-export, diamond, cycle and abrupt module graphs"
);
const witnessedGraphs = jobs.filter((j) => j.linked && j.expectedOutput);
assert.equal(witnessedGraphs.length, broad ? count : 0);
for (const job of witnessedGraphs) assert.deepEqual(job.node.out, job.expectedOutput, job.label);
console.log(`generated linked programs: ${linked.length}, compared with Node and Plan.evalOrder`);
for (const job of jobs.filter((j) => j.opaqueCase?.expectedOutput)) {
  assert.deepEqual(job.node.out, job.opaqueCase.expectedOutput, job.label);
  assert.deepEqual(job.result.out, job.opaqueCase.expectedOutput, job.label);
}
const opaqueMoved = splitJobs.filter((j) => j.of.opaqueCase?.move && !j.orderKept);
assert.equal(
  opaqueMoved.length,
  allOpaqueCases.filter((c) => c.move).length,
  "every opaque closure move must be accepted and executed"
);
const generatedClassJobs = jobs.filter((j) => j.opaqueCase?.generatedClass);
assert.equal(generatedClassJobs.length, broad ? count : 0, "every seed must generate an opaque class case");
const generatedClassMoves = opaqueMoved.filter((j) => j.of.opaqueCase?.generatedClass);
assert.equal(
  generatedClassMoves.length,
  generatedClassJobs.length,
  "every generated opaque class must execute in an accepted reordered split"
);
for (const job of generatedClassMoves) {
  const { bodyCalls } = job.of.opaqueCase;
  assert(job.moves, `${job.label}: the reordered class must have a move certificate`);
  for (const [runName, run] of Object.entries({
    "bundle in Node": job.of.node,
    "bundle in CoreJS": job.of.numbered.result,
    "split in Node": job.node,
    "split in CoreJS": job.result,
    "split replaying the bundle's world": job.inBundleWorld.result,
  })) {
    assert.equal(run.end, "normal", `${job.label}: ${runName} must complete`);
    assert.deepEqual(
      run.out.filter((line) => line.startsWith("body:")).map((line) => line.split(" ")[0]),
      bodyCalls.map((body) => `body:${body}`),
      `${job.label}: ${runName} must invoke every generated opaque body in order`
    );
  }
  assert.equal(
    job.inBundleWorld.result.state,
    job.of.numbered.result.state,
    `${job.label}: the reordered class must preserve the bundle's final model state`
  );
}
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
