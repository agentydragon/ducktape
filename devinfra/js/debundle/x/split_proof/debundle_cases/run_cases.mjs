// Run debundle's file-backed e2e cases (devinfra/js/debundle/e2e/cases) against the Lean checker.
//
//   node run_cases.mjs [--cases <dir>] [--filter <substring>] [--out results/cases.jsonl] [--keep <dir>]
//
// `--keep <dir>` copies each translated case (bundle, translated spec, emitted modules, checker output) to `<dir>/<id>`.
//
// A case is `main.js` (one ESM chunk), a spec, and `expect.yaml` (accept or reject per spec). For every spec entry:
//   1. translate the spec to a `lowersplit` spec: the Rust resolver supplies authored source-unit owners,
//      unclaimed statements follow `unassigned_mode`, `import_order` becomes the import priority;
//   2. `lowersplit --diagnostic --force` checks the split and writes the modules even when it rejects them;
//   3. run the original and the emitted entry in Node;
//   4. compare with the expectation.
// What the adapter cannot translate is reported as `unsupported:<cause>`, not forced. One JSON line per spec entry.
import { certify } from "./certify.mjs";
import { classify, coverageLosses, rejectKind, wanted } from "./verdict.mjs";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  constants,
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { acorn, eslintScope } from "../translation/parser.mjs";
import { parse as parseYaml } from "yaml";
import { dumpParsed } from "../translation/annotated_estree.mjs";
import { writeDeployment } from "../translation/testing/deployment_manifest.mjs";
import { parseModule, topLevelStatements } from "../translation/scope_metadata.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const resolver = path.resolve(
  process.env.DEBUNDLE_RESOLVER ?? "bazel-bin/devinfra/js/debundle/debundle_resolve_placement"
);
const lowersplit = path.resolve(process.env.LOWERSPLIT ?? path.join(here, "../lean/.lake/build/bin/lowersplit"));
const args = process.argv.slice(2);
const option = (name, fallback) => {
  const at = args.indexOf(`--${name}`);
  return at < 0 ? fallback : args[at + 1];
};
const casesRoot = path.resolve(here, option("cases", "../../../e2e/cases"));
const filter = option("filter", "");
const certification = args.includes("--certify");
const outFile = path.resolve(
  option(
    "out",
    certification
      ? path.join(process.env.TEST_TMPDIR ?? ".", "certification.jsonl")
      : path.join(here, "results/cases.jsonl")
  )
);
const keepDir = option("keep", null) && path.resolve(option("keep", null));

// ---------------------------------------------------------------- source analysis

