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
  console.error(`[bindings_test] ${args[0]} ${args[1]}`);
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

if (runSection(5)) {
  const source = join(root, "finally-return-source.mjs");
  const spec = join(root, "finally-return-spec.json");
  const out = join(root, "finally-return-out");
  await writeFile(
    source,
    `let original = 3;\nlet alternate = 5;\nfunction compute() { try { throw original; } finally { return alternate; } }\nexport { compute };\n`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "logic.mjs": { imports: ["state.mjs"], statements: [2], renameLocalImports: true },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).value.result, "generated");
  const logic = join(out, "logic.mjs");
  const text = await readFile(logic, "utf8");
  const unchanged = cli("validate", source, spec, out).value;
  assert.equal(unchanged.result, "proved under assumptions", JSON.stringify(unchanged));
  const calls = [{ name: "compute", args: [] }];
  assert.deepEqual(execute(source, calls), { events: [], results: [5] });
  assert.deepEqual(execute(join(out, "entry.mjs"), calls), { events: [], results: [5] });
  assert.match(text, /alternate as __split_local_1/);
  await writeFile(logic, text.replace("alternate as __split_local_1", "original as __split_local_1"));
  const redirected = cli("validate", source, spec, out).value;
  assert.equal(redirected.result, "unsafe", JSON.stringify(redirected));
  assert.deepEqual(execute(join(out, "entry.mjs"), redirected.calls), { events: [], results: [3] });

  const returningSource = join(root, "finally-return-pending-return.mjs");
  const returningOut = join(root, "finally-return-pending-return-out");
  await writeFile(
    returningSource,
    `let original = 3;\nlet alternate = 5;\nfunction compute() { try { return original; } finally { return alternate; } }\nexport { compute };\n`
  );
  assert.equal(cli("generate", returningSource, spec, returningOut).value.result, "generated");
  const returning = cli("validate", returningSource, spec, returningOut).value;
  assert.equal(returning.result, "proved under assumptions", JSON.stringify(returning));
  assert.deepEqual(execute(returningSource, calls), { events: [], results: [5] });
  assert.deepEqual(execute(join(returningOut, "entry.mjs"), calls), { events: [], results: [5] });

  const nonterminal = join(root, "finally-return-nonterminal.mjs");
  await writeFile(
    nonterminal,
    `let original = 3;\nlet alternate = 5;\nfunction compute() { try { throw original; } finally { return alternate; } return original; }\nexport { compute };\n`
  );
  assert.equal(
    cli("generate", nonterminal, spec, join(root, "finally-return-nonterminal-out")).value.result,
    "unknown"
  );
}

if (runSection(4)) {
  const source = join(root, "finally-throw-source.mjs");
  const spec = join(root, "finally-throw-spec.json");
  const out = join(root, "finally-throw-out");
  await writeFile(
    source,
    `let original = 3;\nlet alternate = 5;\nfunction compute() { try { return original; } finally { throw alternate; } }\nexport { compute };\n`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "logic.mjs": { imports: ["state.mjs"], statements: [2], renameLocalImports: true },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).value.result, "generated");
  const logic = join(out, "logic.mjs");
  const text = await readFile(logic, "utf8");
  const unchanged = cli("validate", source, spec, out).value;
  assert.equal(unchanged.result, "proved under assumptions", JSON.stringify(unchanged));
  assert.match(text, /alternate as __split_local_1/);
  await writeFile(logic, text.replace("alternate as __split_local_1", "original as __split_local_1"));
  const redirected = cli("validate", source, spec, out).value;
  assert.equal(redirected.result, "unsafe", JSON.stringify(redirected));
  const thrown = (path) => {
    const code = `const m = await import(${JSON.stringify(pathToFileURL(path).href)}); try { m.compute(); } catch (value) { console.log(JSON.stringify(value)); }`;
    const child = spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8" });
    assert.equal(child.status, 0, child.stderr);
    return JSON.parse(child.stdout);
  };
  assert.equal(thrown(source), 5);
  assert.equal(thrown(join(out, "entry.mjs")), 3);

  const priorThrowSource = join(root, "finally-prior-throw-source.mjs");
  const priorThrowOut = join(root, "finally-prior-throw-out");
  await writeFile(
    priorThrowSource,
    `let original = 3;\nlet alternate = 5;\nfunction compute() { try { throw original; } finally { throw alternate; } }\nexport { compute };\n`
  );
  assert.equal(cli("generate", priorThrowSource, spec, priorThrowOut).value.result, "generated");
  const priorThrowResult = cli("validate", priorThrowSource, spec, priorThrowOut).value;
  assert.equal(priorThrowResult.result, "proved under assumptions", JSON.stringify(priorThrowResult));
  assert.equal(thrown(priorThrowSource), 5);
  assert.equal(thrown(join(priorThrowOut, "entry.mjs")), 5);
}

