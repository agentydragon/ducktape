import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

if (process.env.ACORN_RLOCATION)
  process.env.JS_MODULE_PROOF_ACORN = resolve(process.env.TEST_SRCDIR, process.env.ACORN_RLOCATION);
process.env.JS_MODULE_PROOF_LEAN = resolve(process.env.TEST_SRCDIR, process.env.LEAN_RLOCATION);
const here = dirname(fileURLToPath(import.meta.url));
const root = await mkdtemp(join(process.env.TEST_TMPDIR, "binding-"));
if (process.env.TEST_SHARD_STATUS_FILE) await writeFile(process.env.TEST_SHARD_STATUS_FILE, "");
const shard = Number(process.env.TEST_SHARD_INDEX ?? "0");
const shards = Number(process.env.TEST_TOTAL_SHARDS ?? "1");
const runSection = (index) => shards === 1 || shard === index;
const source = join(here, "example/bindings/original.mjs");
const cli = (...args) => {
  const child = spawnSync(process.execPath, [join(here, "main.mjs"), ...args], { encoding: "utf8" });
  return { status: child.status, value: JSON.parse(child.stdout || child.stderr) };
};
function execute(path, calls) {
  const code = `const events=[];globalThis.record=value=>events.push(value);import(${JSON.stringify(pathToFileURL(path).href)}).then(m=>console.log(JSON.stringify({events,results:${JSON.stringify(calls)}.map(name=>m[name]())})));`;
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8" });
  assert.equal(child.status, 0, child.stderr);
  return JSON.parse(child.stdout);
}
const models = (
  await Promise.all(
    ["Semantics.lean", "Effects.lean", "Bindings.lean"].map((name) => readFile(join(here, name), "utf8"))
  )
).join("\n");
if (runSection(0)) {
  for (const safe of [true, false]) {
    const label = safe ? "safe" : "unsafe";
    const spec = join(here, `example/bindings/${label}.json`);
    const out = join(root, label);
    assert.equal(cli("generate", source, spec, out).value.result, "generated");
    const checked = cli("validate", source, spec, out);
    assert.equal(checked.value.result, safe ? "proved under assumptions" : "unsafe", JSON.stringify(checked));
    if (!safe) {
      assert.deepEqual(checked.value.original, { events: [1], exports: ["increment"] });
      assert.deepEqual(checked.value.output, { events: [0], exports: ["increment"] });
    }
    if (safe)
      assert.deepEqual(
        execute(source, ["increment", "increment"]),
        execute(join(out, "entry.mjs"), ["increment", "increment"])
      );
    const application = safe
      ? `
theorem fixture_reference : ∃ owner declaration,
    resolveReference bindingModules 1 "amount" = some (owner, declaration) ∧
      sourceDeclaration bindingSource "amount" = some declaration := by
  let selected := bindingModules[1]'(by decide)
  apply accepted_reference_owner bindingSource bindingExports bindingModules bindingEntry 1 selected
    (.function "increment" "count" "amount") "amount"
  · exact binding_graph_valid
  · change 1 ∈ [0, 1, 2, 3]
    exact List.Mem.tail _ (List.Mem.head _)
  · decide
  · change (BindingStmt.function "increment" "count" "amount") ∈
      [BindingStmt.variable "count" 0, BindingStmt.function "increment" "count" "amount", BindingStmt.call "increment"]
    exact List.Mem.tail _ (List.Mem.head _)
  · change "amount" ∈ ["count", "amount"]
    exact List.Mem.tail _ (List.Mem.head _)
#print axioms fixture_reference
`
      : "";
    const proof = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
      encoding: "utf8",
      input: models + checked.value.certificate + application,
    });
    assert.equal(proof.status, 0, proof.stdout + proof.stderr);
    assert.match(proof.stdout, /'accepted_reference_owner' does not depend on any axioms/);
    if (safe) assert.match(proof.stdout, /'fixture_reference' does not depend on any axioms/);
    assert.match(
      proof.stdout,
      safe
        ? /'binding_future_calls' does not depend on any axioms/
        : /'binding_difference' does not depend on any axioms/
    );
    if (safe) {
      const statePath = join(out, "state.mjs");
      const state = await readFile(statePath, "utf8");
      for (const changed of [state.replace("./amount.mjs", "./state.mjs"), state.replace("+= amount", "+= count")]) {
        assert.notEqual(changed, state);
        await writeFile(statePath, changed);
        assert.equal(cli("validate", source, spec, out).value.result, "unknown");
      }
      await writeFile(statePath, state);
      const observerPath = join(out, "observe.mjs");
      const observer = await readFile(observerPath, "utf8");
      await writeFile(observerPath, observer.replace("globalThis.record(count);", ""));
      assert.equal(cli("validate", source, spec, out).value.result, "unknown");
      const [importLine, ...rest] = observer.trim().split("\n");
      await writeFile(observerPath, `${rest.join("\n")}\n${importLine}\n`);
      assert.equal(cli("validate", source, spec, out).value.result, "proved under assumptions");
    }
  }
}

