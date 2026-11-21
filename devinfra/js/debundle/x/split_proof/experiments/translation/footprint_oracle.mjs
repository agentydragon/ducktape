// Oracle for the footprints of `Effects.lean`: run a program unit by unit in Node and compare, after every unit, what each
// module-scope binding reaches with what `lower` says the unit may write.
//
//   node footprint_oracle.mjs <program.mjs> [--premises premises.json] [--runs 2] [--strict] [--json]
//
// A unit is a top-level statement, or one declarator of a variable list, as in `frontend.mjs`. The program is rewritten to
// call the harness (`oracle_harness.mjs`) before the first unit and after each one; the harness digests what every
// top-level binding reaches. A binding whose digest changed during unit `i` must be bound by the unit: a name it declares,
// or one in its `writes`. A unit that changed a binding and is not in either is a **wrong footprint** (`unwritten`): a
// split that moves a reader of the binding across that unit is accepted by `translate` and run differently by Node. For a
// unit that is an effect the same gap is reported as `unlisted`: its `writes` need not name the binding for the unit to be
// ordered against other effects, but a pure reader of the binding can still cross it. Also checked: a unit that printed or
// added a global must be an effect, and a name a unit initializes must be one it declares.
//
// What the oracle cannot see, and so cannot check:
//   - reads: only changes are observed;
//   - closure-private state: a variable a function captured, a private class member, a generator's or an iterator's
//     cursor, the state of a promise, the contents of a `WeakMap` or `WeakSet`, what a `Proxy` does inside;
//   - a change that leaves the digest alone: assigning a primitive its own value, or an object that looks the same and is
//     the same object;
//   - changes by asynchronous continuations: they run after the last unit and are listed apart (`tail`);
//   - the environment beyond the global object's own properties and what is written to stdout and stderr.
// A program that throws, times out or does not give the same answer twice is not a valid input (premise P4): the oracle
// fails on it instead of checking. One that patches a standard object breaks premise P1: it is checked all the same (the
// digests treat the standard objects as opaque, so a patch changes no binding) and the patched objects are listed.
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { frontBundle } from "./frontend.mjs";
import { walk } from "./syntax_inventory.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const harness = pathToFileURL(path.join(here, "oracle_harness.mjs")).href;
const defaultLower = () => process.env.LOWER ?? path.join(here, "../../lean/.lake/build/bin/lower");

/**
 * The program with a call of the harness after each unit. `units[i].source` is the text of unit `i`; `names` are the
 * module-scope bindings that are not imports. Mirrors the units of `frontend.mjs`: imports and export lists are no unit,
 * an exported declaration is its declaration, a variable list is one unit per declarator.
 */
export function instrument(source) {
  const ast = acorn.parse(source, { ecmaVersion: "latest", sourceType: "module", ranges: true });
  // a top-level `await` interleaves the continuations of earlier units with the next unit, and `translate` refuses it
  walk(ast, (node, parent, path) => {
    const awaits = node.type === "AwaitExpression" || (node.type === "ForOfStatement" && node.await);
    if (
      awaits &&
      !path.some((n) => /Function/.test(n.type) || n.type === "StaticBlock" || n.type === "PropertyDefinition")
    )
      throw new Error(
        "a top-level await: the checker refuses it, and its continuations cannot be told from the next unit"
      );
  });
  const scope = eslintScope.analyze(ast, { ecmaVersion: 2024, sourceType: "module", ignoreEval: true });
  const moduleScope = scope.scopes.find((s) => s.type === "module");
  const names = moduleScope.variables.filter((v) => v.defs.every((d) => d.type !== "ImportBinding")).map((v) => v.name);
  const units = [];
  const body = [];
  const call = (i) => `\n;globalThis.__oracle(${i}, __oracleSnapshot);\n`;
  const add = (text) => {
    body.push(text, call(units.length));
    units.push({ source: text });
  };
  for (const statement of ast.body) {
    let node = statement;
    if (statement.type === "ImportDeclaration") {
      body.push(source.slice(statement.start, statement.end), "\n");
      continue;
    }
    if (
      statement.type === "ExportAllDeclaration" ||
      statement.type === "ExportDefaultDeclaration" ||
      (statement.type === "ExportNamedDeclaration" && statement.source)
    )
      throw new Error(`unsupported ${statement.type}`);
    if (statement.type === "ExportNamedDeclaration") {
      if (!statement.declaration) continue;
      node = statement.declaration;
    }
    if (node.type === "VariableDeclaration" && node.declarations.length > 1)
      for (const d of node.declarations) add(`${node.kind} ${source.slice(d.start, d.end)};`);
    else add(source.slice(node.start, node.end));
  }
  const cells = names.map((n) => `(() => { try { return [${n}]; } catch { return undefined; } })()`);
  const text = [
    `__oracleSnapshot.names = ${JSON.stringify(names)};`,
    "globalThis.__oracle(-1, __oracleSnapshot);",
    ...body,
    `function __oracleSnapshot() { return [${cells.join(", ")}]; }`,
    "",
  ].join("\n");
  return { text, units, names };
}

