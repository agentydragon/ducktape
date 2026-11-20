import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";

const { parse } = createRequire(import.meta.url)(process.env.JS_MODULE_PROOF_ACORN ?? "acorn");
const filenamePattern = /^[A-Za-z][A-Za-z0-9_-]*\.mjs$/;

function requireShape(condition, reason) {
  if (!condition) throw new Error(`unknown: ${reason}`);
}

function parseBody(text) {
  return parse(text, { ecmaVersion: "latest", sourceType: "module" }).body;
}

function eventName(statement) {
  const call = statement?.type === "ExpressionStatement" && statement.expression;
  const member = call?.type === "CallExpression" && call.callee;
  requireShape(
    member?.type === "MemberExpression" &&
      !member.computed &&
      !member.optional &&
      member.object.type === "Identifier" &&
      member.object.name === "globalThis" &&
      member.property.type === "Identifier" &&
      member.property.name === "record" &&
      !call.optional &&
      call.arguments.length === 1 &&
      call.arguments[0].type === "Literal" &&
      typeof call.arguments[0].value === "string" &&
      /^[A-Za-z0-9_-]+$/.test(call.arguments[0].value),
    "expected globalThis.record with a simple string literal"
  );
  return call.arguments[0].value;
}

function sourceEvents(text) {
  const body = parseBody(text);
  requireShape(body.length > 0 && body.length <= 12, "expected one to twelve effect statements");
  return body.map((node) => ({ name: eventName(node), source: text.slice(node.start, node.end) }));
}

function effectSpec(spec, count) {
  requireShape(
    spec && spec.kind === "effects" && spec.modules && typeof spec.modules === "object" && !Array.isArray(spec.modules),
    "expected an effects assignment"
  );
  const names = Object.keys(spec.modules);
  requireShape(
    names.length > 0 &&
      names.length <= 12 &&
      names.every((name) => filenamePattern.test(name)) &&
      names.includes(spec.entry),
    "invalid effect module names or entry"
  );
  const assigned = [];
  for (const name of names) {
    const module = spec.modules[name];
    requireShape(
      module && Array.isArray(module.imports) && Array.isArray(module.statements),
      "expected imports and statements arrays"
    );
    requireShape(
      module.imports.every((dep) => names.includes(dep)) &&
        module.statements.every((i) => Number.isInteger(i) && i >= 0 && i < count),
      "invalid dependency or statement index"
    );
    assigned.push(...module.statements);
  }
  requireShape(
    assigned.length === count && new Set(assigned).size === count,
    "every source statement must be assigned once"
  );
  return names;
}

function outputModule(text, names) {
  const imports = [];
  const events = [];
  for (const node of parseBody(text)) {
    if (node.type === "ImportDeclaration") {
      requireShape(
        node.specifiers.length === 0 &&
          typeof node.source.value === "string" &&
          node.source.value.startsWith("./") &&
          names.includes(node.source.value.slice(2)) &&
          (!node.attributes || node.attributes.length === 0),
        "unsupported or external module import"
      );
      imports.push(node.source.value.slice(2));
    } else {
      events.push(eventName(node));
    }
  }
  return { imports, events };
}

const leanString = JSON.stringify;
const leanList = (values) => `[${values.join(", ")}]`;
const digest = (text) => createHash("sha256").update(text).digest("hex");

function leanModule(module, names, assignment) {
  const indices = (items) => leanList(items.map((name) => names.indexOf(name)));
  return `({ imports := ${indices(module.imports)}, expectedImports := ${indices(assignment.imports)}, assignment := ${leanList(assignment.statements)}, effects := ${leanList(module.events.map(leanString))} } : EffectModule)`;
}

async function load(sourcePath, assignmentPath) {
  const text = await readFile(sourcePath, "utf8");
  const statements = sourceEvents(text);
  const spec = JSON.parse(await readFile(assignmentPath, "utf8"));
  const names = effectSpec(spec, statements.length);
  return { text, statements, spec, names };
}

export async function generateEffects(sourcePath, assignmentPath, outputDir) {
  const { statements, spec, names } = await load(sourcePath, assignmentPath);
  await mkdir(outputDir, { recursive: true });
  for (const name of names) {
    const module = spec.modules[name];
    const lines = [
      ...module.imports.map((dep) => `import ${JSON.stringify(`./${dep}`)};`),
      ...module.statements.map((i) => statements[i].source),
    ];
    await writeFile(resolve(outputDir, name), `${lines.join("\n")}\n`, { flag: "wx" });
  }
}

function execute(path) {
  const url = pathToFileURL(resolve(path)).href;
  const script = `const events=[]; globalThis.record=value=>events.push(value); import(${JSON.stringify(url)}).then(()=>console.log(JSON.stringify(events)));`;
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", script], { encoding: "utf8" });
  if (child.error || child.status !== 0) throw new Error(child.stderr || String(child.error));
  return JSON.parse(child.stdout);
}

function candidateTrace(modules, names, entry) {
  const active = new Set();
  const visited = new Set();
  const events = [];
  function visit(name) {
    if (visited.has(name)) return true;
    if (active.has(name)) return false;
    active.add(name);
    const module = modules[names.indexOf(name)];
    for (const dependency of module.imports) if (!visit(dependency)) return false;
    active.delete(name);
    visited.add(name);
    events.push(...module.events);
    return true;
  }
  return visit(entry) && visited.size === names.length ? events : null;
}

