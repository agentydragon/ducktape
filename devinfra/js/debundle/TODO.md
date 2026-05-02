# Debundler — Open Work Items

Forward-looking gaps in the Rust debundler. Items are written to be removed
once closed; this file is not a changelog.

## Excalidraw live-browser smoke

Build an open-source analog of gaffer's `tana/re/web/live_proxy:load_*`
test inside ducktape, against a Bazel-managed Excalidraw bundle. The
motivation: when a Tana debundler issue surfaces (proxy crash, AST
corruption, missing chunk, emit shape regression, optimisation
behaviour bug), reproducing the failure on Excalidraw lets us share
the repro in a public bug report, write a regression test that runs
in ducktape's open CI, and avoid leaking proprietary Tana bundle
detail. Excalidraw is open-source and broadly representative of "real
React + vendored chunks + dynamic imports + service worker."

### Bundle build

Use a Bazel-managed Excalidraw build. Two viable paths:

- **Pull a prebuilt deploy** (snapshot a specific `excalidraw.com`
  publish into `tana/x/upstream/excalidraw/snapshots/<version>/`
  — analogous to `tana/upstream/web/snapshots/`). Requires keeping
  the snapshot fresh enough that upstream's auxiliary endpoints (if
  any) still work. Easier to bootstrap.
- **Build from source under Bazel** — Excalidraw's `excalidraw-app/`
  builds with Vite; reproduce that build (npm + vite via
  aspect_rules_js, or via a `genrule` shelling to npm) and feed the
  output into the pipeline. More work upfront, but the bundle is
  reproducible from a single git pin and we control the optimisation
  level / minifier settings.

Either way, the build configuration must roughly match Tana's: minify
on, scrambled identifiers, production-tree-shaken, real chunk-split
boundaries. A development build (with un-mangled names and source
maps inlined) won't exercise the debundler's RE-relevant code paths.

### Spec scope

Not the round-trip minimum — the spec should exercise realistic-ish
module extraction and rename paths, the same shape the Tana spec
runs. Concretely:

- `apply_vendor_annotations` + `swap_vendor_chunks` over a couple of
  Excalidraw's actual vendor chunks (React, Roughjs, Pointers, etc.)
  so vendor-swap edge cases (named-from-default,
  named-from-module-default, default-only, JSON-default) get covered
  on a real bundle, not only synthetic fixtures.
- `materialize_logical_modules` over a handful of pre-identified
  Excalidraw source modules — pick ones whose shape is recognisable
  in the compiled output (a clearly-bounded React component, a pure
  geometry helper, a state slice). Goal: prove the materialiser
  recovers approximately the right symbols/files from a real
  scrambled bundle.
- A small set of `define_logical_module` rename operations on
  identifiers visible in the compiled output. Goal: exercise the
  rename pipeline at the same aggressiveness Tana uses.
- `rewrite_chunk_entry_specifiers` + `emit_browser_harness` so the
  output is a runnable app the live proxy can serve.

The exact module list / rename list is part of the implementation —
pick stable shapes that are unlikely to drift wildly when Excalidraw
upgrades. Stale picks become a self-test: if the materialiser fails
to find them, that's a real signal (either the bundle moved or our
matchers regressed).

### Smoke target contract

- runs `bazel test //devinfra/js/debundle/excalidraw:load_test` (or
  similar);
- builds the Excalidraw bundle through the same pipeline rule shape
  gaffer uses (`debundle_pipeline` from
  `tana/re/web/transforms/pipeline.bzl` — promote to ducktape as a
  prerequisite);
- starts the live-proxy binary against the resulting harness;
- drives a headless Chromium through the proxy, asserts:
  - no failed asset requests,
  - no console errors,
  - the canvas toolbar is visible (e.g. `[data-testid="toolbar"]`
    or whichever stable selector Excalidraw exposes),
  - a small interaction works (click the rectangle tool, click on
    the canvas, verify a shape was added — proves the React app is
    reactive after debundle).

### Hosting

Self-hosted, no MITM. Tana's smoke has to MITM the live host because
Tana auth/data is server-side; Excalidraw runs entirely in the
browser, so a self-hosted bundle is a fully working app. Self-hosting
removes the network dependency and CDN-rotation flakiness (the test
stays green even when excalidraw.com is down) and matches the
"reproduce against Excalidraw" workflow goal — a public, deterministic
smoke that doesn't depend on third-party uptime.

### Prerequisite

The pipeline rule (`debundle_pipeline` in
`tana/re/web/transforms/pipeline.bzl`) lives in gaffer-private today.
Promote it to ducktape (likely `devinfra/js/debundle/pipeline.bzl`)
before this work — otherwise the Excalidraw target needs a
gaffer→ducktape→gaffer circular dep, and other open-source corpora
can't use it without depending on gaffer.

### Workflow rule

When a Tana-side debundling issue is tractable on Excalidraw too,
prefer landing the regression test on this Excalidraw target (or a
smaller minimised e2e under `devinfra/js/debundle/e2e/`) rather than
only fixing Tana behind the private repo. The latter loses the
public-CI signal and the public-bug-report leverage.

## Import-binding collisions after rename

When `define_logical_module` renames an exported scrambled symbol on a
materialized module, the import-rewrite in consumer chunks emits the new
export name aliased back to the original local binding (the scrambled
name the consumer used in source). If the consumer file _also_ imports a
distinct scrambled symbol — from a different module — that happens to
share the same scrambled name, the local binding collides and the file
fails to parse with `SyntaxError: Identifier '<X>' has already been
declared`.

Tana's `static/index-DI2GynTv/entry.js` currently has 429 such collisions
across the renamed-symbol set, with the canonical pattern:

```js
import { aH } from "./ai/mcp/prompting_runtime.js";
import { buildTaskContextPrompt as aH } from "./ai/mcp/prompting/templates.js";
```

(plus a re-export `s3t as aH` further down). Two distinct source modules
both used the scrambled letter pair `aH` for unrelated symbols; the
mangler-rolled chunk that became `entry.js` resolved that internally,
but the post-debundle import-rewrite re-introduces the clash because it
preserves the consumer-side local-binding name verbatim.

**Minimal fix (do soon, separate PR):** when emitting a consumer import
of a renamed symbol, scan the file's existing import bindings; if the
alias-back to the original scrambled local name would collide with an
already-bound local from another import (or with an existing top-level
declaration / re-export name), mint a fresh local name (e.g., `aH$1`,
`aH$2`, ...) and rewrite all references to that local in the consumer
body to use the fresh name. Lives at the seam between rename and
import-statement rewrite in `logical_modules.rs` / `pipeline.rs`. This
unblocks the Tana smoke without doing the larger fold below.

**Future — propagate readable rename across consumers:** the minimal
fix above keeps the consumer-side body referring to the original
scrambled name (just possibly suffixed). A nicer endpoint is to fold
the new readable name through every consumer too: re-emit the
import as plain `import { buildTaskContextPrompt }` (no alias) and
rename every reference in the consumer body from the scrambled letter
pair to `buildTaskContextPrompt`. That's pure readability gain across
the whole tree once a rename lands, but it interacts with consumer
local-scope name collisions (a consumer that already has its own
`buildTaskContextPrompt` declaration must keep an alias) and with
chains of re-exports. Treat as a follow-up to the minimal fix — both
phases use the same scope-tracking infrastructure.

Until the minimal fix lands, `//tana/re/web/live_proxy:load_78d928dca7`
(Tana smoke) is expected to fail at JS parse on `entry.js`, blocking
the unauth-view selector swap and the no-console-errors /
no-failed-asset-requests tightening (otherwise gated A4 work).

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
