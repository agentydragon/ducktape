// Black-box test harness for the run_transform binary. Only depends on node
// stdlib and the binary itself — no internal debundler modules — so a
// re-implementation in another language can drive these tests verbatim.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createWebFixtureRoots, runNodeScript, writeSnapshotFixture } from "./test_support/black_box.mjs";

let moduleExportAssertionCounter = 0;
let generatedModuleScriptCounter = 0;

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

export async function runLogicalModulesE2eFixture({
  chunkId = "static/app",
  extraFiles,
  includeResidual = true,
  operations,
  prefix,
  source,
}) {
  const { extractedRoot, outRoot, snapshotRoot } = setupFixture({ chunkId, prefix, source, extraFiles });
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

export async function expectLogicalModulesE2eRejection({
  chunkId = "static/app",
  errorPattern,
  includeResidual = true,
  operations,
  prefix,
  source,
}) {
  const { extractedRoot, outRoot, snapshotRoot } = setupFixture({ chunkId, prefix, source });
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

export function assertModuleExports({ excludes = [], includes = [], modulePath, outRoot }) {
  const assertionPath = join(outRoot, `assert_module_exports_${moduleExportAssertionCounter++}.mjs`);
  writeFileSync(
    assertionPath,
    `const mod = await import(${JSON.stringify(`./${modulePath}`)});
const includes = ${JSON.stringify(includes)};
const excludes = ${JSON.stringify(excludes)};
for (const name of includes) {
  if (!Object.prototype.hasOwnProperty.call(mod, name)) {
    throw new Error(\`Expected \${name} to be exported by ${modulePath}\`);
  }
}
for (const name of excludes) {
  if (Object.prototype.hasOwnProperty.call(mod, name)) {
    throw new Error(\`Expected \${name} not to be exported by ${modulePath}\`);
  }
}
`
  );
  assertNodeOutput(assertionPath, { expectedStdout: "" });
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
