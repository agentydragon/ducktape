#!/usr/bin/env node
// Spec generator for the props/frontend debundle smoke. Drives the
// canonical pipeline (vendor-swap → materialize → rename →
// emit_browser_harness) against the snapshot the BUILD's `:snapshot`
// rule produces.
//
// Pre-flight: reads `extracted/vendor-chunks.json` (also produced by
// `:snapshot`, classified from the smoke esbuild metafile) to pin
// each vendor mark op's `chunkPath` to the actual hashed filename
// esbuild emitted on this build. Without this step the smoke would
// fail every time esbuild changed its hash function.

import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const SNAPSHOT_ROOT = "props/frontend/debundle/snapshot";
const JS_LIST_PATH = "props/frontend/debundle/extracted/js-files.txt";
const ASSET_SUMMARY_PATH = "props/frontend/debundle/extracted/asset-summary.json";
const VENDOR_CHUNKS_PATH = "props/frontend/debundle/extracted/vendor-chunks.json";
const DEFAULT_OUT_ROOT = "props/frontend/debundle/out";

function parseArgs(argv) {
  const options = { outPath: null, outRoot: DEFAULT_OUT_ROOT };
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    switch (arg) {
      case "--out":
        options.outPath = requireValue(argv, ++index, arg);
        break;
      case "--out-root":
        options.outRoot = requireValue(argv, ++index, arg);
        break;
      default:
        throw new Error(`Unknown argument: ${arg}`);
    }
  }
  if (options.outPath === null) {
    throw new Error("--out is required");
  }
  return options;
}

function requireValue(argv, index, flag) {
  const value = argv[index];
  if (value === undefined) {
    throw new Error(`${flag} requires a value`);
  }
  return value;
}

