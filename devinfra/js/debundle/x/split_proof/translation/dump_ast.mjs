// Lower a bundle to ESTree JSON with eslint-scope metadata for Lean and a free-variable oracle.
//
//   node dump.mjs <bundle.mjs> > bundle.json
import { readFileSync } from "node:fs";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

const [path] = process.argv.slice(2);
process.stdout.write(JSON.stringify(lowerDump(readFileSync(path, "utf8"), acorn, eslintScope)));