function analyze(code) {
  const parsed = parseModule(code, acorn.parse, eslintScope);
  const moduleScope = parsed.scopes.acquire(parsed.ast, true);
  const declarations = moduleScope.variables
    .flatMap((binding) => binding.defs.filter((def) => def.type !== "ImportBinding").map((def) => def.name.start))
    .sort((a, b) => a - b);
  // The checker derives its own units from the Program. The declaration
  // positions classify our proposed units, including split declarators.
  let next = 0;
  const statements = topLevelStatements(parsed.ast).map(({ node, start, end }) => {
    while (next < declarations.length && declarations[next] < start) next++;
    return { node, declares: next < declarations.length && declarations[next] < end };
  });
  return { program: dumpParsed(parsed), statements };
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
function translate(spec, chunk, statements, placement) {
  const logical = spec.logical_modules ?? {};
  if (Object.keys(logical).length > 1) throw new Unsupported("multi-chunk spec", Object.keys(logical).join(", "));
  const authored = logical[chunk] ?? {};
  for (const key of ["chunk_analysis_options", "chunk_export_purity"])
    if (spec[key]) throw new Unsupported(`spec option ${key}`);

  for (const [module, body] of Object.entries(authored)) {
    if (body?.annotations) throw new Unsupported("annotations", module);
  }
  if (placement.units.length !== statements.length || placement.units.some((row, i) => row.ordinal !== i)) {
    throw new Error("resolver and checker disagree on source-unit ordinals");
  }
  const owner = placement.units.map((row) => row.owner);

  const mode = spec.unassigned_mode?.[chunk];
  const kind = typeof mode === "string" ? mode : mode?.kind;
  if (!["inline_in_entry", "catchall_file"].includes(kind)) throw new Unsupported(`unassigned_mode ${kind}`);

  const entry = "entry.mjs";
  const priority = [...placement.import_order];
  const files = new Map(priority.map((m) => [m, fileName(m)]));
  // `export { … }` is the entry's public surface. Under `catchall_file` the catchall takes the unclaimed declarations,
  // the unclaimed anonymous statements that precede its last statement (their effects must stay ahead of it), and
  // everything when it is the only module. Later anonymous statements stay in the entry (design.md, "Spec explicitness").
  const surface = (i) => statements[i].node.type === "ExportNamedDeclaration" && !statements[i].node.declaration;
  const swept = statements.flatMap((s, i) => (owner[i] === null && !surface(i) && s.declares ? [i] : []));
  let sinkFile = null;
  if (kind === "catchall_file") {
    // The resolver projects out owners with no claims. Only the catchall can
    // need reinsertion. Read its slot in the full authored priority, folding
    // ASCII case and trimming edge slashes as ModulePath does for YAML paths.
    const canonical = (m) => m.replace(/[A-Z]/g, (c) => c.toLowerCase()).replace(/^\/+|\/+$/g, "");
    const full = (spec.import_order?.[chunk] ?? []).map(canonical);
    const extra = full.filter((m) => !files.has(m));
    const target =
      mode.target != null ? canonical(mode.target) : swept.length ? (extra[0] ?? "residual/unhandled") : null;
    if (extra.some((m) => m !== target))
      throw new Unsupported("import_order names an unknown module", extra.join(", "));
    if (target !== null) {
      if (!files.has(target)) {
        const slot = full.indexOf(target);
        if (slot < 0) throw new Unsupported("import_order omits a module", target);
        priority.splice(slot, 0, target);
        files.set(target, fileName(target));
      }
      sinkFile = files.get(target);
    }
  }
  if (new Set([entry, ...files.values()]).size !== files.size + 1) throw new Unsupported("module filenames collide");

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
    else if (s.declares) home = sinkFile;
    else home = onlyModule || i < lastInSink ? sinkFile : entry;
    modules[home].push(`#${i}`);
  });
  return { entry, modules, order: priority.map((m) => files.get(m)) };
}

// ---------------------------------------------------------------- running

// Keep Lean subprocesses bounded while allowing the slowest valid corpus cases to finish.
const subprocessTimeoutMs = 90_000;

function run(cmd, cmdArgs, options = {}) {
  const start = performance.now();
  const r = spawnSync(cmd, cmdArgs, { encoding: "utf8", timeout: subprocessTimeoutMs, ...options });
  return {
    elapsedMs: Math.round(performance.now() - start),
    code: r.status,
    stdout: r.stdout ?? "",
    stderr: r.stderr ?? "",
    failure:
      r.error?.code === "ETIMEDOUT"
        ? "timeout"
        : r.error
          ? `spawn-${r.error.code}`
          : r.signal
            ? `signal-${r.signal}`
            : null,
  };
}

function describeProcessFailure(phase, cmd, cmdArgs, caseDir, result) {
  return {
    phase,
    command: [path.basename(cmd), ...cmdArgs.map((arg) => (path.isAbsolute(arg) ? path.relative(caseDir, arg) : arg))],
    elapsedMs: result.elapsedMs,
    timeoutMs: subprocessTimeoutMs,
    exitCode: result.code,
    cause: result.failure,
    stderrTail: result.stderr.trimEnd().slice(-2048),
  };
}

