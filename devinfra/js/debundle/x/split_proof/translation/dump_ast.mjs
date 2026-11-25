// Lower a bundle to ESTree JSON with eslint-scope metadata for Lean and a free-variable oracle.
//
//   node dump_ast.mjs <bundle.mjs> > bundle.json
import { readFileSync } from "node:fs";
import { dump } from "./parser.mjs";

const [path] = process.argv.slice(2);
process.stdout.write(JSON.stringify(dump(readFileSync(path, "utf8"))));
