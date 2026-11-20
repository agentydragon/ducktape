import { createRequire } from "node:module";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const { parse } = createRequire(import.meta.url)(process.env.JS_MODULE_PROOF_ACORN ?? "acorn");
const body = (text) => parse(text, { ecmaVersion: "latest", sourceType: "module" }).body;
const requireShape = (condition, reason) => {
  if (!condition) throw new Error(`unknown: ${reason}`);
};
const id = (node) => (node?.type === "Identifier" ? node.name : null);
const list = (values) => `[${values.join(", ")}]`;
const str = JSON.stringify;
const filePattern = /^[A-Za-z][A-Za-z0-9_-]*\.mjs$/;

function sourceFacts(text) {
  const nodes = body(text);
  requireShape(nodes.length === 6, "expected a cell, two functions, two initialization calls and one export");
  const variables = nodes.filter((n) => n.type === "VariableDeclaration");
  const functions = nodes.filter((n) => n.type === "FunctionDeclaration");
  const exports = nodes.filter((n) => n.type === "ExportNamedDeclaration");
  requireShape(variables.length === 1 && functions.length === 2 && exports.length === 1, "unsupported declarations");
  const variable = variables[0];
  requireShape(variable.kind === "let" && variable.declarations.length === 1, "expected one let cell");
  const decl = variable.declarations[0];
  const cell = id(decl.id);
  const initial = decl.init?.type === "Literal" && decl.init.value;
  requireShape(
    cell && Number.isSafeInteger(initial) && initial >= 0 && initial < Number.MAX_SAFE_INTEGER,
    "expected a safe integer cell"
  );
  const parseFunction = (node) => {
    requireShape(
      !node.async && !node.generator && node.params.length === 0 && node.body.body.length === 1 && id(node.id),
      "unsupported function"
    );
    const stmt = node.body.body[0];
    if (stmt.type === "ReturnStatement" && id(stmt.argument) === cell) return { kind: "read", name: id(node.id), node };
    const expr = stmt.type === "ExpressionStatement" && stmt.expression;
    requireShape(
      expr?.type === "AssignmentExpression" &&
        expr.operator === "+=" &&
        id(expr.left) === cell &&
        expr.right.type === "Literal" &&
        expr.right.value === 1,
      "unsupported function body"
    );
    return { kind: "increment", name: id(node.id), node };
  };
  const parsed = functions.map(parseFunction);
  const increment = parsed.find((f) => f.kind === "increment");
  const reader = parsed.find((f) => f.kind === "read");
  requireShape(
    increment && reader && new Set([cell, increment.name, reader.name, "globalThis"]).size === 4,
    "invalid binding names"
  );
  const exported = exports[0];
  requireShape(
    !exported.declaration &&
      !exported.source &&
      exported.specifiers.length === 1 &&
      id(exported.specifiers[0].local) === reader.name &&
      id(exported.specifiers[0].exported) === reader.name,
    "expected only the reader export"
  );
  const runtime = [];
  const statements = nodes.filter((n) => n !== exported);
  for (const node of statements) {
    if (node === variable) runtime.push({ node, op: "init" });
    else if (node === increment.node || node === reader.node) continue;
    else {
      const expr = node.type === "ExpressionStatement" && node.expression;
      requireShape(expr?.type === "CallExpression" && !expr.optional, "expected initialization call");
      if (id(expr.callee) === increment.name && expr.arguments.length === 0) runtime.push({ node, op: "increment" });
      else {
        const member = expr.callee;
        requireShape(
          member?.type === "MemberExpression" &&
            !member.computed &&
            !member.optional &&
            id(member.object) === "globalThis" &&
            id(member.property) === "record" &&
            expr.arguments.length === 1 &&
            id(expr.arguments[0]) === cell,
          "expected record of the cell"
        );
        runtime.push({ node, op: "record" });
      }
    }
  }
  requireShape(
    runtime.length === 3 && new Set(runtime.map((r) => r.op)).size === 3,
    "expected one initializer, increment and record"
  );
  return { text, statements, runtime, cell, initial, increment, reader };
}

