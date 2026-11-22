import { readFileSync } from "node:fs";
import { diagnose } from "./prototype_guard.mjs";

if (process.argv.length !== 3) {
  console.error("usage: prototype_guard <module.js>");
  process.exit(2);
}
console.log(JSON.stringify(diagnose(readFileSync(process.argv[2], "utf8"))));
