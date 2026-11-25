// How much of a bundle the CoreJS model's front end takes into the model's syntax, and how much it leaves opaque to
// the world (`model/to_core.mjs`, option `opaque`). The chunks' own `import`s are the host's.
//
//   node opaque_census.mjs [--json] chunk.js…
//
// For each chunk and in total: the top-level units (a statement, or one declarator of a list), those in the model's
// syntax, those opaque, and those the front end refuses even as opaque (`refused`, with why). `--json` prints one line
// per chunk instead, with the reasons.
import { readFileSync } from "node:fs";
import path from "node:path";
import { coreOf } from "./split_dump.mjs";

const args = process.argv.slice(2);
const json = args.includes("--json");
const files = args.filter((a) => a !== "--json");
const tally = (list) => {
  const counts = {};
  for (const { why } of list) {
    const key = why.replace(/^(\w+):.*$/, "$1").replace(/the name \S+/, "a name");
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return Object.fromEntries(Object.entries(counts).sort((a, b) => b[1] - a[1]));
};
const total = { chunks: 0, units: 0, model: 0, opaque: 0, refused: 0, failed: 0 };
const opaqueWhy = [];
const refusedWhy = [];
for (const file of files) {
  let r;
  try {
    r = coreOf(readFileSync(file, "utf8"), { external: true, census: true });
  } catch (e) {
    total.failed++;
    console.log(`${path.basename(file)}: not translated: ${e.message}`);
    continue;
  }
  const units = r.units;
  const row = {
    chunk: path.basename(file),
    units,
    model: units - r.opaque.length,
    opaque: r.opaque.length,
    refused: r.refused.length,
  };
  total.chunks++;
  for (const k of ["units", "model", "opaque", "refused"]) total[k] += row[k];
  opaqueWhy.push(...r.opaque);
  refusedWhy.push(...r.refused);
  if (json) console.log(JSON.stringify({ ...row, why: tally(r.opaque), refusedWhy: r.refused }));
}
const pct = (n) => `${((100 * n) / total.units).toFixed(1)}%`;
console.log(
  `${total.chunks} chunks (${total.failed} not translated): ${total.units} units, ${total.model} in the model (${pct(total.model)}), ` +
    `${total.opaque} opaque (${pct(total.opaque)}), ${total.refused} refused (${pct(total.refused)})`
);
console.log(`opaque, by the first construct outside the fragment: ${JSON.stringify(tally(opaqueWhy))}`);
console.log(`refused: ${JSON.stringify(tally(refusedWhy))}`);
