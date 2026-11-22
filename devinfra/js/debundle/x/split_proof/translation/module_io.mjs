// ESTree transport for LowerSplit's printer, input migration and declaration checker.
import fs from "node:fs";
import path from "node:path";
import { isDeepStrictEqual } from "node:util";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { literalKey, needsPrintRange } from "./scope_metadata.mjs";
import { lowerDump, upgradeLowerDump } from "../lowering/dump_core.mjs";

function program(file) {
  try {
    return lowerDump(fs.readFileSync(file, "utf8"), acorn, eslintScope);
  } catch (error) {
    throw new Error(`${file}: ${error.message}`);
  }
}

function* moduleFiles(root, dir = root) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* moduleFiles(root, file);
    else if (entry.isFile()) yield [path.relative(root, file), file];
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

const ignoredAstFields = new Set(["start", "end", "loc", "range"]);
function callableSource(node, source, description) {
  if (
    !Number.isInteger(node.start) ||
    !Number.isInteger(node.end) ||
    node.start < 0 ||
    node.end <= node.start ||
    node.end > source.length
  )
    throw new Error(`${description}: expected source offsets for ${node.type}`);
  return source.slice(node.start, node.end);
}

function comparableAst(node, source, description, taggedTemplateQuasi = false) {
  if (Array.isArray(node)) return node.map((child) => comparableAst(child, source, description, taggedTemplateQuasi));
  if (!node || typeof node !== "object") return node;

  // JSON transport represents an infinite number as a string. The literal key
  // retains its JavaScript type. Only synthesized literals lack this annotation.
  const comparable = node.type === "Literal" ? { lit: node.lit ?? literalKey(node) } : {};
  if (needsPrintRange(node)) comparable.callableSource = callableSource(node, source, description);
  for (const [key, value] of Object.entries(node)) {
    const isAnnotation =
      (node.type === "Program" && (key === "sourceText" || key === "annotationVersion")) ||
      (node.type === "Literal" && (key === "raw" || key === "lit")) ||
      (node.type === "Identifier" && ["k", "b", "d"].includes(key));
    if (ignoredAstFields.has(key) || isAnnotation) continue;
    if (node.type === "TemplateElement" && key === "value" && !taggedTemplateQuasi) {
      comparable[key] = { cooked: value.cooked };
      continue;
    }
    const childIsTaggedQuasi =
      (node.type === "TaggedTemplateExpression" && key === "quasi") ||
      (node.type === "TemplateLiteral" && taggedTemplateQuasi && key === "quasis");
    comparable[key] = comparableAst(value, source, description, childIsTaggedQuasi);
  }
  return comparable;
}

function readMap(file, description) {
  const value = readJson(file);
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error(`expected a ${description} object at ${file}`);
  return value;
}

function checkOutput(manifestPath, originalSourcePath, providersManifestPath, dir) {
  const expected = readMap(manifestPath, "generated-module manifest");
  const providers = readMap(providersManifestPath, "provider manifest");
  const originalSource = fs.readFileSync(originalSourcePath, "utf8");
  if (!fs.lstatSync(dir).isDirectory()) throw new Error(`expected a module directory at ${dir}`);

  const expectedNames = Object.keys(expected);
  const providerNames = Object.keys(providers);
  for (const name of providerNames) {
    if (Object.hasOwn(expected, name)) throw new Error(`provider ${name} overlaps a generated module`);
  }

  const actual = Object.fromEntries(moduleFiles(dir));
  const wantedNames = [...expectedNames, ...providerNames].sort();
  const actualNames = Object.keys(actual).sort();
  if (!isDeepStrictEqual(actualNames, wantedNames)) {
    const missing = wantedNames.filter((name) => !Object.hasOwn(actual, name));
    const unexpected = actualNames.filter((name) => !Object.hasOwn(expected, name) && !Object.hasOwn(providers, name));
    throw new Error(
      `output module set differs${missing.length ? `; missing: ${missing.join(", ")}` : ""}${unexpected.length ? `; unexpected: ${unexpected.join(", ")}` : ""}`
    );
  }

  for (const name of expectedNames) {
    const parsed = program(actual[name]);
    if (parsed.sourceText.startsWith("#!")) parsed.hashbang = parsed.sourceText.split(/[\r\n\u2028\u2029]/, 1)[0];
    if (
      !isDeepStrictEqual(
        comparableAst(expected[name], originalSource, `manifest module ${name}`),
        comparableAst(parsed, parsed.sourceText, `output module ${name}`)
      )
    )
      throw new Error(`output module ${name} differs from its generated ESTree`);
  }
  for (const name of providerNames) {
    const sourceText = providers[name]?.sourceText;
    if (typeof sourceText !== "string" || !fs.readFileSync(actual[name]).equals(Buffer.from(sourceText)))
      throw new Error(`provider ${name} differs from the source graph`);
  }
}

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
  } else if (mode === "--check-output") {
    const [manifestPath, originalSourcePath, providersManifestPath, dir] = args;
    checkOutput(manifestPath, originalSourcePath, providersManifestPath, dir);
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
  } else throw new Error(`unknown mode: ${mode}`);
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
