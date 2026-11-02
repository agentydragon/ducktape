# Evaluated call results

## Shared call boundary

An ordered body such as `function convert(input) { return String(input); }`
could not use an existing native call proof through its local-only call
adapter. Foreign summaries and local invocations also had separate consumers.
The common dispatcher already owned their synchronous effects, but did not
return their values to the ordered caller.

`RuntimeOperation::Call` now accepts evaluated callee/argument snapshots and
returns a single payload containing effects and an optional value transfer.
The canonical call record supplies its existing policy and proof sites. Known
native results use the same checked resolution that discharged their effects;
foreign summaries retain their established argument/capture capabilities.
Local calls retain the singleton and taint guards and use one invocation.
Values from incomplete local calls remain capability roots for fallback.

Additional result projections run only for callers requesting values. A complete effect model with
no value transfer still truncates an ordered caller; it never retries the
general value evaluator. Result completeness grants no allocation-only proof.
Strict provider queries still decline call operations.

## F25: a source-call cache has the wrong scope

Caching a return under a raw source call site would conflate distinct function
activations, constructor/model activations and hypothetical value queries.
Adding a prefix revision would instead cause stateful calls to miss after
their own writes. Neither key makes an effects pass and a later value query
one evaluation. Use explicit operand/result transport and migrate the caller's
control flow onto ordered operations. The flattened root and general value
paths are still unconverted; no exact loader-cache claim follows from this
change.

## F26: timing completeness is not result coverage

Some existing helpers, including array iteration and from-entries callback
proofs, evaluate source operands internally. Calling them with already
evaluated operands could repeat a stateful child. Ordered dispatch skips those
helpers until they accept evaluated inputs and return their result. Host
timing assertions likewise supply no return value by themselves.

`missing_call_result` now distinguishes a completed timing proof without a
value transfer from an unknown callee, at the call's existing source point.
The ordered target-refusal path remains distinct internally; its initial
`complete` flag alone establishes no timing proof. Details and the source
review boundary are in <2026_11_02_missing_call_result_diagnostic.md>.
The helpers still need actual operand/result transport. Do not respond to
this refusal by retrying source evaluation or adding a premise.

## Review

Reviewed source-policy reuse, callee selection before arguments, native guard
reuse, local taint/singleton admission, rebind propagation, partial-result
retention and unsupported-result truncation. No tests, builds, corpus runs,
profiling or benchmarks were added or run locally. Normal hosted PR CI supplies
existing checks. General root execution, cross-chunk callable state, getter
identity and canonical gate acceptance remain required work.
