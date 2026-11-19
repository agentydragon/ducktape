// Does the model's module evaluation order (`Plan.evalOrder`, used by `OrderOk`) match what Node does?
//
//   node check.mjs [graphs] [seed] [--control]
//
// Random import graphs: 2–8 modules, 0–3 imports each, cycles and unreachable modules included, imports in random
// order. Every module prints its name when its body runs. Node's printed order must equal the model's.
// `--control` writes every module's imports in reverse order for Node only: mismatches must appear.
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const evalorder = path.join(path.dirname(fileURLToPath(import.meta.url)), "../../lean/.lake/build/bin/evalorder");
const graphs = Number(process.argv[2] ?? 500);
let seed = Number(process.argv[3] ?? 1);
const control = process.argv.includes("--control");
const random = () => {
  seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
  return seed / 4294967296;
};

let mismatches = 0;
let cyclic = 0;
for (let g = 0; g < graphs; g++) {
  const n = 2 + Math.floor(random() * 7);
  const names = Array.from({ length: n }, (_, i) => `m${i}.mjs`);
  const imports = Object.fromEntries(
    names.map((name, i) => {
      const k = Math.floor(random() * 4);
      return [
        name,
        Array.from({ length: k }, () => names[Math.floor(random() * n)]).filter(
          (x, j, all) => x !== name && all.indexOf(x) === j
        ),
      ];
    })
  );
  const entry = names[Math.floor(random() * n)];
  const dir = mkdtempSync(path.join(tmpdir(), "es_order_"));
  try {
    for (const name of names) {
      const order = control ? [...imports[name]].reverse() : imports[name];
      const body = order.map((src) => `import "./${src}";`).join("\n");
      writeFileSync(path.join(dir, name), `${body}\nconsole.log(${JSON.stringify(name)});\n`);
    }
    writeFileSync(path.join(dir, "graph.json"), JSON.stringify({ entry, modules: imports }));
    const node = spawnSync("node", [path.join(dir, entry)], { encoding: "utf8" })
      .stdout.trim()
      .split("\n");
    const model = spawnSync(evalorder, [path.join(dir, "graph.json")], { encoding: "utf8" })
      .stdout.trim()
      .split("\n");
    if (JSON.stringify(node) !== JSON.stringify(model)) {
      mismatches++;
      console.log(
        "MISMATCH",
        JSON.stringify({ entry, imports }),
        "\n  node ",
        node.join(" "),
        "\n  model",
        model.join(" ")
      );
    }
    // a graph has a cycle if some module can reach itself
    const reach = (from, seen = new Set()) =>
      imports[from].forEach((x) => seen.has(x) || (seen.add(x), reach(x, seen))) || seen;
    if (names.some((m) => reach(m).has(m))) cyclic++;
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}
console.log(`${graphs} random import graphs (${cyclic} with a cycle): ${mismatches} mismatches`);
process.exitCode = mismatches ? 1 : 0;
if (control) process.exitCode = mismatches ? 0 : 1;
