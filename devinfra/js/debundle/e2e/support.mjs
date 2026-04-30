// Black-box test harness for the run_transform binary. Only depends on node
// stdlib and the binary itself — no internal debundler modules — so a
// re-implementation in another language can drive these tests verbatim.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createWebFixtureRoots, runNodeScript, writeSnapshotFixture } from "./black_box.mjs";

let moduleExportProbeCounter = 0;
let generatedModuleScriptCounter = 0;

export function logicalModule(path, members) {
  return {
    id: `logical__${path.replace(/\//g, "_")}`,
    operation: "define_logical_module",
    selector: { chunkId: "static/app" },
    target: { path },
    members: members.map(({ name, kind = "VariableDeclarator", binding = name, alias }) => ({
      id: `member__${name}`,
      name: alias ?? name,
      selector: { binding: { kind, name: binding } },
    })),
  };
}

const DEFAULT_PIPELINE = (snapshotRoot, jsListPath, outRoot) => [
  {
    id: "load",
    operation: "load_js_chunks",
    args: { inputRoot: snapshotRoot, jsListPath },
  },
  { id: "parse", operation: "compute_js_asts" },
  { id: "normalize", operation: "normalize_js_chunks", args: { jobs: 1 } },
  {
    id: "logical",
    operation: "materialize_logical_modules",
    args: { chunkIds: ["static/app"], pruneOtherChunks: false },
  },
  { id: "write", operation: "write_js_tree", args: { force: true, outDir: outRoot } },
];

function buildSpec({ chunkId, operations, snapshotRoot, jsListPath, outRoot, includeResidual }) {
  return {
    kind: "js.ast_transform_spec",
    operations: includeResidual
      ? [
          ...operations,
          {
            id: "logical__residual_unhandled",
            operation: "define_residual_module",
            selector: { chunkId },
            target: { path: "residual/unhandled" },
          },
        ]
      : operations,
    pipeline: DEFAULT_PIPELINE(snapshotRoot, jsListPath, outRoot),
  };
}

function prefixFromTestContext(t) {
  // Slugify the test name into a temp-dir prefix so failed runs are easy to
  // pick out of /tmp without the test file having to repeat its own name.
  const slug = t.name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `debundle-e2e-${slug}-`;
}

function setupFixture({ chunkId, prefix, source, extraFiles = {} }) {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(prefix);
  const entryFile = `${chunkId}.js`;
  writeSnapshotFixture({
    extractedRoot,
    files: { [entryFile]: source, ...extraFiles },
    jsFiles: [entryFile],
    snapshotRoot,
  });
  mkdirSync(outRoot, { recursive: true });
  return { extractedRoot, outRoot, snapshotRoot, entryFile };
}

function spawnTransform(specPath) {
  const runTransformBin = process.env.DUCKTAPE_RUN_TRANSFORM_BIN;
  assert.ok(
    runTransformBin,
    "DUCKTAPE_RUN_TRANSFORM_BIN must point at //devinfra/js/debundle/transforms:run_transform"
  );
  return spawnSync(runTransformBin, ["--spec", specPath], { encoding: "utf8" });
}

export async function runLogicalModulesE2eFixture(
  t,
  { chunkId = "static/app", extraFiles, includeResidual = true, operations, source }
) {
  const { extractedRoot, outRoot, snapshotRoot } = setupFixture({
    chunkId,
    extraFiles,
    prefix: prefixFromTestContext(t),
    source,
  });
  const specPath = join(outRoot, "transform_spec.jsonc");
  writeJsonFile(
    specPath,
    buildSpec({
      chunkId,
      includeResidual,
      jsListPath: join(extractedRoot, "js-files.txt"),
      operations,
      outRoot,
      snapshotRoot,
    })
  );
  const result = spawnTransform(specPath);
  assert.equal(result.signal, null);
  assert.equal(result.status, 0, result.stderr || result.stdout);

  return {
    chunkId,
    entryPath: join(outRoot, ...chunkId.split("/"), "entry.js"),
    outRoot,
    snapshotRoot,
  };
}

