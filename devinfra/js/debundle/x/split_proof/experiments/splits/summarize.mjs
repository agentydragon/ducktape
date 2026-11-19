// Tables from the JSON lines `run_splits.mjs` wrote.
//
//   node summarize.mjs results/*.jsonl
import { readFileSync } from "node:fs";

const rows = process.argv.slice(2).flatMap((f) =>
  readFileSync(f, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l))
);
const group = (key) => {
  const g = new Map();
  for (const r of rows) g.set(key(r), [...(g.get(key(r)) ?? []), r]);
  return g;
};
const count = (rs, p) => rs.filter(p).length;

const table = [
  [
    "Bundle",
    "Strategy",
    "Splits",
    "Accepted",
    "Accepted, Node differs",
    "Rejected, Node breaks",
    "Rejected, Node fine",
    "Rejected splits that fail each check",
  ],
];
for (const [key, rs] of group((r) => `${r.bundle}\t${r.strategy}`)) {
  const rejected = rs.filter((r) => r.verdict === "reject");
  const why = {};
  for (const r of rejected)
    for (const c of new Set(r.failed.length ? r.failed : ["(none)"])) why[c] = (why[c] ?? 0) + 1;
  table.push([
    ...key.split("\t"),
    String(rs.length),
    String(count(rs, (r) => r.verdict === "accept")),
    String(count(rs, (r) => r.verdict === "accept" && r.node !== "same")),
    String(count(rejected, (r) => r.node !== "same")),
    String(count(rejected, (r) => r.node === "same")),
    Object.entries(why)
      .sort((a, b) => b[1] - a[1])
      .map(([c, k]) => `${c} ${k}`)
      .join(", "),
  ]);
}
// Padded like prettier pads markdown tables, so formatting the file changes nothing.
const widths = table[0].map((_, c) => Math.max(3, ...table.map((row) => row[c].length)));
const line = (cells) => `| ${cells.map((cell, c) => cell.padEnd(widths[c])).join(" | ")} |`;
console.log(line(table[0]));
console.log(line(widths.map((w) => "-".repeat(w))));
for (const row of table.slice(1)) console.log(line(row));
