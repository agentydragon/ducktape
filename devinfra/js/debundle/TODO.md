# Debundler — Open Work Items

Forward-looking gaps in the Rust debundler. Items are written to be removed
once closed; this file is not a changelog.

## Excalidraw live-browser smoke

Build an open-source live-browser smoke test for the debundler against
a Bazel-managed Excalidraw bundle. The motivation: when a debundler
issue surfaces against a private upstream corpus (proxy crash, AST
corruption, missing chunk, emit shape regression, optimisation
behaviour bug), reproducing the failure on Excalidraw lets us share
the repro in a public bug report, write a regression test that runs
in ducktape's open CI, and avoid leaking proprietary upstream bundle
detail. Excalidraw is open-source and broadly representative of "real
React + vendored chunks + dynamic imports + service worker."

### Bundle build

Use a Bazel-managed Excalidraw build. Two viable paths:

- **Pull a prebuilt deploy** (snapshot a specific `excalidraw.com`
  publish). Requires keeping the snapshot fresh enough that
  upstream's auxiliary endpoints (if any) still work. Easier to
  bootstrap.
- **Build from source under Bazel** — Excalidraw's `excalidraw-app/`
  builds with Vite; reproduce that build (npm + vite via
  aspect_rules_js, or via a `genrule` shelling to npm) and feed the
  output into the pipeline. More work upfront, but the bundle is
  reproducible from a single git pin and we control the optimisation
  level / minifier settings.

Either way, the build configuration must produce a realistic
production bundle: minify on, scrambled identifiers,
production-tree-shaken, real chunk-split boundaries. A development
build (with un-mangled names and source maps inlined) won't exercise
the debundler's RE-relevant code paths.

### Spec scope

Not the round-trip minimum — the spec should exercise realistic-ish
module extraction and rename paths, the same shape a private-corpus
spec runs. Concretely:

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
  rename pipeline at realistic aggressiveness.
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
- builds the Excalidraw bundle through the shared `debundle_pipeline`
  rule (<pipeline.bzl>);
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

Self-hosted, no MITM. Private-corpus smokes generally have to MITM the
live host because their auth/data is server-side; Excalidraw runs
entirely in the browser, so a self-hosted bundle is a fully working
app. Self-hosting removes the network dependency and CDN-rotation
flakiness (the test stays green even when excalidraw.com is down) and
matches the "reproduce against Excalidraw" workflow goal — a public,
deterministic smoke that doesn't depend on third-party uptime.

### Workflow rule

When a private-corpus debundler issue is tractable on Excalidraw too,
prefer landing the regression test on this Excalidraw target (or a
smaller minimised e2e under `devinfra/js/debundle/e2e/`) rather than
only fixing it behind the private repo. The latter loses the
public-CI signal and the public-bug-report leverage.

## Propagate readable rename across consumers

When `define_logical_module` renames a scrambled binding `<scrambled>`
to readable `<readable>`, the consumer-side import is currently
emitted as `import { <readable> as <scrambled> } from "..."` and every
reference in the consumer body still spells the original scrambled
local. The disambiguation pass in `logical_modules.rs` only mints a
fresh `<scrambled>$N` when the original local would collide.

A nicer endpoint is to fold the new readable name through every
consumer too: drop the alias (`import { <readable> }`) and rename
every reference in the consumer body from `<scrambled>` to
`<readable>`. That's pure readability gain — instead of `fp(x)` in a
consumer's body, the body reads `<readable>(x)`.

Edge cases the implementation must handle:

- A consumer that already has its own top-level `<readable>` decl
  must keep the alias (collision in the consumer's own scope).
- Chains of re-exports (`export { <scrambled> } from "..."`) need the
  re-export's `orig` rewritten too.
- Locals that shadow `<readable>` inside a function body must not be
  rewritten.

Reuse the scope-tracking infrastructure the disambiguation pass
already builds.

## RE coverage side-output

Implemented in <scrambled_id_frequencies.rs> as a side output of every
manifest-emitting pipeline run (`emit_browser_harness`, `write_js_tree`).
The JSON lands at `<out_dir>/scrambled-identifier-frequencies.json` and
the path is recorded on the stage's manifest under
`scrambledIdentifierFrequencies`.

The scrambled-name heuristic in `is_scrambled_name` is intentionally
conservative on the side of "scrambled"; documented edge cases live in
the focused unit tests at the bottom of the module. Two known
tunability concerns recorded as in-code TODOs:

- The length-5 mixed-case arm flags `setId`-shaped names as scrambled.
  This is technically a false positive for some hand-written short
  developer names. Tightening once we have real-bundle calibration data
  is recorded inline.
- `__name`-style identifiers (length > 4 with `__` prefix) are flagged
  scrambled because they're typically compiler/runtime internals. If a
  developer codebase deliberately uses leading-underscore short names,
  this surfaces them.

## Logical materialization breadth

The current `materialize_logical_modules` covers top-level
function/class/variable declaration movement and simple dependency closure.
Still to do:

- Full boundary analysis data model and selected owner cache files.
- Full lowering matrix: binding placement reports, attached side-effects,
  staged-shell edge cases beyond the focused fixture.
- Owner-fragment modeling parity for nested declarations and re-exports.

## Vendor swap edge cases

- `named-from-default`: documented acceptance contract is "upstream
  default export is an object literal whose keys are `Prop::KeyValue`
  with `Ident` or `Str` names". `e2e/vendor_swap_test.rs` pins the
  accepted shape and explicitly rejects two adjacent shapes:
  - **Shorthand props** (`export default { ping, pong }` where `ping`
    and `pong` are local refs) are silently skipped by
    `collect_default_export_object_keys` and the wrapper fails the
    missing-keys check. Real-world vendor `index.mjs` files commonly
    use shorthand, so accepting them is a useful relaxation. Fix:
    extend the prop-walk to read shorthand keys from the prop's own
    name. Should be a few-line change with one new accepted-shape
    test.
  - **Class / function default declarations** (`export default class
{}`) don't surface as `ExportDefaultExpr` and bail with "no
    export default declaration". Open whether to accept (less common
    for "named-from-default" wrapper shape) or document as
    intentionally out-of-scope.

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
