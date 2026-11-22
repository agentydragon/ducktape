// Run debundle's file-backed e2e cases (devinfra/js/debundle/e2e/cases) against the Lean checker.
//
//   node run_cases.mjs [--cases <dir>] [--filter <substring>] [--out results/cases.jsonl] [--keep <dir>]
//
// `--keep <dir>` copies each translated case (bundle, translated spec, emitted modules, checker output) to `<dir>/<id>`.
//
// A case is `main.js` (one ESM chunk), a spec, and `expect.yaml` (accept or reject per spec). For every spec entry:
//   1. translate the spec to a `lowersplit` spec: binding selectors and hole-free matches become statement numbers,
//      unclaimed statements follow `unassigned_mode`, `import_order` becomes the import priority;
//   2. `lowersplit --force` checks the split and writes the modules even when it rejects them;
//   3. run the original and the emitted entry in Node;
//   4. compare with the expectation.
// What the adapter cannot translate is reported as `unsupported:<cause>`, not forced. One JSON line per spec entry.
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { parse as parseYaml } from "yaml";
import { dumpParsed } from "../lowering/dump_core.mjs";
import { parseModule, topLevelStatements } from "../translation/scope_metadata.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const lowersplit = path.join(here, "../lean/.lake/build/bin/lowersplit");
const args = process.argv.slice(2);
const option = (name, fallback) => {
  const at = args.indexOf(`--${name}`);
  return at < 0 ? fallback : args[at + 1];
};
const casesRoot = path.resolve(here, option("cases", "../../../e2e/cases"));
const filter = option("filter", "");
const outFile = path.resolve(option("out", path.join(here, "results/cases.jsonl")));
const keepDir = option("keep", null) && path.resolve(option("keep", null));

// ---------------------------------------------------------------- source analysis

const HOLES = /\b(ANYTHING|EXPR|STMT|ARGS|STMT_LIST|ARRAY_ELEMENTS|CASE_REST|DECLARATORS|SEQ_EXPRS)(_\w+)?\b/;

/** Position-free AST for comparing a template with a statement; literals keep their values. */
function shape(node) {
  return JSON.stringify(node, (k, v) => {
    if (["start", "end", "loc", "range", "raw"].includes(k)) return undefined;
    return typeof v === "bigint" ? String(v) : v;
  });
}

function boundNames(pattern, out) {
  switch (pattern?.type) {
    case "Identifier":
      out.push(pattern.name);
      break;
    case "ObjectPattern":
      for (const p of pattern.properties) boundNames(p.type === "RestElement" ? p.argument : p.value, out);
      break;
    case "ArrayPattern":
      for (const e of pattern.elements) if (e) boundNames(e, out);
      break;
    case "AssignmentPattern":
      boundNames(pattern.left, out);
      break;
    case "RestElement":
      boundNames(pattern.argument, out);
      break;
  }
}

/** Names declared by `var` inside blocks, loops and `try` (they live in the module scope). */
function varNames(node, out) {
  if (!node || typeof node !== "object") return;
  switch (node.type) {
    case "VariableDeclaration":
      if (node.kind === "var") for (const d of node.declarations) boundNames(d.id, out);
      break;
    case "BlockStatement":
      for (const s of node.body) varNames(s, out);
      break;
    case "IfStatement":
      varNames(node.consequent, out);
      varNames(node.alternate, out);
      break;
    case "ForStatement":
      varNames(node.init, out);
      varNames(node.body, out);
      break;
    case "ForInStatement":
    case "ForOfStatement":
      varNames(node.left, out);
      varNames(node.body, out);
      break;
    case "WhileStatement":
    case "DoWhileStatement":
    case "LabeledStatement":
      varNames(node.body, out);
      break;
    case "TryStatement":
      varNames(node.block, out);
      varNames(node.handler?.body, out);
      varNames(node.finalizer, out);
      break;
    case "SwitchStatement":
      for (const c of node.cases) for (const s of c.consequent) varNames(s, out);
      break;
  }
}

/** Top-level names a statement declares (an exported declaration declares its names too). */
function declaredNames(statement) {
  const out = [];
  const declaration = statement.type.startsWith("Export") ? statement.declaration : statement;
  if (declaration?.type === "VariableDeclaration") for (const d of declaration.declarations) boundNames(d.id, out);
  else if ((declaration?.type === "FunctionDeclaration" || declaration?.type === "ClassDeclaration") && declaration.id)
    out.push(declaration.id.name);
  else if (!statement.type.startsWith("Export")) varNames(statement, out);
  return out;
}

