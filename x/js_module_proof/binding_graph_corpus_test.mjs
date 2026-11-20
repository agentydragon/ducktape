import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

if (process.env.ACORN_RLOCATION)
  process.env.JS_MODULE_PROOF_ACORN = resolve(process.env.TEST_SRCDIR, process.env.ACORN_RLOCATION);
process.env.JS_MODULE_PROOF_LEAN = resolve(process.env.TEST_SRCDIR, process.env.LEAN_RLOCATION);
if (process.env.TEST_SHARD_STATUS_FILE) await writeFile(process.env.TEST_SHARD_STATUS_FILE, "");
const here = dirname(fileURLToPath(import.meta.url));
const shard = Number(process.env.TEST_SHARD_INDEX ?? "0");
const shards = Number(process.env.TEST_TOTAL_SHARDS ?? "1");
const patterns = [
  "safe",
  "unsafe",
  "tdz",
  "wrong_import",
  "body",
  "reader_body",
  "missing",
  "reexport",
  "cycle",
  "alias",
  "import_write",
  "reader_tdz",
];
const models = (
  await Promise.all(
    ["Semantics.lean", "Effects.lean", "Bindings.lean"].map((name) => readFile(join(here, name), "utf8"))
  )
).join("\n");

function cli(...args) {
  const child = spawnSync(process.execPath, [join(here, "main.mjs"), ...args], { encoding: "utf8" });
  assert.ok(child.stdout || child.stderr, `validator did not produce JSON: ${child.error}`);
  const result = JSON.parse(child.stdout || child.stderr);
  assert.equal(child.status, result.result === "unknown" ? 2 : 0, JSON.stringify(result));
  return result;
}

function execute(path, calls) {
  const code = `const events=[];globalThis.record=v=>events.push(v);try {const m=await import(${JSON.stringify(pathToFileURL(path).href)}); const results=${JSON.stringify(calls)}.map(n=>m[n]());console.log(JSON.stringify({events,exports:Object.keys(m),results,error:null}));}catch(e){console.log(JSON.stringify({events,error:e.name}));}`;
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8" });
  assert.equal(child.status, 0, child.stderr);
  return JSON.parse(child.stdout);
}