function assignment(spec, f) {
  requireShape(
    spec?.kind === "combined" && spec.modules && typeof spec.modules === "object",
    "expected combined assignment"
  );
  const names = [spec.state, spec.reader, spec.entry];
  requireShape(
    names.every((n) => typeof n === "string" && filePattern.test(n)) &&
      new Set(names).size === 3 &&
      Object.keys(spec.modules).length === 3 &&
      names.every((n) => Array.isArray(spec.modules[n])),
    "expected three distinct modules"
  );
  const indices = names.flatMap((n) => spec.modules[n]);
  requireShape(
    indices.length === f.statements.length &&
      indices.every((n) => Number.isInteger(n) && n >= 0 && n < f.statements.length) &&
      new Set(indices).size === indices.length,
    "assign every non-export statement once"
  );
  requireShape(
    names.every((name) => spec.modules[name].every((value, index, a) => index === 0 || a[index - 1] < value)),
    "keep statements in source order within a module"
  );
  const owner = (node) => names.find((n) => spec.modules[n].includes(f.statements.indexOf(node)));
  requireShape(
    owner(f.statements.find((n) => n.type === "VariableDeclaration")) === spec.state &&
      owner(f.increment.node) === spec.state &&
      owner(f.reader.node) === spec.reader,
    "cell and increment must be in state, reader in reader"
  );
  return names;
}

function generated(f, spec, names) {
  const emitted = {};
  for (const name of names) {
    const indices = spec.modules[name];
    const hasOp = (op) =>
      indices.some((i) => f.runtime.some((item) => item.node === f.statements[i] && item.op === op));
    const imports = [];
    if (name === spec.reader)
      imports.push(
        `import { ${f.cell}${hasOp("increment") ? `, ${f.increment.name}` : ""} } from ${str(`./${spec.state}`)};`
      );
    if (name === spec.entry) {
      const bindings = [hasOp("record") ? f.cell : null, hasOp("increment") ? f.increment.name : null].filter(Boolean);
      imports.push(
        bindings.length
          ? `import { ${bindings.join(", ")} } from ${str(`./${spec.state}`)};`
          : `import ${str(`./${spec.state}`)};`
      );
      imports.push(`export { ${f.reader.name} } from ${str(`./${spec.reader}`)};`);
    }
    const statements = indices.map((i) => {
      const node = f.statements[i];
      const declaration = node === f.increment.node || node === f.reader.node || node.type === "VariableDeclaration";
      return `${declaration ? "export " : ""}${f.text.slice(node.start, node.end)}`;
    });
    emitted[name] = [...imports, ...statements].join("\n") + "\n";
  }
  return emitted;
}

// Compare syntax trees, not source formatting. Only the explicitly accepted
// generated shape is supported; no output is certified solely from its spec.
function normalized(text) {
  return JSON.stringify(body(text), (key, value) => (["start", "end", "raw"].includes(key) ? undefined : value));
}

async function load(source, specPath) {
  const f = sourceFacts(await readFile(source, "utf8"));
  const spec = JSON.parse(await readFile(specPath, "utf8"));
  const names = assignment(spec, f);
  return { f, spec, names, expected: generated(f, spec, names) };
}

export async function generateCombined(source, specPath, out) {
  const { names, expected } = await load(source, specPath);
  await mkdir(out, { recursive: true });
  for (const name of names) await writeFile(resolve(out, name), expected[name], { flag: "wx" });
}

function execute(path, reader) {
  const code = `const events=[];globalThis.record=v=>events.push(v);import(${str(pathToFileURL(resolve(path)).href)}).then(m=>console.log(JSON.stringify({events,current:m[${str(reader)}]()})));`;
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8" });
  if (child.error || child.status !== 0) throw new Error(child.stderr || String(child.error));
  return JSON.parse(child.stdout);
}

