import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";
import { FORMS } from "./syntax_inventory.mjs";

export function readAnalysisInventory(
  executable = process.env.ANALYSIS_INVENTORY ??
    fileURLToPath(new URL("../lean/.lake/build/bin/analysis_inventory", import.meta.url))
) {
  const witnesses = FORMS.map(({ name, snippet }) => [name, lowerDump(snippet, acorn, eslintScope)]);
  return JSON.parse(execFileSync(executable, [], { input: JSON.stringify(witnesses) + "\n", encoding: "utf8" }));
}
