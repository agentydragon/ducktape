# Excalidraw bundle — debundler benchmark fixture

Frozen snapshot of the production Excalidraw web app, fetched from
<https://excalidraw.com>. Used as a Tana-shaped (Vite + Rollup ESM,
`[name]-[hash].js`, terser-minified) corpus for debundler benchmarks.

## Source

- URL: <https://excalidraw.com>
- Excalidraw deploy version: `2026-04-20T20:07:00Z-b1c6bfc`
- Fetched: 2026-04-26
- License: MIT (https://github.com/excalidraw/excalidraw/blob/master/LICENSE)

## Files

| Path | Size | Role |
| --- | --- | --- |
| `static/index-C2pz2mrZ.js` | 2.0 MB | App entry chunk (`<script type=module>`) |
| `static/mermaid-to-excalidraw-D-aVQaad.js` | 581 KB | Preloaded vendor chunk (`<link rel=modulepreload>`) |

## Running the benchmark

```bash
bb run --remote_executor='' //devinfra/js/debundle/harness:benchmark_excalidraw
# Or to vary the merge count:
bb run --remote_executor='' //devinfra/js/debundle/harness:benchmark_excalidraw -- --merges 50
```

The pipeline runs `load_js_chunks → compute_js_asts → normalize_js_chunks →
split_scope_hoisted_js_tree → extract_atomic_modules → merge_modules` on the
fixture and prints per-stage timings. Merge operations are synthesized by
pair-folding the first `2N` atoms on the chunk that produced the most atoms.

## Refreshing

`refresh.sh` redownloads the current excalidraw.com chunks. Hashes change on
every deploy, so refreshing requires editing the URLs to match. After
refresh, update `js-files.txt`, the table above, and the deploy version
string.