export async function expectLogicalModulesE2eRejection(
  t,
  { chunkId = "static/app", errorPattern, includeResidual = true, operations, source }
) {
  const { extractedRoot, outRoot, snapshotRoot } = setupFixture({
    chunkId,
    prefix: prefixFromTestContext(t),
    source,
  });
  const specPath = join(outRoot, "transform_spec.jsonc");
  writeJsonFile(
    specPath,
    buildSpec({
      chunkId,
      includeResidual,
      jsListPath: join(extractedRoot, "js-files.txt"),
      operations,
      outRoot,
      snapshotRoot,
    })
  );
  const result = spawnTransform(specPath);
  assert.equal(result.signal, null);
  assert.notEqual(
    result.status,
    0,
    `expected spec to be rejected\nstdout:\n${result.stdout}\nstderr:\n${result.stderr}`
  );
  assert.match(result.stderr, errorPattern, `stderr did not match\nstderr:\n${result.stderr}`);
}

export function assertEntryOutput(fixture, expectedStdout) {
  assertNodeOutput(fixture.entryPath, { expectedStdout });
}

export function listModuleExports({ modulePath, outRoot }) {
  // Probe the emitted module by spawning node, importing it, and printing
  // its exported names as JSON. Keeping the probe script trivial moves all
  // assertion logic to the test side, where failures show up as ordinary
  // node:assert diagnostics instead of generic "Expected X to be exported".
  const probePath = join(outRoot, `__probe_module_exports_${moduleExportProbeCounter++}.mjs`);
  writeFileSync(
    probePath,
    `const mod = await import(${JSON.stringify(`./${modulePath}`)});
process.stdout.write(JSON.stringify(Object.keys(mod)));
`
  );
  const result = runNodeScript(probePath);
  assert.equal(result.signal, null);
  assert.equal(result.status, 0, `probing ${modulePath} exited ${result.status}\nstderr:\n${result.stderr}`);
  return JSON.parse(result.stdout);
}

export function assertModuleExports({ excludes = [], includes = [], modulePath, outRoot }) {
  const exported = new Set(listModuleExports({ modulePath, outRoot }));
  const summary = exported.size === 0 ? "<none>" : [...exported].sort().join(", ");
  for (const name of includes) {
    assert.ok(exported.has(name), `expected ${modulePath} to export ${name}; actual exports: ${summary}`);
  }
  for (const name of excludes) {
    assert.ok(!exported.has(name), `expected ${modulePath} to not export ${name}; actual exports: ${summary}`);
  }
}

export function assertModuleSource({ doesNotMatch = [], matches = [], modulePath, outRoot }) {
  const code = readFileSync(join(outRoot, modulePath), "utf8");
  for (const pattern of matches) {
    assert.match(code, pattern, `${modulePath} did not match ${pattern}\n--- ${modulePath} ---\n${code}`);
  }
  for (const pattern of doesNotMatch) {
    assert.doesNotMatch(code, pattern, `${modulePath} unexpectedly matched ${pattern}\n--- ${modulePath} ---\n${code}`);
  }
}

export function assertGeneratedModuleScript({ expectedStdout, outRoot, source }) {
  const assertionPath = join(outRoot, `assert_generated_module_${generatedModuleScriptCounter++}.mjs`);
  writeFileSync(assertionPath, source);
  assertNodeOutput(assertionPath, { expectedStdout });
}

export function assertGeneratedModuleAfterEntryScript({ expectedStdout, outRoot, source }) {
  assertGeneratedModuleScript({
    expectedStdout,
    outRoot,
    source: `const __log = console.log;
console.log = () => {};
await import("./static/app/entry.js");
console.log = __log;
${source}`,
  });
}

function writeJsonFile(path, value) {
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`);
}

export function assertNodeOutput(
  path,
  { expectedSignal = null, expectedStatus = 0, expectedStderr = "", expectedStdout }
) {
  assert.deepEqual(runNodeScript(path), {
    signal: expectedSignal,
    status: expectedStatus,
    stderr: expectedStderr,
    stdout: expectedStdout,
  });
}