for (let i = 0; i < 36; i++) {
  if (i % shards !== shard) continue;
  const mode = patterns[i % patterns.length];
  const variant = Math.floor(i / patterns.length);
  const root = await mkdtemp(join(process.env.TEST_TMPDIR, `binding-graph-${i}-`));
  const output = join(root, "out");
  await mkdir(output);
  const cell = `cell${i}`;
  const amount = `amount${i}`;
  const bump = `bump${i}`;
  const current = `current${i}`;
  const initial = variant === 0 ? Number.MAX_SAFE_INTEGER - 8 : variant;
  const increment = (variant % 2) + 1;
  const declaration = `let ${cell} = ${initial};`;
  const other = `let ${amount} = ${increment};`;
  const functionDeclaration = `function ${bump}() { ${cell} += ${amount}; }`;
  const readerDeclaration = `function ${current}() { return ${cell}; }`;
  const call = `${bump}();`;
  const readerCall = `${current}();`;
  const record = `globalThis.record(${cell});`;
  const statements =
    mode === "tdz"
      ? [record, declaration, other, functionDeclaration, readerDeclaration, call, readerCall]
      : mode === "reader_tdz"
        ? [readerDeclaration, readerCall, declaration, other, functionDeclaration, call, record]
        : variant % 2
          ? [functionDeclaration, readerDeclaration, declaration, other, call, readerCall, record]
          : [declaration, other, functionDeclaration, readerDeclaration, call, readerCall, record];
  const sourcePath = join(root, "original.mjs");
  const specPath = join(root, "spec.json");
  await writeFile(sourcePath, `${statements.join("\n")}\nexport { ${bump}, ${current} };\n`);
  const select = (...items) => items.map((item) => statements.indexOf(item)).sort((a, b) => a - b);
  const stateStatements = [declaration, functionDeclaration];
  if (mode !== "unsafe" && mode !== "import_write") stateStatements.push(call);
  const modules = {
    "amount.mjs": { imports: [], statements: select(other) },
    "state.mjs": { imports: ["amount.mjs"], statements: select(...stateStatements) },
    "observe.mjs": { imports: ["state.mjs"], statements: select(readerDeclaration, readerCall, record) },
    "entry.mjs": { imports: ["state.mjs", "observe.mjs"], statements: mode === "unsafe" ? select(call) : [] },
  };
  if (mode === "import_write") {
    modules["state.mjs"] = { imports: [], statements: select(declaration) };
    modules["logic.mjs"] = { imports: ["state.mjs", "amount.mjs"], statements: select(functionDeclaration, call) };
    modules["entry.mjs"] = { imports: ["logic.mjs", "observe.mjs"], statements: [] };
  }
  await writeFile(specPath, JSON.stringify({ kind: "bindings", entry: "entry.mjs", modules }));

  // Construct candidates directly. No source or output passes through the
  // experiment's split generator, so this exercises the validator separately.
  const stateBody = stateStatements
    .sort((a, b) => statements.indexOf(a) - statements.indexOf(b))
    .map((item) => (item === declaration || item === functionDeclaration ? `export ${item}` : item));
  const observerBody = [readerDeclaration, readerCall, record]
    .sort((a, b) => statements.indexOf(a) - statements.indexOf(b))
    .map((item) => (item === readerDeclaration ? `export ${item}` : item));
  let files;
  if (mode === "import_write") {
    files = {
      "amount.mjs": `export ${other}\n`,
      "state.mjs": `export ${declaration}\n`,
      "logic.mjs": `import { ${cell} } from "./state.mjs";\nimport { ${amount} } from "./amount.mjs";\nexport ${functionDeclaration}\n${call}\n`,
      "observe.mjs": `import { ${cell} } from "./state.mjs";\n${observerBody.join("\n")}\n`,
      "entry.mjs": `import "./logic.mjs";\nimport "./observe.mjs";\nexport { ${bump} } from "./logic.mjs";\nexport { ${current} } from "./observe.mjs";\n`,
    };
  } else {
    files = {
      "amount.mjs": `export ${other}\n`,
      "state.mjs": `import { ${amount} } from "./amount.mjs";\n${stateBody.join("\n")}\n`,
      "observe.mjs": `import { ${cell} } from "./state.mjs";\n${observerBody.join("\n")}\n`,
      "entry.mjs": `import "./${variant % 2 ? "observe" : "state"}.mjs";\nimport "./${variant % 2 ? "state" : "observe"}.mjs";\nexport { ${bump} } from "./state.mjs";\nexport { ${current} } from "./observe.mjs";\n${mode === "unsafe" ? call : ""}\n`,
    };
    if (mode === "unsafe")
      files["entry.mjs"] = files["entry.mjs"].replace(
        'import "./state.mjs";',
        `import { ${bump} } from "./state.mjs";`
      );
    if (mode === "wrong_import") files["state.mjs"] = files["state.mjs"].replace('"./amount.mjs"', '"./state.mjs"');
    if (mode === "body") files["state.mjs"] = files["state.mjs"].replace(`+= ${amount}`, `+= ${cell}`);
    if (mode === "reader_body") {
      files["observe.mjs"] =
        `import { ${amount} } from "./amount.mjs";\n${files["observe.mjs"].replace(`return ${cell}`, `return ${amount}`)}`;
    }
    if (mode === "missing") files["state.mjs"] = files["state.mjs"].replace(call, "");
    if (mode === "reexport")
      files["entry.mjs"] = files["entry.mjs"].replace(`from "./state.mjs"`, `from "./amount.mjs"`);
    if (mode === "cycle") files["amount.mjs"] = `import "./entry.mjs";\n${files["amount.mjs"]}`;
    if (mode === "alias")
      files["state.mjs"] = files["state.mjs"]
        .replace(`{ ${amount} }`, `{ ${amount} as alias${i} }`)
        .replace(`+= ${amount}`, `+= alias${i}`);
    // ES imports are hoisted even if they follow executable statements.
    if (mode === "safe" && variant % 2 === 0) {
      const [first, ...rest] = files["observe.mjs"].trim().split("\n");
      files["observe.mjs"] = `${rest.join("\n")}\n${first}\n`;
    }
  }
  for (const [name, contents] of Object.entries(files)) await writeFile(join(output, name), contents);
  const result = cli("validate", sourcePath, specPath, output);
  const expected = mode === "safe" ? "proved under assumptions" : mode === "unsafe" ? "unsafe" : "unknown";
  assert.equal(result.result, expected, `${mode}: ${JSON.stringify(result)}`);

  const calls = [current, bump, current, bump, current];
  const before = execute(sourcePath, calls);
  const after = execute(join(output, "entry.mjs"), calls);
  if (mode === "safe" || mode === "unsafe") {
    assert.equal(before.error, null);
    assert.equal(after.error, null);
    const leanList = (items) => `[${items.join(", ")}]`;
    const expected = (observed) => {
      assert.ok(observed.events.every(Number.isSafeInteger));
      assert.ok(observed.results.every((value) => value === null || Number.isSafeInteger(value)));
      return `some (${leanList(observed.events.map((value) => `Observation.number ${value}`))}, ${leanList(observed.results.map((value) => (value === null ? "none" : `some ${value}`)))})`;
    };
    const run = (state, definitions) =>
      `((${state}).bind (fun state => (publicFunctionTable ${definitions} bindingExports).bind (fun table => runExportedCalls table state ${leanList(calls.map((name) => JSON.stringify(name)))}))).map (fun state => (state.events, state.returns))`;
    const comparison = `\ntheorem node_source_trace : ${run("sourceBindingState bindingSource", "bindingSource")} = ${expected(before)} := by decide\ntheorem node_output_trace : ${run("outputBindingState bindingSource bindingModules bindingEntry", "(outputDefinitions bindingModules)")} = ${expected(after)} := by decide\n#print axioms node_source_trace\n#print axioms node_output_trace\n`;
    const proof = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["--trust=0", "--stdin"], {
      encoding: "utf8",
      input: models + result.certificate + comparison,
    });
    assert.equal(proof.status, 0, proof.stdout + proof.stderr);
    assert.match(proof.stdout, /'node_source_trace' does not depend on any axioms/);
    assert.match(proof.stdout, /'node_output_trace' does not depend on any axioms/);
  }
  if (mode === "safe") assert.deepEqual(after, before, `Node mismatch in proved case ${i}`);
  if (mode === "unsafe") {
    assert.notDeepEqual(after.events, before.events);
    assert.deepEqual(result.original.events, before.events);
    assert.deepEqual(result.output.events, after.events);
  }
  if (mode === "reader_body") assert.notDeepEqual(after.results, before.results);
  if (mode === "tdz" || mode === "reader_tdz") assert.equal(before.error, "ReferenceError");
  if (mode === "import_write") assert.equal(after.error, "TypeError");
}
