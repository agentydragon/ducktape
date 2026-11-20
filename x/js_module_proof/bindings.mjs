import { createRequire } from "node:module";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const { parse } = createRequire(import.meta.url)(process.env.JS_MODULE_PROOF_ACORN ?? "acorn");
const parseModule = (text) => parse(text, { ecmaVersion: "latest", sourceType: "module" }).body;
const check = (condition, reason) => {
  if (!condition) throw new Error(`unknown: ${reason}`);
};
const identifier = (node) => {
  check(node?.type === "Identifier", "expected an identifier");
  return node.name;
};
const list = (values) => `[${values.join(", ")}]`;
const str = JSON.stringify;

// This is a syntax-directed conversion to the Lean AST, shared by source and
// output extraction. It does not compare output against generator text.
function statement(node) {
  if (node.type === "VariableDeclaration") {
    check(node.kind === "let" && node.declarations.length === 1, "expected a single let declaration");
    const d = node.declarations[0];
    const name = identifier(d.id);
    check(
      name !== "globalThis" && d.init?.type === "Literal" && Number.isSafeInteger(d.init.value) && d.init.value >= 0,
      "expected a nonnegative safe integer initializer"
    );
    return { kind: "variable", name, value: d.init.value };
  }
  if (node.type === "FunctionDeclaration") {
    check(
      !node.async && !node.generator && node.params.length === 0 && node.body.body.length === 1,
      "expected a simple function"
    );
    const name = identifier(node.id);
    check(name !== "globalThis", "globalThis cannot be locally declared");
    const stmt = node.body.body[0];
    const expr = stmt.type === "ExpressionStatement" && stmt.expression;
    check(expr?.type === "AssignmentExpression" && expr.operator === "+=", "expected an addition assignment");
    return { kind: "function", name, target: identifier(expr.left), source: identifier(expr.right) };
  }
  const expr = node.type === "ExpressionStatement" && node.expression;
  check(expr?.type === "CallExpression" && !expr.optional, "expected a supported statement");
  if (expr.callee.type === "Identifier") {
    check(expr.arguments.length === 0, "function calls must have no arguments");
    return { kind: "call", name: identifier(expr.callee) };
  }
  const member = expr.callee;
  check(
    member.type === "MemberExpression" &&
      !member.computed &&
      !member.optional &&
      identifier(member.object) === "globalThis" &&
      identifier(member.property) === "record" &&
      expr.arguments.length === 1,
    "expected globalThis.record(binding)"
  );
  return { kind: "record", name: identifier(expr.arguments[0]) };
}

function exportNames(node) {
  check(
    node.specifiers.every((s) => s.type === "ExportSpecifier" && identifier(s.local) === identifier(s.exported)),
    "export aliases are unsupported"
  );
  return node.specifiers.map((s) => identifier(s.local));
}

function sourceAst(text) {
  const statements = [];
  const exports = [];
  for (const node of parseModule(text)) {
    if (node.type === "ExportNamedDeclaration" && !node.declaration && !node.source) exports.push(...exportNames(node));
    else statements.push({ ast: statement(node), source: text.slice(node.start, node.end) });
  }
  check(
    statements.length > 0 && statements.length <= 20 && exports.length > 0,
    "expected up to twenty statements and public exports"
  );
  return { statements, exports };
}

function readSpec(spec, count) {
  check(
    spec?.kind === "bindings" && spec.modules && typeof spec.modules === "object" && !Array.isArray(spec.modules),
    "expected a bindings assignment"
  );
  const names = Object.keys(spec.modules);
  check(
    names.length > 0 &&
      names.length <= 12 &&
      names.includes(spec.entry) &&
      names.every((name) => /^[A-Za-z][A-Za-z0-9_-]*\.mjs$/.test(name)),
    "invalid module names"
  );
  const assigned = [];
  for (const name of names) {
    const module = spec.modules[name];
    check(
      module && Array.isArray(module.imports) && Array.isArray(module.statements),
      "expected imports and statement indices"
    );
    check(
      module.imports.every((dep) => names.includes(dep)) &&
        module.statements.every((i, n, a) => Number.isInteger(i) && i >= 0 && i < count && (!n || a[n - 1] < i)),
      "invalid imports or statement indices"
    );
    assigned.push(...module.statements);
  }
  check(assigned.length === count && new Set(assigned).size === count, "assign every statement once");
  return names;
}

const declarationName = (item) => (["variable", "function"].includes(item.kind) ? item.name : null);
function references(item) {
  if (item.kind === "function") return [item.target, item.source];
  if (item.kind === "call" || item.kind === "record") return [item.name];
  return [];
}

