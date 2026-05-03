# Debundler — Open Work Items

Forward-looking gaps in the Rust debundler. Items are written to be removed
once closed; this file is not a changelog.

## props/frontend live-browser smoke

The open-source analog of gaffer's `tana/re/web/live_proxy:load_*`
lives at `//props/frontend/debundle:load_props_frontend`. It drives
the canonical pipeline (`apply_vendor_annotations` →
`rename_vendor_exports` → `swap_vendor_chunks` →
`materialize_logical_modules` → identifier renames →
`rewrite_chunk_entry_specifiers` → `emit_browser_harness`) against a
multi-chunk smoke bundle of `props/frontend`'s real Svelte source,
serves the harness with a tiny self-hosted Node http server (no
MITM — `props/frontend` has no server-side dependency), and drives
headless Chromium through it.

The smoke bundle is a parallel build to the production
`//props/frontend:bundle`; production stays single-chunk and
unchanged. The smoke variant adds two marker entries
(`vendor_highlight_marker.ts`, `vendor_datatable_marker.ts`) so
esbuild's chunk-graph algorithm puts each vendored package
(`highlight.js`, `@careswitch/svelte-data-table`) in its own
shared chunk — giving the pipeline distinct vendor chunks to swap
against the upstream packages from `node_modules/`.

### Workflow rule

When a Tana-side debundling issue surfaces, prefer reproducing it
against `props/frontend/debundle` (or a smaller minimised e2e under
`devinfra/js/debundle/e2e/`) rather than only fixing it behind the
private repo. The latter loses the public-CI signal and the
public-bug-report leverage.

### Open follow-ups

- Esbuild emits content-hashed chunk names that differ between
  target and exec build configs (subtle path differences in
  runfiles trees perturb the hash). The current
  `prepare_snapshot.mjs` writes `extracted/vendor-chunks.json`
  classifying each chunk by metafile inspection, which the spec
  generator reads to pin vendor `chunkPath`s — but the
  spec-generator runs in exec config and reads its config-version
  of the file, while the debundler runs in target config and
  consumes the target-version snapshot. The two configs disagree on
  chunk filenames, so the vendor-mark op rejects the chunk path it
  was given. Either pin esbuild's chunk-name template to remove the
  hash (`chunkNames: "[name]-shared"`), or have prepare_snapshot
  rename the chunk files to deterministic names (e.g.
  `dist/vendor-highlight.js`, `dist/vendor-datatable.js`) before
  the snapshot ships out.
- The smoke shell (`smoke_shell.svelte`) reproduces the production
  `App.svelte` chrome (`<nav>`, `<h1>Props</h1>`, nav links) so the
  load-test selectors stay close to the production surface, but
  it isn't the production component. Once `//props/frontend:app`
  is exposed to the debundle subpackage (or once the production
  pages stop hitting the backend on mount), the smoke can mount
  `App.svelte` directly.
- The render targets `data-debundle-smoke="…"` attributes on the
  smoke shell only; production source has no `data-testid`-style
  attributes. If the load test's selectors prove unstable as the
  shell evolves, document the alternatives (semantic selectors
  scoped to `<header>`, `<nav>`, `<main>`) rather than reaching
  into production source for new attributes.

## Propagate readable rename across consumers

The disambiguation pass in `logical_modules.rs` keeps the consumer-side
body referring to the original scrambled name (with a `$N` suffix on
collisions). A nicer endpoint is to fold the new readable name through
every consumer too: re-emit the import as plain `import {
buildTaskContextPrompt }` (no alias) and rename every reference in the
consumer body from the scrambled letter pair to
`buildTaskContextPrompt`. That's pure readability gain across the
whole tree once a rename lands, but it interacts with consumer
local-scope name collisions (a consumer that already has its own
`buildTaskContextPrompt` declaration must keep an alias) and with
chains of re-exports. Reuse the same scope-tracking infrastructure
the disambiguation pass already builds.

## RE coverage side-output

Re-introduce the deleted `extract_scrambled_identifier_frequencies` analysis
(JS source of truth was `analysis/identifier_frequency.mjs`, ~600 LOC) as a
**side output of every pipeline run** rather than a separate stage with its
own `force` / `limit` / `excludedSymbolFiles` knobs. The intent: emit a
machine-readable coverage summary that ranks the still-scrambled top-level
symbols by frequency, so RE workflows can see "% of bundle understood" and
prioritize the next rename wave. Keep the report keyed by stable selector
identity so it stays meaningful across upstream version bumps.

Until this lands, downstream specs should not invoke a scrambled-identifier
stage; gaffer-private's spec generator currently drops it.

## Logical materialization breadth

The current `materialize_logical_modules` covers top-level
function/class/variable declaration movement and simple dependency closure.
Still to do:

- Full boundary analysis data model and selected owner cache files.
- Full lowering matrix: binding placement reports, attached side-effects,
  staged-shell edge cases beyond the focused fixture.
- Owner-fragment modeling parity for nested declarations and re-exports.

## Vendor swap edge cases

- `named-from-default`: settle and document the upstream-formatting
  acceptance criteria — currently SWC parses anything that yields a default
  export, which may be more liberal than callers expect.
- `named-from-module-default`: handle anonymous default function/class
  declarations (currently rejected).

## Analysis semantics breadth

The focused fixtures exercise the core access model. Validate or extend
behavior for:

- Class fields, static blocks, computed keys, nested function bodies.
- Replayable side-effect attachment.
- Top-level side-effect classification across uncommon initializer shapes.

## Corpus breadth

Current passing surface is centered on small synthetic fixtures and the
mock browser bundle. Extend to:

- Large vendor-heavy graphs.
- Unusual dynamic import forms.
- HTML/runtime asset layouts outside the current corpus.
