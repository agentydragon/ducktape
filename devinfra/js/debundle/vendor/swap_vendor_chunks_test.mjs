import assert from "node:assert/strict";
import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { parse } from "@babel/parser";
import { writeJsonFile, writeTextFile } from "../common/js_module_lib.mjs";
import {
  getArtifactVendorAnnotations,
  listChunkIds,
} from "../common/pipeline_artifact_lib.mjs";
import { createTempFixtureRoot, makePipelineArtifact, makePipelineChunk } from "../test_support/fixture_lib.mjs";
import { swapVendorChunks } from "./swap_vendor_chunks_lib.mjs";

function makeChunk(chunkId, files) {
  return makePipelineChunk(chunkId, files);
}

function makeArtifact(entries) {
  return makePipelineArtifact(entries);
}

function hasChunk(artifact, chunkId) {
  return listChunkIds(artifact).includes(chunkId);
}

function swapOp(overrides = {}) {
  return {
    id: "op_katex_swap",
    operation: "mark_vendor",
    level: "swap",
    chunkPath: "static/katex-BZy9Y_85.js",
    identity: "katex/dist/katex.mjs",
    package: "katex",
    version: "0.16.11",
    subpath: "dist/katex.mjs",
    evidence: [{ path: "x", line: 1, text: "y" }],
    ...overrides,
  };
}

function setupFixture({ pkgName = "katex", installed = "0.16.11", upstreamBody, subpath = "dist/katex.mjs", pkgPresent = true } = {}) {
  const dir = createTempFixtureRoot("swap-vendor-");
  const packagesRoot = join(dir, "node_modules");
  const wrapperDir = join(dir, "vendors", "generated");
  if (pkgPresent) {
    writeJsonFile(join(packagesRoot, pkgName, "package.json"), {
      name: pkgName,
      version: installed,
    });
  }
  if (upstreamBody !== undefined) {
    const target = join(packagesRoot, pkgName, subpath);
    mkdirSync(dirname(target), { recursive: true });
    writeTextFile(target, upstreamBody);
  }
  return {
    dir,
    packagesRoot,
    wrapperDir,
    cleanup: () => rmSync(dir, { force: true, recursive: true }),
  };
}

function writeDependencyFile(fx, pkgName, subpath, body) {
  const target = join(fx.packagesRoot, pkgName, subpath);
  mkdirSync(dirname(target), { recursive: true });
  writeTextFile(target, body);
  return target;
}

test("happy path: trivial single-file package, chunk dropped, manifest written", (t) => {
  const upstream = `export const render = () => {};\n`;
  // Vendor chunk body "lexically equivalent" plus trailing boundary export list.
  const vendor = `export const render = () => {};\nexport { render };\n`;
  const fx = setupFixture({ upstreamBody: upstream });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([makeChunk("static/katex-BZy9Y_85", { "runtime.js": vendor })]);
  const outManifestPath = join(fx.dir, "out", "vendor-resolutions.json");

  const { manifest, artifact: outArtifact, outputManifestPath } = swapVendorChunks({
    artifact,
    operations: [swapOp()],
    packagesRoot: fx.packagesRoot,
    outputManifestPath: outManifestPath,
  });

  assert.equal(manifest.counts.swapped, 1);
  assert.equal(hasChunk(outArtifact, "static/katex-BZy9Y_85"), false);
  assert.equal(outputManifestPath, outManifestPath);
  const onDisk = JSON.parse(readFileSync(outManifestPath, "utf8"));
  assert.equal(onDisk.kind, "js.vendor_resolution_manifest");
  assert.deepEqual(onDisk.resolutions["static/katex-BZy9Y_85.js"], {
    chunkId: "static/katex-BZy9Y_85",
    chunkPath: "static/katex-BZy9Y_85.js",
    entryFile: "runtime.js",
    package: "katex",
    version: "0.16.11",
    subpath: "dist/katex.mjs",
  });
  const vendorAnn = getArtifactVendorAnnotations(outArtifact).get("static/katex-BZy9Y_85");
  assert.equal(vendorAnn.swap.package, "katex");
  assert.equal(vendorAnn.swap.verification, "structural");
});

