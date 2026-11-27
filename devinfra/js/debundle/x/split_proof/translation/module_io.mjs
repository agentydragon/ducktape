// ESTree transport for LowerSplit's printer, input migration and declaration checker.
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { isDeepStrictEqual } from "node:util";
import { literalKey, needsPrintRange } from "./scope_metadata.mjs";
import { dump, loadSnapshot } from "./parser.mjs";

const linkedAnnotations = { includeOpaqueCaptures: true };

const linkedProgram = (source) => dump(source, linkedAnnotations);
const utf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

function readProgram(file, parse, expectedHash) {
  try {
    const bytes = fs.readFileSync(file);
    if (expectedHash !== undefined && createHash("sha256").update(bytes).digest("hex") !== expectedHash)
      throw new Error("resolved placement source_sha256 differs from source bytes");
    return parse(utf8.decode(bytes));
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

  // The literal key retains the JavaScript value, including numeric type and
  // isolated surrogates that do not survive the Lean JSON round trip.
  const lit = node.type === "Literal" ? (node.lit ?? literalKey(node)) : undefined;
  const comparable = lit === undefined ? {} : { lit };
  if (needsPrintRange(node)) comparable.callableSource = callableSource(node, source, description);
  for (const [key, value] of Object.entries(node)) {
    const isAnnotation =
      (node.type === "Program" && (key === "sourceText" || key === "annotationVersion")) ||
      key === "opaqueCaptures" ||
      (lit !== undefined && (key === "raw" || key === "lit" || (key === "value" && lit.startsWith("s16:")))) ||
      (node.type === "Identifier" && ["k", "b", "d"].includes(key));
    if (ignoredAstFields.has(key) || isAnnotation) continue;
    if (node.type === "TemplateElement" && key === "value" && node.cookedWellFormed === false) {
      // The source spelling preserves the escapes that produced the ill-formed cooked text.
      comparable[key] = { raw: value.raw };
      continue;
    }
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

function checkOutput(manifestPath, dir, checkedPath) {
  const { parts, providers } = readMap(manifestPath, "output manifest");
  if (!fs.lstatSync(dir).isDirectory()) throw new Error(`expected a module directory at ${dir}`);

  const wanted = new Set(Object.keys(providers));
  for (const { programs } of parts) {
    for (const name of Object.keys(programs)) {
      if (wanted.has(name)) throw new Error(`output module path collision: ${name}`);
      wanted.add(name);
    }
  }

  const actual = Object.fromEntries(moduleFiles(dir));
  const missing = [...wanted].filter((name) => !Object.hasOwn(actual, name)).sort();
  const unexpected = Object.keys(actual)
    .filter((name) => !wanted.has(name))
    .sort();
  if (missing.length || unexpected.length) {
    throw new Error(
      `output module set differs${missing.length ? `; missing: ${missing.join(", ")}` : ""}${unexpected.length ? `; unexpected: ${unexpected.join(", ")}` : ""}`
    );
  }

  const grouped = [];
  const files = Object.create(null);
  for (const { programs, source } of parts) {
    for (const [name, ast] of Object.entries(programs)) {
      const parsed = readProgram(actual[name], linkedProgram);
      files[name] = parsed;
      if (parsed.sourceText.startsWith("#!")) parsed.hashbang = parsed.sourceText.split(/[\r\n\u2028\u2029]/, 1)[0];
      const planned = comparableAst(ast, source, `manifest module ${name}`);
      const observed = comparableAst(parsed, parsed.sourceText, `output module ${name}`);
      if (isDeepStrictEqual(planned, observed)) continue;

      // Lean splits module-level declarations into ordered units. Only a grouped
      // declaration can account for a difference under that operation.
      const hasGroup = observed.body.some((statement) => {
        const decl = statement.type === "ExportNamedDeclaration" ? statement.declaration : statement;
        return decl?.type === "VariableDeclaration" && decl.declarations.length > 1;
      });
      if (!hasGroup) throw new Error(`output module ${name} differs from its generated ESTree`);
      grouped.push({ name, planned, observed });
    }
  }
  for (const [name, sourceText] of Object.entries(providers)) {
    if (!fs.readFileSync(actual[name]).equals(Buffer.from(sourceText)))
      throw new Error(`provider ${name} differs from the source graph`);
  }
  writeJson(checkedPath, { files, grouped });
}

function checkNewRoutes(root, names) {
  for (const name of names) {
    const file = outputPath(root, name);
    if (fs.lstatSync(file, { throwIfNoEntry: false })) throw new Error(`output path already exists: ${name}`);
    for (let dir = path.dirname(file); dir !== root; dir = path.dirname(dir)) {
      const stat = fs.lstatSync(dir, { throwIfNoEntry: false });
      if (stat && !stat.isDirectory()) throw new Error(`output path crosses an original asset: ${name}`);
    }
  }
}

function canonicalDirectory(dir) {
  const absolute = path.resolve(dir);
  if (dir !== absolute || !fs.lstatSync(absolute).isDirectory() || fs.realpathSync(absolute) !== absolute)
    throw new Error(`expected a canonical package directory: ${dir}`);
  return absolute;
}

function overlapsDirectories(left, right) {
  const overlaps = (parent, child) => {
    const relative = path.relative(parent, child);
    return (
      relative === "" || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(".." + path.sep))
    );
  };
  return overlaps(left, right) || overlaps(right, left);
}

function readNodePackage(dir, result, entry, provider, caller) {
  const root = canonicalDirectory(dir);
  if (overlapsDirectories(root, canonicalDirectory(path.dirname(result))))
    throw new Error("scratch overlaps the original package root");
  const names = [entry, provider, caller];
  if (
    new Set(names).size !== 3 ||
    !names.every((name) => typeof name === "string" && name.endsWith(".js") && /^[A-Za-z0-9_.~-]+$/.test(name))
  )
    throw new Error("expected distinct flat .js routes for entry, provider, and caller");
  const readFixed = (name) => {
    const file = path.join(root, name);
    if (!fs.lstatSync(file, { throwIfNoEntry: false })?.isFile())
      throw new Error(`expected a regular package file: ${name}`);
    return utf8.decode(fs.readFileSync(file));
  };
  const manifest = readFixed("package.json");
  if (JSON.parse(manifest)?.type !== "module") throw new Error('package.json must explicitly declare "type": "module"');
  const callerText = readFixed(caller);
  const requests = linkedProgram(callerText).body.filter(
    (node) => ["ImportDeclaration", "ExportNamedDeclaration", "ExportAllDeclaration"].includes(node.type) && node.source
  );
  const [request] = requests;
  if (
    requests.length !== 1 ||
    request.type !== "ImportDeclaration" ||
    request.source.value !== `./${entry}` ||
    request.attributes?.length ||
    request.specifiers.length !== 1 ||
    request.specifiers[0].type !== "ImportDefaultSpecifier"
  )
    throw new Error("expected the fixed caller to import only the original entry default");
  writeJson(result, {
    manifest,
    entry: linkedProgram(readFixed(entry)),
    provider: linkedProgram(readFixed(provider)),
    caller: callerText,
  });
}

function checkNodePackageDestination(source, destination, scratch, mode) {
  const root = canonicalDirectory(source);
  const out = path.resolve(destination);
  if (out !== destination) throw new Error("expected a canonical package destination");
  canonicalDirectory(path.dirname(out));
  if (overlapsDirectories(root, out)) throw new Error("destination overlaps the original package root");
  const work = canonicalDirectory(scratch);
  if (overlapsDirectories(work, root) || overlapsDirectories(work, out))
    throw new Error("scratch overlaps a package root");
  if (mode === "reread") canonicalDirectory(out);
  else if (mode !== "fresh") throw new Error("expected a fresh or reread package destination");
  else if (fs.lstatSync(out, { throwIfNoEntry: false })) throw new Error("owned package destination must not exist");
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
  } else if (mode === "--parse-sha256") {
    const [digest, input, output] = args;
    writeJson(output, readProgram(input, linkedProgram, digest));
  } else if (mode === "--parse") {
    const [input, output] = args;
    writeJson(output, readProgram(input, linkedProgram));
  } else if (mode === "--load-input") {
    const [input, output] = args;
    writeJson(output, loadSnapshot(readJson(input), linkedAnnotations));
  } else if (mode === "--check-output") {
    checkOutput(...args);
  } else if (mode === "--check-new-routes") {
    const [root, names] = args;
    checkNewRoutes(fs.realpathSync(root), readJson(names));
  } else if (mode === "--read-node-package") {
    const [entry, provider, caller, root, result] = args;
    readNodePackage(root, result, entry, provider, caller);
  } else if (mode === "--check-node-package-destination") {
    const [readback, source, destination, scratch] = args;
    checkNodePackageDestination(source, destination, scratch, readback);
  } else if (mode === "--read-modules") {
    const [directory, names, output] = args;
    const root = fs.realpathSync(directory);
    const modules = readJson(names).map((name) => {
      const file = outputPath(root, name);
      if (fs.realpathSync(file) !== file) throw new Error(`unsupported module alias: ${name}`);
      return [name, readProgram(file, linkedProgram)];
    });
    writeJson(output, Object.fromEntries(modules));
  } else throw new Error(`unknown mode: ${mode}`);
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
