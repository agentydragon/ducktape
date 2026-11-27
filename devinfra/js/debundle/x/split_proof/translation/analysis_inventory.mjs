import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dump } from "./parser.mjs";
import { FORMS } from "./syntax_inventory.mjs";

export function readAnalysisInventory(
  executable = process.env.ANALYSIS_INVENTORY ??
    fileURLToPath(new URL("../.lake/build/bin/analysis_inventory", import.meta.url))
) {
  const witnesses = FORMS.map(({ name, snippet }) => [name, dump(snippet)]);
  return JSON.parse(execFileSync(executable, [], { input: JSON.stringify(witnesses) + "\n", encoding: "utf8" }));
}