function analyze(code) {
  const parsed = parseModule(code, acorn.parse, eslintScope);
  // These ordinals propose a placement. The checker derives its units from the Program.
  const statements = topLevelStatements(parsed.ast, code).map(({ node }) => ({
    node,
    declared: declaredNames(node),
    shape: shape(node),
  }));
  return { program: dumpParsed(parsed), statements };
}

/** The one statement a hole-free template parses to, as a shape string; `null` when it is not one statement. */
function templateShape(text) {
  try {
    const body = acorn.parse(text, { ecmaVersion: "latest", sourceType: "module", ranges: true }).body;
    return body.length === 1 ? shape(body[0]) : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- translation

class Unsupported extends Error {
  constructor(cause, detail = "") {
    super(`${cause}: ${detail}`);
    this.cause = cause;
    this.detail = detail;
  }
}

const fileName = (module) => `${module.replace(/[^A-Za-z0-9_]+/g, "_")}.mjs`;

/**
 * A `lowersplit` spec for one debundle spec of one chunk.
 * Statement numbers are explicit, so every statement is claimed exactly once.
 */
function translate(spec, chunk, statements) {
  const logical = spec.logical_modules ?? {};
  if (Object.keys(logical).length > 1) throw new Unsupported("multi-chunk spec", Object.keys(logical).join(", "));
  const authored = logical[chunk] ?? {};
  for (const key of ["chunk_analysis_options", "chunk_export_purity"])
    if (spec[key]) throw new Unsupported(`spec option ${key}`);

  const owner = new Array(statements.length).fill(null);
  const claim = (i, module, why) => {
    if (owner[i] !== null && owner[i] !== module)
      throw new Unsupported(
        "statement claimed by two modules",
        `${why}: statement ${i} is in ${owner[i]} and ${module}`
      );
    owner[i] = module;
  };

  for (const [module, body] of Object.entries(authored)) {
    if (!body || typeof body !== "object") throw new Unsupported("module entry is not a mapping", module);
    if (body.annotations) throw new Unsupported("annotations", module);
    for (const member of body.members ?? []) {
      const kinds = Object.keys(member.selector ?? {});
      if (kinds.join() !== "binding") throw new Unsupported(`selector ${kinds.join("+") || "(none)"}`, module);
      const { name, kind } = member.selector.binding;
      if (kind === "import_specifier") throw new Unsupported("import specifier binding", name);
      const hits = statements.flatMap((s, i) => (s.declared.includes(name) ? [i] : []));
      if (hits.length === 0) throw new Unsupported("binding not found", name);
      for (const i of hits) claim(i, module, `binding ${name}`);
    }
    const templates = [
      ...(body.source_matches ?? []).map((m) => ({ text: m.match, what: "source_matches" })),
      ...(body.anonymous_statements ?? []).map((a) => ({
        text: a.match ?? a.source_match?.match,
        what: "anonymous_statements",
      })),
    ];
    for (const { text, what } of templates) {
      if (typeof text !== "string") throw new Unsupported(`${what} without a template`, module);
      if (HOLES.test(text)) throw new Unsupported(`${what} with syntactic holes`, module);
      const wanted = templateShape(text);
      if (wanted === null) throw new Unsupported(`${what} template is not one statement`, module);
      const hits = statements.flatMap((s, i) => (s.shape === wanted ? [i] : []));
      if (hits.length === 0) throw new Unsupported(`${what} matches no statement`, module);
      for (const i of hits) claim(i, module, what);
    }
  }

  const mode = spec.unassigned_mode?.[chunk];
  const kind = typeof mode === "string" ? mode : mode?.kind;
  if (!["inline_in_entry", "catchall_file"].includes(kind)) throw new Unsupported(`unassigned_mode ${kind}`);

  const entry = "entry.mjs";
  const files = new Map(Object.keys(authored).map((m) => [m, fileName(m)]));
  const priority = (spec.import_order?.[chunk] ?? []).slice();
  // `export { … }` is the entry's public surface. Under `catchall_file` the catchall takes the unclaimed declarations,
  // the unclaimed anonymous statements that precede its last statement (their effects must stay ahead of it), and
  // everything when it is the only module. Later anonymous statements stay in the entry (design.md, "Spec explicitness").
  const surface = (i) => statements[i].node.type === "ExportNamedDeclaration" && !statements[i].node.declaration;
  const swept = statements.flatMap((s, i) => (owner[i] === null && !surface(i) && s.declared.length > 0 ? [i] : []));
  let sinkFile = null;
  if (kind === "catchall_file") {
    const target =
      mode.target ?? (swept.length > 0 ? (priority.find((m) => !files.has(m)) ?? "residual/unhandled") : null);
    if (target) {
      if (!files.has(target)) files.set(target, fileName(target));
      sinkFile = files.get(target);
    }
  }
  for (const m of priority) if (!files.has(m)) throw new Unsupported("import_order names an unknown module", m);
  if (new Set(priority).size !== priority.length) throw new Unsupported("import_order repeats a module");
  for (const m of files.keys()) if (!priority.includes(m)) throw new Unsupported("import_order omits a module", m);

  const inSink = [
    ...swept,
    ...statements.flatMap((_, i) => (owner[i] !== null && files.get(owner[i]) === sinkFile ? [i] : [])),
  ];
  const lastInSink = Math.max(-1, ...inSink);
  const onlyModule = sinkFile !== null && files.size === 1;
  const modules = { [entry]: [] };
  for (const f of files.values()) modules[f] = [];
  statements.forEach((s, i) => {
    let home;
    if (owner[i] !== null) home = files.get(owner[i]);
    else if (surface(i) || sinkFile === null) home = entry;
    else if (s.declared.length > 0) home = sinkFile;
    else home = onlyModule || i < lastInSink ? sinkFile : entry;
    modules[home].push(`#${i}`);
  });
  return { entry, modules, order: priority.map((m) => files.get(m)) };
}

// ---------------------------------------------------------------- running

function run(cmd, cmdArgs, options = {}) {
  const r = spawnSync(cmd, cmdArgs, { encoding: "utf8", timeout: 30_000, ...options });
  return { code: r.status, stdout: r.stdout ?? "", stderr: r.stderr ?? "", timedOut: r.error?.code === "ETIMEDOUT" };
}

function nodeRun(file, cwd) {
  const r = run("node", [file], { cwd });
  const error = (r.stderr.match(/^\w*Error: .*/m) ?? [""])[0];
  return { stdout: r.stdout, code: r.code, error };
}

const rejectKind = (expected) =>
  "atomic_conflict" in expected.reject ? "atomic_conflict" : "cycle" in expected.reject ? "cycle" : "stderr";

/** What the emitted entry should print. The adapter renames nothing, so it is the original's output. */
const wanted = (expected) => expected.accept.original_stdout ?? expected.accept.stdout;

/**
 * `agree-*`: same verdict. `strict-*`: I reject what debundle accepts (`runs` when my plan, written anyway, prints
 * the expected output; `breaks` when it does not, so debundle's splitter is better than mine). `unsound-accept`: I
 * accept and the emitted modules print something else. `lenient`: I accept what debundle rejects (the output is the
 * same on this run). `refusal-test`: the case checks debundle's error text, which has no counterpart here.
 */
function classify(expected, mine, node) {
  if ("accept" in expected) {
    const same = node.emitted === wanted(expected);
    if (mine.verdict === "accept") return same ? "agree-accept" : "unsound-accept";
    return same ? "strict-but-runs" : "strict-and-breaks";
  }
  if (rejectKind(expected) === "stderr") return "refusal-test";
  if (mine.verdict === "reject") return "agree-reject";
  return node.emitted === node.original ? "lenient" : "unsound-accept";
}

function discover() {
  const found = [];
  const walk = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name === "expect.yaml") found.push(dir);
    }
  };
  walk(casesRoot);
  return found.sort();
}

