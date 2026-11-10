# Excalidraw debundle corpus

A public, Bazel-built production bundle of the [Excalidraw](https://github.com/excalidraw/excalidraw)
editor that the debundler runs against in open CI, so realistic acceptance,
regression and performance signals and public bug reproductions do not depend on
private corpora. Plan: <../plans/excalidraw_live_smoke.md>.

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
