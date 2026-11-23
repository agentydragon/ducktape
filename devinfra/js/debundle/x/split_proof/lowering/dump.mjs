// Write the annotated ESTree Program and original source for Lean.
//
//   node dump.mjs <bundle.mjs> > bundle.json
import { readFileSync } from "node:fs";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "./dump_core.mjs";

const [path] = process.argv.slice(2);
process.stdout.write(JSON.stringify(lowerDump(readFileSync(path, "utf8"), acorn, eslintScope)));