function generateFiles(source, spec, names) {
  const defs = new Map();
  for (const name of names)
    for (const index of spec.modules[name].statements) {
      const binding = declarationName(source.statements[index].ast);
      if (binding) {
        check(!defs.has(binding), "duplicate declaration");
        defs.set(binding, name);
      }
    }
  const referencesByModule = Object.fromEntries(
    names.map((name) => [
      name,
      [...new Set(spec.modules[name].statements.flatMap((i) => references(source.statements[i].ast)))],
    ])
  );
  const neededExports = new Set(source.exports);
  for (const name of names)
    for (const binding of referencesByModule[name]) {
      const owner = defs.get(binding);
      check(owner, `missing definition: ${binding}`);
      if (owner !== name) {
        check(spec.modules[name].imports.includes(owner), `missing dependency for ${binding}`);
        neededExports.add(binding);
      }
    }
  const result = {};
  for (const name of names) {
    const module = spec.modules[name];
    const imports = module.imports.map((dep) => {
      const bindings = referencesByModule[name].filter((binding) => defs.get(binding) === dep);
      return bindings.length
        ? `import { ${bindings.join(", ")} } from ${str(`./${dep}`)};`
        : `import ${str(`./${dep}`)};`;
    });
    const declarations = module.statements.map((index) => {
      const item = source.statements[index];
      const binding = declarationName(item.ast);
      const shouldExport =
        binding && neededExports.has(binding) && (name !== spec.entry || source.exports.includes(binding));
      return `${shouldExport ? "export " : ""}${item.source}`;
    });
    const reexports =
      name === spec.entry
        ? source.exports
            .filter((binding) => defs.get(binding) !== name)
            .map((binding) => {
              check(defs.has(binding), `missing exported definition: ${binding}`);
              return `export { ${binding} } from ${str(`./${defs.get(binding)}`)};`;
            })
        : [];
    result[name] = [...imports, ...declarations, ...reexports].join("\n") + "\n";
  }
  return result;
}

function outputAst(text, names) {
  const imports = [];
  const exports = [];
  const statements = [];
  const dependency = (node) => {
    check(typeof node.source?.value === "string" && node.source.value.startsWith("./"), "unsupported module path");
    const dep = names.indexOf(node.source.value.slice(2));
    check(dep >= 0 && (!node.attributes || node.attributes.length === 0), "unrecognized dependency");
    return dep;
  };
  for (const node of parseModule(text)) {
    if (node.type === "ImportDeclaration") {
      const dep = dependency(node);
      const locals = node.specifiers.map((s) => {
        check(
          s.type === "ImportSpecifier" && identifier(s.imported) === identifier(s.local),
          "import aliases are unsupported"
        );
        return identifier(s.local);
      });
      imports.push({ dep, locals, reexports: [] });
    } else if (node.type === "ExportNamedDeclaration") {
      if (node.declaration) {
        const ast = statement(node.declaration);
        check(declarationName(ast), "unsupported exported declaration");
        exports.push(declarationName(ast));
        statements.push(ast);
      } else if (node.source) imports.push({ dep: dependency(node), locals: [], reexports: exportNames(node) });
      else exports.push(...exportNames(node));
    } else statements.push(statement(node));
  }
  return { imports, exports, statements };
}

const leanStrings = (values) => list(values.map(str));
function leanStatement(item) {
  switch (item.kind) {
    case "variable":
      return `.variable ${str(item.name)} ${item.value}`;
    case "function":
      return `.function ${str(item.name)} ${str(item.target)} ${str(item.source)}`;
    case "call":
      return `.call ${str(item.name)}`;
    case "record":
      return `.record ${str(item.name)}`;
  }
  throw new Error("unknown statement");
}
function leanModule(module, assignment) {
  const imports = module.imports.map(
    (i) =>
      `({ dependency := ${i.dep}, locals := ${leanStrings(i.locals)}, reexports := ${leanStrings(i.reexports)} } : BindingImport)`
  );
  return `({ imports := ${list(imports)}, exports := ${leanStrings(module.exports)}, statements := ${list(module.statements.map(leanStatement))}, assignment := ${list(assignment)} } : BindingModule)`;
}

async function load(sourcePath, specPath) {
  const text = await readFile(sourcePath, "utf8");
  const source = sourceAst(text);
  const spec = JSON.parse(await readFile(specPath, "utf8"));
  const names = readSpec(spec, source.statements.length);
  return { source, spec, names, text };
}

export async function generateBindings(sourcePath, specPath, out) {
  const { source, spec, names } = await load(sourcePath, specPath);
  const files = generateFiles(source, spec, names);
  await mkdir(out, { recursive: true });
  for (const name of names) await writeFile(resolve(out, name), files[name], { flag: "wx" });
}

function execute(path) {
  const script = `const events=[];globalThis.record=v=>events.push(v);import(${str(pathToFileURL(resolve(path)).href)}).then(m=>console.log(JSON.stringify({events,exports:Object.keys(m)})));`;
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", script], { encoding: "utf8" });
  if (child.error || child.status !== 0) throw new Error(child.stderr || String(child.error));
  return JSON.parse(child.stdout);
}