export async function validateEffects(sourcePath, assignmentPath, outputDir) {
  const { text, statements, spec, names } = await load(sourcePath, assignmentPath);
  const files = Object.fromEntries(
    await Promise.all(names.map(async (name) => [name, await readFile(resolve(outputDir, name), "utf8")]))
  );
  const modules = names.map((name) => outputModule(files[name], names));
  const model =
    (await readFile(new URL("./Semantics.lean", import.meta.url), "utf8")) +
    "\n" +
    (await readFile(new URL("./Effects.lean", import.meta.url), "utf8"));
  const source = leanList(statements.map((event) => leanString(event.name)));
  const moduleTerms = leanList(modules.map((module, i) => leanModule(module, names, spec.modules[names[i]])));
  const entry = names.indexOf(spec.entry);
  const predicted = candidateTrace(modules, names, spec.entry);
  const predictedTerm = predicted ? `some ${leanList(predicted.map(leanString))}` : "none";
  const traceCertificate = `
-- Source SHA256: ${digest(text)}
-- Output SHA256: ${names.map((name) => `${name}=${digest(files[name])}`).join(" ")}
def extractedEffects : List String := ${source}
def extractedModules : List EffectModule := ${moduleTerms}
def extractedEntry : Nat := ${entry}
theorem effect_output_trace : effectTrace extractedModules extractedEntry = ${predictedTerm} := by decide
#print axioms effect_output_trace
theorem effect_machine_trace : machineTrace extractedModules extractedEntry = ${predictedTerm} := by
  ${predicted ? "exact checked_trace_sound extractedModules extractedEntry _ effect_output_trace" : "decide"}
#print axioms effect_machine_trace
`;
  const wellFormedCertificate = `${traceCertificate}
theorem effect_graph_well_formed :
    assignmentsMatch extractedEffects extractedModules = true ∧
      (effectTrace extractedModules extractedEntry).isSome = true := by decide
#print axioms effect_graph_well_formed
`;
  const certificate = `${wellFormedCertificate}
theorem effect_trace_matches : effectTrace extractedModules extractedEntry = some extractedEffects := by decide
theorem effect_behavior {α : Type} (replies : List (Option α)) :
    outputBehavior extractedModules extractedEntry replies = some (runEffects extractedEffects replies) := by
  exact trace_preserves_behavior extractedEffects extractedModules extractedEntry effect_trace_matches replies
#print axioms effect_behavior
`;
  const lean = process.env.JS_MODULE_PROOF_LEAN ?? "lean";
  const proof = spawnSync(lean, ["-j", "1", "--trust=0", "--stdin"], { encoding: "utf8", input: model + certificate });
  if (proof.error || proof.status === null)
    return { result: "unknown", reason: "Lean could not be run", detail: String(proof.error) };
  const axiomFree = (name) => proof.stdout.includes(`'${name}' does not depend on any axioms`);
  if (!axiomFree("effect_output_trace"))
    return {
      result: "unknown",
      reason: "Lean could not validate the output evaluation trace",
      detail: proof.stderr || proof.stdout,
    };
  if (!axiomFree("effect_machine_trace"))
    return {
      result: "unknown",
      reason: "Lean could not validate the operational-machine trace",
      detail: proof.stderr || proof.stdout,
    };
  if (!axiomFree("effect_graph_well_formed"))
    return {
      result: "unknown",
      reason: "Lean could not validate the module graph and assignment",
      modeledOutput: predicted,
      traceCertificate,
      detail: proof.stderr || proof.stdout,
    };
  if (proof.status !== 0) {
    const mismatchCertificate = `${wellFormedCertificate}
theorem effect_trace_differs : effectTrace extractedModules extractedEntry ≠ some extractedEffects := by decide
#print axioms effect_trace_differs
`;
    const mismatch = spawnSync(lean, ["-j", "1", "--trust=0", "--stdin"], {
      encoding: "utf8",
      input: model + mismatchCertificate,
    });
    if (
      mismatch.error ||
      mismatch.status !== 0 ||
      !mismatch.stdout.includes("'effect_trace_differs' does not depend on any axioms")
    ) {
      return {
        result: "unknown",
        reason: "Lean could not prove a trace difference",
        detail: mismatch.stderr || mismatch.stdout || String(mismatch.error),
      };
    }
    const before = execute(sourcePath);
    const after = execute(resolve(outputDir, spec.entry));
    if (JSON.stringify(before) !== JSON.stringify(after))
      return {
        result: "unsafe",
        original: before,
        output: after,
        modeledOutput: predicted,
        certificate: mismatchCertificate,
      };
    return {
      result: "unknown",
      reason: "Lean did not prove trace equality and no witness was found",
      detail: proof.stderr || proof.stdout,
    };
  }
  if (!axiomFree("effect_behavior"))
    return { result: "unknown", reason: "Lean did not report an axiom-free behavior theorem" };
  return {
    result: "proved under assumptions",
    certificate,
    sourceEffects: statements.map((event) => event.name),
    modeledOutput: predicted,
    modules: Object.fromEntries(names.map((name, i) => [name, modules[i]])),
    assumptions: [
      "globalThis.record is a stable ordinary data-property function throughout initialization",
      "Each record call terminates synchronously by returning or throwing, without reentry or asynchronous effects",
      "The host responds consistently to the same ordered call history; call arguments, exceptions and termination are observed",
      "Only one fresh evaluation is considered; module loading, identity, source text, stack traces and reflection are not observed",
      "Standard module loading and linking succeed for the checked files without intervening changes",
      "The parser and AST conversion faithfully represent the supported effects and module imports",
      "The modeled acyclic module evaluation order matches ECMAScript",
    ],
  };
}
