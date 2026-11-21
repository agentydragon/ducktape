import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
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
  const code = `const events=[];globalThis.record=value=>events.push(value);import(${JSON.stringify(pathToFileURL(path).href)}).then(m=>console.log(JSON.stringify({events,results:${JSON.stringify(calls)}.map(call=>typeof call==='string'?m[call]():m[call.name](...call.args))})));`;
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
        assert.notEqual(cli("validate", source, spec, out).value.result, "proved under assumptions");
      }
      await writeFile(statePath, state);
      const observerPath = join(out, "observe.mjs");
      const observer = await readFile(observerPath, "utf8");
      await writeFile(observerPath, observer.replace("globalThis.record(count);", ""));
      assert.notEqual(cli("validate", source, spec, out).value.result, "proved under assumptions");
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
  assert.equal(cli("validate", readerSource, readerSpec, readerOutput).value.result, "unsafe");
  const brokenResult = execute(join(readerOutput, "entry.mjs"), calls);
  assert.deepEqual(brokenResult.events, sourceResult.events);
  assert.equal(brokenResult.results[0], sourceResult.results[0]);
  assert.notEqual(brokenResult.results[2], sourceResult.results[2]);
}

if (runSection(2)) {
  const aliasSource = join(root, "relay-alias-source.mjs");
  const aliasSpec = join(root, "relay-alias-spec.json");
  const aliasOut = join(root, "relay-alias-out");
  await writeFile(
    aliasSource,
    `let first = 1;\nlet second = 2;\nfunction current() { return first; }\nfunction other() { return second; }\nexport { current, other };\n`
  );
  await writeFile(
    aliasSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "relay.mjs": { imports: ["state.mjs"], statements: [], reexports: { "state.mjs": ["first", "second"] } },
        "logic.mjs": { imports: ["relay.mjs"], statements: [2, 3], aliasImports: true },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", aliasSource, aliasSpec, aliasOut).value.result, "generated");
  assert.equal(cli("validate", aliasSource, aliasSpec, aliasOut).value.result, "proved under assumptions");
  const logicPath = join(aliasOut, "logic.mjs");
  const logic = await readFile(logicPath, "utf8");
  assert.match(logic, /__split_alias_0 as first/);
  await writeFile(logicPath, logic.replace("__split_alias_0 as first", "__split_alias_1 as first"));
  const changed = cli("validate", aliasSource, aliasSpec, aliasOut).value;
  assert.notEqual(changed.result, "proved under assumptions", JSON.stringify(changed));
  assert.notDeepEqual(execute(aliasSource, ["current"]), execute(join(aliasOut, "entry.mjs"), ["current"]));

  const relayPath = join(aliasOut, "relay.mjs");
  const relay = await readFile(relayPath, "utf8");
  assert.match(relay, /export \{ __split_alias_0 \}/);
  await writeFile(relayPath, relay.replace("export { __split_alias_0 }", "export { __split_alias_0 as renamedFirst }"));
  await writeFile(logicPath, logic.replace("__split_alias_0 as first", "renamedFirst as first"));
  const renamed = cli("validate", aliasSource, aliasSpec, aliasOut).value;
  assert.equal(renamed.result, "proved under assumptions", JSON.stringify(renamed));
  assert.deepEqual(
    execute(aliasSource, ["current", "other"]),
    execute(join(aliasOut, "entry.mjs"), ["current", "other"])
  );
  await writeFile(
    relayPath,
    (await readFile(relayPath, "utf8")).replace("__split_alias_0 as renamedFirst", "__split_alias_1 as renamedFirst")
  );
  const wrongRelay = cli("validate", aliasSource, aliasSpec, aliasOut).value;
  assert.notEqual(wrongRelay.result, "proved under assumptions", JSON.stringify(wrongRelay));
  assert.notDeepEqual(execute(aliasSource, ["current"]), execute(join(aliasOut, "entry.mjs"), ["current"]));

  await writeFile(relayPath, relay);
  await writeFile(logicPath, logic);
  const entryPath = join(aliasOut, "entry.mjs");
  const entry = await readFile(entryPath, "utf8");
  assert.match(entry, /export \{ current \}/);
  await writeFile(entryPath, entry.replace("export { current }", "export { other as current }"));
  const wrongPublic = cli("validate", aliasSource, aliasSpec, aliasOut).value;
  assert.equal(wrongPublic.result, "unsafe", JSON.stringify(wrongPublic));
  assert.deepEqual(execute(aliasSource, ["current"]), { events: [], results: [1] });
  assert.deepEqual(execute(entryPath, ["current"]), { events: [], results: [2] });
}

if (runSection(1)) {
  const expressionSource = join(root, "record-expression-source.mjs");
  const expressionSpec = join(root, "record-expression-spec.json");
  const expressionOut = join(root, "record-expression-out");
  await writeFile(
    expressionSource,
    `let seed = 2;\nfunction compute() { return seed + 1; }\nglobalThis.record(compute());\nexport { compute };\n`
  );
  await writeFile(
    expressionSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0] },
        "logic.mjs": { imports: ["state.mjs"], statements: [1, 2] },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", expressionSource, expressionSpec, expressionOut).value.result, "generated");
  const checkedExpression = cli("validate", expressionSource, expressionSpec, expressionOut).value;
  assert.equal(checkedExpression.result, "proved under assumptions", JSON.stringify(checkedExpression));
  assert.deepEqual(execute(expressionSource, ["compute"]), { events: [3], results: [3] });
  assert.deepEqual(execute(join(expressionOut, "entry.mjs"), ["compute"]), execute(expressionSource, ["compute"]));

  const unsupportedArguments = join(root, "record-unsupported-arguments.mjs");
  await writeFile(
    unsupportedArguments,
    `let seed = 2;\nfunction compute(value) { return seed + value; }\nglobalThis.record(compute(9));\nexport { compute };\n`
  );
  assert.equal(
    cli("generate", unsupportedArguments, expressionSpec, join(root, "record-unsupported-out")).value.result,
    "unknown"
  );
  assert.deepEqual(execute(unsupportedArguments, [{ name: "compute", args: [9] }]), { events: [11], results: [11] });
}