export async function validateBindings(sourcePath, specPath, out) {
  const { source, spec, names, text } = await load(sourcePath, specPath);
  const files = await Promise.all(names.map((name) => readFile(resolve(out, name), "utf8")));
  const actual = files.map((text) => outputAst(text, names));
  const terms = actual.map((module, i) => leanModule(module, spec.modules[names[i]].statements));
  const digest = (content) => createHash("sha256").update(content).digest("hex");
  const prefix = `\n-- Source SHA256: ${digest(text)}\n-- Output SHA256: ${names.map((name, i) => `${name}=${digest(files[i])}`).join(" ")}\ndef bindingSource : List BindingStmt := ${list(source.statements.map((item) => leanStatement(item.ast)))}\ndef bindingExports : List String := ${leanStrings(source.exports)}\ndef bindingModules : List BindingModule := ${list(terms)}\ndef bindingEntry : Nat := ${names.indexOf(spec.entry)}\ntheorem binding_graph_valid : modulesValid bindingSource bindingExports bindingModules bindingEntry = true := by decide\ntheorem binding_source_ready : (sourceBindingState bindingSource).isSome = true := by decide\ntheorem binding_output_ready : (outputBindingState bindingSource bindingModules bindingEntry).isSome = true := by decide\n#print axioms binding_graph_valid\n#print axioms binding_source_ready\n#print axioms binding_output_ready\n`;
  const same = `\ntheorem binding_initialization : sourceBindingState bindingSource = outputBindingState bindingSource bindingModules bindingEntry := by decide\ntheorem binding_public_table : publicFunctionTable bindingSource bindingExports = publicFunctionTable (outputDefinitions bindingModules) bindingExports := by decide\ntheorem binding_public_table_ready : (publicFunctionTable bindingSource bindingExports).isSome = true := by decide\ntheorem binding_future_calls (calls : List String) : (sourceBindingState bindingSource).bind (fun state => (publicFunctionTable bindingSource bindingExports).bind (fun table => runExportedCalls table state calls)) = (outputBindingState bindingSource bindingModules bindingEntry).bind (fun state => (publicFunctionTable (outputDefinitions bindingModules) bindingExports).bind (fun table => runExportedCalls table state calls)) := by\n  exact binding_calls_preserved _ _ _ _ binding_initialization binding_public_table calls\n#print axioms binding_initialization\n#print axioms binding_public_table\n#print axioms binding_public_table_ready\n#print axioms binding_future_calls\n`;
  const model = (
    await Promise.all(
      ["Semantics.lean", "Effects.lean", "Bindings.lean"].map((name) =>
        readFile(new URL(`./${name}`, import.meta.url), "utf8")
      )
    )
  ).join("\n");
  const runLean = (certificate) =>
    spawnSync(process.env.JS_MODULE_PROOF_LEAN ?? "lean", ["--trust=0", "--stdin"], {
      encoding: "utf8",
      input: model + certificate,
    });
  const has = (result, name) => result.stdout.includes(`'${name}' does not depend on any axioms`);
  const proof = runLean(prefix + same);
  if (proof.error || proof.status === null)
    return { result: "unknown", reason: "Lean could not be run", detail: String(proof.error) };
  if (!["binding_graph_valid", "binding_source_ready", "binding_output_ready"].every((name) => has(proof, name)))
    return {
      result: "unknown",
      reason: "Lean could not validate the extracted bindings, statements or module graph",
      detail: proof.stdout || proof.stderr,
    };
  if (
    proof.status === 0 &&
    ["binding_initialization", "binding_public_table", "binding_public_table_ready", "binding_future_calls"].every(
      (name) => has(proof, name)
    )
  )
    return {
      result: "proved under assumptions",
      certificate: prefix + same,
      assumptions: [
        "The Acorn-to-Lean AST conversion faithfully represents the actual source and output files",
        "The modeled module loading, linking and execution match ECMAScript for the supported syntax",
        "All binding values and intermediate sums remain nonnegative safe integers",
        "globalThis.record is a stable synchronous host function which returns normally without reentry or asynchronous effects",
        "Only initialization calls, their record observations, and calls through the entry exports after successful initialization are observed; loading, reflection, source text and identity are excluded",
      ],
    };
  const different = `\ntheorem binding_difference : sourceBindingState bindingSource ≠ outputBindingState bindingSource bindingModules bindingEntry := by decide\n#print axioms binding_difference\n`;
  const mismatch = runLean(prefix + different);
  if (mismatch.status !== 0 || !has(mismatch, "binding_difference"))
    return {
      result: "unknown",
      reason: "Lean did not prove a state difference",
      detail: mismatch.stdout || mismatch.stderr,
    };
  const original = execute(sourcePath);
  const output = execute(resolve(out, spec.entry));
  if (JSON.stringify(original) === JSON.stringify(output))
    return { result: "unknown", reason: "modeled difference was not reproduced in Node" };
  return { result: "unsafe", original, output, certificate: prefix + different };
}