test("version mismatch fails closed naming both versions", (t) => {
  const upstream = `export const render = () => {};\n`;
  const vendor = `export const render = () => {};\nexport { render };\n`;
  const fx = setupFixture({ upstreamBody: upstream, installed: "0.16.10" });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([makeChunk("static/katex-BZy9Y_85", { "runtime.js": vendor })]);

  assert.throws(
    () =>
      swapVendorChunks({
        artifact,
        operations: [swapOp({ version: "0.16.11" })],
        packagesRoot: fx.packagesRoot,
        write: false,
      }),
    (err) =>
      /version mismatch/.test(err.message) &&
      /0\.16\.11/.test(err.message) &&
      /0\.16\.10/.test(err.message)
  );
});

test("body divergence is tolerated under structural verification", (t) => {
  // Upstream body differs from vendor body (extra statement). Under the old byte
  // fingerprint this failed; under structural verification only export-shape and
  // import-alignment gate the swap, so this is accepted.
  const upstream = `export const render = () => {};\nconst extra = 42;\n`;
  const vendor = `export const render = () => {};\nexport { render };\n`;
  const fx = setupFixture({ upstreamBody: upstream });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([makeChunk("static/katex-BZy9Y_85", { "runtime.js": vendor })]);

  const result = swapVendorChunks({
    artifact,
    operations: [swapOp()],
    packagesRoot: fx.packagesRoot,
    write: false,
  });

  assert.equal(result.manifest.counts.swapped, 1);
  assert.equal(hasChunk(result.artifact, "static/katex-BZy9Y_85"), false);
});

test("export-shape mismatch fails closed when vendor has a name upstream lacks", (t) => {
  // Upstream exports only `render`; vendor re-exports `render as parse`, so the
  // vendor exports `parse` which upstream does not provide — caller imports of
  // that name would break after swap, so the stage must refuse.
  const upstream = `export const render = () => {};\n`;
  const vendor = `export const render = () => {};\nexport { render, render as parse };\n`;
  const fx = setupFixture({ upstreamBody: upstream });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([makeChunk("static/katex-BZy9Y_85", { "runtime.js": vendor })]);

  assert.throws(
    () =>
      swapVendorChunks({
        artifact,
        operations: [swapOp()],
        packagesRoot: fx.packagesRoot,
        write: false,
      }),
    (err) => /export shape mismatch/.test(err.message) && /parse/.test(err.message)
  );
});

test("subset vendor exports are accepted (upstream may expose more)", (t) => {
  // Vendor is tree-shaken: exposes only `render`. Upstream ships `render`,
  // `parse`, and `version`. The swap must succeed — extras upstream are dead
  // code from the vendor's perspective; every caller specifier is available.
  const upstream = `export const render = () => {};\nexport const parse = () => {};\nexport const version = "0.0.1";\n`;
  const vendor = `export const render = () => {};\nexport { render };\n`;
  const fx = setupFixture({ upstreamBody: upstream });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([makeChunk("static/katex-BZy9Y_85", { "runtime.js": vendor })]);
  const result = swapVendorChunks({
    artifact,
    operations: [swapOp()],
    packagesRoot: fx.packagesRoot,
    write: false,
  });

  assert.equal(result.manifest.counts.swapped, 1);
  assert.equal(hasChunk(result.artifact, "static/katex-BZy9Y_85"), false);
});

test("import-alignment mismatch fails closed naming caller", (t) => {
  const upstream = `export const render = () => {};\n`;
  const vendor = `export const render = () => {};\nexport { render };\n`;
  const fx = setupFixture({ upstreamBody: upstream });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([
    makeChunk("static/katex-BZy9Y_85", { "runtime.js": vendor }),
    makeChunk("static/index-AppEntry", {
      "runtime.js": `import { ua as r } from "../katex-BZy9Y_85/runtime.js";\nr();\n`,
    }),
  ]);

  assert.throws(
    () =>
      swapVendorChunks({
        artifact,
        operations: [swapOp()],
        packagesRoot: fx.packagesRoot,
        write: false,
      }),
    (err) =>
      /import alignment failed/.test(err.message) &&
      /static\/index-AppEntry/.test(err.message) &&
      /"ua"/.test(err.message)
  );
});

