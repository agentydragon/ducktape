import assert from "node:assert/strict";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
import { parse } from "acorn";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import { split } from "../testing/split.mjs";
import { generate } from "astring";

export const root = process.env.TEST_TMPDIR;
assert(root, "TEST_TMPDIR is required");
const certifiedByFixture = new Map();
const previewCounts = { certified: 0, refused: 0 };
const fixtureKey = (source, spec) => `${path.resolve(source)}\n${path.resolve(spec)}`;
process.on("exit", () => {
  const trials = previewCounts.certified + previewCounts.refused;
  if (trials) process.stderr.write(`forced preview certification: ${JSON.stringify(previewCounts)}\n`);
});

export function caseShard() {
  const count = Number(process.env.TEST_TOTAL_SHARDS ?? 1);
  const index = Number(process.env.TEST_SHARD_INDEX ?? 0);
  assert(Number.isInteger(count) && count > 0, "invalid TEST_TOTAL_SHARDS");
  assert(Number.isInteger(index) && index >= 0 && index < count, "invalid TEST_SHARD_INDEX");
  if (process.env.TEST_SHARD_STATUS_FILE) writeFileSync(process.env.TEST_SHARD_STATUS_FILE, "");

  // Every shard visits the same case names in the same order.
  const names = new Set();
  return (name) => {
    assert(!names.has(name), `duplicate case ${name}`);
    const selected = names.size % count === index;
    names.add(name);
    if (selected) console.log(`Case ${name}`);
    return selected;
  };
}

export const id = (name) => ({ type: "Identifier", name });
export const literal = (value) => ({ type: "Literal", value });
export const binary = (operator, left, right) => ({ type: "BinaryExpression", operator, left, right });
export const unary = (operator, argument) => ({ type: "UnaryExpression", operator, argument, prefix: true });
export const array = (elements) => ({ type: "ArrayExpression", elements });
export const member = (object, property, computed = false) => ({
  type: "MemberExpression",
  object,
  property,
  computed,
  optional: false,
});

export function primitive(value) {
  // Astring's literal serializer loses signed zero and non-finite numbers.
  if (value === undefined || Number.isNaN(value) || value === Infinity) return id(String(value));
  if (typeof value === "number" && (value < 0 || Object.is(value, -0))) return unary("-", primitive(-value));
  return literal(value);
}

export const call = (callee, args = []) => ({ type: "CallExpression", callee, arguments: args });
export const conditional = (test, consequent, alternate) => ({
  type: "ConditionalExpression",
  test,
  consequent,
  alternate,
});
export const expression = (expression) => ({ type: "ExpressionStatement", expression });
export const returns = (argument) => ({ type: "ReturnStatement", argument });
export const throws = (argument) => ({ type: "ThrowStatement", argument });
export const block = (body) => ({ type: "BlockStatement", body });
export const tryStatement = (body, handler = null, finalizer = null) => ({
  type: "TryStatement",
  block: body,
  handler,
  finalizer,
});
export const catchClause = (param, body) => ({ type: "CatchClause", param, body });
export const arrow = (params, body) => ({
  type: "ArrowFunctionExpression",
  params,
  body,
  expression: body.type !== "BlockStatement",
});
export const functionExpression = (params, body) => ({ type: "FunctionExpression", id: null, params, body });
export const variables = (kind, bindings) => ({
  type: "VariableDeclaration",
  kind,
  declarations: bindings.map(([name, init]) => ({ type: "VariableDeclarator", id: id(name), init })),
});
export const variable = (kind, name, init) => variables(kind, [[name, init]]);
export const property = (key, value) => ({
  type: "Property",
  key: id(key),
  value,
  kind: "init",
  method: false,
  shorthand: false,
  computed: false,
});
export const importNamed = (source, bindings) => ({
  type: "ImportDeclaration",
  source: literal(source),
  specifiers: bindings.map(([imported, local]) => ({
    type: "ImportSpecifier",
    imported: id(imported),
    local: id(local),
  })),
});
export const exportNamed = (bindings) => ({
  type: "ExportNamedDeclaration",
  declaration: null,
  specifiers: bindings.map(([local, exported]) => ({
    type: "ExportSpecifier",
    local: id(local),
    exported: id(exported),
  })),
  source: null,
});
export const program = (body) => generate({ type: "Program", sourceType: "module", body });

const probe = path.join(root, "linked_programs_observe.mjs");
writeFileSync(
  probe,
  `try {
  const module = await import(process.argv[2]);
  const names = JSON.parse(process.argv[3]);
  const values = Object.fromEntries(names.map((name) => {
    const exported = module[name];
    const value = typeof exported === "function" ? exported() : exported;
    return [name, typeof value === "function" ? value() : value];
  }));
  console.log(JSON.stringify({ values }));
} catch (error) {
  console.log(JSON.stringify({ error: error.name }));
}
`
);

