import { createHash } from "node:crypto";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";

const { parse } = createRequire(import.meta.url)(process.env.JS_MODULE_PROOF_ACORN ?? "acorn");
const maxInteger = Number.MAX_SAFE_INTEGER;

function requireShape(condition, reason) {
  if (!condition) throw new Error(`unknown: ${reason}`);
}

function parseModule(text) {
  return parse(text, { ecmaVersion: "latest", sourceType: "module" }).body;
}

function identifier(node) {
  requireShape(
    node?.type === "Identifier" && /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(node.name),
    "expected a simple identifier"
  );
  return node.name;
}

function variable(node, kind) {
  requireShape(
    node?.type === "VariableDeclaration" && node.kind === kind && node.declarations.length === 1,
    `expected one ${kind} binding`
  );
  const decl = node.declarations[0];
  return { name: identifier(decl.id), init: decl.init };
}

function integer(node) {
  requireShape(
    node?.type === "Literal" && Number.isSafeInteger(node.value) && node.value >= 0,
    "expected a nonnegative safe integer"
  );
  return node.value;
}

function functionBody(node) {
  requireShape(
    node?.type === "FunctionDeclaration" &&
      !node.async &&
      !node.generator &&
      node.params.length === 0 &&
      node.body.body.length === 1,
    "unsupported function"
  );
  const name = identifier(node.id);
  const stmt = node.body.body[0];
  if (stmt.type === "ReturnStatement") return { name, kind: "current", ref: identifier(stmt.argument) };
  const expr = stmt.type === "ExpressionStatement" && stmt.expression;
  requireShape(
    expr?.type === "AssignmentExpression" &&
      expr.operator === "+=" &&
      expr.right.type === "Literal" &&
      expr.right.value === 1,
    "expected increment or return"
  );
  return { name, kind: "increment", ref: identifier(expr.left) };
}

function exportNames(node) {
  requireShape(
    node?.type === "ExportNamedDeclaration" && !node.declaration && node.specifiers.length > 0,
    "unsupported exports"
  );
  return node.specifiers.map((s) => ({ local: identifier(s.local), exported: identifier(s.exported) }));
}

function sourceFacts(text) {
  const body = parseModule(text);
  requireShape(body.length === 4, "expected one cell, two functions, and exports");
  const vars = body.filter((n) => n.type === "VariableDeclaration");
  const funcs = body.filter((n) => n.type === "FunctionDeclaration");
  const exports = body.filter((n) => n.type === "ExportNamedDeclaration");
  requireShape(
    vars.length === 1 && funcs.length === 2 && exports.length === 1 && !exports[0].source,
    "unsupported source declarations"
  );
  const cell = variable(vars[0], "let");
  const initial = integer(cell.init);
  const parsed = funcs.map(functionBody);
  const inc = parsed.find((f) => f.kind === "increment");
  const read = parsed.find((f) => f.kind === "current");
  requireShape(inc && read, "expected increment and current functions");
  const exported = exportNames(exports[0]);
  return {
    initial,
    cell: cell.name,
    inc: inc.name,
    incRef: inc.ref,
    read: read.name,
    readRef: read.ref,
    exported,
    nodes: {
      cell: vars[0],
      inc: funcs.find((n) => n.id.name === inc.name),
      read: funcs.find((n) => n.id.name === read.name),
    },
  };
}

function assignmentFacts(spec, facts) {
  requireShape(spec && typeof spec === "object" && !Array.isArray(spec), "assignment must be an object");
  const names = Object.keys(spec);
  requireShape(
    names.length === 2 &&
      names.every((n) => /^[A-Za-z][A-Za-z0-9_-]*\.mjs$/.test(n) && n !== "entry.mjs" && Array.isArray(spec[n])),
    "expected two safe module filenames"
  );
  const state = names.find((n) => spec[n].includes(facts.cell));
  const reader = names.find((n) => spec[n].includes(facts.read));
  requireShape(
    state &&
      reader &&
      state !== reader &&
      spec[state].length === 2 &&
      spec[state].includes(facts.inc) &&
      spec[reader].length === 1 &&
      spec[reader][0] === facts.read,
    "assignment must keep the cell with increment and put current in the other module"
  );
  return { state, reader };
}

