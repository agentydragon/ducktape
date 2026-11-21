import { readFileSync } from "node:fs";
import { inspectCalls } from "./call_facts.mjs";

if (process.argv.length !== 4) {
  console.error("usage: call_facts <bundle.js> <module-scope-helper-name>");
  process.exit(2);
}
const report = inspectCalls(readFileSync(process.argv[2], "utf8"), process.argv[3]);
const counts = {};
for (const call of report.calls) for (const reason of call.uncertainties) counts[reason] = (counts[reason] ?? 0) + 1;
console.log(JSON.stringify({ ...report, uncertainty_counts: counts }, null, 2));