export async function validateCombined(source, specPath, out) {
  const { f, spec, names, expected } = await load(source, specPath);
  const files = await Promise.all(names.map((name) => readFile(resolve(out, name), "utf8")));
  if (!files.every((text, i) => normalized(text) === normalized(expected[names[i]])))
    return { result: "unknown", reason: "emitted module syntax does not match the assignment" };
  const ops = f.runtime.map((item) => item.op);
  const modules = names.map((name) => {
    const indices = spec.modules[name]
      .map((i) => f.runtime.findIndex((item) => item.node === f.statements[i]))
      .filter((i) => i !== -1);
    const imports = name === spec.state ? [] : name === spec.reader ? [0] : [0, 1];
    return `({ imports := ${list(imports)}, expectedImports := ${list(imports)}, assignment := ${list(indices)}, effects := ${list(indices.map((i) => str(ops[i])))} } : EffectModule)`;
  });
  const prefix = `\ndef combinedSource : List String := ${list(ops.map(str))}\ndef combinedModules : List EffectModule := ${list(modules)}\ndef combinedEntry : Nat := 2\ntheorem combined_assignment : assignmentsMatch combinedSource combinedModules = true := by decide\ntheorem combined_source_ready : (sourceCombined ${f.initial} combinedSource).isSome = true := by decide\ntheorem combined_output_ready : (outputCombined ${f.initial} combinedModules combinedEntry).isSome = true := by decide\n#print axioms combined_assignment\n#print axioms combined_source_ready\n#print axioms combined_output_ready\n`;
  const same = `\ntheorem combined_initialization : sourceCombined ${f.initial} combinedSource = outputCombined ${f.initial} combinedModules combinedEntry := by decide\ntheorem combined_calls (count : Nat) : (sourceCombined ${f.initial} combinedSource).bind (fun state => currentCalls state count) = (outputCombined ${f.initial} combinedModules combinedEntry).bind (fun state => currentCalls state count) := by\n  exact combined_current_preserved _ _ combined_initialization count\n#print axioms combined_initialization\n#print axioms combined_calls\n`;
  const model =
    (await readFile(new URL("./Semantics.lean", import.meta.url), "utf8")) +
    "\n" +
    (await readFile(new URL("./Effects.lean", import.meta.url), "utf8")) +
    "\n" +
    (await readFile(new URL("./Combined.lean", import.meta.url), "utf8"));
  const lean = process.env.JS_MODULE_PROOF_LEAN ?? "lean";
  const runLean = (certificate) =>
    spawnSync(lean, ["--trust=0", "--stdin"], { encoding: "utf8", input: model + certificate });
  const proof = runLean(prefix + same);
  if (proof.error || proof.status === null)
    return { result: "unknown", reason: "Lean could not be run", detail: String(proof.error) };
  const has = (result, name) => result.stdout.includes(`'${name}' does not depend on any axioms`);
  if (!["combined_assignment", "combined_source_ready", "combined_output_ready"].every((name) => has(proof, name)))
    return {
      result: "unknown",
      reason: "invalid modeled assignment or initialization",
      detail: proof.stdout || proof.stderr,
    };
  if (proof.status === 0 && ["combined_initialization", "combined_calls"].every((name) => has(proof, name))) {
    return {
      result: "proved under assumptions",
      certificate: prefix + same,
      assumptions: [
        "The Acorn extraction and output-shape check faithfully represent the files",
        "The modeled acyclic module scheduler and execution operations match ECMAScript",
        "record is a stable synchronous host function that returns normally without reentry or asynchronous effects",
        "Only fresh initialization and subsequent calls to the exported reader are observed; loading, reflection, source text and identity are excluded",
      ],
    };
  }
  const different = `\ntheorem combined_difference : sourceCombined ${f.initial} combinedSource ≠ outputCombined ${f.initial} combinedModules combinedEntry := by decide\n#print axioms combined_difference\n`;
  const mismatch = runLean(prefix + different);
  if (mismatch.status !== 0 || !has(mismatch, "combined_difference"))
    return { result: "unknown", reason: "Lean did not prove a difference", detail: mismatch.stdout || mismatch.stderr };
  const original = execute(source, f.reader.name);
  const output = execute(resolve(out, spec.entry), f.reader.name);
  if (JSON.stringify(original) === JSON.stringify(output))
    return { result: "unknown", reason: "modeled difference was not reproduced in Node" };
  return { result: "unsafe", original, output, certificate: prefix + different };
}