async function loadInput(sourcePath, assignmentPath) {
  const text = await readFile(sourcePath, "utf8");
  const facts = sourceFacts(text);
  const modules = assignmentFacts(JSON.parse(await readFile(assignmentPath, "utf8")), facts);
  return { text, facts, modules };
}

function outputFacts(stateText, readerText, entryText, modules) {
  const state = parseModule(stateText);
  requireShape(
    state.length === 2 && state.every((n) => n.type === "ExportNamedDeclaration" && n.declaration && !n.source),
    "unsupported state module"
  );
  const cellNode = state.find((n) => n.declaration.type === "VariableDeclaration")?.declaration;
  const incNode = state.find((n) => n.declaration.type === "FunctionDeclaration")?.declaration;
  const cell = variable(cellNode, "let");
  const initial = integer(cell.init);
  const inc = functionBody(incNode);
  requireShape(inc.kind === "increment", "expected an increment function");

  const reader = parseModule(readerText);
  requireShape(reader.length === 2 || reader.length === 3, "unsupported reader module");
  const imports = reader.filter((n) => n.type === "ImportDeclaration");
  const exports = reader.filter((n) => n.type === "ExportNamedDeclaration" && n.declaration);
  const snapshots = reader.filter((n) => n.type === "VariableDeclaration");
  requireShape(
    imports.length === 1 &&
      exports.length === 1 &&
      snapshots.length === reader.length - 2 &&
      typeof imports[0].source.value === "string" &&
      imports[0].specifiers.length === 1 &&
      imports[0].specifiers[0].type === "ImportSpecifier",
    "unsupported reader imports or declarations"
  );
  const imported = identifier(imports[0].specifiers[0].imported);
  const local = identifier(imports[0].specifiers[0].local);
  const read = functionBody(exports[0].declaration);
  requireShape(read.kind === "current", "expected a reader function");
  let snapshot = null;
  if (snapshots.length) {
    const decl = variable(snapshots[0], "const");
    snapshot = { name: decl.name, value: identifier(decl.init) };
  }

  const entry = parseModule(entryText);
  requireShape(
    entry.length === 2 && entry.every((n) => n.type === "ExportNamedDeclaration" && n.source),
    "unsupported entry module"
  );
  const pairs = entry.flatMap((n) => exportNames(n).map((e) => ({ ...e, from: n.source.value })));
  return {
    stateModule: `./${modules.state}`,
    readerModule: `./${modules.reader}`,
    cell: cell.name,
    initial,
    increment: inc.name,
    incrementRef: inc.ref,
    importFrom: imports[0].source.value,
    importedName: imported,
    importedLocal: local,
    snapshot,
    current: read.name,
    currentRef: read.ref,
    exports: pairs,
  };
}

const leanString = JSON.stringify;

function leanSource(f) {
  const exports = f.exported.map((e) => `⟨${leanString(e.local)}, ${leanString(e.exported)}⟩`);
  return `({ cell := ${leanString(f.cell)}, initial := ${f.initial}, increment := ${leanString(f.inc)}, incrementRef := ${leanString(f.incRef)}, current := ${leanString(f.read)}, currentRef := ${leanString(f.readRef)}, exports := [${exports.join(", ")}] } : SourceAst)`;
}

function leanOutput(o) {
  const exports = o.exports
    .map((e) => `⟨${leanString(e.from)}, ${leanString(e.local)}, ${leanString(e.exported)}⟩`)
    .join(", ");
  const snapshot = o.snapshot ? `some (${leanString(o.snapshot.name)}, ${leanString(o.snapshot.value)})` : "none";
  return `({ stateModule := ${leanString(o.stateModule)}, readerModule := ${leanString(o.readerModule)}, cell := ${leanString(o.cell)}, initial := ${o.initial}, increment := ${leanString(o.increment)}, incrementRef := ${leanString(o.incrementRef)}, importFrom := ${leanString(o.importFrom)}, importedName := ${leanString(o.importedName)}, importedLocal := ${leanString(o.importedLocal)}, snapshot := ${snapshot}, current := ${leanString(o.current)}, currentRef := ${leanString(o.currentRef)}, exports := [${exports}] } : OutputAst)`;
}

function digest(text) {
  return createHash("sha256").update(text).digest("hex");
}