test("subpath escape attempt fails closed", (t) => {
  const upstream = `export const render = () => {};\n`;
  const vendor = `export const render = () => {};\nexport { render };\n`;
  const fx = setupFixture({ upstreamBody: upstream });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([makeChunk("static/katex-BZy9Y_85", { "runtime.js": vendor })]);

  assert.throws(
    () =>
      swapVendorChunks({
        artifact,
        operations: [swapOp({ subpath: "../../etc/passwd" })],
        packagesRoot: fx.packagesRoot,
        write: false,
      }),
    (err) => /subpath escapes package root/.test(err.message) || /not found/.test(err.message)
  );
});

test("missing package in package.json fails closed", (t) => {
  const upstream = `export const render = () => {};\n`;
  const vendor = `export const render = () => {};\nexport { render };\n`;
  const fx = setupFixture({ upstreamBody: upstream, pkgPresent: false });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([makeChunk("static/katex-BZy9Y_85", { "runtime.js": vendor })]);

  assert.throws(
    () =>
      swapVendorChunks({
        artifact,
        operations: [swapOp()],
        packagesRoot: fx.packagesRoot,
        write: false,
      }),
    (err) => /Package (root not found|metadata missing)/.test(err.message) && /katex/.test(err.message)
  );
});

test("dynamic-import callers do not trigger alignment failure", (t) => {
  const upstream = `export const render = () => {};\n`;
  const vendor = `export const render = () => {};\nexport { render };\n`;
  const fx = setupFixture({ upstreamBody: upstream });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([
    makeChunk("static/katex-BZy9Y_85", { "runtime.js": vendor }),
    makeChunk("static/index-AppEntry", {
      "runtime.js": `async function f() { return await import("../katex-BZy9Y_85/runtime.js"); }\n`,
    }),
  ]);

  const { manifest } = swapVendorChunks({
    artifact,
    operations: [swapOp()],
    packagesRoot: fx.packagesRoot,
    write: false,
  });
  assert.equal(manifest.counts.swapped, 1);
});

test("swaps preserve explicit non-runtime entry file paths in the resolution manifest", (t) => {
  const upstream = `export const render = () => {};\n`;
  const vendor = `export const render = () => {};\nexport { render };\n`;
  const fx = setupFixture({ upstreamBody: upstream });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([
    makeChunk("static/katex-BZy9Y_85", { "entry.js": vendor }),
    makeChunk("static/index-AppEntry", {
      "entry.js": `import { render } from "../katex-BZy9Y_85/entry.js";\nrender();\n`,
    }),
  ]);
  const outManifestPath = join(fx.dir, "out", "vendor-resolutions.json");

  const { manifest } = swapVendorChunks({
    artifact,
    operations: [swapOp()],
    packagesRoot: fx.packagesRoot,
    outputManifestPath: outManifestPath,
  });

  assert.equal(manifest.counts.swapped, 1);
  const onDisk = JSON.parse(readFileSync(outManifestPath, "utf8"));
  assert.equal(onDisk.resolutions["static/katex-BZy9Y_85.js"].entryFile, "entry.js");
});