function readVendorChunkMap() {
  // vendor-chunks.json reaches the spec generator via `data=` on
  // `:spec_generator_lib`, surfaced in the binary's runfiles tree.
  // The js_binary launcher chdir's to BAZEL_BINDIR (target config),
  // but the runfiles live under the *exec*-config bin tree
  // (`bazel-out/<exec>/bin/.../spec_generator.runfiles/_main/…`).
  // Walk a small list of candidate roots and pick the first match.
  const workspaceRoot = process.env.BUILD_WORKSPACE_DIRECTORY ?? process.env.BUILD_WORKING_DIRECTORY ?? process.cwd();
  const runfilesDir = process.env.RUNFILES_DIR;
  const runfilesWorkspace = process.env.JS_BINARY__RUNFILES;
  const candidates = [];
  if (runfilesWorkspace) candidates.push(resolve(runfilesWorkspace, "_main", VENDOR_CHUNKS_PATH));
  if (runfilesDir) candidates.push(resolve(runfilesDir, "_main", VENDOR_CHUNKS_PATH));
  if (process.env.BAZEL_BINDIR) candidates.push(resolve(process.env.BAZEL_BINDIR, VENDOR_CHUNKS_PATH));
  candidates.push(resolve(workspaceRoot, VENDOR_CHUNKS_PATH));
  for (const candidate of candidates) {
    try {
      return JSON.parse(readFileSync(candidate, "utf8"));
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
  throw new Error(
    `Could not read ${VENDOR_CHUNKS_PATH}; tried ${candidates.join(", ")}. Ensure //props/frontend/debundle:snapshot has built before invoking the spec generator.`
  );
}

// Stable picks (props/frontend source, not upstream-rotating) for the
// rename pipeline. These bindings live in the main chunk produced by
// the smoke bundle. The selectors target the un-minified shape (the
// smoke esbuild config keeps `minify: false`), so the binding name
// in the bundle matches the symbol name in source.
//
// The "rename" arm is deliberately conservative — names map back to
// readable function/class identifiers a debundler reader would
// recognize, rather than reaching into the chunk graph for opaque
// internals (which would drift across upstream package upgrades).
const RENAME_OPS = [
  {
    id: "rename_resolve",
    name: "resolveRoute",
    selector: { chunkId: "dist/main", binding: { name: "resolve", kind: "FunctionDeclaration" } },
  },
  {
    id: "rename_goto",
    name: "gotoRoute",
    selector: { chunkId: "dist/main", binding: { name: "goto", kind: "FunctionDeclaration" } },
  },
  {
    id: "rename_parse_params",
    name: "parseRouteParams",
    selector: { chunkId: "dist/main", binding: { name: "parseParams", kind: "FunctionDeclaration" } },
  },
  {
    id: "rename_parse_hash",
    name: "parseRouteHash",
    selector: { chunkId: "dist/main", binding: { name: "parseHash", kind: "FunctionDeclaration" } },
  },
  {
    id: "rename_highlight_lines",
    name: "highlightCodeLines",
    selector: { chunkId: "dist/main", binding: { name: "highlightLines", kind: "FunctionDeclaration" } },
  },
];

// Three logical modules pulled out of the main chunk. Each maps to a
// recognisable file in props/frontend source — the smoke checks that
// the materializer can recover the right shape from a real bundle
// (function declarations, exported bindings, dependency closure).
const LOGICAL_MODULES = [
  {
    id: "materialize_router",
    operation: "define_logical_module",
    chunkId: "dist/main",
    targetPath: "lib/router",
    members: [
      { binding: "parseHash", exportName: "parseHash" },
      { binding: "createRouter", exportName: "createRouter" },
      { binding: "pathname", exportName: "pathname" },
      { binding: "searchParams", exportName: "searchParams" },
      { binding: "goto", exportName: "goto" },
      { binding: "resolve", exportName: "resolve" },
      { binding: "parseParams", exportName: "parseParams" },
    ],
  },
  {
    id: "materialize_token",
    operation: "define_logical_module",
    chunkId: "dist/main",
    targetPath: "lib/token",
    members: [
      { binding: "needsToken", exportName: "needsToken" },
      { binding: "getToken", exportName: "getToken" },
      { binding: "setToken", exportName: "setToken" },
      { binding: "clearToken", exportName: "clearToken" },
      { binding: "captureTokenFromUrl", exportName: "captureTokenFromUrl" },
      { binding: "onAuthFailed", exportName: "onAuthFailed" },
    ],
  },
  {
    id: "materialize_highlighting",
    operation: "define_logical_module",
    chunkId: "dist/main",
    targetPath: "lib/highlighting",
    members: [{ binding: "highlightLines", exportName: "highlightLines" }],
  },
];

function buildVendorOps(vendorChunkMap) {
  return [
    {
      id: "mark_vendor_highlight",
      operation: "mark_vendor",
      level: "swap",
      chunkPath: vendorChunkMap.chunks["highlight.js"],
      package: "highlight.js",
      version: "11.11.1",
      subpath: "es/index.js",
      // highlight.js `es/index.js` is `import x from "..."; export
      // default x;` — the named-from-module-default wrapper shape
      // matches that pattern (rename a re-exported module default
      // back to a named export). The named-from-default shape would
      // require an object-literal default, which highlight.js's
      // identifier-based default doesn't satisfy.
      wrapperShape: "named-from-module-default",
      confidence: "confirmed",
      identity: "highlight.js (smoke)",
      upstreamFamily: "highlight.js",
      evidence: [{ path: vendorChunkMap.chunks["highlight.js"], line: 1, text: "smoke marker chunk: highlight.js" }],
    },
    {
      id: "mark_vendor_datatable",
      operation: "mark_vendor",
      level: "swap",
      chunkPath: vendorChunkMap.chunks["@careswitch/svelte-data-table"],
      package: "@careswitch/svelte-data-table",
      version: "0.6.3",
      subpath: "dist/index.js",
      // svelte-data-table's `dist/index.js` is a plain re-export
      // (`export { DataTable } from "./DataTable.svelte.js"`) — no
      // default to wrap, just a same-named pass-through. Omitting
      // `wrapperShape` selects the no-wrapper path: the debundler
      // swaps the chunk for the upstream package directly and
      // verifies that every vendor-side named export is present
      // upstream.
      confidence: "confirmed",
      identity: "@careswitch/svelte-data-table (smoke)",
      upstreamFamily: "@careswitch/svelte-data-table",
      evidence: [
        {
          path: vendorChunkMap.chunks["@careswitch/svelte-data-table"],
          line: 1,
          text: "smoke marker chunk: @careswitch/svelte-data-table",
        },
      ],
    },
  ];
}

function buildPipeline(outRoot) {
  return [
    // Run vendor stages before materialize so the vendor chunks are
    // visible at annotate time. Materialize's default settings keep
    // other chunks intact, but reordering hedges against future
    // materializer behavior changes — and matches the gaffer Tana
    // pipeline order more closely.
    { id: "annotate_vendor_chunks", operation: "apply_vendor_annotations" },
    { id: "rewrite_vendor_export_imports", operation: "rename_vendor_exports" },
    {
      id: "emit_vendor_wrappers",
      operation: "swap_vendor_chunks",
      args: {
        outputManifestPath: `${outRoot}/vendors/manifest.json`,
        outputWrapperDir: `${outRoot}/vendors/generated`,
        write: true,
      },
    },
    {
      id: "materialize_logical_module_files",
      operation: "materialize_logical_modules",
      args: {
        chunkIds: ["dist/main"],
        force: true,
        targetDir: "modules",
        reportOutDir: `${outRoot}/analysis/logical_modules`,
        reportSummaryPath: `${outRoot}/analysis/logical_modules/summary.json`,
      },
    },
    { id: "realize_chunk_entry_specifiers", operation: "rewrite_chunk_entry_specifiers" },
    {
      id: "emit_browser_test_app",
      operation: "emit_browser_harness",
      args: {
        assetSummaryPath: ASSET_SUMMARY_PATH,
        force: true,
        outDir: outRoot,
        snapshotRoot: SNAPSHOT_ROOT,
      },
    },
  ];
}

function buildSpec(outRoot) {
  const vendorChunkMap = readVendorChunkMap();
  return {
    schemaVersion: 1,
    kind: "js.ast_transform_spec",
    inputs: { inputRoot: SNAPSHOT_ROOT, jsListPath: JS_LIST_PATH },
    operations: [...buildVendorOps(vendorChunkMap), ...RENAME_OPS, ...LOGICAL_MODULES],
    pipeline: buildPipeline(outRoot),
  };
}

function writeSpec(spec, outPath) {
  const workspaceRoot = process.env.BUILD_WORKSPACE_DIRECTORY ?? process.env.BUILD_WORKING_DIRECTORY ?? process.cwd();
  const resolved = resolve(workspaceRoot, outPath);
  mkdirSync(dirname(resolved), { recursive: true });
  writeFileSync(
    resolved,
    `// Generated transform spec for the props/frontend debundle smoke. Edit the spec generator instead.\n${JSON.stringify(spec, null, 2)}\n`
  );
}

const options = parseArgs(process.argv.slice(2));
writeSpec(buildSpec(options.outRoot), options.outPath);
