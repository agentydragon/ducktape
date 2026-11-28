// Compare original, unmodified artifacts independently of the compiled constructors.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { acorn } from "../../translation/parser.mjs";
import { topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

const tmp = mkdtempSync(path.join(process.env.TEST_TMPDIR, "identity-"));
const report = { unchanged: {}, cli: {}, controls: {} };
const sorted = (items) => [...items].sort();
const read = (root, file) => readFileSync(path.join(root, file));
const parse = (bytes) => acorn.parse(bytes.toString("utf8"), { ecmaVersion: "latest", sourceType: "module" });
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

function write(root, file, bytes) {
  const dest = path.join(root, file);
  mkdirSync(path.dirname(dest), { recursive: true });
  writeFileSync(dest, bytes);
  return dest;
}

function treeFiles(root) {
  return sorted(readdirSync(root, { recursive: true })).filter((file) => {
    const info = lstatSync(path.join(root, file));
    assert(info.isFile() || info.isDirectory(), `unexpected file type: ${file}`);
    return info.isFile();
  });
}

// This compares a selected artifact map, not two deployment environments.
function compare(original, entry, copy, copyEntry) {
  const files = sorted(original.keys());
  assert.equal(copyEntry, entry, "entry route changed");
  assert.deepEqual(treeFiles(copy), files, "file routes changed");
  for (const file of files) assert(read(copy, file).equals(original.get(file)), `bytes changed: ${file}`);
}

function snapshot(name, entry, original) {
  const copy = path.join(tmp, name, "copy");
  const files = sorted(original.keys());
  assert(original.has(entry));
  for (const file of files) write(copy, file, original.get(file));
  compare(original, entry, copy, entry);
  const rows = files.map((file) => {
    const bytes = original.get(file);
    const units = /\.m?js$/.test(file) ? topLevelStatements(parse(bytes)).length : null;
    return { file, bytes: bytes.length, units, sha256: sha256(bytes) };
  });
  report.unchanged[name] = {
    entry,
    unchanged_file_count: rows.length,
    js_file_count: rows.filter((r) => r.units !== null).length,
    bytes: rows.reduce((n, r) => n + r.bytes, 0),
    source_units: rows.reduce((n, r) => n + (r.units ?? 0), 0),
    moved_units: 0,
    new_owners: 0,
    files: rows,
  };
  return copy;
}

const multi = "no constructor supports multi-chunk deployments";
const singleton = (file) => `no constructor accepts the requested placement of ${file}`;
const noHost = "no constructor supports host or structural deployment options";

function refused(name, entry, source, files, existing, reason, { authored = false, flags = {} } = {}) {
  const spec = authored
    ? write(
        tmp,
        `${name}.spec.json`,
        JSON.stringify({
          entry,
          modules: { [entry]: topLevelStatements(parse(read(source, entry))).map((_, i) => `#${i}`) },
          order: [],
          sequential: true,
        })
      )
    : undefined;
  const manifest = write(
    tmp,
    `${name}.manifest.json`,
    JSON.stringify({
      entry,
      chunks: files.map((file) => ({
        path: file,
        source: path.resolve(source, file),
        ...(file === entry && spec ? { spec } : {}),
      })),
    })
  );
  const fresh = split({ manifest, out: path.join(tmp, name, "fresh"), ...flags });
  const occupied = split({ manifest, out: existing, allowExistingOut: true, ...flags });
  const readback = split({ manifest, out: existing, checkOutput: true, ...flags });
  assert.deepEqual(fresh.verdict, occupied.verdict);
  assert.deepEqual(fresh.verdict, readback.verdict);
  assert.deepEqual(
    fresh.verdict,
    {
      status: "rejected",
      requested: flags.require ?? "normal-completion",
      guarantee: null,
      scope: null,
      every_ending_matches: false,
      reason_code: "no_constructed_rule",
      reason,
    },
    fresh.log
  );
  // split() also checks the exit status, absence of a fresh output path,
  // and that the pre-existing output is untouched in both modes.
  report.cli[name] = {
    requests: ["publish-fresh", "publish-existing", "check-output"],
    exit: 1,
    verdict: fresh.verdict,
  };
}

const ex = process.env.EXCALIDRAW_OUTPUTS.split(" ").find((p) => p.endsWith("/bundle"));
assert(ex, "missing esbuild output directory");
const exFiles = treeFiles(ex);
const exJs = exFiles.filter((f) => f.endsWith(".js"));
assert.equal(exJs.length, 79);
const exCopy = snapshot("excalidraw", "main.js", new Map(exFiles.map((f) => [f, read(ex, f)])));
const main = read(ex, "main.js").toString("utf8");
assert(main.includes("import.meta"));
assert.match(main, /\b(?:window|document)\b/);
assert(exJs.some((file) => read(ex, file).includes('import("@excalidraw/mermaid-to-excalidraw")')));
refused("excalidraw", "main.js", ex, exJs, exCopy, multi);
refused("excalidraw-authored-main-only", "main.js", ex, ["main.js"], exCopy, singleton("main.js"), {
  authored: true,
  flags: { require: "all-outcomes" },
});
refused("excalidraw-host-option", "main.js", ex, exJs, exCopy, noHost, {
  flags: { hostContract: "browser-page" },
});

const libraries = path.join(tmp, "libraries");
execFileSync(process.env.LIBRARY_PREPARE, [libraries], { stdio: "inherit" });
const variants = readdirSync(libraries)
  .filter((f) => f.endsWith(".mjs"))
  .sort();
assert.deepEqual(
  variants,
  ["acorn", "lodash", "semver", "zod"].flatMap((stem) => [`${stem}.min.mjs`, `${stem}.mjs`])
);
for (const file of variants) {
  const copy = snapshot(file, file, new Map([[file, read(libraries, file)]]));
  refused(file, file, libraries, [file], copy, singleton(file));
}
assert(parse(read(libraries, "acorn.mjs")).body.some((node) => node.type === "ExportNamedDeclaration"));
refused(
  "acorn-authored",
  "acorn.mjs",
  libraries,
  ["acorn.mjs"],
  path.join(tmp, "acorn.mjs", "copy"),
  singleton("acorn.mjs"),
  { authored: true }
);

const zod = process.env.ZOD_PACKAGE;
assert.equal(JSON.parse(read(zod, "package.json")).version, "4.6.7");
const zodEntry = "v3/index.js";
const zodGraph = new Map();
function collect(file) {
  if (zodGraph.has(file)) return;
  const bytes = read(zod, file);
  zodGraph.set(file, bytes);
  for (const { source } of parse(bytes).body) {
    if (!source) continue;
    assert(source.value.startsWith("./") || source.value.startsWith("../"));
    const target = path.posix.join(path.posix.dirname(file), source.value);
    assert(target.startsWith("v3/"));
    collect(target);
  }
}
collect(zodEntry);
assert.equal(zodGraph.size, 10);
const zodModules = sorted(zodGraph.keys());
for (const file of ["package.json", "v3/package.json"]) zodGraph.set(file, read(zod, file));
const zodCopy = snapshot("published-zod-v3", zodEntry, zodGraph);
refused("published-zod-v3", zodEntry, zod, zodModules, zodCopy, multi);

// Two native observations disprove inferring unchanged behavior from
// bytes, relative routes, and import spellings across different deployments.
const nativeEntry = "entry.mjs";
const nativeBytes = Buffer.from('export const url = import.meta.url;\nexport { token } from "provider";\n');
const native = new Map([
  [nativeEntry, nativeBytes],
  ["node_modules/provider/package.json", Buffer.from('{"type":"module","exports":"./index.js"}\n')],
  ["node_modules/provider/index.js", Buffer.from("export const token = {};\n")],
]);
const asModule = (root, file) => import(pathToFileURL(path.join(root, file)).href);
const nativeOriginal = path.join(tmp, "native-original");
for (const [file, bytes] of native) write(nativeOriginal, file, bytes);
const nativeCopy = snapshot("native-control", nativeEntry, native);
const originalModule = await asModule(nativeOriginal, nativeEntry);
const copyModule = await asModule(nativeCopy, nativeEntry);
assert.deepEqual(Object.keys(originalModule), ["token", "url"]);
assert.deepEqual(Object.keys(copyModule), Object.keys(originalModule));
assert.notEqual(copyModule.url, originalModule.url);
assert.notEqual(copyModule.token, originalModule.token);
assert.equal(copyModule.token, (await asModule(nativeCopy, "node_modules/provider/index.js")).token);
assert.equal(originalModule.token, (await asModule(nativeOriginal, "node_modules/provider/index.js")).token);
refused("native-authored", nativeEntry, nativeOriginal, [nativeEntry], nativeCopy, singleton(nativeEntry), {
  authored: true,
});
const renamed = "renamed.mjs";
renameSync(path.join(nativeCopy, nativeEntry), path.join(nativeCopy, renamed));
assert(read(nativeCopy, renamed).equals(nativeBytes));
assert.throws(() => compare(native, nativeEntry, nativeCopy, renamed), /entry route changed/);
assert.notEqual((await asModule(nativeCopy, renamed)).url, copyModule.url);
renameSync(path.join(nativeCopy, renamed), path.join(nativeCopy, nativeEntry));
write(nativeCopy, "extra.txt", "extra");
assert.throws(() => compare(native, nativeEntry, nativeCopy, nativeEntry), /file routes changed/);

const exportSource = Buffer.from("export const published = 1;\n");
const renamedExport = Buffer.from("export const otherName = 1;\n");
const exportCopy = snapshot("named-export-control", nativeEntry, new Map([[nativeEntry, exportSource]]));
assert.deepEqual(Object.keys(await asModule(exportCopy, nativeEntry)), ["published"]);
write(exportCopy, nativeEntry, renamedExport);
assert.throws(
  () => compare(new Map([[nativeEntry, exportSource]]), nativeEntry, exportCopy, nativeEntry),
  /bytes changed/
);
assert.deepEqual(Object.keys(await import(pathToFileURL(path.join(exportCopy, nativeEntry)).href + "?edited")), [
  "otherName",
]);
report.controls = {
  renamed_entry: "rejected-by-artifact-comparison; different native metadata URL",
  added_file: "rejected-by-artifact-comparison",
  renamed_export: "rejected-by-artifact-comparison; different native namespace keys",
  relocated_copy: "equal artifact map; different native metadata URL and bare-package live object",
};

const inlineSource = write(tmp, "inline/entry.mjs", "  export const published = 1;\n\n\n");
const inlineSpec = write(
  tmp,
  "inline/spec.json",
  JSON.stringify({
    entry: "entry.mjs",
    modules: { "entry.mjs": ["#0"] },
    order: [],
    sequential: true,
  })
);
const inlineOut = path.join(tmp, "inline", "out");
const inline = split({ source: inlineSource, spec: inlineSpec, out: inlineOut });
assert.equal(inline.status, "certified", inline.log);
assert.equal(inline.scope, "named-declarations");
assert.deepEqual(inline.files, ["entry.mjs"]);
assert(!read(inlineOut, "entry.mjs").equals(readFileSync(inlineSource)));
const inlineReadback = split({ source: inlineSource, spec: inlineSpec, out: inlineOut, checkOutput: true });
assert.deepEqual(inlineReadback.verdict, inline.verdict);
report.unchanged_layout_control = {
  owners: inline.files,
  byte_equal: false,
  verdict: inline.verdict,
  readback: inlineReadback.status,
};

const source = write(tmp, "real-split/entry.mjs", "const seed = 7; export const result = seed;\n");
const spec = write(
  tmp,
  "real-split/spec.json",
  JSON.stringify({
    entry: "entry.mjs",
    modules: { "worker.mjs": ["#0"], "entry.mjs": ["#1"] },
    order: ["worker.mjs"],
    sequential: true,
  })
);
const out = path.join(tmp, "real-split", "out");
const emission = split({ source, spec, out });
assert.equal(emission.status, "certified", emission.log);
assert.equal(emission.guarantee, "all-outcomes", emission.log);
assert.equal(emission.verdict.strategy, "compiled-constructor", emission.log);
assert.deepEqual(emission.files, ["entry.mjs", "worker.mjs"]);
assert(!read(out, "entry.mjs").equals(readFileSync(source)));
const readback = split({ source, spec, out, checkOutput: true });
assert.deepEqual(readback.verdict, emission.verdict);
assert.equal((await asModule(out, "entry.mjs")).result, 7);
report.nontrivial_split_control = { owners: emission.files, verdict: emission.verdict, readback: readback.status };

write(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "identity-baseline.json", JSON.stringify(report, null, 2) + "\n");
console.log(
  JSON.stringify({
    unchanged: Object.fromEntries(
      Object.entries(report.unchanged).map(([name, row]) => [
        name,
        Object.fromEntries(Object.entries(row).filter(([key]) => key !== "files")),
      ])
    ),
    refused_requests: Object.values(report.cli).reduce((n, row) => n + row.requests.length, 0),
    certified_nontrivial_controls: 1,
  })
);
