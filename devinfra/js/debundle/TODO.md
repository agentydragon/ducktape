# Debundler — Open Work Items

Forward-looking gaps in the Rust debundler. Items are written to be removed
once closed; this file is not a changelog.

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
