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

`:debundle` is the residual baseline: the unmodified chunks, no module claims,
no vendor substitutions, every chunk `inline_in_entry`. `gen_inputs.py` writes
the JS list and tree config from the built chunk roster and
`spec/config_template.yaml`, because esbuild names shared chunks by content
hash. `:analysis` selects its saved analysis.

`test_debundle_tree` checks the accepted tree is complete: no `GATE_REJECTED`,
every input chunk has a non-empty emitted entry and an accepting gate and
translation-validation summary, and every static chunk import names an emitted
chunk. Dynamic imports and browser loading are left to the live-browser smoke.

## Shape and cost

Measured at `@excalidraw/excalidraw` 0.18.1 (2026-11-10):

- 75 JS chunks, 4,936,831 bytes: `main` (802,906 bytes); a shared `chunk-*`
  holding most of the editor (544,734 bytes); a 1,825,162-byte chunk shared by
  the two lazy font-subsetting chunks, 96% base64 WASM strings; five small
  shared chunks; and 67 chunks reached only by `import()` (54 locales, font
  subsetting, image, file-system and pica helpers).
- Largest chunks by top-level structure, from the run's `chunk.json` counts:
  the shared editor chunk has 274 top-level declarations, 29 side-effect
  statements and 1,879 top-level bindings (1,859 source units); `main` has 493
  declarations, 119 side-effect statements and 1,517 bindings (1,626 source
  units).
- The residual baseline is gate-accepted and translation-validated for all 75
  chunks; input-chunk admission (A1/A3/A5) refuses none, so the spec carries no
  `admission_overrides`.
- `DebundleAnalyze` on RBE, two cold runs: 304 s wall, 337 s CPU, 939–942 MiB
  peak RSS. `DebundleProjectAnalysis` ~2 s; `DebundlePipeline` 11–14 s.

### Cost

Analyze reruns whenever the analyzer's compiled closure or the bundle changes,
which is common in debundle pull requests, and its ~5 minutes are nearly
single-threaded (CPU/wall ≈ 1.1). `:debundle`,
`:analysis` and `test_debundle_tree` are therefore tagged `postsubmit`: pushes to
`devel` build and test them, pull requests do not. Run them on a branch with
`bbr test //devinfra/js/debundle/excalidraw:all`.

## What it represents, and what it does not

It represents: minified, identifier-mangled, tree-shaken ESM with real
chunk-split boundaries (shared chunks, locale and lazy-feature chunks reached by
`import()`), bundled React and other vendor code, `import.meta.url` asset and
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
3. Build `:debundle` and re-measure (below); update anything in `spec/` that
   names chunks or source shapes, validating the new source/spec pair as one.

## License

Excalidraw is MIT-licensed (`@excalidraw/excalidraw` `package.json`). No bundle
bytes are committed; Bazel fetches the package from the npm registry.