function run(command, args, { env, timeoutMs }) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { env, stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, timeoutMs);
    child.stderr.on("data", (d) => {
      if (stderr.length < 65536) stderr += d;
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (status) => {
      clearTimeout(timer);
      resolve({ status, timedOut, stderr });
    });
  });
}

/** Run the instrumented program once: `{ invalid }` if it is not a valid input, else the harness's report. */
async function runOnce(text, dir, tag, timeoutMs) {
  const file = path.join(dir, `oracle_${tag}.mjs`);
  const out = path.join(dir, `oracle_${tag}.json`);
  writeFileSync(file, text);
  const r = await run(process.execPath, ["--max-old-space-size=512", "--import", harness, file], {
    env: { ...process.env, ORACLE_OUT: out },
    timeoutMs,
  });
  if (r.timedOut) return { invalid: `the program did not finish in ${timeoutMs} ms` };
  let report;
  try {
    report = JSON.parse(readFileSync(out, "utf8"));
  } catch {
    return { invalid: `no report (exit ${r.status}): ${r.stderr.split("\n").slice(0, 3).join(" | ")}` };
  }
  if (r.status !== 0)
    return {
      invalid: `the program throws (premise P4): ${r.stderr.split("\n").find((l) => /Error/.test(l)) ?? r.stderr.split("\n")[0]}`,
    };
  return { report };
}

/** The footprints `lower` computes for the program, one per unit. */
export function footprintsOf(file, { lower = defaultLower(), premises = { noWorldHooks: true }, dir }) {
  const bundle = path.join(dir, "bundle.json");
  const premisesFile = path.join(dir, "premises.json");
  writeFileSync(bundle, JSON.stringify(frontBundle(file)));
  writeFileSync(premisesFile, JSON.stringify(premises));
  const r = spawnSync(lower, [bundle, "--premises", premisesFile], { encoding: "utf8", maxBuffer: 1 << 28 });
  if (r.status !== 0) throw new Error(`lower exited ${r.status}: ${r.stderr}`);
  return r.stdout
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l));
}

/**
 * Compare what each unit changed with its footprint. `report.units[i]` is `{ changed, initialized, printed }`.
 * Kinds: `unwritten` (a binding changed, not declared or written by the unit, and the unit is not an effect),
 * `unlisted` (the same, for an effect), `global` (a global added or changed by a unit that is not an effect), `printed`
 * (output by a unit that is not an effect), `foreign-definition` (a binding initialized by a unit that does not declare it).
 */
export function compare(units, footprints, report) {
  const findings = [];
  report.units.forEach((entry, i) => {
    const fp = footprints[i];
    const bound = new Set([...fp.defs, ...fp.writes]);
    for (const name of entry.changed) {
      if (name === "<global>") {
        if (!fp.effect) findings.push({ unit: i, kind: "global", name });
      } else if (!bound.has(name)) findings.push({ unit: i, kind: fp.effect ? "unlisted" : "unwritten", name });
    }
    if (entry.printed > 0 && !fp.effect) findings.push({ unit: i, kind: "printed", name: `${entry.printed} writes` });
    for (const name of entry.initialized)
      if (name !== "<global>" && !fp.defs.includes(name)) findings.push({ unit: i, kind: "foreign-definition", name });
  });
  return findings.map((f) => ({ ...f, source: units[f.unit].source }));
}

/**
 * Run the instrumented program `runs` times: `{ invalid }` when it is not a valid input (it throws, does not finish or does
 * not give the same answer every time), else `{ units, names, report }` with `report` as the harness writes it:
 * `units[i]` is `{ unit, printed, changed, initialized }`, `tail` the bindings that continuations changed after the last
 * unit, `patched` the standard objects the program changed.
 */
