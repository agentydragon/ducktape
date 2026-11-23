// The coverage audit's parts: the syntax inventory agrees with the parser, the readers of the Lean sources read the shapes
// they are written for, the probe counts what a program calls and nothing else, and a sample of two generators renders.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
import { GENERATORS, analysisOf, renderReport, sampleGenerator } from "./coverage_audit.mjs";
import { memberPaths, nodeTypeArms, stringList, tableKeys } from "./analysis_inventory.mjs";
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

// ---- the readers of the Lean sources, on small samples of the shapes they read
{
  const effects = `
partial def evalIdent (fr : Fr) (n : String) : M AV := do
  | "NotThisOne" => pure ()

partial def eval (fr : Fr) (j : Json) : M AV := do
  match typ j with
  | "Literal" =>
    return AV.prim
  | "FunctionExpression" | "ArrowFunctionExpression" =>
    match typ x with
    | some "__proto__" => pure ()
    return AV.prim
  | t =>
    giveUp t j

partial def exec (fr : Fr) (s : Json) : M Unit := do
  | "IfStatement" => pure ()
`;
  assert.deepEqual([...nodeTypeArms(effects, "eval")].sort(), [
    "ArrowFunctionExpression",
    "FunctionExpression",
    "Literal",
  ]);
  assert.deepEqual([...nodeTypeArms(effects, "exec")], ["IfStatement"]);
  assert.throws(() => nodeTypeArms(effects, "missing"));

  const intrinsics = `
def iteratorHelperRows : List (String × Summary) :=
  ["map", "filter"].flatMap fun m =>
    ["Map", "Set"].map fun c => (c ++ ".prototype." ++ m, { world := true })

def table : Std.HashMap String Summary := Std.HashMap.ofList (([
  ("Object.keys", { ret := freshPrims }),
  ("Math.abs", {}), ("Math.max", {}),
  ("env:console.log", { world := true })
] : List (String × Summary)) ++ iteratorHelperRows)

def intrinsicGlobals : Std.HashSet String := Std.HashSet.ofList [
  "Object", "Array"]
`;
  assert.deepEqual(tableKeys(intrinsics), [
    "Object.keys",
    "Math.abs",
    "Math.max",
    "env:console.log",
    "Map.prototype.map",
    "Map.prototype.filter",
    "Set.prototype.map",
    "Set.prototype.filter",
  ]);
  assert.deepEqual(stringList(intrinsics, "intrinsicGlobals"), ["Object", "Array"]);
  assert.deepEqual(memberPaths('def memberPaths : List String := [\n  "Array.from",\n  "Array.of"\n]\n'), [
    "Array.from",
    "Array.of",
  ]);
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

// ---- a sample of two generators renders a report with the tables
{
  const inventory = {
    arms: {
      eval: new Set(["Identifier", "Literal"]),
      exec: new Set(["ExpressionStatement"]),
      evalClass: new Set(),
      bindPattern: new Set(),
    },
    tableKeys: ["Object.assign", "env:console.log"],
    memberPaths: ["Object.assign", "JSON.stringify", "env:console.log"],
    intrinsicGlobals: [],
  };
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
  const report = renderReport({ inventory, samples, count: 4, seed: 1, effectsText: "" });
  assert.match(report, /\| `ExpressionStatement` \| ES5; `exec` \|/);
  assert.match(report, /\| `YieldExpression` \| ES2015; \*\*gives up\*\*/);
  assert.match(report, /\| `Object\.assign` \|/);
  assert.match(report, /\| `JSON\.stringify` \|/);
  assert.equal(analysisOf("Property", inventory), "part of `evalObject`");
}

rmSync(work, { recursive: true, force: true });
console.log("coverage audit parts ok");
