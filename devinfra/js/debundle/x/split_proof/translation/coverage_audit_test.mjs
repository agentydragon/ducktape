// The coverage audit's parts: the syntax inventory agrees with the parser, the readers of the Lean sources read the shapes
// they are written for, the probe counts what a program calls and nothing else, and a sample of two generators renders.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
import { GENERATORS, renderReport, sampleGenerator } from "./coverage_audit.mjs";
import { readAnalysisInventory } from "./analysis_inventory.mjs";
import { FORMS, NODE_TYPES, featuresOf, parseModule } from "./syntax_inventory.mjs";

const work = realpathSync(mkdtempSync(path.join(process.env.TEST_TMPDIR ?? os.tmpdir(), "coverage_audit_test_")));

// ---- the inventory: every form's witness snippet parses and is found by the form's own predicate
{
  const seen = new Set();
  for (const form of FORMS) {
    const { types, forms } = featuresOf(parseModule(form.snippet));
    assert(forms.has(form.name), `form ${form.name} is not found in its snippet ${form.snippet}`);
    for (const type of types.keys()) seen.add(type);
  }
  const listed = new Set(NODE_TYPES.map(([type]) => type));
  assert.deepEqual(
    [...seen].filter((type) => !listed.has(type)),
    [],
    "node types the snippets have and the list lacks"
  );
  // `WithStatement` is a sloppy-mode statement: no module has it
  assert.deepEqual(
    [...listed].filter((type) => !seen.has(type)),
    ["WithStatement"]
  );
  assert.equal(new Set(FORMS.map((f) => f.name)).size, FORMS.length, "duplicate form names");
}

// ---- the probe counts the calls of the program's own code, through callbacks too, and not the engine's or Node's
{
  const keys = path.join(work, "keys.json");
  writeFileSync(
    keys,
    JSON.stringify([
      "Object.keys",
      "Array.prototype.map",
      "prim.toUpperCase",
      "env:console.log",
      "Math.max",
      "Map",
      "Map.prototype.next",
      "Set.prototype.map",
      "Promise.resolve",
      "Number.parseInt",
      "parseInt",
    ])
  );
  const file = path.join(work, "probe_program.mjs");
  writeFileSync(
    file,
    [
      "const o = { a: 1, b: 2 };",
      "console.log(Object.keys(o).map((k) => k.toUpperCase()).join());",
      "const m = new Map([[1, 2]]);",
      "m.keys().next();",
      "class Sub extends Map {}",
      "new Sub();",
      "const p = Promise.resolve(1);",
      "console.log(Promise.resolve(p) === p, parseInt('1'), Number.parseInt('2'));",
    ].join("\n")
  );
  const out = path.join(work, "probe_out.json");
  const run = spawnSync(
    process.execPath,
    ["--import", pathToFileURL(fileURLToPath(new URL("./intrinsics_probe.mjs", import.meta.url))).href, file],
    {
      env: { ...process.env, PROBE_KEYS: keys, PROBE_OUT: out, PROBE_PROGRAM: pathToFileURL(file).href },
      encoding: "utf8",
    }
  );
  assert.equal(run.status, 0, run.stderr);
  assert.equal(run.stdout, "A,B\ntrue 1 2\n", "the probe changed what the program prints");
  const { counts } = JSON.parse(readFileSync(out, "utf8"));
  assert.deepEqual(counts, {
    "Object.keys": 1,
    "Array.prototype.map": 1,
    "prim.toUpperCase": 2,
    "env:console.log": 2,
    Map: 2,
    "Map.prototype.next": 1,
    "Promise.resolve": 2,
    parseInt: 1,
    "Number.parseInt": 1,
  });
}

// Seed 6 contains a ThisExpression. Its script is eligible without instrumentation
// and excluded when observing modern-marker execution.
{
  const sample = { count: 1, seed: 6 };
  const script = GENERATORS.find((g) => g.id === "shift-script");
  const modern = GENERATORS.find((g) => g.id === "shift-modern");
  assert.equal((await script.produce(sample))[0].skipped, undefined);
  assert.equal((await modern.produce(sample))[0].skipped, "the test skips it");
}

// ---- a sample of two generators renders a report with the tables
{
  const inventory = readAnalysisInventory();
  assert(inventory.tableKeys.includes("Object.keys"));
  assert(inventory.memberPaths.includes("Array.prototype.map"));
  assert.deepEqual(Object.keys(inventory.witnesses).sort(), FORMS.map((f) => f.name).sort());
  const keysFile = path.join(work, "audit_keys.json");
  writeFileSync(keysFile, JSON.stringify([...inventory.tableKeys, ...inventory.memberPaths]));
  const samples = [];
  for (const id of ["templates", "ast"]) {
    const dir = path.join(work, id);
    mkdirSync(dir, { recursive: true });
    samples.push(
      await sampleGenerator(
        GENERATORS.find((g) => g.id === id),
        { count: 4, seed: 1, work: dir, jobs: 2, keysFile }
      )
    );
  }
  for (const s of samples) {
    assert.equal(s.generated, 4);
    assert.equal(s.status.ok, 4, `${s.id}: programs that throw: ${JSON.stringify(s.errors)}`);
    assert(s.types.get("Program") === 4 && s.types.get("FunctionDeclaration") > 0, `${s.id}: node types`);
    assert(s.rows.get("env:console.log") > 0, `${s.id}: the probe saw no console.log`);
  }
  const report = renderReport({ inventory, samples, count: 4, seed: 1 });
  assert.match(report, /\| `ExpressionStatement` \| ES5 \|/);
  assert.match(report, /\| `Object\.assign` \|/);
  assert.match(report, /\| `JSON\.stringify` \|/);
  assert.match(report, /since; witness observation/);
  assert.deepEqual(inventory.witnesses["function:generator"].effectUnits, []);
  assert.deepEqual(inventory.witnesses["import()"].effectUnits, [0]);
}

rmSync(work, { recursive: true, force: true });
console.log("coverage audit parts ok");
