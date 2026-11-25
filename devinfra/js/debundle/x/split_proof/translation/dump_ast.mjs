// Lower a bundle to ESTree JSON with eslint-scope metadata for Lean and a free-variable oracle.
//
//   node dump_ast.mjs <bundle.mjs> [--opaque-captures] > bundle.json
import { readFileSync } from "node:fs";
import { dump } from "./parser.mjs";

const [path, ...options] = process.argv.slice(2);
if (!path || options.some((option) => option !== "--opaque-captures") || options.length > 1) {
  throw new Error("usage: dump_ast <bundle.mjs> [--opaque-captures]");
}
process.stdout.write(JSON.stringify(dump(readFileSync(path, "utf8"), { includeOpaqueCaptures: options.length === 1 })));