test("multiple swaps in one call: both chunks dropped, both manifest entries present", (t) => {
  const upstreamKatex = `export const render = () => {};\n`;
  const vendorKatex = `export const render = () => {};\nexport { render };\n`;
  const upstreamLodash = `export const noop = () => {};\n`;
  const vendorLodash = `export const noop = () => {};\nexport { noop };\n`;

  const dir = createTempFixtureRoot("swap-vendor-multi-");
  t.after(() => rmSync(dir, { force: true, recursive: true }));
  const packagesRoot = join(dir, "node_modules");
  writeJsonFile(join(packagesRoot, "katex", "package.json"), { name: "katex", version: "0.16.11" });
  mkdirSync(join(packagesRoot, "katex", "dist"), { recursive: true });
  writeTextFile(join(packagesRoot, "katex", "dist", "katex.mjs"), upstreamKatex);
  writeJsonFile(join(packagesRoot, "lodash", "package.json"), { name: "lodash", version: "4.17.21" });
  mkdirSync(join(packagesRoot, "lodash"), { recursive: true });
  writeTextFile(join(packagesRoot, "lodash", "lodash.mjs"), upstreamLodash);

  const artifact = makeArtifact([
    makeChunk("static/katex-BZy9Y_85", { "runtime.js": vendorKatex }),
    makeChunk("static/lodash-ABC", { "runtime.js": vendorLodash }),
  ]);
  const outManifestPath = join(dir, "out", "resolutions.json");

  const { manifest, artifact: out } = swapVendorChunks({
    artifact,
    operations: [
      swapOp(),
      swapOp({
        id: "op_lodash",
        chunkPath: "static/lodash-ABC.js",
        identity: "lodash/lodash.mjs",
        package: "lodash",
        version: "4.17.21",
        subpath: "lodash.mjs",
      }),
    ],
    packagesRoot,
    outputManifestPath: outManifestPath,
  });

  assert.equal(manifest.counts.swapped, 2);
  assert.equal(hasChunk(out, "static/katex-BZy9Y_85"), false);
  assert.equal(hasChunk(out, "static/lodash-ABC"), false);
  const onDisk = JSON.parse(readFileSync(outManifestPath, "utf8"));
  assert.ok(onDisk.resolutions["static/katex-BZy9Y_85.js"]);
  assert.ok(onDisk.resolutions["static/lodash-ABC.js"]);
});

test("write:false returns manifest without touching disk; write:true writes", (t) => {
  const upstream = `export const render = () => {};\n`;
  const vendor = `export const render = () => {};\nexport { render };\n`;
  const fx = setupFixture({ upstreamBody: upstream });
  t.after(() => fx.cleanup());

  // write: false
  const artifact1 = makeArtifact([makeChunk("static/katex-BZy9Y_85", { "runtime.js": vendor })]);
  const outPath = join(fx.dir, "out", "resolutions.json");
  const { manifest, outputManifestPath } = swapVendorChunks({
    artifact: artifact1,
    operations: [swapOp()],
    packagesRoot: fx.packagesRoot,
    outputManifestPath: outPath,
    write: false,
  });
  assert.equal(manifest.counts.swapped, 1);
  assert.equal(outputManifestPath, null);
  assert.equal(existsSync(outPath), false);

  // write: true
  const artifact2 = makeArtifact([makeChunk("static/katex-BZy9Y_85", { "runtime.js": vendor })]);
  const res = swapVendorChunks({
    artifact: artifact2,
    operations: [swapOp()],
    packagesRoot: fx.packagesRoot,
    outputManifestPath: outPath,
    write: true,
  });
  assert.equal(res.outputManifestPath, outPath);
  assert.equal(existsSync(outPath), true);
});

test("named-from-default wrapper: object-literal default becomes named exports", (t) => {
  const fx = setupFixture({
    pkgName: "hello-data",
    installed: "1.2.3",
    subpath: "dist/data.js",
    upstreamBody: `export default { hello: "world", answer: 42 };\n`,
  });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([
    makeChunk("static/hello-data-ABC", {
      "runtime.js": `const hello = "world";\nconst answer = 42;\nconst all = { hello, answer };\nexport { hello, answer, all as default };\n`,
    }),
  ]);

  const result = swapVendorChunks({
    artifact,
    operations: [
      swapOp({
        id: "op_hello_data",
        chunkPath: "static/hello-data-ABC.js",
        identity: "hello-data/dist/data.js",
        package: "hello-data",
        version: "1.2.3",
        subpath: "dist/data.js",
        wrapperShape: "named-from-default",
      }),
    ],
    packagesRoot: fx.packagesRoot,
    outputWrapperDir: fx.wrapperDir,
    write: true,
  });

  assert.equal(result.manifest.counts.swapped, 1);
  const annotation = getArtifactVendorAnnotations(result.artifact).get("static/hello-data-ABC");
  assert.equal(annotation.swap.wrapperShape, "named-from-default");
  const wrapperPath = join(fx.wrapperDir, "static/hello-data-ABC", "runtime.js");
  const wrapperCode = readFileSync(wrapperPath, "utf8");
  assert.match(wrapperCode, /export default _d;/);
  assert.match(wrapperCode, /export const hello = _d\.hello;/);
  assert.match(wrapperCode, /export const answer = _d\.answer;/);
});

