// ESTree transport for LowerSplit's printer, input migration and declaration checker.
import fs from "node:fs";
import path from "node:path";
import { isDeepStrictEqual } from "node:util";
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
  } else if (mode === "--parse") {
    const [input, output] = args;
    writeJson(output, program(input));
  } else if (mode === "--load-input") {
    const [input, output] = args;
    const snapshot = readJson(input);
    const hasVersion =
      snapshot &&
      typeof snapshot === "object" &&
      !Array.isArray(snapshot) &&
      Object.hasOwn(snapshot, "annotationVersion");
    if (hasVersion) {
      if (snapshot.annotationVersion !== 2)
        throw new Error(`unsupported annotationVersion: ${JSON.stringify(snapshot.annotationVersion)}`);
      if (
        typeof snapshot.sourceText !== "string" ||
        !isDeepStrictEqual(snapshot, lowerDump(snapshot.sourceText, acorn, eslintScope))
      )
        throw new Error("input annotations differ from a fresh parse of sourceText");
      writeJson(output, snapshot);
    } else {
      writeJson(output, upgradeLowerDump(snapshot, acorn, eslintScope));
    }
  } else if (mode === "--read-output") {
    const [dir, output] = args;
    if (!fs.lstatSync(dir).isDirectory()) throw new Error(`expected a module directory at ${dir}`);
    writeJson(output, Object.fromEntries(readModules(dir)));
  } else if (mode === "--read-modules") {
    const [entry, names, output] = args;
    const source = fs.realpathSync(entry);
    const root = path.dirname(source);
    const modules = readJson(names).map((name) => {
      const file = outputPath(root, name);
      if (fs.realpathSync(file) !== file || file === source)
        throw new Error(`unsupported module alias or request back to the source entry: ${name}`);
      return [name, program(file)];
    });
    writeJson(output, Object.fromEntries(modules));
  } else if (mode === "--upgrade") {
    const [input, output] = args;
    writeJson(output, upgradeLowerDump(readJson(input), acorn, eslintScope));
  } else throw new Error(`unknown mode: ${mode}`);
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
