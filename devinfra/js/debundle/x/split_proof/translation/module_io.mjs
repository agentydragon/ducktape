// ESTree transport for LowerSplit's printer, input migration and declaration checker.
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump, upgradeLowerDump } from "../lowering/dump_core.mjs";

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

function outputPath(dir, name) {
  const root = path.resolve(dir);
  const file = path.resolve(root, name);
  const escapes = (relative) => path.isAbsolute(relative) || relative === ".." || relative.startsWith(`..${path.sep}`);
  if (file === root || path.isAbsolute(name) || escapes(path.relative(root, file)))
    throw new Error(`invalid output module path: ${name}`);
  for (let component = file; component !== root; component = path.dirname(component)) {
    if (fs.lstatSync(component, { throwIfNoEntry: false })?.isSymbolicLink()) {
      if (escapes(path.relative(fs.realpathSync(root), fs.realpathSync(component))))
        throw new Error(`output module path follows a symbolic link outside its output directory: ${name}`);
    }
  }
  return file;
}

const readJson = (file) => JSON.parse(fs.readFileSync(file, "utf8"));
const writeJson = (file, value) => fs.writeFileSync(file, JSON.stringify(value));

try {
  const [mode, ...args] = process.argv.slice(2);
  if (mode === "--print") {
    const [manifest, dir, sourceFile] = args;
    const { printProgram } = await import("./print.mjs");
    const source = fs.readFileSync(sourceFile, "utf8");
    const modules = Object.entries(readJson(manifest)).map(([name, ast]) => ({
      file: outputPath(dir, name),
      code: printProgram(ast, source),
    }));
    for (const { file, code } of modules) {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, code);
    }
  } else if (mode === "--print-units") {
    const [input, sourceFile, output] = args;
    const { printProgram } = await import("./print.mjs");
    const source = fs.readFileSync(sourceFile, "utf8");
    const units = readJson(input).map((unit) =>
      printProgram({ type: "Program", sourceType: "module", body: [unit] }, source).trimEnd()
    );
    writeJson(output, units);
  } else if (mode === "--linked-input") {
    const [consumer, provider, output] = args;
    const relative = path.relative(path.dirname(consumer), provider);
    const specifier = relative.startsWith("../") ? relative : `./${relative}`;
    if (new URL(specifier, pathToFileURL(consumer)).href !== pathToFileURL(provider).href)
      throw new Error(`unsupported relative module specifier: ${specifier}`);
    writeJson(output, { consumer: program(consumer), provider: program(provider), specifier });
  } else if (mode === "--upgrade") {
    const [input, output] = args;
    writeJson(output, upgradeLowerDump(readJson(input), acorn, eslintScope));
  } else {
    const dir = mode;
    const [input, output] = args;
    if (!fs.lstatSync(dir).isDirectory()) throw new Error(`expected a module directory at ${dir}`);
    writeJson(output, { source: program(input), files: Object.fromEntries(readModules(dir)) });
  }
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
