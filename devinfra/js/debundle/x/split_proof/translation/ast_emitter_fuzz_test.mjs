// Independent AST-generated programs through the production LowerSplit emitter.
import { strict as assert } from "node:assert";
import { spawn, spawnSync } from "node:child_process";
import { cpSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { dump } from "./parser.mjs";
import { topLevelStatements } from "./scope_metadata.mjs";
import { generateAstBundle } from "./testing/ast_generator.mjs";
import { nodeRecordingHostContract, split } from "./testing/split.mjs";
import { lcg32 } from "../seeded_random.mjs";

const tmp = process.env.TEST_TMPDIR;
assert(tmp);
const diagnostics = process.env.TEST_UNDECLARED_OUTPUTS_DIR ?? tmp;
const run = (binary, args) => spawnSync(binary, args, { encoding: "utf8" });
function random(seed) {
  const next = lcg32(seed);
  return (n) => next() % n;
}
function preserve(dir, seed, round, plan, message) {
  const target = path.join(diagnostics, `ast-emitter-seed-${seed}-round-${round}-plan-${plan}`);
  cpSync(dir, target, { recursive: true });
  writeFileSync(path.join(target, "failure.txt"), message);
  const count = Math.max(2, round + 1);
  throw new Error(
    `${message}\nReproduce round=${round} plan=${plan}: bbr test ` +
      `//devinfra/js/debundle/x/split_proof/translation:ast_emitter_fuzz_test ` +
      `--test_arg=${seed} --test_arg=${count}\nFiles: ${target}`
  );
}

if (process.argv[2] !== "--worker") {
  const seed = Number(process.argv[2] ?? "1337");
  const count = Number(process.argv[3] ?? "16");
  assert(Number.isInteger(seed) && Number.isInteger(count) && count > 1);
  const worker = (start, end) =>
    new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [process.argv[1], "--worker", String(seed), String(start), String(end)], {
        stdio: ["ignore", "pipe", "pipe"],
      });
      let stdout = "",
        stderr = "";
      child.stdout.on("data", (data) => {
        stdout += data;
      });
      child.stderr.on("data", (data) => {
        stderr += data;
      });
      child.on("error", reject);
      child.on("close", (code) => {
        if (code !== 0) reject(new Error(`worker ${start}-${end}: ${stdout}\n${stderr}`));
        else resolve(JSON.parse(stdout));
      });
    });
  const parts = await Promise.all([worker(0, Math.ceil(count / 2)), worker(Math.ceil(count / 2), count)]);
  const totals = {
    seed,
    programs: count,
    plans: count * 2,
    certified: 0,
    certifiedNontrivial: 0,
    certifiedRefused: 0,
  };
  for (const part of parts)
    for (const key of ["certified", "certifiedNontrivial", "certifiedRefused"]) totals[key] += part[key];
  console.log(JSON.stringify(totals));
  assert.equal(totals.certified + totals.certifiedRefused, totals.plans);
  assert(totals.certifiedRefused > 0, "the broad emitter corpus should count certified refusals");
  assert(totals.certifiedNontrivial >= 2, "fewer than two nontrivial certified plans");
} else {
  const seed = Number(process.argv[3]);
  const start = Number(process.argv[4]);
  const end = Number(process.argv[5]);
  const stats = { certified: 0, certifiedNontrivial: 0, certifiedRefused: 0 };
  for (let round = start; round < end; round++) {
    const choose = random((seed ^ 0xa341316c ^ Math.imul(round, 0x9e3779b1)) >>> 0);
    const { source } = generateAstBundle(seed, round);
    const program = dump(source);
    const units = topLevelStatements(program);
    const original = path.join(tmp, `original-${round}.mjs`);
    writeFileSync(original, source);
    const expected = run(process.execPath, [original]);
    if (expected.status !== 0) {
      const dir = path.join(tmp, `original-failure-${round}`);
      mkdirSync(dir, { recursive: true });
      writeFileSync(path.join(dir, "source.mjs"), source);
      writeFileSync(path.join(dir, "runtime.log"), expected.stdout + expected.stderr);
      preserve(dir, seed, round, "original", "generated original did not run successfully");
    }
    for (let plan = 0; plan < 2; plan++) {
      const dir = path.join(tmp, `case-${round}-${plan}`);
      mkdirSync(dir, { recursive: true });
      const modules = { "entry.mjs": [], "side-a.mjs": [], "side-b.mjs": [] };
      const names = Object.keys(modules);
      const prefixLength = 1 + choose(2);
      units.forEach(({ node: unit }, i) => {
        // One randomized source prefix has a chance to preserve execution order;
        // the other plan scatters statements freely among modules.
        const target = unit.type.startsWith("Export")
          ? 0
          : plan === 0
            ? i < prefixLength
              ? 1
              : 0
            : choose(names.length);
        modules[names[target]].push(`#${i}`);
      });
      const order = choose(2) ? ["side-a.mjs", "side-b.mjs"] : ["side-b.mjs", "side-a.mjs"];
      writeFileSync(path.join(dir, "source.mjs"), source);
      writeFileSync(path.join(dir, "ast.json"), JSON.stringify(program));
      writeFileSync(path.join(dir, "spec.json"), JSON.stringify({ entry: "entry.mjs", modules, order }));
      const certifiedOut = path.join(dir, "certified");
      const certificate = split({
        ast: path.join(dir, "ast.json"),
        spec: path.join(dir, "spec.json"),
        out: certifiedOut,
        hostContract: nodeRecordingHostContract,
        timeoutMs: 60_000,
      });
      if (certificate.status === "certified") {
        stats.certified++;
        if (modules["entry.mjs"].length && (modules["side-a.mjs"].length || modules["side-b.mjs"].length))
          stats.certifiedNontrivial++;
        const checkedOutput = run(process.execPath, [path.join(certifiedOut, "entry.mjs")]);
        if (checkedOutput.status !== expected.status || checkedOutput.stdout !== expected.stdout) {
          writeFileSync(
            path.join(dir, "certified-runtime.log"),
            `original:\n${expected.stdout}\noutput:\n${checkedOutput.stdout}\n${checkedOutput.stderr}`
          );
          preserve(dir, seed, round, plan, "certified output differs from original");
        }
      } else {
        stats.certifiedRefused++;
        assert.equal(certificate.files.length, 0, certificate.log);
      }
    }
  }
  console.log(JSON.stringify(stats));
}