export async function generate(sourcePath, assignmentPath, outputDir) {
  const { text, facts, modules } = await loadInput(sourcePath, assignmentPath);
  const emit = (node) => text.slice(node.start, node.end);
  const files = {
    [modules.state]: `export ${emit(facts.nodes.cell)}\n\nexport ${emit(facts.nodes.inc)}\n`,
    [modules.reader]: `import { ${facts.cell} } from "./${modules.state}";\n\nexport ${emit(facts.nodes.read)}\n`,
    "entry.mjs": `export { ${facts.inc} } from "./${modules.state}";\nexport { ${facts.read} } from "./${modules.reader}";\n`,
  };
  await mkdir(outputDir, { recursive: true });
  for (const [name, contents] of Object.entries(files))
    await writeFile(resolve(outputDir, name), contents, { flag: "wx" });
}

function execute(path, calls) {
  const operations = calls.map((call) => (call === "increment" ? "m[inc]()" : "console.log(m[read]())")).join(";");
  const code = `import(${JSON.stringify(pathToFileURL(resolve(path)).href)}).then(m => { ${operations}; });`;
  return (inc, read) => {
    const child = spawnSync(
      process.execPath,
      ["--input-type=module", "-e", `const inc=${JSON.stringify(inc)}, read=${JSON.stringify(read)}; ${code}`],
      { encoding: "utf8" }
    );
    if (child.error || child.status !== 0) throw new Error(child.stderr || String(child.error));
    return child.stdout.trim();
  };
}

function witness(sourcePath, outputPath, facts) {
  for (const calls of [["current"], ["increment", "current"]]) {
    const before = execute(sourcePath, calls)(facts.inc, facts.read);
    const after = execute(outputPath, calls)(facts.inc, facts.read);
    if (before !== after) return { result: "unsafe", calls, original: Number(before), output: Number(after) };
  }
  return { result: "unknown", reason: "Lean did not prove the graphs equal and no witness was found" };
}

