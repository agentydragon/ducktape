// Compare Plan.evalOrder with the order in which Node executes ES module bodies.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { lcg32 } from "../seeded_random.mjs";

const evalorder = path.resolve(process.env.EVALORDER);
const graphs = Number(process.argv[2] ?? 40);
if (!Number.isSafeInteger(graphs) || graphs < 1) throw new Error("expected a positive graph count");
const initialSeed = Number(process.argv[3] ?? 1);
const next = lcg32(initialSeed);
const random = () => next() / 2 ** 32;

function stdout(command, ...args) {
  const result = spawnSync(command, args, { encoding: "utf8", timeout: 15_000 });
  if (result.error) throw result.error;
  assert.equal(result.status, 0, `${command} ${args}: ${result.signal ?? result.stderr}`);
  return result.stdout;
}

const batch = [];
function compare(entry, modules, { reverseImports = false, batched = false } = {}) {
  const dir = mkdtempSync(path.join(process.env.TEST_TMPDIR ?? tmpdir(), "es_order_"));
  try {
    for (const [name, imports] of Object.entries(modules)) {
      const order = reverseImports ? [...imports].reverse() : imports;
      const body = order.map((src) => `import "./${src}";`).join("\n");
      writeFileSync(path.join(dir, name), `${body}\nconsole.log(${JSON.stringify(name)});\n`);
    }
    const node = stdout(process.execPath, path.join(dir, entry));
    if (batched) {
      batch.push({ graph: { entry, modules }, node });
      return;
    }
    const graph = path.join(dir, "graph.json");
    writeFileSync(graph, JSON.stringify({ entry, modules }));
    return { node, lean: stdout(evalorder, graph) };
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
  compare(entry, modules, { batched: true });
}
const scratch = mkdtempSync(path.join(process.env.TEST_TMPDIR ?? tmpdir(), "graph_batch_"));
try {
  const file = path.join(scratch, "graphs.json");
  writeFileSync(file, JSON.stringify(batch.map(({ graph }) => graph)));
  const orders = JSON.parse(stdout(evalorder, "--batch", file));
  assert.equal(orders.length, batch.length);
  batch.forEach(({ graph, node }, i) => {
    assert.deepEqual(
      orders[i],
      node.trimEnd().split("\n"),
      `seed ${initialSeed}, graph ${i}: ${JSON.stringify(graph)}`
    );
  });
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
console.log(`${graphs} random graphs matched Node (seed ${initialSeed}); reversed-import control differed`);
