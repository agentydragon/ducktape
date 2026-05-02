# Debundler — Open Work Items

Forward-looking gaps in the Rust debundler. Items are written to be removed
once closed; this file is not a changelog.

## Internalize input loading

`load_js_chunks` / `compute_js_asts` / `normalize_js_chunks` are no longer
exposed in the spec — PR #1443 made them auto-prepended from the spec-level
`inputs` block — but they're still implemented as pipeline stages that the
runner virtually inserts into the dispatch list. There is no remaining
caller that benefits from the stage shape: the binary always runs them
exactly once, in the same order, with args sourced from `inputs`. They
should be plain startup code paths in `pipeline.rs` (or a small
`load_inputs.rs`), not stages in the dispatch table or stage-id enum.

Once internalized:

- the `STAGE_IDS` for the three input-loading stages can be removed,
- the dispatch arms for their `operation` strings can be deleted, and
- the spec validator no longer needs the "auto-prepend" shim — `inputs`
  becomes the single way these run.

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
