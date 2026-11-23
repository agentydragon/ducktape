// Tables from the JSON lines `run_splits.mjs` wrote.
// Uses Map.groupBy (Node.js 21+).
//
//   node summarize.mjs results/*.jsonl
import { markdownTable, readJsonLines } from "../../reporting.mjs";

const rows = readJsonLines(process.argv.slice(2));
const count = (rs, p) => rs.filter(p).length;

const header = [
  "Bundle",
  "Strategy",
  "Splits",
  "Accepted",
  "Accepted, reordered",
  "Accepted, Node differs",
  "Rejected, Node breaks",
  "Rejected, Node fine",
  "Rejected splits that fail each check",
];
const body = [];
for (const [key, rs] of Map.groupBy(rows, (r) => `${r.bundle}\t${r.strategy}`)) {
  const rejected = rs.filter((r) => r.verdict === "reject");
  const why = {};
  for (const r of rejected)
    for (const c of new Set(r.failed.length ? r.failed : ["(none)"])) why[c] = (why[c] ?? 0) + 1;
  body.push([
    ...key.split("\t"),
    rs.length,
    count(rs, (r) => r.verdict === "accept"),
    rs.every((r) => typeof r.reordered === "boolean")
      ? count(rs, (r) => r.verdict === "accept" && r.reordered)
      : "unmeasured",
    count(rs, (r) => r.verdict === "accept" && r.node !== "same"),
    count(rejected, (r) => r.node !== "same"),
    count(rejected, (r) => r.node === "same"),
    Object.entries(why)
      .sort((a, b) => b[1] - a[1])
      .map(([c, k]) => `${c} ${k}`)
      .join(", "),
  ]);
}
console.log(markdownTable(header, body));