if (runSection(2)) {
  // Numeric const bindings remain immutable across extraction and generation.
  const constSource = join(root, "const-source.mjs");
  const constOut = join(root, "const-out");
  await writeFile(constSource, (await readFile(source, "utf8")).replace("let amount = 1", "const amount = 1"));
  const safeSpec = join(here, "example/bindings/safe.json");
  assert.equal(cli("generate", constSource, safeSpec, constOut).value.result, "generated");
  assert.equal(cli("validate", constSource, safeSpec, constOut).value.result, "proved under assumptions");
  assert.deepEqual(execute(constSource, ["increment"]), execute(join(constOut, "entry.mjs"), ["increment"]));
  const amountFile = join(constOut, "amount.mjs");
  await writeFile(amountFile, (await readFile(amountFile, "utf8")).replace("const amount", "let amount"));
  assert.equal(cli("validate", constSource, safeSpec, constOut).value.result, "unknown");
  const immutableTarget = join(root, "immutable-target.mjs");
  await writeFile(immutableTarget, (await readFile(source, "utf8")).replace("let count = 0", "const count = 0"));
  assert.equal(cli("generate", immutableTarget, safeSpec, join(root, "immutable-target-out")).value.result, "unknown");

  // Assigning a function to a different module from its write target would turn
  // the target into an immutable import; it must not be proved safe.
  const importedWriteSpec = join(root, "imported-write.json");
  const importedWriteOut = join(root, "imported-write");
  await writeFile(
    importedWriteSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "amount.mjs": { imports: [], statements: [1] },
        "state.mjs": { imports: [], statements: [0] },
        "logic.mjs": { imports: ["state.mjs", "amount.mjs"], statements: [2, 3] },
        "observe.mjs": { imports: ["state.mjs"], statements: [4] },
        "entry.mjs": { imports: ["logic.mjs", "observe.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", source, importedWriteSpec, importedWriteOut).value.result, "unknown");

  // A different number of declarations, functions and calls must use the same
  // syntax-directed frontend and Lean linker, not a template for the first input.
  const other = join(root, "other.mjs");
  const otherSpec = join(root, "other.json");
  const otherOut = join(root, "other");
  await writeFile(
    other,
    `let total = 0;\nlet first = 2;\nlet second = 3;\nfunction plus() { total += first; }\nfunction extra() { total += second; }\nplus();\nextra();\nglobalThis.record(total);\nexport { plus, extra };\n`
  );
  await writeFile(
    otherSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "amount.mjs": { imports: [], statements: [1, 2] },
        "state.mjs": { imports: ["amount.mjs"], statements: [0, 3, 4, 5, 6] },
        "observe.mjs": { imports: ["state.mjs"], statements: [7] },
        "entry.mjs": { imports: ["state.mjs", "observe.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", other, otherSpec, otherOut).value.result, "generated");
  const otherResult = cli("validate", other, otherSpec, otherOut);
  assert.equal(otherResult.value.result, "proved under assumptions", JSON.stringify(otherResult));
  assert.deepEqual(execute(other, ["plus", "extra"]), execute(join(otherOut, "entry.mjs"), ["plus", "extra"]));
}

if (runSection(1)) {
  // Exercise the real generator and validator with an exported getter, then
  // change the emitted getter so the bug appears only after a later call.
  const readerSource = join(root, "reader_source.mjs");
  const readerSpec = join(root, "reader_spec.json");
  const readerOutput = join(root, "reader_output");
  await writeFile(
    readerSource,
    `let count = 0;\nlet amount = 2;\nfunction increment() { count += amount; }\nfunction current() { return count; }\nincrement();\nglobalThis.record(count);\nexport { increment, current };\n`
  );
  await writeFile(
    readerSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "amount.mjs": { imports: [], statements: [1] },
        "state.mjs": { imports: ["amount.mjs"], statements: [0, 2, 4] },
        "reader.mjs": { imports: ["state.mjs"], statements: [3, 5] },
        "entry.mjs": { imports: ["state.mjs", "reader.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", readerSource, readerSpec, readerOutput).value.result, "generated");
  const readerResult = cli("validate", readerSource, readerSpec, readerOutput);
  assert.equal(readerResult.value.result, "proved under assumptions", JSON.stringify(readerResult));
  assert.match(readerResult.value.certificate, /theorem binding_return_values/);
  const calls = ["current", "increment", "current", "increment", "current"];
  const sourceResult = execute(readerSource, calls);
  assert.deepEqual(sourceResult, { events: [2], results: [2, null, 4, null, 6] });
  assert.deepEqual(execute(join(readerOutput, "entry.mjs"), calls), sourceResult);
  const readerPath = join(readerOutput, "reader.mjs");
  const readerText = await readFile(readerPath, "utf8");
  await writeFile(
    readerPath,
    `import { amount } from "./amount.mjs";\n${readerText.replace("return count", "return amount")}`
  );
  assert.equal(cli("validate", readerSource, readerSpec, readerOutput).value.result, "unknown");
  const brokenResult = execute(join(readerOutput, "entry.mjs"), calls);
  assert.deepEqual(brokenResult.events, sourceResult.events);
  assert.equal(brokenResult.results[0], sourceResult.results[0]);
  assert.notEqual(brokenResult.results[2], sourceResult.results[2]);
}