function jsFiles(dir) {
  const out = [];
  const walk = (d) => {
    for (const entry of readdirSync(d, { withFileTypes: true })) {
      const full = path.join(d, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith(".js")) out.push(path.relative(dir, full));
    }
  };
  walk(dir);
  return out.sort();
}

function runCase(dir, specName, expected) {
  const id = `${path.relative(casesRoot, dir)}/${specName.replace(/\.?spec\.yaml$|\.yaml$/, "") || "spec"}`;
  const base = { id, area: path.relative(casesRoot, dir).split(path.sep)[0] };
  const expectation = "accept" in expected ? "accept" : `reject:${rejectKind(expected)}`;
  const record = { ...base, expect: expectation };
  const unsupported = (cause, detail = "") => ({ ...record, outcome: `unsupported:${cause}`, detail });

  const inputs = existsSync(path.join(dir, "js-files.txt"))
    ? readFileSync(path.join(dir, "js-files.txt"), "utf8").split("\n").filter(Boolean)
    : jsFiles(dir);
  if (inputs.length !== 1) return unsupported("several input chunks", inputs.join(", "));
  const chunkFile = inputs[0];
  const chunk = chunkFile.replace(/\.js$/, "");

  const stage = mkdtempSync(path.join(tmpdir(), "debundle_case_"));
  try {
    cpSync(dir, stage, { recursive: true });
    if (!existsSync(path.join(stage, "package.json")))
      writeFileSync(path.join(stage, "package.json"), '{"type":"module"}\n');
    const code = readFileSync(path.join(stage, chunkFile), "utf8");
    let analysis;
    try {
      analysis = analyze(code);
    } catch (error) {
      return unsupported("input does not parse as a module", error.message);
    }
    const spec = parseYaml(readFileSync(path.join(dir, specName), "utf8"));
    let plan;
    try {
      plan = translate(spec, chunk, analysis.statements);
    } catch (error) {
      if (error instanceof Unsupported) return unsupported(error.cause, error.detail);
      throw error;
    }

    writeFileSync(path.join(stage, "bundle.json"), JSON.stringify(analysis.program));
    writeFileSync(
      path.join(stage, "lean_spec.json"),
      JSON.stringify({ entry: plan.entry, modules: plan.modules, order: plan.order })
    );
    const checked = run(lowersplit, [
      path.join(stage, "bundle.json"),
      path.join(stage, "lean_spec.json"),
      path.join(stage, "split"),
      "--force",
    ]);
    if (checked.code === 3)
      return unsupported("statement kind the model rejects", checked.stderr.trim().split("\n").pop());
    if (checked.code !== 0 && checked.code !== 1)
      return { ...record, outcome: "error:lowersplit", detail: checked.stderr.trim().split("\n").pop() };
    const failed = [...checked.stdout.matchAll(/^\s+FAIL\s+(.*)$/gm)].map(
      ([, what]) =>
        what.match(/^statement \d+ is an `(Import|Export)/)?.[1].concat("OutsideEntry") ?? what.split(/\s+/)[0]
    );
    const mine = { verdict: checked.code === 0 ? "accept" : "reject", failed };
    const placed = Object.entries(plan.modules).filter(([file]) => file !== plan.entry);
    const split = {
      statements: analysis.statements.length,
      modules: placed.filter(([, held]) => held.length > 0).length,
      moved: placed.reduce((sum, [, held]) => sum + held.length, 0),
    };

    if (keepDir) {
      writeFileSync(path.join(stage, "checker_output.txt"), checked.stdout + checked.stderr);
      cpSync(stage, path.join(keepDir, id), { recursive: true });
    }
    const original = nodeRun(chunkFile, stage);
    const emitted = nodeRun(path.join(stage, "split", plan.entry), stage);
    const node = { original: original.stdout, emitted: emitted.stdout, emittedError: emitted.error };
    const want = "accept" in expected ? wanted(expected) : null;
    const outcome = classify(expected, mine, node);
    return {
      ...record,
      outcome,
      mine,
      split,
      node: {
        originalMatchesExpect: want === null ? null : original.stdout === want,
        emittedMatchesExpect: want === null ? null : emitted.stdout === want,
        emittedMatchesOriginal: emitted.stdout === original.stdout,
        emittedError: emitted.error,
      },
      unchecked:
        "accept" in expected
          ? Object.keys(expected.accept).filter((k) =>
              ["source", "exports", "probes", "check_all_js", "runtime_files"].includes(k)
            )
          : [],
    };
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
}

mkdirSync(path.dirname(outFile), { recursive: true });
const lines = [];
for (const dir of discover()) {
  const expectations = parseYaml(readFileSync(path.join(dir, "expect.yaml"), "utf8"));
  for (const [specName, expected] of Object.entries(expectations)) {
    const id = `${path.relative(casesRoot, dir)}/${specName}`;
    if (filter && !id.includes(filter)) continue;
    lines.push(JSON.stringify(runCase(dir, specName, expected)));
  }
}
writeFileSync(outFile, `${lines.join("\n")}\n`);
console.log(`${lines.length} cases -> ${outFile}`);
