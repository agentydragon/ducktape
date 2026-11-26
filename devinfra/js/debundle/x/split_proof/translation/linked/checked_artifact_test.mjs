import assert from "node:assert/strict";
import { cpSync, existsSync, readFileSync, readdirSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { split } from "../testing/split.mjs";
import { observe, prepare, root } from "./harness.mjs";

if (process.env.SPLIT_PROOF_ASSERT_NO_LEAN === "1") {
  assert(!(process.env.PATH ?? "").split(path.delimiter).some((dir) => existsSync(path.join(dir, "lean"))));
}

const entry = "entry.mjs";
const worker = "worker.mjs";
const readDefinition = "function read() { /* retain this text */ return seed; }";
const source = `const seed = 42;
${readDefinition}
function other() { return seed; }
export const result = read();
const decoy = other();
`;
const spec = { entry, modules: { [entry]: ["result", "decoy"], [worker]: ["seed", "read", "other"] } };
const files = prepare(root, { source, provider: "", spec });
const out = path.join(root, "out");
const requested = "all-outcomes";
const traces = [];
function check(name, status, options = {}) {
  const run = split({ source: files.sourceFile, spec: files.specFile, out, require: requested, ...options });
  assert.equal(run.verdict.status, status, run.log);
  const phases = run.stderr
    .trim()
    .split("\n")
    .filter((line) => line.startsWith('{"elapsed_ms"') || line.startsWith('{"phase"'))
    .map((line) => JSON.parse(line));
  traces.push({ name, exit: status === "certified" ? 0 : 1, verdict: run.verdict, phases });
  return { run, phases: phases.map(({ phase }) => phase) };
}

const emitted = check("emit", "certified");
assert.equal(emitted.run.verdict.guarantee, requested);
assert.deepEqual(emitted.run.files, [entry, worker]);
assert.deepEqual(emitted.phases, [
  "input_parse",
  "source_lowering",
  "checked_construction",
  "output_ast",
  "output_staging",
  "output_comparison",
  "checked_correspondence",
  "output_publication",
]);

const reread = check("check-output", "certified", { checkOutput: true });
assert.deepEqual(reread.run.verdict, emitted.run.verdict);
assert.deepEqual(
  reread.phases,
  emitted.phases.filter((phase) => !["output_staging", "output_publication"].includes(phase))
);
const entryFile = path.join(out, entry);
const workerFile = path.join(out, worker);
assert.deepEqual(observe(files.sourceFile, "result"), { result: 42 });
assert.deepEqual(observe(entryFile, "result"), { result: 42 });
const { read } = await import(pathToFileURL(workerFile).href);
assert.equal(read.name, "read");
assert.equal(Function.prototype.toString.call(read), readDefinition);

const goodEntry = readFileSync(entryFile);
writeFileSync(
  entryFile,
  `import { other as read, other } from "./${worker}";
export const result = read();
const decoy = other();
`
);
assert.deepEqual(observe(entryFile, "result"), { result: 42 });
const wrongCell = check("wrong-cell", "rejected", { checkOutput: true });
assert.match(wrongCell.run.verdict.reason, /output module entry\.mjs differs from its generated ESTree/);
writeFileSync(entryFile, goodEntry);

const wrongName = path.join(out, "wrong.mjs");
renameSync(workerFile, wrongName);
const wrongModule = check("wrong-module", "rejected", { checkOutput: true });
assert.match(wrongModule.run.verdict.reason, /output module set differs.*missing: worker\.mjs.*unexpected: wrong\.mjs/);
renameSync(wrongName, workerFile);

const unsupportedSpec = { entry, modules: { [entry]: ["seed", "result", "decoy"], [worker]: ["read", "other"] } };
const unsupportedFile = path.join(root, "unsupported.json");
writeFileSync(unsupportedFile, JSON.stringify(unsupportedSpec));
const fresh = path.join(root, "refused");
const freshRefusal = check("unsupported-fresh", "rejected", { out: fresh, spec: unsupportedFile });
assert.match(freshRefusal.run.verdict.reason, /requested placement changes the source unit order/);
assert(!existsSync(fresh));
writeFileSync(path.join(out, "keep.txt"), "existing output\n");
const snapshot = () =>
  readdirSync(out)
    .sort()
    .map((file) => [file, readFileSync(path.join(out, file))]);
const before = snapshot();
const existing = check("unsupported-existing", "rejected", { spec: unsupportedFile, allowExistingOut: true });
assert.deepEqual(existing.run.verdict, freshRefusal.run.verdict);
assert.deepEqual(snapshot(), before);
assert.deepEqual(existing.phases, ["input_parse", "source_lowering", "checked_construction"]);

const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
assert(artifacts);
writeFileSync(path.join(artifacts, "trace.json"), JSON.stringify(traces, null, 2));
writeFileSync(path.join(artifacts, "source.mjs"), source);
writeFileSync(path.join(artifacts, "spec.json"), JSON.stringify(spec, null, 2));
writeFileSync(path.join(artifacts, "unsupported.json"), JSON.stringify(unsupportedSpec, null, 2));
writeFileSync(
  path.join(artifacts, "deployment.json"),
  JSON.stringify({ entry, chunks: [{ path: entry, source: "source.mjs", spec: "spec.json" }] }, null, 2)
);
cpSync(out, path.join(artifacts, "output"), { recursive: true, filter: (file) => !file.endsWith("keep.txt") });
