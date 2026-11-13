# Excalidraw debundle corpus

A public, Bazel-built production bundle of the [Excalidraw](https://github.com/excalidraw/excalidraw)
editor that the debundler runs against in open CI, so realistic acceptance,
regression and performance signals and public bug reproductions do not depend on
private corpora.

## What it is

`:bundle` is an esbuild production build (`minify`, ESM, code splitting,
`NODE_ENV=production`) of `app/main.js`, which mounts `<Excalidraw/>` with
React 19 as an embedding application would. Everything comes from the npm
registry through `rules_js`: `@excalidraw/excalidraw` is pinned exactly in the
root `package.json` and by integrity in `pnpm-lock.yaml`; React, roughjs,
jotai, radix and the other dependencies resolve through the same lockfile.
The `app/vendor/` entry points each reach one package but not the editor, so
esbuild splits that package into its own shared chunk, as an application's
vendor entries would.

`gen_inputs.py` writes the JS list, a copy of the chunks and the tree config
from the built chunk roster, `spec/config_template.yaml` and
`spec/chunk_aliases.yaml`: esbuild names shared chunks by content hash, so a
chunk the spec names gets a stable ID, found through the esbuild metafile by a
module it bundles. Every other chunk is `inline_in_entry`.

## Spec

`:debundle` runs the spec; `:analysis` selects its saved analysis.

The [module specs](spec/modules) use structural source selectors and mirror
original paths in the pinned
[`v0.18.1` source tree](https://github.com/excalidraw/excalidraw/tree/v0.18.1/packages).
Chunk directories identify bundle inputs; paths inside them identify upstream
source homes. [Vendor marks](spec/vendor_marks.yaml) configure package swaps.

The spec is partial: unmatched code stays in each entry in source order.
Acceptance of the whole tree does not measure readable extraction coverage.
Generated `reports/output.json` records named-module size fractions;
`reports/tree/<chunk>/modules.json` records the actual claimed members.

`test_debundle_tree` verifies complete accepted output and resolvable static
chunk imports. `spec_output_test` checks the spec's emitted contracts through
the e2e helpers. `test_smoke` loads the emitted tree in Chromium, draws a
rectangle, exercises undo/redo and runs behavior probes for recovered modules.

The browser harness serves Excalidraw's fonts from the pinned package and
builds its import map from `reports/vendor_swaps.json`. Startup and drawing do
not load every lazy locale, font-subsetting or file-system chunk.

Candidates the gate or vendor strip rejected, kept out of the spec:
`APP_STATE_STORAGE_CONF` and `KEYS`/`isDarwin` (impure initializers on the
editor chunk's side-effect chain), welcome-screen components (each
`X.displayName = …` write chains them with their neighbours), nanoid and roughjs
`partial_swap`s (residual editor code shares their implementation; roughjs also
exports three subpaths), and open-color (a JSON default).

`:canonical_summary` reduces the accepted tree to per-chunk gate and
translation-validation verdicts and emitted-file hashes, so an engine change can
be diffed against its base.

## Shape and cost

Measured at `@excalidraw/excalidraw` 0.18.1 (2026-11-10):

- 79 JS chunks, 4,937,119 bytes: `main` (802,981 bytes, 1,628 source units);
  the shared `editor` chunk (517,159 bytes, 1,787 source units); a
  1,825,162-byte chunk shared by the two lazy font-subsetting chunks, 96% base64
  WASM strings; the roughjs (27,188 bytes) and clsx shared chunks with their
  two entry stubs; five small shared chunks; and 67 chunks reached only by
  `import()` (54 locales, font subsetting, image, file-system and pica
  helpers).
- Gate-accepted and translation-validated for all 79 chunks; input-chunk
  admission (A1/A3/A5) refuses none, so the spec carries no
  `admission_overrides`.
- `DebundleAnalyze` on RBE: 39.5–43.6 s wall / 59.5–64.9 s CPU / 927–988 MiB
  peak RSS, and 48.6 s / 50.2 s / 808 MiB with `analysis_threads = 1`. With roughjs inlined
  in the editor chunk it took about 305 s (<../perf/excalidraw_roughjs_analyze.md>).
  `DebundlePipeline` 10.9–24.5 s.

That is cheap enough to run on every pull request whose affected set includes
it, so no target here is tagged `postsubmit`.

## What it represents, and what it does not

It represents: minified, identifier-mangled, tree-shaken ESM with real
chunk-split boundaries (shared chunks, locale and lazy-feature chunks reached by
`import()`, vendor chunks split by extra entry points), vendor code bundled into
application chunks (React and most dependencies), `import.meta.url` asset and
worker URLs, and large base64-embedded WASM strings.

It does not represent: the excalidraw.com application (collaboration, Firebase,
PWA/service worker, the app shell); a Vite/Rollup chunking strategy; or code
compiled only once (the published package is itself esbuild-minified before this
build re-bundles it). `@excalidraw/mermaid-to-excalidraw` is external: the
mermaid closure would add ~105 lazy chunks and ~3.5 MB that only the
text-to-diagram dialog loads, so that dialog cannot load in this build.

## Bumping the pin

1. Change the exact `@excalidraw/excalidraw` version in the root `package.json`.
2. Build `//devinfra/js/debundle/excalidraw:bundle` once; it fails after the
   managed lockfile update, then build again and commit `pnpm-lock.yaml`
   (<../../../docs/lockfiles.md>). Never run `pnpm install`.
3. Build `:debundle` and re-measure. Update `spec/`: aliases whose module moved
   chunks, the clsx chunk's export name in `vendor_marks.yaml`, vendor versions
   in `package.json` matching the ones Excalidraw resolves, and any selector the
   new source no longer matches, validating the new source/spec pair as one.

## License

Excalidraw is MIT-licensed (`@excalidraw/excalidraw` `package.json`). No bundle
bytes are committed; Bazel fetches the package from the npm registry.