export function observe(file, exportNames) {
  const names = Array.isArray(exportNames) ? exportNames : [exportNames];
  const result = spawnSync(process.execPath, [probe, pathToFileURL(file).href, JSON.stringify(names)], {
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.error?.message ?? result.stdout + result.stderr);
  const observed = JSON.parse(result.stdout.trim());
  if (observed.error) return observed;
  return Object.fromEntries(names.map((name) => [name, observed.values[name]]));
}

export function lower(source, spec, out, extra = [], scope = null) {
  const options = { source, spec, out };
  for (let i = 0; i < extra.length; i++) {
    const flag = extra[i];
    if (flag === "--check-output") options.checkOutput = true;
    else if (flag === "--structural") options.structural = true;
    else if (flag === "--require") options.require = extra[++i];
    else if (flag === "--host-contract") options.hostContract = extra[++i];
    else if (flag === "--deployment-origin") options.deploymentOrigin = extra[++i];
    else if (flag === "--deployment-preset") options.deploymentPreset = extra[++i];
    else if (flag === "--deployment-import-map") options.deploymentImportMap = extra[++i];
    else throw new Error(`unsupported certified split option: ${flag}`);
  }
  const result = split(options);
  const cached = certifiedByFixture.get(fixtureKey(source, spec)) ?? [];
  cached.push({ options, result });
  certifiedByFixture.set(fixtureKey(source, spec), cached);
  if (process.env.SPLIT_PROOF_TIMINGS === "1") process.stderr.write(result.stderr);
  if (scope === null) {
    if (result.status === "certified") {
      assert(
        [
          "closed-declarations",
          "linked-programs",
          "linked-slice-order-kept",
          "linked-slice-moves",
          "linked-slice-certified-bindings",
          "linked-slice-heritage",
        ].includes(result.scope),
        `unexpected linked strategy scope ${result.scope}: ${result.log}`
      );
    }
  } else assert.equal(result.scope, scope, result.log);
  return { verdict: result.verdict, log: result.log };
}

export function prepare(dir, { source, provider, extras = {}, sourceName = "source.mjs", spec }) {
  mkdirSync(dir, { recursive: true });
  const sourceFile = path.join(dir, sourceName);
  const providerFile = path.join(dir, "provider.mjs");
  const specFile = path.join(dir, "spec.json");
  writeFileSync(sourceFile, source);
  writeFileSync(providerFile, provider);
  for (const [name, contents] of Object.entries(extras)) writeFileSync(path.join(dir, name), contents);
  writeFileSync(specFile, JSON.stringify(spec, null, 2));
  return { sourceFile, providerFile, specFile };
}

export function threeUnitFixture(name, source, entry = "entry.mjs", withProvider = false) {
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source: withProvider ? `import "./provider.mjs";\n${source}` : source,
    provider: withProvider ? "export {};\n" : "",
    spec: withProvider
      ? {
          entry,
          modules: { "worker.mjs": ["#1"], [entry]: ["#0", "#2", "#3"] },
          order: ["worker.mjs"],
          sequential: true,
        }
      : { entry, modules: { "worker.mjs": ["#0"], [entry]: ["#1", "#2"] }, order: ["worker.mjs"], sequential: true },
  });
  const out = path.join(dir, "out");
  return {
    ...files,
    out,
    run: (...opts) => {
      if (!opts.includes("--check-output")) rmSync(out, { recursive: true, force: true });
      return lower(files.sourceFile, files.specFile, out, opts).verdict;
    },
  };
}

// Compute evaluation order from the emitted requests, including the cycle a fixed
// schedule can introduce when the second provider reads the first provider.
export function moduleEvaluationOrder(directory, entry) {
  const visited = new Set();
  const order = [];
  function visit(file) {
    if (visited.has(file)) return;
    visited.add(file);
    const ast = parse(readFileSync(path.join(directory, file), "utf8"), {
      sourceType: "module",
      ecmaVersion: "latest",
    });
    for (const node of ast.body) {
      if (node.type === "ImportDeclaration") visit(path.join(path.dirname(file), node.source.value));
    }
    order.push(file);
  }
  visit(entry);
  return order;
}

// Linked counterexample tests explicitly exercise the legacy model diagnostic.
// The certified result is counted before the test-only preview is requested.
export function forcePreview(files, directory) {
  const key = fixtureKey(files.sourceFile, files.specFile);
  const cached = certifiedByFixture.get(key)?.find(({ options }) => Object.keys(options).length === 3);
  const certification = cached?.result ?? split({ source: files.sourceFile, spec: files.specFile, out: directory });
  previewCounts[certification.status]++;

  if (certification.status === "certified") {
    if (cached && cached.options.out !== directory) {
      assert(!existsSync(directory), `certified output destination already exists: ${directory}`);
      for (const file of certification.files) {
        const source = path.join(cached.options.out, file);
        const target = path.join(directory, file);
        mkdirSync(path.dirname(target), { recursive: true });
        copyFileSync(source, target);
      }
    }
    assert(certification.files.length > 0, certification.log);
    return certification;
  }

  assert.equal(certification.status, "refused");
  assert.equal(certification.files.length, 0, certification.log);
  const preview = spawnSync(
    process.env.SPLIT_PREVIEW,
    [writeDeployment(files.sourceFile, files.specFile, directory), directory, "--force"],
    { encoding: "utf8" }
  );
  assert([0, 1].includes(preview.status), preview.error?.message ?? preview.stdout + preview.stderr);
  assert.match(preview.stdout, /model diagnostic: /, preview.stdout + preview.stderr);
  return certification;
}

