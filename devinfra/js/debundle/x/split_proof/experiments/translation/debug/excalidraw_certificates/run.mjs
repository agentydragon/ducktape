import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { promisify } from "node:util";
import { coreOf, dumpOf } from "../../split_dump.mjs";

const execute = promisify(execFile);
const root = process.env.EXCALIDRAW_OUTPUTS.split(" ").find((p) => p.endsWith("/bundle"));
assert(root, "missing esbuild output directory");
const out = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
assert(out, "run this investigation through its Bazel target");
const save = (file, value) => writeFileSync(path.join(out, file), JSON.stringify(value, null, 2) + "\n");
const files = readdirSync(root, { recursive: true })
  .filter((p) => p.endsWith(".js"))
  .sort();
assert(files.length > 0);
const chunks = [];
for (const file of files) {
  const source = readFileSync(path.join(root, file), "utf8");
  const row = { file, bytes: Buffer.byteLength(source), sha256: createHash("sha256").update(source).digest("hex") };
  try {
    const core = coreOf(source, { external: true, census: true });
    Object.assign(row, {
      statements: core.numbered,
      modeled: core.units - core.opaque.length,
      opaque: core.opaque,
      opaqueBodies: core.opaqueBodies,
      refused: core.refused,
    });
  } catch (e) {
    row.frontendError = e.message;
  }
  chunks.push(row);
}
save("chunks.json", chunks);
console.log(`Inventoried ${chunks.length} chunks; ${chunks.filter((c) => c.frontendError).length} frontend failures`);
const metadataFile = process.env.EXCALIDRAW_OUTPUTS.split(" ").find((p) => p.endsWith("_metadata.json"));
const metadata = JSON.parse(readFileSync(metadataFile, "utf8"));
const containing = (suffix) => {
  const match = Object.entries(metadata.outputs).find(
    ([file, info]) => file.endsWith(".js") && Object.keys(info.inputs).some((p) => p.endsWith(suffix))
  );
  assert(match, `no chunk contains ${suffix}`);
  return chunks.find((c) => c.file === path.basename(match[0]));
};
const selected = [
  ...new Set([
    chunks.find((c) => c.file === "main.js"),
    containing("fractional-indexing/src/index.js"),
    containing("roughjs/bin/rough.js"),
    [...chunks].filter((c) => c.statements > 10).sort((a, b) => a.bytes - b.bytes)[0],
  ]),
];
assert(selected.every(Boolean));
async function run(binary, args, stem) {
  try {
    const r = await execute(binary, args, {
      timeout: 180000,
      maxBuffer: 64 * 1024 * 1024,
      env: { ...process.env, EFFECTS_WHY: "1" },
    });
    writeFileSync(`${stem}.stdout`, r.stdout);
    writeFileSync(`${stem}.stderr`, r.stderr);
    return { status: 0, stdout: r.stdout, stderr: r.stderr };
  } catch (e) {
    writeFileSync(`${stem}.stdout`, e.stdout ?? "");
    writeFileSync(`${stem}.stderr`, e.stderr ?? e.message);
    return {
      status: e.code,
      signal: e.signal,
      killed: e.killed,
      stdout: e.stdout ?? "",
      stderr: e.stderr ?? e.message,
    };
  }
}
const results = [];
for (const chunk of selected) {
  console.log(`Probing ${chunk.file} (${chunk.bytes} bytes, ${chunk.statements} statements)`);
  const dir = path.join(out, chunk.file.replaceAll("/", "_"));
  mkdirSync(dir, { recursive: true });
  const source = readFileSync(path.join(root, chunk.file), "utf8");
  let dump, core;
  try {
    dump = dumpOf(source);
    core = coreOf(source, { external: true, census: true });
  } catch (e) {
    results.push({ file: chunk.file, frontendError: e.message });
    continue;
  }
  const astFile = path.join(dir, "ast.json"),
    programFile = path.join(dir, "program.json"),
    fpFile = path.join(dir, "footprints.jsonl");
  writeFileSync(astFile, JSON.stringify(dump));
  writeFileSync(
    programFile,
    JSON.stringify({
      entry: "main",
      modules: [{ name: "main", imports: [], exports: core.exports, ids: core.ids, tokens: core.tokens }],
    })
  );
  const analysis = await run(process.env.LOWER, [astFile], path.join(dir, "lower"));
  const row = {
    file: chunk.file,
    analysis: { status: analysis.status, signal: analysis.signal, killed: analysis.killed },
    probes: [],
  };
  results.push(row);
  if (analysis.status !== 0) {
    save("results.json", results);
    continue;
  }
  writeFileSync(fpFile, analysis.stdout);
  const fps = analysis.stdout
    .trim()
    .split("\n")
    .map((s) => JSON.parse(s));
  assert.equal(fps.length, dump.statements.length);
  row.effects = fps.filter((f) => f.effect).length;
  const pinned = dump.statements.map((s) => /^(Import|Export)/.test(s.ast.type));
  const n = dump.statements.length;
  // These are bounded diagnostic probes, not inferred source-module boundaries.
  const probes = [
    ["identity", () => false],
    ["prefix", (i) => i < Math.floor(n / 2)],
    ["alternating", (i) => i % 2 === 0],
  ];
  for (const [name, move] of probes) {
    const modules = { "entry.mjs": [], "part.mjs": [] };
    for (let i = 0; i < n; i++) modules[!pinned[i] && move(i) ? "part.mjs" : "entry.mjs"].push(`#${i}`);
    if (modules["part.mjs"].length === 0) delete modules["part.mjs"];
    const specFile = path.join(dir, `${name}.json`);
    writeFileSync(specFile, JSON.stringify({ entry: "entry.mjs", modules }));
    const r = await run(
      process.env.LOWERSPLIT,
      [
        astFile,
        specFile,
        path.join(dir, name),
        "--footprints",
        fpFile,
        "--corejs",
        programFile,
        "--plan",
        path.join(dir, `${name}.plan.json`),
        "--move-report",
        path.join(dir, `${name}.moves.json`),
      ],
      path.join(dir, name)
    );
    const probe = {
      name,
      status: r.status,
      signal: r.signal,
      killed: r.killed,
      slice: r.stdout.match(/^ {2}proved slice: (.*)$/m)?.[1],
      failures: r.stdout.split("\n").filter((line) => line.includes("FAIL")),
      stderr: r.stderr,
    };
    if (r.status === 0 || r.status === 1) {
      const report = JSON.parse(readFileSync(path.join(dir, `${name}.moves.json`), "utf8"));
      if (report.error) probe.moveReportError = report.error;
      else {
        probe.invertedPairs = report.invertedPairs;
        probe.failedPairs = report.failedPairs;
        probe.failedPairExamples = report.failedPairExamples;
        probe.pairwiseFailedPairs = report.pairwiseFailedPairs;
        probe.certifiedClassHeritage = report.certifiedClassHeritage;
        if (probe.certifiedClassHeritage) {
          assert(probe.pairwiseFailedPairs > 0);
          assert.equal(probe.failedPairs, 0);
        }
        assert(report.failedPairs <= report.invertedPairs);
        assert.equal(new Set(report.statements.map((s) => s.id)).size, report.statements.length);
        assert(report.statements.every((s) => s.id >= 0 && s.id < n));
        const rejected = probe.slice?.match(/(\d+) of the (\d+) do not/);
        if (rejected) {
          assert.equal(report.failedPairs, Number(rejected[1]));
          assert.equal(report.invertedPairs, Number(rejected[2]));
        }
        if (probe.slice?.startsWith("moves") || probe.slice?.startsWith("order kept")) {
          assert.equal(report.footprintsOfSyntax, true);
          assert.equal(report.failedPairs, 0);
        }
        if (name === "identity")
          row.categories = Object.fromEntries(
            ["opaque", "inert", "allocatesOnly", "bindingSummary", "bindingCertificate"].map((key) => [
              key,
              report.statements.filter((s) => s[key]).length,
            ])
          );
      }
    }
    row.probes.push(probe);
    console.log(JSON.stringify({ file: chunk.file, ...probe }));
    save("results.json", results);
  }
}
console.log("Results are in chunks.json, results.json, and per-chunk checker logs in test outputs.");
