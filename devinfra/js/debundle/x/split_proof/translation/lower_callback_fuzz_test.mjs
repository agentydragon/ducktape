// Deterministic generated cases through certified LowerSplit. Unsafe plans
// must be counted as refusals and publish no files.
import { strict as assert } from "node:assert";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { lcg32 } from "../seeded_random.mjs";
import { prepareSideSplit, run } from "./testing/diagnostic_split.mjs";
import { split } from "./testing/split.mjs";

function emit(name, code, side) {
  const fixture = prepareSideSplit(name, code, side);
  const result = split({
    ast: fixture.input,
    spec: fixture.spec,
    out: fixture.certifiedOut,
    hostContract: fixture.hostContract,
  });
  counts[result.status]++;
  return { ...fixture, dir: fixture.certifiedOut, result };
}

const counts = { certified: 0, refused: 0 };

// Two workers use disjoint case directories and inherit the same immutable runfiles.
if (process.argv[2] !== "--worker") {
  const count = Number(process.argv[2] ?? "16");
  assert(Number.isInteger(count) && count > 0);
  const runWorker = (start, end) =>
    new Promise((resolve, reject) => {
      const worker = spawn(process.execPath, [process.argv[1], "--worker", String(start), String(end)], {
        stdio: ["ignore", "pipe", "pipe"],
      });
      let stdout = "";
      let stderr = "";
      worker.stdout.on("data", (chunk) => {
        stdout += chunk;
      });
      worker.stderr.on("data", (chunk) => {
        stderr += chunk;
      });
      worker.on("error", reject);
      worker.on("close", (code) => {
        if (code !== 0) reject(new Error(`worker ${start}-${end}: ${stdout}\n${stderr}`));
        else {
          try {
            resolve(JSON.parse(stdout.trim()));
          } catch (error) {
            reject(new Error(`worker ${start}-${end} returned invalid counts: ${error}\n${stdout}\n${stderr}`));
          }
        }
      });
    });
  const parts = await Promise.all([runWorker(0, Math.ceil(count / 2)), runWorker(Math.ceil(count / 2), count)]);
  const totals = parts.reduce(
    (sum, part) => ({ certified: sum.certified + part.certified, refused: sum.refused + part.refused }),
    { certified: 0, refused: 0 }
  );
  console.log(`callback split certification: ${JSON.stringify(totals)}`);
  assert(totals.certified > 0, "the generated safe controls should include certified splits");
  assert(totals.refused > 0, "the generated unsafe controls should count certified refusals");
} else {
  // Fixed seed and stable PRNG make every failing case reproducible.
  const next = lcg32(0x91e10da5);
  const start = Number(process.argv[3]);
  const end = Number(process.argv[4]);
  for (let i = 0; i < start; i++) next();
  for (let i = start; i < end; i++) {
    const value = next() % 100000;
    const call = i % 2 ? "return () => { const captured = f; return captured(); };" : "return () => f();";
    const extra = i >= 4 ? `function spare_${i}() { return ${value}; }\n` : "";
    const callback = `callback_${i}`,
      factory = `factory_${i}`,
      result = `result_${i}`,
      alias = `alias_${i}`;
    const base = `function ${callback}() { console.log(${value}); }\nfunction ${factory}(f) { ${call} }\n${extra}`;
    const shift = extra ? 1 : 0;
    const safe = `${base}const ${result} = ${factory}(${callback});\nconsole.log("side-${value}");\n${result}();`;
    // Some plans move the factory with the side effect; others import it from entry.
    const safeSide = i % 4 >= 2 ? [1, 3 + shift] : [3 + shift];
    const emitted = emit(`safe-${i}`, safe, safeSide);
    assert.equal(emitted.result.status, "certified", `${i}: ${emitted.result.log}`);
    const original = run(process.execPath, [emitted.source]);
    const split = run(process.execPath, [path.join(emitted.dir, "entry.mjs")]);
    assert.equal(original.status, 0, original.stderr);
    assert.equal(split.status, 0, split.stderr);
    assert.equal(split.stdout, original.stdout);
    assert.equal(original.stdout, `side-${value}\n${value}\n`);

    const forms = [
      [`globalThis.cb = () => ${result}();\nconst ${result} = ${factory}(${callback});\nglobalThis.cb();`, [2, 4]],
      [
        `globalThis.cb = () => ${alias}();\nconst ${result} = ${factory}(${callback});\nconst ${alias} = ${result};\nglobalThis.cb();`,
        [2, 5],
      ],
      [
        `globalThis.cb = () => ${alias}.value();\nconst ${result} = ${factory}(${callback});\nconst ${alias} = {value: ${result}};\nglobalThis.cb();`,
        [2, 5],
      ],
      [
        `globalThis.Klass = class { x = ${result}(); };\nconst ${result} = ${factory}(${callback});\nnew globalThis.Klass();`,
        [2, 4],
      ],
    ];
    const [suffix, rawSide] = forms[i % forms.length];
    const side = rawSide.map((index) => index + shift);
    const bad = base + suffix;
    const rejected = emit(`unsafe-${i}`, bad, side);
    assert.equal(rejected.result.status, "refused", `${i}: ${rejected.result.log}`);
    assert(rejected.result.reason, `unsafe ${i}`);
    assert.equal(rejected.result.files.length, 0, `unsafe ${i}`);
    assert(!existsSync(path.join(rejected.dir, "entry.mjs")), `unsafe ${i} emitted a module`);
  }
  console.log(JSON.stringify(counts));
}