if (runSection(3)) {
  const source = join(root, "conditional-throw-source.mjs");
  const spec = join(root, "conditional-throw-spec.json");
  const out = join(root, "conditional-throw-out");
  await writeFile(
    source,
    `let original = 3;\nlet alternate = 5;\nfunction compute(p) { if (p < original) { throw original; } else { return p + original; } }\nexport { compute };\n`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "logic.mjs": { imports: ["state.mjs"], statements: [2], renameLocalImports: true },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).value.result, "generated");
  const state = join(out, "state.mjs");
  await writeFile(state, (await readFile(state, "utf8")) + "export { alternate };\n");
  const logic = join(out, "logic.mjs");
  const text = await readFile(logic, "utf8");
  const original = cli("validate", source, spec, out).value;
  assert.equal(original.result, "proved under assumptions", JSON.stringify(original));
  const observe = (path, argument) => {
    const script = `const m = await import(${JSON.stringify(pathToFileURL(path).href)}); try { console.log(JSON.stringify({value: m.compute(${argument})})); } catch (value) { console.log(JSON.stringify({thrown: value})); }`;
    const child = spawnSync(process.execPath, ["--input-type=module", "-e", script], { encoding: "utf8" });
    assert.equal(child.status, 0, child.stderr);
    return JSON.parse(child.stdout);
  };
  for (const argument of [0, 4]) assert.deepEqual(observe(source, argument), observe(join(out, "entry.mjs"), argument));
  await writeFile(logic, text.replace("original as __split_local_0", "alternate as __split_local_0"));
  const redirected = cli("validate", source, spec, out).value;
  assert.equal(redirected.result, "unsafe", JSON.stringify(redirected));
  assert.deepEqual(observe(source, 0), { thrown: 3 });
  assert.deepEqual(observe(join(out, "entry.mjs"), 0), { thrown: 5 });
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