function nodeRun(phase, file, cwd) {
  const r = run(process.execPath, [file], { cwd });
  const error = (r.stderr.match(/^\w*Error: .*/m) ?? [""])[0];
  return {
    stdout: r.stdout,
    code: r.code,
    failure: r.failure,
    error,
    processFailure: r.failure ? describeProcessFailure(phase, process.execPath, [file], cwd, r) : undefined,
  };
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

function caseId(dir, specName) {
  return `${path.relative(casesRoot, dir)}/${specName.replace(/\.?spec\.yaml$|\.yaml$/, "") || "spec"}`;
}

function runCase(dir, specName, expected, id) {
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
      // Corpus specs omit the fixture input paths; preserve explicit overrides.
      mkdirSync(path.join(stage, ".fixture"), { recursive: true });
      writeFileSync(path.join(stage, ".fixture/js-files.txt"), inputs.join("\n") + "\n");
      const flat = { ...spec, inputs: { input_root: ".", js_list_path: ".fixture/js-files.txt", ...spec.inputs } };
      writeFileSync(path.join(stage, ".fixture/spec.json"), JSON.stringify(flat));
      const resolveArgs = ["--spec", ".fixture/spec.json", "--chunk", chunk, "--out", ".fixture/placement.json"];
      const result = run(resolver, resolveArgs, { cwd: stage });
      if (result.code !== 0) {
        const processFailure = describeProcessFailure("resolve-placement", resolver, resolveArgs, stage, result);
        if (result.code === 1 && !result.failure) {
          return {
            ...record,
            outcome: expectation === "reject:stderr" ? "refusal-test" : "unsupported:resolver refusal",
            detail: processFailure.stderrTail,
          };
        }
        return { ...record, outcome: "error:resolver", processFailure };
      }
      const placement = JSON.parse(readFileSync(path.join(stage, ".fixture/placement.json"), "utf8"));
      if (placement.chunk_id !== chunk || placement.source_sha256 !== createHash("sha256").update(code).digest("hex")) {
        throw new Error("resolved placement does not identify the checker input");
      }
      plan = translate(spec, chunk, analysis.statements, placement);
    } catch (error) {
      if (error instanceof Unsupported) return unsupported(error.cause, error.detail);
      throw error;
    }

    writeFileSync(path.join(stage, "bundle.json"), JSON.stringify(analysis.program));
    writeFileSync(
      path.join(stage, "lean_spec.json"),
      JSON.stringify({ entry: plan.entry, modules: plan.modules, order: plan.order })
    );
    if (certification) {
      return {
        id,
        ...certify({
          stage,
          chunkFile,
          plan,
          want: wanted(expected),
          providers: Object.values(expected.accept.runtime_files ?? {}),
          run,
          lowersplit,
          nodeRun,
        }),
      };
    }
    const checkerArgs = [
      "--diagnostic",
      writeDeployment(path.join(stage, "bundle.json"), path.join(stage, "lean_spec.json"), path.join(stage, "split")),
      path.join(stage, "split"),
      "--force",
    ];
    const checked = run(lowersplit, checkerArgs);
    if (checked.code === 3)
      return unsupported("statement kind the model rejects", checked.stderr.trim().split("\n").pop());
    if (checked.code !== 0 && checked.code !== 1)
      return {
        ...record,
        outcome: "error:lowersplit",
        detail: checked.failure ?? checked.stderr.trim().split("\n").pop(),
        processFailure: {
          ...describeProcessFailure("lowersplit", lowersplit, checkerArgs, stage, checked),
          stdoutTail: checked.stdout.trimEnd().slice(-2048),
        },
      };
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

    // The adapter writes one flat chunk into split/. Debundle's runtime_files
    // destinations are rooted at the chunk (e.g. main/provider.js).
    const runtimeFiles = Object.entries(expected.accept?.runtime_files ?? {});
    for (const [dest, src] of runtimeFiles) {
      if (!dest.startsWith(chunk + "/")) continue;
      const relative = dest.slice(chunk.length + 1);
      if (
        [relative, src].some(
          (file) => path.isAbsolute(file) || file.split("/").some((part) => !part || part === "." || part === "..")
        )
      ) {
        throw new Error(`invalid runtime fixture: ${dest}: ${src}`);
      }
      const file = path.join(stage, "split", relative);
      mkdirSync(path.dirname(file), { recursive: true });
      copyFileSync(path.join(dir, src), file, constants.COPYFILE_EXCL);
    }

    if (keepDir) {
      writeFileSync(path.join(stage, "checker_output.txt"), checked.stdout + checked.stderr);
      cpSync(stage, path.join(keepDir, id), { recursive: true });
    }
    const original = nodeRun("original", chunkFile, stage);
    const emitted = nodeRun("emitted", path.join(stage, "split", plan.entry), stage);
    const node = { original, emitted };
    const want = "accept" in expected ? wanted(expected) : null;
    const outcome = classify(expected, mine, node);
    return {
      ...record,
      outcome,
      processFailure: original.processFailure ?? emitted.processFailure,
      mine,
      split,
      node: {
        originalMatchesExpect: want === null ? null : original.stdout === want,
        emittedMatchesExpect: want === null ? null : emitted.stdout === want,
        emittedMatchesOriginal: emitted.stdout === original.stdout,
        originalCode: original.code,
        emittedCode: emitted.code,
        originalFailure: original.failure,
        emittedFailure: emitted.failure,
        emittedError: emitted.error,
      },
      unchecked:
        "accept" in expected
          ? Object.keys(expected.accept).filter(
              (k) =>
                ["source", "exports", "probes", "check_all_js"].includes(k) ||
                (k === "runtime_files" && runtimeFiles.some(([dest]) => !dest.startsWith(chunk + "/")))
            )
          : [],
    };
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
}