// Exercise the public command against both a fresh path and the exact modules
// produced by the separate diagnostic command. split enforces exit 1 on refusal.
export function assertNoCompiledRule(files, out) {
  const assertRefusal = ({ verdict, log }) => {
    assert.equal(verdict.status, "rejected", log);
    assert.equal(verdict.reason_code, "no_compiled_rule", log);
    assert.equal(verdict.guarantee, null, log);
  };

  assertRefusal(lower(files.sourceFile, files.specFile, out));
  assert(!existsSync(out), "refusal created a fresh destination");

  forcePreview(files, out);
  const snapshot = () =>
    readdirSync(out)
      .sort()
      .map((name) => {
        const file = path.join(out, name);
        const { ino, mtimeNs } = statSync(file, { bigint: true });
        return [name, ino, mtimeNs, readFileSync(file)];
      });
  const before = snapshot();
  const options = { source: files.sourceFile, spec: files.specFile, out, allowExistingOut: true };
  assertRefusal(split(options));
  assert.deepEqual(snapshot(), before, "refusal changed the existing output");
  assertRefusal(split({ ...options, checkOutput: true }));
  assert.deepEqual(snapshot(), before, "check-output changed the existing output");
}

export function runSplit(name, fixture) {
  const dir = path.join(root, name);
  const files = prepare(dir, fixture);
  const out = path.join(dir, "out");
  return { dir, files, out, ...lower(files.sourceFile, files.specFile, out) };
}

const entry = "entry.mjs";
const worker = "worker.mjs";
const workerModules = { [worker]: ["#0"], [entry]: ["#1", "#2"] };

export function sequentialSplit(name, source, modules = workerModules, order = [worker], provider = "") {
  return runSplit(name, { source, provider, spec: { entry, modules, order, sequential: true } });
}

const owner = "owner.mjs";
const reader = "reader.mjs";

export function ownerReaderSplit(
  name,
  source,
  modules = { [owner]: ["#0"], [reader]: ["#1"], [entry]: ["#2"] },
  order = [owner, reader]
) {
  return sequentialSplit(name, source, modules, order);
}

// Move the first declaration to its own module and observe the result exported by the entry.
export function workerSplit(name, source, entryUnits = ["#1"]) {
  return runSplit(name, {
    source,
    provider: "",
    spec: { entry, modules: { [worker]: ["#0"], [entry]: entryUnits }, order: [worker] },
  });
}

export function assertCertified(verdict, log) {
  assert.equal(verdict.status, "certified", log);
  assert(["all-outcomes", "normal-completion"].includes(verdict.guarantee), log);
}

export function assertCertifiedResult({ files, out, verdict, log }, result) {
  assertCertified(verdict, log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result }, file);
  }
}

export function assertRejected({ verdict, log, out }, reason) {
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, reason, log);
  assert(!existsSync(out), out);
}

// Order-kept slices prove the stronger all-outcomes result for these cases.
export function assertRejectedOrOrderKeptSlice({ verdict, log, out }, reason) {
  if (verdict.status === "certified" && verdict.scope === "linked-slice-order-kept") {
    assert.equal(verdict.guarantee, "all-outcomes", log);
    assert(existsSync(out), out);
    return true;
  }
  assertRejected({ verdict, log, out }, reason);
  return false;
}

export function assertRefusedOrOrderKeptSlice(verdict, out, reason, log = JSON.stringify(verdict)) {
  return assertRejectedOrOrderKeptSlice({ verdict, out, reason, log }, reason);
}

export function assertWorkerResult(name, source, expected, entryUnits) {
  assertCertifiedResult(workerSplit(name, source, entryUnits), expected);
}

export function observedProgram(initializer, result) {
  return program([
    variable("const", "worker", initializer),
    {
      type: "ExportNamedDeclaration",
      declaration: variable("const", "result", result),
      specifiers: [],
      source: null,
    },
  ]);
}

export function structuralSplit(name, source, modules, provider = "") {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider, spec: { entry, modules } });
  const out = path.join(dir, "out");
  const run = (scope, ...args) =>
    lower(files.sourceFile, files.specFile, out, ["--structural", ...args], scope).verdict;
  return { ...files, dir, out, run };
}

export function structuralFixture(name, source, moved, kept, provider = "") {
  return structuralSplit(name, source, { [entry]: kept, "scroll.mjs": moved }, provider);
}

// A diagnostic preview must exhibit the specific behavior change that certification rejected.
export function assertRejectedResultChange(test, before, after) {
  assert.equal(test.run("linked-structural").status, "rejected");
  assert(!existsSync(test.out));
  const preview = path.join(test.dir, "preview");
  forcePreview(test, preview);
  assert.deepEqual(observe(test.sourceFile, "result"), before);
  assert.deepEqual(observe(path.join(preview, entry), "result"), after);
}
