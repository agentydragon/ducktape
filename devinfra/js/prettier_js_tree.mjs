// Format JavaScript files from a declared input tree and copy all other bytes unchanged.
//
// argv: <input-tree> <output-tree> <prettier-config> <prettier-package-runfiles-path> [input-subdir]
import { promises as fs } from "node:fs";
import { createRequire } from "node:module";
import * as path from "node:path";

function isWithin(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === "" || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`));
}

function validateSubdir(subdir) {
  if (subdir === "") return;
  if (path.isAbsolute(subdir) || subdir.includes("\\")) {
    throw new Error(`input subdirectory must be a relative POSIX path: ${JSON.stringify(subdir)}`);
  }
  if (subdir.split("/").some((segment) => segment === "" || segment === "." || segment === "..")) {
    throw new Error(`input subdirectory must not contain empty, '.' or '..' segments: ${JSON.stringify(subdir)}`);
  }
}

const execroot = process.env.JS_BINARY__EXECROOT ?? process.cwd();
const runfilesDir = process.env.JS_BINARY__RUNFILES;
const [inputArg, outputArg, configArg, prettierPackageArg, inputSubdir = ""] = process.argv.slice(2);
if (!inputArg || !outputArg || !configArg || !prettierPackageArg) {
  throw new Error(
    "usage: prettier_js_tree.mjs <input-tree> <output-tree> <prettier-config> <prettier-package-runfiles-path> [input-subdir]"
  );
}
if (!runfilesDir) {
  throw new Error("JS_BINARY__RUNFILES is required to resolve the caller's Prettier package");
}
validateSubdir(inputSubdir);

const prettierPackageDir = path.resolve(runfilesDir, prettierPackageArg);
const prettierRequire = createRequire(path.join(prettierPackageDir, "package.json"));
const prettier = prettierRequire("prettier");

const sourceRoot = path.resolve(execroot, inputArg);
const inputCandidate = path.resolve(sourceRoot, inputSubdir || ".");
if (!isWithin(sourceRoot, inputCandidate)) {
  throw new Error(`input subdirectory escapes the source tree: ${JSON.stringify(inputSubdir)}`);
}
const [realSourceRoot, inputDir] = await Promise.all([fs.realpath(sourceRoot), fs.realpath(inputCandidate)]);
if (!isWithin(realSourceRoot, inputDir)) {
  throw new Error(`input subdirectory resolves outside the source tree: ${JSON.stringify(inputSubdir)}`);
}
if (!(await fs.stat(inputDir)).isDirectory()) {
  throw new Error(`input path is not a directory: ${inputDir}`);
}

const outputDir = path.resolve(execroot, outputArg);
if (isWithin(sourceRoot, outputDir) || isWithin(outputDir, sourceRoot)) {
  throw new Error("input and output trees must not overlap");
}
const configPath = path.resolve(execroot, configArg);

async function* walk(dir) {
  const entries = await fs.readdir(dir, { withFileTypes: true });
  entries.sort((left, right) => left.name.localeCompare(right.name));
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(fullPath);
    else yield fullPath;
  }
}

for await (const file of walk(inputDir)) {
  const relativePath = path.relative(inputDir, file);
  const destination = path.join(outputDir, relativePath);
  await fs.mkdir(path.dirname(destination), { recursive: true });
  if (file.endsWith(".js")) {
    const source = await fs.readFile(file, "utf8");
    const options = await prettier.resolveConfig(file, { config: configPath });
    const formatted = await prettier.format(source, { ...options, filepath: file });
    await fs.writeFile(destination, formatted);
  } else {
    await fs.copyFile(file, destination);
  }
}