if (runSection(0)) {
  const aliasSource = join(root, "renamed-local-source.mjs");
  const aliasSpec = join(root, "renamed-local-spec.json");
  const aliasOut = join(root, "renamed-local-out");
  await writeFile(aliasSource, `let value = 3;\nlet other = 8;\nfunction read() { return value; }\nexport { read };\n`);
  await writeFile(
    aliasSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "logic.mjs": { imports: ["state.mjs"], statements: [2] },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", aliasSource, aliasSpec, aliasOut).value.result, "generated");
  const aliasState = join(aliasOut, "state.mjs");
  await writeFile(aliasState, (await readFile(aliasState, "utf8")) + "export { other };\n");
  const aliasLogic = join(aliasOut, "logic.mjs");
  const originalLogic = await readFile(aliasLogic, "utf8");
  assert.match(originalLogic, /import \{ value \}/);
  const aliasedLogic = originalLogic
    .replace("import { value }", "import { value as localValue }")
    .replace("return value", "return localValue");
  await writeFile(aliasLogic, aliasedLogic);
  const checked = cli("validate", aliasSource, aliasSpec, aliasOut).value;
  assert.equal(checked.result, "proved under assumptions", JSON.stringify(checked));
  assert.match(checked.certificate, /binding_simple_future_calls/);
  assert.deepEqual(execute(aliasSource, ["read"]), execute(join(aliasOut, "entry.mjs"), ["read"]));
  await writeFile(aliasLogic, aliasedLogic.replace("value as localValue", "other as localValue"));
  const redirected = cli("validate", aliasSource, aliasSpec, aliasOut).value;
  assert.equal(redirected.result, "unsafe", JSON.stringify(redirected));
  assert.deepEqual(execute(aliasSource, redirected.calls), { events: [], results: [3] });
  assert.deepEqual(execute(join(aliasOut, "entry.mjs"), redirected.calls), { events: [], results: [8] });

  const generatedAliasSpec = join(root, "generated-renamed-local-spec.json");
  const generatedAliasOut = join(root, "generated-renamed-local-out");
  const generatedSpec = JSON.parse(await readFile(aliasSpec, "utf8"));
  generatedSpec.modules["logic.mjs"].renameLocalImports = true;
  await writeFile(generatedAliasSpec, JSON.stringify(generatedSpec));
  assert.equal(cli("generate", aliasSource, generatedAliasSpec, generatedAliasOut).value.result, "generated");
  const generatedLogic = await readFile(join(generatedAliasOut, "logic.mjs"), "utf8");
  assert.match(generatedLogic, /import \{ value as __split_local_\d+ \}/);
  assert.match(generatedLogic, /return __split_local_\d+/);
  const generated = cli("validate", aliasSource, generatedAliasSpec, generatedAliasOut).value;
  assert.equal(generated.result, "proved under assumptions", JSON.stringify(generated));
  assert.deepEqual(execute(aliasSource, ["read"]), execute(join(generatedAliasOut, "entry.mjs"), ["read"]));

  const expressionSource = join(root, "unsupported-renamed-local-source.mjs");
  const expressionSpec = join(root, "unsupported-renamed-local-spec.json");
  await writeFile(
    expressionSource,
    `let value = 3;\nfunction read(arg) { let local = arg; return value + local; }\nexport { read };\n`
  );
  generatedSpec.modules["state.mjs"].statements = [0];
  generatedSpec.modules["logic.mjs"].statements = [1];
  await writeFile(expressionSpec, JSON.stringify(generatedSpec));
  const unsupported = cli("generate", expressionSource, expressionSpec, join(root, "unsupported-renamed-local-out"));
  assert.equal(unsupported.value.result, "unknown");
  assert.match(unsupported.value.reason, /local import renaming supports only pure returning expressions/);

  const observedSource = join(root, "observed-renamed-local-source.mjs");
  const observedSpec = join(root, "observed-renamed-local-spec.json");
  const observedOut = join(root, "observed-renamed-local-out");
  await writeFile(
    observedSource,
    `let value = 3;\nglobalThis.record(value);\nfunction read() { return value; }\nexport { read };\n`
  );
  generatedSpec.modules["logic.mjs"].statements = [1, 2];
  await writeFile(observedSpec, JSON.stringify(generatedSpec));
  assert.equal(cli("generate", observedSource, observedSpec, observedOut).value.result, "generated");
  const observed = cli("validate", observedSource, observedSpec, observedOut).value;
  assert.equal(observed.result, "proved under assumptions", JSON.stringify(observed));
  assert.deepEqual(execute(observedSource, ["read"]), { events: [3], results: [3] });
  assert.deepEqual(execute(join(observedOut, "entry.mjs"), ["read"]), { events: [3], results: [3] });

  const redirectedInitSource = join(root, "redirected-init-local-source.mjs");
  const redirectedInitSpec = join(root, "redirected-init-local-spec.json");
  const redirectedInitOut = join(root, "redirected-init-local-out");
  await writeFile(
    redirectedInitSource,
    `let value = 3;\nlet other = 8;\nglobalThis.record(value);\nfunction read() { return value; }\nexport { read };\n`
  );
  generatedSpec.modules["state.mjs"].statements = [0, 1];
  generatedSpec.modules["logic.mjs"].statements = [2, 3];
  await writeFile(redirectedInitSpec, JSON.stringify(generatedSpec));
  assert.equal(cli("generate", redirectedInitSource, redirectedInitSpec, redirectedInitOut).value.result, "generated");
  const redirectedInitState = join(redirectedInitOut, "state.mjs");
  await writeFile(redirectedInitState, (await readFile(redirectedInitState, "utf8")) + "export { other };\n");
  const redirectedInitLogic = join(redirectedInitOut, "logic.mjs");
  const initLogic = await readFile(redirectedInitLogic, "utf8");
  assert.match(initLogic, /value as __split_local_\d+/);
  await writeFile(redirectedInitLogic, initLogic.replace("value as __split_local_0", "other as __split_local_0"));
  const initDifference = cli("validate", redirectedInitSource, redirectedInitSpec, redirectedInitOut).value;
  assert.equal(initDifference.result, "unsafe", JSON.stringify(initDifference));
  assert.match(initDifference.certificate, /binding_witness_ready/);
  assert.deepEqual(execute(redirectedInitSource, []), { events: [3], results: [] });
  assert.deepEqual(execute(join(redirectedInitOut, "entry.mjs"), []), { events: [8], results: [] });

  const calledSource = join(root, "called-renamed-local-source.mjs");
  const calledSpec = join(root, "called-renamed-local-spec.json");
  const calledOut = join(root, "called-renamed-local-out");
  await writeFile(calledSource, `let value = 3;\nfunction read() { return value; }\nread();\nexport { read };\n`);
  generatedSpec.modules["state.mjs"].statements = [0, 1];
  generatedSpec.modules["logic.mjs"].statements = [2];
  await writeFile(calledSpec, JSON.stringify(generatedSpec));
  assert.equal(cli("generate", calledSource, calledSpec, calledOut).value.result, "generated");
  const called = cli("validate", calledSource, calledSpec, calledOut).value;
  assert.equal(called.result, "proved under assumptions", JSON.stringify(called));
  assert.deepEqual(execute(calledSource, ["read"]), execute(join(calledOut, "entry.mjs"), ["read"]));

  const updateSource = join(root, "update-renamed-local-source.mjs");
  const updateSpec = join(root, "update-renamed-local-spec.json");
  const updateOut = join(root, "update-renamed-local-out");
  await writeFile(
    updateSource,
    `let step = 2;\nlet total = 1;\nfunction bump() { total += step; }\nfunction read() { return total; }\nexport { bump, read };\n`
  );
  generatedSpec.modules["state.mjs"].statements = [0];
  generatedSpec.modules["logic.mjs"].statements = [1, 2, 3];
  await writeFile(updateSpec, JSON.stringify(generatedSpec));
  assert.equal(cli("generate", updateSource, updateSpec, updateOut).value.result, "generated");
  const update = cli("validate", updateSource, updateSpec, updateOut).value;
  assert.equal(update.result, "proved under assumptions", JSON.stringify(update));
  const updateCalls = ["read", "bump", "read", "bump", "read"];
  assert.deepEqual(execute(updateSource, updateCalls), { events: [], results: [1, null, 3, null, 5] });
  assert.deepEqual(execute(join(updateOut, "entry.mjs"), updateCalls), execute(updateSource, updateCalls));

  const importedWriteSpec = join(root, "imported-write-alias-spec.json");
  const importedWriteOut = join(root, "imported-write-alias-out");
  generatedSpec.modules["state.mjs"].statements = [0, 1];
  generatedSpec.modules["logic.mjs"].statements = [2, 3];
  await writeFile(importedWriteSpec, JSON.stringify(generatedSpec));
  await mkdir(importedWriteOut);
  await writeFile(join(importedWriteOut, "state.mjs"), `export let step = 2;\nexport let total = 1;\n`);
  await writeFile(
    join(importedWriteOut, "logic.mjs"),
    `import { step as localStep, total } from "./state.mjs";\nexport function bump() { total += localStep; }\nexport function read() { return total; }\n`
  );
  await writeFile(
    join(importedWriteOut, "entry.mjs"),
    `import "./logic.mjs";\nexport { bump, read } from "./logic.mjs";\n`
  );
  assert.notEqual(
    cli("validate", updateSource, importedWriteSpec, importedWriteOut).value.result,
    "proved under assumptions"
  );
  const importedWriteScript = `const m = await import(${JSON.stringify(pathToFileURL(join(importedWriteOut, "entry.mjs")).href)}); try { m.bump(); } catch (e) { console.log(e.name); }`;
  const importedWrite = spawnSync(process.execPath, ["--input-type=module", "-e", importedWriteScript], {
    encoding: "utf8",
  });
  assert.equal(importedWrite.status, 0, importedWrite.stderr);
  assert.equal(importedWrite.stdout.trim(), "TypeError");

  const nonnumericSource = join(root, "nonnumeric-alias-source.mjs");
  const nonnumericSpec = join(root, "nonnumeric-alias-spec.json");
  const nonnumericOut = join(root, "nonnumeric-alias-out");
  await writeFile(
    nonnumericSource,
    `let value = 3;\nfunction other() { return value; }\nfunction read() { return other; }\nexport { read };\n`
  );
  generatedSpec.modules["state.mjs"].statements = [0, 1];
  generatedSpec.modules["logic.mjs"].statements = [2];
  await writeFile(nonnumericSpec, JSON.stringify(generatedSpec));
  assert.equal(cli("generate", nonnumericSource, nonnumericSpec, nonnumericOut).value.result, "generated");
  assert.notEqual(
    cli("validate", nonnumericSource, nonnumericSpec, nonnumericOut).value.result,
    "proved under assumptions"
  );
  const nonnumericScript = `const m = await import(${JSON.stringify(pathToFileURL(join(nonnumericOut, "entry.mjs")).href)}); console.log(typeof m.read());`;
  const nonnumeric = spawnSync(process.execPath, ["--input-type=module", "-e", nonnumericScript], { encoding: "utf8" });
  assert.equal(nonnumeric.status, 0, nonnumeric.stderr);
  assert.equal(nonnumeric.stdout.trim(), "function");
}

if (runSection(2)) {
  const generatedSpec = {
    kind: "bindings",
    entry: "entry.mjs",
    modules: {
      "state.mjs": { imports: [], statements: [] },
      "logic.mjs": { imports: ["state.mjs"], statements: [], renameLocalImports: true },
      "entry.mjs": { imports: ["logic.mjs"], statements: [] },
    },
  };
  const numericSource = join(root, "numeric-renamed-local-source.mjs");
  const numericSpec = join(root, "numeric-renamed-local-spec.json");
  const numericOut = join(root, "numeric-renamed-local-out");
  await writeFile(
    numericSource,
    `let first = 3;\nlet second = 4;\nfunction compute() { return first * 2 + second; }\nexport { compute };\n`
  );
  generatedSpec.modules["state.mjs"].statements = [0, 1];
  generatedSpec.modules["logic.mjs"].statements = [2];
  await writeFile(numericSpec, JSON.stringify(generatedSpec));
  assert.equal(cli("generate", numericSource, numericSpec, numericOut).value.result, "generated");
  const numeric = cli("validate", numericSource, numericSpec, numericOut).value;
  assert.equal(numeric.result, "proved under assumptions", JSON.stringify(numeric));
  assert.match(numeric.certificate, /binding_simple_future_calls/);
  assert.deepEqual(execute(numericSource, [{ name: "compute", args: [] }]), { events: [], results: [10] });
  assert.deepEqual(execute(join(numericOut, "entry.mjs"), [{ name: "compute", args: [] }]), {
    events: [],
    results: [10],
  });
  const numericLogic = join(numericOut, "logic.mjs");
  const numericText = await readFile(numericLogic, "utf8");
  assert.match(numericText, /first as __split_local_0/);
  await writeFile(numericLogic, numericText.replace("first as __split_local_0", "second as __split_local_0"));
  const numericRedirect = cli("validate", numericSource, numericSpec, numericOut).value;
  assert.equal(numericRedirect.result, "unsafe", JSON.stringify(numericRedirect));
  assert.deepEqual(execute(numericSource, numericRedirect.calls), { events: [], results: [10] });
  assert.deepEqual(execute(join(numericOut, "entry.mjs"), numericRedirect.calls), { events: [], results: [12] });

  const parameterSource = join(root, "parameter-alias-source.mjs");
  const parameterSpec = join(root, "parameter-alias-spec.json");
  const parameterOut = join(root, "parameter-alias-out");
  await writeFile(
    parameterSource,
    `let value = 3;\nlet shadow = 8;\nfunction compute(shadow) { return value + shadow; }\nexport { compute };\n`
  );
  generatedSpec.modules["state.mjs"].statements = [0, 1];
  generatedSpec.modules["logic.mjs"].statements = [2];
  await writeFile(parameterSpec, JSON.stringify(generatedSpec));
  assert.equal(cli("generate", parameterSource, parameterSpec, parameterOut).value.result, "generated");
  const parameterState = join(parameterOut, "state.mjs");
  await writeFile(parameterState, (await readFile(parameterState, "utf8")) + "export { shadow };\n");
  const parameter = cli("validate", parameterSource, parameterSpec, parameterOut).value;
  assert.equal(parameter.result, "proved under assumptions", JSON.stringify(parameter));
  const parameterCalls = [
    { name: "compute", args: [0] },
    { name: "compute", args: [2] },
  ];
  assert.deepEqual(execute(parameterSource, parameterCalls), { events: [], results: [3, 5] });
  assert.deepEqual(execute(join(parameterOut, "entry.mjs"), parameterCalls), { events: [], results: [3, 5] });
  const parameterLogic = join(parameterOut, "logic.mjs");
  const parameterText = await readFile(parameterLogic, "utf8");
  assert.match(parameterText, /value as __split_local_0/);
  await writeFile(parameterLogic, parameterText.replace("value as __split_local_0", "shadow as __split_local_0"));
  const shadowRedirect = cli("validate", parameterSource, parameterSpec, parameterOut).value;
  assert.equal(shadowRedirect.result, "unsafe", JSON.stringify(shadowRedirect));
  assert.deepEqual(execute(parameterSource, shadowRedirect.calls), { events: [], results: [3] });
  assert.deepEqual(execute(join(parameterOut, "entry.mjs"), shadowRedirect.calls), { events: [], results: [8] });

  const collisionSource = join(root, "alias-name-collision-source.mjs");
  const collisionSpec = join(root, "alias-name-collision-spec.json");
  const collisionOut = join(root, "alias-name-collision-out");
  await writeFile(
    collisionSource,
    `let value = 3;\nfunction compute(__split_local_0) { return value + __split_local_0; }\nexport { compute };\n`
  );
  generatedSpec.modules["state.mjs"].statements = [0];
  generatedSpec.modules["logic.mjs"].statements = [1];
  await writeFile(collisionSpec, JSON.stringify(generatedSpec));
  assert.equal(cli("generate", collisionSource, collisionSpec, collisionOut).value.result, "generated");
  assert.match(await readFile(join(collisionOut, "logic.mjs"), "utf8"), /value as __split_local__0/);
  const collision = cli("validate", collisionSource, collisionSpec, collisionOut).value;
  assert.equal(collision.result, "proved under assumptions", JSON.stringify(collision));
  assert.deepEqual(execute(join(collisionOut, "entry.mjs"), [{ name: "compute", args: [2] }]), {
    events: [],
    results: [5],
  });

  const logicalSource = join(root, "logical-alias-source.mjs");
  const logicalSpec = join(root, "logical-alias-spec.json");
  const logicalOut = join(root, "logical-alias-out");
  await writeFile(
    logicalSource,
    `let value = 3;\nfunction compute(p) { return (value || p) ? value + p : 0; }\nexport { compute };\n`
  );
  await writeFile(logicalSpec, JSON.stringify(generatedSpec));
  assert.equal(cli("generate", logicalSource, logicalSpec, logicalOut).value.result, "generated");
  const logical = cli("validate", logicalSource, logicalSpec, logicalOut).value;
  assert.equal(logical.result, "proved under assumptions", JSON.stringify(logical));
  const logicalCalls = [
    { name: "compute", args: [0] },
    { name: "compute", args: [2] },
  ];
  assert.deepEqual(execute(logicalSource, logicalCalls), { events: [], results: [3, 5] });
  assert.deepEqual(execute(join(logicalOut, "entry.mjs"), logicalCalls), { events: [], results: [3, 5] });
}

if (runSection(0)) {
  const calleeSource = join(root, "redirected-callee-source.mjs");
  const calleeSpec = join(root, "redirected-callee-spec.json");
  const calleeOut = join(root, "redirected-callee-out");
  await writeFile(
    calleeSource,
    `function first(value) { return value + 1; }\nfunction second(value) { return value + 2; }\nfunction call(value) { return first(value); }\nexport { call };\n`
  );
  await writeFile(
    calleeSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "relay.mjs": { imports: ["state.mjs"], statements: [], reexports: { "state.mjs": ["first", "second"] } },
        "logic.mjs": { imports: ["relay.mjs"], statements: [2] },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", calleeSource, calleeSpec, calleeOut).value.result, "generated");
  assert.equal(cli("validate", calleeSource, calleeSpec, calleeOut).value.result, "proved under assumptions");
  const relayPath = join(calleeOut, "relay.mjs");
  const relayText = await readFile(relayPath, "utf8");
  assert.match(relayText, /export \{ first \}/);
  await writeFile(relayPath, relayText.replace("export { first }", "export { second as first }"));
  const redirected = cli("validate", calleeSource, calleeSpec, calleeOut).value;
  assert.equal(redirected.result, "unsafe", JSON.stringify(redirected));
  assert.deepEqual(redirected.calls, [{ name: "call", args: [0] }]);
  assert.deepEqual(execute(calleeSource, redirected.calls), { events: [], results: [1] });
  assert.deepEqual(execute(join(calleeOut, "entry.mjs"), redirected.calls), { events: [], results: [2] });

  const largeArgumentSource = join(root, "redirected-large-argument-source.mjs");
  const largeArgumentOut = join(root, "redirected-large-argument-out");
  await writeFile(
    largeArgumentSource,
    `function first(value) { return value; }\nfunction second(value) { return value * 2; }\nfunction call(value) { return first(value + 5000000000000000); }\nexport { call };\n`
  );
  assert.equal(cli("generate", largeArgumentSource, calleeSpec, largeArgumentOut).value.result, "generated");
  const largeRelayPath = join(largeArgumentOut, "relay.mjs");
  const largeRelay = await readFile(largeRelayPath, "utf8");
  await writeFile(largeRelayPath, largeRelay.replace("export { first }", "export { second as first }"));
  const calls = [{ name: "call", args: [0] }];
  assert.deepEqual(execute(largeArgumentSource, calls), { events: [], results: [5000000000000000] });
  assert.deepEqual(execute(join(largeArgumentOut, "entry.mjs"), calls), { events: [], results: [10000000000000000] });
  const largeRedirect = cli("validate", largeArgumentSource, calleeSpec, largeArgumentOut).value;
  assert.equal(largeRedirect.result, "unknown", JSON.stringify(largeRedirect));
  assert.match(largeRedirect.detail, /numeric bounds/);

  const largeReturnSource = join(root, "redirected-large-return-source.mjs");
  const largeReturnOut = join(root, "redirected-large-return-out");
  await writeFile(
    largeReturnSource,
    `function first(value) { return 1; }\nfunction second(value) { return 5000000000000000; }\nfunction call(value) { return (first(value) + first(value)) - first(value); }\nexport { call };\n`
  );
  assert.equal(cli("generate", largeReturnSource, calleeSpec, largeReturnOut).value.result, "generated");
  const largeReturnRelay = join(largeReturnOut, "relay.mjs");
  await writeFile(
    largeReturnRelay,
    (await readFile(largeReturnRelay, "utf8")).replace("export { first }", "export { second as first }")
  );
  assert.deepEqual(execute(largeReturnSource, calls), { events: [], results: [1] });
  assert.deepEqual(execute(join(largeReturnOut, "entry.mjs"), calls), { events: [], results: [5000000000000000] });
  const largeReturn = cli("validate", largeReturnSource, calleeSpec, largeReturnOut).value;
  assert.equal(largeReturn.result, "unknown", JSON.stringify(largeReturn));
  assert.match(largeReturn.detail, /numeric bounds/);
}

if (runSection(1)) {
  const largeSource = join(root, "unsafe-number-source.mjs");
  const largeSpec = join(root, "unsafe-number-spec.json");
  const largeOut = join(root, "unsafe-number-out");
  await writeFile(
    largeSource,
    `let seed = 9007199254740991;\nfunction compute() { return (seed + 1) - seed; }\nglobalThis.record(compute());\nexport { compute };\n`
  );
  await writeFile(
    largeSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0] },
        "logic.mjs": { imports: ["state.mjs"], statements: [1, 2] },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", largeSource, largeSpec, largeOut).value.result, "generated");
  const logicPath = join(largeOut, "logic.mjs");
  const logic = await readFile(logicPath, "utf8");
  assert.match(logic, /seed \+ 1/);
  await writeFile(logicPath, logic.replace("seed + 1", "seed + 3"));
  assert.deepEqual(execute(largeSource, []), { events: [1], results: [] });
  assert.deepEqual(execute(join(largeOut, "entry.mjs"), []), { events: [3], results: [] });
  const checked = cli("validate", largeSource, largeSpec, largeOut).value;
  assert.equal(checked.result, "unknown", JSON.stringify(checked));
  assert.match(checked.detail, /numeric bounds/);
}

if (runSection(1)) {
  const source = join(root, "unsafe-mutating-operand-source.mjs");
  const spec = join(root, "unsafe-mutating-operand-spec.json");
  const out = join(root, "unsafe-mutating-operand-out");
  await writeFile(
    source,
    `function compute() { let x = 1; function bump() { x = 5000000000000000; return x; } return (bump() + x) - x; }\nglobalThis.record(compute());\nexport { compute };\n`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: { "entry.mjs": { imports: [], statements: [0, 1] } },
    })
  );
  assert.equal(cli("generate", source, spec, out).value.result, "generated");
  const output = join(out, "entry.mjs");
  const text = await readFile(output, "utf8");
  assert.match(text, /bump\(\) \+ x/);
  await writeFile(output, text.replace("bump() + x", "bump() + x + 2"));
  assert.deepEqual(execute(source, []), { events: [5000000000000000], results: [] });
  assert.deepEqual(execute(output, []), { events: [5000000000000002], results: [] });
  const checked = cli("validate", source, spec, out).value;
  assert.equal(checked.result, "unknown", JSON.stringify(checked));
  assert.match(checked.detail, /numeric bounds/);
}
