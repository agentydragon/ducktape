# Availability-aware foreign callback source promotion

`callback_future_availability_test` exposes an incorrect rejection with an
imported host whose unsupported rest-argument body falls back to retained
callbacks. A callback reads a future `const`/`let` callable and throws in its TDZ.
The callable's initializer retains a later `styles` read, but cannot have run at
that prefix. The source prints `ReferenceError ready`; extracting `styles` is
valid because the early callback cannot reach that later body.

The local fallback already uses `extend_available_source_units`: retain the
predecessor's future-binding read, stop traversal into its unavailable sole
lexical initializer, and revisit it after initialization. Foreign promotion used
an unconditional synchronous closure, producing a false early `styles` read.
The immutable `SourcePromotionView` cache preserved that earlier discrepancy.

The cached census now carries the initialized-binding requirements of traversed
lexical targets. Actual callable roots remain available independently of their
declaration cell, matching local fallback. When the requirements hold, the
immutable census can be reused. Otherwise foreign promotion uses the same
availability-aware expansion as local fallback at the current module prefix.
Incomplete reachability continues to retain the full conservative closure.
Values, callback graphs, effects, and class promotion are still evaluated for
each operation; no execution result is cached by this change.

The real CLI regression checks both local/imported hosts and `const`/`let`
initializers, emitted Node output, later hoisted functions, callbacks created
before/during the host initializer, moving the future binding itself, and a
second fallback after initialization. The latter controls retain structured
refusal when moving `styles` or `future` would change the observed TDZ.

Before the repair, RBE `92898c7f-61ef-48eb-a60e-6c17fd0a79cd` fails the positive
future-initializer case with an atomic conflict, while all five negative/revisit
controls pass. After the repair, RBE `8059e2ce-db1a-425d-a278-6cabfe8c685a` passes
analysis, callback future availability, unknown-call intermediate availability,
bounded callback fallback heap, and imported-class TDZ suites. Final RBE
`a9b09f23-369f-4de7-a40e-1f1dc405de36` also passes the expanded local/imported
`const`/`let` CLI controls. Canonical extraction is checked separately by the
parent lane.
