// Supply observed Programs to the Lean declaration checker, including every file in the output tree.
import fs from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

function program(file) {
  try {
    return lowerDump(fs.readFileSync(file, "utf8"), acorn, eslintScope);
  } catch (error) {
    throw new Error(`${file}: ${error.message}`);
  }
}

function* readModules(root, dir = root) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* readModules(root, file);
    else if (entry.isFile()) yield [path.relative(root, file), program(file)];
    else throw new Error(`expected a regular module file at ${file}`);
  }
}

try {
  const [dir, input, output] = process.argv.slice(2);
  if (!fs.lstatSync(dir).isDirectory()) throw new Error(`expected a module directory at ${dir}`);
  fs.writeFileSync(output, JSON.stringify({ source: program(input), files: Object.fromEntries(readModules(dir)) }));
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