export async function validate(sourcePath, assignmentPath, outputDir) {
  const { text, facts, modules } = await loadInput(sourcePath, assignmentPath);
  const names = [modules.state, modules.reader, "entry.mjs"];
  const files = Object.fromEntries(
    await Promise.all(names.map(async (name) => [name, await readFile(resolve(outputDir, name), "utf8")]))
  );
  const output = outputFacts(files[modules.state], files[modules.reader], files["entry.mjs"], modules);
  const model =
    (await readFile(new URL("./Semantics.lean", import.meta.url), "utf8")) +
    "\n" +
    (await readFile(new URL("./Effects.lean", import.meta.url), "utf8")) +
    "\n" +
    (await readFile(new URL("./Counter.lean", import.meta.url), "utf8"));
  const certificate = `
-- Source SHA256: ${digest(text)}
-- Output SHA256: ${names.map((name) => `${name}=${digest(files[name])}`).join(" ")}
def extractedSource : SourceAst := ${leanSource(facts)}
def extractedOutput : OutputAst := ${leanOutput(output)}
theorem shared_counter_semantics (state : State) :
    counterOperation state .increment = (⟨state.live + 1, state.snapshot⟩, none) ∧
    counterOperation state .incrementSnapshot = (⟨state.live, state.snapshot + 1⟩, none) ∧
    counterOperation state .readCell = (state, some state.live) ∧
    counterOperation state .readSnapshot = (state, some state.snapshot) :=
  counter_shared_operations state
#print axioms shared_counter_semantics
theorem shared_counter_schedule (initial : Nat) :
    sourceCounterInit initial = initializeCounter initial ∧
    outputCounterInit initial = initializeCounter initial :=
  counter_initialization_scheduled initial
#print axioms shared_counter_schedule
theorem source_resolves : (sourceProgram extractedSource).isSome = true := by decide
theorem output_resolves : (outputProgram extractedSource extractedOutput).isSome = true := by decide
#print axioms source_resolves
#print axioms output_resolves
theorem source_frontend_sound : directSourceProgram extractedSource = sourceProgram extractedSource := by
  cases hs : analyzeSource extractedSource with
  | none =>
    have impossible := source_resolves
    rw [sourceProgram, hs] at impossible
    cases impossible
  | some g =>
    rw [sourceProgram, hs]
    exact source_frontend_general extractedSource g hs
theorem output_frontend_sound : outputProgram extractedSource extractedOutput = directOutputProgram extractedOutput := by decide
theorem source_program_value : sourceProgram extractedSource = some ⟨${facts.initial}, .live, .live⟩ := by decide
theorem output_program_value : outputProgram extractedSource extractedOutput = some ⟨${facts.initial}, .live, .live⟩ := by decide
theorem source_startup_correct :
    (sourceStartup extractedSource).bind readyState = (sourceProgram extractedSource).map initialState := by
  rw [source_program_value]
  exact source_startup_general extractedSource _ source_program_value
theorem output_startup_correct :
    (outputStartup extractedOutput).bind readyState = (outputProgram extractedSource extractedOutput).map initialState := by
  rw [output_program_value]
  exact output_startup_general extractedSource extractedOutput _ output_program_value
theorem output_import_linked :
    (outputStartup extractedOutput).bind (fun machine => machine.importAlias) =
      some (⟨extractedOutput.readerModule, extractedOutput.importedLocal⟩,
        ⟨extractedOutput.stateModule, extractedOutput.cell⟩) := by decide
theorem extracted_operational_step (state : State) (call : Call) :
    sourceOperationalStep extractedSource state call = outputOperationalStep extractedOutput state call := by
  exact frontend_operational_general extractedSource extractedOutput ⟨${facts.initial}, .live, .live⟩
    source_program_value output_program_value state call
theorem extracted_operational_sequence (state : State) (calls : List Call) :
    runOperational (sourceOperationalStep extractedSource) state calls =
      runOperational (outputOperationalStep extractedOutput) state calls := by
  exact frontend_operational_sequence_general extractedSource extractedOutput ⟨${facts.initial}, .live, .live⟩
    source_program_value output_program_value state calls
theorem extracted_counter_certificate (calls : List Call) :
    sourceObservations extractedSource calls = outputObservations extractedSource extractedOutput calls := by
  apply frontend_observations_equivalent
  · exact source_frontend_sound
  · exact output_frontend_sound
  · decide
theorem extracted_state_certificate (calls : List Call) :
    sourceState extractedSource calls = outputState extractedSource extractedOutput calls := by
  apply frontend_state_equivalent
  · exact source_frontend_sound
  · exact output_frontend_sound
  · decide
#print axioms source_startup_correct
#print axioms output_startup_correct
#print axioms output_import_linked
#print axioms extracted_operational_sequence
#print axioms extracted_counter_certificate
#print axioms extracted_state_certificate
`;
  const proof = spawnSync(process.env.JS_MODULE_PROOF_LEAN ?? "lean", ["--trust=0", "--stdin"], {
    encoding: "utf8",
    input: model + certificate,
  });
  if (proof.error || proof.status === null)
    return { result: "unknown", reason: "Lean could not be run", detail: String(proof.error) };
  const axiomFree = (name) => proof.stdout.includes(`'${name}' does not depend on any axioms`);
  if (!["shared_counter_semantics", "shared_counter_schedule", "source_resolves", "output_resolves"].every(axiomFree)) {
    return {
      result: "unknown",
      reason: "Lean could not resolve the extracted ASTs",
      detail: proof.stderr || proof.stdout,
    };
  }
  if (proof.status !== 0) {
    const result = witness(sourcePath, resolve(outputDir, "entry.mjs"), facts);
    return result.result === "unsafe" ? result : { ...result, detail: proof.stderr || proof.stdout };
  }
  if (
    ![
      "source_startup_correct",
      "output_startup_correct",
      "output_import_linked",
      "extracted_operational_sequence",
      "extracted_counter_certificate",
      "extracted_state_certificate",
    ].every(axiomFree)
  )
    return { result: "unknown", reason: "Lean did not report axiom-free certificates" };
  const sourceAst = {
    cell: facts.cell,
    initial: facts.initial,
    inc: facts.inc,
    incRef: facts.incRef,
    read: facts.read,
    readRef: facts.readRef,
    exported: facts.exported,
  };
  return {
    result: "proved under assumptions",
    certificate,
    sourceAst,
    outputAst: output,
    sourceSha256: digest(text),
    outputSha256: Object.fromEntries(names.map((name) => [name, digest(files[name])])),
    assumptions: [
      "Only the two entry exports are called synchronously after module initialization",
      `At most ${maxInteger - Math.max(facts.initial, output.initial)} increments occur`,
      "Module identity, source text, stack traces and loading are not observed",
      "Standard module resolution loads the checked files without intervening changes",
      "The parser and AST conversion correctly map the supported JavaScript subset to the Lean AST and its modeled semantics",
    ],
  };
}
