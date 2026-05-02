# Debundler — Open Work Items

Forward-looking gaps in the Rust debundler. Items are written to be removed
once closed; this file is not a changelog.

## Excalidraw live-browser smoke

Build an open-source analog of gaffer's `tana/re/web/live_proxy:load_*`
test inside ducktape, against Excalidraw. The motivation: when a Tana
debundler issue surfaces (proxy crash, AST corruption, missing chunk,
emit shape regression), reproducing the failure on Excalidraw lets us
share the repro in a public bug report, write a regression test that
runs in ducktape's open CI, and avoid leaking proprietary Tana bundle
detail. Excalidraw is open source (Apache-2 / MIT mix), so its
debundled output can live in this repo. It's also a good representative
of "real React-driven web app with vendored chunks, dynamic imports,
service worker," which exercises most of the debundler features.

The smoke target's contract:

- runs `bazel test //devinfra/js/debundle/excalidraw:load_test` (or
  similar);
- builds the Excalidraw bundle through the same pipeline rule shape
  gaffer uses (`debundle_pipeline` from
  `tana/re/web/transforms/pipeline.bzl` — promote to ducktape if not
  already there);
- starts the live-proxy binary against the resulting harness and a
  pinned Excalidraw upstream;
- drives a headless Chromium through the proxy at the Excalidraw URL,
  asserts: no failed asset requests, no console errors, the canvas
  toolbar is visible (e.g. `[data-testid="toolbar"]` or whichever
  stable-looking selector Excalidraw exposes), and a small interaction
  works (click the rectangle tool, click on the canvas, verify a shape
  was added — the interaction's main job is to prove the React app is
  reactive after debundle, not to test Excalidraw itself).

Open design questions to resolve when picking this up:

1. **Where the Excalidraw bundle comes from.** Three options:
   - a. Pin a published Excalidraw release and download the prebuilt
     artifact via `http_archive` / `nix-prefetch-url`. Requires
     Excalidraw to publish prebuilt artifacts (verify; their releases
     may be source-only, in which case fall through to (b) or (c)).
   - b. Build Excalidraw from source as a Bazel rule, using their
     `excalidraw-app/` directory. Heavier — requires reproducing
     their build (vite). Pro: bundle is reproducible from a single
     git pin.
   - c. Snapshot a specific public deploy of `https://excalidraw.com`
     the same way `tana/upstream/web/snapshots/` snapshots a Tana
     deploy. Easiest but couples to their CDN cache lifetime; the
     same staleness/CSS-rotation problem the Tana smoke just hit.
2. **Self-host or proxy real Excalidraw.** Tana's smoke has to MITM
   the live host because Tana auth/data is server-side. Excalidraw
   runs entirely in the browser with optional collaboration; a
   self-hosted Excalidraw is a fully working app. Self-hosting
   removes the network dependency and CDN-rotation flakiness — the
   test stays green even when excalidraw.com is down. Strongly
   prefer self-host; reserve "proxy real upstream" for a separate
   target if we want to test against rotating production specifically.
3. **What spec to run.** Start with the minimum: just chunk-split,
   parse, normalize. No `materialize_logical_modules` (Excalidraw's
   reverse-engineering target isn't this repo's mission), no vendor
   swap (Excalidraw's vendors are the same React/etc. ecosystem we'd
   already be running through aspect_rules_js — could vendor-swap
   them, optional). Smoke proves the debundler can round-trip a real
   bundle into a working app, not that we're decomposing it
   semantically.

The pipeline rule used for gaffer (`debundle_pipeline` in
`tana/re/web/transforms/pipeline.bzl`) is gaffer-side today. As part of
this task it should move to ducktape (`devinfra/js/debundle/` or
similar) so the Excalidraw target can use it without a circular
gaffer→ducktape→gaffer dep, and so future open-source corpora can use
it without depending on gaffer.

Workflow rule for downstream: when a Tana-side debundling issue is
tractable on Excalidraw too, prefer to land the regression test on
this Excalidraw target (or a smaller minimised e2e under
`devinfra/js/debundle/e2e/`) rather than only fixing Tana behind the
private repo. The latter loses the public-CI signal and the public-
bug-report leverage.

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
