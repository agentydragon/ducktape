// Compare Plan.evalOrder with the order in which Node executes ES module bodies.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const evalorder = path.resolve(process.env.EVALORDER);
const graphs = Number(process.argv[2] ?? 40);
if (!Number.isSafeInteger(graphs) || graphs < 1) throw new Error("expected a positive graph count");
const initialSeed = Number(process.argv[3] ?? 1);
let seed = initialSeed;
const random = () => {
  seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
  return seed / 2 ** 32;
};

function stdout(command, file) {
  const result = spawnSync(command, [file], { encoding: "utf8", timeout: 15_000 });
  if (result.error) throw result.error;
  assert.equal(result.status, 0, `${command} ${file}: ${result.signal ?? result.stderr}`);
  return result.stdout;
}

function compare(entry, modules, { reverseImports = false } = {}) {
  const dir = mkdtempSync(path.join(process.env.TEST_TMPDIR ?? tmpdir(), "es_order_"));
  try {
    for (const [name, imports] of Object.entries(modules)) {
      const order = reverseImports ? [...imports].reverse() : imports;
      const body = order.map((src) => `import "./${src}";`).join("\n");
      writeFileSync(path.join(dir, name), `${body}\nconsole.log(${JSON.stringify(name)});\n`);
    }
    const graph = path.join(dir, "graph.json");
    writeFileSync(graph, JSON.stringify({ entry, modules }));
    return { node: stdout(process.execPath, path.join(dir, entry)), lean: stdout(evalorder, graph) };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

const cycle = {
  "entry.mjs": ["a.mjs", "b.mjs"],
  "a.mjs": ["entry.mjs"],
  "b.mjs": [],
  "unreachable.mjs": [],
};
const expected = "a.mjs\nb.mjs\nentry.mjs\n";
const original = compare("entry.mjs", cycle);
assert.equal(original.node, expected);
assert.equal(original.lean, expected);
const reversed = compare("entry.mjs", cycle, { reverseImports: true });
assert.equal(reversed.node, "b.mjs\na.mjs\nentry.mjs\n");
assert.equal(reversed.lean, expected);

for (let g = 0; g < graphs; g++) {
  const n = 2 + Math.floor(random() * 7);
  const names = Array.from({ length: n }, (_, i) => `m${i}.mjs`);
  const modules = Object.fromEntries(
    names.map((name) => {
      const k = Math.floor(random() * 4);
      const imports = new Set(Array.from({ length: k }, () => names[Math.floor(random() * n)]));
      imports.delete(name);
      return [name, [...imports]];
    })
  );
  const entry = names[Math.floor(random() * n)];
  const result = compare(entry, modules);
  assert.equal(result.lean, result.node, `seed ${initialSeed}, graph ${g}: ${JSON.stringify({ entry, modules })}`);
}
console.log(`${graphs} random graphs matched Node (seed ${initialSeed}); reversed-import control differed`);
