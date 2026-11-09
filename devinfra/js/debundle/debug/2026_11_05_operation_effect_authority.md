# Compositional operation authority: bounded returned-callable control

The neutral CLI control in `e2e/effect_tv_initializer_coverage_test.rs` creates a
returned callable with `function make() { return () => 3; }`, calls it between
two lexical writes to `marker`, and assigns the writes together
separately from the callable/result.
The existing dataflow-aware S-chain policy is selected in both comparisons.
There are no authored purity or no-reentry assertions.

At base `7fc04701455bc2c47fd6961b0140dce82a94d609`, the ordinary gate rejects one
mutual-constraining SCC between the result and markers modules. The reported edge
is an opaque at-init-call barrier, despite actual local-call execution and its
lexical receipts. Baseline RBE invocation:
<https://app.buildbuddy.io/invocation/51533308-aa72-43b3-985f-1d2967929f34>.

The two source operations were individually missing positive initialization-domain
coverage: canonical primitive-literal evaluation, and checked deferred callable
creation. The latter retains its existing captured-cell, body, async/generator
and activation guards; creating a callable never certifies invoking its body.
Their fragments compose through selected calls with the existing exact prefix
prerequisites, then seal only the matching normally completed root initializer.

A separate consumer seam also mattered: `dataflow_summarizable()` previously read
only raw classifier havoc. The bounded refinement requires both a completed
initialization-domain receipt and lexical operation-receipt authority for the
same owner. It discharges only `OpaqueAtInitCall`; other havoc, observed cells,
callee heap opacity, unknown-call fallback and strict-mode purity are preserved.
The live dataflow S-chain emitter previously read raw classifier havoc directly;
it now consumes the same residual-opacity projection as initialization-effect
diagnostics. Raw classifier provenance remains stored.

`e2e/effect_tv_initializer_coverage_test.rs` checks nested creation/invocation,
exact source-only dependency sets, an exact nonempty native-input dependency
through enclosing calls and root owners, and unknown/getter/reentry controls.
The imported primitive-return equivalent remains conservatively uncovered;
a foreign summary does not acquire local operation receipts.

The ordinary strict-mode control still rejects with its strict S-chain edge.
Array-push and field-read variants are deliberately outside this repair:
an opaque array push retains strict pair fallback even against a summarized
statement, and an observed property read stays ordered against an earlier
opaque operation even if the read has no additional unknown effects. Neither a coverage counter nor an empty
observed-access ledger licenses removing such a real observation constraint.

This neutral control establishes a public engine capability; it does not claim
acceptance of any private source/spec pair or replace the ordinary gate.