export async function observe(source, { runs = 2, timeoutMs = 60000, dir } = {}) {
  const own = dir === undefined;
  dir ??= mkdtempSync(path.join(process.env.TEST_TMPDIR ?? process.env.TMPDIR ?? os.tmpdir(), "footprint_oracle_"));
  mkdirSync(dir, { recursive: true });
  try {
    let instrumented;
    try {
      instrumented = instrument(source);
    } catch (error) {
      return { invalid: `the program cannot be run unit by unit: ${error.message.split("\n")[0]}` };
    }
    const { text, units, names } = instrumented;
    const reports = [];
    for (let k = 0; k < runs; k++) {
      const r = await runOnce(text, dir, k, timeoutMs);
      if (r.invalid) return { invalid: r.invalid };
      reports.push(r.report);
    }
    const key = (r) => JSON.stringify([r.units, r.tail]);
    if (reports.some((r) => key(r) !== key(reports[0])))
      return { invalid: "the program does not give the same answer twice (it is not deterministic)" };
    return { units, names, report: reports[0] };
  } finally {
    if (own) rmSync(dir, { recursive: true, force: true });
  }
}

/**
 * Check one program (the text of an ES module): `{ invalid }` when it is not a valid input, else
 * `{ units, names, changed, findings, tail, patched }` where `changed` counts the (unit, binding) pairs the oracle saw
 * change. `lower` is the path of the footprint binary, `premises` what `lower` assumes (P3 by default, as the fuzzers do).
 */
export async function checkProgram(
  source,
  { lower = defaultLower(), premises = { noWorldHooks: true }, runs = 2, timeoutMs = 60000, dir } = {}
) {
  const own = dir === undefined;
  dir ??= mkdtempSync(path.join(process.env.TEST_TMPDIR ?? process.env.TMPDIR ?? os.tmpdir(), "footprint_oracle_"));
  mkdirSync(dir, { recursive: true });
  try {
    const seen = await observe(source, { runs, timeoutMs, dir });
    if (seen.invalid) return seen;
    const original = path.join(dir, "program.mjs");
    writeFileSync(original, source);
    const footprints = footprintsOf(original, { lower, premises, dir });
    if (footprints.length !== seen.units.length)
      throw new Error(`the oracle sees ${seen.units.length} units, lower ${footprints.length}`);
    return {
      units: seen.units.length,
      names: seen.names.length,
      changed: seen.report.units.reduce((n, u) => n + u.changed.length, 0),
      findings: compare(seen.units, footprints, seen.report),
      tail: seen.report.tail,
      patched: seen.report.patched,
      footprints,
      report: seen.report,
    };
  } finally {
    if (own) rmSync(dir, { recursive: true, force: true });
  }
}

const describe = (f) => `unit ${f.unit} ${f.kind} ${f.name}: ${f.source.replace(/\s+/g, " ").slice(0, 160)}`;

async function main() {
  const args = process.argv.slice(2);
  const option = (name, fallback) => (args.includes(`--${name}`) ? args[args.indexOf(`--${name}`) + 1] : fallback);
  const valueOptions = ["--premises", "--runs"];
  const file = args.find((a, i) => !a.startsWith("--") && !valueOptions.includes(args[i - 1]));
  if (!file)
    throw new Error("usage: footprint_oracle.mjs <program.mjs> [--premises file] [--runs N] [--strict] [--json]");
  const premisesFile = option("premises", undefined);
  const result = await checkProgram(readFileSync(file, "utf8"), {
    premises: premisesFile ? JSON.parse(readFileSync(premisesFile, "utf8")) : { noWorldHooks: true },
    runs: Number(option("runs", 2)),
  });
  if (result.invalid) {
    console.log(`invalid input: ${result.invalid}`);
    process.exit(2);
  }
  const count = (kind) => result.findings.filter((f) => f.kind === kind).length;
  if (args.includes("--json")) console.log(JSON.stringify({ ...result, footprints: undefined, report: undefined }));
  else {
    console.log(
      `${result.units} units, ${result.names} bindings, ${result.changed} changes seen; ` +
        `unwritten ${count("unwritten")}, unlisted ${count("unlisted")}, global ${count("global")}, printed ${count("printed")}, ` +
        `foreign definitions ${count("foreign-definition")}; ${result.tail.length} bindings changed after the last unit`
    );
    if (result.patched.length > 0)
      console.log(`  premise P1 does not hold: the program patches ${result.patched.slice(0, 5).join(", ")}`);
    for (const f of result.findings) console.log(`  ${describe(f)}`);
  }
  const hard = result.findings.filter((f) => f.kind !== "unlisted" || args.includes("--strict"));
  process.exit(hard.length > 0 ? 1 : 0);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) await main();
