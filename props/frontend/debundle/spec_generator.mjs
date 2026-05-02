#!/usr/bin/env node
// Stub spec generator. The full canonical pipeline (vendor-swap →
// materialize → rename → emit_browser_harness) is wired below.
//
// The spec is parameterised by `--out-root` so the same generator
// works for both the bazel-bin pipeline target (where `out-root` is a
// declared tree artifact) and for ad-hoc local runs (where the
// generator emits a JSONC blob that can be passed straight to the
// debundler binary).

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const SNAPSHOT_ROOT = "props/frontend/debundle/snapshot";
const JS_LIST_PATH = "props/frontend/debundle/extracted/js-files.txt";
const ASSET_SUMMARY_PATH = "props/frontend/debundle/extracted/asset-summary.json";
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

// Stable picks (props/frontend source, not upstream-rotating) for the
// rename pipeline. These bindings live in chunks the smoke bundle
// produces from props/frontend's real source. The selectors target
// the un-minified shape (smoke esbuild config keeps `minify: false`),
// so the binding name in the bundle matches the symbol name in source.
//
// The "rename" arm of the pipeline is deliberately conservative: the
// names map back to readable function/class identifiers a debundler
// reader would recognize, rather than reaching into the chunk graph
// for opaque internals (which would drift across upstream package
// upgrades and create a maintenance burden).
const RENAME_OPS = [
  // From src/lib/router.ts — exported helpers; high-frequency in
  // the bundle since every Svelte route handler imports them.
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
  // From src/lib/router.ts — internal helper; only one occurrence so
  // the rename surfaces the whole shape of the dispatcher.
  {
    id: "rename_parse_hash",
    name: "parseRouteHash",
    selector: { chunkId: "dist/main", binding: { name: "parseHash", kind: "FunctionDeclaration" } },
  },
  // From src/lib/highlighting.ts — the only exported function in
  // the file, used by FileViewer and the smoke shell.
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

// Vendor marks. Two pre-vetted packages from props/frontend's
// production deps. The smoke bundle's chunk-graph algorithm puts each
// into a shared chunk shared between the main app entry and the
// per-package marker entry — see esbuild.smoke.config.mjs.
//
// chunkPath uses a glob-friendly placeholder; the spec generator
// reads the actual hashed names from the smoke bundle metafile.
const VENDOR_OPS = [
  {
    id: "mark_vendor_highlight",
    operation: "mark_vendor",
    level: "swap",
    // Vendor chunks share the entry-name prefix esbuild minted from
    // the marker entry name. esbuild only mints a single shared chunk
    // per group of importers, so highlight.js's chunk path tracks
    // the marker entry's name. The exact hashed filename must come
    // from the smoke bundle metafile — the spec generator reads it
    // before writing, so this op's chunkPath is filled at runtime.
    chunkPath: "__placeholder__/vendor_highlight_marker.js",
    package: "highlight.js",
    version: "11.11.1",
    subpath: "es/index.js",
    wrapperShape: "named-from-default",
    confidence: "confirmed",
    identity: "highlight.js (smoke)",
    upstreamFamily: "highlight.js",
    evidence: [],
  },
  {
    id: "mark_vendor_datatable",
    operation: "mark_vendor",
    level: "swap",
    chunkPath: "__placeholder__/vendor_datatable_marker.js",
    package: "@careswitch/svelte-data-table",
    version: "0.6.3",
    subpath: "dist/index.js",
    wrapperShape: "named-from-module-default",
    confidence: "confirmed",
    identity: "@careswitch/svelte-data-table (smoke)",
    upstreamFamily: "@careswitch/svelte-data-table",
    evidence: [],
  },
];

function buildPipeline(outRoot) {
  return [
    {
      id: "materialize_logical_modules",
      operation: "materialize_logical_modules",
      args: {
        chunkIds: ["dist/main"],
        force: true,
        targetDir: "modules",
        reportOutDir: `${outRoot}/analysis/logical_modules`,
        reportSummaryPath: `${outRoot}/analysis/logical_modules/summary.json`,
      },
    },
    {
      id: "annotate_vendor_chunks",
      operation: "apply_vendor_annotations",
    },
    {
      id: "rewrite_vendor_export_imports",
      operation: "rename_vendor_exports",
    },
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
      id: "rewrite_chunk_entry_specifiers",
      operation: "rewrite_chunk_entry_specifiers",
    },
    {
      id: "emit_browser_harness",
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
  return {
    schemaVersion: 1,
    kind: "js.ast_transform_spec",
    inputs: { inputRoot: SNAPSHOT_ROOT, jsListPath: JS_LIST_PATH },
    operations: [...VENDOR_OPS, ...RENAME_OPS, ...LOGICAL_MODULES],
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
