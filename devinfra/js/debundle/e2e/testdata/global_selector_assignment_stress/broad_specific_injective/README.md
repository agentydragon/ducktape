# Broad Specific Injective Global Selector Fixture

This fixture is a public, anonymized stand-in for the hard global-selector
assignment shape: many alpha-renamed declarations match one broad selector,
most of those declarations are pinned by more specific selectors, and the
remaining broad claim becomes unique only through `all_different`.

The source is intentionally synthetic. It does not preserve names, literals,
module paths, or source text from any private bundle.

## Shape

- `source.js` declares twelve same-shape exported functions, `route00` through
  `route11`.
- Each route has the same structural body and the same stable labels except for
  one slot literal, `slot-00` through `slot-11`.
- Local variable names differ across routes to mimic `identifiers: alpha_all`
  without requiring real minified names.
- `pending_selector_constraints.yaml` sketches the future benchmark contract:
  eleven specific targets pin `slot-00` through `slot-10`; one broad target
  matches every route and is forced to `route11` only by the target-level
  `all_different` constraint.

The fixture is not wired into a Bazel target yet. It should become executable
when the selector backend benchmark harness can build a `SelectorProgram` from
this pending contract and run alternate `SelectorConstraintModel` backends.

## Expected Semantics

- Without `all_different`, the broad target is ambiguous over twelve owners.
- With the eleven specific claims and target-level injectivity, the broad target
  is forced to the one remaining owner, `route11`.
- Every backend should return the same unique claim map.

## Bottleneck Shape

This fixture stresses the costly selector-assignment shape without copying real
code:

- a broad source match creates a large candidate domain for one target;
- specific selectors create many small domains that overlap the broad one;
- alpha-renamed locals prevent spelling-based pruning;
- injectivity is the semantic reason the broad target becomes categorical.

Scale the same template by increasing route count `N` and pinning `N - 1`
specific slots. Useful points are `N = 12` for smoke, `N = 48` for interactive
regression checks, and `N = 96` for offline profiling once a real harness
exists.
