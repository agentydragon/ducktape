// Broad grammar-generated ASTs supplement the targeted, scope-aware corpus.
import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { cpSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import shiftFuzzer from "shift-fuzzer";
import codegen from "shift-codegen";
import { lowerDump } from "../lowering/dump_core.mjs";

const root = process.env.TEST_TMPDIR;
assert(root);
const worker = fileURLToPath(new URL("./shift_fuzz_worker.mjs", import.meta.url));
const option = (name, fallback) => {
  const i = process.argv.lastIndexOf(`--${name}`);
  return i < 0 ? fallback : Number(process.argv[i + 1]);
};
const count = option("count", 128);
const startSeed = option("seed", 1);
assert(Number.isInteger(count) && count > 0 && Number.isInteger(startSeed) && startSeed >= 0);
function rng(seed) {
  let n = seed >>> 0;
  return () => {
    n = (n + 0x6d2b79f5) >>> 0;
    let t = Math.imul(n ^ (n >>> 15), n | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function run(rootDir, entry) {
  const child = spawnSync(
    process.execPath,
    ["--max-old-space-size=128", "--experimental-vm-modules", worker, rootDir, entry],
    {
      encoding: "utf8",
      timeout: 2000,
      maxBuffer: 1048576,
      env: {
        PATH: process.env.PATH,
        ...Object.fromEntries(Object.entries(process.env).filter(([key]) => key.startsWith("JS_BINARY__"))),
      },
    }
  );
  if (child.error?.code === "ETIMEDOUT") return { status: "timeout" };
  if (child.error || child.status !== 0) throw new Error(`module worker failed: ${child.error ?? child.stderr}`);
  try {
    return JSON.parse(child.stdout);
  } catch (error) {
    throw new Error(`module worker produced invalid JSON: ${error}; stderr=${child.stderr}`);
  }
}
const stats = {
  attempted: 0,
  parseRejected: 0,
  moduleSyntax: 0,
  small: 0,
  baselineTimeout: 0,
  baselineThrows: 0,
  unstable: 0,
  loweringRejected: 0,
  emitterRejected: 0,
  accepted: 0,
  reordered: 0,
  differing: 0,
};
const syntax = new Set(),
  acceptedSyntax = new Set();
function collect(node, target) {
  if (!node || typeof node !== "object") return;
  if (Array.isArray(node)) return node.forEach((child) => collect(child, target));
  if (typeof node.type === "string") target.add(node.type);
  for (const value of Object.values(node)) if (value && typeof value === "object") collect(value, target);
}
for (let seed = startSeed; seed < startSeed + count; seed++) {
  stats.attempted++;
  const random = rng(seed);
  const shift = shiftFuzzer.fuzzModule(new shiftFuzzer.FuzzerState({ maxDepth: 4, rng: random }));
  let source = codegen.default ? codegen.default(shift) : codegen(shift);
  let parsed;
  try {
    parsed = acorn.parse(source, { ecmaVersion: "latest", sourceType: "module" });
  } catch {
    stats.parseRejected++;
    continue;
  }
  collect(parsed, syntax);
  if (parsed.body.some((s) => /^(Import|Export)/.test(s.type))) {
    stats.moduleSyntax++;
    continue;
  }
  if (parsed.body.length < 2 || parsed.body.length > 30) {
    stats.small++;
    continue;
  }
  // Observe initialized top-level values without accessing arbitrary object
  // properties. The observer is kept last in the entry module.
  const names = parsed.body.flatMap((s) =>
    s.type === "VariableDeclaration"
      ? s.declarations.filter((d) => d.id.type === "Identifier").map((d) => d.id.name)
      : ["FunctionDeclaration", "ClassDeclaration"].includes(s.type) && s.id
        ? [s.id.name]
        : []
  );
  const unique = [...new Set(names)].slice(0, 8);
  source += `\nconsole.log("__bindings", ${unique.map((n) => `(typeof ${n} === "object" || typeof ${n} === "function" ? typeof ${n} : ${n})`).join(", ")});\n`;
  const dir = path.join(root, `seed-${seed}`);
  const out = path.join(dir, "out");
  mkdirSync(out, { recursive: true });
  writeFileSync(path.join(dir, "original.mjs"), source);
  const baseline = run(dir, "original.mjs");
  if (!baseline || baseline.status === "timeout") {
    stats.baselineTimeout++;
    continue;
  }
  if (baseline.status !== "ok") stats.baselineThrows++;
  const repeat = run(dir, "original.mjs");
  if (JSON.stringify(baseline) !== JSON.stringify(repeat)) {
    stats.unstable++;
    continue;
  }
  let dump;
  try {
    dump = lowerDump(source, acorn, eslintScope);
  } catch {
    stats.loweringRejected++;
    continue;
  }
  const total = dump.statements.length;
  const side = [];
  for (let i = 0; i < total - 1; i++) if (random() < 0.5) side.push(i);
  if (!side.length) side.push(0);
  const spec = {
    entry: "entry.mjs",
    modules: {
      "entry.mjs": Array.from({ length: total }, (_, i) => i)
        .filter((i) => !side.includes(i))
        .map((i) => `#${i}`),
      "side.mjs": side.map((i) => `#${i}`),
    },
  };
  writeFileSync(path.join(dir, "ast.json"), JSON.stringify(dump));
  writeFileSync(path.join(dir, "spec.json"), JSON.stringify(spec));
  const emitter = spawnSync(process.env.LOWERSPLIT, [path.join(dir, "ast.json"), path.join(dir, "spec.json"), out], {
    encoding: "utf8",
    timeout: 20000,
  });
  if (emitter.error) {
    if (process.env.TEST_UNDECLARED_OUTPUTS_DIR)
      cpSync(dir, path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, `emitter-error-${seed}`), { recursive: true });
    throw new Error(
      `seed ${seed}: emitter failed: ${emitter.error}; source=${JSON.stringify(source)}; stdout=${emitter.stdout}; stderr=${emitter.stderr}`
    );
  }
  if (emitter.status !== 0) {
    if (![1, 3].includes(emitter.status))
      throw new Error(`seed ${seed}: emitter exited ${emitter.status}: ${emitter.stderr}`);
    if (emitter.status === 1 && !emitter.stderr.includes("rejected; nothing written"))
      throw new Error(`seed ${seed}: unexpected emitter failure: ${emitter.stderr}`);
    stats.emitterRejected++;
    continue;
  }
  stats.accepted++;
  collect(parsed, acceptedSyntax);
  const match = emitter.stdout.match(/statement order\s+\[([^\]]*)\]/);
  if (match) {
    const order = match[1].split(",").map((x) => Number(x.trim()));
    if (order.some((item, i) => item !== i)) stats.reordered++;
  }
  const emitted = run(out, "entry.mjs");
  if (!emitted) throw new Error(`seed ${seed}: emitted tree did not complete; artifacts at ${dir}`);
  if (JSON.stringify(baseline) !== JSON.stringify(emitted)) {
    stats.differing++;
    if (process.env.TEST_UNDECLARED_OUTPUTS_DIR) {
      writeFileSync(path.join(dir, "shift-ast.json"), JSON.stringify(shift));
      writeFileSync(path.join(dir, "observed.json"), JSON.stringify({ baseline, emitted, seed }));
      cpSync(dir, path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, `wrong-accept-${seed}`), { recursive: true });
    }
    throw new Error(
      `seed ${seed}: accepted output differs: ${JSON.stringify({ baseline, emitted })}; artifacts at ${dir}`
    );
  }
}
console.log(
  JSON.stringify({ seed: startSeed, ...stats, syntax: [...syntax].sort(), acceptedSyntax: [...acceptedSyntax].sort() })
);
assert(stats.accepted > 0, "no executable generated split was accepted");
assert(stats.reordered > 0, "no accepted generated split reordered statements");