const regression = args.includes("--regression");
const baselinePath = path.resolve(option("baseline", path.join(here, "results/cases.jsonl")));
if ((regression || certification) && outFile === baselinePath)
  throw new Error("--regression needs an --out distinct from its baseline");
const baseline =
  regression || certification
    ? readFileSync(baselinePath, "utf8")
        .trimEnd()
        .split("\n")
        .map((line) => JSON.parse(line))
    : [];
if (certification && regression) throw new Error("diagnostic --regression and --certify are separate runs");
const selected = baseline.filter((r) => r.outcome === "agree-accept");
const cohort = new Set(selected.map((r) => r.id));
mkdirSync(path.dirname(outFile), { recursive: true });
const lines = [];
for (const dir of discover()) {
  const expectations = parseYaml(readFileSync(path.join(dir, "expect.yaml"), "utf8"));
  for (const [specName, expected] of Object.entries(expectations)) {
    const id = `${path.relative(casesRoot, dir)}/${specName}`;
    if (filter && !id.includes(filter)) continue;
    const key = caseId(dir, specName);
    if (certification && !cohort.delete(key)) continue;
    try {
      lines.push(runCase(dir, specName, expected, key));
    } catch (error) {
      if (!certification) throw error;
      lines.push({ id: key, outcome: "error:runner", detail: error.message });
    }
  }
}
writeFileSync(outFile, `${lines.map((row) => JSON.stringify(row)).join("\n")}\n`);
console.log(`${lines.length} cases -> ${outFile}`);
if (certification) {
  if (!filter && cohort.size) throw new Error("missing cohort cases: " + [...cohort].join(", "));
  const failures = lines.filter((r) => r.outcome !== "certified");
  for (const row of failures) console.error(JSON.stringify(row));
  if (!lines.length || failures.length) process.exitCode = 1;
}
if (regression) {
  const failures = lines.filter(({ outcome }) => outcome.startsWith("error:") || outcome === "unsound-accept");
  console.log(`${lines.filter(({ outcome }) => outcome.startsWith("unsupported:")).length} unsupported`);
  if (!lines.length) console.error("No cases selected.");
  for (const { id, outcome, processFailure } of failures)
    console.error(JSON.stringify({ id, outcome, processFailure }));
  const lost = coverageLosses(baseline, lines, { filtered: Boolean(filter) });
  for (const message of lost) console.error(message);
  if (!lines.length || failures.length || lost.length) process.exitCode = 1;
}