if (runSection(2)) {
  const source = join(root, "terminal-catch-source.mjs");
  const spec = join(root, "terminal-catch-spec.json");
  const out = join(root, "terminal-catch-out");
  await writeFile(
    source,
    `let original = 3;\nlet alternate = 5;\nfunction compute() { try { throw original; } catch (alternate) { return alternate + original; } }\nexport { compute };\n`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "logic.mjs": { imports: ["state.mjs"], statements: [2], renameLocalImports: true },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).value.result, "generated");
  const state = join(out, "state.mjs");
  await writeFile(state, (await readFile(state, "utf8")) + "export { alternate };\n");
  const logic = join(out, "logic.mjs");
  const text = await readFile(logic, "utf8");
  assert.match(text, /catch \(alternate\).*alternate \+ __split_local_0/);
  const unchanged = cli("validate", source, spec, out).value;
  assert.equal(unchanged.result, "proved under assumptions", JSON.stringify(unchanged));
  assert.deepEqual(
    execute(source, [{ name: "compute", args: [] }]),
    execute(join(out, "entry.mjs"), [{ name: "compute", args: [] }])
  );
  await writeFile(logic, text.replace("original as __split_local_0", "alternate as __split_local_0"));
  const redirected = cli("validate", source, spec, out).value;
  assert.equal(redirected.result, "unsafe", JSON.stringify(redirected));
  assert.deepEqual(execute(source, redirected.calls), { events: [], results: [6] });
  assert.deepEqual(execute(join(out, "entry.mjs"), redirected.calls), { events: [], results: [10] });

  const throwingSource = join(root, "terminal-throw-source.mjs");
  const throwingOut = join(root, "terminal-throw-out");
  await writeFile(
    throwingSource,
    `let original = 3;\nlet alternate = 5;\nfunction compute() { throw original; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", throwingSource, spec, throwingOut).value.result, "generated");
  const throwingState = join(throwingOut, "state.mjs");
  await writeFile(throwingState, (await readFile(throwingState, "utf8")) + "export { alternate };\n");
  const throwingLogic = join(throwingOut, "logic.mjs");
  const throwingText = await readFile(throwingLogic, "utf8");
  const throwingUnchanged = cli("validate", throwingSource, spec, throwingOut).value;
  assert.equal(throwingUnchanged.result, "proved under assumptions", JSON.stringify(throwingUnchanged));
  await writeFile(throwingLogic, throwingText.replace("original as __split_local_0", "alternate as __split_local_0"));
  const throwingRedirected = cli("validate", throwingSource, spec, throwingOut).value;
  assert.equal(throwingRedirected.result, "unsafe", JSON.stringify(throwingRedirected));
  const thrown = (path) => {
    const code = `const m = await import(${JSON.stringify(pathToFileURL(path).href)}); try { m.compute(); } catch (value) { console.log(JSON.stringify(value)); }`;
    const child = spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8" });
    assert.equal(child.status, 0, child.stderr);
    return JSON.parse(child.stdout);
  };
  assert.equal(thrown(throwingSource), 3);
  assert.equal(thrown(join(throwingOut, "entry.mjs")), 5);
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

if (runSection(5)) {
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
    `let value = 3;\nfunction read(arg) { let local = arg; value += local; return local; }\nexport { read };\n`
  );
  generatedSpec.modules["state.mjs"].statements = [0];
  generatedSpec.modules["logic.mjs"].statements = [1];
  await writeFile(expressionSpec, JSON.stringify(generatedSpec));
  const unsupported = cli("generate", expressionSource, expressionSpec, join(root, "unsupported-renamed-local-out"));
  assert.equal(unsupported.value.result, "unknown");
  assert.match(unsupported.value.reason, /cannot write an imported (?:or immutable )?binding/);
  await writeFile(
    expressionSource,
    `let value = 3;\nfunction read(arg) { let local = arg; value++; return local; }\nexport { read };\n`
  );
  const importedUpdate = cli("generate", expressionSource, expressionSpec, join(root, "imported-update-out"));
  assert.equal(importedUpdate.value.result, "unknown");
  assert.match(importedUpdate.value.reason, /cannot write an imported (?:or immutable )?binding/);

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

if (runSection(4)) {
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

  const logicalValueSource = join(root, "logical-value-alias-source.mjs");
  const logicalValueSpec = join(root, "logical-value-alias-spec.json");
  const logicalValueOut = join(root, "logical-value-alias-out");
  await writeFile(
    logicalValueSource,
    `let value = 0;\nlet other = 3;\nfunction compute(p) { return value || p; }\nexport { compute };\n`
  );
  generatedSpec.modules["state.mjs"].statements = [0, 1];
  generatedSpec.modules["logic.mjs"].statements = [2];
  await writeFile(logicalValueSpec, JSON.stringify(generatedSpec));
  assert.equal(cli("generate", logicalValueSource, logicalValueSpec, logicalValueOut).value.result, "generated");
  const logicalValueState = join(logicalValueOut, "state.mjs");
  await writeFile(logicalValueState, (await readFile(logicalValueState, "utf8")) + "export { other };\n");
  const logicalValue = cli("validate", logicalValueSource, logicalValueSpec, logicalValueOut).value;
  assert.equal(logicalValue.result, "proved under assumptions", JSON.stringify(logicalValue));
  assert.deepEqual(execute(logicalValueSource, logicalCalls), { events: [], results: [0, 2] });
  assert.deepEqual(execute(join(logicalValueOut, "entry.mjs"), logicalCalls), { events: [], results: [0, 2] });
  const logicalValueLogic = join(logicalValueOut, "logic.mjs");
  const logicalValueText = await readFile(logicalValueLogic, "utf8");
  assert.match(logicalValueText, /value as __split_local_0/);
  await writeFile(logicalValueLogic, logicalValueText.replace("value as __split_local_0", "other as __split_local_0"));
  const logicalValueRedirect = cli("validate", logicalValueSource, logicalValueSpec, logicalValueOut).value;
  assert.equal(logicalValueRedirect.result, "unsafe", JSON.stringify(logicalValueRedirect));
  assert.deepEqual(execute(logicalValueSource, logicalValueRedirect.calls), { events: [], results: [0] });
  assert.deepEqual(execute(join(logicalValueOut, "entry.mjs"), logicalValueRedirect.calls), {
    events: [],
    results: [3],
  });
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

if (runSection(3)) {
  const constSource = join(root, "const-alias-source.mjs");
  const constSpec = join(root, "const-alias-spec.json");
  const constOut = join(root, "const-alias-out");
  await writeFile(
    constSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { const __split_local_0 = p + value; return __split_local_0 + value; }\nexport { compute };\n`
  );
  await writeFile(
    constSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "logic.mjs": { imports: ["state.mjs"], statements: [2], renameLocalImports: true },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", constSource, constSpec, constOut).value.result, "generated");
  const constState = join(constOut, "state.mjs");
  await writeFile(constState, (await readFile(constState, "utf8")) + "export { other };\n");
  const constLogic = join(constOut, "logic.mjs");
  const constText = await readFile(constLogic, "utf8");
  assert.match(constText, /value as __split_local__0/);
  const constResult = cli("validate", constSource, constSpec, constOut).value;
  assert.equal(constResult.result, "proved under assumptions", JSON.stringify(constResult));
  const calls = [
    { name: "compute", args: [0] },
    { name: "compute", args: [2] },
  ];
  assert.deepEqual(execute(constSource, calls), { events: [], results: [6, 8] });
  assert.deepEqual(execute(join(constOut, "entry.mjs"), calls), { events: [], results: [6, 8] });
  await writeFile(constLogic, constText.replace("value as __split_local__0", "other as __split_local__0"));
  const redirected = cli("validate", constSource, constSpec, constOut).value;
  assert.equal(redirected.result, "unsafe", JSON.stringify(redirected));
  assert.deepEqual(execute(constSource, redirected.calls), { events: [], results: [6] });
  assert.deepEqual(execute(join(constOut, "entry.mjs"), redirected.calls), { events: [], results: [10] });

  const letSource = join(root, "let-alias-source.mjs");
  const letOut = join(root, "let-alias-out");
  await writeFile(
    letSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let offset = p + value; return offset + value; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", letSource, constSpec, letOut).value.result, "generated");
  const letResult = cli("validate", letSource, constSpec, letOut).value;
  assert.equal(letResult.result, "proved under assumptions", JSON.stringify(letResult));
  assert.deepEqual(execute(letSource, calls), { events: [], results: [6, 8] });
  assert.deepEqual(execute(join(letOut, "entry.mjs"), calls), { events: [], results: [6, 8] });

  const writeSource = join(root, "local-write-alias-source.mjs");
  const writeOut = join(root, "local-write-alias-out");
  await writeFile(
    writeSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; local += value; return local; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", writeSource, constSpec, writeOut).value.result, "generated");
  const writeResult = cli("validate", writeSource, constSpec, writeOut).value;
  assert.equal(writeResult.result, "proved under assumptions", JSON.stringify(writeResult));
  assert.deepEqual(execute(writeSource, calls), { events: [], results: [3, 5] });
  assert.deepEqual(execute(join(writeOut, "entry.mjs"), calls), { events: [], results: [3, 5] });
  const writeLogic = join(writeOut, "logic.mjs");
  const writeText = await readFile(writeLogic, "utf8");
  assert.match(writeText, /local \+= __split_local_0/);
  await writeFile(writeLogic, writeText.replace("local += __split_local_0", "__split_local_0 += local"));
  const importedWrite = cli("validate", writeSource, constSpec, writeOut).value;
  assert.notEqual(importedWrite.result, "proved under assumptions", JSON.stringify(importedWrite));
  const writeScript = `import(${JSON.stringify(pathToFileURL(join(writeOut, "entry.mjs")).href)}).then(m => { try { m.compute(1); } catch (error) { console.log(error.name); } });`;
  const writeNode = spawnSync(process.execPath, ["--input-type=module", "-e", writeScript], { encoding: "utf8" });
  assert.equal(writeNode.status, 0, writeNode.stderr);
  assert.equal(writeNode.stdout.trim(), "TypeError");

  const branchSource = join(root, "branch-alias-source.mjs");
  const branchOut = join(root, "branch-alias-out");
  await writeFile(
    branchSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; if (value > p) { local += value; } return local; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", branchSource, constSpec, branchOut).value.result, "generated");
  const branchState = join(branchOut, "state.mjs");
  await writeFile(branchState, (await readFile(branchState, "utf8")) + "export { other };\n");
  const branchLogic = join(branchOut, "logic.mjs");
  const branchText = await readFile(branchLogic, "utf8");
  const branchResult = cli("validate", branchSource, constSpec, branchOut).value;
  assert.equal(branchResult.result, "proved under assumptions", JSON.stringify(branchResult));
  const branchCalls = [
    { name: "compute", args: [0] },
    { name: "compute", args: [4] },
  ];
  assert.deepEqual(execute(branchSource, branchCalls), { events: [], results: [3, 4] });
  assert.deepEqual(execute(join(branchOut, "entry.mjs"), branchCalls), { events: [], results: [3, 4] });
  assert.match(branchText, /value as __split_local_0/);
  await writeFile(branchLogic, branchText.replace("value as __split_local_0", "other as __split_local_0"));
  const redirectedBranch = cli("validate", branchSource, constSpec, branchOut).value;
  assert.equal(redirectedBranch.result, "unsafe", JSON.stringify(redirectedBranch));
  assert.deepEqual(execute(branchSource, redirectedBranch.calls), { events: [], results: [3] });
  assert.deepEqual(execute(join(branchOut, "entry.mjs"), redirectedBranch.calls), { events: [], results: [5] });

  const elseSource = join(root, "else-alias-source.mjs");
  const elseOut = join(root, "else-alias-out");
  await writeFile(
    elseSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; if (value > p) { local += value; } else { local += p; } return local; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", elseSource, constSpec, elseOut).value.result, "generated");
  const elseResult = cli("validate", elseSource, constSpec, elseOut).value;
  assert.equal(elseResult.result, "proved under assumptions", JSON.stringify(elseResult));
  assert.deepEqual(execute(elseSource, branchCalls), { events: [], results: [3, 8] });
  assert.deepEqual(execute(join(elseOut, "entry.mjs"), branchCalls), { events: [], results: [3, 8] });

  const earlySource = join(root, "early-return-alias-source.mjs");
  const earlyOut = join(root, "early-return-alias-out");
  await writeFile(
    earlySource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { if (p < value) { return value; } return p; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", earlySource, constSpec, earlyOut).value.result, "generated");
  const earlyResult = cli("validate", earlySource, constSpec, earlyOut).value;
  assert.equal(earlyResult.result, "proved under assumptions", JSON.stringify(earlyResult));
  assert.deepEqual(execute(earlySource, branchCalls), { events: [], results: [3, 4] });
  assert.deepEqual(execute(join(earlyOut, "entry.mjs"), branchCalls), { events: [], results: [3, 4] });

  const terminalSource = join(root, "terminal-if-alias-source.mjs");
  const terminalOut = join(root, "terminal-if-alias-out");
  await writeFile(
    terminalSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { if (p < value) { return value; } else { return p; } }\nexport { compute };\n`
  );
  assert.equal(cli("generate", terminalSource, constSpec, terminalOut).value.result, "generated");
  const terminalResult = cli("validate", terminalSource, constSpec, terminalOut).value;
  assert.equal(terminalResult.result, "proved under assumptions", JSON.stringify(terminalResult));
  assert.deepEqual(execute(terminalSource, branchCalls), { events: [], results: [3, 4] });
  assert.deepEqual(execute(join(terminalOut, "entry.mjs"), branchCalls), { events: [], results: [3, 4] });
}

if (runSection(1)) {
  const loopSource = join(root, "loop-alias-source.mjs");
  const loopSpec = join(root, "loop-alias-spec.json");
  const loopOut = join(root, "loop-alias-out");
  await writeFile(
    loopSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; for (let i = 0; i < 2; i++) { local += value; } return local; }\nexport { compute };\n`
  );
  await writeFile(
    loopSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "logic.mjs": { imports: ["state.mjs"], statements: [2], renameLocalImports: true },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", loopSource, loopSpec, loopOut).value.result, "generated");
  const loopState = join(loopOut, "state.mjs");
  await writeFile(loopState, (await readFile(loopState, "utf8")) + "export { other };\n");
  const loopLogic = join(loopOut, "logic.mjs");
  const loopText = await readFile(loopLogic, "utf8");
  const loopResult = cli("validate", loopSource, loopSpec, loopOut).value;
  assert.equal(loopResult.result, "proved under assumptions", JSON.stringify(loopResult));
  const calls = [
    { name: "compute", args: [0] },
    { name: "compute", args: [4] },
  ];
  assert.deepEqual(execute(loopSource, calls), { events: [], results: [6, 10] });
  assert.deepEqual(execute(join(loopOut, "entry.mjs"), calls), { events: [], results: [6, 10] });
  assert.match(loopText, /value as __split_local_0/);
  await writeFile(loopLogic, loopText.replace("value as __split_local_0", "other as __split_local_0"));
  const redirected = cli("validate", loopSource, loopSpec, loopOut).value;
  assert.equal(redirected.result, "unsafe", JSON.stringify(redirected));
  assert.deepEqual(execute(loopSource, redirected.calls), { events: [], results: [6] });
  assert.deepEqual(execute(join(loopOut, "entry.mjs"), redirected.calls), { events: [], results: [10] });
  await writeFile(
    loopSource,
    `let i = 3;\nlet other = 5;\nfunction compute(p) { let local = p; for (let i = 0; i < 2; i++) { local += i; } return local + i; }\nexport { compute };\n`
  );
  const shadowOut = join(root, "shadow-loop-alias-out");
  assert.equal(cli("generate", loopSource, loopSpec, shadowOut).value.result, "generated");
  const shadowLogic = await readFile(join(shadowOut, "logic.mjs"), "utf8");
  assert.match(shadowLogic, /local \+= i/);
  assert.match(shadowLogic, /return local \+ __split_local_0/);
  assert.deepEqual(execute(loopSource, calls), { events: [], results: [4, 8] });
  assert.deepEqual(execute(join(shadowOut, "entry.mjs"), calls), { events: [], results: [4, 8] });
  const shadowResult = cli("validate", loopSource, loopSpec, shadowOut).value;
  assert.ok(["proved under assumptions", "unknown"].includes(shadowResult.result), JSON.stringify(shadowResult));
}

if (runSection(6)) {
  const finallySource = join(root, "finally-alias-source.mjs");
  const finallySpec = join(root, "finally-alias-spec.json");
  const finallyOut = join(root, "finally-alias-out");
  await writeFile(
    finallySource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; try { local += value; } finally { local += value; } return local; }\nexport { compute };\n`
  );
  await writeFile(
    finallySpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "logic.mjs": { imports: ["state.mjs"], statements: [2], renameLocalImports: true },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", finallySource, finallySpec, finallyOut).value.result, "generated");
  const finallyState = join(finallyOut, "state.mjs");
  await writeFile(finallyState, (await readFile(finallyState, "utf8")) + "export { other };\n");
  const finallyLogic = join(finallyOut, "logic.mjs");
  const finallyText = await readFile(finallyLogic, "utf8");
  const finallyResult = cli("validate", finallySource, finallySpec, finallyOut).value;
  assert.equal(finallyResult.result, "proved under assumptions", JSON.stringify(finallyResult));
  const calls = [
    { name: "compute", args: [0] },
    { name: "compute", args: [4] },
  ];
  assert.deepEqual(execute(finallySource, calls), { events: [], results: [6, 10] });
  assert.deepEqual(execute(join(finallyOut, "entry.mjs"), calls), { events: [], results: [6, 10] });
  assert.match(finallyText, /value as __split_local_0/);
  await writeFile(finallyLogic, finallyText.replace("value as __split_local_0", "other as __split_local_0"));
  const redirected = cli("validate", finallySource, finallySpec, finallyOut).value;
  assert.equal(redirected.result, "unsafe", JSON.stringify(redirected));
  assert.deepEqual(execute(finallySource, redirected.calls), { events: [], results: [6] });
  assert.deepEqual(execute(join(finallyOut, "entry.mjs"), redirected.calls), { events: [], results: [10] });

  const throwSource = join(root, "throw-finally-alias-source.mjs");
  const throwOut = join(root, "throw-finally-alias-out");
  await writeFile(
    throwSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; try { throw value; } finally { local += value; } return local; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", throwSource, finallySpec, throwOut).value.result, "generated");
  const throwState = join(throwOut, "state.mjs");
  await writeFile(throwState, (await readFile(throwState, "utf8")) + "export { other };\n");
  const throwLogic = join(throwOut, "logic.mjs");
  const throwText = await readFile(throwLogic, "utf8");
  const throwResult = cli("validate", throwSource, finallySpec, throwOut).value;
  assert.equal(throwResult.result, "proved under assumptions", JSON.stringify(throwResult));
  const observedThrow = (path) => {
    const script = `import(${JSON.stringify(pathToFileURL(path).href)}).then(m => { try { m.compute(0); } catch (error) { console.log(JSON.stringify(error)); } });`;
    const child = spawnSync(process.execPath, ["--input-type=module", "-e", script], { encoding: "utf8" });
    assert.equal(child.status, 0, child.stderr);
    return JSON.parse(child.stdout);
  };
  assert.equal(observedThrow(throwSource), 3);
  assert.equal(observedThrow(join(throwOut, "entry.mjs")), 3);
  assert.match(throwText, /value as __split_local_0/);
  await writeFile(throwLogic, throwText.replace("value as __split_local_0", "other as __split_local_0"));
  const throwRedirect = cli("validate", throwSource, finallySpec, throwOut).value;
  assert.equal(throwRedirect.result, "unsafe", JSON.stringify(throwRedirect));
  assert.equal(observedThrow(join(throwOut, "entry.mjs")), 5);

  const catchSource = join(root, "catch-finally-alias-source.mjs");
  const catchOut = join(root, "catch-finally-alias-out");
  await writeFile(
    catchSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; try { throw value; } catch { local += value; } finally { local += value; } return local; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", catchSource, finallySpec, catchOut).value.result, "generated");
  const catchState = join(catchOut, "state.mjs");
  await writeFile(catchState, (await readFile(catchState, "utf8")) + "export { other };\n");
  const catchLogic = join(catchOut, "logic.mjs");
  const catchText = await readFile(catchLogic, "utf8");
  const catchResult = cli("validate", catchSource, finallySpec, catchOut).value;
  assert.equal(catchResult.result, "proved under assumptions", JSON.stringify(catchResult));
  assert.deepEqual(execute(catchSource, calls), { events: [], results: [6, 10] });
  assert.deepEqual(execute(join(catchOut, "entry.mjs"), calls), { events: [], results: [6, 10] });
  assert.match(catchText, /value as __split_local_0/);
  await writeFile(catchLogic, catchText.replace("value as __split_local_0", "other as __split_local_0"));
  const catchRedirect = cli("validate", catchSource, finallySpec, catchOut).value;
  assert.equal(catchRedirect.result, "unsafe", JSON.stringify(catchRedirect));
  assert.deepEqual(execute(catchSource, catchRedirect.calls), { events: [], results: [6] });
  assert.deepEqual(execute(join(catchOut, "entry.mjs"), catchRedirect.calls), { events: [], results: [10] });

  const boundSource = join(root, "catch-bound-alias-source.mjs");
  const boundOut = join(root, "catch-bound-alias-out");
  await writeFile(
    boundSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; try { throw value; } catch (caught) { local += caught; } finally { local += value; } return local; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", boundSource, finallySpec, boundOut).value.result, "generated");
  const boundState = join(boundOut, "state.mjs");
  await writeFile(boundState, (await readFile(boundState, "utf8")) + "export { other };\n");
  const boundLogic = join(boundOut, "logic.mjs");
  const boundText = await readFile(boundLogic, "utf8");
  const boundResult = cli("validate", boundSource, finallySpec, boundOut).value;
  assert.equal(boundResult.result, "proved under assumptions", JSON.stringify(boundResult));
  assert.deepEqual(execute(boundSource, calls), { events: [], results: [6, 10] });
  assert.deepEqual(execute(join(boundOut, "entry.mjs"), calls), { events: [], results: [6, 10] });
  assert.match(boundText, /value as __split_local_0/);
  await writeFile(boundLogic, boundText.replace("value as __split_local_0", "other as __split_local_0"));
  const boundRedirect = cli("validate", boundSource, finallySpec, boundOut).value;
  assert.equal(boundRedirect.result, "unsafe", JSON.stringify(boundRedirect));
  assert.deepEqual(execute(boundSource, boundRedirect.calls), { events: [], results: [6] });
  assert.deepEqual(execute(join(boundOut, "entry.mjs"), boundRedirect.calls), { events: [], results: [10] });

  const nonnumericCatch = join(root, "nonnumeric-catch-alias-source.mjs");
  await writeFile(
    nonnumericCatch,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; try { throw "x"; } catch (caught) { local += caught; } finally { local += value; } return local; }\nexport { compute };\n`
  );
  const refusedCatch = cli("generate", nonnumericCatch, finallySpec, join(root, "nonnumeric-catch-out")).value;
  assert.equal(refusedCatch.result, "unknown");

  const shadowCatch = join(root, "shadow-catch-alias-source.mjs");
  const shadowCatchOut = join(root, "shadow-catch-alias-out");
  await writeFile(
    shadowCatch,
    `let caught = 3;\nlet value = 5;\nfunction compute(p) { let local = p; try { throw value; } catch (caught) { local += caught; } finally { local += value; } return local + caught; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", shadowCatch, finallySpec, shadowCatchOut).value.result, "generated");
  const shadowCatchText = await readFile(join(shadowCatchOut, "logic.mjs"), "utf8");
  assert.match(shadowCatchText, /local \+= caught/);
  assert.match(shadowCatchText, /return local \+ __split_local_0/);
  assert.deepEqual(execute(shadowCatch, calls), { events: [], results: [13, 17] });
  assert.deepEqual(execute(join(shadowCatchOut, "entry.mjs"), calls), { events: [], results: [13, 17] });
}

if (runSection(7)) {
  const arithmeticSource = join(root, "catch-arithmetic-source.mjs");
  const arithmeticSpec = join(root, "catch-arithmetic-spec.json");
  const arithmeticOut = join(root, "catch-arithmetic-out");
  await writeFile(
    arithmeticSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; try { throw value + p; } catch (caught) { local += caught; } finally { local += value; } return local; }\nexport { compute };\n`
  );
  await writeFile(
    arithmeticSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "logic.mjs": { imports: ["state.mjs"], statements: [2], renameLocalImports: true },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", arithmeticSource, arithmeticSpec, arithmeticOut).value.result, "generated");
  const arithmeticState = join(arithmeticOut, "state.mjs");
  await writeFile(arithmeticState, (await readFile(arithmeticState, "utf8")) + "export { other };\n");
  const arithmeticLogic = join(arithmeticOut, "logic.mjs");
  const arithmeticText = await readFile(arithmeticLogic, "utf8");
  const arithmeticResult = cli("validate", arithmeticSource, arithmeticSpec, arithmeticOut).value;
  assert.equal(arithmeticResult.result, "proved under assumptions", JSON.stringify(arithmeticResult));
  const calls = [
    { name: "compute", args: [0] },
    { name: "compute", args: [4] },
  ];
  assert.deepEqual(execute(arithmeticSource, calls), { events: [], results: [6, 14] });
  assert.deepEqual(execute(join(arithmeticOut, "entry.mjs"), calls), { events: [], results: [6, 14] });
  assert.match(arithmeticText, /value as __split_local_0/);
  await writeFile(arithmeticLogic, arithmeticText.replace("value as __split_local_0", "other as __split_local_0"));
  const redirected = cli("validate", arithmeticSource, arithmeticSpec, arithmeticOut).value;
  assert.equal(redirected.result, "unsafe", JSON.stringify(redirected));
  assert.deepEqual(execute(arithmeticSource, redirected.calls), { events: [], results: [6] });
  assert.deepEqual(execute(join(arithmeticOut, "entry.mjs"), redirected.calls), { events: [], results: [10] });

  const literalSource = join(root, "catch-literal-source.mjs");
  const literalOut = join(root, "catch-literal-out");
  await writeFile(
    literalSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; try { throw 1; } catch (caught) { local += caught; } finally { local += value; } return local; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", literalSource, arithmeticSpec, literalOut).value.result, "generated");
  const literalResult = cli("validate", literalSource, arithmeticSpec, literalOut).value;
  assert.equal(literalResult.result, "proved under assumptions", JSON.stringify(literalResult));
  assert.deepEqual(execute(literalSource, calls), { events: [], results: [4, 8] });
  assert.deepEqual(execute(join(literalOut, "entry.mjs"), calls), { events: [], results: [4, 8] });

  const catchOnlySource = join(root, "catch-only-source.mjs");
  const catchOnlyOut = join(root, "catch-only-out");
  await writeFile(
    catchOnlySource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; try { throw value; } catch (caught) { local += caught; } return local; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", catchOnlySource, arithmeticSpec, catchOnlyOut).value.result, "generated");
  const catchOnlyResult = cli("validate", catchOnlySource, arithmeticSpec, catchOnlyOut).value;
  assert.equal(catchOnlyResult.result, "proved under assumptions", JSON.stringify(catchOnlyResult));
  assert.deepEqual(execute(catchOnlySource, calls), { events: [], results: [3, 7] });
  assert.deepEqual(execute(join(catchOnlyOut, "entry.mjs"), calls), { events: [], results: [3, 7] });

  const mutableCatchSource = join(root, "mutable-catch-source.mjs");
  const mutableCatchOut = join(root, "mutable-catch-out");
  await writeFile(
    mutableCatchSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; try { throw value; } catch (caught) { caught += value; local += caught; } return local; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", mutableCatchSource, arithmeticSpec, mutableCatchOut).value.result, "generated");
  const mutableCatchResult = cli("validate", mutableCatchSource, arithmeticSpec, mutableCatchOut).value;
  assert.equal(mutableCatchResult.result, "proved under assumptions", JSON.stringify(mutableCatchResult));
  assert.deepEqual(execute(mutableCatchSource, calls), { events: [], results: [6, 10] });
  assert.deepEqual(execute(join(mutableCatchOut, "entry.mjs"), calls), { events: [], results: [6, 10] });

  const priorUpdateSource = join(root, "prior-update-catch-source.mjs");
  const priorUpdateOut = join(root, "prior-update-catch-out");
  await writeFile(
    priorUpdateSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; try { local += value; throw value + p; } catch (caught) { local += caught; } return local; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", priorUpdateSource, arithmeticSpec, priorUpdateOut).value.result, "generated");
  const priorUpdateResult = cli("validate", priorUpdateSource, arithmeticSpec, priorUpdateOut).value;
  assert.equal(priorUpdateResult.result, "proved under assumptions", JSON.stringify(priorUpdateResult));
  assert.deepEqual(execute(priorUpdateSource, calls), { events: [], results: [6, 14] });
  assert.deepEqual(execute(join(priorUpdateOut, "entry.mjs"), calls), { events: [], results: [6, 14] });

  const priorIncrementSource = join(root, "prior-increment-catch-source.mjs");
  const priorIncrementOut = join(root, "prior-increment-catch-out");
  await writeFile(
    priorIncrementSource,
    `let value = 3;\nlet other = 5;\nfunction compute(p) { let local = p; try { local++; throw value + local; } catch (caught) { local += caught; } return local; }\nexport { compute };\n`
  );
  assert.equal(cli("generate", priorIncrementSource, arithmeticSpec, priorIncrementOut).value.result, "generated");
  const priorIncrementResult = cli("validate", priorIncrementSource, arithmeticSpec, priorIncrementOut).value;
  assert.equal(priorIncrementResult.result, "proved under assumptions", JSON.stringify(priorIncrementResult));
  assert.deepEqual(execute(priorIncrementSource, calls), { events: [], results: [5, 13] });
  assert.deepEqual(execute(join(priorIncrementOut, "entry.mjs"), calls), { events: [], results: [5, 13] });
  const priorIncrementLogic = join(priorIncrementOut, "logic.mjs");
  const priorIncrementText = await readFile(priorIncrementLogic, "utf8");
  assert.match(priorIncrementText, /local\+\+/);
  await writeFile(priorIncrementLogic, priorIncrementText.replace("local++", "__split_local_0++"));
  const importedIncrement = cli("validate", priorIncrementSource, arithmeticSpec, priorIncrementOut).value;
  assert.notEqual(importedIncrement.result, "proved under assumptions", JSON.stringify(importedIncrement));
  const oneCall = [{ name: "compute", args: [0] }];
  assert.notDeepEqual(execute(priorIncrementSource, oneCall), execute(join(priorIncrementOut, "entry.mjs"), oneCall));
}

if (runSection(1)) {
  const source = join(root, "catch-capture-source.mjs");
  const spec = join(root, "catch-capture-spec.json");
  const out = join(root, "catch-capture-out");
  await writeFile(
    source,
    `let original = 3;\nlet alternate = 5;\nfunction compute() { let local = 0; try { throw original; } catch (alternate) { local += alternate; local += original; } return local; }\nexport { compute };\n`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "logic.mjs": { imports: ["state.mjs"], statements: [2], renameLocalImports: true },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).value.result, "generated");
  const state = join(out, "state.mjs");
  await writeFile(state, (await readFile(state, "utf8")) + "export { alternate };\n");
  const logic = join(out, "logic.mjs");
  const text = await readFile(logic, "utf8");
  assert.match(text, /original as __split_local_0/);
  const unchanged = cli("validate", source, spec, out).value;
  assert.equal(unchanged.result, "proved under assumptions", JSON.stringify(unchanged));
  await writeFile(logic, text.replace("original as __split_local_0", "alternate as __split_local_0"));
  const redirected = cli("validate", source, spec, out).value;
  assert.equal(redirected.result, "unsafe", JSON.stringify(redirected));
  assert.deepEqual(execute(source, redirected.calls), { events: [], results: [6] });
  assert.deepEqual(execute(join(out, "entry.mjs"), redirected.calls), { events: [], results: [10] });
  const axioms = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + "\n#print axioms witnessedExpressionFuel\n",
  });
  assert.equal(axioms.status, 0, axioms.stdout + axioms.stderr);
  assert.match(axioms.stdout, /'witnessedExpressionFuel' does not depend on any axioms/);
}

if (runSection(0)) {
  const insertedSource = join(root, "inserted-event-source.mjs");
  const insertedSpec = join(root, "inserted-event-spec.json");
  const insertedOut = join(root, "inserted-event-out");
  await writeFile(insertedSource, `let value = 3;\nfunction read() { return value; }\nexport { read };\n`);
  await writeFile(
    insertedSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0] },
        "entry.mjs": { imports: ["state.mjs"], statements: [1] },
      },
    })
  );
  assert.equal(cli("generate", insertedSource, insertedSpec, insertedOut).value.result, "generated");
  const insertedState = join(insertedOut, "state.mjs");
  const insertedText = await readFile(insertedState, "utf8");
  assert.match(insertedText, /let value = 3;/);
  await writeFile(insertedState, insertedText.replace("let value = 3;", "let value = 3;\nglobalThis.record(value);"));
  const inserted = cli("validate", insertedSource, insertedSpec, insertedOut).value;
  assert.equal(inserted.result, "unsafe", JSON.stringify(inserted));
  assert.deepEqual(execute(insertedSource, []), { events: [], results: [] });
  assert.deepEqual(execute(join(insertedOut, "entry.mjs"), []), { events: [3], results: [] });

  const removedSource = join(root, "removed-before-call-source.mjs");
  const removedSpec = join(root, "removed-before-call-spec.json");
  const removedOut = join(root, "removed-before-call-out");
  await writeFile(removedSource, `let value = 3;\n0;\nfunction read(p) { return value + p; }\nexport { read };\n`);
  await writeFile(
    removedSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "entry.mjs": { imports: ["state.mjs"], statements: [2] },
      },
    })
  );
  assert.equal(cli("generate", removedSource, removedSpec, removedOut).value.result, "generated");
  const removedState = join(removedOut, "state.mjs");
  const removedStateText = await readFile(removedState, "utf8");
  assert.match(removedStateText, /0;/);
  await writeFile(removedState, removedStateText.replace("0;", ""));
  const removedEntry = join(removedOut, "entry.mjs");
  const removedEntryText = await readFile(removedEntry, "utf8");
  assert.match(removedEntryText, /return value \+ p;/);
  await writeFile(removedEntry, removedEntryText.replace("return value + p;", "return value + p + 1;"));
  const removed = cli("validate", removedSource, removedSpec, removedOut).value;
  assert.equal(removed.result, "unsafe", JSON.stringify(removed));
  assert.deepEqual(execute(removedSource, removed.calls), { events: [], results: [3] });
  assert.deepEqual(execute(removedEntry, removed.calls), { events: [], results: [4] });
}