test("named-from-json-default wrapper: JSON object becomes default plus named exports", (t) => {
  const fx = setupFixture({
    pkgName: "@example/data",
    installed: "9.9.9",
    subpath: "sets/default.json",
    upstreamBody: `{"hello":"world","answer":42}\n`,
  });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([
    makeChunk("static/example-data-ABC", {
      "runtime.js": `const hello = "world";\nconst answer = 42;\nconst all = { hello, answer };\nexport { hello, answer, all as default };\n`,
    }),
  ]);

  const result = swapVendorChunks({
    artifact,
    operations: [
      swapOp({
        id: "op_example_json",
        chunkPath: "static/example-data-ABC.js",
        identity: "@example/data json",
        package: "@example/data",
        version: "9.9.9",
        subpath: "sets/default.json",
        wrapperShape: "named-from-json-default",
      }),
    ],
    packagesRoot: fx.packagesRoot,
    outputWrapperDir: fx.wrapperDir,
    write: true,
  });

  assert.equal(result.manifest.counts.swapped, 1);
  const wrapperPath = join(fx.wrapperDir, "static/example-data-ABC", "runtime.js");
  const wrapperCode = readFileSync(wrapperPath, "utf8");
  assert.match(wrapperCode, /const _d = \{/);
  assert.match(wrapperCode, /"hello": "world"/);
  assert.match(wrapperCode, /export const hello = _d\.hello;/);
  assert.match(wrapperCode, /export const answer = _d\.answer;/);
});

test("named-from-module-default wrapper: default-exported value can be re-exported under vendor alias", (t) => {
  const fx = setupFixture({
    pkgName: "hello-module",
    installed: "2.0.0",
    subpath: "dist/hello.mjs",
    upstreamBody: `const hello = () => "world";\nexport const version = "2.0.0";\nexport default hello;\n`,
  });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([
    makeChunk("static/hello-module-ABC", {
      "runtime.js": `const hello = () => "world";\nexport { hello as c, hello as default };\n`,
    }),
  ]);

  const result = swapVendorChunks({
    artifact,
    operations: [
      swapOp({
        id: "op_hello_module",
        chunkPath: "static/hello-module-ABC.js",
        identity: "hello-module/dist/hello.mjs",
        package: "hello-module",
        version: "2.0.0",
        subpath: "dist/hello.mjs",
        wrapperShape: "named-from-module-default",
      }),
    ],
    packagesRoot: fx.packagesRoot,
    outputWrapperDir: fx.wrapperDir,
    write: true,
  });

  assert.equal(result.manifest.counts.swapped, 1);
  const annotation = getArtifactVendorAnnotations(result.artifact).get("static/hello-module-ABC");
  assert.equal(annotation.swap.wrapperShape, "named-from-module-default");
  const wrapperPath = join(fx.wrapperDir, "static/hello-module-ABC", "runtime.js");
  const wrapperCode = readFileSync(wrapperPath, "utf8");
  assert.match(wrapperCode, /const __vendor_default__ = hello;/);
  assert.match(wrapperCode, /export default __vendor_default__;/);
  assert.match(wrapperCode, /export const c = __vendor_default__;/);
  assert.match(wrapperCode, /export const version = "2\.0\.0";/);
});

test("named-from-module-default wrapper: export-specifier default is rewritten correctly", (t) => {
  const fx = setupFixture({
    pkgName: "hello-module",
    installed: "2.0.0",
    subpath: "dist/hello.mjs",
    upstreamBody: `const hello = () => "world";\nconst side = "keep";\nexport { hello as default, side };\n`,
  });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([
    makeChunk("static/hello-module-ABC", {
      "runtime.js": `const hello = () => "world";\nexport { hello as c, hello as default };\n`,
    }),
  ]);

  swapVendorChunks({
    artifact,
    operations: [
      swapOp({
        id: "op_hello_module_specifier",
        chunkPath: "static/hello-module-ABC.js",
        identity: "hello-module/dist/hello.mjs",
        package: "hello-module",
        version: "2.0.0",
        subpath: "dist/hello.mjs",
        wrapperShape: "named-from-module-default",
      }),
    ],
    packagesRoot: fx.packagesRoot,
    outputWrapperDir: fx.wrapperDir,
    write: true,
  });

  const wrapperCode = readFileSync(join(fx.wrapperDir, "static/hello-module-ABC", "runtime.js"), "utf8");
  assert.match(wrapperCode, /export \{ side \};/);
  assert.match(wrapperCode, /const __vendor_default__ = hello;/);
  assert.doesNotThrow(() => parse(wrapperCode, { sourceType: "module" }));
});

test("named-from-module-default wrapper: missing default export fails closed", (t) => {
  const fx = setupFixture({
    pkgName: "hello-module",
    installed: "2.0.0",
    subpath: "dist/hello.mjs",
    upstreamBody: `export const hello = () => "world";\n`,
  });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([
    makeChunk("static/hello-module-ABC", {
      "runtime.js": `const hello = () => "world";\nexport { hello as c, hello as default };\n`,
    }),
  ]);

  assert.throws(
    () =>
      swapVendorChunks({
        artifact,
        operations: [
          swapOp({
            id: "op_hello_module_missing_default",
            chunkPath: "static/hello-module-ABC.js",
            identity: "hello-module/dist/hello.mjs",
            package: "hello-module",
            version: "2.0.0",
            subpath: "dist/hello.mjs",
            wrapperShape: "named-from-module-default",
          }),
        ],
        packagesRoot: fx.packagesRoot,
        outputWrapperDir: fx.wrapperDir,
        write: true,
      }),
    (err) => /named-from-module-default/.test(err.message) && /no default export/.test(err.message)
  );
});

test("wrapper generation paths: multiple wrapper-backed swaps can coexist in one call", (t) => {
  const fx = setupFixture({
    pkgName: "hello-data",
    installed: "1.0.0",
    subpath: "dist/data.js",
    upstreamBody: `export default { hello: "world" };\n`,
  });
  writeDependencyFile(fx, "hello-module", "dist/hello.mjs", `const hello = () => "world";\nexport default hello;\n`);
  writeJsonFile(join(fx.packagesRoot, "hello-module", "package.json"), {
    name: "hello-module",
    version: "2.0.0",
  });
  t.after(() => fx.cleanup());

  const artifact = makeArtifact([
    makeChunk("static/hello-data-ABC", {
      "runtime.js": `const hello = "world";\nconst all = { hello };\nexport { hello, all as default };\n`,
    }),
    makeChunk("static/hello-module-ABC", {
      "runtime.js": `const hello = () => "world";\nexport { hello as c, hello as default };\n`,
    }),
  ]);

  const result = swapVendorChunks({
    artifact,
    operations: [
      swapOp({
        id: "op_data",
        chunkPath: "static/hello-data-ABC.js",
        identity: "hello-data/dist/data.js",
        package: "hello-data",
        version: "1.0.0",
        subpath: "dist/data.js",
        wrapperShape: "named-from-default",
      }),
      swapOp({
        id: "op_module",
        chunkPath: "static/hello-module-ABC.js",
        identity: "hello-module/dist/hello.mjs",
        package: "hello-module",
        version: "2.0.0",
        subpath: "dist/hello.mjs",
        wrapperShape: "named-from-module-default",
      }),
    ],
    packagesRoot: fx.packagesRoot,
    outputWrapperDir: fx.wrapperDir,
    write: true,
  });

  assert.equal(result.manifest.counts.swapped, 2);
  assert.ok(existsSync(join(fx.wrapperDir, "static/hello-data-ABC", "runtime.js")));
  assert.ok(existsSync(join(fx.wrapperDir, "static/hello-module-ABC", "runtime.js")));
});
